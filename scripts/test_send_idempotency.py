"""发信幂等 + 草稿归并的回归测试：python scripts/test_send_idempotency.py

盯的是 2026-10-13 实测出来的两条真实故障：

1. 重复投递：前端在「自动保存在途」时按钮还没禁用，同一次点击被并发提交了两次，
   两封的 Message-ID 只差 1 个时间单位（≤10ms）就都发出去了。
   这里守住后端的第二道防线 —— 同一个 client_token 只真正投递一次。

2. 草稿堆积：草稿身份只认服务器 UID 链，关窗重开 / 刷新页面就断链，同一次写信在草稿箱里
   一路新建（实测同一封堆了 6 份），而且发出去之后一份都没删。
   这里守住 draft_key 那条链：存草稿按 key 顶掉旧版、发信按 key 收网。

没有网络、没有真实邮箱：IMAP 与 Store 全部用假对象顶掉，纯逻辑判定。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import api_mail, mailout, mailparse  # noqa: E402
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
    name="test", email="me@example.com", display_name="Example User",
    imap_host="imap.example.com", imap_port=993, imap_ssl=True,
    username="me@example.com", password="x",
)


class FakeConn:
    """够用的假 IMAP 连接：只实现 _draft_uids_by_key / 删除用到的三个动作。"""

    def __init__(self, by_key: dict[str, list[int]] | None = None, fail_delete: tuple = ()):
        self.by_key = by_key or {}
        self.fail_delete = set(fail_delete)
        self.selected: list[str] = []
        self.searches: list[list[str]] = []
        self.deleted: list[tuple[str, int]] = []

    def select(self, folder, readonly=False):
        self.selected.append(folder)

    def search_uids(self, criteria):
        self.searches.append(list(criteria))
        key = criteria[2] if len(criteria) > 2 else ""
        return list(self.by_key.get(key, []))

    def delete_message(self, uid, folder):
        if uid in self.fail_delete:
            raise RuntimeError("mock delete failure")
        self.deleted.append((folder, uid))
        # 删完就不该再被搜出来
        for k, uids in self.by_key.items():
            if uid in uids:
                self.by_key[k] = [u for u in uids if u != uid]


class FakeStore:
    def __init__(self):
        self.deleted: list[tuple[str, int]] = []

    def delete_message(self, folder, uid):
        self.deleted.append((folder, uid))


class FakeCtx:
    def __init__(self, conn: FakeConn, logs: list[str]):
        self.conn = conn
        self.logs = logs
        self.sync = SimpleNamespace(log_add=logs.append)

    def imap_write(self):
        return _CM(self.conn)


class _CM:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False


def run_purge(by_key, key="K1", uid_hint=0, keep_uid=0, fail_delete=()):
    """跑一次 _purge_stale_drafts，返回 (清掉几封, 服务器删除动作, 本地索引删除动作, 日志)。"""
    conn, store, logs = FakeConn(by_key, fail_delete), FakeStore(), []
    with mock.patch.object(api_mail, "ctx", FakeCtx(conn, logs)):
        n = api_mail._purge_stale_drafts(store, "Drafts", keep_uid=keep_uid,
                                        key=key, uid_hint=uid_hint)
    return n, conn.deleted, store.deleted, logs


def reset_tokens():
    with api_mail._send_lock:
        api_mail._send_tokens.clear()


# ---------------------------------------------------------------- 发信幂等
print("[发信幂等键：同一个 token 只投递一次]")
reset_tokens()

eq(api_mail._send_token_claim(""), None, "空 token 不做幂等（老客户端 / 直接调 API 不受影响）")
eq(api_mail._send_token_claim(""), None, "再来一次还是放行")

eq(api_mail._send_token_claim("T1"), None, "首次提交放行")
first = api_mail._send_token_claim("T1")
eq(first, {"status": 409, "detail": "同一封邮件正在发送中，已忽略这次重复提交。"},
   "同一个 token 在途时再提交 -> 409 而不是又发一封")

payload = {"ok": True, "message_id": "<m@x>", "accepted": ["a@b.c"], "draft_removed": 6}
api_mail._send_token_settle("T1", "done", payload)
again = api_mail._send_token_claim("T1")
ok(again is not None and again.get("reuse") == payload,
   "投递成功后重复提交 -> 直接复用首次结果（不再投递 SMTP）")

api_mail._send_token_settle("T2", "retry")   # 明确的业务失败（如没有有效收件人）
eq(api_mail._send_token_claim("T2"), None, "说得清原因的失败 -> 不占坑，允许原样重试")

api_mail._send_token_settle("T3", "uncertain")
blocked = api_mail._send_token_claim("T3")
eq(blocked.get("status"), 409, "网络中断（不确定是否已送达）-> 拒绝再次提交")
ok("已发送" in blocked.get("detail", "") and "不再重发" in blocked.get("detail", ""),
   "拒绝时的文案能照着做（让用户先去看「已发送」）")

api_mail._send_token_claim("T4")
with api_mail._send_lock:                     # 把时间戳拨到 TTL 之外
    api_mail._send_tokens["T4"]["at"] = time.time() - api_mail._SEND_TOKEN_TTL - 1
eq(api_mail._send_token_claim("T4"), None, "过了复用窗口之后旧 token 自动失效，可以重发")

eq(api_mail._send_token_claim("T5"), None, "不同 token 互不影响（各发各的）")
eq(api_mail._send_token_claim("T6"), None, "不同 token 互不影响（第二条）")

reset_tokens()

# ---------------------------------------------------------------- 草稿归并
print("\n[草稿归并：按 draft_key 顶掉 / 收网]")

n, srv, loc, logs = run_purge({"K1": [101]}, keep_uid=101)
eq((n, srv, loc), (0, [], []), "只剩自己这一版时不误删")

# 同一封草稿的三个历史版本 + 刚存的 103
n, srv, loc, logs = run_purge({"K1": [101, 102, 103]}, keep_uid=103)
eq(n, 2, "同 key 的另外两版被清掉")
eq(sorted(u for _, u in srv), [101, 102], "服务器上删的是旧版，刚存那版留着")
eq(sorted(u for _, u in loc), [101, 102], "本地索引同步清掉，否则前端草稿箱还看得见")
eq({f for f, _ in srv}, {"Drafts"}, "删在草稿目录里")

# 没有 key 的老草稿：靠前端递过来的 draft_uid 兜底
n, srv, loc, logs = run_purge({}, key="", uid_hint=77, keep_uid=88)
eq(n, 1, "没有 key 的发送仍能删掉前端记住的那一版")
eq(srv, [("Drafts", 77)], "删的正是 draft_uid")

# key 与 uid_hint 一起传：两边都要收
n, srv, loc, logs = run_purge({"K1": [101, 102]}, key="K1", uid_hint=99, keep_uid=102)
eq(sorted(u for _, u in srv), [99, 101], "draft_key 与 draft_uid 各收各的，不重不漏")

# 服务器不给删（权限 / 连接抖动）：只记日志，不能把整封发送拖挂
n, srv, loc, logs = run_purge({"K1": [101, 102]}, key="K1", keep_uid=102, fail_delete=(101,))
eq(n, 0, "删不掉时不虚报清理数量")
eq(loc, [], "服务器没删掉的，本地索引也不删（两边保持一致）")
ok(any("draft replace failed" in m for m in logs), "删失败留下可查的日志")

# 搜索本身报错：整段不能炸
conn = FakeConn()
with mock.patch.object(api_mail, "ctx", FakeCtx(conn, [])):
    conn.search_uids = lambda criteria: (_ for _ in ()).throw(RuntimeError("search down"))
    n = api_mail._purge_stale_drafts(FakeStore(), "Drafts", keep_uid=1, key="K1")
eq(n, 0, "搜索失败时返回 0 而不是抛异常（发信流程不被草稿清理拖挂）")

print("\n[同一个 draft_key 的保存串行]")
lock_a1 = api_mail._draft_lock_for("K1")
lock_a2 = api_mail._draft_lock_for("K1")
lock_b = api_mail._draft_lock_for("K2")
ok(lock_a1 is lock_a2, "同一个 key 拿到同一把锁（并发保存才不会互相删掉对方刚写的）")
ok(lock_a1 is not lock_b, "不同 key 互不阻塞")

# ---------------------------------------------------------------- 头的往返
print("\n[草稿身份落进头里]")
msg = mailout.build_message(
    ACC, to="a@b.c", subject="x", body_text="草稿正文",
    extra_headers={mailout.DRAFT_KEY_HEADER: "key-abc-123"},
    allow_empty_recipients=True,
)
parsed = mailparse.parse_raw(msg.as_bytes())
eq(parsed["draft_key"], "key-abc-123", "写进去的 draft_key 读得回来（关窗重开靠它续上）")

plain = mailparse.parse_raw(
    mailout.build_message(ACC, to="a@b.c", subject="x", body_text="正文",
                          allow_empty_recipients=True).as_bytes()
)
eq(plain["draft_key"], None, "普通邮件 / 老草稿没有 key，不硬造")

with_key = mailout.build_message(
    ACC, to="a@b.c", subject="Re: x", body_text="草稿",
    extra_headers={mailout.DRAFT_KEY_HEADER: "K9", mailout.REPLY_FOLDER_HEADER: "INBOX",
                   mailout.REPLY_UID_HEADER: "1737102008"},
    allow_empty_recipients=True,
)
both = mailparse.parse_raw(with_key.as_bytes())
eq(both["draft_key"], "K9", "草稿身份与回复上下文可以共存")
eq(both["reply_context"], {"folder": "INBOX", "uid": 1737102008}, "共存时回复上下文没被挤掉")

print(f"\n通过 {PASS} / 失败 {FAIL}")
sys.exit(1 if FAIL else 0)
