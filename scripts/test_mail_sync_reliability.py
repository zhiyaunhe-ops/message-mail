"""Offline regressions for IMAP UID gap recovery and atomic body storage."""
from __future__ import annotations

import tempfile
import unittest
import sys
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.imap_client import Envelope, IMAPClient
from app.store import Store
from app.sync import sync_folder


def sample(uid: int) -> bytes:
    msg = EmailMessage()
    msg["From"] = "sender@example.com"
    msg["To"] = "recipient@example.com"
    msg["Subject"] = f"Message {uid}"
    msg["Date"] = "Mon, 21 Sep 2026 12:00:00 +0000"
    msg["Message-ID"] = f"<{uid}@example.com>"
    msg.set_content(f"Body {uid}")
    return msg.as_bytes()


class FakeClient:
    def __init__(self, uids: set[int]):
        self.uids = uids
        self.omit_envelope_once: set[int] = set()
        self.omit_envelope_always: set[int] = set()
        self.omit_body_once: set[int] = set()
        self.fail_search = False
        self.account = SimpleNamespace(email="recipient@example.com")

    def status(self, folder):
        return {"uidvalidity": 1, "uidnext": max(self.uids, default=0) + 1,
                "messages": len(self.uids)}

    def select(self, folder, readonly=True):
        return {"exists": len(self.uids), "uidvalidity": 1}

    def search_uids(self, criteria):
        if self.fail_search:
            raise RuntimeError("SEARCH NO")
        return sorted(self.uids)

    def fetch_envelopes(self, uids):
        out = []
        for uid in uids:
            if uid in self.omit_envelope_always:
                continue
            if uid in self.omit_envelope_once:
                self.omit_envelope_once.remove(uid)
                continue
            raw = sample(uid)
            out.append(Envelope(uid, [], "", len(raw), raw))
        return out

    def fetch_raw_batch(self, uids):
        out = {}
        for uid in uids:
            if uid in self.omit_body_once:
                self.omit_body_once.remove(uid)
                continue
            out[uid] = sample(uid)
        return out


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "mail.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_missing_envelope_retried_and_history_paged(self):
        client = FakeClient(set(range(1, 6)))
        client.omit_envelope_once.add(4)
        first = sync_folder(client, self.store, "INBOX", envelope_limit=2, with_body=False)
        self.assertEqual(self.store.known_uids("INBOX"), {5})
        self.assertTrue(any("envelope uid=4" in e for e in first["errors"]))
        for _ in range(3):
            sync_folder(client, self.store, "INBOX", envelope_limit=2, with_body=False)
        self.assertEqual(self.store.known_uids("INBOX"), set(range(1, 6)))

    def test_permanent_bad_uid_does_not_block_older_backfill(self):
        client = FakeClient({1, 2, 3})
        client.omit_envelope_always.add(3)
        for _ in range(4):
            sync_folder(client, self.store, "INBOX", envelope_limit=1, with_body=False)
        self.assertEqual(self.store.known_uids("INBOX"), {1, 2})

    def test_missing_body_and_legacy_marker_retried(self):
        client = FakeClient({1})
        client.omit_body_once.add(1)
        first = sync_folder(client, self.store, "INBOX", envelope_limit=2)
        self.assertTrue(any("body uid=1" in e for e in first["errors"]))
        self.assertEqual(self.store.get_message("INBOX", 1)["has_body"], 0)
        sync_folder(client, self.store, "INBOX", envelope_limit=2)
        self.assertEqual(self.store.get_body("INBOX", 1)["body_text"].strip(), "Body 1")
        self.store.conn.execute("DELETE FROM bodies WHERE folder='INBOX' AND uid=1")
        self.store.conn.commit()
        sync_folder(client, self.store, "INBOX", envelope_limit=2)
        self.assertEqual(self.store.get_body("INBOX", 1)["body_text"].strip(), "Body 1")

    def test_search_failure_does_not_delete_local_index(self):
        client = FakeClient({1})
        sync_folder(client, self.store, "INBOX", with_body=False)
        client.fail_search = True
        result = sync_folder(client, self.store, "INBOX", with_body=False)
        self.assertTrue(any("search:" in e for e in result["errors"]))
        self.assertEqual(self.store.known_uids("INBOX"), {1})

    def test_message_body_transaction_rolls_back_on_body_failure(self):
        client = FakeClient({1})
        sync_folder(client, self.store, "INBOX", with_body=False)
        rec = dict(self.store.conn.execute(
            "SELECT folder,uid,message_id,subject,from_name,from_addr,to_json,cc_json,"
            "date_iso,date_ts,flags,size,has_attach,attach_count,snippet,in_reply_to,has_body "
            "FROM messages WHERE folder='INBOX' AND uid=1"
        ).fetchone())
        rec["has_body"] = 1
        self.store.conn.execute(
            "CREATE TRIGGER reject_body BEFORE INSERT ON bodies BEGIN SELECT RAISE(FAIL, 'no body'); END"
        )
        self.store.conn.commit()
        with self.assertRaises(Exception):
            self.store.upsert_message_with_body(rec, "text", "", {})
        self.assertEqual(self.store.get_message("INBOX", 1)["has_body"], 0)
        self.assertEqual(self.store.get_body("INBOX", 1)["body_text"], "")

    def test_deleted_mail_reconciled_after_complete_search(self):
        client = FakeClient({1, 2})
        sync_folder(client, self.store, "INBOX", with_body=False)
        client.uids.remove(1)
        result = sync_folder(client, self.store, "INBOX", with_body=False)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(self.store.known_uids("INBOX"), {2})


class FakeIMAPConnection:
    def __init__(self):
        self.calls = []

    def uid(self, command, sequence, fields):
        self.calls.append(sequence)
        if sequence == "1,2":
            return "OK", [(b"1 (UID 1 FLAGS () RFC822.SIZE 5)", b"a")]
        if sequence == "2":
            return "OK", [(b"2 (UID 2 FLAGS () RFC822.SIZE 5)", b"b")]
        return "NO", []

    def status(self, folder, fields):
        return "NO", [b"temporary failure"]


class IMAPBatchTests(unittest.TestCase):
    def test_partial_fetch_retries_missing_uid_individually(self):
        conn = FakeIMAPConnection()
        client = IMAPClient(SimpleNamespace())
        client.connect = lambda: conn
        envelopes = client.fetch_envelopes([1, 2], chunk=2)
        self.assertEqual({e.uid for e in envelopes}, {1, 2})
        self.assertEqual(conn.calls, ["1,2", "2"])

    def test_failed_status_is_not_treated_as_uidvalidity_zero(self):
        client = IMAPClient(SimpleNamespace())
        client.connect = lambda: FakeIMAPConnection()
        with self.assertRaisesRegex(RuntimeError, "STATUS failed"):
            client.status("INBOX")


if __name__ == "__main__":
    unittest.main()
