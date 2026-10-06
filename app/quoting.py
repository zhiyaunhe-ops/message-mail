"""回复 / 转发的引用块：标准英文引用头 + HTML 引用体，纯文本再留一份 > 前缀兜底。

回复（Re:）和转发（Fwd:）发出去的是同一份东西 —— 引用头 + 缩进的原文，
区别只在主题前缀与要不要挂 In-Reply-To，那两件事在 app/api_mail.py 里按 mode 决定。

**为什么不是「手工 > 前缀的纯文本」**：收件方的网关（对端是 Exchange + Defender
Safe Links）会把纯文本正文做一次 plain→html→plain 往返，顺手重排折行、吃掉行尾空格、
重写 URL —— 引用块当场散架（断句错位、URL 被拆成几截）。别人（Outlook）发的是 HTML 引用，
重排伤不到；所以这里也发 HTML，另存一份 text/plain 给只看纯文本的场景兜底。

**引用头用英文 From/Sent/To/Cc/Subject**（和收件方的 Outlook 一致），不再拼
「----- 原始邮件 -----」那一套 —— 否则对方邮件里会出现两套头、两种语言并排。

阅读器侧配套：HTML 引用整体包在一个以 ``From:`` 开头的 div 里，
``web/app.js`` 的 foldQuotes 会把它折起来；纯文本引用头 ``-----Original Message-----``
也命中它和 ``app/ai.py`` 的分隔线正则。
"""
from __future__ import annotations

import html as html_mod
import re
from datetime import datetime, tzinfo
from pathlib import Path

# ---------------------------------------------------------------- 常量 / 正则

# 「碰到上一轮的引用就停」（不套娃）
QUOTE_SEP_RE = re.compile(r"^-{2,}\s*(?:原始邮件|Original\s+Message)\s*-*$", re.I)
NESTED_HDR_RE = re.compile(r"^(?:From\s*:|发件人[:：])", re.I)
# 纯文本里的 [cid:xxx] 占位
CID_PLACEHOLDER_RE = re.compile(r"\[\s*cid:[^\]\s]+\s*\]", re.I)

MAX_TEXT_LINES = 20           # 纯文本引用最多带多少行
MAX_HTML_CHARS = 120_000      # HTML 引用上限，超了退回纯文本（别把整封 HTML 邮件塞回去）
MAX_INLINE_IMG = 512 * 1024   # 单个内嵌图上限（签名 logo 一般几十 KB）
TRUNC_MARK = "> […]"          # 截断了就说一声，别留一个裸的 > …

