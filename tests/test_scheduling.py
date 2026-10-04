import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import scheduling

SCHEMA = (Path(__file__).resolve().parents[1] / "clinic" / "schema.sql").read_text()


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def _book(conn, appt_date, start_time, duration_minutes=15, status="booked"):
    cur = conn.execute(
        "INSERT INTO appointments (patient_name, appt_date, start_time, duration_minutes, status) "
        "VALUES ('Test Patient', ?, ?, ?, ?)",
        (appt_date, start_time, duration_minutes, status),
    )
    return cur.lastrowid


class IsSlotFreeTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def test_empty_day_is_free(self):
        self.assertTrue(scheduling.is_slot_free(self.conn, "2026-10-01", "09:00", 15))

    def test_exact_overlap_is_not_free(self):
        _book(self.conn, "2026-10-01", "09:00", 15)
        self.assertFalse(scheduling.is_slot_free(self.conn, "2026-10-01", "09:00", 15))

    def test_partial_overlap_is_not_free(self):
        _book(self.conn, "2026-10-01", "09:00", 30)
        self.assertFalse(scheduling.is_slot_free(self.conn, "2026-10-01", "09:15", 15))

    def test_adjacent_slot_is_free(self):
        _book(self.conn, "2026-10-01", "09:00", 15)
        self.assertTrue(scheduling.is_slot_free(self.conn, "2026-10-01", "09:15", 15))

    def test_cancelled_appointment_does_not_block(self):
        _book(self.conn, "2026-10-01", "09:00", 15, status="cancelled")
        self.assertTrue(scheduling.is_slot_free(self.conn, "2026-10-01", "09:00", 15))

    def test_confirmed_appointment_blocks_like_booked(self):
        _book(self.conn, "2026-10-01", "09:00", 15, status="confirmed")
        self.assertFalse(scheduling.is_slot_free(self.conn, "2026-10-01", "09:00", 15))

    def test_different_day_does_not_conflict(self):
        _book(self.conn, "2026-10-01", "09:00", 15)
        self.assertTrue(scheduling.is_slot_free(self.conn, "2026-10-02", "09:00", 15))

    def test_exclude_appointment_id_ignores_its_own_row(self):
        appt_id = _book(self.conn, "2026-10-01", "09:00", 15)
        self.assertFalse(scheduling.is_slot_free(self.conn, "2026-10-01", "09:00", 15))
        self.assertTrue(
            scheduling.is_slot_free(self.conn, "2026-10-01", "09:00", 15, exclude_appointment_id=appt_id)
        )


class GenerateSlotsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def test_empty_day_returns_all_clinic_hour_slots(self):
        slots = scheduling.generate_slots(self.conn, "2026-10-01")
        # Slots are 30 minutes: (13:00 - 09:00) / 30 + (20:00 - 16:00) / 30 = 8 + 8 = 16
        self.assertEqual(scheduling.SLOT_MINUTES, 30)
        self.assertEqual(len(slots), 16)
        self.assertIn("09:00", slots)
        self.assertIn("12:30", slots)
        self.assertNotIn("12:45", slots)  # a 30-minute visit starting here would run past 13:00
        self.assertNotIn("13:00", slots)  # shift ends at 13:00, exclusive
        self.assertIn("16:00", slots)
        self.assertIn("19:30", slots)
        self.assertNotIn("19:45", slots)

    def test_booked_slot_is_excluded(self):
        _book(self.conn, "2026-10-01", "09:00", 30)
        slots = scheduling.generate_slots(self.conn, "2026-10-01")
        self.assertNotIn("09:00", slots)
        self.assertIn("09:30", slots)

    def test_an_older_15_minute_appointment_still_blocks_the_slot_it_overlaps(self):
        _book(self.conn, "2026-10-01", "09:15", 15)   # booked back when appointments were 15 minutes
        slots = scheduling.generate_slots(self.conn, "2026-10-01")
        self.assertNotIn("09:00", slots)   # 09:00-09:30 overlaps it
        self.assertIn("09:30", slots)

    def test_default_appointment_length_is_30_minutes(self):
        self.assertEqual(scheduling.SLOT_MINUTES, 30)

    def test_slots_are_chronologically_ordered(self):
        slots = scheduling.generate_slots(self.conn, "2026-10-01")
        self.assertEqual(slots, sorted(slots))


if __name__ == "__main__":
    unittest.main()


class DefaultAppointmentLengthTests(unittest.TestCase):
    """A new appointment is 30 minutes unless a length is given explicitly."""

    def test_a_new_booking_is_30_minutes_by_default(self):
        from clinic import core
        from clinic.intents import HANDLERS
        conn = make_db()
        pid = core.propose(conn, "book_appointment", {
            "patient_name": "Walk In", "patient_phone": "9000000001",
            "appt_date": "2026-10-06", "start_time": "10:00",
        })
        _, appointment_id = core.confirm(conn, pid, HANDLERS)
        row = conn.execute("SELECT duration_minutes FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
        self.assertEqual(row[0], 30)
        # ...so the next 30-minute slot is the earliest free one after it
        self.assertFalse(scheduling.is_slot_free(conn, "2026-10-06", "10:00", 30))
        self.assertFalse(scheduling.is_slot_free(conn, "2026-10-06", "10:15", 30))
        self.assertTrue(scheduling.is_slot_free(conn, "2026-10-06", "10:30", 30))

    def test_an_explicit_length_is_still_respected(self):
        from clinic import core
        from clinic.intents import HANDLERS
        conn = make_db()
        pid = core.propose(conn, "book_appointment", {
            "patient_name": "Walk In", "patient_phone": "9000000001",
            "appt_date": "2026-10-06", "start_time": "10:00", "duration_minutes": 45,
        })
        _, appointment_id = core.confirm(conn, pid, HANDLERS)
        row = conn.execute("SELECT duration_minutes FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
        self.assertEqual(row[0], 45)

    def test_calendar_events_default_to_the_same_length(self):
        from clinic import gcal_sync
        self.assertEqual(gcal_sync.DEFAULT_DURATION_MINUTES, 30)
