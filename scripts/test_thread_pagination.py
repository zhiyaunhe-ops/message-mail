"""Offline regression tests for database-level thread paging."""
from __future__ import annotations

import tempfile
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import api_mail, services
from app.context import ctx
from app.store import Store


def add_message(store: Store, tid: str, folder: str, uid: int, ts: int,
                subject: str, *, flags: str = "\\Seen", category: str = "personal",
                body: str = "") -> None:
    store.upsert_message({
        "folder": folder, "uid": uid, "message_id": f"<{uid}@example.com>",
        "subject": subject, "from_name": "Sender", "from_addr": "sender@example.com",
        "to_json": '[{"name":"Me","email":"me@example.com"}]', "cc_json": "[]",
        "date_iso": f"2026-01-{uid:02d}", "date_ts": ts,
        "flags": flags, "size": 10, "has_attach": 0, "attach_count": 0,
        "snippet": f"Snippet {uid}", "in_reply_to": "", "has_body": bool(body),
    })
    store.conn.execute(
        "UPDATE messages SET thread_id=?, category=?, is_boring=? WHERE folder=? AND uid=?",
        (tid, category, int(category != "personal"), folder, uid),
    )
    store.conn.commit()
    if body:
        store.upsert_body(folder, uid, body, "", {})


class ThreadPaginationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "threads.sqlite3")
        add_message(self.store, "tA", "INBOX", 1, 1000, "Alpha needle", flags="")
        add_message(self.store, "tA", "Sent", 2, 3000, "Re: Alpha", category="meeting")
        add_message(self.store, "tB", "INBOX", 3, 4000, "Single", body="body match")
        add_message(self.store, "tC", "INBOX", 4, 2000, "Charlie")
        add_message(self.store, "tC", "Sent", 5, 5000, "Re: Charlie")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_grouped_total_and_offset_are_before_page(self):
        total, page = self.store.list_threads(limit=2)
        self.assertEqual(total, 3)
        self.assertEqual([t["thread_id"] for t in page], ["tC", "tB"])
        total, page = self.store.list_threads(only_grouped=True, limit=1, offset=1)
        self.assertEqual(total, 2)
        self.assertEqual([t["thread_id"] for t in page], ["tA"])
        self.assertEqual(page[0]["message_count"], 2)
        total, page = self.store.list_threads(only_grouped=True, limit=1, offset=10)
        self.assertEqual((total, page), (2, []))

    def test_filter_any_message_but_summarize_full_thread(self):
        cases = [
            {"q": "needle"},
            {"folders": ["Sent"], "category": "meeting"},
            {"unread_only": True},
        ]
        for filters in cases:
            with self.subTest(filters=filters):
                total, page = self.store.list_threads(**filters)
                self.assertEqual(total, 1)
                self.assertEqual(page[0]["thread_id"], "tA")
                self.assertEqual(page[0]["message_count"], 2)
                self.assertEqual(set(page[0]["folders"]), {"INBOX", "Sent"})
        total, page = self.store.list_threads(q="body match")
        self.assertEqual((total, page[0]["thread_id"]), (1, "tB"))
        total, page = self.store.list_threads(since="2500", until="3500")
        self.assertEqual((total, page[0]["thread_id"], page[0]["message_count"]),
                         (1, "tA", 2))

    def test_route_and_ai_service_return_full_total_for_later_page(self):
        with patch.object(api_mail, "store", return_value=self.store), \
             patch.object(api_mail, "own_emails", return_value={"me@example.com"}), \
             patch.object(api_mail, "is_mine", return_value=False):
            response = api_mail.list_threads(
                folder=None, since=None, until=None, q=None, unread_only=False,
                category=None, boring=None, only_grouped=True, limit=1,
                offset=1, preview=0,
            )
        self.assertEqual(response["total"], 2)
        self.assertEqual(response["count"], 1)
        self.assertEqual(response["items"][0]["thread_id"], "tA")
        self.assertEqual(response["items"][0]["preview"], [])

        with patch.object(ctx, "store", return_value=self.store), \
             patch.object(ctx, "account", return_value=SimpleNamespace(email="me@example.com")), \
             patch.object(ctx, "is_mine", return_value=False):
            response = services.build_ai_threads(
                only_grouped=True, limit=1, offset=1, max_messages=1,
            )
        self.assertEqual(response["total"], 2)
        self.assertEqual(response["count"], 1)
        self.assertEqual(response["threads"][0]["thread_id"], "tA")
        self.assertEqual(len(response["threads"][0]["messages"]), 1)


if __name__ == "__main__":
    unittest.main()
