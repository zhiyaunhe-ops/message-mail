"""SMTP 发信：构建 MIME -> 发送 -> 副本 APPEND 回 Sent Items。

纯标准库（smtplib / email.message），无第三方依赖。
密码复用 IMAP 的 Account.password；SMTP 地址来自 config.toml 的
smtp.server（未配置时按 settings 的兜底逻辑用 IMAP 同主机:465）。
"""
from __future__ import annotations

import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid

from .settings import Account


class MailOutError(Exception):
    pass


# 草稿带上「这封本来要回复谁」——草稿只存正文，回复上下文不落库的话，
# 从草稿箱重开再发就退化成新邮件（对方客户端另起线程）。见 api_mail.save_draft。
REPLY_FOLDER_HEADER = "X-Message-CLI-Reply-Folder"
REPLY_UID_HEADER = "X-Message-CLI-Reply-UID"
# 转发同理：草稿要记住「本来在转发哪封」，否则继续编辑再发就丢掉了引用块归属。
FORWARD_FOLDER_HEADER = "X-Message-CLI-Forward-Folder"
FORWARD_UID_HEADER = "X-Message-CLI-Forward-UID"


# 地址合法性：'Family, Given'、'xu'、'Sender Sample' 这种没有 @ 的串，getaddresses() 也会
# 当成一个「地址」收下，于是被塞进 SMTP 的 RCPT，服务器回一句 550 ——
# 前端只看到一个 400「部分收件人被拒」，完全看不出是自己少写了 @。
# 所以发信前统一过一遍：不像地址的一律不进 To/Cc，也不当收件人。
_ADDR_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+$")


def looks_like_address(addr: str) -> bool:
    """能不能直接拿去当 SMTP 收件人（有且只有一个 @，没有空白与分隔符）。"""
    return bool(_ADDR_RE.match((addr or "").strip()))


def split_addresses(raw: str | list) -> tuple[list[tuple[str, str]], list[str]]:
    """解析地址串 -> (有效 [(name, addr)], 无效的原始串)。

    无效的定义是没有 @（'Family, Given'、'xu'）—— 这类串留下的唯一后果就是被服务器拒收，
    不如提前挑出来，让调用方能给出「收件人里没有有效邮箱地址：Family, Given」这种话。
    """
    good: list[tuple[str, str]] = []
    bad: list[str] = []
    for name, addr in parse_addresses(raw):
        if looks_like_address(addr):
            good.append((name, addr))
        elif addr.strip():
            bad.append(addr.strip())
    return good, bad


def parse_addresses(raw: str | list) -> list[tuple[str, str]]:
    """'a@x.com, Name <b@y.com>' 或 list -> [(name, addr)]。"""
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    return [(n or a, a) for n, a in getaddresses([str(x) for x in raw]) if a]


def _format_addrs(pairs: list[tuple[str, str]]) -> list[str]:
    """写 To/Cc 头用。没有显示名（或显示名就是地址本身）时只写地址 ——
    不然 formataddr 会把带 @ 的「名字」加引号，变成 '"a@b.c" <a@b.c>' 这种丑样子。"""
    out = []
    for name, addr in pairs:
        out.append(formataddr_safe(name, addr) if (name and name != addr) else addr)
    return out


def formataddr_safe(name: str, addr: str) -> str:
    from email.header import Header
    from email.utils import formataddr

    try:
        name.encode("ascii")
        return formataddr((name, addr))
    except UnicodeEncodeError:
        return formataddr((str(Header(name, "utf-8")), addr))


def smtp_connect(acc: Account, timeout: int = 30) -> smtplib.SMTP:
    if not acc.smtp_host:
        raise MailOutError("未配置 SMTP 服务器（config.toml: smtp.server）")
    try:
        if acc.smtp_ssl:
            s = smtplib.SMTP_SSL(acc.smtp_host, acc.smtp_port or 465, timeout=timeout,
                                 context=ssl.create_default_context())
        else:
            s = smtplib.SMTP(acc.smtp_host, acc.smtp_port or 587, timeout=timeout)
            if acc.smtp_starttls:
                s.starttls(context=ssl.create_default_context())
        s.login(acc.username, acc.password)
        return s
    except smtplib.SMTPAuthenticationError as e:
        raise MailOutError(f"SMTP 登录被拒（{e.smtp_code} {e.smtp_error!r}）") from e
    except Exception as e:
        raise MailOutError(f"SMTP 连接失败 {acc.smtp_host}:{acc.smtp_port} -> {e}") from e


