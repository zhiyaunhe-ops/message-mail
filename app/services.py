"""业务服务层：不依赖 FastAPI，路由与 CLI 共用同一份逻辑。

为什么要有这一层：
    路由函数的默认值常写成 `Query(...)` / `Body(...)`，那是给框架解析用的。
    CLI 或后台任务如果直接调路由函数，**没显式传的参数会拿到 Query 对象**并一路传下去
    （曾经就因此把 Query 传到 sqlite 参数里报错）。把逻辑放到这里、参数收普通值，
    路由只做「解析参数 → 调服务 → 返回」，CLI 也能安全复用。
"""
from __future__ import annotations

import re
import time

from .classify import label as cat_label
from .context import ctx

# ------------------------------------------------------------------ 展示辅助
_CID_RE = re.compile(r"""(?i)(?:cid:|["']cid:)([^\s"'<>)\]]+)""")


def att_url(folder: str, uid: int, index: int, disposition: str = "inline") -> str:
    from urllib.parse import quote

    return f"/api/messages/{uid}/attachment/{index}?folder={quote(folder, safe='')}&disposition={disposition}"


def resolve_cids(html: str, folder: str, uid: int, atts: list[dict]) -> str:
    """把正文里的 cid: 引用替换成指向本地附件的 URL，让内嵌图片直接显示在正文中。"""
    if not html or "cid:" not in html.lower():
        return html
    by_cid: dict[str, dict] = {}
    for a in atts:
        cid = (a.get("content_id") or "").strip().strip("<>")
        if cid:
            by_cid[cid.lower()] = a
            by_cid[cid.split("@")[0].lower()] = a
        if a.get("filename"):
            by_cid[a["filename"].lower()] = a

    def repl(m: re.Match) -> str:
        cid = m.group(1).strip()
        a = by_cid.get(cid.lower()) or by_cid.get(cid.split("@")[0].lower())
        if not a:
            return m.group(0)
        return att_url(folder, uid, a["index"], "inline")

    return _CID_RE.sub(repl, html)


def split_attachments(atts: list[dict], folder: str, uid: int) -> tuple[list[dict], list[dict]]:
    """拆成「真实附件」与「内嵌资源（签名 logo / 插图等，已渲染进正文）」。"""
    real, inline = [], []
    for a in atts:
        item = {
            "filename": a["filename"],
            "content_type": a["content_type"],
            "size": a["size"],
            "is_inline": bool(a["is_inline"]),
            "content_id": a.get("content_id") or "",
            "url": att_url(folder, uid, a["index"], "attachment"),
            "inline_url": att_url(folder, uid, a["index"], "inline"),
        }
        (inline if a["is_inline"] or a.get("content_id") else real).append(item)
    return real, inline


def truncate(text: str, n: int) -> str:
    if n <= 0 or not text or len(text) <= n:
        return text or ""
    return text[:n] + f"\n…（已截断，共 {len(text)} 字符，取全文请调用 /api/messages/{{uid}}）"


def folder_arg(folder: list[str] | None, default_inbox: bool = True) -> list[str] | None:
    """folder 参数语义：None → ['INBOX']（或全部）；含 'all' → None 表示不限目录。"""
    if not folder:
        return ["INBOX"] if default_inbox else None
    if any(str(f).lower() == "all" for f in folder):
        return None
    return list(folder)


# ------------------------------------------------------------------ 派生索引
def reindex(classify_all: bool = False, rebuild: bool = True) -> dict:
    """重建本地派生数据：分类 + 会话归组（启动时与 /api/reindex 都用它）。"""
    from .threads import rebuild as rebuild_threads

    s = ctx.store()
    out: dict = {}
    if classify_all:
        out["classified"] = s.classify_all(force=True)
    else:
        pending = s.conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE category='' OR category IS NULL"
        ).fetchone()["c"]
        out["classified"] = s.classify_all() if pending else {"scanned": 0}
    if rebuild:
        out["threads"] = rebuild_threads(s, own_emails=ctx.own_emails)
    out["categories"] = s.category_stats()
    return out


