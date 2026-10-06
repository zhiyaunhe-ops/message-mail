"""会话归组：把来回往来的邮件聚成一条会话（类似 IM 的聊天窗口）。

策略（纯本地，无需 Thread 扩展支持）：
  1. In-Reply-To / References 命中已知 Message-ID  -> 直接串联（最可靠）
  2. 主题归一化（剥掉 Re:/答复:/转发: 等前缀与标点）相同
     + 参与者有交集（排除自己）+ 时间相近      -> 视为同一会话
  3. 单一邮件自成一条会话

thread_id 由「根邮件（最早那封）的 folder:uid」哈希而来，稳定且可写回数据库。
"""
from __future__ import annotations

import hashlib
import json
import re

_PREFIX_RE = re.compile(
    r"^\s*(?:\[[^\]]{0,24}\]\s*)*"
    r"(?:(?:re|aw|antw|fw|fwd|wg|tr|rv|enc|odp|答复|回覆|回复|轉寄|转发)\s*[:：]\s*|\(\s*(?:re|fw|fwd)\s*\)\s*)+",
    re.I,
)
_NOISE_RE = re.compile(r"[^\w\u4e00-\u9fff ]+")
_WS_RE = re.compile(r"\s+")

# 同一主题下，两封邮件相隔超过这个天数就不再认为是同一会话
MAX_GAP_DAYS = 180


def normalize_subject(subject: str) -> str:
    """把 'Re: Re: 【重要】周会纪要' 归一成 '重要周会纪要'。"""
    s = (subject or "").strip()
    prev = None
    while prev != s:
        prev = s
        s = _PREFIX_RE.sub("", s).strip()
    s = _NOISE_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip().lower()
    return s


def subject_title(subject: str) -> str:
    """展示用的标题：只剥一层前缀，保留原文大小写。"""
    s = (subject or "").strip()
    prev = None
    while prev != s:
        prev = s
        s = _PREFIX_RE.sub("", s).strip()
    return s or "(无主题)"


def _f(rec, key: str, default=""):
    """同时兼容 sqlite3.Row 与 dict。"""
    try:
        v = rec[key]
    except (IndexError, KeyError, TypeError):
        v = None
    if v is None and hasattr(rec, "get"):
        v = rec.get(key)
    return default if v is None else v


def _emails(rec: dict, own: set[str]) -> set[str]:
    out = set()
    addr = str(_f(rec, "from_addr") or "").strip().lower()
    if addr and addr not in own:
        out.add(addr)
    for key in ("to_json", "cc_json"):
        raw = _f(rec, key) or "[]"
        try:
            people = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            people = []
        for p in people or []:
            a = str((p or {}).get("email") or "").strip().lower()
            if a and a not in own:
                out.add(a)
    return out


class _UF:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def _thread_id(folder: str, uid: int) -> str:
    return "t" + hashlib.sha1(f"{folder}:{uid}".encode("utf-8")).hexdigest()[:14]


def rebuild(store, own_emails: set[str] | None = None, progress=None) -> dict:
    """重算全部邮件的 thread_id 并写回数据库。返回统计。"""
    own = {a.strip().lower() for a in (own_emails or set()) if a}
    rows = store.conn.execute(
        "SELECT folder,uid,subject,from_addr,to_json,cc_json,date_ts,message_id,in_reply_to,thread_id "
        "FROM messages ORDER BY date_ts ASC, uid ASC"
    ).fetchall()
    n = len(rows)
    uf = _UF(n)
    if progress:
        progress(f"归组 {n} 封邮件为会话…")

    # 1) In-Reply-To / References 串联
    by_msgid: dict[str, int] = {}
    for i, r in enumerate(rows):
        mid = (r["message_id"] or "").strip().strip("<>").lower()
        if mid:
            by_msgid.setdefault(mid, i)

    for i, r in enumerate(rows):
        for ref in ((r["in_reply_to"] or ""),):
            ref = ref.strip().strip("<>").lower()
            if not ref:
                continue
            # References 可能含多个，取每个都能查到的
            for one in re.findall(r"<[^>]+>", ref) or ([ref] if ref else []):
                one = one.strip("<>").lower()
                j = by_msgid.get(one)
                if j is not None and j != i:
                    uf.union(i, j)

    # 2) 主题 + 参与者 + 时间
    groups: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        key = normalize_subject(r["subject"])
        if len(key) < 4:  # 空主题 / 太泛的主题不做主题归并
            continue
        groups.setdefault(key, []).append(i)

    gap = MAX_GAP_DAYS * 86400
    for key, idxs in groups.items():
        idxs.sort(key=lambda i: (rows[i]["date_ts"] or 0, rows[i]["uid"]))
        for a, b in zip(idxs, idxs[1:]):
            ra, rb = rows[a], rows[b]
            ta = ra["date_ts"] or 0
            tb = rb["date_ts"] or 0
            if ta and tb and abs(tb - ta) > gap:
                continue
            if _emails(ra, own) & _emails(rb, own):
                uf.union(a, b)

    # 3) 落库
    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(uf.find(i), []).append(i)

    updates: list[tuple[str, str, int]] = []
    for members in clusters.values():
        root = min(members, key=lambda i: (rows[i]["date_ts"] or 0, rows[i]["uid"]))
        tid = _thread_id(rows[root]["folder"], rows[root]["uid"])
        for i in members:
            if rows[i]["thread_id"] != tid:
                updates.append((tid, rows[i]["folder"], rows[i]["uid"]))

    with store._lock:
        store.conn.executemany("UPDATE messages SET thread_id=? WHERE folder=? AND uid=?", updates)
        store.conn.commit()

    return {
        "messages": n,
        "threads": len(clusters),
        "grouped": sum(1 for m in clusters.values() if len(m) > 1),
        "changed": len(updates),
    }
