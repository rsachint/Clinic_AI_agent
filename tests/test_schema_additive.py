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

    def test_planner_log_gains_the_sarvam_usage_columns_in_place(self):
        # planner_log exactly as it was before the hosted planner recorded its backend, tokens and cost.
        old_log = """
        CREATE TABLE planner_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (datetime('now')),
            source TEXT NOT NULL DEFAULT 'voice' CHECK (source IN ('voice', 'wa_staff')),
            transcript TEXT NOT NULL,
            previous_turn TEXT,
            planner_tool TEXT,
            planner_args_json TEXT,
            route_taken TEXT NOT NULL CHECK (route_taken IN ('rules', 'planner', 'label_fallback', 'rephrase')),
            final_intent TEXT,
            latency_ms INTEGER,
            override_notes TEXT,
            outcome TEXT CHECK (outcome IS NULL OR outcome IN ('approved', 'rejected', 'edited'))
        );"""
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            old = sqlite3.connect(path)
            old.executescript(old_log)
            old.execute("INSERT INTO planner_log (transcript, route_taken, final_intent, latency_ms) VALUES ('book Amit', 'planner', 'book_appointment', 900)")
            old.commit()
            old.close()
            conn = db.connect(path)
            conn2 = db.connect(path)    # idempotent
            self.assertTrue({"backend", "tokens_in", "tokens_out", "cost_paise", "route_detail"} <= columns(conn, "planner_log"))
            row = conn.execute("SELECT * FROM planner_log").fetchone()
            self.assertEqual((row["transcript"], row["route_taken"], row["latency_ms"]), ("book Amit", "planner", 900))
            self.assertEqual((row["backend"], row["tokens_in"], row["tokens_out"], row["cost_paise"]), (None, None, None, None))
            self.assertIsNone(row["route_detail"])
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'planner_log'").fetchone()[0]
            self.assertIn("'rules', 'planner', 'label_fallback', 'rephrase'", sql)        # the route CHECK is untouched
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO planner_log (transcript, route_taken) VALUES ('x', 'sarvam')")
            conn.execute("INSERT INTO planner_log (transcript, route_taken, backend, tokens_in, tokens_out, cost_paise) "
                         "VALUES ('y', 'rules', 'sarvam', 3700, 40, 11)")
            conn.execute("INSERT INTO planner_log (transcript, route_taken, route_detail) VALUES ('z', 'rules', 'rule:move')")
            conn.close()
            conn2.close()

    def test_planner_log_gains_the_state_card_column_in_place(self):
        # planner_log exactly as it was before model-first mode (everything up to route_detail), with a row in it.
        before = """
        CREATE TABLE planner_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (datetime('now')),
            source TEXT NOT NULL DEFAULT 'voice' CHECK (source IN ('voice', 'wa_staff')),
            transcript TEXT NOT NULL,
            previous_turn TEXT,
            planner_tool TEXT,
            planner_args_json TEXT,
            route_taken TEXT NOT NULL CHECK (route_taken IN ('rules', 'planner', 'label_fallback', 'rephrase')),
            final_intent TEXT,
            latency_ms INTEGER,
            override_notes TEXT,
            outcome TEXT CHECK (outcome IS NULL OR outcome IN ('approved', 'rejected', 'edited')),
            backend TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_paise INTEGER, route_detail TEXT
        );"""
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            old = sqlite3.connect(path)
            old.executescript(before)
            old.execute("INSERT INTO planner_log (transcript, route_taken, route_detail) VALUES ('move Manju', 'rules', 'rule:move')")
            old.commit()
            old.close()
            conn = db.connect(path)
            conn2 = db.connect(path)    # idempotent
            self.assertIn("state_card", columns(conn, "planner_log"))
            row = conn.execute("SELECT * FROM planner_log").fetchone()
            self.assertEqual((row["transcript"], row["route_detail"], row["state_card"]), ("move Manju", "rule:move", None))
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'planner_log'").fetchone()[0]
            self.assertIn("'rules', 'planner', 'label_fallback', 'rephrase'", sql)        # the route CHECK is untouched
            from clinic import planner_log
            planner_log.record(conn, "voice", "book Amit", None, "book_appointment", {}, "planner", "book_appointment", 10, [],
                               route_detail="mf:book_appointment", state_card="STATE CARD")
            planner_log.record(conn, "voice", "plain", None, None, None, "rules", "x", 1, [])      # classic rows leave it NULL
            self.assertEqual([r[0] for r in conn.execute("SELECT state_card FROM planner_log ORDER BY id")], [None, "STATE CARD", None])
            conn.close()
            conn2.close()

    def test_a_fresh_database_has_the_state_card_column_and_nothing_else_new(self):
        conn = db.connect(":memory:")
        self.assertTrue({"route_detail", "state_card"} <= columns(conn, "planner_log"))
        self.assertEqual(conn.execute("SELECT value FROM app_settings WHERE key = 'intent_architecture'").fetchone(), None)   # classic by default

    def test_the_read_views_are_added_in_place_idempotently_and_change_no_table(self):
        from clinic import read_schema
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            self.make_old(path)                      # a database from before the read views existed
            conn = db.connect(path)
            conn2 = db.connect(path)                 # idempotent: CREATE VIEW IF NOT EXISTS
            views = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")}
            self.assertEqual(views, set(read_schema.VIEW_NAMES))
            first = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'view'")}
            conn3 = db.connect(path)
            self.assertEqual({r[0]: r[1] for r in conn3.execute("SELECT name, sql FROM sqlite_master WHERE type = 'view'")}, first)
            row = conn.execute("SELECT * FROM wa_messages").fetchone()
            self.assertEqual((row["raw_text"], row["status"]), ("hello", "classified"))     # existing rows untouched
            with self.assertRaises(sqlite3.OperationalError):                               # a view is not a table: nothing can be written to it
                conn.execute("INSERT INTO v_patients (name) VALUES ('x')")
            conn.close()
            conn2.close()
            conn3.close()

    def test_views_that_call_a_function_only_the_read_connection_has_do_not_block_other_work_on_the_database(self):
        conn = db.connect(":memory:")
        # v_branches and v_roster_days call today_ist(), which this ordinary connection does not have ...
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("SELECT * FROM v_branches").fetchall()
        # ... and that must not stop anything else: more columns, other queries, a second connect (schema re-run)
        conn.execute("ALTER TABLE patients ADD COLUMN extra_test_column TEXT")
        db.ensure_columns(conn)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM v_patients").fetchone()[0], 0)
        conn.execute("PRAGMA integrity_check")
        conn.close()

    def test_the_views_exist_on_a_fresh_database_and_the_other_tables_are_the_same_as_before(self):
        conn = db.connect(":memory:")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertFalse([t for t in tables if t.startswith("v_")])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'view'").fetchone()[0], 17)

    def test_session_table_constraints(self):
        conn = db.connect(":memory:")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO wa_sessions (wa_id, mode) VALUES ('1', 'robot')")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO wa_sessions (wa_id, goal) VALUES ('2', 'register')")
        conn.execute("INSERT INTO wa_sessions (wa_id) VALUES ('3')")
        self.assertEqual(conn.execute("SELECT mode, goal FROM wa_sessions").fetchone()[:], ("agent", None))

    def test_network_events_is_a_new_table_added_in_place_and_nothing_else_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            self.make_old(path)                      # a database from before the connection log existed
            conn = db.connect(path)
            conn2 = db.connect(path)                 # idempotent
            self.assertEqual(columns(conn, "network_events"),
                             {"id", "ts", "service", "kind", "detail", "duration_ms"})
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM network_events").fetchone()[0], 0)
            row = conn.execute("SELECT * FROM wa_messages").fetchone()
            self.assertEqual((row["raw_text"], row["status"]), ("hello", "classified"))    # existing rows untouched
            conn.execute("INSERT INTO network_events (service, kind, detail, duration_ms) "
                         "VALUES ('voice', 'connect', 'Could not connect', 10001)")
            with self.assertRaises(sqlite3.IntegrityError):                                # fixed service names only
                conn.execute("INSERT INTO network_events (service) VALUES ('telephone')")
            conn.close()
            conn2.close()

    def test_the_help_tables_are_new_tables_added_in_place_with_seeded_categories_and_an_append_only_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            self.make_old(path)                      # a database from before "Need help" existed
            conn = db.connect(path)
            conn2 = db.connect(path)                 # idempotent: the seed is not repeated or overwritten
            self.assertEqual(columns(conn, "help_categories"), {"key", "label", "sort_order", "active"})
            self.assertEqual(columns(conn, "help_requests"), {
                "id", "ticket_no", "created_at", "username", "category_key", "severity", "description", "source", "status",
                "sla_hours", "sla_due_at", "resolved_at", "context_json", "exported_at", "notified_at"})
            self.assertEqual(columns(conn, "help_attachments"), {
                "id", "request_id", "original_name", "stored_name", "mime", "bytes", "sha256", "created_at"})
            self.assertEqual(columns(conn, "help_request_events"), {
                "id", "request_id", "at", "actor", "kind", "from_status", "to_status", "note"})
            self.assertEqual([tuple(r) for r in conn.execute("SELECT key, label, active FROM help_categories ORDER BY sort_order")],
                             [("voice_assistant", "Voice assistant", 1), ("patients_data", "Patients data", 1),
                              ("language_understanding", "Language understanding", 1), ("other", "Other", 1)])
            conn.execute("UPDATE help_categories SET label = 'Renamed', active = 0 WHERE key = 'other'")
            conn.commit()
            conn3 = db.connect(path)
            self.assertEqual(conn3.execute("SELECT label, active FROM help_categories WHERE key = 'other'").fetchone()[:], ("Renamed", 0))
            row = conn.execute("SELECT * FROM wa_messages").fetchone()
            self.assertEqual((row["raw_text"], row["status"]), ("hello", "classified"))    # existing rows untouched
            conn.execute("INSERT INTO help_requests (ticket_no, created_at, username, category_key, description, sla_hours, sla_due_at) "
                         "VALUES ('HELP-0001', '2026-10-05 10:00:00', 'u', 'other', 'd', 24, '2026-10-06 10:00:00')")
            with self.assertRaises(sqlite3.IntegrityError):                                # ticket numbers are unique
                conn.execute("INSERT INTO help_requests (ticket_no, created_at, username, category_key, description, sla_hours, sla_due_at) "
                             "VALUES ('HELP-0001', '2026-10-05 10:00:00', 'u', 'other', 'd', 24, '2026-10-06 10:00:00')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO help_requests (ticket_no, created_at, username, category_key, description, status, sla_hours, sla_due_at) "
                             "VALUES ('HELP-0002', 'x', 'u', 'other', 'd', 'done', 24, 'y')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO help_requests (ticket_no, created_at, username, category_key, description, severity, sla_hours, sla_due_at) "
                             "VALUES ('HELP-0003', 'x', 'u', 'other', 'd', 'huge', 24, 'y')")
            conn.execute("INSERT INTO help_request_events (request_id, at, actor, kind) VALUES (1, 'x', 'user', 'created')")
            with self.assertRaises(sqlite3.IntegrityError):                                # fixed actors only
                conn.execute("INSERT INTO help_request_events (request_id, at, actor, kind) VALUES (1, 'x', 'robot', 'created')")
            with self.assertRaises(sqlite3.DatabaseError):                                 # append-only
                conn.execute("UPDATE help_request_events SET note = 'x'")
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM help_request_events")
            conn.close()
            conn2.close()
            conn3.close()


if __name__ == "__main__":
    unittest.main()
