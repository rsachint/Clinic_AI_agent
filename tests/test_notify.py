import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ["WHATSAPP_NOTIFY_MODE"] = "dry_run"  # belt and braces: nothing here may ever reach the real API

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import core, notify, token_queue, whatsapp
from clinic.intents import HANDLERS
from clinic.notify import Now

SCHEMA = (Path(__file__).resolve().parents[1] / "clinic" / "schema.sql").read_text()

TODAY = "2026-10-05"   # a Monday
TOMORROW = "2026-10-06"
NOW = Now(local=datetime(2026, 10, 5, 10, 0), utc=datetime(2026, 10, 5, 4, 30))


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


class FakeSender:
    """Records calls instead of sending anything."""

    def __init__(self, fail_with=None):
        self.calls = []
        self.fail_with = fail_with

    def __call__(self, wa_id, text):
        self.calls.append((wa_id, text))
        if self.fail_with:
            raise self.fail_with


def inbound(conn, wa_id, text, when_utc=None, n=[0]):
    n[0] += 1
    when = (when_utc or NOW.utc - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO wa_messages (wa_message_id, wa_id, message_type, raw_text, received_at) VALUES (?, ?, 'text', ?, ?)",
        ("wamid.test{}".format(n[0]), wa_id, text, when),
    )
    conn.commit()


def book(conn, name, appt_date, start_time, phone="9876543210", now=NOW, hook=True):
    pid = core.propose(conn, "book_appointment", {
        "patient_name": name, "patient_phone": phone, "appt_date": appt_date, "start_time": start_time,
        "duration_minutes": 15,  # fixtures lay appointments out 15 minutes apart
    })
    _, appt_id = core.confirm(conn, pid, HANDLERS)
    if hook:
        notify.after_write(conn, "book_appointment", {"appt_date": appt_date}, appt_id, now=now)
    return appt_id


def book_legacy_without_phone(conn, name, appt_date, start_time):
    """An appointment from before a phone was mandatory (the booking handler now refuses one): written straight
    to the table, as older rows are, then announced through the notification hook like any booking."""
    cur = conn.execute("INSERT INTO appointments (patient_name, appt_date, start_time, duration_minutes) VALUES (?, ?, ?, 15)",
                       (name, appt_date, start_time))
    conn.commit()
    notify.after_write(conn, "book_appointment", {"appt_date": appt_date}, cur.lastrowid, now=NOW)
    return cur.lastrowid


def rows(conn, event=None):
    q = "SELECT * FROM notifications"
    params = ()
    if event:
        q += " WHERE event = ?"
        params = (event,)
    return conn.execute(q + " ORDER BY id", params).fetchall()


class PhoneToWaIdTests(unittest.TestCase):
    def test_conversions(self):
        self.assertEqual(whatsapp.phone_to_wa_id("9876543210"), "919876543210")
        self.assertEqual(whatsapp.phone_to_wa_id("98765 43210"), "919876543210")
        self.assertEqual(whatsapp.phone_to_wa_id("09876543210"), "919876543210")
        self.assertEqual(whatsapp.phone_to_wa_id("919876543210"), "919876543210")
        self.assertEqual(whatsapp.phone_to_wa_id("+91 98765-43210"), "919876543210")
        self.assertIsNone(whatsapp.phone_to_wa_id("98765"))
        self.assertIsNone(whatsapp.phone_to_wa_id(None))
        self.assertIsNone(whatsapp.phone_to_wa_id(""))


