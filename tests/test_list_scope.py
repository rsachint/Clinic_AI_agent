import sqlite3
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic.adapters.local_sqlite import LocalSQLiteAdapter
from clinic.nlu.answer import compose_answer
from clinic.pipeline import PipelineError, transcript_to_response


class _Cite:
    source, as_of = "local clinic records", "updated just now"


class ListAnswerTextTests(unittest.TestCase):
    def test_the_day_is_named_in_english_and_hindi(self):
        self.assertEqual(compose_answer("list_appointments", [], _Cite, "en-IN", scope="Wed 7 Oct"),
                         "No appointments on Wed 7 Oct. Source: local clinic records, updated just now.")
        self.assertIn("Wed 7 Oct ko koi appointment nahi hai.", compose_answer("list_appointments", [], _Cite, "hi-IN", scope="Wed 7 Oct"))
        self.assertIn("2 appointment(s) on Wed 7 Oct.", compose_answer("list_appointments", [1, 2], _Cite, "en-IN", scope="Wed 7 Oct"))

    def test_without_a_scope_the_old_text_is_kept(self):
        self.assertIn("1 appointment(s) scheduled.", compose_answer("list_appointments", [1], _Cite, "en-IN"))


class ListPipelineTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(Path("clinic/schema.sql").read_text())
        self.addCleanup(self.conn.close)
        self.adapter = LocalSQLiteAdapter()
        today = date.today()
        self.day7 = today.replace(day=7)
        self.conn.execute(
            "INSERT INTO appointments (patient_name, appt_date, start_time, duration_minutes, status) VALUES (?,?,?,?,?)",
            ("Today Pt", today.isoformat(), "10:00", 30, "booked"))
        self.conn.commit()

    def ask(self, text):
        # No person in these commands; the name model is never called for real.
        with patch("clinic.nlu.parser.pick_intent", return_value="list_appointments"), \
                patch("clinic.nlu.parser.extract_name", return_value=None):
            return transcript_to_response(self.conn, text, self.adapter, self.adapter, "en-IN")

    def test_a_named_day_lists_that_day_not_today(self):
        result = self.ask("सात तारीख के सारे अपॉइंटमेंट निकालो।")
        if date.today().day == 7:
            self.skipTest("today is the 7th")
        self.assertEqual(result.data, [])
        self.assertIn(self.day7.strftime("%a") + " 7 " + self.day7.strftime("%b"), result.answer_text)

    def test_no_date_means_today_and_says_so(self):
        result = self.ask("show appointments")
        self.assertEqual(len(result.data), 1)
        self.assertIn("(today)", result.answer_text)

    def test_an_unreadable_date_asks_again_instead_of_showing_today(self):
        with self.assertRaises(PipelineError) as ctx:
            self.ask("tareekh ke appointments batao")
        self.assertIn("couldn't read the date", str(ctx.exception))

    def test_tomorrow_is_named(self):
        result = self.ask("get all appointments for tomorrow")
        tomorrow = date.today() + timedelta(days=1)
        self.assertIn(str(tomorrow.day), result.answer_text)


if __name__ == "__main__":
    unittest.main()


class WeekListIsForwardLookingTests(unittest.TestCase):
    def setUp(self):
        from datetime import datetime
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(Path("clinic/schema.sql").read_text())
        self.addCleanup(self.conn.close)
        self.adapter = LocalSQLiteAdapter()
        today = date.today().isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        for name, day, start in (("Earlier Today", today, "09:00"), ("Later Today", today, "20:00"), ("Tomorrow Pt", tomorrow, "10:00")):
            self.conn.execute(
                "INSERT INTO appointments (patient_name, appt_date, start_time, duration_minutes, status) VALUES (?,?,?,30,'booked')",
                (name, day, start))
        self.conn.commit()
        self.now = datetime.combine(date.today(), datetime.strptime("14:00", "%H:%M").time())

    def ask(self, text):
        with patch("clinic.nlu.parser.pick_intent", return_value="list_appointments"), \
                patch("clinic.nlu.parser.extract_name", return_value=None), \
                patch("clinic.pipeline._local_now", return_value=self.now):
            return transcript_to_response(self.conn, text, self.adapter, self.adapter, "en-IN")

    def names(self, result):
        return [r["patient_name"] for r in result.data]

    def test_a_week_list_leaves_out_todays_appointments_that_have_passed(self):
        self.assertEqual(self.names(self.ask("next week ke saare appointments batao")), ["Later Today", "Tomorrow Pt"])
        self.assertEqual(self.names(self.ask("अगले एक हफ्ते के सारे अपॉइंटमेंट")), ["Later Today", "Tomorrow Pt"])

    def test_a_one_day_list_keeps_the_whole_day(self):
        self.assertEqual(self.names(self.ask("show appointments today")), ["Earlier Today", "Later Today"])

    def test_the_context_for_follow_ups_matches_what_was_shown(self):
        from clinic.voice_context import VoiceContext
        ctx = VoiceContext()
        with patch("clinic.nlu.parser.pick_intent", return_value="list_appointments"), \
                patch("clinic.nlu.parser.extract_name", return_value=None), \
                patch("clinic.pipeline._local_now", return_value=self.now):
            transcript_to_response(self.conn, "this week's schedule", self.adapter, self.adapter, "en-IN", context=ctx)
        self.assertEqual([r["patient_name"] for r in ctx.list_rows], ["Later Today", "Tomorrow Pt"])
