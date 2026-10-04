import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic.nlu.classify_patient import classify
from clinic.nlu.patient_parser import UnrecognizedPatientMessage, parse_patient_message


class ClassifyPatientTests(unittest.TestCase):
    def test_confirm_hindi(self):
        self.assertEqual(classify("haan main aaunga"), "confirm_followup")

    def test_cancel_english(self):
        self.assertEqual(classify("Sorry, I can't come, please cancel"), "cancel_followup")

    def test_reschedule(self):
        self.assertEqual(classify("Can we reschedule to another day"), "reschedule_followup")

    def test_new_appointment_inquiry_is_a_booking_request(self):
        # Was register_patient before the token/appointment build: an
        # appointment request is now a book_appointment proposal.
        self.assertEqual(classify("I want to book an appointment"), "book_appointment")
        self.assertEqual(classify("mujhe appointment chahiye"), "book_appointment")
        self.assertEqual(classify("मुझे अपॉइंटमेंट चाहिए"), "book_appointment")
        self.assertEqual(classify("naya patient hoon, appointment chahiye"), "book_appointment")

    def test_explicit_registration_is_still_registration(self):
        self.assertEqual(classify("I want to register"), "register_patient")
        self.assertEqual(classify("naya patient hoon"), "register_patient")

    def test_status_questions(self):
        for text in ("what is my token", "mera token kya hai", "mera number kab aayega",
                     "kitne log baaki hain", "where am I in the queue", "मेरा टोकन क्या है", "कितने लोग बाकी हैं"):
            self.assertEqual(classify(text), "my_status", text)

    def test_status_and_booking_do_not_steal_cancel_reschedule_or_confirm(self):
        self.assertEqual(classify("cancel my token"), "cancel_followup")
        self.assertEqual(classify("haan main aaunga"), "confirm_followup")
        self.assertEqual(classify("haan appointment chahiye"), "book_appointment")
        self.assertEqual(classify("theek hai, mera number kab aayega"), "my_status")

    def test_unrelated_message_returns_none(self):
        self.assertIsNone(classify("What are your clinic timings today"))


class ParsePatientMessageTests(unittest.TestCase):
    def test_confirm_and_cancel_have_no_slots(self):
        self.assertEqual(parse_patient_message("haan aaunga"), ("confirm_followup", {}))
        self.assertEqual(parse_patient_message("cancel karo"), ("cancel_followup", {}))

    def test_reschedule_extracts_kal_as_tomorrow(self):
        from datetime import date, timedelta
        intent, slots = parse_patient_message("kal reschedule kar do")
        self.assertEqual(intent, "reschedule_followup")
        self.assertEqual(slots["new_due_date"], (date.today() + timedelta(days=1)).isoformat())

    def test_reschedule_leaves_ambiguous_date_blank(self):
        intent, slots = parse_patient_message("reschedule to next monday please")
        self.assertEqual(intent, "reschedule_followup")
        self.assertIsNone(slots["new_due_date"])

    @patch("clinic.nlu.patient_parser.extract_name", return_value="Neeta Sharma")
    def test_register_patient_extracts_name(self, _mock_name):
        intent, slots = parse_patient_message("I want to register")
        self.assertEqual(intent, "register_patient")
        self.assertEqual(slots["name"], "Neeta Sharma")

    def test_unclassified_raises(self):
        with self.assertRaises(UnrecognizedPatientMessage):
            parse_patient_message("what is the doctor timing today")


if __name__ == "__main__":
    unittest.main()
