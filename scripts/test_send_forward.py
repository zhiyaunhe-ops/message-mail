"""发信端点（回复 / 转发）的回归测试：python scripts/test_send_forward.py

这个文件盯的是「点发送之后到底发了什么」，全部离线：
- 转发：主题补 Fwd:（不重复叠）、**不挂 In-Reply-To**、正文带引用块、mode=forward
- 回复：主题补 Re:、挂 In-Reply-To、mode=reply
- 收件人：显示名带逗号的人只算一个人（老代码会被拆成两个人，其中一个被服务器 550 拒收）
- 收件人：少写了 @ 的串不进 To / 不进 RCPT，并在响应里如实回报 dropped
- 只发抄送（To 空 Cc 有）也能发；只填自己会被说清楚，而不是「收件人为空」
- 失败要留痕：发信失败会写进同步日志（光看前端红字查不出原因）

SMTP / IMAP 写连接 / 账号与 Store 全部用假对象顶掉，不碰网络、不碰真实邮箱。
"""
from __future__ import annotations

import contextlib
import email
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402

from app import api_mail, mailout  # noqa: E402
from app.context import ctx  # noqa: E402
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


def code_of(fn):
    """跑一下 fn()，返回 ('ok', 结果) 或 (状态码, 错误文案)。"""
    try:
        return "ok", fn()
    except HTTPException as e:
        return e.status_code, str(e.detail)


# ---------------------------------------------------------------- 夹具
ACC = Account(
    name="test", email="me@example.com", display_name="Sender Sample",
    imap_host="imap.example.com", imap_port=993, imap_ssl=True,
    username="me@example.com", password="x",
)

ORIG = {
    "folder": "INBOX", "uid": 7,
    "subject": "RE: BIP / OA integration requirement study",
    "from": {"name": "Edmond Sample (UNIT-HRIT)", "email": "second.sample@example.com"},
    "to": [{"name": "Me", "email": "me@example.com"}],
    "cc": [],
    "date": "2026-09-15T01:00:28+00:00",
    "message_id": "<orig@example.com>",
}
BODY = {"body_text": "Dear Sender,\r\n\r\nour IT department would like to have a meeting.\r\n",
        "body_html": "<p>Dear Sender,</p><p>our IT department would like to have a meeting.</p>"}


class FakeStore:
    def get_message(self, folder, uid):
        return ORIG if (folder, uid) == ("INBOX", 7) else None

    def get_body(self, folder, uid):
        return BODY

    def get_attachments(self, folder, uid):
        return []

    def delete_message(self, folder, uid):
        return None


class FakeSMTP:
    """记下 SMTP 收到的那份 RCPT；refuse 有值时模拟「部分收件人被拒」。"""

    def __init__(self):
        self.rcpts = None
        self.raw = b""
        self.refuse = None

    def sendmail(self, from_addr, to_addrs, msg):
        self.rcpts = list(to_addrs)
        self.raw = bytes(msg)
        return self.refuse or {}

    def quit(self):
        pass


class FakeIMAP:
    def append_message(self, raw, folder, flags=None):
        return 999

    def delete_message(self, uid, folder):
        return None


@contextlib.contextmanager
def _fake_write():
    yield FakeIMAP()


SMTP = FakeSMTP()
LOG: list[str] = []


def install_fakes():
    api_mail.account = lambda: ACC            # type: ignore[assignment]
    api_mail.store = lambda: FakeStore()      # type: ignore[assignment]
    mailout.smtp_connect = lambda acc, timeout=30: SMTP      # type: ignore[assignment]
    ctx.imap_write = _fake_write              # type: ignore[assignment]
    ctx.sync.log_add = lambda m: LOG.append(m)               # type: ignore[assignment]


def send(**kw):
    """直调端点：Form(...) 的默认值在函数直调时不是字符串，得逐个补齐。"""
    SMTP.__init__()
    args = dict(to="", cc="", subject="", body="",
                reply_folder="", reply_uid=0, forward_folder="", forward_uid=0,
                draft_folder="", draft_uid=0, files=[])
    args.update(kw)
    return api_mail.send_mail(**args)


def parts_of(raw: bytes) -> tuple[str, str, email.message.Message]:
    msg = email.message_from_bytes(raw)
    text = html = ""
    for part in msg.walk():
        ct = part.get_content_type()
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        if ct == "text/plain":
            text += payload.decode("utf-8", "replace")
        elif ct == "text/html":
            html += payload.decode("utf-8", "replace")
    return text, html, msg


install_fakes()

# ---------------------------------------------------------------- 转发
print("[转发]")
r = send(to="sheldonslxu@cmhhk.org.hk", cc="", subject="", body="转你一份。",
         forward_folder="INBOX", forward_uid=7)