_DROP_PAIRED_RE = re.compile(
    r"(?is)<(script|style|head|title|iframe|frame|frameset|object|embed|applet|svg|canvas"
    r"|form|select|textarea)\b[^>]*>.*?<\s*/\s*\1\s*>"
)
_DROP_LONE_RE = re.compile(
    r"(?is)<(script|style|iframe|frame|object|embed|applet|link|meta|base"
    r"|input|button|select|textarea|svg|canvas)\b[^>]*/?>"
)
_COMMENT_RE = re.compile(r"(?s)<!--.*?-->")
_BODY_INNER_RE = re.compile(r"(?is)<body\b[^>]*>(.*)</body\s*>")
_DOC_TAGS_RE = re.compile(r"(?is)</?(?:html|body|head)\b[^>]*>")
_ON_ATTR_RE = re.compile(r"""(?is)\son[a-z]{3,}\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""")
_JS_URL_RE = re.compile(
    r"""(?is)\s(?:href|src|action)\s*=\s*(?:"\s*javascript:[^"]*"|'\s*javascript:[^']*'|javascript:[^\s>]+)"""
)
_STYLE_ATTR_RE = re.compile(r"""(?is)\sstyle\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_BAD_STYLE_RE = re.compile(
    r"(?i)\b(?:position\s*:\s*(?:fixed|absolute|sticky)|z-index\s*:|top\s*:|bottom\s*:|left\s*:|right\s*:)[^;\"']*;?"
)
_BALANCE_TAGS = ("div", "table", "tbody", "tr", "td", "th", "span", "p")
_BALANCE_RE = re.compile(r"(?is)<(/?)(%s)\b[^>]*?(/?)>" % "|".join(_BALANCE_TAGS))

_IMG_TAG_RE = re.compile(r"(?is)<img\b[^>]*>")
_IMG_SRC_RE = re.compile(r"""(?is)\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
_IMG_ALT_RE = re.compile(r"""(?is)\balt\s*=\s*(?:"([^"]*)"|'([^']*)')""")

_URL_RE = re.compile(r"(https?://[^\s<>\"']+)")

# 上一轮的引用在 HTML 里长什么样：碰到就切掉，别把整条往来链（对方也在引用，
# 还叠着签名/免责声明）整包带回去 —— 和纯文本版「碰到嵌套头就停」保持一致。
_NESTED_QUOTE_MARKERS = (
    re.compile(r"(?is)-{2,}\s*(?:original message|原始邮件)\s*-*"),
    re.compile(r"""(?is)<div[^>]+id\s*=\s*["']?(?:divRplyFwdMsg|appendonly)"""),
    re.compile(r"""(?is)<div[^>]+class\s*=\s*["'][^"']*\bgmail_quote\b"""),
    re.compile(r"""(?is)<blockquote[^>]+type\s*=\s*["']?cite"""),
    re.compile(r"(?is)<(?:b|strong|font|span|div|p)[^>]*>\s*(?:From|发件人)\s*[:：]"),
)


def _cut_nested(html: str) -> str:
    """切掉上一轮的引用（含截断可能落在半个标签上的尾巴）。"""
    cut = len(html)
    for rx in _NESTED_QUOTE_MARKERS:
        m = rx.search(html)
        if m and m.start() < cut:
            cut = m.start()
    frag = html[:cut]
    lt = frag.rfind("<")
    if lt > frag.rfind(">"):        # 正好切在标签中间/属性里
        frag = frag[:lt]
    return frag


# ---------------------------------------------------------------- 引用头

QUOTE_TIME_FMT = "%A, %d %B %Y %H:%M"   # 如 Tuesday, 15 September 2026 09:02


def fmt_quote_time(date_iso: str, tz: tzinfo | None = None) -> str:
    """引用头的时间给人看。

    库里 ``date_iso`` 来自原信自己的 ``Date:`` 头，**带着发信方的偏移**（实测 419 封里
    ``+00:00`` 265、``+08:00`` 138、``-07:00`` 16，没有 naive 的）。所以这里做的不是
    「补一个时区」，而是把它换算到**阅读者所在的时区** —— 前端列表用的也是读者本地时区
    （``web/app.js`` 里 ``new Date(s).getHours()``），两边口径要一致。

    ``tz`` 不传就是**进程本地时区**，也就是这个换算的默认依据。问题在于「进程本地」
    在容器 / 别的时区主机上并不等于「读者本地」：服务器跑在 UTC 容器里，一封
    09:02+08:00 的信会被印成 01:02，静默错 8 小时写进发出去的信里。
    所以调用方（``app/api_mail.py``）按配置把 ``tz`` 显式传进来。
    """
    try:
        dt = datetime.fromisoformat(date_iso or "")
    except (TypeError, ValueError):
        return date_iso or ""
    if dt.tzinfo is None:
        # 原文没带偏移：只有 sync.py 退回 IMAP INTERNALDATE 那条兜底才会这样
        # （mailparse._INTERNALDATE_FMT 里有个不带 %z 的写法）。它的时区未知，
        # 所以**不猜** —— 按进程本地再折一次等于凭空发明一个偏移量，
        # 跟这个函数要修的正是同一类毛病。INTERNALDATE 本身就是服务器本地时间，
        # 原样显示比多折一次更接近事实。
        return dt.strftime(QUOTE_TIME_FMT)
    try:
        dt = dt.astimezone(tz)        # tz=None -> 进程本地，与从前一致
    except (ValueError, OSError):     # 极老的 fromisoformat 边界，宁可原样
        pass
    return dt.strftime(QUOTE_TIME_FMT)


def fmt_addr(item: dict | None) -> str:
    name = str((item or {}).get("name") or "").strip()
    addr = str((item or {}).get("email") or "").strip()
    if not addr:
        return name
    if not name or name == addr:
        return addr
    if re.search(r'[",;:<>@\\]', name):
        name = '"' + name.replace('"', "'") + '"'
    return f"{name} <{addr}>"


def header_pairs(orig: dict, tz: tzinfo | None = None) -> list[tuple[str, str]]:
    """From / Sent / To / Cc / Subject（英文，和收件方的 Outlook 一致）。"""
    pairs = [
        ("From", fmt_addr(orig.get("from") or {})),
        ("Sent", fmt_quote_time(orig.get("date") or "", tz)),
    ]
    for key, label in (("to", "To"), ("cc", "Cc")):
        val = ", ".join(x for x in (fmt_addr(p) for p in (orig.get(key) or [])) if x)
        if val:
            pairs.append((label, val))
    subject = (orig.get("subject") or "").strip()
    if subject:
        pairs.append(("Subject", subject))
    return [(k, v) for k, v in pairs if v]


# ---------------------------------------------------------------- 纯文本引用

def quote_text(body_text: str, max_lines: int = MAX_TEXT_LINES) -> str:
    """引用正文：cid 图片占位换掉，碰到上一轮的引用分隔线/嵌套引用头就停，截断留标记。"""
    src = (body_text or "").splitlines()
    lines: list[str] = []
    for i, ln in enumerate(src):
        head = ln.strip()
        if QUOTE_SEP_RE.match(head) or NESTED_HDR_RE.match(head):
            break
        if i >= max_lines:
            if any(x.strip() for x in src[i:]):
                lines.append(TRUNC_MARK)
            break
        lines.append(f"> {CID_PLACEHOLDER_RE.sub('[图片]', ln)}")
    return re.sub(r"(?:\n> ?)+$", "", "\n".join(lines))


# ---------------------------------------------------------------- HTML 清洗 / 降级

def _clean_style_attr(m: re.Match) -> str:
    quote = '"' if m.group(1) is not None else "'"
    val = _BAD_STYLE_RE.sub("", m.group(1) if m.group(1) is not None else m.group(2) or "")
    val = val.strip().strip(";").strip()
    return f" style={quote}{val}{quote}" if val else ""


def _balance(fragment: str) -> str:
    """把引来的 HTML 收尾收干净：多余的闭合标签丢掉（免得把外层容器提前关掉），缺的补上。"""
    stack: list[str] = []

    def repl(m: re.Match) -> str:
        closing, name, selfclose = m.group(1), m.group(2).lower(), m.group(3)
        if selfclose or name == "p":       # <p> 会自动闭合，不参与配对
            return m.group(0)
        if closing:
            if name not in stack:
                return ""
            while stack and stack[-1] != name:
                stack.pop()
            stack.pop()
            return m.group(0)
        stack.append(name)
        return m.group(0)

    out = _BALANCE_RE.sub(repl, fragment)
    return out + "".join(f"</{n}>" for n in reversed(stack))


def sanitize_html(fragment: str) -> str:
    """引用别人的 HTML 之前过一遍：先切掉上一轮的引用，再拿掉脚本/外链资源/事件处理器。"""
    s = _COMMENT_RE.sub("", fragment or "")
    m = _BODY_INNER_RE.search(s)
    if m:
        s = m.group(1)
    s = _DROP_PAIRED_RE.sub("", s)
    s = _DROP_LONE_RE.sub("", s)
    s = _ON_ATTR_RE.sub("", s)
    s = _JS_URL_RE.sub("", s)
    s = _STYLE_ATTR_RE.sub(_clean_style_attr, s)
    s = _DOC_TAGS_RE.sub("", s)
    return _balance(_cut_nested(s)).strip()


def _nl2br(text: str) -> str:
    return html_mod.escape(text or "").replace("\n", "<br>")


def _text_to_html(text: str) -> str:
    """把写信框里的纯文本正文转成 HTML（转义 → 空行分段 → 裸链接点成 <a>）。"""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not t:
        return ""
    blocks = [b for b in re.split(r"\n{2,}", t) if b.strip()]
    out = []
    for b in blocks:
        body = "<br>".join(_URL_RE.sub(r'<a href="\1">\1</a>', html_mod.escape(x)) for x in b.split("\n"))
        out.append(f'<div style="margin:0 0 10px">{body}</div>')
    return "".join(out)


# ---------------------------------------------------------------- 内嵌图（cid）

def inline_cids(html: str, atts: list[dict] | None, domain: str = "") -> tuple[str, list[dict]]:
    """把引用里的 ``src="cid:…"`` 换成新的 Content-ID，并把图片字节作为 related 部分带出去。

    拿不到图（文件名不在本地、太大、附件没落盘）就把 <img> 退成 alt 文本或整个去掉 ——
    宁可少一张图，也不要给对方一个破图占位。
    """
    if "cid:" not in (html or "").lower():
        return html, []

    by_cid: dict[str, dict] = {}
    for a in atts or []:
        cid = str(a.get("content_id") or "").strip().strip("<>")
        if cid:
            by_cid[cid.lower()] = a
            by_cid[cid.split("@")[0].lower()] = a
        if a.get("filename"):
            by_cid.setdefault(str(a["filename"]).lower(), a)

    cache: dict[int, dict | None] = {}
    related: list[dict] = []

    def take(att: dict | None) -> dict | None:
        if not att:
            return None
        idx = int(att.get("index") or 0)
        if idx in cache:
            return cache[idx]
        item = None
        path = att.get("path")
        try:
            p = Path(path) if path else None
            if p and p.is_file() and p.stat().st_size <= MAX_INLINE_IMG:
                item = {
                    "content_type": att.get("content_type") or "application/octet-stream",
                    "data": p.read_bytes(),
                    "cid": f"quote-{idx}@{domain or 'quoted.invalid'}",
                }
                related.append(item)
        except OSError:
            item = None
        cache[idx] = item
        return item

    def repl(m: re.Match) -> str:
        tag = m.group(0)
        sm = _IMG_SRC_RE.search(tag)
        if not sm:
            return tag
        raw = (sm.group(1) or sm.group(2) or sm.group(3) or "").strip()
        if not raw.lower().startswith("cid:"):
            return tag                                   # 外链图原样留着
        cid = raw[4:].strip().strip("<>")
        item = take(by_cid.get(cid.lower()) or by_cid.get(cid.split("@")[0].lower()))
        if not item:
            am = _IMG_ALT_RE.search(tag)
            alt = (am.group(1) or am.group(2) or "") if am else ""
            return f"[{html_mod.escape(alt)}]" if alt.strip() else ""
        return _IMG_SRC_RE.sub(f'src="cid:{item["cid"]}"', tag, count=1)

    return _IMG_TAG_RE.sub(repl, html), related


# ---------------------------------------------------------------- 组装

def quote_html(
    orig: dict, body: dict, atts: list[dict] | None, domain: str = "", tz: tzinfo | None = None
) -> tuple[str, list[dict]]:
    """HTML 引用块：整体包一个以 ``From:`` 开头的 div（阅读器据此折叠）。"""
    src_html = (body.get("body_html") or "").strip()
    related: list[dict] = []
    src = ""
    if src_html:
        clean = sanitize_html(src_html)
        if clean and len(clean) <= MAX_HTML_CHARS:
            src, related = inline_cids(clean, atts, domain)
    if not src:
        src = _nl2br((body.get("body_text") or "").strip())
    head = "".join(
        f"<b>{html_mod.escape(k)}:</b> {html_mod.escape(v)}<br>" for k, v in header_pairs(orig, tz)
    )
    return (
        '<div>'
        '<div style="border-top:1px solid #c9c9c9;margin-top:14px;padding-top:2px"></div>'
        f'<div style="font-size:13px;color:#333">{head}</div>'
        f'<div style="border-left:2px solid #d6d6d6;margin-top:8px;padding-left:12px">{src}</div>'
        "</div>",
        related,
    )


def build_quote(
    user_text: str,
    orig: dict,
    body: dict,
    atts: list[dict] | None = None,
    domain: str = "",
    tz: tzinfo | None = None,
) -> tuple[str, str, list[dict]]:
    """返回 (text_body, html_body, related)：两边都是「写信框正文 + 引用」。

    回复和转发共用这一份：引用块本身长得一样（标准引用头 + 缩进原文），
    两者的区别只在主题前缀（Re: / Fwd:）和要不要挂 In-Reply-To ——
    那两件事在 app/api_mail.py 的发信端点里按 mode 决定，不在这里。

    ``tz`` 是引用头里 ``Sent:`` 那一行用的展示时区，由调用方按配置给出（见 fmt_quote_time）。
    """
    pairs = header_pairs(orig, tz)
    text_head = "-----Original Message-----\n" + "\n".join(f"{k}: {v}" for k, v in pairs)
    quoted = quote_text(body.get("body_text") or "")
    text = f"{(user_text or '').rstrip()}\n\n{text_head}\n\n{quoted}".rstrip()
    html_quote, related = quote_html(orig, body, atts, domain=domain, tz=tz)
    return text, _text_to_html(user_text) + html_quote, related


# 旧名（回复场景先落地时叫 build_reply）。转发复用同一份引用块，故统一成 build_quote。
build_reply = build_quote