class TemplateTests(unittest.TestCase):
    def test_every_template_exists_in_every_language_and_renders_fully(self):
        for key, by_lang in notify.TEMPLATES.items():
            self.assertEqual(set(by_lang), {"en", "hi", "hinglish"}, key)
            for lang in ("en", "hi", "hinglish", "bilingual"):
                text = notify.render(key, lang, name="Sunita", token=4, old_token=5,
                                     date=TOMORROW, time="16:30", ahead=2)
                self.assertNotIn("{", text, (key, lang))
                self.assertNotIn("}", text, (key, lang))
                self.assertTrue(text.strip())

    def test_token_is_rendered_zero_padded(self):
        self.assertIn("T-04", notify.render("booking_confirmed_today", "en", name="A", token=4, time="09:00", ahead=1))

    def test_languages_differ_and_bilingual_has_both(self):
        kw = dict(name="Sunita", token=4, time="09:00", ahead=1)
        en = notify.render("booking_confirmed_today", "en", **kw)
        hi = notify.render("booking_confirmed_today", "hi", **kw)
        hing = notify.render("booking_confirmed_today", "hinglish", **kw)
        both = notify.render("booking_confirmed_today", "bilingual", **kw)
        self.assertEqual(len({en, hi, hing}), 3)
        self.assertIn("आपकी", hi)
        self.assertIn("aapki", hing)
        self.assertIn(en, both)
        self.assertIn(hing, both)

    def test_ahead_phrase(self):
        self.assertEqual(notify.ahead_line("en", 0), "You are next.")
        self.assertEqual(notify.ahead_line("en", 1), "1 patient is ahead of you.")
        self.assertEqual(notify.ahead_line("en", 3), "3 patients are ahead of you.")
        self.assertEqual(notify.ahead_line("en", None), "")

    def test_registered_never_claims_an_appointment(self):
        for lang in ("en", "hi", "hinglish", "bilingual"):
            text = notify.render("registered", lang, name="Sunita")
            self.assertNotIn("appointment", text.lower())
            self.assertNotIn("अपॉइंटमेंट", text)
            self.assertNotIn("token", text.lower())
        self.assertEqual(
            notify.render("registered", "en", name="Sunita"),
            "You're registered with us, Sunita. We'll contact you shortly to schedule your visit.",
        )

    def test_future_booking_is_marked_provisional(self):
        text = notify.render("booking_confirmed_future", "en", name="A", token=7, date=TOMORROW, time="09:00")
        self.assertIn("currently T-07", text)
        self.assertIn("final token is confirmed on the day", text)

    def test_date_and_time_formatting(self):
        self.assertEqual(notify.format_date("2026-10-06", "en"), "Tuesday, 6 Oct 2026")
        self.assertEqual(notify.format_time("16:30", "en"), "4:30 PM")
        self.assertEqual(notify.format_time("09:05", "en"), "9:05 AM")
        self.assertEqual(notify.format_time("16:30", "hi"), "शाम 4:30")

    def test_meta_templates_are_well_formed(self):
        self.assertTrue(notify.META_TEMPLATES)
        names = set()
        for t in notify.META_TEMPLATES:
            self.assertEqual(set(t), {"name", "language", "variables", "body"})
            self.assertNotIn(t["name"], names)
            names.add(t["name"])
            for i in range(1, len(t["variables"]) + 1):
                self.assertIn("{{%d}}" % i, t["body"], t["name"])
            self.assertNotIn("{greet}", t["body"])


class EnqueueTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def test_enqueue_is_idempotent_on_dedup_key(self):
        first = notify.enqueue(self.conn, event="your_turn", dedup_key="k1", body="x", wa_id="919876543210", now=NOW)
        second = notify.enqueue(self.conn, event="your_turn", dedup_key="k1", body="x", wa_id="919876543210", now=NOW)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(rows(self.conn)), 1)

    def test_no_phone_is_recorded_as_skipped(self):
        notify.enqueue(self.conn, event="your_turn", dedup_key="k1", body="x", wa_id=None, now=NOW)
        self.assertEqual(rows(self.conn)[0]["status"], "skipped_no_phone")

    def test_appointment_with_no_phone_records_a_skip_but_no_token_marker(self):
        a = book_legacy_without_phone(self.conn, "NoPhone", TODAY, "09:00")
        self.assertEqual([r["status"] for r in rows(self.conn)], ["skipped_no_phone"])
        self.assertIsNone(self.conn.execute("SELECT last_notified_token FROM appointments WHERE id=?", (a,)).fetchone()[0])

    def test_recipient_phone_falls_back_to_the_registered_patient(self):
        pid = core.confirm(self.conn, core.propose(self.conn, "register_patient", {"name": "Sunita", "phone": "9123456780"}), HANDLERS)[1]
        proposal = core.propose(self.conn, "book_appointment", {"patient_id": pid, "appt_date": TODAY, "start_time": "09:00"})
        _, appt = core.confirm(self.conn, proposal, HANDLERS)
        notify.after_write(self.conn, "book_appointment", {"appt_date": TODAY}, appt, now=NOW)
        r = rows(self.conn)[0]
        self.assertEqual(r["wa_id"], "919123456780")
        self.assertIn("Sunita", r["body"])


class FlushTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.appt = book(self.conn, "Sunita", TODAY, "09:00", phone="9876543210")

    def test_no_inbound_message_means_blocked_and_nothing_sent(self):
        sender = FakeSender()
        notify.flush(self.conn, sender, now=NOW)
        self.assertEqual(sender.calls, [])
        r = rows(self.conn)[0]
        self.assertEqual(r["status"], "blocked_no_window")
        self.assertIn("24", r["error"])

    def test_inbound_within_24h_is_sent(self):
        inbound(self.conn, "919876543210", "mujhe appointment chahiye", NOW.utc - timedelta(hours=23))
        sender = FakeSender()
        counts = notify.flush(self.conn, sender, now=NOW)
        self.assertEqual(counts, {"sent": 1})
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(sender.calls[0][0], "919876543210")
        r = rows(self.conn)[0]
        self.assertEqual(r["status"], "sent")
        self.assertIsNotNone(r["sent_at"])
        self.assertEqual(r["attempts"], 1)

    def test_inbound_older_than_24h_is_blocked(self):
        inbound(self.conn, "919876543210", "hello", NOW.utc - timedelta(hours=25))
        sender = FakeSender()
        notify.flush(self.conn, sender, now=NOW)
        self.assertEqual(sender.calls, [])
        self.assertEqual(rows(self.conn)[0]["status"], "blocked_no_window")

    def test_window_matches_a_sender_by_last_10_digits(self):
        inbound(self.conn, "9876543210", "hello")  # stored without the country code
        sender = FakeSender()
        notify.flush(self.conn, sender, now=NOW)
        self.assertEqual(len(sender.calls), 1)

    def test_another_numbers_inbound_does_not_open_the_window(self):
        inbound(self.conn, "919111111111", "hello")
        sender = FakeSender()
        notify.flush(self.conn, sender, now=NOW)
        self.assertEqual(sender.calls, [])

    def test_dry_run_records_status_and_sends_nothing(self):
        inbound(self.conn, "919876543210", "hello")
        sender = FakeSender()
        notify.flush(self.conn, sender, now=NOW, dry_run=True)
        self.assertEqual(sender.calls, [])
        self.assertEqual(rows(self.conn)[0]["status"], "dry_run")

    def test_dry_run_still_reports_a_closed_window(self):
        notify.flush(self.conn, None, now=NOW, dry_run=True)
        self.assertEqual(rows(self.conn)[0]["status"], "blocked_no_window")

    def test_flush_without_sender_and_not_dry_run_is_an_error(self):
        with self.assertRaises(ValueError):
            notify.flush(self.conn, None, now=NOW)

    def test_failure_is_recorded_and_retried_a_bounded_number_of_times(self):
        inbound(self.conn, "919876543210", "hello")
        sender = FakeSender(fail_with=RuntimeError("boom"))
        for _ in range(6):
            notify.flush(self.conn, sender, now=NOW)
        r = rows(self.conn)[0]
        self.assertEqual(r["status"], "failed")
        self.assertIn("boom", r["error"])
        self.assertEqual(r["attempts"], notify.RETRY_MAX_ATTEMPTS)
        self.assertEqual(len(sender.calls), notify.RETRY_MAX_ATTEMPTS)

    def test_old_failures_are_not_retried(self):
        inbound(self.conn, "919876543210", "hello")
        notify.flush(self.conn, FakeSender(fail_with=RuntimeError("boom")), now=NOW)
        later = Now(NOW.local + timedelta(hours=3), NOW.utc + timedelta(hours=3))
        inbound(self.conn, "919876543210", "hello again", later.utc - timedelta(minutes=5))
        sender = FakeSender()
        notify.flush(self.conn, sender, now=later)
        self.assertEqual(sender.calls, [])

    def test_a_failing_row_does_not_stop_the_others(self):
        book(self.conn, "Ramesh", TODAY, "09:15", phone="9111111111")
        inbound(self.conn, "919876543210", "hello")
        inbound(self.conn, "919111111111", "hello")

        def picky(wa_id, text):
            if wa_id == "919876543210":
                raise RuntimeError("only this one fails")

        counts = notify.flush(self.conn, picky, now=NOW)
        self.assertEqual(counts, {"failed": 1, "sent": 1})

    def test_retry_requeues_a_blocked_row_and_window_is_rechecked(self):
        notify.flush(self.conn, FakeSender(), now=NOW)
        nid = rows(self.conn)[0]["id"]
        self.assertTrue(notify.retry(self.conn, nid))
        sender = FakeSender()
        notify.flush(self.conn, sender, now=NOW)       # still no inbound: blocks again
        self.assertEqual(sender.calls, [])
        self.assertTrue(notify.retry(self.conn, nid))
        inbound(self.conn, "919876543210", "hi")
        notify.flush(self.conn, sender, now=NOW)       # window now open
        self.assertEqual(len(sender.calls), 1)
        self.assertFalse(notify.retry(self.conn, nid))  # already sent: not retryable

    def test_resolve_sender_modes(self):
        with patch.dict(os.environ, {"WHATSAPP_NOTIFY_MODE": "dry_run"}):
            self.assertEqual(notify.resolve_sender(), (None, True))
        with patch.dict(os.environ, {"WHATSAPP_NOTIFY_MODE": "live"}):
            self.assertEqual(notify.resolve_sender(), (notify.live_sender, False))
        with patch.dict(os.environ, {"WHATSAPP_NOTIFY_MODE": "garbage"}):
            self.assertEqual(notify.resolve_sender(), (None, True))  # unknown -> cannot message anyone
        fake = FakeSender()
        self.assertEqual(notify.resolve_sender(fake), (fake, False))

    def test_live_sender_uses_the_whatsapp_client(self):
        with patch("clinic.whatsapp.send_message") as send:
            notify.live_sender("919876543210", "hi")
        send.assert_called_once_with("919876543210", "hi")


class LanguageTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def test_language_follows_the_latest_inbound_message(self):
        inbound(self.conn, "919876543210", "I want an appointment", NOW.utc - timedelta(hours=5))
        self.assertEqual(notify.patient_language(self.conn, "919876543210"), "en")
        inbound(self.conn, "919876543210", "mujhe appointment chahiye", NOW.utc - timedelta(hours=3))
        self.assertEqual(notify.patient_language(self.conn, "919876543210"), "hinglish")
        inbound(self.conn, "919876543210", "मुझे अपॉइंटमेंट चाहिए", NOW.utc - timedelta(hours=1))
        self.assertEqual(notify.patient_language(self.conn, "919876543210"), "hi")

    def test_default_is_bilingual(self):
        self.assertEqual(notify.patient_language(self.conn, "919876543210"), "bilingual")

    def test_booking_message_uses_detected_language(self):
        inbound(self.conn, "919876543210", "मुझे अपॉइंटमेंट चाहिए")
        book(self.conn, "Sunita", TODAY, "09:00")
        self.assertIn("आपकी अपॉइंटमेंट", rows(self.conn)[0]["body"])


class BookingNotificationTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def test_same_day_booking_states_token_and_position(self):
        book(self.conn, "A", TODAY, "09:00", phone="9000000001")
        book(self.conn, "B", TODAY, "09:15", phone="9000000002")
        body = rows(self.conn, "booking_confirmed")[1]["body"]
        self.assertIn("T-02", body)
        self.assertIn("1 patient is ahead of you", body)
        self.assertNotIn("provisional", body)

    def test_future_booking_is_provisional_and_arms_nothing(self):
        a = book(self.conn, "A", TOMORROW, "09:00")
        body = rows(self.conn)[0]["body"]
        self.assertIn("currently T-01", body)
        self.assertIn("provisional", body)
        self.assertIsNone(self.conn.execute("SELECT last_notified_token FROM appointments WHERE id=?", (a,)).fetchone()[0])

    def test_same_day_booking_records_the_token_told(self):
        a = book(self.conn, "A", TODAY, "09:00")
        self.assertEqual(self.conn.execute("SELECT last_notified_token FROM appointments WHERE id=?", (a,)).fetchone()[0], 1)

    def test_replaying_the_hook_does_not_duplicate(self):
        a = book(self.conn, "A", TODAY, "09:00")
        notify.after_write(self.conn, "book_appointment", {"appt_date": TODAY}, a, now=NOW)
        self.assertEqual(len(rows(self.conn, "booking_confirmed")), 1)

    def test_cancel_sends_cancelled_message_with_no_token(self):
        a = book(self.conn, "A", TODAY, "09:00")
        core.confirm(self.conn, core.propose(self.conn, "cancel_appointment", {"appointment_id": a}), HANDLERS)
        notify.after_write(self.conn, "cancel_appointment", {"appointment_id": a}, a, now=NOW)
        r = rows(self.conn, "appointment_cancelled")
        self.assertEqual(len(r), 1)
        self.assertIn("cancelled", r[0]["body"])
        self.assertNotIn("T-", r[0]["body"])

    def test_cancel_hook_before_the_cancel_committed_sends_nothing(self):
        a = book(self.conn, "A", TODAY, "09:00")
        notify.after_write(self.conn, "cancel_appointment", {"appointment_id": a}, a, now=NOW)
        self.assertEqual(rows(self.conn, "appointment_cancelled"), [])

    def test_reschedule_message_carries_new_slot_and_token(self):
        book(self.conn, "A", TOMORROW, "09:00", phone="9000000001")
        b = book(self.conn, "B", TOMORROW, "10:00", phone="9000000002")
        slots = {"appointment_id": b, "appt_date": TOMORROW, "start_time": "08:00"}
        core.confirm(self.conn, core.propose(self.conn, "reschedule_appointment", slots), HANDLERS)
        notify.after_write(self.conn, "reschedule_appointment", slots, b, now=NOW)
        body = rows(self.conn, "appointment_rescheduled")[0]["body"]
        self.assertIn("8:00 AM", body)
        self.assertIn("T-01", body)

    def test_registered_goes_to_the_sender_and_is_idempotent(self):
        pid = core.confirm(self.conn, core.propose(self.conn, "register_patient", {"name": "Sunita", "phone": "9876543210"}), HANDLERS)[1]
        slots = {"name": "Sunita", "phone": "9876543210"}
        notify.after_write(self.conn, "register_patient", slots, pid, now=NOW, wa_id="919876543210")
        notify.after_write(self.conn, "register_patient", slots, pid, now=NOW, wa_id="919876543210")
        r = rows(self.conn, "registered")
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["wa_id"], "919876543210")
        self.assertIn("Sunita", r[0]["body"])

    def test_voice_registration_sends_nothing(self):
        notify.after_write(self.conn, "register_patient", {"name": "X", "phone": "9876543210"}, 1, now=NOW)
        self.assertEqual(rows(self.conn), [])


class TokenChangedFanoutTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.a = book(self.conn, "A", TODAY, "09:00", phone="9000000001")
        self.b = book(self.conn, "B", TODAY, "09:15", phone="9000000002")
        self.c = book(self.conn, "C", TODAY, "09:30", phone="9000000003")

    def cancel(self, appointment_id):
        core.confirm(self.conn, core.propose(self.conn, "cancel_appointment", {"appointment_id": appointment_id}), HANDLERS)
        notify.after_write(self.conn, "cancel_appointment", {"appointment_id": appointment_id}, appointment_id, now=NOW)

    def changed(self):
        return {r["appointment_id"]: r["body"] for r in rows(self.conn, "token_changed")}

    def test_cancellation_notifies_only_those_whose_token_changed(self):
        self.cancel(self.a)
        changed = self.changed()
        self.assertEqual(set(changed), {self.b, self.c})
        self.assertIn("now T-01", changed[self.b])
        self.assertIn("it was T-02", changed[self.b])
        self.assertIn("now T-02", changed[self.c])

    def test_cancelling_the_last_patient_changes_nobody(self):
        self.cancel(self.c)
        self.assertEqual(self.changed(), {})

    def test_repeated_fanout_does_not_duplicate(self):
        self.cancel(self.a)
        notify.fanout_queue_changes(self.conn, NOW)
        notify.fanout_queue_changes(self.conn, NOW)
        self.assertEqual(len(rows(self.conn, "token_changed")), 2)

    def test_earlier_booking_shifts_those_behind(self):
        book(self.conn, "Early", TODAY, "08:45", phone="9000000009")
        self.assertEqual(set(self.changed()), {self.a, self.b, self.c})

    def test_a_second_shift_notifies_again(self):
        self.cancel(self.a)          # c: 3 -> 2
        self.cancel(self.b)          # c: 2 -> 1
        c_msgs = [r for r in rows(self.conn, "token_changed") if r["appointment_id"] == self.c]
        self.assertEqual(len(c_msgs), 2)
        self.assertIn("now T-01", c_msgs[1]["body"])

    def test_completed_and_no_show_do_not_shift_tokens_so_nobody_is_notified(self):
        for appt, intent in ((self.a, "queue_mark_done"), (self.b, "queue_mark_no_show")):
            core.confirm(self.conn, core.propose(self.conn, intent, {"appointment_id": appt}), HANDLERS)
            notify.after_write(self.conn, intent, {"appointment_id": appt}, appt, now=NOW)
        self.assertEqual(self.changed(), {})

    def test_future_dates_never_get_token_changed(self):
        x = book(self.conn, "X", TOMORROW, "09:00", phone="9000000011")
        y = book(self.conn, "Y", TOMORROW, "09:15", phone="9000000012")
        core.confirm(self.conn, core.propose(self.conn, "cancel_appointment", {"appointment_id": x}), HANDLERS)
        notify.after_write(self.conn, "cancel_appointment", {"appointment_id": x}, x, now=NOW)
        self.assertEqual([r for r in rows(self.conn, "token_changed") if r["appointment_id"] in (x, y)], [])

    def test_a_provisional_token_does_not_arm_token_changed_until_the_day(self):
        # booked for tomorrow (provisional), then "tomorrow" arrives and an earlier booking shifts it
        x = book(self.conn, "X", TOMORROW, "09:00", phone="9000000011")
        next_day = Now(datetime(2026, 10, 6, 7, 0), datetime(2026, 10, 6, 1, 30))
        book(self.conn, "Early", TOMORROW, "08:45", phone="9000000012", now=next_day)
        self.assertEqual([r for r in rows(self.conn, "token_changed") if r["appointment_id"] == x], [])
        # the morning reminder tells X the real token and arms it
        notify.generate_reminders(self.conn, Now(datetime(2026, 10, 6, 8, 0), datetime(2026, 10, 6, 2, 30)))
        self.assertEqual(self.conn.execute("SELECT last_notified_token FROM appointments WHERE id=?", (x,)).fetchone()[0], 2)

    def test_patient_in_consultation_is_not_sent_token_changed(self):
        notify.after_write(self.conn, "queue_check_in", {"appointment_id": self.b}, self.b, now=NOW)
        core.confirm(self.conn, core.propose(self.conn, "queue_call_next", {"appointment_id": self.b}), HANDLERS)
        self.cancel(self.a)
        self.assertNotIn(self.b, self.changed())


class QueueNotificationTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.ids = [book(self.conn, n, TODAY, t, phone=p) for n, t, p in (
            ("A", "09:00", "9000000001"), ("B", "09:15", "9000000002"),
            ("C", "09:30", "9000000003"), ("D", "09:45", "9000000004"))]

    def do(self, intent, appointment_id):
        core.confirm(self.conn, core.propose(self.conn, intent, {"appointment_id": appointment_id}), HANDLERS)
        notify.after_write(self.conn, intent, {"appointment_id": appointment_id}, appointment_id, now=NOW)

    def test_two_ahead_fires_once_when_position_first_becomes_two(self):
        a, b, c, d = self.ids
        # Bookings alone never send it, even though C was booked at "2 ahead".
        self.assertEqual(rows(self.conn, "queue_two_ahead"), [])
        self.do("queue_mark_done", a)          # D: 3 ahead -> 2 ahead
        msgs = rows(self.conn, "queue_two_ahead")
        self.assertEqual([m["appointment_id"] for m in msgs], [d])
        self.assertIn("T-04", msgs[0]["body"])
        # further actions that leave D at 2 ahead do not repeat it
        self.do("queue_check_in", b)
        notify.fanout_queue_changes(self.conn, NOW)
        self.assertEqual(len(rows(self.conn, "queue_two_ahead")), 1)

    def test_two_ahead_waits_until_the_queue_has_started(self):
        self.do("queue_check_in", self.ids[0])     # arrival alone is not progress
        self.assertEqual(rows(self.conn, "queue_two_ahead"), [])
        self.do("queue_call_next", self.ids[0])    # doctor starts: C is now 2 ahead (A in consultation, B waiting)
        self.assertEqual([m["appointment_id"] for m in rows(self.conn, "queue_two_ahead")], [self.ids[2]])

    def test_a_no_show_ahead_can_trigger_two_ahead(self):
        self.do("queue_mark_no_show", self.ids[0])
        self.assertEqual([m["appointment_id"] for m in rows(self.conn, "queue_two_ahead")], [self.ids[3]])

    def test_call_sends_your_turn_to_that_patient_only(self):
        a, b = self.ids[0], self.ids[1]
        self.do("queue_check_in", b)
        self.do("queue_call_next", b)
        turn = rows(self.conn, "your_turn")
        self.assertEqual([t["appointment_id"] for t in turn], [b])
        self.assertIn("T-02", turn[0]["body"])
        self.assertEqual(turn[0]["wa_id"], "919000000002")

    def test_check_in_and_done_and_no_show_send_nothing_to_that_patient(self):
        a = self.ids[0]
        self.do("queue_check_in", a)
        self.do("queue_mark_done", a)
        self.assertEqual([r for r in rows(self.conn) if r["appointment_id"] == a and r["event"] != "booking_confirmed"], [])


class StatusReplyTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.a = book(self.conn, "A", TODAY, "09:00", phone="9000000001")
        self.b = book(self.conn, "B", TODAY, "09:15", phone="9876543210")

    def test_reply_has_token_and_position(self):
        rid = notify.notify_status_reply(self.conn, "919876543210", self.b, 7, now=NOW)
        body = self.conn.execute("SELECT body, wa_id FROM notifications WHERE id=?", (rid,)).fetchone()
        self.assertIn("T-02", body["body"])
        self.assertIn("1 patient is ahead of you", body["body"])
        self.assertEqual(body["wa_id"], "919876543210")

    def test_reply_is_refused_to_a_number_that_does_not_own_the_appointment(self):
        self.assertIsNone(notify.notify_status_reply(self.conn, "919111111111", self.b, 8, now=NOW))
        self.assertEqual(rows(self.conn, "status_reply"), [])

    def test_one_reply_per_inbound_message(self):
        notify.notify_status_reply(self.conn, "919876543210", self.b, 7, now=NOW)
        self.assertIsNone(notify.notify_status_reply(self.conn, "919876543210", self.b, 7, now=NOW))
        self.assertIsNotNone(notify.notify_status_reply(self.conn, "919876543210", self.b, 9, now=NOW))

    def test_future_appointment_reply_is_provisional(self):
        f = book(self.conn, "F", TOMORROW, "09:00", phone="9333333333")
        rid = notify.notify_status_reply(self.conn, "919333333333", f, 5, now=NOW)
        body = self.conn.execute("SELECT body FROM notifications WHERE id=?", (rid,)).fetchone()["body"]
        self.assertIn("provisional", body)

    def test_in_consultation_reply(self):
        core.confirm(self.conn, core.propose(self.conn, "queue_call_next", {"appointment_id": self.b}), HANDLERS)
        rid = notify.notify_status_reply(self.conn, "919876543210", self.b, 7, now=NOW)
        body = self.conn.execute("SELECT body FROM notifications WHERE id=?", (rid,)).fetchone()["body"]
        self.assertIn("seeing you now", body)


