"""MIME 解析：把 RFC822 原始字节转成结构化 dict。

重点处理国内邮箱（Coremail 等）常见的 GBK/GB2312 编码、无 charset 声明、
以及 multipart 嵌套与内联图片。
"""
from __future__ import annotations

import email
import html
import re
from datetime import datetime
from email import policy
from email.utils import getaddresses, parsedate_to_datetime

from .mailout import (
    FORWARD_FOLDER_HEADER,
    FORWARD_UID_HEADER,
    REPLY_FOLDER_HEADER,
    REPLY_UID_HEADER,
)

CHARSET_CHAIN = ("utf-8", "gb18030", "big5", "shift_jis", "latin-1")

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"(?is)<(script|style|head)[^>]*>.*?</\1>")
_BR_RE = re.compile(r"(?i)<(br|/p|/div|/tr|/li|/h[1-6])[^>]*>")
_WS_RE = re.compile(r"[ \t\u3000\xa0]+")
_NL_RE = re.compile(r"\n{3,}")


def decode_bytes(raw: bytes, charset: str | None) -> str:
    if raw is None:
        return ""
    tried = []
    if charset:
        tried.append(charset)
    tried.extend(CHARSET_CHAIN)
    for cs in tried:
        try:
            return raw.decode(cs)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _part_text(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    return decode_bytes(payload, part.get_content_charset())


def html_to_text(fragment: str) -> str:
    if not fragment:
        return ""
    s = _SCRIPT_RE.sub(" ", fragment)
    s = _BR_RE.sub("\n", s)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _WS_RE.sub(" ", s)
    lines = [ln.strip() for ln in s.split("\n")]
    s = "\n".join(ln for ln in lines if ln)
    return _NL_RE.sub("\n\n", s).strip()


def _decode_header_value(msg, key: str) -> str:
    val = msg.get(key)
    if val is None:
        return ""
    try:
        return str(val)
    except Exception:
        return ""


def _parse_addr_list(msg, key: str) -> list[dict]:
    raw = _decode_header_value(msg, key)
    if not raw:
        return []
    out = []
    for name, addr in getaddresses([raw]):
        name = (name or "").strip().strip('"')
        addr = (addr or "").strip()
        if not name and not addr:
            continue
        out.append({"name": name or addr.split("@")[0], "email": addr})
    return out


def _parse_date(msg) -> tuple[str | None, float]:
    dt = None
    try:
        hdr = msg.get("Date")
        if hdr is not None and hasattr(hdr, "datetime") and hdr.datetime:
            dt = hdr.datetime
    except Exception:
        dt = None
    if dt is None:
        raw = _decode_header_value(msg, "Date")
        try:
            dt = parsedate_to_datetime(raw)
        except Exception:
            dt = None
    if dt is None:
        return None, 0.0
    try:
        return dt.isoformat(), dt.timestamp()
    except Exception:
        return dt.isoformat(), 0.0


_INTERNALDATE_FMT = ("%d-%b-%Y %H:%M:%S %z", "%d-%b-%Y %H:%M:%S")


def parse_internaldate(value: str) -> tuple[str | None, float]:
    """解析 IMAP 的 INTERNALDATE（如 ``26-Aug-2026 20:00:00 +0800``）。

    用于邮件自身没有 ``Date:`` 头时的兜底 —— postmaster 之类的系统信常常不给 Date，
    没有它这几封会被记成 1970 并落进「无日期」堆里（也拿不到正文）。
    注意 IMAP 里个位数日期是空格补齐的（`` 3-Sep-2026 …``），先 strip 再按 %d 解析。
    """
    s = (value or "").strip()
    if not s:
        return None, 0.0
    for fmt in _INTERNALDATE_FMT:
        try:
            dt = datetime.strptime(s, fmt)
        except Exception:
            continue
        try:
            return dt.isoformat(), dt.timestamp()
        except Exception:
            return dt.isoformat(), 0.0
    return None, 0.0


def make_snippet(text: str, limit: int = 220) -> str:
    s = _WS_RE.sub(" ", (text or "").replace("\n", " ")).strip()
    return s[:limit] + ("…" if len(s) > limit else "")


def parse_raw(raw: bytes) -> dict:
    """解析一封邮件，返回 headers / 正文 / 附件等结构化数据。"""
    try:
        msg = email.message_from_bytes(raw, policy=policy.default)
    except Exception:
        msg = email.message_from_bytes(raw)
    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict] = []

    idx = 0
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        ctype = part.get_content_type()
        disposition = str(part.get("Content-Disposition") or "")
        filename = part.get_filename()
        is_attachment = bool(filename) or "attachment" in disposition.lower()

        if is_attachment:
            data = part.get_payload(decode=True) or b""
            attachments.append(
                {
                    "index": idx,
                    "filename": str(filename or f"part-{idx}"),
                    "content_type": ctype,
                    "size": len(data),
                    "content_id": str(part.get("Content-ID") or "").strip("<>") or None,
                    "is_inline": "inline" in disposition.lower(),
                    "_data": data,
                }
            )
            idx += 1
            continue

        if ctype == "text/plain":
            t = _part_text(part)
            if t.strip():
                text_parts.append(t)
        elif ctype == "text/html":
            h = _part_text(part)
            if h.strip():
                html_parts.append(h)
        elif ctype in ("text/calendar", "application/ics"):
            # 日历邀请：作为附件存下来（分类时用来识别会议通知）
            data = part.get_payload(decode=True) or b""
            attachments.append(
                {
                    "index": idx,
                    "filename": str(filename or "invite.ics"),
                    "content_type": ctype,
                    "size": len(data),
                    "content_id": "",
                    "is_inline": False,
                    "_data": data,
                }
            )
            idx += 1
        else:
            data = part.get_payload(decode=True) or b""
            if data and (ctype.startswith(("image/", "audio/", "video/", "application/"))):
                attachments.append(
                    {
                        "index": idx,
                        "filename": str(filename or f"part-{idx}.bin"),
                        "content_type": ctype,
                        "size": len(data),
                        "content_id": str(part.get("Content-ID") or "").strip("<>") or None,
                        "is_inline": True,
                        "_data": data,
                    }
                )
                idx += 1

    body_text = "\n\n".join(p for p in text_parts if p.strip()).strip()
    body_html = "\n".join(html_parts).strip()
    if not body_text and body_html:
        body_text = html_to_text(body_html)

    date_iso, date_ts = _parse_date(msg)
    from_list = _parse_addr_list(msg, "From")
    sender = from_list[0] if from_list else {"name": "", "email": ""}

    def _sig(key: str) -> str:
        return _decode_header_value(msg, key).strip()

    signals = {
        "auto_submitted": _sig("Auto-Submitted").lower(),
        "precedence": _sig("Precedence").lower(),
        "list_unsubscribe": _sig("List-Unsubscribe"),
        "x_autoreply": _sig("X-Autoreply").lower(),
        "x_autorespond": _sig("X-Autorespond").lower(),
        "x_mailer": _sig("X-Mailer").lower(),
        "return_path": _sig("Return-Path").lower(),
        "has_calendar": any(
            (a["content_type"] or "").lower().startswith("text/calendar")
            or str(a["filename"]).lower().endswith(".ics")
            for a in attachments
        ),
    }

    # 我们自己存草稿时记下的「本来要回复谁 / 转发哪封」（见 api_mail.save_draft）；
    # 别的邮件不会有这些头，返回 None。
    stored_folder = _sig(REPLY_FOLDER_HEADER)
    stored_uid = _sig(REPLY_UID_HEADER)
    reply_context = (
        {"folder": stored_folder, "uid": int(stored_uid)}
        if stored_folder and stored_uid.isdigit()
        else None
    )
    fwd_folder = _sig(FORWARD_FOLDER_HEADER)
    fwd_uid = _sig(FORWARD_UID_HEADER)
    forward_context = (
        {"folder": fwd_folder, "uid": int(fwd_uid)}
        if fwd_folder and fwd_uid.isdigit()
        else None
    )

    return {
        "message_id": _decode_header_value(msg, "Message-ID").strip(),
        "subject": _decode_header_value(msg, "Subject").strip() or "(无主题)",
        "from_name": sender["name"],
        "from_addr": sender["email"],
        "to": _parse_addr_list(msg, "To"),
        "cc": _parse_addr_list(msg, "Cc"),
        "reply_to": _decode_header_value(msg, "Reply-To"),
        "date_iso": date_iso,
        "date_ts": date_ts,
        "in_reply_to": _decode_header_value(msg, "In-Reply-To").strip() or None,
        "references": _decode_header_value(msg, "References").strip() or None,
        "body_text": body_text,
        "body_html": body_html,
        "snippet": make_snippet(body_text or html_to_text(body_html)),
        "attachments": attachments,
        "signals": signals,
        "reply_context": reply_context,
        "forward_context": forward_context,
        "_raw": raw,
    }