Attachment = tuple[str, str, bytes]  # (filename, content_type, data)
Related = dict  # {"content_type", "data", "cid"} —— 引用块里的内嵌图（multipart/related）


def build_message(
    acc: Account,
    to: str | list,
    subject: str,
    body_text: str,
    cc: str | list = "",
    body_html: str = "",
    attachments: list[Attachment] | None = None,
    related: list[Related] | None = None,
    in_reply_to: str = "",
    references: str = "",
    extra_headers: dict[str, str] | None = None,
    allow_empty_recipients: bool = False,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr_safe(acc.display_name or acc.email, acc.email)
    to_pairs, to_bad = split_addresses(to)
    cc_pairs, cc_bad = split_addresses(cc)
    # To 与 Cc 加起来至少要有一个收件人（只看 Cc 也是能发的，别卡成「收件人为空」）
    if not (to_pairs or cc_pairs) and not allow_empty_recipients:
        if to_bad or cc_bad:
            raise MailOutError(
                "收件人里没有有效邮箱地址：" + "、".join(dict.fromkeys(to_bad + cc_bad))
                + "（要写成 名字 <a@b.com>，或者直接写 a@b.com）"
            )
        raise MailOutError("收件人为空")
    if to_pairs:
        msg["To"] = ", ".join(_format_addrs(to_pairs))
    if cc_pairs:
        msg["Cc"] = ", ".join(_format_addrs(cc_pairs))
    msg["Subject"] = subject or "(无主题)"
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=acc.email.split("@")[-1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    for key, value in (extra_headers or {}).items():
        if value:
            msg[key] = value
    msg.set_content(body_text or "")
    if body_html:
        msg.add_alternative(body_html, subtype="html")
        if related:
            # 内嵌图挂在 text/html 那一支上，add_related 会把它就地升级成 multipart/related
            html_part = msg.get_payload()[1]
            for item in related:
                maintype, _, subtype = (item.get("content_type") or "image/png").partition("/")
                # Content-ID 必须带尖括号（RFC 2045），少了这一对 Outlook 认不出 cid:
                html_part.add_related(
                    item["data"],
                    maintype=maintype or "image",
                    subtype=subtype or "png",
                    cid=f"<{item['cid']}>",
                    disposition="inline",
                )

    for filename, content_type, data in attachments or []:
        maintype, _, subtype = (content_type or "application/octet-stream").partition("/")
        msg.add_attachment(
            data,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=filename or "attachment.bin",
        )
    return msg


def send_message(acc: Account, msg: EmailMessage, timeout: int = 30) -> dict:
    """发送并返回 {message_id, accepted, skipped}。

    skipped 是从 To/Cc 里挑出来、根本没往 SMTP 送的串（没有 @ 的那种）——
    正常路径上它由 build_message 挡掉了，这里是最后一道兜底。
    """
    all_addrs = getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []))
    raw_rcpts = [a.strip() for _, a in all_addrs if (a or "").strip()]
    rcpts: list[str] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for a in raw_rcpts:
        if not looks_like_address(a):
            skipped.append(a)
            continue
        if a.lower() in seen:      # 同一个人（大小写不同也算）只投一次
            continue
        seen.add(a.lower())
        rcpts.append(a)
    if not rcpts:
        if skipped:
            raise MailOutError(
                "收件人里没有有效邮箱地址：" + "、".join(dict.fromkeys(skipped))
                + "（要写成 名字 <a@b.com>，或者直接写 a@b.com）"
            )
        raise MailOutError("没有有效收件人")
    s = smtp_connect(acc, timeout=timeout)
    try:
        refused = s.sendmail(acc.email, rcpts, msg.as_bytes())
    finally:
        try:
            s.quit()
        except Exception:
            pass
    mid = (msg.get("Message-ID") or "").strip()
    if refused:
        raise MailOutError(f"部分收件人被拒: {refused}")
    return {"message_id": mid, "accepted": rcpts, "skipped": skipped}
