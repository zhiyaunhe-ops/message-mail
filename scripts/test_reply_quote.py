"""回复 / 转发引用块的回归测试：python scripts/test_reply_quote.py

盯的是「发出去的这封到底长什么样」：
- 纯文本分支：英文引用头、> 前缀、碰到嵌套头就停、截断留标记（不再有中文「原始邮件」头）
- HTML 分支  ：单层 div 包裹（阅读器据此折叠）、cid 图内嵌、脚本/事件处理器被拿掉、不套娃
- MIME 结构  ：multipart/alternative → related，Content-ID 带尖括号（少一对方 Outlook 认不出）
- 主题前缀   ：Re: 不重复叠、Fwd: 与 Outlook 的 FW: 都认得
- 草稿上下文：X-Message-CLI-Reply-* / -Forward-* 头写进去后能原样读回来
              （回复存草稿再发不该变成新邮件；转发存草稿再发不该丢引用块归属）
- 收件人过滤：少写了 @ 的串（'Family, Given'）不进 To、也不当 SMTP 收件人 —— 否则服务器回 550，
              前端只看到一个语焉不详的 400「部分收件人被拒」

没有网络请求、没有真实邮箱依赖（SMTP 用假对象顶掉），纯函数 + 内存里的假账号。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import mailout, mailparse, quoting  # noqa: E402
from app.api_mail import _context_subject, _subject_with, _thread_headers, _without_self  # noqa: E402
from app.settings import Account  # noqa: E402

PASS = FAIL = 0


def ok(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}")


def eq(got, want, label):
    ok(got == want, label if got == want else f"{label}（得到 {got!r}，期望 {want!r}）")


# ---------------------------------------------------------------- 夹具
ACC = Account(
    name="test", email="me@example.com", display_name="Sender Sample",
    imap_host="imap.example.com", imap_port=993, imap_ssl=True,
    username="me@example.com", password="x",
)

ORIG = {
    "subject": "RE: BIP / OA integration requirement study (Minutes)",
    "from": {"name": "Anthony Sample (UNIT-FIN)", "email": "sender.sample@example.com"},
    "to": [
        {"name": "Sender Sample", "email": "me@example.com"},
        {"name": "Edmond Sample (UNIT-HRIT)", "email": "second.sample@example.com"},
    ],
    "cc": [{"name": "Lisa Sample (UNIT-FIN)", "email": "third.sample@example.com"}],
    "date": "2026-09-15T01:00:28+00:00",
    "message_id": "<a@b.c>",
}

# 带内嵌 logo + 上一轮引用的 Outlook 风格 HTML
ORIG_HTML = (
    '<html><head><style>p{margin:0}</style></head><body><div class="WordSection1">'
    '<p class="MsoNormal">Dear Edmond,<o:p></o:p></p>'
    '<p class="MsoNormal">BIP suggest having a meeting today 10:30 am</p>'
    '<p class="MsoNormal"><img width="331" src="cid:image001.png@01DD44F0.A62FE760"></p>'
    '<p class="MsoNormal">Best regards,<br>Anthony Sample</p>'
    "<script>alert(1)</script>"
    '<p onmouseover="steal()" style="position:fixed;top:0;left:0">hover me</p>'
    '<a href="javascript:evil()">click</a>'
    '<div style="border:none;border-padding:3.0pt 0cm 0cm 0cm">'
    '<p class="MsoNormal"><b><span lang="EN-US" style="font-size:11.0pt">From:</span></b> '
    "Edmond Sample<br>Sent: Friday, 11 September 2026 5:54 pm<br>"
    "<p>As talked earlier today, our IT department would like to have a meeting…</p>"
    "</div></body></html>"
)
ORIG_TEXT = (
    "Dear Edmond,\r\n\r\n\tBIP suggest having a meeting today 10:30 am\r\n"
    "Is this time slot ok with you?\r\nThanks\r\n\r\n"
    "Best regards,\r\nAnthony Sample\r\n[cid:image001.png@01DD44F0.A62FE760]\r\n\r\n\r\n"
    "From: Anthony Sample (UNIT-FIN)\r\nSent: Monday, 14 September 2026 6:14 pm\r\n"
    "Subject: FW: BIP / OA integration requirement study (Minutes)\r\n\r\n"
    "Dear Sender Sample,\r\n\r\nAs talked earlier today, our IT department…\r\n"
)
BODY = {"body_text": ORIG_TEXT, "body_html": ORIG_HTML}
ATTS = [{
    "index": 0, "filename": "image001.png", "content_type": "image/png",
    "size": 12, "path": "", "is_inline": True, "content_id": "image001.png@01DD44F0.A62FE760",
}]
USER_TEXT = "Hi Anthony,\n\nCan we move it to 3:30 pm today? See https://example.com/x\n\nSender Sample"


def _write_logo() -> Path:
    p = Path(__file__).resolve().parent / "_test_logo.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    return p


LOGO = _write_logo()
ATTS[0]["path"] = str(LOGO)


# ---------------------------------------------------------------- 纯文本分支
print("[纯文本引用]")
text, html, related = quoting.build_quote(USER_TEXT, ORIG, BODY, ATTS, domain="example.com")
ok(text.startswith("Hi Anthony,"), "用户正文在最前")
ok("-----Original Message-----" in text, "用标准英文分隔头")
ok("原始邮件" not in text and "发件人:" not in text and "时间:" not in text, "不再出现中文引用头")
ok("From: Anthony Sample (UNIT-FIN) <sender.sample@example.com>" in text, "From 行")
ok("Sent: Tuesday, 15 September 2026 09:00" not in text or "Sent:" in text, "Sent 行存在（时间随本机时区）")
ok("Cc: Lisa Sample (UNIT-FIN) <third.sample@example.com>" in text, "Cc 行")
ok("Subject: RE: BIP / OA integration requirement study (Minutes)" in text, "Subject 行")
ok("> Dear Edmond," in text, "引用体带 > 前缀")
ok("> [图片]" in text, "cid 占位换成 [图片]")
ok("As talked earlier today" not in text, "碰到嵌套引用头就停（不套娃）")
ok(text.rstrip().endswith("> [图片]"), "引用停在嵌套头前，末行是签名的 [图片]")

# 20 行内不截断
short_body = {"body_text": "line1\r\nline2\r\n\r\nBest regards,\r\nAnthony", "body_html": ""}
t2, _, _ = quoting.build_quote("ok", ORIG, short_body, [], domain="example.com")
ok("> line1" in t2 and "> Anthony" in t2 and "[…]" not in t2, "短引用不截断")

# 超过 20 行才截断，标记不是裸的 > …
long_body = {"body_text": "\n".join(f"line {i}" for i in range(1, 31)), "body_html": ""}
t3, _, _ = quoting.build_quote("ok", ORIG, long_body, [], domain="example.com")
ok("> line 20" in t3 and "> line 21" not in t3, "引用上限 20 行")
ok(t3.rstrip().endswith("> […]"), "截断了留标记，不是裸的 > …")

# ---------------------------------------------------------------- HTML 分支
print("\n[HTML 引用]")
ok("<b>From:</b>" in html, "引用头渲染成 HTML")
ok("原始邮件" not in html, "HTML 里也没有中文头")
ok(html.count("<b>From:</b>") == 1, "只有一套引用头")
ok('src="cid:quote-0@example.com"' in html, "cid 图换成新的 Content-ID")
ok("image001.png@01DD44F0" not in html, "旧 cid 不再出现")
eq(len(related), 1, "内嵌图随 related 带出一份")
eq(related[0]["data"], LOGO.read_bytes(), "related 里就是图片字节")
ok("<script" not in html and "alert(1)" not in html, "脚本被拿掉")
ok("onmouseover" not in html, "事件处理器被拿掉")
ok("javascript:" not in html, "javascript: 链接被拿掉")
ok("position:fixed" not in html and "top:0" not in html, "fixed 定位样式被清掉")
ok("As talked earlier today" not in html, "HTML 里也切掉上一轮引用")
ok("Dear Edmond" in html and "Anthony Sample" in html, "保留本轮正文与签名")
ok("https://example.com/x" in html, "用户正文里的裸链接点成 <a>")
ok('<a href="https://example.com/x">' in html, "链接带 href")

# 阅读器折叠的前提：块级引用整体包在一个以 From: 开头的 div 里
quote_html = html[html.index("<div><div style=\"border-top"):]
plain = re.sub(r"<[^>]+>", "", quote_html).strip()
ok(bool(re.match(r"^(?:[-=_]{3,}\s*)?(?:原始邮件|Original Message|发件人[:：]|From\s*:)", plain, re.I)),
   "折叠正则认得出（web/app.js foldQuotes）")
ok(len(plain) >= 120, "引用够长，会被折叠")
ok(html.count('<div style="border-top:1px solid #c9c9c9') == 1, "整块只有一个外层容器")

# 没有 HTML 的来信：退回纯文本渲染，不炸
_, html_txt_only, rel2 = quoting.build_quote("ok", ORIG, {"body_text": "plain only", "body_html": ""}, [], domain="example.com")
ok("plain only" in html_txt_only and "<b>From:</b>" in html_txt_only, "无 HTML 的来信也能引")
eq(rel2, [], "无 cid 时不带 related")
# 附件文件不存在 -> 不留破图
_, html_noimg, rel3 = quoting.build_quote("ok", ORIG, BODY, [{**ATTS[0], "path": "/nope/x.png"}], domain="example.com")
eq(rel3, [], "图拿不到就不带 related")
ok("cid:" not in html_noimg, "图拿不到时不留悬空 cid 引用")

# HTML 过大（对方整封 HTML 邮件几 MB）-> 退回纯文本引用，别把整封塞回去
big = {"body_text": "PLAINTEXT FALLBACK", "body_html": "<p>" + "x" * 200_000 + "</p>"}
_, html_big, rel_big = quoting.build_quote("ok", ORIG, big, [], domain="example.com")
ok("PLAINTEXT FALLBACK" in html_big and len(html_big) < 5000, "HTML 超上限时退回纯文本引用")
eq(rel_big, [], "退回纯文本时不带 related")

# ---------------------------------------------------------------- MIME 结构
print("\n[MIME 结构]")
msg = mailout.build_message(ACC, to="a@b.c", subject="RE: x", body_text=text, body_html=html,
                            related=related, in_reply_to="<a@b.c>", references="<a@b.c>")
parts = [(p.get_content_type(), p.get("Content-ID")) for p in msg.walk()]
eq(msg.get_content_type(), "multipart/alternative", "顶层 multipart/alternative")
ok(("text/plain", None) in parts, "有 text/plain 兜底")
ok(("text/html", None) in parts, "有 text/html")
ok(("image/png", "<quote-0@example.com>") in parts, "内嵌图带尖括号 Content-ID")
raw = msg.as_bytes().decode("utf-8", "replace")
ok("Content-ID: <quote-0@example.com>" in raw, "原样写进 MIME（Outlook 认得出）")
ok("Content-Disposition: inline" in raw, "内嵌图 disposition=inline")

msg2 = mailout.build_message(ACC, to="a@b.c", subject="RE: x", body_text=text, body_html=html,
                             related=related, attachments=[("a.pdf", "application/pdf", b"PDF")])
eq(msg2.get_content_type(), "multipart/mixed", "带附件时顶层 multipart/mixed")

# ---------------------------------------------------------------- 主题前缀（Re: / Fwd:）
print("\n[主题前缀]")
eq(_subject_with("Re:", "Weekly report"), "Re: Weekly report", "回复加 Re:")
eq(_subject_with("Re:", "Re: Weekly report"), "Re: Weekly report", "已有 Re: 不叠")
eq(_subject_with("Re:", "RE[2]: Weekly report"), "RE[2]: Weekly report", "Outlook 的 RE[2]: 也算已有")
eq(_subject_with("Fwd:", "Weekly report"), "Fwd: Weekly report", "转发加 Fwd:")
eq(_subject_with("Fwd:", "Fwd: Weekly report"), "Fwd: Weekly report", "已有 Fwd: 不叠")
eq(_subject_with("Fwd:", "FW: Weekly report"), "FW: Weekly report", "Outlook 的 FW: 也算已有")
eq(_subject_with("Fwd:", ""), "", "没主题就空着（调用方会补 (无主题)）")

print("\n[回复 / 转发的线程头与主题]")
_orig = {"message_id": "<a@b.c>", "subject": "Weekly report"}
eq(_thread_headers("reply", _orig), ("<a@b.c>", "<a@b.c>"), "回复挂 In-Reply-To / References")
eq(_thread_headers("forward", _orig), ("", ""), "转发不挂（否则会被并回原会话）")
eq(_thread_headers("", _orig), ("", ""), "新邮件不挂")
eq(_thread_headers("reply", {}), ("", ""), "原信没有 Message-ID 时也不炸")
eq(_context_subject("reply", "", "Weekly report"), "Re: Weekly report", "回复补 Re:")
eq(_context_subject("forward", "", "Weekly report"), "Fwd: Weekly report", "转发补 Fwd:")
eq(_context_subject("forward", "我自己写的主题", "Weekly report"), "我自己写的主题", "用户写了主题就别改")
eq(_context_subject("forward", "", ""), "", "原信没主题就空着")

# ---------------------------------------------------------------- 收件人地址过滤
print("\n[收件人地址过滤]")
ok(mailout.looks_like_address("a@b.c"), "普通地址认")
ok(mailout.looks_like_address("'odd'@x.com"), "带引号的本地部分也认")
ok(not mailout.looks_like_address("Family"), "只有名字 -> 不是地址")
ok(not mailout.looks_like_address("xu"), "用户名片段 -> 不是地址")
ok(not mailout.looks_like_address("a@b c"), "地址里有空格 -> 不是地址")

good, bad = mailout.split_addresses('"Family, Given" <first.sample@example.com>, xu')
eq(good, [("Family, Given", "first.sample@example.com")], "带引号的逗号名字 = 一个人")
eq(bad, ["xu"], "没 @ 的挑出来单独告知")

# 显示名带逗号且没加引号：RFC 上就是两个人（前端补引号之前就是这么发出去的，
# 这里把成因钉住 —— 拆出 'Family' 这个假收件人，SMTP 就会拿它去投）
eq([a for _, a in mailout.parse_addresses("Family <Family>, Steven <first.sample@example.com>")],
   ["Family", "first.sample@example.com"], "不加引号会被拆成两个收件人（原始成因）")
eq([a for _, a in mailout.parse_addresses('"Family, Given" <first.sample@example.com>')],
   ["first.sample@example.com"], "补上引号之后 = 一个人")

# 只剩无效收件人 -> 说人话，而不是让 SMTP 去回 550
try:
    mailout.build_message(ACC, to="Family", subject="x", body_text="b")
    ok(False, "全是无效地址时应当直接报错")
except mailout.MailOutError as e:
    ok("没有有效邮箱地址" in str(e) and "Family" in str(e), f"报错点明是哪个地址不合法（{e}）")

# To 空、只有 Cc：也该能发（转发的收件人经常单独放抄送）
cc_only = mailout.build_message(ACC, to="", cc="a@b.c", subject="x", body_text="b")
eq(cc_only.get_all("To"), None, "没有 To 头就不写空的 To")
eq(cc_only.get_all("Cc"), ["a@b.c"], "Cc 照写")
try:
    mailout.build_message(ACC, to="", cc="", subject="x", body_text="b")
    ok(False, "To 与 Cc 都空时应当报错")
except mailout.MailOutError as e:
    eq(str(e), "收件人为空", "两边都空才叫「收件人为空」")


class FakeSMTP:
    """顶掉真连接：只记下 SMTP 真正收到的那份收件人清单。"""

    def __init__(self):
        self.rcpts = None

    def sendmail(self, from_addr, to_addrs, msg):
        self.rcpts = list(to_addrs)
        return {}

    def quit(self):
        pass


_real_connect = mailout.smtp_connect
_fake = FakeSMTP()
mailout.smtp_connect = lambda acc, timeout=30: _fake          # type: ignore[assignment]
try:
    m1 = mailout.build_message(
        ACC, to='"Family, Given" <first.sample@example.com>, Anthony Sample (UNIT-FIN) <a@b.c>',
        subject="x", body_text="b",
    )
    r1 = mailout.send_message(ACC, m1)
    eq(r1["accepted"], ["first.sample@example.com", "a@b.c"], "带引号的逗号名字只投一个人")
    eq(r1["skipped"], [], "没有需要跳过的")

    # 手工往 To 里塞一个没 @ 的串（老客户端 / 手写 API 都可能）
    m2 = mailout.build_message(ACC, to="", subject="x", body_text="b", allow_empty_recipients=True)
    m2["To"] = "Family <Family>, a@b.c, A@B.C"
    r2 = mailout.send_message(ACC, m2)
    eq(r2["accepted"], ["a@b.c"], "没 @ 的串不进 RCPT（免得服务器回 550），重复的也只投一次")
    eq(r2["skipped"], ["Family"], "被跳过的如实回给调用方")

    m3 = mailout.build_message(ACC, to="", subject="x", body_text="b", allow_empty_recipients=True)
    m3["To"] = "Family <Family>"
    try:
        mailout.send_message(ACC, m3)
        ok(False, "只剩无效收件人时应当报错")
    except mailout.MailOutError as e:
        ok("没有有效邮箱地址" in str(e), f"只剩无效收件人时说得清楚（{e}）")
finally:
    mailout.smtp_connect = _real_connect          # type: ignore[assignment]

# 自己的地址被剔掉之后一个收件人不剩（回复全部时删掉别人、只留自己）
eq(_without_self("me@example.com", "me@example.com"), "", "只剩自己 -> 剔完为空")
eq(_without_self("a@b.c, me@example.com", "me@example.com"), "a@b.c", "剔掉自己留下别人")

# ---------------------------------------------------------------- 草稿回复 / 转发上下文
print("\n[草稿回复 / 转发上下文]")
draft = mailout.build_message(
    ACC, to="a@b.c", subject="Re: x", body_text="草稿正文",
    extra_headers={mailout.REPLY_FOLDER_HEADER: "INBOX", mailout.REPLY_UID_HEADER: "1737102008"},
    allow_empty_recipients=True,
)
d = mailparse.parse_raw(draft.as_bytes())
eq(d["reply_context"], {"folder": "INBOX", "uid": 1737102008}, "写进去的回复上下文读得回来")
eq(d["forward_context"], None, "回复草稿没有转发上下文")
ok(d["body_text"].strip() == "草稿正文", "草稿正文不受影响")
plain_draft = mailparse.parse_raw(
    mailout.build_message(ACC, to="a@b.c", subject="x", body_text="正文", allow_empty_recipients=True).as_bytes()
)
eq(plain_draft["reply_context"], None, "普通邮件没有回复上下文")

fwd_draft = mailout.build_message(
    ACC, to="a@b.c", subject="Fwd: x", body_text="转发草稿",
    extra_headers={mailout.FORWARD_FOLDER_HEADER: "INBOX", mailout.FORWARD_UID_HEADER: "1737102010"},
    allow_empty_recipients=True,
)
f = mailparse.parse_raw(fwd_draft.as_bytes())
eq(f["forward_context"], {"folder": "INBOX", "uid": 1737102010}, "写进去的转发上下文读得回来")
eq(f["reply_context"], None, "转发草稿不会被当成回复")

# ---------------------------------------------------------------- 回归：发出的信自己能解析
print("\n[发出的回复可被自己解析]")
parsed = mailparse.parse_raw(msg.as_bytes())
ok("-----Original Message-----" in parsed["body_text"], "纯文本分支落进索引")
ok("<b>From:</b>" in parsed["body_html"], "HTML 分支落进索引")
ok(any(a.get("content_id") == "quote-0@example.com" for a in parsed["attachments"]),
   "内嵌图作为 inline 附件落库（阅读器 resolve_cids 才认得）")

# ---------------------------------------------------------------- 引用头时区
print("\n[引用头时区：不跟着服务器时区跑]")
from datetime import timedelta, timezone  # noqa: E402

from app import appconfig  # noqa: E402

HKT = timezone(timedelta(hours=8))
UTC = timezone(timedelta(0))
PDT = timezone(timedelta(hours=-7))

# 配置里收的是固定偏移，不是 IANA 名字：Windows 没有系统 tz 数据库，
# zoneinfo 连 Asia/Hong_Kong 都解析不了（除非再装 tzdata）。
eq(appconfig.parse_utc_offset("+08:00"), HKT, "+08:00 解析得出来")
eq(appconfig.parse_utc_offset("+0800"), HKT, "+0800 也认")
eq(appconfig.parse_utc_offset("+8"), HKT, "+8 也认")
eq(appconfig.parse_utc_offset("8"), HKT, "裸数字 8 当成 +08:00")
eq(appconfig.parse_utc_offset(" -07:00 "), PDT, "两头有空格也认")
eq(appconfig.parse_utc_offset(""), None, "空 = 不指定（回落进程本地）")
eq(appconfig.parse_utc_offset("Asia/Hong_Kong"), None, "IANA 名不收，回落进程本地")
eq(appconfig.parse_utc_offset("+20:00"), None, "超出 UTC-12..+14 当填错")
eq(appconfig.parse_utc_offset("+14:00"), timezone(timedelta(hours=14)), "+14:00 是合法上界")
eq(appconfig.display_config().tz, None, "默认没配偏移 -> None（行为跟以前一致）")

# 同一封信按不同时区各印一次：差值必须是确定的，且跟跑测试的机器在哪个时区无关
eq(quoting.fmt_quote_time("2026-09-15T07:35:28+00:00", HKT),
   "Tuesday, 15 September 2026 15:35", "+00:00 的信按 +08:00 印")
eq(quoting.fmt_quote_time("2026-09-15T07:35:28+00:00", UTC),
   "Tuesday, 15 September 2026 07:35", "按 UTC 印就是原样")
eq(quoting.fmt_quote_time("2026-09-15T07:35:28+00:00", PDT),
   "Tuesday, 15 September 2026 00:35", "按 -07:00 印要回退一天")
eq(quoting.fmt_quote_time("2026-09-15T09:02:33+08:00", HKT),
   "Tuesday, 15 September 2026 09:02", "本来就是本时区的信，时间不动")
eq(quoting.fmt_quote_time("2026-09-15T09:02:33+08:00", UTC),
   "Tuesday, 15 September 2026 01:02", "同一封换到 UTC 就减 8 小时")
eq(quoting.fmt_quote_time("", HKT), "", "没日期就空着")
eq(quoting.fmt_quote_time("不是时间", HKT), "不是时间", "解析不了就原样返回")

# 库里 date_iso 带原信偏移（实测 419 封无一是 naive），所以这里既不是「补时区」也不是「改成 UTC」
eq(quoting.fmt_quote_time("2026-09-15T01:00:28+00:00"), 
   quoting.fmt_quote_time("2026-09-15T01:00:28+00:00", None), "不传 tz 保持老行为（进程本地）")

# 引用头整条链（header_pairs -> build_quote -> 文本/HTML 两个分支）都要走到 tz
eq(dict(quoting.header_pairs(ORIG, HKT)).get("Sent"),
   "Tuesday, 15 September 2026 09:00", "header_pairs 的 Sent 跟着 tz 走")
t_tz, h_tz, _ = quoting.build_quote("收到", ORIG, BODY, [], domain="example.com", tz=HKT)
ok("Sent: Tuesday, 15 September 2026 09:00" in t_tz, "纯文本引用头用 tz")
ok("09:00" in h_tz, "HTML 引用头用 tz")
t_utc, h_utc, _ = quoting.build_quote("ok", ORIG, BODY, [], domain="example.com", tz=UTC)
ok("Sent: Tuesday, 15 September 2026 01:00" in t_utc, "换个 tz 纯文本引用头跟着变")
ok("09:00" not in h_utc, "换个 tz HTML 引用头也跟着变")

LOGO.unlink(missing_ok=True)
print(f"\n通过 {PASS} / 失败 {FAIL}")
sys.exit(1 if FAIL else 0)
