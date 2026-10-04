"""The conversation agent's schema changes are additive and idempotent: an
existing database (the owner's live one) keeps every row and every CHECK
constraint, and gains only new columns / tables."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import db

# wa_messages and notifications exactly as they were before the agent existed.
OLD_SCHEMA = """
CREATE TABLE wa_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_message_id TEXT NOT NULL UNIQUE,
    wa_id TEXT NOT NULL,
    patient_id INTEGER,
    received_at TEXT NOT NULL DEFAULT (datetime('now')),
    message_type TEXT NOT NULL CHECK (message_type IN ('text', 'audio')),
    raw_text TEXT,
    media_id TEXT,
    intent TEXT,
    slots_json TEXT,
    status TEXT NOT NULL DEFAULT 'received'
        CHECK (status IN ('received', 'classified', 'needs_human_reply', 'approved', 'rejected', 'dismissed', 'error')),
    proposal_id INTEGER,
    error_text TEXT,
    resolved_at TEXT
);
CREATE TABLE notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER,
    wa_id TEXT,
    event TEXT NOT NULL,
    dedup_key TEXT NOT NULL UNIQUE,
    language TEXT,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sent', 'dry_run', 'blocked_no_window', 'failed', 'skipped_no_phone')),
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at TEXT
);
"""


def columns(conn, table):
    return {row[1] for row in conn.execute("PRAGMA table_info({})".format(table))}


class AdditiveSchemaTests(unittest.TestCase):
    def make_old(self, path):
        old = sqlite3.connect(path)
        old.executescript(OLD_SCHEMA)
        old.execute("INSERT INTO wa_messages (wa_message_id, wa_id, message_type, raw_text, status) "
                    "VALUES ('w1', '919876543210', 'text', 'hello', 'classified')")
        old.execute("INSERT INTO notifications (wa_id, event, dedup_key, body, status) "
                    "VALUES ('919876543210', 'your_turn', 'k1', 'hi', 'sent')")
        old.commit()
        old.close()

    def test_connect_upgrades_an_existing_database_without_losing_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            self.make_old(path)
            conn = db.connect(path)
            conn2 = db.connect(path)    # idempotent
            self.assertIn("agent_handled", columns(conn, "wa_messages"))
            self.assertIn("interactive_json", columns(conn, "notifications"))
            row = conn.execute("SELECT * FROM wa_messages").fetchone()
            self.assertEqual((row["raw_text"], row["status"], row["agent_handled"]), ("hello", "classified", 0))
            note = conn.execute("SELECT * FROM notifications").fetchone()
            self.assertEqual((note["body"], note["status"], note["interactive_json"]), ("hi", "sent", None))
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"wa_sessions", "slot_holds"} <= tables)
            conn.close()
            conn2.close()

    def test_existing_check_constraints_are_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            self.make_old(path)
            conn = db.connect(path)
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'wa_messages'").fetchone()[0]
            self.assertIn("'received', 'classified', 'needs_human_reply', 'approved', 'rejected', 'dismissed', 'error'", sql)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO wa_messages (wa_message_id, wa_id, message_type, status) "
                             "VALUES ('w2', '1', 'text', 'agent_handled')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO wa_messages (wa_message_id, wa_id, message_type) VALUES ('w3', '1', 'interactive')")
            conn.close()

    def test_a_partial_old_schema_is_not_an_error(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE appointments (id INTEGER PRIMARY KEY, appt_date TEXT, start_time TEXT)")
        db.ensure_columns(conn)     # no wa_messages / notifications tables here
        self.assertIn("queue_state", columns(conn, "appointments"))

    def test_fresh_database_has_everything(self):
        conn = db.connect(":memory:")
        self.assertIn("agent_handled", columns(conn, "wa_messages"))
        self.assertIn("interactive_json", columns(conn, "notifications"))
        self.assertEqual(columns(conn, "wa_sessions"), {
            "wa_id", "goal", "step", "slots_json", "language", "mode", "confusion_count", "turn_count",
            "rate_window_start", "updated_at", "expires_at"})
        self.assertTrue({"wa_id", "appt_date", "start_time", "expires_at"} <= columns(conn, "slot_holds"))

    def test_session_table_constraints(self):
        conn = db.connect(":memory:")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO wa_sessions (wa_id, mode) VALUES ('1', 'robot')")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO wa_sessions (wa_id, goal) VALUES ('2', 'register')")
        conn.execute("INSERT INTO wa_sessions (wa_id) VALUES ('3')")
        self.assertEqual(conn.execute("SELECT mode, goal FROM wa_sessions").fetchone()[:], ("agent", None))


if __name__ == "__main__":
    unittest.main()
