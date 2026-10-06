"""邮件模块路由：目录 / 列表 / 详情 / 附件 / 发信 / 会话 / 分类 / AI 端点。

全部挂在同一个 FastAPI 服务里（app/main.py 装配），共享 app/context.py 的运行上下文。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import appconfig
from . import autosync
from . import mailout
from . import quoting
from . import sync as syncmod
from .classify import CATEGORY_EMOJI, CATEGORY_LABELS, label as cat_label
from .context import AppContext, account, ctx, is_mine, own_emails, store
from .deps import get_account, get_ctx, get_store
from .services import (
    att_url,
    build_ai_inbox,
    build_ai_threads,
    reindex,
    resolve_cids,
    split_attachments,
    truncate,
)
from .settings import Account
from .store import Store
from .threads import rebuild as rebuild_threads

router = APIRouter(tags=["mail"])

# 默认时间窗 = 近 1 月（滚动值，不再是写死的某一天）。
# 之前硬编码 "2026-07-01"，日子一长这个默认值会越来越离谱。
DEFAULT_SINCE_DAYS = 30


def _rolling_since(days: int = DEFAULT_SINCE_DAYS) -> str:
    from .appconfig import since_days_to_date

    return since_days_to_date(days)


DEFAULT_SINCE = _rolling_since()

def _enrich_categories(msg: dict) -> dict:
    cat = msg.get("category") or "personal"
    msg["category"] = cat
    msg["category_label"] = cat_label(cat)
    msg["category_emoji"] = CATEGORY_EMOJI.get(cat, "✉️")
    msg["is_boring"] = bool(msg.get("is_boring"))
    msg["mine"] = is_mine(msg)
    return msg


@router.get("/api/ai/schema", summary="AI 接口自述：可直接照此调用")
def ai_schema():
    base = "/api"
    return {
        "service": "mail-api",
        "account": {"email": account().email, "display_name": account().display_name},
        "conventions": {
            "time": "ISO8601，默认 UTC 偏移已含在字符串里；since/until 接受 'YYYY-MM-DD' 或 ISO datetime。"
                    f"不传 since 时同步接口默认近 1 月（{DEFAULT_SINCE}）",
            "folder": "服务端原名，如 INBOX / Sent Items / Drafts；folder 参数可重复传入",
            "uid": "IMAP UID，仅在所属 folder 内唯一；全局 id 形如 'INBOX:1234'",
            "pagination": "limit + offset，响应含 total",
            "category": "personal=人工邮件；meeting=会议/日程；automated=系统自动；promotion=营销推广。"
                        "后三类合称「无聊邮件」(is_boring=true)，可用 ?boring=true/false 或 ?category= 过滤",
            "thread_id": "会话 ID，同一来回复用一封；thread_id 形如 't'+14 位十六进制",
            "mine": "true 表示这封是本人发出的（发件人是自己，或在已发送/草稿目录里）",
        },
        "endpoints": [
            {"method": "GET", "path": f"{base}/ai/inbox", "desc": "AI 首选：一次拿到结构化邮件列表（含清洗后的正文）",
             "params": ["since", "until", "folder", "q", "unread_only", "has_attachment", "limit", "offset", "body_chars", "include_html"]},
            {"method": "GET", "path": f"{base}/ai/threads", "desc": "会话视图：同一来回复用一封，按时间顺序给出聊天式内容",
             "params": ["since", "until", "folder", "q", "unread_only", "boring", "category", "only_grouped", "limit", "offset", "body_chars", "max_messages"]},
            {"method": "GET", "path": f"{base}/threads", "desc": "会话列表（不含正文，轻量）",
             "params": ["folder", "since", "until", "q", "unread_only", "boring", "category", "only_grouped", "limit", "offset", "preview"]},
            {"method": "GET", "path": f"{base}/threads/{{thread_id}}", "desc": "单条会话全文，聊天视图直接消费",
             "params": ["body_chars", "body_format"]},
            {"method": "GET", "path": f"{base}/categories", "desc": "分类统计：无聊邮件占比、各类数量、会话数"},
            {"method": "POST", "path": f"{base}/reindex", "desc": "重建派生索引（分类 + 会话归组）",
             "body": {"classify_all": False, "rebuild": True}},
            {"method": "GET", "path": f"{base}/ai/digest", "desc": "聚合视图：按发件人/日期/主题分组统计，适合快速概览",
             "params": ["since", "until", "folder", "q", "group_by", "top"]},
            {"method": "GET", "path": f"{base}/messages", "desc": "信封级列表（不含正文，轻量）",
             "params": ["folder", "since", "until", "q", "unread_only", "has_attachment", "limit", "offset", "order"]},
            {"method": "GET", "path": f"{base}/messages/{{uid}}", "desc": "单封全文（正文 + 附件清单）",
             "params": ["folder", "include_body", "body_format"]},
            {"method": "GET", "path": f"{base}/messages/{{uid}}/attachment/{{index}}", "desc": "下载附件"},
            {"method": "POST", "path": f"{base}/messages/{{uid}}/flags", "desc": "标记已读/未读、加星标",
             "body": {"folder": "INBOX", "add": ["\\Seen"], "remove": []}},
            {"method": "DELETE", "path": f"{base}/messages/{{uid}}?folder=INBOX", "desc": "删除邮件（COPY 到回收站 + \\Deleted + EXPUNGE）"},
            {"method": "POST", "path": f"{base}/send", "desc": "发邮件（SMTP 465，multipart；可选附件；副本自动存回已发送）",
             "fields": ["to(与 cc 至少填一个，逗号分隔)", "cc", "subject", "body",
                        "reply_folder", "reply_uid", "forward_folder", "forward_uid",
                        "draft_folder", "draft_uid", "files[]"],
             "returns": {"mode": "reply / forward / ''", "accepted": "真正投递的收件人",
                         "skipped": "服务器当场拒收的", "dropped": "不像地址、已被剔除的串"}},
            {"method": "POST", "path": f"{base}/drafts", "desc": "存草稿（APPEND 到草稿箱；带 draft_uid 时替换上一版，不堆积）",
             "body": {"to": "", "cc": "", "subject": "", "body": "", "draft_folder": "", "draft_uid": 0,
                      "reply_folder": "", "reply_uid": 0, "forward_folder": "", "forward_uid": 0}},
            {"method": "POST", "path": f"{base}/compose/ai", "desc": "AI 生成正文：读收件人最近 N 封往来邮件 + 当前草稿，返回 3 个场景方案",
             "body": {"to": "", "cc": "", "subject": "", "body": "", "recent": 10}},
            {"method": "GET", "path": f"{base}/config/ai", "desc": "读 AI 配置（密钥打码；来源 config/app.toml）"},
            {"method": "PUT", "path": f"{base}/config/ai", "desc": "改 AI 配置（base_url/api_key/model/temperature/timeout…）"},
            {"method": "POST", "path": f"{base}/config/ai/test", "desc": "测试 AI 接口连通性"},
            {"method": "GET", "path": f"{base}/config/sync", "desc": "读同步策略与自动同步运行状态（间隔 / 回溯天数 / 启动同步 / 下一轮倒计时）"},
            {"method": "PUT", "path": f"{base}/config/sync", "desc": "改同步策略（改完立即生效，无需重启）",
             "body": {"auto_enabled": True, "interval_minutes": 10, "auto_since_days": 1,
                      "startup_enabled": True, "startup_since_days": 7, "with_body": True}},
            {"method": "POST", "path": f"{base}/config/sync/now", "desc": "立刻跑一轮自动同步（用 auto_since_days 的时间窗）"},
            {"method": "GET", "path": f"{base}/config/display", "desc": "读展示配置：回复/转发引用头里 Sent: 用哪个时区",
             "returns": {"utc_offset": "填的值", "effective": "解析出来的偏移，空 = 按服务器本地", "source": "default / file / env"}},
            {"method": "PUT", "path": f"{base}/config/display", "desc": "改引用头时区（收固定偏移；填错 400）",
             "body": {"utc_offset": "+08:00"}},
            {"method": "GET", "path": f"{base}/folders", "desc": "所有目录及计数 / 同步状态"},
            {"method": "DELETE", "path": f"{base}/folders/{{name}}", "desc": "删除一个目录（服务器 + 本地索引；系统目录不可删；?local_only=true 只清本地，?empty=false 禁止先清空）"},
            {"method": "GET", "path": f"{base}/accounts", "desc": "邮箱账号列表 + 当前选中的那个"},
            {"method": "POST", "path": f"{base}/accounts", "desc": "新增邮箱账号", "body": {"name": "work", "email": "a@b.com", "imap_host": "imap.example.com", "imap_port": 993, "password": "..."}},
            {"method": "PUT", "path": f"{base}/accounts/{{name}}", "desc": "修改邮箱账号（password 留空=不改）"},
            {"method": "DELETE", "path": f"{base}/accounts/{{name}}", "desc": "删除邮箱账号（?purge=true 连本地索引库一起删）"},
            {"method": "POST", "path": f"{base}/accounts/{{name}}/activate", "desc": "切换当前邮箱账号（换本地索引库）"},
            {"method": "POST", "path": f"{base}/accounts/{{name}}/test", "desc": "测试该账号 IMAP 连通性"},
            {"method": "GET", "path": f"{base}/contacts", "desc": "联系人联想：从历史邮件汇总的收件人/抄送候选（写信补全用）",
             "params": ["q", "limit"]},
            {"method": "POST", "path": f"{base}/sync", "desc": "从 IMAP 增量同步到本地索引（since 不传 = 近 1 月）",
             "body": {"folder": "INBOX", "since": DEFAULT_SINCE, "limit": syncmod.DEFAULT_ENVELOPE_LIMIT,
                      "force": False, "with_body": True}},
            {"method": "GET", "path": f"{base}/status", "desc": "服务与索引健康状态（含 auto_sync 自动同步状态）"},
        ],
        "tips": [
            f"想读近 1 月的全部邮件：GET /api/ai/inbox?since={DEFAULT_SINCE}&limit=200",
            f"只想看人写的邮件（滤掉会议通知/系统自动/营销）：GET /api/ai/inbox?since={DEFAULT_SINCE}&boring=false",
            f"想按会话读（推荐，省 token）：GET /api/ai/threads?since={DEFAULT_SINCE}&only_grouped=true",
            "正文默认截断到 body_chars（默认 4000）字符，需要全文再调 /api/messages/{uid}",
            "搜索 q 会同时匹配主题、发件人、收件人和正文",
            "转发某封：POST /api/send 带 forward_folder + forward_uid（主题补 Fwd:，不挂 In-Reply-To，所以不会被并回原会话）；回复同理换成 reply_folder + reply_uid",
            "收件人显示名里带逗号的要加引号（\"Sample, Steven\" <a@b.com>），不加会被解析成两个人",
            "服务默认每 10 分钟自动同步近 1 天、启动时自动同步近 1 周；可用 PUT /api/config/sync 调整",
        ],
    }


# ---------------------------------------------------------------- 状态
@router.get("/api/status", summary="服务与索引状态")
def status(
    s: Store = Depends(get_store),
    acc: Account = Depends(get_account),
    app_ctx: AppContext = Depends(get_ctx),
):
    folders = []
    for f in s.list_folders():
        c = s.folder_counts(f["name"])
        folders.append(
            {
                "name": f["name"],
                "total": c["total"],
                "unread": c["unread"],
                "uidvalidity": f["uidvalidity"],
                "last_synced": f["last_synced"],
                "last_error": f["last_error"],
            }
        )
    return {
        "ok": True,
        "service": "message-webui",
        "modules": {"mail": {"available": True}},
        "account": {"name": acc.name, "email": acc.email, "display_name": acc.display_name},
        "accounts": _safe_accounts(),
        "active_account": acc.name,
        "imap": {"host": acc.imap_host, "port": acc.imap_port, "ssl": acc.imap_ssl},
        "smtp": {"host": acc.smtp_host, "port": acc.smtp_port, "ssl": acc.smtp_ssl},
        "stats": s.stats(),
        "categories": s.category_stats(),
        "threads": s.thread_stats(),
        "folders": folders,
        "syncing": app_ctx.sync.syncing,
        "last_sync": app_ctx.sync.last_sync,
        "auto_sync": autosync.status(),
        "log": app_ctx.sync.recent_log(20),
        "cache_keys": app_ctx.cache.keys(),
    }


def _safe_accounts() -> list[dict]:
    """账号清单（不含明文密码）；读配置失败不该让 /api/status 整个倒掉。"""
    try:
        from .settings import list_accounts

        return list_accounts()
    except Exception:
        return []


CACHE_KEY_FOLDERS_REMOTE = "mail:folders:remote"
FOLDERS_TTL = 300       # 远端目录清单缓存秒数


def remote_folders(refresh: bool = False) -> dict:
    """远端目录清单（IMAP LIST），带 TTL 缓存。

    LIST 本身几乎不变，但每取一次都要 建 TLS 连接 + LOGIN + LIST —— 这是整页加载里
    最慢的一步（冷启动实测 2.3s）。缓存命中时首屏只剩本地 SQLite 计数（毫秒级）。
    回源失败时退回上一次快照，避免网络抖一下侧栏就变空。
    """
    if not refresh:
        hit = ctx.cache.get(CACHE_KEY_FOLDERS_REMOTE)
        if hit is not None:
            return hit
    try:
        with ctx.imap() as c:
            remote = {f["name"]: f for f in c.list_folders()}
    except Exception as e:
        ctx.sync.log_add(f"list_folders failed: {e}")
        return ctx.cache.get(CACHE_KEY_FOLDERS_REMOTE) or {}
    if remote:
        ctx.cache.put(CACHE_KEY_FOLDERS_REMOTE, remote, ttl=FOLDERS_TTL)
    return remote


@router.get("/api/folders", summary="目录列表与计数")
def folders(refresh: bool = Query(False, description="跳过缓存，强制回源 IMAP 重取目录清单")):
    s = store()
    known = {f["name"]: f for f in s.list_folders()}
    remote = remote_folders(refresh=refresh)
    out = []
    for name in sorted(set(known) | set(remote)):
        cnt = s.folder_counts(name)
        synced = name in known
        out.append(
            {
                "name": name,
                "total": cnt["total"],
                "unread": cnt["unread"],
                "synced": synced,
                "last_synced": known[name]["last_synced"] if synced else 0,
                "flags": remote.get(name, {}).get("flags", []),
            }
        )
    return {"folders": out, "count": len(out), "cached": not refresh and CACHE_KEY_FOLDERS_REMOTE in ctx.cache.keys()}


# 不允许通过接口删掉的「系统目录」（收件箱 + 会话配置里用到的别名目录）
_PROTECTED_ALIASES = ("inbox", "sent", "drafts", "trash")


def _protected_folders() -> set[str]:
    acc = account()
    out = {"INBOX"}
    for key in _PROTECTED_ALIASES:
        v = acc.aliases.get(key)
        if v:
            out.add(v)
    return out


@router.delete("/api/folders/{folder:path}", summary="删除目录（服务器 + 本地索引，不可逆）")
def delete_folder(
    folder: str,
    local_only: bool = Query(False, description="只清本地索引，不动服务器目录"),
    empty: bool = Query(True, description="直接删不动时，先清空目录里的邮件再删（多数服务端要求目录为空）"),
):
    """把一个目录从服务器和本地索引里移除。

    收件箱以及别名里配置的已发送/草稿/回收站不允许删除 —— 那些是发信流程要用的。
    """
    s = store()
    if folder in _protected_folders():
        raise HTTPException(400, f"「{folder}」是系统目录，不能删除")
    known = {f["name"] for f in s.list_folders()}
    remote = remote_folders()
    if folder not in known and folder not in remote:
        raise HTTPException(404, f"未找到目录「{folder}」")

    server = {"ok": False, "skipped": True}
    if not local_only:
        try:
            with ctx.imap_write() as c:
                try:
                    server = c.delete_folder(folder)
                except Exception as first:
                    # Coremail 这类服务端不允许删非空目录（"DELETE can't delete mailbox
                    # with letter in it"）。既然用户要连邮件一起删，就先清空再删。
                    if not empty:
                        raise
                    ctx.sync.log_add(f"目录 {folder} 直接删除失败（{first}），改为先清空再删")
                    purged = c.purge_folder(folder)
                    server = c.delete_folder(folder)
                    server["purged"] = purged["purged"]
        except Exception as e:
            raise HTTPException(500, f"服务器端删除失败：{e}")
    local = s.drop_folder(folder)
    ctx.cache.invalidate(CACHE_KEY_FOLDERS_REMOTE)      # 目录清单变了
    ctx.cache.invalidate(CACHE_KEY_CONTACTS)
    ctx.sync.log_add(
        f"删除目录 {folder}（服务器清空 {server.get('purged', 0)} 封，本地移除 {local['removed']} 行）"
        if not local_only else
        f"删除目录 {folder}（仅本地，移除 {local['removed']} 行）"
    )
    return {"ok": True, "folder": folder, "server": server, "local": local}


# ---------------------------------------------------------------- 联系人联想
CACHE_KEY_CONTACTS = "mail:contacts"
CONTACTS_TTL = 600      # 本地聚合，纯 CPU；同步/发信后会主动失效


def _contacts() -> list[dict]:
    hit = ctx.cache.get(CACHE_KEY_CONTACTS)
    if hit is None:
        hit = store().contact_index(own_emails())
        ctx.cache.put(CACHE_KEY_CONTACTS, hit, ttl=CONTACTS_TTL)
    return hit


@router.get("/api/contacts", summary="联系人联想：历史邮件的收件人/抄送候选")
def list_contacts(
    q: str = Query("", description="姓名或邮箱片段；可用空格分词，全部命中才算匹配"),
    limit: int = Query(default=8, ge=1, le=50),
):
    """写信时收件人 / 抄送的自动补全数据源。

    排序：前缀命中 > 词内命中；同档按往来次数、最近通信时间。q 为空则直接给最常联系的人。
    """
    index = _contacts()
    tokens = [t for t in re.split(r"[\s,;，；]+", (q or "").strip().lower()) if t]
    if not tokens:
        return {"total": len(index), "query": q, "items": index[:limit]}

    hits: list[tuple] = []
    for c in index:
        name = (c["name"] or "").lower()
        email = c["email"]
        hay = f"{name} {email}"
        if not all(t in hay for t in tokens):
            continue
        if all(name.startswith(t) for t in tokens) or all(email.startswith(t) for t in tokens):
            tier = 0        # 各词全是前缀命中
        elif name.startswith(tokens[0]) or email.startswith(tokens[0]):
            tier = 1        # 首词前缀命中
        else:
            tier = 2        # 仅词内命中
        hits.append((tier, -c["addressed"], -c["weight"], -c["count"], -c["last_ts"], c))
    hits.sort(key=lambda x: x[:5])
    return {"total": len(hits), "query": q, "items": [h[5] for h in hits[:limit]]}


# ---------------------------------------------------------------- 同步
class SyncRequest(BaseModel):
    folder: str | list[str] | None = None
    since: str | None = Field(
        default=None,
        description=f"只抓这个日期之后的正文/附件；不传 = 近 1 月（{DEFAULT_SINCE}）",
    )
    limit: int = syncmod.DEFAULT_ENVELOPE_LIMIT   # 单目录单轮处理上限，即「信箱容量」
    body_limit: int | None = None
    force: bool = False
    with_body: bool = True
    all_folders: bool = False


@router.post("/api/sync", summary="增量同步 IMAP -> 本地索引")
def do_sync(req: SyncRequest):
    # 手动同步与后台自动同步共用这把旗：抢不到说明后台正在跑，让用户等几秒就行
    if not ctx.sync.try_begin():
        raise HTTPException(429, "同步正在进行中（可能是后台自动同步），稍等几秒再试")
    t0 = time.time()
    try:
        s = store()
        since = req.since or _rolling_since()

        def progress(folder: str, msg: str):
            ctx.sync.log_add(f"[{folder}] {msg}")

        # 写操作：独立连接 + 全局串行（同一时刻只跑一个 IMAP 写动作）
        with ctx.imap_write() as c:
            fresh: dict = {}
            if req.all_folders or req.folder is None:
                fresh = {f["name"]: f for f in c.list_folders()}
                targets = list(fresh)
            elif isinstance(req.folder, str):
                targets = [req.folder]
            else:
                targets = list(req.folder)
            results = [
                syncmod.sync_folder(
                    c,
                    s,
                    f,
                    since=since,
                    envelope_limit=req.limit,
                    body_limit=req.body_limit,
                    force=req.force,
                    with_body=req.with_body,
                    progress=progress,
                )
                for f in targets
            ]
        # 同步时顺手拿到的目录清单直接更新缓存，省掉 /api/folders 的一次回源
        if fresh:
            ctx.cache.put(CACHE_KEY_FOLDERS_REMOTE, fresh, ttl=FOLDERS_TTL)
        ctx.cache.invalidate(CACHE_KEY_CONTACTS)   # 新邮件可能带来新联系人
        ctx.sync.last_sync = {"at": time.time(), "results": results}
        return {
            "ok": True,
            "since": since,
            "elapsed": round(time.time() - t0, 2),
            "folders": results,
            "new": sum(r["new"] for r in results),
            "bodies": sum(r["bodies"] for r in results),
            "attachments": sum(r["attachments"] for r in results),
        }
    except Exception as e:
        raise HTTPException(500, f"同步失败: {e}")
    finally:
        ctx.sync.end()


# ---------------------------------------------------------------- 邮件列表 / 详情
@router.get("/api/messages", summary="信封级邮件列表")
def list_messages(
    folder: list[str] | None = Query(default=None, description="可重复；默认 INBOX"),
    since: str | None = Query(default=None, examples=["2026-07-01"]),
    until: str | None = None,
    q: str | None = None,
    unread_only: bool = False,
    has_attachment: bool | None = None,
    category: str | None = Query(default=None, pattern="^(personal|meeting|automated|promotion)$"),
    boring: bool | None = Query(default=None, description="true=只看无聊邮件，false=只看人工邮件"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    order: str = "desc",
):
    s = store()
    folders = folder or ["INBOX"]
    total, items = s.list_messages(
        folders=folders,
        since=since,
        until=until,
        q=q,
        unread_only=unread_only,
        has_attachment=has_attachment,
        category=category,
        boring=boring,
        limit=limit,
        offset=offset,
        order=order,
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "count": len(items),
        "items": [_enrich_categories(m) for m in items],
    }


@router.get("/api/messages/{uid}", summary="单封邮件全文")
def get_message(
    uid: int,
    folder: str = "INBOX",
    include_body: bool = True,
    body_format: str = Query(default="both", pattern="^(text|html|both)$"),
):
    s = store()
    msg = s.get_message(folder, uid)
    if not msg:
        raise HTTPException(404, f"未找到邮件 {folder}:{uid}（可能需要先同步）")
    out = dict(msg)
    atts = s.get_attachments(folder, uid)
    real, inline = split_attachments(atts, folder, uid)
    if include_body:
        body = s.get_body(folder, uid)
        if body_format in ("text", "both"):
            out["body_text"] = body["body_text"]
        if body_format in ("html", "both"):
            # cid: 已替换为可直接引用的 URL，前端可直接把这段 HTML 渲染进正文
            out["body_html"] = resolve_cids(body["body_html"], folder, uid, atts)
        out["headers"] = body["headers"]
    out["attachments"] = real          # 真实附件（下载用）
    out["inline_images"] = inline      # 内嵌资源（已渲染进正文，不单独列出）
    out["attachment_count"] = len(real)
    out["inline_count"] = len(inline)
    _enrich_categories(out)
    return out


@router.get("/api/messages/{uid}/attachment/{index}", summary="下载附件")
def get_attachment(uid: int, index: int, folder: str = "INBOX", disposition: str = "attachment"):
    s = store()
    atts = [a for a in s.get_attachments(folder, uid) if a["index"] == index]
    if not atts:
        raise HTTPException(404, "附件不存在")
    a = atts[0]
    p = Path(a["path"]) if a["path"] else None
    if not p or not p.exists():
        raise HTTPException(404, "附件文件尚未落盘，请重新同步")
    # disposition=inline 用于正文里的 <img src>，不带 filename 浏览器才会直接显示
    fname = None if disposition == "inline" else a["filename"]
    return FileResponse(p, filename=fname, media_type=a["content_type"] or "application/octet-stream")


class FlagsRequest(BaseModel):
    folder: str = "INBOX"
    add: list[str] = []
    remove: list[str] = []


@router.post("/api/messages/{uid}/flags", summary="修改标志（已读/未读/星标）")
def set_flags(uid: int, req: FlagsRequest):
    s = store()
    msg = s.get_message(req.folder, uid)
    if not msg:
        raise HTTPException(404, "邮件不存在")
    try:
        with ctx.imap_write() as c:
            flags = c.set_flags(uid, add=req.add, remove=req.remove, folder=req.folder)
        s.set_flags(req.folder, uid, flags)
    except Exception as e:
        raise HTTPException(500, f"设置标志失败: {e}")
    return {"ok": True, "uid": uid, "folder": req.folder, "flags": flags, "unread": "\\Seen" not in flags}


@router.delete("/api/messages/{uid}", summary="删除邮件（移入回收站）")
def trash_message(uid: int, folder: str = "INBOX"):
    s = store()
    msg = s.get_message(folder, uid)
    if not msg:
        raise HTTPException(404, f"未找到邮件 {folder}:{uid}")
    try:
        with ctx.imap_write() as c:
            r = c.move_to_trash(uid, folder)
    except Exception as e:
        raise HTTPException(500, f"删除失败: {e}")
    s.delete_message(folder, uid)  # 服务器已移走，本地索引同步清掉
    return {"ok": True, "uid": uid, "folder": folder, "trash": r.get("trash"), "copied": r.get("copied")}


# ---------------------------------------------------------------- 发信（SMTP）
def _format_pair(name: str, addr: str) -> str:
    return addr if (not name or name == addr) else mailout.formataddr_safe(name, addr)


def _without_self(raw: str, me: str) -> str:
    """去掉收件人/抄送里的自己 —— 「回复全部」最容易把自己带上。"""
    if not raw or not me:
        return raw
    pairs = [(n, a) for n, a in mailout.parse_addresses(raw) if (a or "").strip().lower() != me]
    return ", ".join(_format_pair(n, a) for n, a in pairs)


def _subject_with(prefix: str, orig_subject: str) -> str:
    """按 Re: / Fwd: 加前缀（已经有了就不重复加）。

    Re: 认 `Re:` 与 `RE[2]:`；Fwd: 认 `Fwd:` 与 Outlook 的 `FW:` —— 不认的话
    转转发过的信会叠出「Fwd: FW: Fwd: …」。
    """
    sub = (orig_subject or "").strip()
    if not sub:
        return ""
    if prefix.lower().startswith("fwd"):
        already = re.match(r"^(?:fwd?|fw)\s*:", sub, re.I)
    else:
        already = re.match(r"^re(?:\[\d+\])?\s*:", sub, re.I)
    return sub if already else f"{prefix} {sub}"


def _thread_headers(mode: str, orig: dict | None) -> tuple[str, str]:
    """回复才挂 In-Reply-To / References（对方客户端据此并进同一条会话）。

    转发是一封新邮件 —— 挂了就会把「转给外人」的那封塞回原会话里，收件方看到
    一个不属于自己的线程；所以转发一律不挂。
    """
    if mode == "reply" and orig and orig.get("message_id"):
        return orig["message_id"], orig["message_id"]
    return "", ""


def _context_subject(mode: str, subject: str, orig_subject: str) -> str:
    """用户没写主题时，按 mode 补上 Re: / Fwd:（写了就尊重用户写的）。"""
    if subject or not orig_subject:
        return subject
    return _subject_with("Re:" if mode == "reply" else "Fwd:", orig_subject)


@router.post("/api/send", summary="发邮件（SMTP，可选附件；支持回复 / 转发上下文，自动存副本到已发送）")
def send_mail(
    to: str = Form("", description="收件人，多个用逗号分隔；支持 Name <a@b.com> 形式"),
    cc: str = Form("", description="抄送，同上"),
    subject: str = Form(""),
    body: str = Form("", description="纯文本正文；直接换行即分段"),
    reply_folder: str = Form("", description="若为回复：原信所在目录"),
    reply_uid: int = Form(0, description="若为回复：原信 UID"),
    forward_folder: str = Form("", description="若为转发：原信所在目录"),
    forward_uid: int = Form(0, description="若为转发：原信 UID"),
    draft_folder: str = Form("", description="若这封由草稿发出：草稿所在目录（发完自动清理）"),
    draft_uid: int = Form(0, description="若这封由草稿发出：草稿 UID"),
    files: list[UploadFile] = File(default=[], description="附件，可多个"),
):
    acc = account()
    s = store()

    # 回复与转发共用一套「引用原文」的逻辑，只有主题前缀和线程头不同
    mode = "reply" if (reply_folder and reply_uid) else ("forward" if (forward_folder and forward_uid) else "")
    orig_folder = reply_folder if mode == "reply" else forward_folder
    orig_uid = reply_uid if mode == "reply" else forward_uid

    me = (acc.email or "").strip().lower()
    raw_to, raw_cc = to, cc
    to = _without_self(to, me)
    cc = _without_self(cc, me)
    # 自己也被剔掉之后一个收件人不剩：与其让下游报「收件人为空」，不如直接说清楚
    # （最常见的就是「回复全部」时删掉别人、只留下自己）
    if (raw_to.strip() or raw_cc.strip()) and not (to.strip() or cc.strip()):
        raise HTTPException(400, "收件人里只有你自己（发送时会自动去掉自己），请再填一个别的收件人")

    in_reply_to, references = "", ""
    orig_subject = ""
    orig = None
    if mode:
        orig = s.get_message(orig_folder, orig_uid)
        if orig:
            orig_subject = orig.get("subject") or ""
            in_reply_to, references = _thread_headers(mode, orig)

    attachments: list[tuple[str, str, bytes]] = []
    for f in files or []:
        try:
            data = f.file.read()  # 同步句柄，线程池端点里安全
        finally:
            try:
                f.file.close()
            except Exception:
                pass
        attachments.append((f.filename or "attachment.bin", f.content_type or "application/octet-stream", data))

    if orig_subject and not subject:
        subject = _context_subject(mode, subject, orig_subject)

    # 正文拼上引用（回复 / 转发同一份引用块）。text 与 html 各一份（html 才是
    # 收件方看到的那个，纯文本手工 > 前缀会被对方网关重排，见 app/quoting.py 的说明）。
    text = body or ""
    body_html = ""
    related: list[dict] = []
    if orig and (orig.get("from") or {}).get("email"):
        text, body_html, related = quoting.build_quote(
            body or "",
            orig,
            s.get_body(orig_folder, orig_uid),
            s.get_attachments(orig_folder, orig_uid),
            domain=acc.email.split("@")[-1],
            tz=appconfig.display_config().tz,
        )

    # 不像地址的收件人（少写了 @ 的那种）不进 To/Cc —— 否则 SMTP 会拿它当 RCPT
    # 回一句 550，前端只看到一个语焉不详的 400。这里挑出来，发完如实告诉用户。
    _, dropped_to = mailout.split_addresses(to)
    _, dropped_cc = mailout.split_addresses(cc)
    dropped = list(dict.fromkeys(dropped_to + dropped_cc))

    try:
        msg = mailout.build_message(
            acc, to=to, subject=subject, body_text=text, body_html=body_html,
            cc=cc, attachments=attachments, related=related,
            in_reply_to=in_reply_to, references=references,
        )
        raw = msg.as_bytes()
        result = mailout.send_message(acc, msg)
    except mailout.MailOutError as e:
        # 发信失败要留痕：收件人是谁、卡在哪一步 —— 光看前端那句红字查不出原因
        ctx.sync.log_add(f"send failed ({mode or 'new'}): to={to!r} cc={cc!r} -> {e}")
        raise HTTPException(400, str(e))
    except Exception as e:
        ctx.sync.log_add(f"send error ({mode or 'new'}): to={to!r} cc={cc!r} -> {e!r}")
        raise HTTPException(500, f"发送失败: {e}")

    # 副本存回已发送并同步到本地索引
    sent_folder = acc.aliases.get("sent", "Sent Items")
    sent_uid = None
    try:
        with ctx.imap_write() as c:
            sent_uid = c.append_message(raw, sent_folder)
    except Exception as e:
        ctx.sync.log_add(f"append sent failed: {e}")
    try:
        with ctx.imap_write() as c:
            syncmod.sync_folder(c, s, sent_folder, since=time.strftime("%Y-%m-%d"))
    except Exception as e:
        ctx.sync.log_add(f"sent sync failed: {e}")

    # 已发出 -> 草稿箱里的那一版没用了，直接清掉（失败不影响发信结果）
    draft_removed = False
    if draft_uid:
        dfolder = draft_folder or acc.aliases.get("drafts", "Drafts")
        try:
            with ctx.imap_write() as c:
                c.delete_message(draft_uid, dfolder)
            # 服务器删掉了还不够：前端读的是本地索引，不一起清掉草稿箱里还会留着这一版
            s.delete_message(dfolder, draft_uid)
            draft_removed = True
        except Exception as e:
            ctx.sync.log_add(f"draft cleanup failed: {e}")

    ctx.cache.invalidate(CACHE_KEY_CONTACTS)   # 刚联系过的人排到前面

    return {
        "ok": True,
        "message_id": result["message_id"],
        "accepted": result["accepted"],
        "skipped": result.get("skipped") or [],
        "dropped": dropped,          # 少了 @、被忽略掉的收件人（有事要在前端说一声）
        "mode": mode,
        "sent_folder": sent_folder,
        "sent_uid": sent_uid,
        "draft_removed": draft_removed,
        "web_url": f"/?folder={sent_folder}&uid={sent_uid}" if sent_uid else None,
    }


# ---------------------------------------------------------------- 草稿
class DraftIn(BaseModel):
    to: str = ""
    cc: str = ""
    subject: str = ""
    body: str = ""
    draft_folder: str = ""
    draft_uid: int = 0          # 上一版草稿的 UID，有的话会被新版本顶掉
    reply_folder: str = ""      # 这封本来是回复哪封：存进 X-Message-CLI-Reply-* 头
    reply_uid: int = 0          # （否则关闭窗口再从草稿箱打开，回复就变成新邮件了）
    forward_folder: str = ""    # 这封本来是转发哪封：X-Message-CLI-Forward-* 头
    forward_uid: int = 0        # （同理，丢了的话草稿重开就不知道引用的是哪封了）


@router.post("/api/drafts", summary="存草稿（APPEND 到草稿箱；带 draft_uid 则替换上一版）")
def save_draft(d: DraftIn):
    """写信界面边写边存：同一封草稿反复保存不会堆积，服务器上始终只留最新一版。"""
    acc = account()
    s = store()
    folder = d.draft_folder or acc.aliases.get("drafts", "Drafts")
    if not (d.to.strip() or d.cc.strip() or d.subject.strip() or d.body.strip()):
        raise HTTPException(400, "草稿是空的，无需保存")

    extra_headers: dict[str, str] = {}
    if d.reply_folder and d.reply_uid:
        extra_headers = {
            mailout.REPLY_FOLDER_HEADER: d.reply_folder,
            mailout.REPLY_UID_HEADER: str(d.reply_uid),
        }
    elif d.forward_folder and d.forward_uid:
        extra_headers = {
            mailout.FORWARD_FOLDER_HEADER: d.forward_folder,
            mailout.FORWARD_UID_HEADER: str(d.forward_uid),
        }

    try:
        msg = mailout.build_message(
            acc, to=d.to, subject=d.subject, body_text=d.body, cc=d.cc,
            extra_headers=extra_headers,
            allow_empty_recipients=True,
        )
    except mailout.MailOutError as e:
        raise HTTPException(400, str(e))
    raw = msg.as_bytes()

    try:
        with ctx.imap_write() as c:
            uid = c.append_message(raw, folder, flags=r"(\Seen \Draft)")
    except Exception as e:
        raise HTTPException(500, f"草稿保存失败：{e}")

    # APPEND 返回的 UID 是 uidnext-1 的推测值，同步后用 Message-ID 换回真实 UID
    try:
        with ctx.imap_write() as c:
            syncmod.sync_folder(c, s, folder, since=time.strftime("%Y-%m-%d"))
        row = s.conn.execute(
            "SELECT uid FROM messages WHERE folder=? AND message_id=?", (folder, msg["Message-ID"])
        ).fetchone()
        if row:
            uid = int(row["uid"])
    except Exception as e:
        ctx.sync.log_add(f"draft sync failed: {e}")

    # 顶掉上一版草稿
    if d.draft_uid and d.draft_uid != uid:
        try:
            with ctx.imap_write() as c:
                c.delete_message(d.draft_uid, folder)
            s.delete_message(folder, d.draft_uid)
        except Exception as e:
            ctx.sync.log_add(f"draft replace failed: {e}")

    return {
        "ok": True,
        "uid": uid,
        "folder": folder,
        "saved_at": time.strftime("%H:%M:%S"),
        "chars": len(d.body or ""),
    }


@router.get("/api/search", summary="全文搜索")
def search(
    q: str = Query(..., min_length=1),
    folder: list[str] | None = Query(default=None),
    since: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = 0,
):
    total, items = store().list_messages(folders=folder, since=since, q=q, limit=limit, offset=offset)
    return {"query": q, "total": total, "items": items}


# ---------------------------------------------------------------- AI 专用
# ---------------------------------------------------------------- 会话（聊天视图）
@router.get("/api/threads", summary="会话列表（同一来回复用一封，类似 IM 会话）")
def list_threads(
    folder: list[str] | None = Query(default=None, description="可重复；不传=全部目录"),
    since: str | None = Query(default=None, examples=["2026-07-01"]),
    until: str | None = None,
    q: str | None = None,
    unread_only: bool = False,
    category: str | None = Query(default=None, pattern="^(personal|meeting|automated|promotion)$"),
    boring: bool | None = Query(default=None, description="true=只看无聊邮件，false=只看人工邮件"),
    only_grouped: bool = Query(default=False, description="true=只看多于一封的会话"),
    limit: int = Query(default=50, ge=1, le=300),
    offset: int = Query(default=0, ge=0),
    preview: int = Query(default=3, ge=0, le=20, description="每条会话预览几封邮件"),
):
    s = store()
    total, threads = s.list_threads(
        folders=list(folder) if folder else None,
        since=since,
        until=until,
        q=q,
        unread_only=unread_only,
        boring=boring,
        category=category,
        own_emails=own_emails(),
        limit=limit,
        offset=offset,
        only_grouped=only_grouped,
    )
    items = []
    for t in threads:
        msgs = t["messages"]
        items.append(
            {
                "thread_id": t["thread_id"],
                "subject": t["subject"],
                "message_count": t["message_count"],
                "participants": t["participants"],
                "participant_count": t["participant_count"],
                "unread": t["unread"],
                "first_date": t["first_date"],
                "last_date": t["last_date"],
                "folders": t["folders"],
                "has_attachment": t["has_attachment"],
                "attachment_count": t["attachment_count"],
                "snippet": t["snippet"],
                "last_from": t["last_from"],
                "category": t["category"],
                "category_label": cat_label(t["category"]),
                "category_emoji": CATEGORY_EMOJI.get(t["category"], "✉️"),
                "is_boring": t["is_boring"],
                "preview": [
                    {
                        "id": m["id"],
                        "folder": m["folder"],
                        "uid": m["uid"],
                        "date": m["date"],
                        "from": m["from"],
                        "mine": is_mine(m),
                        "snippet": m["snippet"],
                    }
                    for m in (msgs[-preview:] if preview else [])
                ],
                "url": f"/api/threads/{t['thread_id']}",
                "web_url": f"/?thread={t['thread_id']}",
            }
        )
    return {"total": total, "limit": limit, "offset": offset,
            "count": len(items), "items": items}


@router.get("/api/threads/{thread_id}", summary="单条会话全文（聊天视图数据）")
def get_thread(
    thread_id: str,
    body_chars: int = Query(default=20000, ge=0, le=200000),
    body_format: str = Query(default="both", pattern="^(text|html|both)$"),
):
    s = store()
    t = s.get_thread(thread_id, own_emails=own_emails())
    if not t:
        raise HTTPException(404, f"未找到会话 {thread_id}（可能索引已重建）")
    msgs = t.pop("messages")
    out_msgs = []
    for m in msgs:
        body = s.get_body(m["folder"], m["uid"])
        atts = s.get_attachments(m["folder"], m["uid"])
        real, inline = split_attachments(atts, m["folder"], m["uid"])
        item = dict(m)
        if body_format in ("text", "both"):
            item["body_text"] = truncate(body["body_text"], body_chars)
        if body_format in ("html", "both"):
            item["body_html"] = resolve_cids(body["body_html"], m["folder"], m["uid"], atts)
        item["attachments"] = real
        item["inline_images"] = inline
        item["attachment_count"] = len(real)
        item["inline_count"] = len(inline)
        # 会话气泡里也要能「继续编辑」草稿 —— 那只认 headers.x_reply / x_forward
        item["headers"] = body["headers"]
        item["web_url"] = f"/?folder={m['folder']}&uid={m['uid']}"
        item["url"] = f"/api/messages/{m['uid']}?folder={m['folder']}"
        out_msgs.append(_enrich_categories(item))
    t["messages"] = out_msgs
    t["category_label"] = cat_label(t["category"])
    t["category_emoji"] = CATEGORY_EMOJI.get(t["category"], "✉️")
    t["participants"] = t["participants"][:8]
    return t


@router.post("/api/reindex", summary="重建本地派生索引（分类 + 会话归组）")
def do_reindex(classify_all: bool = False, rebuild: bool = True):
    try:
        out = reindex(classify_all=classify_all, rebuild=rebuild)
        ctx.cache.invalidate(CACHE_KEY_CONTACTS)
        return {"ok": True, **out}
    except Exception as e:
        raise HTTPException(500, f"重建索引失败: {e}")


@router.get("/api/categories", summary="分类统计：无聊邮件占比")
def categories():
    s = store()
    stats = s.category_stats()
    stats["labels"] = CATEGORY_LABELS
    stats["threads"] = s.thread_stats()
    return stats


@router.get("/api/ai/inbox", summary="AI 入口：结构化邮件列表（含清洗正文）")
def ai_inbox(
    since: str | None = Query(default=DEFAULT_SINCE, examples=["2026-07-01"]),
    until: str | None = None,
    folder: list[str] | None = Query(default=None, description="默认 INBOX；传 all 表示全部目录"),
    q: str | None = None,
    unread_only: bool = False,
    has_attachment: bool | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = 0,
    body_chars: int = Query(default=4000, ge=0, le=200000),
    include_html: bool = False,
    include_inline: bool = Query(default=False, description="内嵌图片（签名 logo 等）是否列入 attachments"),
    category: str | None = Query(default=None, pattern="^(personal|meeting|automated|promotion)$"),
    boring: bool | None = Query(default=None, description="true=只要无聊邮件，false=排除无聊邮件"),
    order: str = "desc",
):
    """路由只解析参数；逻辑在 services.build_ai_inbox（CLI 复用同一份）。"""
    return build_ai_inbox(
        since=since,
        until=until,
        folder=folder,
        q=q,
        unread_only=unread_only,
        has_attachment=has_attachment,
        limit=limit,
        offset=offset,
        body_chars=body_chars,
        include_html=include_html,
        include_inline=include_inline,
        category=category,
        boring=boring,
        order=order,
    )


@router.get("/api/ai/threads", summary="AI 入口：会话视图（同一来回聚合，聊天式输出）")
def ai_threads(
    since: str | None = Query(default=DEFAULT_SINCE, examples=["2026-07-01"]),
    until: str | None = None,
    folder: list[str] | None = Query(default=None, description="默认全部目录；传 all 也是全部"),
    q: str | None = None,
    unread_only: bool = False,
    category: str | None = Query(default=None, pattern="^(personal|meeting|automated|promotion)$"),
    boring: bool | None = Query(default=None),
    only_grouped: bool = Query(default=False, description="true=只返回多于一封的会话"),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = 0,
    body_chars: int = Query(default=1500, ge=0, le=200000),
    max_messages: int = Query(default=30, ge=1, le=200, description="单条会话最多返回多少封"),
):
    """路由只解析参数；逻辑在 services.build_ai_threads。"""
    return build_ai_threads(
        since=since,
        until=until,
        folder=folder,
        q=q,
        unread_only=unread_only,
        category=category,
        boring=boring,
        only_grouped=only_grouped,
        limit=limit,
        offset=offset,
        body_chars=body_chars,
        max_messages=max_messages,
    )


@router.get("/api/ai/digest", summary="AI 入口：按维度聚合统计")
def ai_digest(
    since: str | None = Query(default=DEFAULT_SINCE),
    until: str | None = None,
    folder: list[str] | None = Query(default=None),
    q: str | None = None,
    group_by: str = Query(default="sender", pattern="^(sender|date|subject|folder)$"),
    top: int = Query(default=20, ge=1, le=200),
):
    s = store()
    folders = None if folder and any(f.lower() == "all" for f in folder) else (list(folder) if folder else ["INBOX"])
    _, items = s.list_messages(folders=folders, since=since, until=until, q=q, limit=5000, order="desc")

    def key(m: dict) -> str:
        if group_by == "sender":
            return f'{m["from"]["name"]} <{m["from"]["email"]}>'
        if group_by == "date":
            return (m["date"] or "")[:10]
        if group_by == "folder":
            return m["folder"]
        import re as _re

        return _re.sub(r"^(re|fw|fwd)\s*[:：]\s*", "", (m["subject"] or ""), flags=_re.I).strip().lower()

    buckets: dict[str, dict] = {}
    for m in items:
        k = key(m)
        b = buckets.setdefault(k, {"key": k, "count": 0, "unread": 0, "with_attachment": 0, "latest": "", "samples": []})
        b["count"] += 1
        b["unread"] += 1 if m["unread"] else 0
        b["with_attachment"] += 1 if m["has_attachment"] else 0
        if (m["date"] or "") > b["latest"]:
            b["latest"] = m["date"] or ""
        if len(b["samples"]) < 3:
            b["samples"].append({"id": m["id"], "date": m["date"], "subject": m["subject"], "snippet": m["snippet"][:120]})

    groups = sorted(buckets.values(), key=lambda x: x["count"], reverse=True)[:top]
    return {
        "account": account().email,
        "query": {"since": since, "until": until, "folder": folders or "all", "q": q, "group_by": group_by},
        "total_messages": len(items),
        "group_count": len(groups),
        "groups": groups,
    }


@router.get("/api/ai/verify", summary="校验：指定时间窗内各目录的邮件数量与日期范围")
def ai_verify(since: str = Query(default=DEFAULT_SINCE), folder: list[str] | None = Query(default=None)):
    s = store()
    folders = None if folder and any(f.lower() == "all" for f in folder) else (list(folder) if folder else None)
    if folders is None:
        folders = [f["name"] for f in s.list_folders()]
    out = []
    total = 0
    with_body = 0
    for f in folders:
        total_, items = s.list_messages(folders=[f], since=since, limit=100000)
        bodies = s.conn.execute(
            "SELECT COUNT(*) AS c FROM messages m JOIN bodies b ON b.folder=m.folder AND b.uid=m.uid "
            "WHERE m.folder=? AND m.date_ts>=?",
            (f, s._ts(since)),
        ).fetchone()["c"]
        dates = [i["date"] for i in items if i["date"]]
        out.append(
            {
                "folder": f,
                "count": total_,
                "with_body": bodies,
                "oldest": min(dates) if dates else None,
                "newest": max(dates) if dates else None,
                "attachments": s.conn.execute(
                    "SELECT COUNT(*) AS c FROM attachments a JOIN messages m ON m.folder=a.folder AND m.uid=a.uid "
                    "WHERE m.folder=? AND m.date_ts>=?",
                    (f, s._ts(since)),
                ).fetchone()["c"],
            }
        )
        total += total_
        with_body += bodies
    return {"since": since, "total": total, "with_body": with_body, "folders": out}
