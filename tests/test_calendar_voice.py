"""Voice navigation: "open the calendar" (classification, view extraction, the
pipeline result and the realtime event) -- read-only, never a database write --
plus regression checks that it does not steal any existing intent."""
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic.adapters.local_sqlite import LocalSQLiteAdapter
from clinic.nlu.answer import compose_navigation
from clinic.nlu.classify import calendar_mode, classify
from clinic.nlu.intent_llm import KNOWN_INTENTS
from clinic.nlu.parser import parse
from clinic.pipeline import NavigateResult, transcript_to_response

SCHEMA = (Path(__file__).resolve().parents[1] / "clinic" / "schema.sql").read_text()


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


class ClassifyTests(unittest.TestCase):
    def test_calendar_commands_in_english_devanagari_and_hinglish(self):
        phrases = [
            # English
            "open calendar", "Open the calendar", "show calendar", "show me the calendar", "calendar please",
            "go to the calendar", "show month view", "show me the week view", "agenda view", "day view",
            "switch to month view", "show tomorrow's calendar", "open the calender",
            # Hinglish
            "calendar dikhao", "calendar kholo", "calendar dikha do", "kal ka calendar", "aaj ka calendar",
            "mahine ka calendar", "hafte ka calendar dikhao", "week view dikhao", "month view dikhao",
            "calendar mein dekho", "appointments calendar dikhao",
            # Devanagari
            "कैलेंडर खोलो", "कैलेंडर दिखाओ", "कल का कैलेंडर", "आज का कैलेंडर", "महीने का कैलेंडर दिखाओ",
            "मंथ व्यू दिखाओ", "वीक व्यू", "एजेंडा व्यू", "हफ्ते का कैलेंडर", "कैलेंडर",
        ]
        for phrase in phrases:
            self.assertEqual(classify(phrase), "open_calendar", phrase)

    def test_view_extraction(self):
        cases = {
            "open calendar": None, "कैलेंडर खोलो": None, "calendar dikhao": None,
            "show month view": "month", "monthly calendar": "month", "mahine ka calendar": "month",
            "महीने का कैलेंडर दिखाओ": "month", "मंथ व्यू": "month",
            "show me the week view": "week", "weekly calendar": "week", "hafte ka calendar dikhao": "week",
            "हफ्ते का कैलेंडर": "week", "वीक व्यू": "week",
            "agenda view": "agenda", "calendar list view": "agenda", "एजेंडा व्यू": "agenda",
            # "day" has no view of its own in Google's embed: it maps to agenda
            "day view": "agenda", "kal ka calendar": "agenda", "aaj ka calendar": "agenda",
            "show tomorrow's calendar": "agenda", "कल का कैलेंडर": "agenda", "आज का कैलेंडर": "agenda",
        }
        for phrase, expected in cases.items():
            self.assertEqual(calendar_mode(phrase), expected, phrase)

    def test_unrelated_text_is_unchanged(self):
        # No calendar / view words: navigation can never fire, so these are exactly the legacy answers.
        cases = {
            "register a new patient Sunita": "register_patient",
            "Sunita ka follow up cancel karo": "cancel_followup",
            "Sunita ko 7 din baad bulao": "set_followup",
            "aaj ka hisaab": "day_end_cashbook",
            "kaun nahi aaya": "missed_followups",
            "Sunita ka phone number kya hai": "patient_lookup",
            "token 5 aa gaya": "queue_check_in",
            "who's next": "queue_status",
            "queue status": "queue_status",
            "today's schedule": "list_appointments",
            "this week's schedule": "list_appointments",
            "is hafte ka schedule": "list_appointments",
            "free slots tomorrow": "check_availability",
            "kal koi slot khaali hai": "check_availability",
            "when is my next appointment": "next_appointment",
            "book an appointment for Ravi tomorrow at 5 pm": "book_appointment",
            "cancel Sunita's appointment": "cancel_appointment",
            "reschedule my appointment to Monday 10 am": "reschedule_appointment",
            "random unrelated sentence about the weather": None,
        }
        for phrase, expected in cases.items():
            self.assertEqual(classify(phrase), expected, phrase)

    def test_collisions_with_existing_intents_keep_the_existing_intent(self):
        cases = {
            # writes that merely mention the calendar
            "book an appointment in the calendar for tomorrow 5 pm": "book_appointment",
            "calendar mein Sunita ka appointment daalo": "book_appointment",
            "appointment book karo calendar mein": "book_appointment",
            "कैलेंडर में अपॉइंटमेंट बुक करो": "book_appointment",
            "add an appointment to the calendar": "book_appointment",
            "cancel my appointment on the calendar": "cancel_appointment",
            "calendar se Sunita ka appointment cancel karo": "cancel_appointment",
            "कैलेंडर से अपॉइंटमेंट कैंसिल करो": "cancel_appointment",
            "reschedule the appointment on the calendar to 10 am": "reschedule_appointment",
            "Sunita ka follow up cancel karo calendar se": "cancel_followup",
            "calendar ke liye 200 rupaye kharch hue": "log_expense",
            # reads against our own database stay with the existing read intents
            "how many appointments on the calendar tomorrow": "book_appointment",   # legacy verdict, unchanged
            "kitne appointments hain calendar mein": "book_appointment",             # legacy verdict, unchanged
            "who is on the calendar": None,                                          # legacy verdict, unchanged
            "today's schedule on the calendar": "list_appointments",
            "this week's schedule calendar": "list_appointments",
            "view this week's schedule": "list_appointments",
            "आज का शेड्यूल कैलेंडर": "list_appointments",
            "free slots in the calendar tomorrow": "check_availability",
            "calendar mein kal koi slot khaali hai": "check_availability",
            "when is the next appointment on the calendar": "next_appointment",
            "who's next on the calendar": "queue_status",
            "queue calendar": "queue_status",
            "token 5 done calendar": "queue_mark_done",
        }
        for phrase, expected in cases.items():
            self.assertEqual(classify(phrase), expected, phrase)
            self.assertNotEqual(classify(phrase), "open_calendar", phrase)

    def test_the_bare_appointment_noun_needs_a_display_verb_to_navigate(self):
        # Legacy says book_appointment from the noun alone; only an explicit
        # "show / open / dikhao" turns it into navigation.
        self.assertEqual(classify("appointment calendar"), "book_appointment")
        self.assertEqual(classify("show appointments on the calendar"), "open_calendar")
        self.assertEqual(classify("calendar mein appointments dikhao"), "open_calendar")
        self.assertEqual(classify("कैलेंडर में अपॉइंटमेंट दिखाओ"), "open_calendar")

    def test_calendar_is_a_known_stage2_label(self):
        self.assertIn("open_calendar", KNOWN_INTENTS)


