import sqlite3
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import core
from clinic.adapters.local_sqlite import LocalSQLiteAdapter
from clinic.intents import HANDLERS
from clinic.whatsapp_pipeline import MAX_OPEN_BOOKING_REQUESTS, classify_text_message, sender_appointments, suggest_slot

SCHEMA = (Path(__file__).resolve().parents[1] / "clinic" / "schema.sql").read_text()
NOW = datetime(2026, 10, 5, 10, 0)   # Monday 10:00
TODAY, TOMORROW = "2026-10-05", "2026-10-06"
WA = "919876543210"


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


class FlowCase(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.adapter = LocalSQLiteAdapter()

    def classify(self, text, wa_id=WA):
        return classify_text_message(self.conn, wa_id, text, self.adapter, now=NOW)

    def register(self, name="Sunita Devi", phone="9876543210"):
        return core.confirm(self.conn, core.propose(self.conn, "register_patient", {"name": name, "phone": phone}), HANDLERS)[1]

    def book(self, appt_date, start, **who):
        slots = dict(who, appt_date=appt_date, start_time=start)
        return core.confirm(self.conn, core.propose(self.conn, "book_appointment", slots), HANDLERS)[1]

    def followup(self, patient_id, due="2026-10-09"):
        return core.confirm(self.conn, core.propose(self.conn, "set_followup", {"patient_id": patient_id, "due_date": due}), HANDLERS)[1]


@patch("clinic.whatsapp_pipeline.extract_name", return_value="Neeta Sharma")
class BookingRequestTests(FlowCase):
    def test_stated_free_date_and_time_are_used_as_stated(self, _n):
        r = self.classify("mujhe kal 11 baje appointment chahiye")
        self.assertEqual(r["intent"], "book_appointment")
        self.assertEqual((r["slots"]["appt_date"], r["slots"]["start_time"]), (TOMORROW, "11:00"))
        self.assertNotIn("suggested", r["slots"])
        self.assertNotIn("suggestion_note", r["slots"])

    def test_date_without_time_gets_earliest_free_slot_flagged_as_suggestion(self, _n):
        self.book(TOMORROW, "09:00", patient_name="X", patient_phone="9000000001")
        r = self.classify("kal appointment chahiye")
        self.assertEqual((r["slots"]["appt_date"], r["slots"]["start_time"]), (TOMORROW, "09:30"))   # 09:00-09:30 is taken
        self.assertEqual(r["slots"]["suggested"], ["start_time"])
        self.assertIn("patient didn't specify a time", r["slots"]["suggestion_note"])

    def test_no_date_gets_next_date_with_a_free_slot_after_now(self, _n):
        r = self.classify("I want to book an appointment")
        self.assertEqual((r["slots"]["appt_date"], r["slots"]["start_time"]), (TODAY, "10:30"))  # not 09:00 or 10:00: those have passed
        self.assertEqual(r["slots"]["suggested"], ["appt_date", "start_time"])
        self.assertIn("patient didn't specify a date or time", r["slots"]["suggestion_note"])

    def test_no_date_skips_a_fully_booked_day(self, _n):
        # Fill every remaining 30-minute slot today (10:00 and earlier have already passed).
        for minute in list(range(10 * 60 + 30, 13 * 60, 30)) + list(range(16 * 60, 20 * 60, 30)):
            self.book(TODAY, "{:02d}:{:02d}".format(minute // 60, minute % 60), patient_name="X", patient_phone="9000000001")
        r = self.classify("need an appointment")
        self.assertEqual((r["slots"]["appt_date"], r["slots"]["start_time"]), (TOMORROW, "09:00"))

    def test_time_without_date_gets_the_next_date_with_that_time_free(self, _n):
        r = self.classify("मुझे शाम 5 बजे अपॉइंटमेंट चाहिए")
        self.assertEqual((r["slots"]["appt_date"], r["slots"]["start_time"]), (TODAY, "17:00"))
        self.assertEqual(r["slots"]["suggested"], ["appt_date"])

    def test_taken_requested_time_falls_back_to_earliest_free_that_day(self, _n):
        self.book(TOMORROW, "11:00", patient_name="X", patient_phone="9000000001")
        r = self.classify("mujhe kal 11 baje appointment chahiye")
        self.assertEqual((r["slots"]["appt_date"], r["slots"]["start_time"]), (TOMORROW, "09:00"))
        self.assertEqual(r["slots"]["suggested"], ["start_time"])
        self.assertIn("11:00", r["slots"]["suggestion_note"])

    def test_registered_sender_is_resolved_by_phone(self, _n):
        pid = self.register()
        r = self.classify("appointment chahiye")
        self.assertEqual(r["patient_id"], pid)
        self.assertEqual(r["slots"]["patient_id"], pid)
        self.assertIsNone(r["slots"]["patient_name"])

    def test_unregistered_sender_uses_the_unregistered_caller_fields(self, _n):
        r = self.classify("appointment chahiye", wa_id="919123456780")
        self.assertIsNone(r["patient_id"])
        self.assertIsNone(r["slots"]["patient_id"])
        self.assertEqual(r["slots"]["patient_name"], "Neeta Sharma")
        self.assertEqual(r["slots"]["patient_phone"], "9123456780")

    def test_name_extractor_outage_does_not_break_classification(self, mock_name):
        mock_name.side_effect = RuntimeError("ollama down")
        r = self.classify("appointment chahiye", wa_id="919123456780")
        self.assertEqual(r["intent"], "book_appointment")
        self.assertIsNone(r["slots"]["patient_name"])

    def test_sixth_open_request_goes_to_a_human(self, _n):
        for i in range(MAX_OPEN_BOOKING_REQUESTS):
            self.conn.execute(
                "INSERT INTO wa_messages (wa_message_id, wa_id, message_type, raw_text, intent, status) "
                "VALUES (?, ?, 'text', 'appointment chahiye', 'book_appointment', 'classified')", ("w%d" % i, WA))
        self.conn.commit()
        r = self.classify("appointment chahiye")
        self.assertIsNone(r["intent"])
        # a different number is not affected, nor are resolved items
        self.assertEqual(self.classify("appointment chahiye", wa_id="919111111111")["intent"], "book_appointment")
        self.conn.execute("UPDATE wa_messages SET status = 'approved' WHERE wa_message_id = 'w0'")
        self.conn.commit()
        self.assertEqual(self.classify("appointment chahiye")["intent"], "book_appointment")

    def test_the_double_booking_guard_still_applies_at_approve_time(self, _n):
        r = self.classify("kal 11 baje appointment chahiye")
        self.book(TOMORROW, "11:00", patient_name="Someone", patient_phone="9000000001")  # taken after the card was made
        slots = {k: v for k, v in r["slots"].items() if k not in ("suggested", "suggestion_note")}
        pid = core.propose(self.conn, "book_appointment", slots)
        with self.assertRaises(Exception):
            core.confirm(self.conn, pid, HANDLERS)

    def test_suggest_slot_ignores_a_past_date(self, _n):
        date_, time_, suggested, _note = suggest_slot(self.conn, "2026-10-01", None, NOW)
        self.assertEqual((date_, time_), (TODAY, "10:30"))
        self.assertEqual(suggested, ["appt_date", "start_time"])


class CancelRescheduleTests(FlowCase):
    def test_cancel_with_an_upcoming_appointment_proposes_cancel_appointment_for_the_nearest(self):
        pid = self.register()
        later = self.book(TOMORROW, "11:00", patient_id=pid)
        nearest = self.book(TODAY, "16:00", patient_id=pid)
        r = self.classify("cancel karo")
        self.assertEqual(r["intent"], "cancel_appointment")
        self.assertEqual(r["slots"], {"appointment_id": nearest})
        self.assertNotEqual(nearest, later)

    def test_appointment_is_preferred_when_both_an_appointment_and_a_followup_exist(self):
        pid = self.register()
        appt = self.book(TOMORROW, "11:00", patient_id=pid)
        self.followup(pid)
        self.assertEqual(self.classify("I can't come, please cancel")["slots"], {"appointment_id": appt})

    def test_explicit_followup_mention_still_targets_the_followup(self):
        pid = self.register()
        self.book(TOMORROW, "11:00", patient_id=pid)
        fid = self.followup(pid)
        r = self.classify("please cancel my follow up")
        self.assertEqual(r["intent"], "cancel_followup")
        self.assertEqual(r["slots"]["followup_id"], fid)

    def test_followup_behaviour_applies_when_there_is_no_appointment(self):
        pid = self.register()
        fid = self.followup(pid)
        r = self.classify("cancel karo")
        self.assertEqual((r["intent"], r["slots"]["followup_id"]), ("cancel_followup", fid))

    def test_nothing_to_cancel_is_left_to_a_human(self):
        self.register()
        self.assertIsNone(self.classify("cancel karo")["intent"])
        self.assertIsNone(self.classify("cancel karo", wa_id="919111111111")["intent"])

    def test_a_cancelled_appointment_is_not_a_target(self):
        pid = self.register()
        appt = self.book(TOMORROW, "11:00", patient_id=pid)
        core.confirm(self.conn, core.propose(self.conn, "cancel_appointment", {"appointment_id": appt}), HANDLERS)
        self.assertIsNone(self.classify("cancel karo")["intent"])

    def test_unregistered_sender_can_cancel_an_appointment_booked_under_their_phone(self):
        # Bookings made before name + phone registered people (and walk-ins entered by hand) have no patient row.
        appt = self.conn.execute(
            "INSERT INTO appointments (patient_name, patient_phone, appt_date, start_time, status) VALUES (?, ?, ?, ?, 'booked')",
            ("Walk In", "9876543210", TOMORROW, "11:00")).lastrowid
        self.conn.commit()
        r = self.classify("cancel karo")
        self.assertEqual((r["patient_id"], r["intent"], r["slots"]), (None, "cancel_appointment", {"appointment_id": appt}))

    def test_someone_elses_appointment_is_never_targeted(self):
        self.book(TOMORROW, "11:00", patient_name="Other", patient_phone="9000000001")
        self.assertIsNone(self.classify("cancel karo")["intent"])

    def test_reschedule_extracts_date_and_time(self):
        pid = self.register()
        appt = self.book(TOMORROW, "11:00", patient_id=pid)
        r = self.classify("reschedule kal shaam 4 baje")
        self.assertEqual(r["intent"], "reschedule_appointment")
        self.assertEqual(r["slots"], {"appointment_id": appt, "appt_date": TOMORROW, "start_time": "16:00"})

    def test_reschedule_leaves_unstated_fields_blank_for_staff(self):
        pid = self.register()
        self.book(TOMORROW, "11:00", patient_id=pid)
        r = self.classify("can we reschedule")
        self.assertIsNone(r["slots"]["appt_date"])
        self.assertIsNone(r["slots"]["start_time"])

    def test_confirm_followup_is_unchanged(self):
        pid = self.register()
        fid = self.followup(pid)
        r = self.classify("haan main aaunga")
        self.assertEqual((r["intent"], r["slots"]["followup_id"]), ("confirm_followup", fid))


class StatusQuestionTests(FlowCase):
    def test_status_question_matches_the_senders_own_appointment(self):
        pid = self.register()
        appt = self.book(TODAY, "16:00", patient_id=pid)
        for text in ("what is my token", "mera number kab aayega", "kitne log baaki hain", "मेरा टोकन क्या है"):
            r = self.classify(text)
            self.assertEqual((r["intent"], r["slots"]), ("my_status", {"appointment_id": appt}), text)

    def test_matches_by_phone_for_an_unregistered_booker(self):
        appt = self.book(TODAY, "16:00", patient_name="Walk In", patient_phone="9876543210")
        self.assertEqual(self.classify("my token")["slots"], {"appointment_id": appt})

    def test_no_matching_appointment_falls_through_to_a_human(self):
        self.assertIsNone(self.classify("my token")["intent"])
        self.book(TODAY, "16:00", patient_name="Other", patient_phone="9000000001")
        self.assertIsNone(self.classify("my token")["intent"])

    def test_finished_and_cancelled_appointments_do_not_match(self):
        pid = self.register()
        done = self.book(TODAY, "16:00", patient_id=pid)
        core.confirm(self.conn, core.propose(self.conn, "queue_mark_done", {"appointment_id": done}), HANDLERS)
        self.assertIsNone(self.classify("my token")["intent"])

    def test_nearest_upcoming_is_chosen(self):
        pid = self.register()
        self.book(TOMORROW, "09:00", patient_id=pid)
        today_appt = self.book(TODAY, "18:00", patient_id=pid)
        self.assertEqual(self.classify("my token")["slots"], {"appointment_id": today_appt})

    def test_sender_appointments_lists_only_the_senders(self):
        pid = self.register()
        mine = self.book(TOMORROW, "09:00", patient_id=pid)
        self.book(TOMORROW, "09:30", patient_name="Other", patient_phone="9000000001")
        self.assertEqual([a["id"] for a in sender_appointments(self.conn, WA, pid, NOW)], [mine])


if __name__ == "__main__":
    unittest.main()
