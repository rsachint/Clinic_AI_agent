import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import core, db, token_queue
from clinic.adapters.registry import build_write_handlers, get_adapters
from clinic.intents import HANDLERS

SCHEMA = (Path(__file__).resolve().parents[1] / "clinic" / "schema.sql").read_text()

# The appointments table exactly as it was before queue_state /
# last_notified_token existed -- what the live clinic.db has.
OLD_APPOINTMENTS_SCHEMA = """
CREATE TABLE appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER REFERENCES patients(id),
    patient_name TEXT,
    patient_phone TEXT,
    appt_date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL DEFAULT 15,
    status TEXT NOT NULL DEFAULT 'booked'
        CHECK (status IN ('booked', 'confirmed', 'cancelled', 'rescheduled', 'completed', 'no_show')),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT
);
"""


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def book(conn, name, appt_date, start_time, phone="9876543210"):
    pid = core.propose(conn, "book_appointment", {
        "patient_name": name, "patient_phone": phone, "appt_date": appt_date, "start_time": start_time,
        "duration_minutes": 15,  # fixtures lay appointments out 15 minutes apart
    })
    return core.confirm(conn, pid, HANDLERS)[1]


def act(conn, intent, appointment_id, handlers=HANDLERS):
    pid = core.propose(conn, intent, {"appointment_id": appointment_id})
    return core.confirm(conn, pid, handlers)


DAY = "2026-10-05"


class SchemaUpgradeTests(unittest.TestCase):
    def _columns(self, conn):
        return {row[1] for row in conn.execute("PRAGMA table_info(appointments)")}

    def test_fresh_db_has_new_columns_and_notifications_table(self):
        conn = make_db()
        self.assertIn("queue_state", self._columns(conn))
        self.assertIn("last_notified_token", self._columns(conn))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 0)

    def test_old_schema_is_upgraded_in_place_without_losing_rows(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(OLD_APPOINTMENTS_SCHEMA)
        conn.execute("INSERT INTO appointments (patient_name, appt_date, start_time) VALUES ('Old Row', ?, '09:00')", (DAY,))
        conn.commit()
        self.assertNotIn("queue_state", self._columns(conn))

        db.ensure_columns(conn)
        db.ensure_columns(conn)  # idempotent

        self.assertIn("queue_state", self._columns(conn))
        self.assertIn("last_notified_token", self._columns(conn))
        row = conn.execute("SELECT * FROM appointments").fetchone()
        self.assertEqual(row["patient_name"], "Old Row")
        self.assertIsNone(row["queue_state"])
        self.assertIsNone(row["last_notified_token"])

    def test_connect_upgrades_an_existing_file_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            old = sqlite3.connect(path)
            old.executescript(OLD_APPOINTMENTS_SCHEMA)
            old.execute("INSERT INTO appointments (patient_name, appt_date, start_time) VALUES ('Old Row', ?, '09:00')", (DAY,))
            old.commit()
            old.close()

            conn = db.connect(path)
            self.assertIn("queue_state", self._columns(conn))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0], 1)
            conn.close()
            conn = db.connect(path)  # a second connect is a no-op
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 0)
            conn.close()


class TokenRankingTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tokens(self, day=DAY):
        return {e["name"]: e["token"] for e in token_queue.day_queue(self.conn, day)}

    def test_tokens_follow_slot_order_not_booking_order(self):
        book(self.conn, "Late", DAY, "11:00")
        book(self.conn, "Early", DAY, "09:00")
        book(self.conn, "Mid", DAY, "10:00")
        self.assertEqual(self.tokens(), {"Early": 1, "Mid": 2, "Late": 3})

    def test_format_token_is_zero_padded(self):
        self.assertEqual(token_queue.format_token(4), "T-04")
        self.assertEqual(token_queue.format_token(12), "T-12")

    def test_tokens_are_per_date(self):
        book(self.conn, "A", DAY, "09:00")
        book(self.conn, "B", "2026-10-06", "09:00")
        self.assertEqual(self.tokens(DAY), {"A": 1})
        self.assertEqual(self.tokens("2026-10-06"), {"B": 1})

    def test_new_earlier_booking_shifts_everyone_behind(self):
        book(self.conn, "A", DAY, "10:00")
        book(self.conn, "B", DAY, "10:15")
        self.assertEqual(self.tokens(), {"A": 1, "B": 2})
        book(self.conn, "Early", DAY, "09:00")
        self.assertEqual(self.tokens(), {"Early": 1, "A": 2, "B": 3})

    def test_cancellation_renumbers_those_behind_and_cancelled_has_no_token(self):
        a = book(self.conn, "A", DAY, "09:00")
        book(self.conn, "B", DAY, "09:15")
        book(self.conn, "C", DAY, "09:30")
        core.confirm(self.conn, core.propose(self.conn, "cancel_appointment", {"appointment_id": a}), HANDLERS)
        self.assertEqual(self.tokens(), {"B": 1, "C": 2})
        self.assertIsNone(token_queue.token_for(self.conn, a))

    def test_reschedule_away_renumbers_old_day_and_ranks_on_new_day(self):
        a = book(self.conn, "A", DAY, "09:00")
        book(self.conn, "B", DAY, "09:15")
        book(self.conn, "X", "2026-10-06", "09:00")
        core.confirm(self.conn, core.propose(self.conn, "reschedule_appointment", {
            "appointment_id": a, "appt_date": "2026-10-06", "start_time": "09:30"}), HANDLERS)
        self.assertEqual(self.tokens(DAY), {"B": 1})
        self.assertEqual(self.tokens("2026-10-06"), {"X": 1, "A": 2})

    def test_rescheduled_status_is_unranked_like_cancelled(self):
        a = book(self.conn, "A", DAY, "09:00")
        book(self.conn, "B", DAY, "09:15")
        self.conn.execute("UPDATE appointments SET status = 'rescheduled' WHERE id = ?", (a,))
        self.assertEqual(self.tokens(), {"B": 1})

    def test_completed_and_no_show_keep_their_rank(self):
        a = book(self.conn, "A", DAY, "09:00")
        b = book(self.conn, "B", DAY, "09:15")
        book(self.conn, "C", DAY, "09:30")
        act(self.conn, "queue_mark_done", a)
        act(self.conn, "queue_mark_no_show", b)
        self.assertEqual(self.tokens(), {"A": 1, "B": 2, "C": 3})


class PositionTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.a = book(self.conn, "A", DAY, "09:00")
        self.b = book(self.conn, "B", DAY, "09:15")
        self.c = book(self.conn, "C", DAY, "09:30")
        self.d = book(self.conn, "D", DAY, "09:45")

    def ahead(self, appointment_id):
        return token_queue.queue_entry(self.conn, appointment_id)["ahead"]

    def test_ahead_counts_outstanding_with_smaller_token(self):
        self.assertEqual([self.ahead(x) for x in (self.a, self.b, self.c, self.d)], [0, 1, 2, 3])

    def test_completed_ahead_does_not_count(self):
        act(self.conn, "queue_mark_done", self.a)
        self.assertEqual(self.ahead(self.d), 2)
        self.assertEqual(token_queue.queue_entry(self.conn, self.d)["token"], 4)  # but its token is unchanged

    def test_no_show_ahead_does_not_count(self):
        act(self.conn, "queue_mark_no_show", self.b)
        self.assertEqual(self.ahead(self.d), 2)

    def test_checked_in_and_in_consultation_ahead_still_count(self):
        act(self.conn, "queue_check_in", self.a)
        act(self.conn, "queue_call_next", self.a)
        act(self.conn, "queue_check_in", self.b)
        self.assertEqual(self.ahead(self.c), 2)

    def test_finished_entries_have_no_position(self):
        act(self.conn, "queue_mark_done", self.a)
        self.assertIsNone(self.ahead(self.a))

    def test_snapshot_and_next_to_call(self):
        self.assertIsNone(token_queue.next_to_call(self.conn, DAY))
        act(self.conn, "queue_check_in", self.c)
        act(self.conn, "queue_check_in", self.b)
        self.assertEqual(token_queue.next_to_call(self.conn, DAY)["id"], self.b)  # lowest token checked in
        act(self.conn, "queue_call_next", self.b)
        snap = token_queue.queue_snapshot(self.conn, DAY)
        self.assertEqual(snap["now_serving"], "T-02 B")
        self.assertEqual(snap["next_up"], "T-03 C")
        self.assertEqual(snap["waiting"], 3)
        self.assertEqual(snap["total_today"], 4)


class QueueActionTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.a = book(self.conn, "A", DAY, "09:00")
        self.b = book(self.conn, "B", DAY, "09:15")

    def row(self, appointment_id):
        return self.conn.execute("SELECT status, queue_state FROM appointments WHERE id = ?", (appointment_id,)).fetchone()

    def test_full_lifecycle_and_audit(self):
        act(self.conn, "queue_check_in", self.a)
        self.assertEqual(tuple(self.row(self.a)), ("booked", "checked_in"))
        act(self.conn, "queue_call_next", self.a)
        self.assertEqual(tuple(self.row(self.a)), ("booked", "in_consultation"))
        act(self.conn, "queue_mark_done", self.a)
        self.assertEqual(tuple(self.row(self.a)), ("completed", None))
        intents = [r["intent"] for r in self.conn.execute("SELECT intent FROM audit_log ORDER BY id")]
        self.assertEqual(intents[-3:], ["queue_check_in", "queue_call_next", "queue_mark_done"])

    def test_no_show(self):
        act(self.conn, "queue_mark_no_show", self.b)
        self.assertEqual(tuple(self.row(self.b)), ("no_show", None))

    def test_cannot_act_on_cancelled(self):
        core.confirm(self.conn, core.propose(self.conn, "cancel_appointment", {"appointment_id": self.a}), HANDLERS)
        with self.assertRaises(token_queue.QueueActionError):
            act(self.conn, "queue_check_in", self.a)
        self.assertEqual(self.row(self.a)["status"], "cancelled")

    def test_cannot_act_twice_on_a_finished_appointment(self):
        act(self.conn, "queue_mark_done", self.a)
        with self.assertRaises(token_queue.QueueActionError):
            act(self.conn, "queue_mark_no_show", self.a)

    def test_second_call_is_blocked_while_someone_is_in_consultation(self):
        act(self.conn, "queue_call_next", self.a)
        with self.assertRaises(token_queue.QueueActionError):
            act(self.conn, "queue_call_next", self.b)
        self.assertIsNone(self.row(self.b)["queue_state"])
        # the failed approve wrote no audit row
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM audit_log WHERE intent='queue_call_next'").fetchone()[0], 1)

    def test_missing_appointment_id_is_an_error(self):
        with self.assertRaises(token_queue.QueueActionError):
            act(self.conn, "queue_check_in", None)

    def test_handlers_are_wired_through_the_adapter_seam(self):
        clinical, ops = get_adapters()
        handlers = build_write_handlers(clinical, ops)
        for intent in ("queue_check_in", "queue_call_next", "queue_mark_done", "queue_mark_no_show"):
            self.assertIn(intent, handlers)
        act(self.conn, "queue_check_in", self.a, handlers=handlers)
        self.assertEqual(self.row(self.a)["queue_state"], "checked_in")


if __name__ == "__main__":
    unittest.main()
