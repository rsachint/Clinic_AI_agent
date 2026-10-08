"""The follow-up schema changes are additive: a database from before this feature
(the owner's live one) upgrades in place, keeps every row and every CHECK
constraint, and the old date-only follow-ups keep working untouched."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import core, db, followups  # noqa: E402
from clinic.intents import HANDLERS  # noqa: E402

# followups and notifications exactly as they were before the follow-up reminders existed.
OLD_SCHEMA = """
CREATE TABLE patients (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, phone TEXT NOT NULL, age INTEGER,
                       registered_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE visits (id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id INTEGER NOT NULL, visit_date TEXT NOT NULL,
                     fee_paise INTEGER NOT NULL, notes TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE followups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    visit_id INTEGER REFERENCES visits(id),
    due_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'missed', 'cancelled')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
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
    sent_at TEXT,
    interactive_json TEXT
);
"""


def columns(conn, table):
    return {row[1] for row in conn.execute("PRAGMA table_info({})".format(table))}


class FollowupSchemaTests(unittest.TestCase):
    def make_old(self, path):
        old = sqlite3.connect(path)
        old.executescript(OLD_SCHEMA)
        old.execute("INSERT INTO patients (name, phone) VALUES ('Sunita Devi', '9876543210')")
        old.execute("INSERT INTO followups (patient_id, due_date, status) VALUES (1, '2026-10-12', 'pending')")
        old.execute("INSERT INTO followups (patient_id, due_date, status) VALUES (1, '2026-09-01', 'done')")
        old.execute("INSERT INTO notifications (wa_id, event, dedup_key, body, status, interactive_json) "
                    "VALUES ('919876543210', 'your_turn', 'k1', 'hi', 'sent', '{}')")
        old.commit()
        old.close()

    def test_an_old_database_upgrades_in_place_and_keeps_its_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            self.make_old(path)
            conn = db.connect(path)
            again = db.connect(path)         # idempotent: a second open is not an error
            self.assertTrue({"due_time", "doctor_id", "branch_id", "appointment_id", "diagnosis", "batch_id"}
                            <= columns(conn, "followups"))
            self.assertIn("template_json", columns(conn, "notifications"))
            rows = conn.execute("SELECT id, due_date, status, due_time, appointment_id, diagnosis FROM followups ORDER BY id").fetchall()
            self.assertEqual([tuple(r) for r in rows],
                             [(1, "2026-10-12", "pending", None, None, None), (2, "2026-09-01", "done", None, None, None)])
            note = conn.execute("SELECT body, status, interactive_json, template_json FROM notifications").fetchone()
            self.assertEqual(tuple(note), ("hi", "sent", "{}", None))
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"followup_reminders", "followup_batches", "reminder_opt_outs"} <= tables)
            conn.close()
            again.close()

    def test_existing_check_constraints_are_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            self.make_old(path)
            conn = db.connect(path)
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'followups'").fetchone()[0]
            self.assertIn("CHECK (status IN ('pending', 'done', 'missed', 'cancelled'))", sql)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO followups (patient_id, due_date, status) VALUES (1, '2026-10-12', 'someday')")
            note_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'notifications'").fetchone()[0]
            self.assertIn("'pending', 'sent', 'dry_run', 'blocked_no_window', 'failed', 'skipped_no_phone'", note_sql)
            conn.close()

    def test_a_fresh_database_has_the_same_columns_as_an_upgraded_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            self.make_old(path)
            upgraded = db.connect(path)
            fresh = db.connect(":memory:")
            for table in ("followups", "notifications"):
                self.assertEqual(columns(upgraded, table), columns(fresh, table), table)
            upgraded.close()

    def test_the_old_date_only_followup_keeps_working_after_the_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            self.make_old(path)
            conn = db.connect(path)
            proposal = core.propose(conn, "set_followup", {"patient_id": 1, "due_date": "2026-10-20"})
            _, fid = core.confirm(conn, proposal, HANDLERS)
            fu = followups.get_followup(conn, fid)
            self.assertEqual((fu["status"], fu["appointment_id"], fu["due_time"]), ("pending", None, None))
            self.assertFalse(followups.is_slot_followup(fu))
            # no slot, no reminders: nothing is scheduled for it, and the reminder pass ignores it
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM followup_reminders").fetchone()[0], 0)
            self.assertEqual(followups.tick(conn, None), 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications WHERE event LIKE 'followup_reminder%'").fetchone()[0], 0)
            proposal = core.propose(conn, "reschedule_followup", {"followup_id": fid, "new_due_date": "2026-10-22"})
            core.confirm(conn, proposal, HANDLERS)
            self.assertEqual(followups.get_followup(conn, fid)["due_date"], "2026-10-22")
            proposal = core.propose(conn, "cancel_followup", {"followup_id": fid})
            core.confirm(conn, proposal, HANDLERS)
            self.assertEqual(followups.get_followup(conn, fid)["status"], "cancelled")
            conn.close()

    def test_a_partial_old_schema_is_not_an_error(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE appointments (id INTEGER PRIMARY KEY, appt_date TEXT, start_time TEXT)")
        db.ensure_columns(conn)      # no followups / notifications tables here
        self.assertIn("queue_state", columns(conn, "appointments"))

    def test_reminder_table_constraints(self):
        conn = db.connect(":memory:")
        conn.execute("INSERT INTO patients (name, phone) VALUES ('A', '9876543210')")
        conn.execute("INSERT INTO followups (patient_id, due_date) VALUES (1, '2026-10-12')")
        conn.execute("INSERT INTO followup_reminders (followup_id, kind, slot_date, slot_time) VALUES (1, '2d', '2026-10-12', '10:00')")
        with self.assertRaises(sqlite3.IntegrityError):       # one row per follow-up + slot + kind
            conn.execute("INSERT INTO followup_reminders (followup_id, kind, slot_date, slot_time) VALUES (1, '2d', '2026-10-12', '10:00')")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO followup_reminders (followup_id, kind, slot_date, slot_time) VALUES (1, '1h', '2026-10-12', '10:00')")
        # the same kind for a new slot is a new row
        conn.execute("INSERT INTO followup_reminders (followup_id, kind, slot_date, slot_time) VALUES (1, '2d', '2026-10-13', '10:00')")


if __name__ == "__main__":
    unittest.main()
