"""Schema upgrade for automatic appointments: the previous release's database
(everything in schema.sql before app_settings / booking_blocks / patient_activity
existed) is upgraded in place, additively and idempotently, without losing a
row or touching an existing table, and the immutable audit_log stays immutable."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import db, patient_activity, scheduling, settings

SCHEMA_TEXT = (Path(__file__).resolve().parents[1] / "clinic" / "schema.sql").read_text()
START = "-- Automatic WhatsApp appointment actions"
END = "-- audit_log is append-only"
NEW_TABLES = {"app_settings", "booking_blocks", "patient_activity"}
PREVIOUS_SCHEMA = SCHEMA_TEXT[:SCHEMA_TEXT.index(START)] + SCHEMA_TEXT[SCHEMA_TEXT.index(END):]


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def table_sql(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table'")}


class SchemaUpgradeTests(unittest.TestCase):
    def make_previous(self, path):
        old = sqlite3.connect(path)
        old.executescript(PREVIOUS_SCHEMA)
        old.execute("INSERT INTO patients (name, phone) VALUES ('Sunita Devi', '9876543210')")
        old.execute("INSERT INTO appointments (patient_id, appt_date, start_time, status) VALUES (1, '2026-10-06', '09:00', 'booked')")
        old.execute("INSERT INTO proposals (intent, slots_json, source_text, status) VALUES ('book_appointment', '{}', 'voice', 'confirmed')")
        old.execute("INSERT INTO audit_log (proposal_id, intent, entity_type, entity_id, payload_json) "
                    "VALUES (1, 'book_appointment', 'appointment', 1, '{}')")
        old.execute("INSERT INTO wa_messages (wa_message_id, wa_id, message_type, raw_text, status) "
                    "VALUES ('w1', '919876543210', 'text', 'hello', 'classified')")
        old.execute("INSERT INTO wa_sessions (wa_id, mode) VALUES ('919876543210', 'human')")
        old.commit()
        old.close()

    def test_the_previous_schema_really_lacks_the_new_tables(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(PREVIOUS_SCHEMA)
        self.assertFalse(NEW_TABLES & tables(conn))

    def test_upgrade_adds_only_the_new_tables_and_loses_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "previous.db")
            self.make_previous(path)
            before = sqlite3.connect(path)
            sql_before = table_sql(before)
            counts_before = {t: before.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0] for t in sql_before}
            before.close()

            conn = db.connect(path)
            conn_again = db.connect(path)                         # idempotent: a second connect changes nothing
            sql_after = table_sql(conn)
            self.assertEqual(set(sql_after) - set(sql_before), NEW_TABLES)
            for name, sql in sql_before.items():                  # every pre-existing table is byte-for-byte what it was
                self.assertEqual(sql_after[name], sql, name)
            for name, n in counts_before.items():
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM {}".format(name)).fetchone()[0], n, name)
            self.assertEqual(conn.execute("SELECT status FROM appointments").fetchone()[0], "booked")
            self.assertEqual(conn.execute("SELECT mode FROM wa_sessions").fetchone()[0], "human")
            conn.close()
            conn_again.close()

    def test_defaults_work_straight_after_the_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "previous.db")
            self.make_previous(path)
            conn = db.connect(path)
            self.assertTrue(settings.auto_enabled(conn))             # default ON
            self.assertEqual(settings.auto_daily_cap(conn), 40)
            self.assertEqual(scheduling.generate_slots(conn, "2026-10-07")[:2], ["09:00", "09:30"])
            self.assertIsNotNone(patient_activity.log(conn, event="requested", source="staff"))
            conn.close()

    def test_audit_log_is_still_immutable_after_the_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "previous.db")
            self.make_previous(path)
            conn = db.connect(path)
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("UPDATE audit_log SET intent = 'x'")
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM audit_log")
            conn.close()

    def test_new_table_shapes(self):
        conn = db.connect(":memory:")
        cols = lambda t: {r[1] for r in conn.execute("PRAGMA table_info({})".format(t))}
        self.assertEqual(cols("booking_blocks"), {"id", "start_date", "end_date", "start_time", "end_time", "reason", "active", "created_at", "branch_id", "doctor_id"})  # + branch / doctor scope (multi-branch)
        self.assertTrue({"id", "patient_id", "wa_id", "patient_name", "appointment_id", "event", "source", "detail", "created_at"} <= cols("patient_activity"))
        self.assertTrue({"key", "value"} <= cols("app_settings"))
        conn.execute("INSERT INTO booking_blocks (start_date, end_date) VALUES ('2026-10-06', '2026-10-06')")
        self.assertEqual(conn.execute("SELECT active, start_time FROM booking_blocks").fetchone()[:], (1, None))


if __name__ == "__main__":
    unittest.main()
