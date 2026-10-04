import os
import sqlite3
import sys
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ["WHATSAPP_NOTIFY_MODE"] = "dry_run"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import core, notify, scheduler
from clinic.intents import HANDLERS
from clinic.notify import Now

SCHEMA = (Path(__file__).resolve().parents[1] / "clinic" / "schema.sql").read_text()


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


class FakeSender:
    def __init__(self, fail_with=None):
        self.calls = []
        self.fail_with = fail_with

    def __call__(self, wa_id, text):
        self.calls.append((wa_id, text))
        if self.fail_with:
            raise self.fail_with


def at(day, hour, minute=0):
    local = datetime(2026, 10, day, hour, minute)
    return Now(local, local - timedelta(hours=5, minutes=30))


def book(conn, name, day, start, phone):
    pid = core.propose(conn, "book_appointment", {
        "patient_name": name, "patient_phone": phone, "appt_date": "2026-10-{:02d}".format(day), "start_time": start})
    return core.confirm(conn, pid, HANDLERS)[1]


def inbound(conn, wa_id, now):
    conn.execute(
        "INSERT INTO wa_messages (wa_message_id, wa_id, message_type, raw_text, received_at) VALUES (?, ?, 'text', 'hello', ?)",
        ("w" + wa_id + str(now.utc), wa_id, now.utc.strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()


class TickTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.appt = book(self.conn, "Sunita", 6, "09:00", "9876543210")   # tomorrow relative to the 5th
        inbound(self.conn, "919876543210", at(5, 18))

    def test_evening_tick_sends_the_day_before_reminder(self):
        sender = FakeSender()
        summary = scheduler.tick(self.conn, sender, now=at(5, 18, 5))
        self.assertEqual(summary["reminders_created"], 1)
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(sender.calls[0][0], "919876543210")
        self.assertIn("tomorrow", sender.calls[0][1].lower() + "")  # bilingual/en text mentions tomorrow
        self.assertEqual(summary["flushed"], {"sent": 1})

    def test_repeated_ticks_never_duplicate_a_reminder(self):
        sender = FakeSender()
        for minute in range(1, 30):
            scheduler.tick(self.conn, sender, now=at(5, 18, minute))
        self.assertEqual(len(sender.calls), 1)

    def test_morning_tick_sends_the_final_token_once(self):
        sender = FakeSender()
        inbound(self.conn, "919876543210", at(6, 7))
        scheduler.tick(self.conn, sender, now=at(6, 7, 59))
        self.assertEqual(sender.calls, [])
        scheduler.tick(self.conn, sender, now=at(6, 8, 0))
        scheduler.tick(self.conn, sender, now=at(6, 8, 1))
        morning = [c for c in sender.calls if "T-01" in c[1]]
        self.assertEqual(len(morning), 1)

    def test_tick_retries_a_failed_send_and_stops_after_the_limit(self):
        sender = FakeSender(fail_with=RuntimeError("down"))
        for minute in range(1, 8):
            scheduler.tick(self.conn, sender, now=at(5, 18, minute))
        row = self.conn.execute("SELECT status, attempts FROM notifications").fetchone()
        self.assertEqual(tuple(row), ("failed", notify.RETRY_MAX_ATTEMPTS))
        self.assertEqual(len(sender.calls), notify.RETRY_MAX_ATTEMPTS)

    def test_a_retry_succeeds_once_the_sender_recovers(self):
        flaky = FakeSender(fail_with=RuntimeError("down"))
        scheduler.tick(self.conn, flaky, now=at(5, 18, 1))
        good = FakeSender()
        scheduler.tick(self.conn, good, now=at(5, 18, 2))
        self.assertEqual(self.conn.execute("SELECT status FROM notifications").fetchone()[0], "sent")

    def test_dry_run_tick_sends_nothing_and_records_dry_run(self):
        scheduler.tick(self.conn, None, dry_run=True, now=at(5, 18, 1))
        self.assertEqual(self.conn.execute("SELECT status FROM notifications").fetchone()[0], "dry_run")

    def test_tick_without_a_window_blocks_instead_of_sending(self):
        conn = make_db()
        book(conn, "Nobody", 6, "09:00", "9333333333")
        sender = FakeSender()
        scheduler.tick(conn, sender, now=at(5, 18, 1))
        self.assertEqual(sender.calls, [])
        self.assertEqual(conn.execute("SELECT status FROM notifications").fetchone()[0], "blocked_no_window")

    def test_a_step_that_raises_is_logged_and_does_not_stop_the_tick(self):
        sender = FakeSender()
        with patch("clinic.notify.generate_reminders", side_effect=RuntimeError("bug")):
            summary = scheduler.tick(self.conn, sender, now=at(5, 18, 1))
        self.assertEqual(summary["errors"], 1)
        # and the next tick, with the bug gone, carries on normally
        summary = scheduler.tick(self.conn, sender, now=at(5, 18, 2))
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(len(sender.calls), 1)

    def test_tick_never_raises_even_if_flush_does(self):
        with patch("clinic.notify.flush", side_effect=sqlite3.OperationalError("database is locked")):
            summary = scheduler.tick(self.conn, FakeSender(), now=at(5, 18, 1))
        self.assertGreaterEqual(summary["errors"], 1)


class NoThreadsTests(unittest.TestCase):
    def test_importing_the_app_does_not_start_the_scheduler(self):
        os.environ.setdefault("SARVAM_API_KEY", "test-not-real")
        from unittest import mock
        with mock.patch("dotenv.load_dotenv"):
            import app  # noqa: F401
        self.assertNotIn("notification-scheduler", [t.name for t in threading.enumerate()])

    def test_hour_constants_live_in_one_place(self):
        self.assertEqual(notify.REMINDER_DAY_BEFORE_FROM_HOUR, 18)
        self.assertEqual(notify.REMINDER_MORNING_FROM_HOUR, 8)


if __name__ == "__main__":
    unittest.main()
