import sqlite3
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import core, token_queue
from clinic.adapters.local_sqlite import LocalSQLiteAdapter
from clinic.intents import HANDLERS
from clinic.nlu import extract
from clinic.nlu.classify import classify
from clinic.nlu.intent_llm import KNOWN_INTENTS
from clinic.nlu.parser import parse
from clinic.pipeline import ParsedResult, PipelineError, ReadResult, transcript_to_response

SCHEMA = (Path(__file__).resolve().parents[1] / "clinic" / "schema.sql").read_text()
TODAY = date.today().isoformat()
QUEUE_WRITES = ("queue_check_in", "queue_call_next", "queue_mark_done", "queue_mark_no_show")


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


class ExtractTokenTests(unittest.TestCase):
    def test_forms(self):
        cases = {
            "token 5 ko bulao": 5, "Token number 12 aa gaya": 12, "टोकन 3 को अंदर भेजो": 3,
            "टोकन नंबर पांच": 5, "token paanch done": 5, "T-07 no show": 7,
            "5 number token aa gaya": 5, "token five": 5, "Mark token 4 as done.": 4,
            "token number is 9": 9, "टोकन तीन": 3,
        }
        for text, expected in cases.items():
            self.assertEqual(extract.extract_token_number(text), expected, text)

    def test_no_token(self):
        for text in ("hello", "token is great", "Sunita aa gayi", "5 baje aana"):
            self.assertIsNone(extract.extract_token_number(text), text)


class QueueClassifyTests(unittest.TestCase):
    def check(self, expected, *texts):
        for text in texts:
            self.assertEqual(classify(text), expected, text)

    def test_check_in(self):
        self.check("queue_check_in",
                   "Token 5 aa gaya", "Sunita checked in", "check in token 3", "Ramesh aa gayi",
                   "टोकन 3 आ गया", "सुनीता चेक इन", "token 5 present", "patient has arrived", "Sunita pahunch gayi")

    def test_call_next(self):
        self.check("queue_call_next",
                   "Call next patient", "next patient please", "अगला मरीज बुलाओ", "agla patient bhejo",
                   "token 5 ko bulao", "टोकन 3 को अंदर भेजो", "Sunita ko andar bulao", "call token 4")

    def test_mark_done(self):
        self.check("queue_mark_done",
                   "Token 4 done", "mark done token 4", "consultation ho gaya for Sunita", "Sunita consultation done",
                   "टोकन 2 हो गया", "टोकन 2 का कंसल्टेशन हो गया", "token 6 finished", "Ramesh ko dikha liya")

    def test_mark_no_show(self):
        self.check("queue_mark_no_show",
                   "Token 5 no show", "token 5 nahi aaya", "no-show Sunita", "टोकन 5 नहीं आया", "T-03 absent", "token 7 gayab")

    def test_status(self):
        self.check("queue_status",
                   "Queue mein kaun hai?", "Who's next?", "who is next", "whos next", "अगला कौन है",
                   "queue status", "kaun next hai", "how many patients are waiting", "कतार में कितने हैं")

    def test_who_is_the_next_patient_is_a_question_not_a_call(self):
        self.check("queue_status", "who is the next patient")

    # -- collisions with the older rules: each of these must be unchanged --

    def test_attendance_present_is_still_attendance(self):
        self.check("log_attendance", "Ramesh present hai", "प्रेजेंट है रमेश", "Anita half day", "Sunita absent hai")

    def test_nahi_aaya_without_a_token_is_still_missed_followups(self):
        self.check("missed_followups", "aaj kaun nahi aaya", "Sunita nahi aaya", "आज कौन नहीं आया")

    def test_bulao_is_still_set_followup(self):
        self.check("set_followup", "Sunita ko 7 din baad bulao", "सुनीता को 7 दिन बाद बुलाओ", "Sunita ko follow up pe bulao")

    def test_visit_and_fee_commands_are_still_record_visit(self):
        self.check("record_visit",
                   "Sunita ji ka consultation 300 rupees",
                   "Sunita consultation done fee 300 rupees",
                   "Sunita ka visit 300 rupees done",
                   "consultation ho gaya Sunita fees ₹500")

    def test_next_appointment_is_still_next_appointment(self):
        self.check("next_appointment", "next appointment for Sunita", "Sunita ki agli appointment kab hai", "agla appointment Ramesh ka")

    def test_appointment_commands_are_untouched(self):
        self.check("cancel_appointment", "cancel Sunita's appointment")
        self.check("reschedule_appointment", "reschedule Sunita's appointment to kal 5 baje")
        self.check("book_appointment", "book appointment for Sunita kal 11 baje")
        self.check("check_availability", "kal 5 baje slot free hai")
        self.check("list_appointments", "today's schedule")

    def test_followup_cancel_and_reschedule_are_untouched(self):
        self.check("cancel_followup", "Sunita ka follow up cancel karo")
        self.check("reschedule_followup", "reschedule the follow-up to next week")

    def test_negated_arrival_is_not_a_check_in(self):
        self.assertNotEqual(classify("token 5 abhi tak nahi aaya"), "queue_check_in")
        self.assertNotEqual(classify("Sunita has not arrived"), "queue_check_in")
        self.assertNotEqual(classify("Sunita abhi nahi aa gayi"), "queue_check_in")

    def test_staff_arrival_is_not_a_patient_check_in(self):
        self.assertNotEqual(classify("staff Ramesh aa gaya"), "queue_check_in")

    def test_word_boundaries(self):
        self.assertNotEqual(classify("token 5 recover"), "queue_mark_done")  # 'over' inside 'recover'

    def test_unrelated_text_is_not_a_queue_command(self):
        for text in ("random unrelated sentence about the weather", "Sunita ka phone number kya hai", "aaj ka hisaab"):
            self.assertNotIn(classify(text), QUEUE_WRITES + ("queue_status",), text)

    def test_all_queue_intents_are_known_to_the_stage2_picker(self):
        for intent in QUEUE_WRITES + ("queue_status",):
            self.assertIn(intent, KNOWN_INTENTS)