# ------------------------------------------------------------------ AI：结构化邮件
def build_ai_inbox(
    *,
    since: str | None = None,
    until: str | None = None,
    folder: list[str] | None = None,
    q: str | None = None,
    unread_only: bool = False,
    has_attachment: bool | None = None,
    limit: int = 50,
    offset: int = 0,
    body_chars: int = 4000,
    include_html: bool = False,
    include_inline: bool = False,
    category: str | None = None,
    boring: bool | None = None,
    order: str = "desc",
) -> dict:
    s = ctx.store()
    folders = folder_arg(folder, default_inbox=True)
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
    out = []
    for m in items:
        body = s.get_body(m["folder"], m["uid"])
        atts = s.get_attachments(m["folder"], m["uid"])
        real, inline = split_attachments(atts, m["folder"], m["uid"])
        listed = real + inline if include_inline else real
        cat = m.get("category") or "personal"
        item = {
            "id": m["id"],
            "folder": m["folder"],
            "uid": m["uid"],
            "date": m["date"],
            "from": m["from"],
            "to": m["to"],
            "cc": m["cc"],
            "subject": m["subject"],
            "unread": m["unread"],
            "flags": m["flags"],
            "snippet": m["snippet"],
            # 分类：personal=人工邮件，其余为「无聊邮件」
            "category": cat,
            "category_label": cat_label(cat),
            "is_boring": bool(m.get("is_boring")),
            "mine": ctx.is_mine(m),
            "thread_id": m.get("thread_id") or "",
            "body_text": truncate(body["body_text"], body_chars),
            # 默认只列真实附件；内嵌图片已在正文里渲染，对 AI 也是噪声
            "attachments": [
                {
                    "filename": a["filename"],
                    "content_type": a["content_type"],
                    "size": a["size"],
                    "is_inline": a["is_inline"],
                    "url": a["url"],
                }
                for a in listed
            ],
            "inline_count": len(inline),
            "url": f"/api/messages/{m['uid']}?folder={m['folder']}",
            "web_url": f"/?folder={m['folder']}&uid={m['uid']}",
        }
        if include_html:
            item["body_html"] = resolve_cids(body["body_html"], m["folder"], m["uid"], atts)
        out.append(item)

    return {
        "account": ctx.account().email,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "query": {
            "since": since,
            "until": until,
            "folder": folders or "all",
            "q": q,
            "unread_only": unread_only,
            "limit": limit,
            "offset": offset,
            "body_chars": body_chars,
        },
        "total": total,
        "count": len(out),
        "next_offset": offset + len(out) if offset + len(out) < total else None,
        "messages": out,
    }


# ------------------------------------------------------------------ AI：会话视图
def build_ai_threads(
    *,
    since: str | None = None,
    until: str | None = None,
    folder: list[str] | None = None,
    q: str | None = None,
    unread_only: bool = False,
    category: str | None = None,
    boring: bool | None = None,
    only_grouped: bool = False,
    limit: int = 20,
    offset: int = 0,
    body_chars: int = 1500,
    max_messages: int = 30,
) -> dict:
    s = ctx.store()
    folders = folder_arg(folder, default_inbox=False)
    total, threads = s.list_threads(
        folders=folders,
        since=since,
        until=until,
        q=q,
        unread_only=unread_only,
        boring=boring,
        category=category,
        own_emails=ctx.own_emails,
        limit=limit,
        offset=offset,
        only_grouped=only_grouped,
    )
    out = []
    for t in threads:
        msgs = t["messages"][-max_messages:]
        out.append(
            {
                "thread_id": t["thread_id"],
                "subject": t["subject"],
                "participants": [p["name"] or p["email"] for p in t["participants"]],
                "message_count": t["message_count"],
                "unread": t["unread"],
                "first_date": t["first_date"],
                "last_date": t["last_date"],
                "folders": t["folders"],
                "category": t["category"],
                "category_label": cat_label(t["category"]),
                "is_boring": t["is_boring"],
                "attachment_count": t["attachment_count"],
                "messages": [
                    {
                        "id": m["id"],
                        "folder": m["folder"],
                        "uid": m["uid"],
                        "date": m["date"],
                        "from": m["from"],
                        "to": m["to"],
                        "mine": ctx.is_mine(m),
                        "unread": m["unread"],
                        "body_text": truncate(s.get_body(m["folder"], m["uid"])["body_text"], body_chars),
                        "attachments": [
                            {
                                "filename": a["filename"],
                                "size": a["size"],
                                "url": att_url(m["folder"], m["uid"], a["index"], "attachment"),
                            }
                            for a in s.get_attachments(m["folder"], m["uid"])
                            if not a["is_inline"]
                        ],
                        "url": f"/api/messages/{m['uid']}?folder={m['folder']}",
                        "web_url": f"/?folder={m['folder']}&uid={m['uid']}",
                    }
                    for m in msgs
                ],
                "url": f"/api/threads/{t['thread_id']}",
                "web_url": f"/?thread={t['thread_id']}",
            }
        )
    return {
        "account": ctx.account().email,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "query": {"since": since, "until": until, "folder": folders or "all", "q": q,
                  "boring": boring, "category": category, "only_grouped": only_grouped},
        "total": total,
        "count": len(out),
        "threads": out,
    }
