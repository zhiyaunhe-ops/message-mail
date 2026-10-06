"""AI 与同步相关路由：配置读写 / 连通自检 / 写信正文生成。

配置落在 config/app.toml（应用级共同配置），也可以在 WebUI 右上角 ⚙ 里改：
  · [ai]      AI 接口（本文件）
  · [sync]    同步策略（本文件 + app/autosync.py 消费）
  · [display] 引用头时区（本文件；app/quoting.py 发信时消费）
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import ai as aimod
from . import autosync
from .appconfig import (
    ai_config,
    display_config,
    ensure_example,
    parse_utc_offset,
    save_ai,
    save_display,
    save_sync,
    sync_config,
)

router = APIRouter(tags=["ai"])


class AIConfigPatch(BaseModel):
    enabled: bool | None = None
    base_url: str | None = None
    api_key: str | None = Field(default=None, description="留空表示不修改已有密钥")
    model: str | None = None
    temperature: float | None = None
    timeout: int | None = None
    recent_count: int | None = None
    context_chars: int | None = None


class SyncConfigPatch(BaseModel):
    auto_enabled: bool | None = Field(default=None, description="后台定时自动同步开关")
    interval_minutes: int | None = Field(default=None, description="自动同步间隔（分钟，1-1440）")
    auto_since_days: int | None = Field(default=None, description="自动同步只回溯最近几天")
    startup_enabled: bool | None = Field(default=None, description="服务启动时同步一次")
    startup_since_days: int | None = Field(default=None, description="启动同步回溯最近几天")
    with_body: bool | None = Field(default=None, description="同步时连正文/附件一起抓")
    body_limit: int | None = Field(default=None, description="单目录每轮抓正文上限，0=不限")
    archive_enabled: bool | None = Field(default=None, description="群打包快照自动装入开关")
    archive_interval_minutes: int | None = Field(
        default=None, description="群打包快照扫描间隔（分钟，1-1440）")


class DisplayConfigPatch(BaseModel):
    utc_offset: str | None = Field(
        default=None,
        description='引用头 Sent: 用的时区偏移，如 "+08:00"；留空 = 按服务器本地时区',
    )


class ComposeRequest(BaseModel):
    to: str = ""
    cc: str = ""
    subject: str = ""
    body: str = ""
    recent: int | None = Field(default=None, ge=1, le=30, description="参考最近几封往来邮件，默认取配置值")


@router.get("/api/config/ai", summary="读取 AI 配置（密钥打码）")
def get_ai_config():
    ensure_example()
    return {"ok": True, "config": ai_config().public(), "file": "config/app.toml"}


@router.put("/api/config/ai", summary="写入 AI 配置（局部更新）")
def put_ai_config(patch: AIConfigPatch):
    try:
        cfg = save_ai(patch.model_dump(exclude_none=True))
    except Exception as e:
        raise HTTPException(500, f"保存配置失败：{e}")
    return {"ok": True, "config": cfg.public(), "file": "config/app.toml"}


@router.post("/api/config/ai/test", summary="测试 AI 接口连通性")
def test_ai_config():
    try:
        return aimod.ping()
    except aimod.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"测试失败：{e}")


@router.post("/api/compose/ai", summary="AI 生成邮件正文：3 个场景方案供选择")
def compose_ai(req: ComposeRequest):
    if not (req.to.strip() or req.cc.strip() or req.subject.strip() or req.body.strip()):
        raise HTTPException(400, "请先填写收件人或写点内容，AI 才有东西可参考")
    try:
        return aimod.draft_suggestions(
            to=req.to, cc=req.cc, subject=req.subject, body=req.body, recent=req.recent,
        )
    except aimod.AIError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"生成失败：{e}")


# ---------------------------------------------------------------- 同步配置
@router.get("/api/config/sync", summary="读取同步策略（自动同步间隔 / 回溯天数 / 启动同步）")
def get_sync_config():
    ensure_example()
    return {
        "ok": True,
        "config": sync_config().public(),
        "status": autosync.status(),
        "file": "config/app.toml",
    }


@router.put("/api/config/sync", summary="写入同步策略（局部更新，改完立即生效不用重启）")
def put_sync_config(patch: SyncConfigPatch):
    try:
        cfg = save_sync(patch.model_dump(exclude_none=True))
    except Exception as e:
        raise HTTPException(500, f"保存配置失败：{e}")
    return {
        "ok": True,
        "config": cfg.public(),
        "status": autosync.status(),
        "file": "config/app.toml",
    }


@router.post("/api/config/sync/now", summary="立刻跑一轮后台同步（等同自动同步的一轮）")
def run_sync_now():
    from .appconfig import sync_config as _cfg

    cfg = _cfg()
    r = autosync.run_once(cfg.auto_since, reason="manual")
    if r is None:
        return {"ok": False, "reason": "已有同步在进行", "status": autosync.status()}
    return {"ok": True, "run": r, "status": autosync.status()}


# ---------------------------------------------------------------- 展示配置
@router.get("/api/config/display", summary="读取展示配置（回复/转发引用头用哪个时区的 Sent 时间）")
def get_display_config():
    ensure_example()
    return {"ok": True, "config": display_config().public(), "file": "config/app.toml"}


@router.put("/api/config/display", summary="写入展示配置（改完下一次发信就生效，不用重启）")
def put_display_config(patch: DisplayConfigPatch):
    off = (patch.utc_offset or "").strip()
    # 填错了要当场说清楚，别等到引用头里印出一个偏移几小时的 Sent 时间才发现
    if off and parse_utc_offset(off) is None:
        raise HTTPException(
            400,
            f"时区偏移填得不对：{off}（要写成 +08:00 / +8 / -07:00；留空 = 按服务器本地时区）",
        )
    try:
        cfg = save_display({"utc_offset": off})
    except Exception as e:
        raise HTTPException(500, f"保存配置失败：{e}")
    return {"ok": True, "config": cfg.public(), "file": "config/app.toml"}
