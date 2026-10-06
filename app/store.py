"""本地 SQLite 缓存：邮件信封、正文、附件落盘，供 WebUI 与 AI 快速读取。"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path

from .settings import DB_FILE, ensure_dirs

# 明显的机器地址（发件人是「no-reply / 通知 / 订阅推送」），联想排序时降权
BULK_LOCAL_RE = re.compile(
    r"^(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|notifications?|newsletters?|news|"
    r"bounces?|mailers?|mailer-daemon|postmaster|updates?)$",
    re.IGNORECASE,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    name TEXT PRIMARY KEY,
    uidvalidity INTEGER DEFAULT 0,
    uidnext INTEGER DEFAULT 0,
    last_synced REAL DEFAULT 0,
    last_error TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS messages (
    folder TEXT NOT NULL,
    uid INTEGER NOT NULL,
    message_id TEXT DEFAULT '',
    subject TEXT DEFAULT '',
    from_name TEXT DEFAULT '',
    from_addr TEXT DEFAULT '',
    to_json TEXT DEFAULT '[]',
    cc_json TEXT DEFAULT '[]',
    date_iso TEXT DEFAULT '',
    date_ts REAL DEFAULT 0,
    flags TEXT DEFAULT '',
    size INTEGER DEFAULT 0,
    has_attach INTEGER DEFAULT 0,
    attach_count INTEGER DEFAULT 0,
    snippet TEXT DEFAULT '',
    in_reply_to TEXT DEFAULT '',
    has_body INTEGER DEFAULT 0,
    PRIMARY KEY (folder, uid)
);
CREATE INDEX IF NOT EXISTS idx_msg_date ON messages(date_ts DESC);
CREATE INDEX IF NOT EXISTS idx_msg_folder_date ON messages(folder, date_ts DESC);
CREATE INDEX IF NOT EXISTS idx_msg_from ON messages(from_addr);
CREATE TABLE IF NOT EXISTS bodies (
    folder TEXT NOT NULL,
    uid INTEGER NOT NULL,
    body_text TEXT DEFAULT '',
    body_html TEXT DEFAULT '',
    headers_json TEXT DEFAULT '{}',
    PRIMARY KEY (folder, uid)
);
CREATE TABLE IF NOT EXISTS attachments (
    folder TEXT NOT NULL,
    uid INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    filename TEXT DEFAULT '',
    content_type TEXT DEFAULT '',
    size INTEGER DEFAULT 0,
    path TEXT DEFAULT '',
    is_inline INTEGER DEFAULT 0,
    content_id TEXT DEFAULT '',
    PRIMARY KEY (folder, uid, idx)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT DEFAULT ''
);
"""

MESSAGE_UPSERT = """INSERT INTO messages(folder,uid,message_id,subject,from_name,from_addr,to_json,cc_json,
                   date_iso,date_ts,flags,size,has_attach,attach_count,snippet,in_reply_to,has_body)
               VALUES(:folder,:uid,:message_id,:subject,:from_name,:from_addr,:to_json,:cc_json,
                   :date_iso,:date_ts,:flags,:size,:has_attach,:attach_count,:snippet,:in_reply_to,:has_body)
               ON CONFLICT(folder,uid) DO UPDATE SET
                   message_id=excluded.message_id, subject=excluded.subject, from_name=excluded.from_name,
                   from_addr=excluded.from_addr, to_json=excluded.to_json, cc_json=excluded.cc_json,
                   date_iso=excluded.date_iso, date_ts=excluded.date_ts, flags=excluded.flags,
                   size=excluded.size, has_attach=excluded.has_attach, attach_count=excluded.attach_count,
                   snippet=excluded.snippet, in_reply_to=excluded.in_reply_to, has_body=excluded.has_body"""
BODY_UPSERT = (
    "INSERT INTO bodies(folder,uid,body_text,body_html,headers_json) VALUES(?,?,?,?,?) "
    "ON CONFLICT(folder,uid) DO UPDATE SET body_text=excluded.body_text, body_html=excluded.body_html, "
    "headers_json=excluded.headers_json"
)


