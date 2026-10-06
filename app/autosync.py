"""后台同步：服务启动先补一次，之后按设定间隔定时跑。

为什么放在这里而不是塞进 main.py：它需要自己的生命周期（一个 daemon 线程 +
一份可查询状态），而且设定是运行时可改的 —— 改完不用重启，下一个 tick 就生效。

和手动同步（POST /api/sync）的关系：
  · 共用 app/context.py 的 ctx.sync.try_begin() 这把旗，任何时刻只有一个 IMAP 会话在拉信；
  · 抢不到旗就直接跳过本轮（不是错误），日志里记一句，下一轮再来。

设定在 config/app.toml 的 [sync] 段（也可在 WebUI ⚙ 里改）：
    auto_enabled / interval_minutes / auto_since_days
    startup_enabled / startup_since_days / with_body / body_limit
"""
from __future__ import annotations

import threading
import time

from . import sync as syncmod
from .appconfig import SyncConfig, sync_config
from .context import ctx

TICK = 5.0          # 调度循环的检查间隔（秒）。远小于最小 60s 的同步间隔，
                    # 目的是「改完设定很快生效」，不用重启服务
MAX_ERRORS = 10     # 状态里最多留几条失败记录


_state: dict = {
    "started": False,
    "running": False,          # 本模块发起的同步是否正在进行
    "runs": 0,                 # 累计跑了几轮
    "last_run": None,          # 最近一轮的结果摘要
    "next_run_at": 0.0,        # 下一轮的时间戳（0 = 未安排，比如自动同步已关闭）
    "errors": [],              # 最近的失败信息
}
_lock = threading.Lock()


def _clip_errors(errors: list[str]) -> list[str]:
    return errors[-MAX_ERRORS:]


def _aggregate(results: list[dict]) -> dict:
    return {
        "new": sum(r.get("new", 0) for r in results),
        "updated": sum(r.get("updated", 0) for r in results),
        "bodies": sum(r.get("bodies", 0) for r in results),
        "attachments": sum(r.get("attachments", 0) for r in results),
        "errors": [f"[{r.get('folder')}] {e}" for r in results for e in r.get("errors", []) if e],
    }


def run_once(
    since: str,
    reason: str = "auto",
    folders: list[str] | None = None,
    force: bool = False,
    cfg: SyncConfig | None = None,
) -> dict | None:
    """跑一轮同步。抢不到同步旗（有人在同步）时返回 None。"""
    cfg = cfg or sync_config()
    if not ctx.sync.try_begin():
        ctx.sync.log_add(f"[自动同步·{reason}] 跳过：已有同步在进行")
        return None

    with _lock:
        _state["running"] = True
    t0 = time.time()
    ctx.sync.log_add(f"[自动同步·{reason}] 开始，since={since}")
    try:
        s = ctx.store()

        def progress(folder: str, msg: str) -> None:
            ctx.sync.log_add(f"[自动同步·{folder}] {msg}")

        with ctx.imap_write() as c:
            if not folders:
                fresh = {f["name"]: f for f in c.list_folders()}
                folders = list(fresh)
            else:
                fresh = {}
            results = syncmod.sync_all(
                c,
                s,
                folders=folders,
                since=since,
                body_limit=cfg.body_limit or None,
                force=force,
                with_body=cfg.with_body,
                progress=progress,
            )
        if fresh:
            # 缓存键与 api_mail.CACHE_KEY_FOLDERS_REMOTE 一致（api_mail 会 import 本模块，
            # 反向 import 会成环，所以这里写字面量）
            ctx.cache.put("mail:folders:remote", fresh, ttl=300)
        agg = _aggregate(results)
        agg["at"] = time.time()
        agg["elapsed"] = round(time.time() - t0, 2)
        agg["since"] = since
        agg["reason"] = reason
        agg["folders"] = len(results)
        # 新邮件可能带来新联系人，顺手让缓存失效（和手动同步同一套）
        ctx.cache.invalidate("mail:contacts")
        ctx.sync.last_sync = {"at": agg["at"], "results": results}
        with _lock:
            _state["last_run"] = agg
            _state["runs"] += 1
        ctx.sync.log_add(
            f"[自动同步·{reason}] 完成：新增 {agg['new']} / 正文 {agg['bodies']} / "
            f"附件 {agg['attachments']}（{agg['elapsed']}s）"
        )
        return agg
    except Exception as e:      # 后台线程绝不能因为一次异常就死掉
        ctx.sync.log_add(f"[自动同步·{reason}] 失败：{e}")
        with _lock:
            _state["errors"] = _clip_errors(_state["errors"] + [f"{time.strftime('%m-%d %H:%M')} {reason}: {e}"])
        return None
    finally:
        with _lock:
            _state["running"] = False
        ctx.sync.end()


def _loop() -> None:
    """调度循环：先跑启动同步，之后每 interval 分钟一轮。"""
    try:
        cfg = sync_config()
        if cfg.startup_enabled:
            run_once(cfg.startup_since, reason="startup", cfg=cfg)
        else:
            ctx.sync.log_add("[自动同步] 启动同步已关闭（config/app.toml [sync] startup_enabled）")
        with _lock:
            _state["next_run_at"] = time.time() + sync_config().interval_seconds
    except Exception as e:
        ctx.sync.log_add(f"[自动同步] 启动阶段异常：{e}")

    while True:
        time.sleep(TICK)
        try:
            cfg = sync_config()
            if not cfg.auto_enabled:
                # 关掉时把排期清空：重新打开后下一轮立刻同步一次，不用等满一个间隔
                with _lock:
                    if _state["next_run_at"]:
                        _state["next_run_at"] = 0.0
                        ctx.sync.log_add("[自动同步] 已按设定关闭")
                continue
            if time.time() < _state["next_run_at"]:
                continue
            run_once(cfg.auto_since, reason="auto", cfg=cfg)
            with _lock:
                _state["next_run_at"] = time.time() + sync_config().interval_seconds
        except Exception as e:
            ctx.sync.log_add(f"[自动同步] 调度异常：{e}")
            with _lock:
                _state["next_run_at"] = time.time() + 60


def start() -> bool:
    """启动后台同步线程（幂等：重复调用只生效一次）。"""
    with _lock:
        if _state["started"]:
            return False
        _state["started"] = True
    threading.Thread(target=_loop, name="autosync", daemon=True).start()
    return True


def status() -> dict:
    """给 /api/status 与设置面板看的一份状态。"""
    cfg = sync_config()
    with _lock:
        st = dict(_state)
    nxt = st.get("next_run_at") or 0
    return {
        "config": cfg.public(),
        "since": cfg.auto_since,
        "startup_since": cfg.startup_since,
        "started": st["started"],
        "running": st["running"],
        "runs": st["runs"],
        "last_run": st["last_run"],
        "next_run_in": max(0, round(nxt - time.time())) if nxt else None,
        "errors": list(st["errors"]),
    }