class QueueParseTests(unittest.TestCase):
    def test_token_command_never_calls_the_name_extractor(self):
        with patch("clinic.nlu.parser.extract_name", side_effect=AssertionError("LLM must not run")):
            self.assertEqual(parse("Token 5 aa gaya"), ("queue_check_in", {"token": 5, "patient_name": None}))
            self.assertEqual(parse("call next patient"), ("queue_call_next", {"token": None, "patient_name": None}))
            self.assertEqual(parse("queue mein kaun hai"), ("queue_status", {}))

    @patch("clinic.nlu.parser.extract_name", return_value="Sunita")
    def test_name_is_used_when_there_is_no_token(self, _mock):
        self.assertEqual(parse("Sunita aa gayi"), ("queue_check_in", {"token": None, "patient_name": "Sunita"}))
        self.assertEqual(parse("Sunita ka consultation done"), ("queue_mark_done", {"token": None, "patient_name": "Sunita"}))


class QueuePipelineTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.adapter = LocalSQLiteAdapter()
        pid = core.confirm(self.conn, core.propose(self.conn, "register_patient", {"name": "Sunita Devi", "phone": "9876543210"}), HANDLERS)[1]
        self.sunita = pid
        self.a = self.book(patient_id=pid, start="09:00")
        self.b = self.book(patient_name="Walk In", patient_phone="9111111111", start="09:15")
        self.c = self.book(patient_name="Ramesh Gupta", patient_phone="9222222222", start="09:30")

    def book(self, start, **who):
        slots = dict(who, appt_date=TODAY, start_time=start, duration_minutes=15)  # fixtures lay appointments out 15 minutes apart
        return core.confirm(self.conn, core.propose(self.conn, "book_appointment", slots), HANDLERS)[1]

    def run_cmd(self, text):
        return transcript_to_response(self.conn, text, self.adapter, self.adapter, "en-IN", defer_intents=frozenset(QUEUE_WRITES))

    def act(self, intent, appointment_id):
        core.confirm(self.conn, core.propose(self.conn, intent, {"appointment_id": appointment_id}), HANDLERS)

    def test_token_resolves_to_todays_appointment(self):
        result = self.run_cmd("Token 2 aa gaya")
        self.assertIsInstance(result, ParsedResult)
        self.assertEqual(result.intent, "queue_check_in")
        self.assertEqual(result.slots, {"appointment_id": self.b})
        self.assertEqual([o["id"] for o in result.resolved["appointments"]], [self.a, self.b, self.c])
        self.assertEqual(result.resolved["appointments"][1]["label"], "T-02 Walk In 09:15")
        self.assertNotIn("note", result.resolved)

    def test_unknown_token_leaves_the_dropdown_blank_with_a_note(self):
        result = self.run_cmd("Token 9 aa gaya")
        self.assertIsNone(result.slots["appointment_id"])
        self.assertIn("Token 9", result.resolved["note"])

    def test_finished_token_is_not_offered_or_resolved(self):
        self.act("queue_mark_done", self.a)
        result = self.run_cmd("Token 1 aa gaya")
        self.assertIsNone(result.slots["appointment_id"])
        self.assertNotIn(self.a, [o["id"] for o in result.resolved["appointments"]])

    def test_call_next_picks_the_lowest_token_who_has_checked_in(self):
        self.act("queue_check_in", self.c)
        self.act("queue_check_in", self.b)
        self.assertEqual(self.run_cmd("call next patient").slots["appointment_id"], self.b)

    def test_call_next_with_nobody_checked_in_is_blank(self):
        result = self.run_cmd("call next patient")
        self.assertIsNone(result.slots["appointment_id"])
        self.assertIn("checked in", result.resolved["note"])

    @patch("clinic.nlu.parser.extract_name", return_value="Sunita")
    def test_registered_patient_name_resolves(self, _mock):
        self.assertEqual(self.run_cmd("Sunita aa gayi").slots["appointment_id"], self.a)

    @patch("clinic.nlu.parser.extract_name", return_value="Ramesh Gupta")
    def test_walk_in_name_resolves_by_close_match(self, _mock):
        self.assertEqual(self.run_cmd("Ramesh Gupta aa gaya").slots["appointment_id"], self.c)

    @patch("clinic.nlu.parser.extract_name", return_value="Zebra Unknown")
    def test_unknown_name_is_blank(self, _mock):
        result = self.run_cmd("Zebra Unknown aa gaya")
        self.assertIsNone(result.slots["appointment_id"])
        self.assertIn("Zebra Unknown", result.resolved["note"])

    def test_nothing_is_written_by_the_voice_path(self):
        before = self.conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0]
        self.run_cmd("Token 2 aa gaya")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0], before)
        self.assertIsNone(self.conn.execute("SELECT queue_state FROM appointments WHERE id=?", (self.b,)).fetchone()[0])

    def test_queue_status_is_a_read_with_a_fixed_template_answer(self):
        self.act("queue_check_in", self.a)
        self.act("queue_call_next", self.a)
        self.act("queue_check_in", self.b)
        result = transcript_to_response(self.conn, "Queue mein kaun hai?", self.adapter, self.adapter, "en-IN")
        self.assertIsInstance(result, ReadResult)
        self.assertEqual(result.data["now_serving"], "T-01 Sunita Devi")
        self.assertEqual(result.data["next_up"], "T-02 Walk In")
        self.assertEqual(result.data["waiting"], 2)
        self.assertIn("Now with the doctor: T-01 Sunita Devi.", result.answer_text)
        self.assertIn("Next: T-02 Walk In.", result.answer_text)
        self.assertIn("2 waiting.", result.answer_text)

    def test_queue_status_hindi_and_empty_day(self):
        result = transcript_to_response(self.conn, "Who's next?", self.adapter, self.adapter, "hi-IN")
        self.assertIn("intezaar mein hain", result.answer_text)
        empty = make_db()
        result = transcript_to_response(empty, "Who's next?", self.adapter, self.adapter, "en-IN")
        self.assertIn("No appointments today", result.answer_text)

    def test_approving_the_card_goes_through_the_normal_write_path(self):
        result = self.run_cmd("Token 2 aa gaya")
        pid = core.propose(self.conn, result.intent, {"appointment_id": result.slots["appointment_id"]})
        core.confirm(self.conn, pid, HANDLERS)
        self.assertEqual(token_queue.queue_entry(self.conn, self.b)["queue_state"], "checked_in")


if __name__ == "__main__":
    unittest.main()