class Store:
    """SQLite 索引。

    连接策略：**每线程一个连接**（thread-local），WAL 模式下多读单写；
    写操作仍用一把全局锁串行化，避免 "database is locked" 竞争。
    这样既不共享有状态连接，也不需要 check_same_thread 打补丁。
    """

    def __init__(self, db_path: Path | str = DB_FILE):
        ensure_dirs()
        self.db_path = str(db_path)
        self._lock = threading.RLock()              # 写锁（沿用旧名，调用点不变）
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        with self._lock:                            # 建表 + 迁移走本线程连接
            conn = self.conn
            conn.executescript(SCHEMA)
            conn.commit()
            self._migrate()

    # ---------- 连接 ----------
    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.db_path, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=5000")
            self._local.conn = c
            with self._conns_lock:
                self._conns.append(c)
        return c

    def close(self) -> None:
        """关闭本进程内所有线程的连接（服务退出时调用）。"""
        with self._conns_lock:
            conns, self._conns = self._conns, []
        for c in conns:
            try:
                c.close()
            except Exception:
                pass
        if getattr(self._local, "conn", None) is not None:
            try:
                del self._local.conn
            except Exception:
                pass

    # ---------- 基础 ----------
    def _migrate(self) -> None:
        """给早期建好的表补列（CREATE TABLE IF NOT EXISTS 不会加列）。"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(attachments)")}
        if "content_id" not in cols:
            with self._lock:
                self.conn.execute("ALTER TABLE attachments ADD COLUMN content_id TEXT DEFAULT ''")
                self.conn.commit()

        mcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(messages)")}
        with self._lock:
            if "category" not in mcols:
                self.conn.execute("ALTER TABLE messages ADD COLUMN category TEXT DEFAULT ''")
            if "is_boring" not in mcols:
                self.conn.execute("ALTER TABLE messages ADD COLUMN is_boring INTEGER DEFAULT 0")
            if "thread_id" not in mcols:
                self.conn.execute("ALTER TABLE messages ADD COLUMN thread_id TEXT DEFAULT ''")
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_thread ON messages(thread_id)")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_msg_thread_date ON messages(thread_id, date_ts, uid)"
            )
            self.conn.commit()

    def _exec(self, sql: str, params=()):
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def set_meta(self, key: str, value: str) -> None:
        self._exec("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # ---------- 目录 ----------
    def upsert_folder(self, name: str, uidvalidity: int = 0, uidnext: int = 0) -> None:
        self._exec(
            "INSERT INTO folders(name,uidvalidity,uidnext,last_synced) VALUES(?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET uidvalidity=excluded.uidvalidity, uidnext=excluded.uidnext, last_synced=excluded.last_synced",
            (name, uidvalidity, uidnext, time.time()),
        )

    def get_folder(self, name: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM folders WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def list_folders(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM folders ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def set_folder_error(self, name: str, err: str) -> None:
        self._exec("UPDATE folders SET last_error=?, last_synced=? WHERE name=?", (err, time.time(), name))

    def invalidate_folder(self, name: str) -> None:
        """UIDVALIDITY 变化时，旧 UID 全部失效。"""
        with self._lock:
            self.conn.execute("DELETE FROM messages WHERE folder=?", (name,))
            self.conn.execute("DELETE FROM bodies WHERE folder=?", (name,))
            self.conn.execute("DELETE FROM attachments WHERE folder=?", (name,))
            self.conn.commit()

    def drop_folder(self, name: str) -> dict:
        """整个目录从本地索引里移除（服务器目录已删/不再同步时用）。"""
        with self._lock:
            removed = 0
            for t in ("messages", "bodies", "attachments"):
                cur = self.conn.execute(f"DELETE FROM {t} WHERE folder=?", (name,))
                removed += cur.rowcount or 0
            self.conn.execute("DELETE FROM folders WHERE name=?", (name,))
            self.conn.commit()
            return {"folder": name, "removed": removed}

    def folder_counts(self, name: str) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN flags NOT LIKE '%\\Seen%' THEN 1 ELSE 0 END) AS unread "
            "FROM messages WHERE folder=?",
            (name,),
        ).fetchone()
        return {"total": row["total"] or 0, "unread": row["unread"] or 0}

    # ---------- 写入 ----------
    def upsert_message(self, rec: dict) -> None:
        self._exec(MESSAGE_UPSERT, rec)

    def upsert_body(self, folder: str, uid: int, text: str, html: str, headers: dict) -> None:
        self._exec(BODY_UPSERT, (folder, uid, text, html, json.dumps(headers, ensure_ascii=False)))

    def upsert_message_with_body(self, rec: dict, text: str, html: str, headers: dict) -> None:
        """Persist the full message atomically so has_body always matches a body row."""
        with self._lock:
            conn = self.conn
            try:
                conn.execute(MESSAGE_UPSERT, rec)
                conn.execute(BODY_UPSERT, (
                    rec["folder"], rec["uid"], text, html,
                    json.dumps(headers, ensure_ascii=False),
                ))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def clear_attachments(self, folder: str, uid: int) -> None:
        self._exec("DELETE FROM attachments WHERE folder=? AND uid=?", (folder, uid))

    def add_attachment(self, folder: str, uid: int, att: dict) -> None:
        self._exec(
            "INSERT INTO attachments(folder,uid,idx,filename,content_type,size,path,is_inline,content_id) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(folder,uid,idx) DO UPDATE SET filename=excluded.filename, content_type=excluded.content_type, "
            "size=excluded.size, path=excluded.path, is_inline=excluded.is_inline, content_id=excluded.content_id",
            (
                folder,
                uid,
                att["index"],
                att["filename"],
                att["content_type"],
                att["size"],
                att.get("path", ""),
                1 if att.get("is_inline") else 0,
                att.get("content_id") or "",
            ),
        )

    def max_uid(self, folder: str) -> int:
        row = self.conn.execute("SELECT MAX(uid) AS m FROM messages WHERE folder=?", (folder,)).fetchone()
        return int(row["m"] or 0)

    def known_uids(self, folder: str) -> set[int]:
        rows = self.conn.execute("SELECT uid FROM messages WHERE folder=?", (folder,)).fetchall()
        return {int(r["uid"]) for r in rows}

    def uids_without_body(self, folder: str, limit: int = 1000, since_ts: float | None = None) -> list[int]:
        sql = ("SELECT m.uid FROM messages m LEFT JOIN bodies b ON b.folder=m.folder AND b.uid=m.uid "
               "WHERE m.folder=? AND (m.has_body=0 OR b.uid IS NULL)")
        params: list = [folder]
        if since_ts is not None:
            sql += " AND m.date_ts>=?"
            params.append(since_ts)
        sql += " ORDER BY m.uid DESC"
        if limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [int(r["uid"]) for r in rows]

    def delete_missing_messages(self, folder: str, server_uids: set[int]) -> int:
        """Remove messages expunged from a fully searched folder."""
        missing = self.known_uids(folder) - server_uids
        if not missing:
            return 0
        with self._lock:
            conn = self.conn
            try:
                for uid in missing:
                    for table in ("messages", "bodies", "attachments"):
                        conn.execute(f"DELETE FROM {table} WHERE folder=? AND uid=?", (folder, uid))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return len(missing)

    # ---------- 查询条件 ----------
    def _msg_where(
        self,
        folders=None,
        since=None,
        until=None,
        q=None,
        unread_only=False,
        has_attachment=None,
        boring=None,
        category=None,
    ) -> tuple[str, list]:
        where, params = [], []
        if folders:
            where.append("m.folder IN (%s)" % ",".join("?" * len(folders)))
            params.extend(folders)
        if since:
            where.append("m.date_ts >= ?")
            params.append(self._ts(since))
        if until:
            where.append("m.date_ts < ?")
            params.append(self._ts(until))
        if unread_only:
            where.append(r"m.flags NOT LIKE '%\Seen%'")
        if has_attachment is True:
            where.append("m.has_attach = 1")
        elif has_attachment is False:
            where.append("m.has_attach = 0")
        if category:
            where.append("m.category = ?")
            params.append(category)
        if boring is True:
            where.append("m.is_boring = 1")
        elif boring is False:
            where.append("m.is_boring = 0")
        if q:
            like = f"%{q}%"
            where.append(
                "(m.subject LIKE ? OR m.from_name LIKE ? OR m.from_addr LIKE ? OR m.to_json LIKE ? "
                "OR m.snippet LIKE ? OR COALESCE(b.body_text,'') LIKE ?)"
            )
            params.extend([like] * 6)
        return ("WHERE " + " AND ".join(where)) if where else "", params

    # ---------- 读取 ----------
    def list_messages(
        self,
        folders: list[str] | None = None,
        since: str | None = None,
        until: str | None = None,
        q: str | None = None,
        unread_only: bool = False,
        has_attachment: bool | None = None,
        category: str | None = None,
        boring: bool | None = None,
        limit: int = 50,
        offset: int = 0,
        order: str = "desc",
    ) -> tuple[int, list[dict]]:
        wsql, params = self._msg_where(folders, since, until, q, unread_only, has_attachment, boring, category)
        ordsql = "DESC" if order.lower() != "asc" else "ASC"

        total = self.conn.execute(
            f"SELECT COUNT(*) AS c FROM messages m LEFT JOIN bodies b ON b.folder=m.folder AND b.uid=m.uid {wsql}",
            params,
        ).fetchone()["c"]

        rows = self.conn.execute(
            f"SELECT m.* FROM messages m LEFT JOIN bodies b ON b.folder=m.folder AND b.uid=m.uid {wsql} "
            f"ORDER BY m.date_ts {ordsql} LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return total, [self._row_to_msg(r) for r in rows]

    def get_message(self, folder: str, uid: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM messages WHERE folder=? AND uid=?", (folder, uid)).fetchone()
        if not row:
            return None
        return self._row_to_msg(row)

    def get_body(self, folder: str, uid: int) -> dict:
        row = self.conn.execute("SELECT * FROM bodies WHERE folder=? AND uid=?", (folder, uid)).fetchone()
        if not row:
            return {"body_text": "", "body_html": "", "headers": {}}
        return {
            "body_text": row["body_text"] or "",
            "body_html": row["body_html"] or "",
            "headers": json.loads(row["headers_json"] or "{}"),
        }

    def get_attachments(self, folder: str, uid: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM attachments WHERE folder=? AND uid=? ORDER BY idx", (folder, uid)
        ).fetchall()
        return [
            {
                "index": r["idx"],
                "filename": r["filename"],
                "content_type": r["content_type"],
                "size": r["size"],
                "path": r["path"],
                "is_inline": bool(r["is_inline"]),
                "content_id": r["content_id"] or "",
            }
            for r in rows
        ]

    def set_flags(self, folder: str, uid: int, flags: list[str]) -> None:
        self._exec("UPDATE messages SET flags=? WHERE folder=? AND uid=?", (" ".join(flags), folder, uid))

    def delete_message(self, folder: str, uid: int) -> None:
        """邮件已从服务器删除/移走，把本地索引三张表的相关行一并清掉。"""
        with self._lock:
            for t in ("messages", "bodies", "attachments"):
                self.conn.execute(f"DELETE FROM {t} WHERE folder=? AND uid=?", (folder, uid))
            self.conn.commit()

    def stats(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) AS total, MIN(date_iso) AS oldest, MAX(date_iso) AS newest FROM messages WHERE date_ts>0"
        ).fetchone()
        return {"total": row["total"] or 0, "oldest": row["oldest"], "newest": row["newest"]}

    def date_range(self, folder: str | None = None) -> dict:
        sql = "SELECT MIN(date_iso) AS oldest, MAX(date_iso) AS newest, COUNT(*) AS n FROM messages WHERE date_ts>0"
        params: list = []
        if folder:
            sql += " AND folder=?"
            params.append(folder)
        row = self.conn.execute(sql, params).fetchone()
        return {"count": row["n"] or 0, "oldest": row["oldest"], "newest": row["newest"]}

    # ---------- 分类 ----------
    def classify_all(self, force: bool = False) -> dict:
        """给邮件打上 category / is_boring（默认只处理还没分类的）。"""
        from . import classify as classify_mod

        sql = "SELECT folder,uid,subject,from_name,from_addr,snippet FROM messages"
        if not force:
            sql += " WHERE category='' OR category IS NULL"
        rows = self.conn.execute(sql).fetchall()
        counts: dict[str, int] = {}
        for r in rows:
            brow = self.conn.execute(
                "SELECT headers_json FROM bodies WHERE folder=? AND uid=?", (r["folder"], r["uid"])
            ).fetchone()
            headers = json.loads(brow["headers_json"] or "{}") if brow else {}
            rec = {
                "subject": r["subject"],
                "from_name": r["from_name"],
                "from_addr": r["from_addr"],
                "snippet": r["snippet"],
                "headers": headers,
                "attachments": self.get_attachments(r["folder"], r["uid"]),
            }
            cat, boring, _score, _reasons = classify_mod.classify(rec)
            counts[cat] = counts.get(cat, 0) + 1
            self._exec(
                "UPDATE messages SET category=?, is_boring=? WHERE folder=? AND uid=?",
                (cat, 1 if boring else 0, r["folder"], r["uid"]),
            )
        return {"scanned": len(rows), "counts": counts}

    def category_stats(self) -> dict:
        rows = self.conn.execute(
            "SELECT COALESCE(NULLIF(category,''),'personal') AS c, COUNT(*) AS n FROM messages GROUP BY c"
        ).fetchall()
        by = {r["c"]: r["n"] for r in rows}
        total = sum(by.values())
        boring = sum(v for k, v in by.items() if k != "personal")
        return {
            "total": total,
            "boring": boring,
            "personal": by.get("personal", 0),
            "boring_ratio": round(boring / total, 3) if total else 0.0,
            "by_category": by,
        }

    # ---------- 联系人 ----------
    HUMAN_CATEGORIES = ("", "personal", "meeting")   # 不算「无聊邮件」的类别

    def contact_index(self, exclude: set[str] | None = None, limit: int = 5000) -> list[dict]:
        """从已索引邮件里汇总联系人（发件人 + 收件人 + 抄送），按往来频次排序。

        纯本地聚合：不联网、不读通讯录，就是"我以前跟谁通过信"。
        写信时的收件人 / 抄送联想直接吃这份结果。

        count     = 总出现次数（发件算一次，收件 / 抄送各算一次）
        addressed = 被「我」写进收件人 / 抄送的次数 —— 首排序键，只给你真正写过信的人
                    （订阅推送再频繁也挤不到前面）
        weight    = 去掉系统自动 / 营销后的加权次数，次排序键
        """
        skip = {a.strip().lower() for a in (exclude or set()) if a}
        agg: dict[str, dict] = {}

        def bump(name: str, addr: str, ts: float, w: int, addressed: int = 0) -> None:
            e = (addr or "").strip().lower()
            if not e or "@" not in e or e in skip or len(e) > 200:
                return
            if w and BULK_LOCAL_RE.match(e.split("@", 1)[0]):
                w = 0       # no-reply 之类永远不排前面
            nm = (name or "").strip().strip("'\"")      # 有些客户端把显示名连引号写进头里
            rec = agg.get(e)
            if rec is None:
                agg[e] = {"email": e, "name": nm, "count": 1,
                          "weight": w, "addressed": addressed, "last_ts": ts}
                return
            rec["count"] += 1
            rec["weight"] += w
            rec["addressed"] += addressed
            if ts > rec["last_ts"]:
                rec["last_ts"] = ts
            if not rec["name"] and nm:
                rec["name"] = nm

        rows = self.conn.execute(
            "SELECT from_name, from_addr, to_json, cc_json, date_ts, category FROM messages"
        ).fetchall()
        for r in rows:
            ts = float(r["date_ts"] or 0)
            w = 1 if (r["category"] or "") in self.HUMAN_CATEGORIES else 0
            # 发件人是自己 -> 这封里的收件人/抄送就是「我主动写过的人」
            outbound = 1 if (r["from_addr"] or "").strip().lower() in skip else 0
            bump(r["from_name"], r["from_addr"], ts, w)
            for field in ("to_json", "cc_json"):
                try:
                    people = json.loads(r[field] or "[]")
                except Exception:
                    continue
                if not isinstance(people, list):
                    continue
                for p in people:
                    if isinstance(p, dict):
                        bump(p.get("name") or "", p.get("email") or "", ts, w, outbound)
                    elif isinstance(p, str):
                        bump("", p, ts, w, outbound)

        out = sorted(
            agg.values(),
            key=lambda c: (-c["addressed"], -c["weight"], -c["count"], -c["last_ts"], c["email"]),
        )
        return out[:limit]

    def thread_stats(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT thread_id) AS t, COUNT(*) AS m FROM messages WHERE thread_id<>''"
        ).fetchone()
        grouped = self.conn.execute(
            "SELECT COUNT(*) AS c FROM (SELECT thread_id FROM messages WHERE thread_id<>'' "
            "GROUP BY thread_id HAVING COUNT(*)>1)"
        ).fetchone()["c"]
        return {"threads": row["t"] or 0, "messages": row["m"] or 0, "grouped": grouped or 0}

    # ---------- 会话 ----------
    def _thread_summary(self, tid: str, rows: list, own: set[str]) -> dict:
        from . import threads as threads_mod

        msgs = [self._row_to_msg(r) for r in rows]
        msgs.sort(key=lambda m: (m["date_ts"] or 0, m["uid"]))
        last = msgs[-1]
        first = msgs[0]

        seen: set[str] = set()
        participants: list[dict] = []
        for m in msgs:
            people = [m["from"]] + (m["to"] or [])
            for p in people:
                e = (p.get("email") or "").strip().lower()
                if not e or e in own or e in seen:
                    continue
                seen.add(e)
                participants.append({"name": p.get("name") or e.split("@")[0], "email": e})

        cats = [m["category"] for m in msgs if m["is_boring"]]
        category = max(set(cats), key=cats.count) if cats else "personal"

        return {
            "thread_id": tid,
            "subject": threads_mod.subject_title(last["subject"]),
            "message_count": len(msgs),
            "participants": participants[:6],
            "participant_count": len(participants),
            "unread": sum(1 for m in msgs if m["unread"]),
            "first_date": first["date"],
            "last_date": last["date"],
            "last_ts": last["date_ts"],
            "folders": sorted({m["folder"] for m in msgs}),
            "has_attachment": any(m["has_attachment"] for m in msgs),
            "attachment_count": sum(m["attachment_count"] or 0 for m in msgs),
            "category": category,
            "is_boring": bool(cats),
            "snippet": last["snippet"],
            "last_from": last["from"],
            "last_uid": last["uid"],
            "last_folder": last["folder"],
            "messages": msgs,
        }

    def list_threads(
        self,
        folders: list[str] | None = None,
        since: str | None = None,
        until: str | None = None,
        q: str | None = None,
        unread_only: bool = False,
        boring: bool | None = None,
        category: str | None = None,
        own_emails: set[str] | None = None,
        limit: int = 50,
        offset: int = 0,
        only_grouped: bool = False,
    ) -> tuple[int, list[dict]]:
        own = {a.strip().lower() for a in (own_emails or set()) if a}
        wsql, params = self._msg_where(folders, since, until, q, unread_only, None, boring, category)
        wsql = (wsql + " AND" if wsql else "WHERE") + " m.thread_id<>''"
        body_join = "LEFT JOIN bodies b ON b.folder=m.folder AND b.uid=m.uid " if q else ""
        grouped_filter = "HAVING COUNT(*) > 1" if only_grouped else ""
        cte = (
            "WITH matched AS (SELECT DISTINCT m.thread_id FROM messages m " + body_join + wsql + "), "
            "eligible AS (SELECT m.thread_id, MAX(COALESCE(m.date_ts,0)) AS last_ts "
            "FROM messages m JOIN matched hit ON hit.thread_id=m.thread_id "
            "GROUP BY m.thread_id " + grouped_filter + ") "
        )
        # A window count returns the exact total before LIMIT/OFFSET. If the
        # requested page is beyond the end, run a count-only query instead.
        page = self.conn.execute(
            cte + "SELECT thread_id, COUNT(*) OVER() AS total FROM eligible "
            "ORDER BY last_ts DESC, thread_id ASC LIMIT ? OFFSET ?",
            [*params, max(0, limit), max(0, offset)],
        ).fetchall()
        if not page:
            total = self.conn.execute(cte + "SELECT COUNT(*) AS n FROM eligible", params).fetchone()["n"]
            return total, []
        total = page[0]["total"]
        tids = [r["thread_id"] for r in page]
        groups: dict[str, list] = {tid: [] for tid in tids}
        # SQLite builds have different placeholder limits; bound each detail
        # query while keeping work proportional to the selected page.
        for start in range(0, len(tids), 500):
            batch = tids[start:start + 500]
            marks = ",".join("?" for _ in batch)
            rows = self.conn.execute(
                f"SELECT * FROM messages WHERE thread_id IN ({marks}) "
                "ORDER BY thread_id, date_ts ASC, uid ASC", batch,
            ).fetchall()
            for row in rows:
                groups[row["thread_id"]].append(row)
        return total, [self._thread_summary(tid, groups[tid], own) for tid in tids]

    def get_thread(self, thread_id: str, own_emails: set[str] | None = None) -> dict | None:
        own = {a.strip().lower() for a in (own_emails or set()) if a}
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE thread_id=? ORDER BY date_ts ASC, uid ASC", (thread_id,)
        ).fetchall()
        if not rows:
            return None
        return self._thread_summary(thread_id, rows, own)

    # ---------- 工具 ----------
    @staticmethod
    def _ts(date_str: str) -> float:
        """接受 '2026-07-01' / ISO datetime / 时间戳字符串。"""
        import datetime as _dt

        s = date_str.strip()
        try:
            return float(s)
        except ValueError:
            pass
        try:
            if "T" in s:
                return _dt.datetime.fromisoformat(s).timestamp()
            return _dt.datetime.strptime(s, "%Y-%m-%d").timestamp()
        except ValueError:
            return 0.0

    @staticmethod
    def _row_to_msg(row: sqlite3.Row) -> dict:
        flags = (row["flags"] or "").split()
        return {
            "folder": row["folder"],
            "uid": row["uid"],
            "id": f"{row['folder']}:{row['uid']}",
            "message_id": row["message_id"],
            "subject": row["subject"],
            "from": {"name": row["from_name"], "email": row["from_addr"]},
            "to": json.loads(row["to_json"] or "[]"),
            "cc": json.loads(row["cc_json"] or "[]"),
            "date": row["date_iso"],
            "date_ts": row["date_ts"],
            "flags": flags,
            "unread": "\\Seen" not in flags,
            "size": row["size"],
            "has_attachment": bool(row["has_attach"]),
            "attachment_count": row["attach_count"],
            "snippet": row["snippet"],
            "has_body": bool(row["has_body"]),
            "category": row["category"] or "",
            "is_boring": bool(row["is_boring"]),
            "thread_id": row["thread_id"] or "",
        }
