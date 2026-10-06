#!/usr/bin/env python
"""MCP server for Message-cli (email-only branch) — read/search access for AI clients.

A thin adapter: every tool is one call against the Message-cli web app
(default http://127.0.0.1:8791, override with MESSAGE_CLI_URL). The web app
owns all state (IMAP index); this process holds nothing, so there is no second
writer on the SQLite files.

Run standalone (what the MCP client does):
    python mcp_server.py            # stdio transport

Deps (not in requirements.txt — this is a separate env from the web app):
    pip install fastmcp httpx

Tool surface: 6 read-only + 1 write (send_mail). Deliberately NOT exposed:
delete/trash, account management, config writes — those stay in the web UI.
"""
from __future__ import annotations

import os
from urllib.parse import quote

import httpx
from fastmcp import FastMCP

BASE_URL = os.environ.get("MESSAGE_CLI_URL", "http://127.0.0.1:8791").rstrip("/")
TIMEOUT = float(os.environ.get("MESSAGE_CLI_TIMEOUT", "90"))

mcp = FastMCP(
    name="message-cli",
    instructions=(
        "Read/search access to the local Message-cli mail hub "
        "(IMAP -> local index). The Message-cli web app must "
        f"be running on {BASE_URL} (start it with: python run.py serve --port 8791). "
        "Everything is read-only except send_mail, which really sends email via SMTP — "
        "confirm with the user before calling it."
    ),
)

_http: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT)
    return _http


async def _api(method: str, path: str, **kw):
    """One call to the web app. Errors come back as {"error": ...} dicts so the
    model can read the reason instead of crashing the tool call."""
    try:
        r = await _client().request(method, path, **kw)
        r.raise_for_status()
        return r.json()
    except httpx.ConnectError:
        return {"error": f"Message-cli web app not reachable at {BASE_URL}. "
                         "Start it: python run.py serve --port 8791"}
    except httpx.HTTPStatusError as e:
        try:
            detail = e.response.json().get("detail", e.response.text[:300])
        except Exception:
            detail = e.response.text[:300]
        return {"error": f"HTTP {e.response.status_code} from {path}: {detail}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- 邮件
@mcp.tool
async def mail_status() -> dict:
    """Health check: active mail account, per-folder counts, and sync state.
    Call this first when unsure what is reachable."""
    data = await _api("GET", "/api/status")
    if isinstance(data, dict) and "error" not in data:
        data.pop("log", None)  # sync log is noise for a model
        data.pop("cache_keys", None)
    return data


@mcp.tool
async def search_mail(
    q: str | None = None,
    since: str | None = None,
    until: str | None = None,
    folder: str | None = None,
    category: str | None = None,
    boring: bool | None = None,
    unread_only: bool = False,
    has_attachment: bool | None = None,
    limit: int = 20,
    body_chars: int = 2000,
) -> dict:
    """Search mail; returns a structured list where each item has subject,
    from/to, date, category and a cleaned plain-text body (truncated to
    body_chars). Primary mail-search tool.

    q: substring across subject/from/to/body. since/until: "YYYY-MM-DD"
    (since defaults to the last 30 days). folder: null = INBOX only,
    "all" = every folder, otherwise a folder name (e.g. "Sent Messages").
    category: personal | meeting | automated | promotion.
    boring: true = only newsletter/system/meeting noise, false = exclude it.
    Raise limit/body_chars for broader sweeps; use read_mail for one full message.
    """
    params: dict = {"limit": max(1, min(limit, 500)), "body_chars": max(0, min(body_chars, 50000)),
                    "unread_only": unread_only}
    for k, v in [("q", q), ("since", since), ("until", until), ("category", category)]:
        if v:
            params[k] = v
    if folder:
        params["folder"] = ["all"] if folder.lower() == "all" else [folder]
    if boring is not None:
        params["boring"] = boring
    if has_attachment is not None:
        params["has_attachment"] = has_attachment
    return await _api("GET", "/api/ai/inbox", params=params)


@mcp.tool
async def read_mail(uid: int, folder: str = "INBOX", include_html: bool = False) -> dict:
    """Read one full email by uid (uids come from search_mail results; each
    item's id field). Returns headers, attachments list and body_text
    (plus body_html if include_html)."""
    return await _api("GET", f"/api/messages/{uid}", params={
        "folder": folder, "body_format": "both" if include_html else "text"})


@mcp.tool
async def mail_threads(
    q: str | None = None,
    since: str | None = None,
    folder: str | None = None,
    category: str | None = None,
    boring: bool | None = None,
    only_grouped: bool = False,
    limit: int = 20,
) -> dict:
    """List mail grouped into conversations (one entry per back-and-forth, IM
    style). Cheaper first pass than search_mail when reconstructing a dialogue:
    each thread has participants, message_count, snippet and a short preview;
    then read the whole conversation with read_thread(thread_id)."""
    params: dict = {"limit": max(1, min(limit, 200)), "only_grouped": only_grouped}
    for k, v in [("q", q), ("since", since), ("category", category)]:
        if v:
            params[k] = v
    if folder:
        params["folder"] = ["all"] if folder.lower() == "all" else [folder]
    if boring is not None:
        params["boring"] = boring
    return await _api("GET", "/api/ai/threads", params=params)


@mcp.tool
async def read_thread(thread_id: str, body_chars: int = 6000) -> dict:
    """Read one whole mail conversation (all messages, in order, with plain-text
    bodies). thread_id comes from mail_threads results."""
    return await _api("GET", f"/api/threads/{quote(thread_id, safe='')}",
                      params={"body_format": "text",
                              "body_chars": max(0, min(body_chars, 100000))})


@mcp.tool
async def mail_digest(
    group_by: str = "sender",
    since: str | None = None,
    q: str | None = None,
    folder: str | None = None,
    top: int = 20,
) -> dict:
    """Aggregate statistics over mail: top senders / days / subjects / folders
    with counts, unread, attachment ratio and sample subjects. group_by:
    sender | date | subject | folder. Good for "who mails me most this month"
    style questions."""
    params: dict = {"group_by": group_by if group_by in ("sender", "date", "subject", "folder") else "sender",
                    "top": max(1, min(top, 200))}
    for k, v in [("since", since), ("q", q)]:
        if v:
            params[k] = v
    if folder:
        params["folder"] = ["all"] if folder.lower() == "all" else [folder]
    return await _api("GET", "/api/ai/digest", params=params)


@mcp.tool
async def send_mail(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    reply_folder: str = "",
    reply_uid: int = 0,
    forward_folder: str = "",
    forward_uid: int = 0,
) -> dict:
    """SEND an email via SMTP (real side effect — confirm with the user first).

    to/cc: comma-separated addresses, "Name <a@b.com>" allowed. body: plain
    text, newlines become paragraphs. Reply: set reply_folder + reply_uid (from
    search_mail) — the subject gets "Re:" and a quote of the original is
    appended automatically. Forward: set forward_folder + forward_uid instead
    ("Fwd:" + quote). Attachments are not supported over MCP.
    """
    data = {"to": to, "subject": subject, "body": body, "cc": cc}
    if reply_folder and reply_uid:
        data["reply_folder"], data["reply_uid"] = reply_folder, reply_uid
    if forward_folder and forward_uid:
        data["forward_folder"], data["forward_uid"] = forward_folder, forward_uid
    return await _api("POST", "/api/send", data=data)


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