text, html, msg = parts_of(SMTP.raw)
eq(r["mode"], "forward", "响应标出这是转发")
eq(msg.get("Subject"), "Fwd: RE: BIP / OA integration requirement study", "主题补 Fwd:（原信已有 RE: 也照加）")
eq(msg.get_all("In-Reply-To"), None, "转发不挂 In-Reply-To（否则会被并回原会话）")
eq(msg.get_all("References"), None, "References 也不挂")
eq(r["accepted"], ["sheldonslxu@cmhhk.org.hk"], "投递到填的收件人")
ok("转你一份。" in text and "-----Original Message-----" in text, "纯文本分支：自己的话 + 引用头")
ok("From: Edmond Sample (UNIT-HRIT) <second.sample@example.com>" in text, "引用头里带原发件人")
ok("our IT department would like to have a meeting." in text, "引用体带原文")
ok("<b>From:</b>" in html and "border-top:1px solid #c9c9c9" in html, "HTML 分支带引用块（阅读器据此折叠）")
ok(r.get("dropped") == [], "没有需要忽略的收件人")

print("\n[转发：用户自己写了主题 / 只有抄送]")
r = send(to="", cc="janet.sample@example.com", subject="我自己写的主题", body="",
         forward_folder="INBOX", forward_uid=7)
_, _, msg = parts_of(SMTP.raw)
decoded = str(email.header.make_header(email.header.decode_header(msg.get("Subject"))))
eq(decoded, "我自己写的主题", "用户写了主题就尊重用户")
eq(msg.get_all("To"), None, "To 为空就不写新的 To 头")
eq(r["accepted"], ["janet.sample@example.com"], "只填抄送也能发出去（转发的收件人常常放抄送）")

# ---------------------------------------------------------------- 回复
print("\n[回复]")
r = send(to="second.sample@example.com", cc="", subject="", body="收到。",
         reply_folder="INBOX", reply_uid=7)
_, _, msg = parts_of(SMTP.raw)
eq(r["mode"], "reply", "响应标出这是回复")
eq(msg.get("In-Reply-To"), "<orig@example.com>", "回复挂 In-Reply-To")
eq(msg.get("References"), "<orig@example.com>", "References 同值")
eq(msg.get("Subject"), "RE: BIP / OA integration requirement study", "原信已有 RE: 就不叠成 Re: RE:")

# ---------------------------------------------------------------- 收件人
print("\n[收件人：显示名带逗号]")
r = send(to='"Family, Given" <first.sample@example.com>', forward_folder="INBOX", forward_uid=7)
_, _, msg = parts_of(SMTP.raw)
eq(r["accepted"], ["first.sample@example.com"], "逗号名字只算一个人（老代码会拆成 Family + Steven）")
eq(msg.get("To"), '"Family, Given" <first.sample@example.com>', "To 头带引号写出去")

r = send(to='"Family, Given" <first.sample@example.com>, Anthony Sample (UNIT-FIN) <a@example.com>',
         forward_folder="INBOX", forward_uid=7)
eq(r["accepted"], ["first.sample@example.com", "a@example.com"], "多个人也不串味")

print("\n[收件人：少写 @ 的串]")
r = send(to="a@b.c, Family", forward_folder="INBOX", forward_uid=7)
eq(r["accepted"], ["a@b.c"], "没 @ 的串不投")
eq(r["dropped"], ["Family"], "如实回报被忽略的收件人（前端会提示）")

status, detail = code_of(lambda: send(to="Family", forward_folder="INBOX", forward_uid=7))
eq(status, 400, "全是无效地址 -> 400")
ok("没有有效邮箱地址" in detail and "Family" in detail, f"报错点明是哪个地址（{detail}）")

status, detail = code_of(lambda: send(to="me@example.com", forward_folder="INBOX", forward_uid=7))
eq(status, 400, "只填自己 -> 400")
ok("只有你自己" in detail, f"说清楚是自己被剔掉了（{detail}）")

status, detail = code_of(lambda: send(**{}))
eq(status, 400, "一个收件人都没有 -> 400")
eq(detail, "收件人为空", "空收件人还是报「收件人为空」")

print("\n[发信失败要留痕]")
LOG.clear()
SMTP_refuse = {"nobody@example.com": (550, b"5.1.1 user unknown")}


class RefusingSMTP(FakeSMTP):
    def sendmail(self, from_addr, to_addrs, msg):
        self.rcpts = list(to_addrs)
        self.raw = bytes(msg)
        return SMTP_refuse


mailout.smtp_connect = lambda acc, timeout=30: RefusingSMTP()   # type: ignore[assignment]
status, detail = code_of(lambda: send(to="nobody@example.com", forward_folder="INBOX", forward_uid=7))
eq(status, 400, "对方服务器拒收 -> 400")
ok("部分收件人被拒" in detail, f"把服务器的原话带出来（{detail}）")
ok(any("send failed" in m and "nobody@example.com" in m for m in LOG),
   "失败写进同步日志（收件人 + 原因都在）")
install_fakes()

print(f"\n通过 {PASS} / 失败 {FAIL}")
sys.exit(1 if FAIL else 0)