class PostCommitSafetyTests(unittest.TestCase):
    """A notification problem must never change the outcome of a write."""

    def setUp(self):
        self.conn = make_db()
        inbound(self.conn, "919876543210", "hello")
        pid = core.propose(self.conn, "book_appointment", {
            "patient_name": "Sunita", "patient_phone": "9876543210", "appt_date": TODAY, "start_time": "09:00"})
        self.entity_type, self.appt = core.confirm(self.conn, pid, HANDLERS)

    def test_raising_sender_is_swallowed_and_recorded(self):
        sender = FakeSender(fail_with=RuntimeError("network down"))
        result = notify.post_commit(self.conn, "book_appointment", {"appt_date": TODAY}, self.appt, sender=sender, now=NOW)
        self.assertIsNone(result)
        self.assertEqual(rows(self.conn)[0]["status"], "failed")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0], 1)

    def test_raising_notifier_is_swallowed(self):
        with patch("clinic.notify.after_write", side_effect=ValueError("template bug")):
            notify.post_commit(self.conn, "book_appointment", {"appt_date": TODAY}, self.appt, sender=FakeSender(), now=NOW)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0], 1)

    def test_raising_flush_is_swallowed(self):
        with patch("clinic.notify.flush", side_effect=sqlite3.OperationalError("database is locked")):
            notify.post_commit(self.conn, "book_appointment", {"appt_date": TODAY}, self.appt, sender=FakeSender(), now=NOW)

    def test_post_commit_delivers_when_all_is_well(self):
        sender = FakeSender()
        notify.post_commit(self.conn, "book_appointment", {"appt_date": TODAY}, self.appt, sender=sender, now=NOW)
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(rows(self.conn)[0]["status"], "sent")

    def test_unrelated_intents_are_ignored(self):
        sender = FakeSender()
        notify.post_commit(self.conn, "log_expense", {}, 1, sender=sender, now=NOW)
        self.assertEqual(sender.calls, [])


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.today_appt = book(self.conn, "Today", TODAY, "11:00", phone="9000000001")
        self.tomorrow_appt = book(self.conn, "Tomorrow", TOMORROW, "09:00", phone="9000000002")
        self.conn.execute("DELETE FROM notifications")   # start from a clean outbox
        self.conn.commit()

    def at(self, day, hour, minute=0):
        local = datetime(2026, 10, day, hour, minute)
        return Now(local, local - timedelta(hours=5, minutes=30))

    def test_nothing_before_the_reminder_hours(self):
        self.assertEqual(notify.generate_reminders(self.conn, self.at(5, 7, 59)), 0)

    def test_morning_reminder_has_final_token_and_position(self):
        self.assertEqual(notify.generate_reminders(self.conn, self.at(5, 8)), 1)
        r = rows(self.conn, "reminder_morning")[0]
        self.assertEqual(r["appointment_id"], self.today_appt)
        self.assertIn("T-01", r["body"])
        self.assertIn("You are next", r["body"])
        self.assertEqual(self.conn.execute("SELECT last_notified_token FROM appointments WHERE id=?", (self.today_appt,)).fetchone()[0], 1)

    def test_nothing_for_tomorrow_before_the_evening_hour(self):
        notify.generate_reminders(self.conn, self.at(5, 17, 59))
        self.assertEqual(rows(self.conn, "reminder_day_before"), [])

    def test_day_before(self):
        notify.generate_reminders(self.conn, self.at(5, 18))
        events = {r["event"] for r in rows(self.conn)}
        self.assertIn("reminder_day_before", events)
        body = rows(self.conn, "reminder_day_before")[0]["body"]
        self.assertIn("provisional", body)
        self.assertIn("T-01", body)

    def test_repeated_ticks_never_duplicate(self):
        for minute in range(0, 10):
            notify.generate_reminders(self.conn, self.at(5, 18, minute))
        self.assertEqual(len(rows(self.conn, "reminder_day_before")), 1)
        for minute in range(0, 10):
            notify.generate_reminders(self.conn, self.at(6, 8, minute))
        self.assertEqual(len(rows(self.conn, "reminder_morning")), 1)

    def test_day_before_skipped_if_patient_was_just_told(self):
        fresh = book(self.conn, "Fresh", TOMORROW, "09:30", phone="9000000003", now=Now(
            datetime(2026, 10, 5, 17, 0), datetime(2026, 10, 5, 11, 30)))
        notify.generate_reminders(self.conn, self.at(5, 18))
        reminded = {r["appointment_id"] for r in rows(self.conn, "reminder_day_before")}
        self.assertIn(self.tomorrow_appt, reminded)
        self.assertNotIn(fresh, reminded)

    def test_morning_reminder_skips_arrived_finished_and_past_slots(self):
        arrived = book(self.conn, "Arrived", TODAY, "12:00", phone="9000000004")
        past = book(self.conn, "Past", TODAY, "09:00", phone="9000000005")
        core.confirm(self.conn, core.propose(self.conn, "queue_check_in", {"appointment_id": arrived}), HANDLERS)
        self.conn.execute("DELETE FROM notifications")
        self.conn.commit()
        notify.generate_reminders(self.conn, self.at(5, 10))
        reminded = {r["appointment_id"] for r in rows(self.conn, "reminder_morning")}
        self.assertEqual(reminded, {self.today_appt})

    def test_cancelled_appointments_get_no_reminder(self):
        core.confirm(self.conn, core.propose(self.conn, "cancel_appointment", {"appointment_id": self.tomorrow_appt}), HANDLERS)
        notify.generate_reminders(self.conn, self.at(5, 19))
        self.assertEqual(rows(self.conn, "reminder_day_before"), [])

    def test_no_phone_creates_no_reminder_noise(self):
        book_legacy_without_phone(self.conn, "NoPhone", TODAY, "12:00")
        self.conn.execute("DELETE FROM notifications")
        self.conn.commit()
        notify.generate_reminders(self.conn, self.at(5, 9))
        self.assertEqual([r for r in rows(self.conn) if r["status"] == "skipped_no_phone"], [])


if __name__ == "__main__":
    unittest.main()