class ParseTests(unittest.TestCase):
    def test_parse_returns_the_view_and_uses_no_name_llm(self):
        with patch("clinic.nlu.parser.extract_name", side_effect=AssertionError("LLM must not run")), \
                patch("clinic.nlu.parser.pick_intent", return_value=None):
            self.assertEqual(parse("show month view"), ("open_calendar", {"mode": "month"}))
            self.assertEqual(parse("calendar dikhao"), ("open_calendar", {"mode": None}))
            self.assertEqual(parse("कैलेंडर खोलो"), ("open_calendar", {"mode": None}))
            self.assertEqual(parse("kal ka calendar"), ("open_calendar", {"mode": "agenda"}))

    def test_stage2_may_pick_the_label_and_parse_still_works(self):
        with patch("clinic.nlu.parser.pick_intent", return_value="open_calendar"):
            self.assertEqual(parse("could you pull up the schedule picture thing for the doctor"),
                             ("open_calendar", {"mode": None}))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.adapter = LocalSQLiteAdapter()

    def respond(self, text, language="en-IN"):
        every_write = frozenset((
            "register_patient", "register_staff", "record_visit", "set_followup", "log_attendance", "log_expense",
            "cancel_followup", "reschedule_followup", "book_appointment", "cancel_appointment",
            "reschedule_appointment", "queue_check_in", "queue_call_next", "queue_mark_done", "queue_mark_no_show"))
        return transcript_to_response(self.conn, text, self.adapter, self.adapter, language, defer_intents=every_write)

    def table_counts(self):
        return {t: self.conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0]
                for t in ("patients", "appointments", "proposals", "audit_log", "calendar_sync_queue", "notifications")}

    def test_navigation_result_writes_nothing_and_needs_no_review_card(self):
        before = self.table_counts()
        result = self.respond("show month view")
        self.assertIsInstance(result, NavigateResult)
        self.assertEqual((result.intent, result.tab, result.mode), ("open_calendar", "appointments", "month"))
        self.assertEqual(result.answer_text, "Opening the Appointments calendar (Month view).")
        self.assertEqual(self.table_counts(), before)
        self.assertEqual(sum(before.values()), 0)

    def test_hindi_acknowledgement_and_no_mode(self):
        result = self.respond("कैलेंडर खोलो", language="hi-IN")
        self.assertEqual((result.tab, result.mode), ("appointments", None))
        self.assertEqual(result.answer_text, "Appointments calendar khol raha hoon.")

    def test_acknowledgement_templates(self):
        self.assertEqual(compose_navigation("open_calendar", "week", "en-IN"), "Opening the Appointments calendar (Week view).")
        self.assertEqual(compose_navigation("open_calendar", "agenda", "hi-IN"),
                         "Appointments calendar khol raha hoon (Agenda view).")
        self.assertEqual(compose_navigation("open_calendar", None, "en-IN"), "Opening the Appointments calendar.")

    def test_counting_questions_still_use_our_database_not_google(self):
        # "how many appointments tomorrow" is not navigation; whatever it
        # resolves to is the existing pipeline (a review card or a DB read).
        result = self.respond("how many appointments on the calendar tomorrow")
        self.assertNotIsInstance(result, NavigateResult)


