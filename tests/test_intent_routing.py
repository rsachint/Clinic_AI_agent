import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic.nlu.classify import classify
from clinic.nlu.parser import UnrecognizedCommand, parse

# The router: the local model decides, the keyword rules are the backup. The
# model is always mocked here (tests never wait on a live Ollama).

MISROUTES = [
    "Can you get all the appointments for tomorrow?",
    "fetch all the appointments for tomorrow",
    "show me tomorrow's appointments",
    "list all appointments for this week",
    "kal ke saare appointments dikhao",
]


class RulesBackupTests(unittest.TestCase):
    def test_reading_appointments_is_a_list_not_a_booking(self):
        for text in MISROUTES:
            with self.subTest(text=text):
                self.assertEqual(classify(text), "list_appointments")

    def test_real_bookings_are_still_bookings(self):
        for text in ("book an appointment for Ramesh tomorrow at 5 pm",
                     "Sunita ke liye kal appointment chahiye",
                     "appointment for Mohan on Friday"):
            with self.subTest(text=text):
                self.assertEqual(classify(text), "book_appointment")


class ModelDecidesTests(unittest.TestCase):
    def setUp(self):
        # A list command now also looks for a name; no test may reach the live name model.
        patcher = patch("clinic.nlu.parser.extract_name", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    @patch("clinic.nlu.parser.pick_intent", return_value="list_appointments")
    def test_the_model_wins_over_the_rules(self, _pick):
        # Rules alone would say book_appointment ("appointment" + no read verb).
        self.assertEqual(classify("the appointment thing for Mohan tomorrow"), "book_appointment")
        intent, _ = parse("the appointment thing for Mohan tomorrow")
        self.assertEqual(intent, "list_appointments")

    @patch("clinic.nlu.parser.pick_intent", return_value=None)
    def test_rules_decide_when_the_model_has_no_answer(self, _pick):
        intent, _ = parse("fetch all the appointments for tomorrow")
        self.assertEqual(intent, "list_appointments")

    @patch("clinic.nlu.parser.pick_intent", return_value=None)
    def test_nothing_from_either_is_unrecognised(self, _pick):
        with self.assertRaises(UnrecognizedCommand):
            parse("the weather is nice today")

    @patch("clinic.nlu.parser.pick_intent", return_value="list_appointments")
    def test_a_disagreement_is_logged(self, _pick):
        with self.assertLogs("clinic.nlu.parser", level="INFO") as logs:
            parse("book an appointment for Ramesh tomorrow at 5 pm")
        self.assertTrue(any("disagreement" in line for line in logs.output))


class ListAppointmentsDateTests(unittest.TestCase):
    def setUp(self):
        # A list command now also looks for a name; no test may reach the live name model.
        patcher = patch("clinic.nlu.parser.extract_name", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    @patch("clinic.nlu.parser.pick_intent", return_value=None)
    def test_tomorrow_becomes_a_date(self, _pick):
        from datetime import date, timedelta
        _, slots = parse("fetch all the appointments for tomorrow")
        self.assertEqual(slots["date"], (date.today() + timedelta(days=1)).isoformat())

    @patch("clinic.nlu.parser.pick_intent", return_value=None)
    def test_today_and_week_still_work(self, _pick):
        _, today = parse("show appointments today")
        self.assertEqual(today["range"], "today")
        _, week = parse("list all appointments for this week")
        self.assertEqual(week, {"range": "week"})


if __name__ == "__main__":
    unittest.main()
