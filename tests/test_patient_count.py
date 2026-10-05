"""The patient-count read: "how many patients have been registered so far?"
used to open an empty Register-patient card because the keyword `register` is
inside "registered". It is now its own read (nothing is saved)."""
import sqlite3
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import queries  # noqa: E402
from clinic.adapters.local_sqlite import LocalSQLiteAdapter  # noqa: E402
from clinic.nlu.classify import classify, is_patient_count  # noqa: E402
from clinic.nlu.parser import _parse  # noqa: E402
from clinic.pipeline import ReadResult, transcript_to_response  # noqa: E402

SCHEMA = (Path(__file__).resolve().parents[1] / "clinic" / "schema.sql").read_text()
ASKS = [
    "How many patients have been registered so far?", "total patients", "how many patients do we have",
    "number of patients registered this week", "patient count", "kitne patients registered hain",
    "कितने मरीज रजिस्टर्ड हैं", "कुल कितने पेशेंट हैं", "kul kitne mareez hain",
]
NOT_COUNTS = [
    "how many patients are waiting", "how many appointments are booked this week", "kitne patient baaki hain",
    "how many patients paid a fee today", "register a new patient Ravi 9876543210", "Ramesh ka phone number batao",
    "show tomorrow's appointments",
]


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


class Routing(unittest.TestCase):
    def test_counting_questions_are_recognised_in_three_languages(self):
        for text in ASKS:
            with self.subTest(text=text):
                self.assertTrue(is_patient_count(text))
                self.assertEqual(classify(text), "patient_count")

    def test_other_questions_and_real_registrations_are_left_alone(self):
        for text in NOT_COUNTS:
            with self.subTest(text=text):
                self.assertFalse(is_patient_count(text))
                self.assertNotEqual(classify(text), "patient_count")

    def test_the_parser_trusts_the_rule_over_a_model_that_says_register(self):
        with patch("clinic.nlu.parser.pick_intent", return_value="register_patient"):
            self.assertEqual(_parse("How many patients have been registered so far?"), ("patient_count", {}))

    def test_a_real_registration_still_goes_to_the_model_and_the_card(self):
        with patch("clinic.nlu.parser.pick_intent", return_value="register_patient"), \
                patch("clinic.nlu.parser.extract_name", return_value="Ravi"):
            self.assertEqual(_parse("register a new patient Ravi 9876543210")[0], "register_patient")


class TheCounts(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        today = date.today()
        for name, phone, days_ago in (("Old One", "9000000001", 40), ("Last Week", "9000000002", 6),
                                      ("Seven Ago", "9000000003", 7), ("Yesterday", "9000000004", 1),
                                      ("Today A", "9000000005", 0), ("Today B", "9000000006", 0)):
            self.conn.execute("INSERT INTO patients (name, phone, registered_at) VALUES (?, ?, ?)",
                              (name, phone, (today - timedelta(days=days_ago)).isoformat() + " 10:00:00"))
        self.conn.commit()

    def test_totals_today_and_last_seven_days(self):
        self.assertEqual(queries.patient_counts(self.conn),
                         {"total_patients": 6, "added_today": 2, "added_this_week": 4})

    def test_an_empty_clinic(self):
        self.conn.execute("DELETE FROM patients")
        self.assertEqual(queries.patient_counts(self.conn), {"total_patients": 0, "added_today": 0, "added_this_week": 0})

    def test_the_voice_answer_is_a_read_with_the_numbers_and_saves_nothing(self):
        adapter = LocalSQLiteAdapter()
        with patch("clinic.nlu.parser.pick_intent", return_value=None):
            result = transcript_to_response(self.conn, "How many patients have been registered so far?", adapter, adapter,
                                            "en-IN", frozenset({"register_patient"}))
        self.assertIsInstance(result, ReadResult)
        self.assertEqual(result.intent, "patient_count")
        self.assertEqual(result.data["total_patients"], 6)
        self.assertTrue(result.answer_text.startswith("6 patients registered (4 added in the last 7 days, 2 today)."))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0], 6)

    def test_hindi_answer(self):
        adapter = LocalSQLiteAdapter()
        with patch("clinic.nlu.parser.pick_intent", return_value=None):
            result = transcript_to_response(self.conn, "kul kitne mareez hain", adapter, adapter, "hi-IN", frozenset())
        self.assertTrue(result.answer_text.startswith("Total 6 patient registered hain"))

    def test_the_singular(self):
        self.conn.execute("DELETE FROM patients WHERE name != 'Old One'")
        adapter = LocalSQLiteAdapter()
        with patch("clinic.nlu.parser.pick_intent", return_value=None):
            result = transcript_to_response(self.conn, "total patients", adapter, adapter, "en-IN", frozenset())
        self.assertTrue(result.answer_text.startswith("1 patient registered."))


if __name__ == "__main__":
    unittest.main()