class RealtimeEventTests(unittest.TestCase):
    def test_voice_session_emits_navigate_and_touches_nothing(self):
        os.environ.setdefault("SARVAM_API_KEY", "test-not-real")
        from clinic.realtime_voice import VoiceSession
        conn = make_db()
        self.addCleanup(conn.close)
        adapter = LocalSQLiteAdapter()
        emitted = []
        session = VoiceSession("sid", "key", lambda event, data: emitted.append((event, data)),
                               lambda: conn, adapter, adapter, frozenset(("book_appointment",)))
        session._handle_final_transcript("kal ka calendar")
        # every turn also tells the page what the assistant is "talking about"
        emitted = [e for e in emitted if e[0] != "context_update"]
        self.assertEqual(emitted, [("navigate", {
            "transcript": "kal ka calendar", "tab": "appointments", "mode": "agenda",
            # a session's language hint starts as hi-IN until a transcript reports one
            "answer_text": "Appointments calendar khol raha hoon (Agenda view).",
        })])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0], 0)


class FrontEndWiringTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_appointments_tab_sits_right_after_queue(self):
        source = (self.ROOT / "static" / "nav.js").read_text()
        ids = [line.split('id: "')[1].split('"')[0] for line in source.splitlines() if 'id: "' in line]
        self.assertEqual(ids[ids.index("queue") + 1], "appointments")
        self.assertIn('label: "Appointments"', source)

    def test_voice_script_handles_the_navigate_event(self):
        source = (self.ROOT / "static" / "live_voice.js").read_text()
        self.assertIn('socket.on("navigate"', source)
        self.assertIn("CalendarTab.show", source)


if __name__ == "__main__":
    unittest.main()
