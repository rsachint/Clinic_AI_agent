import sqlite3
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import branches, notify  # noqa: E402
from clinic.notify import Now  # noqa: E402

SCHEMA = (Path(__file__).resolve().parents[1] / "clinic" / "schema.sql").read_text()
TODAY, TOMORROW = "2026-10-05", "2026-10-06"


def at(day, hour, minute=0):
    local = datetime(2026, 10, day, hour, minute)
    return Now(local, local - timedelta(hours=5, minutes=30))


class BranchNotifyTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.addCleanup(self.conn.close)
        self.b = branches.add_branch(self.conn, "B", "Branch B", "Sector 56, Gurugram", maps_url="https://maps.example/b", pin_code="122011")
        self.c = branches.add_branch(self.conn, "C", "Branch C", pin_code="122018")

    def book(self, name, day, time, branch, phone="9876500001"):
        cur = self.conn.execute(
            "INSERT INTO appointments (patient_name, patient_phone, appt_date, start_time, status, branch_id) VALUES (?, ?, ?, ?, 'booked', ?)",
            (name, phone, day, time, branch))
        self.conn.commit()
        return cur.lastrowid

    def bodies(self, event):
        return [r["body"] for r in self.conn.execute("SELECT body FROM notifications WHERE event = ? ORDER BY id", (event,))]

    def test_a_booking_confirmation_names_the_branch_the_token_and_where_it_is(self):
        appt = self.book("Pt B", TOMORROW, "10:00", self.b)
        notify.notify_appointment(self.conn, "booking_confirmed", appt, TOMORROW, at(5, 12), language="en")
        body = self.bodies("booking_confirmed")[0]
        self.assertIn("B-T01", body)
        self.assertIn("\U0001F4CD Branch B, Sector 56, Gurugram", body)
        self.assertIn("https://maps.example/b", body)

    def test_the_default_branch_is_named_too_when_there_are_several(self):
        appt = self.book("Pt A", TOMORROW, "10:00", 1)
        notify.notify_appointment(self.conn, "booking_confirmed", appt, TOMORROW, at(5, 12), language="en")
        body = self.bodies("booking_confirmed")[0]
        self.assertIn("A-T01", body)
        self.assertIn("\U0001F4CD Branch A", body)

    def test_a_cancellation_has_no_where_line(self):
        appt = self.book("Pt B", TOMORROW, "10:00", self.b)
        self.conn.execute("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (appt,))
        self.conn.commit()
        notify.notify_appointment(self.conn, "appointment_cancelled", appt, TOMORROW, at(5, 12), language="en")
        for body in self.bodies("appointment_cancelled"):
            self.assertNotIn("\U0001F4CD", body)

    def test_a_single_branch_database_is_unchanged(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        cur = conn.execute("INSERT INTO appointments (patient_name, patient_phone, appt_date, start_time, status) VALUES ('P', '9876500001', ?, '10:00', 'booked')", (TOMORROW,))
        notify.notify_appointment(conn, "booking_confirmed", cur.lastrowid, TOMORROW, at(5, 12), language="en")
        body = conn.execute("SELECT body FROM notifications").fetchone()[0]
        self.assertIn("T-01", body)
        self.assertNotIn("A-T01", body)
        self.assertNotIn("\U0001F4CD", body)
        conn.close()

    def test_reminders_go_to_patients_at_every_branch(self):
        for i, branch in enumerate((1, self.b, self.c)):
            self.book("Today {}".format(i), TODAY, "11:00", branch, phone="98765002{:02d}".format(i))
            self.book("Tomorrow {}".format(i), TOMORROW, "09:00", branch, phone="98765003{:02d}".format(i))
        self.conn.execute("DELETE FROM notifications")
        self.conn.commit()
        self.assertEqual(notify.generate_reminders(self.conn, at(5, 8)), 3)           # a morning reminder at each branch
        self.assertEqual(len(self.bodies("reminder_morning")), 3)
        self.assertEqual(notify.generate_reminders(self.conn, at(5, 18)), 3)          # and a day-before at each
        self.assertEqual(len(self.bodies("reminder_day_before")), 3)
        for body in self.bodies("reminder_day_before"):
            self.assertIn("\U0001F4CD", body)

    def test_a_token_change_at_one_branch_only_renumbers_that_branch(self):
        first = self.book("First B", TODAY, "10:00", self.b, phone="9876500401")
        second = self.book("Second B", TODAY, "10:30", self.b, phone="9876500402")
        other = self.book("Other A", TODAY, "10:00", 1, phone="9876500403")
        for appt in (first, second, other):
            notify.notify_appointment(self.conn, "booking_confirmed", appt, TODAY, at(5, 9), language="en")
        self.conn.execute("DELETE FROM notifications")
        self.conn.execute("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (first,))
        self.conn.commit()
        notify.fanout_queue_changes(self.conn, at(5, 9, 5))
        changed = [r["appointment_id"] for r in self.conn.execute("SELECT appointment_id FROM notifications WHERE event = 'token_changed'")]
        self.assertEqual(changed, [second])           # Branch A's patient is not told anything


if __name__ == "__main__":
    unittest.main()
