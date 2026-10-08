"""Names and phone numbers are matched exactly (a score of 1.0 or 0.0, never in between); a 10-digit phone
number in the command is matched first. Two patients who fit equally well are asked about, never guessed."""

import sqlite3
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from clinic import entity_resolution, voice_context
from clinic.adapters.local_sqlite import LocalSQLiteAdapter
from clinic.entity_resolution import full_number, name_match, resolve_patient, resolve_staff
from clinic.pipeline import ParsedResult, PipelineError
from clinic.voice_context import AskResult, VoiceContext
from clinic.voice_turns import handle_pick, handle_turn

SCHEMA = (Path(__file__).resolve().parents[1] / "clinic" / "schema.sql").read_text()
DEFER = frozenset({"book_appointment", "cancel_appointment", "reschedule_appointment", "record_visit",
                   "set_followup", "log_attendance", "register_patient"})


def make_db(patients=(), staff=()):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for name, phone in patients:
        conn.execute("INSERT INTO patients (name, phone) VALUES (?, ?)", (name, phone))
    for name in staff:
        conn.execute("INSERT INTO staff (name, role) VALUES (?, 'nurse')", (name,))
    conn.commit()
    return conn


class NameMatchTests(unittest.TestCase):
    def test_only_exact_names_score_and_the_score_is_one_or_zero(self):
        pairs = [("Amit Dua", "Amit Dua", 1.0), ("amit  dua", "Amit Dua", 1.0), ("Mr Amit Dua", "Amit Dua", 1.0),
                 ("Amit", "Amit Dua", 1.0),            # the first name, exactly
                 ("Ami", "Amit Dua", 0.0),             # a piece of a name
                 ("Amit D", "Amit Dua", 0.0),
                 ("Dua", "Amit Dua", 0.0),             # not the first name
                 ("Amit Dua", "Amit", 0.0),            # more than is registered
                 ("Kavitha", "Kavita", 0.0),           # a different spelling
                 ("Nalin", "Nalini", 0.0),
                 ("", "Amit", 0.0)]
        for spoken, stored, expected in pairs:
            with self.subTest(spoken=spoken, stored=stored):
                self.assertEqual(name_match(spoken, stored), expected)

    def test_a_devanagari_name_matches_the_same_name_in_roman_letters(self):
        self.assertEqual(name_match("अमित दुआ", "Amit Dua"), 1.0)
        self.assertEqual(name_match("सुनीता", "Sunita Devi"), 1.0)
        self.assertEqual(name_match("Amit Dua", "अमित दुआ"), 1.0)
        self.assertEqual(name_match("अमित", "Amita Rao"), 0.0)

    def test_full_number_is_exactly_ten_digits(self):
        self.assertEqual(full_number("98765 00301"), "9876500301")
        self.assertEqual(full_number("+91 9876500301"), "9876500301")
        self.assertEqual(full_number("09876500301"), "9876500301")
        for bad in ("987650030", "98765003011", "", None):
            self.assertEqual(full_number(bad), "")


class ResolvePatientTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db([("Amit Dua", "9811111111"), ("Amit Anand", "9822222222"), ("Sunita Devi", "9833333333"),
                             ("Ravi Kumar", "9844444444"), ("Meena Kumar", "9844444444")])

    def names(self, *args, **kw):
        return [c.label.split(" (")[0] for c in resolve_patient(self.conn, *args, **kw)]

    def test_a_phone_number_alone_finds_its_owner_exactly(self):
        self.assertEqual(self.names("9811111111"), ["Amit Dua"])
        self.assertEqual(self.names("", phone="+91 98111 11111"), ["Amit Dua"])
        self.assertEqual(resolve_patient(self.conn, "9811111111")[0].score, 1.0)

    def test_the_phone_is_matched_before_the_name(self):
        self.assertEqual(self.names("Sunita Devi", phone="9811111111"), ["Amit Dua"])

    def test_a_phone_nobody_has_matches_nobody_even_when_the_name_exists(self):
        self.assertEqual(self.names("Amit Dua", phone="9899999999"), [])

    def test_a_phone_that_is_not_ten_digits_is_ignored_and_the_name_is_used(self):
        self.assertEqual(self.names("Amit Dua", phone="981111111"), ["Amit Dua"])

    def test_people_who_share_a_phone_are_told_apart_by_name_when_it_is_said(self):
        self.assertEqual(self.names("", phone="9844444444"), ["Ravi Kumar", "Meena Kumar"])
        self.assertEqual(self.names("Meena", phone="9844444444"), ["Meena Kumar"])
        self.assertEqual(self.names("Someone Else", phone="9844444444"), ["Ravi Kumar", "Meena Kumar"])

    def test_a_first_name_with_two_patients_returns_both_and_one_returns_one(self):
        self.assertEqual(self.names("Amit", top_n=4), ["Amit Dua", "Amit Anand"])
        self.assertEqual(self.names("Sunita"), ["Sunita Devi"])
        self.assertEqual(self.names("Sun"), [])
        self.assertEqual(self.names("Anand"), [])

    def test_a_chosen_patient_id_wins(self):
        self.assertEqual(self.names("Amit", patient_id=2), ["Amit Anand"])

    def test_staff_are_matched_exactly_too(self):
        conn = make_db(staff=["Ravi Singh", "Ravi Rao", "Sunita"])
        self.assertEqual([c.label for c in resolve_staff(conn, "Ravi")], ["Ravi Singh", "Ravi Rao"])
        self.assertEqual([c.label for c in resolve_staff(conn, "Sunit")], [])
        self.assertEqual([c.label for c in resolve_staff(conn, "Ravi Rao")], ["Ravi Rao"])

    def test_ambiguous_patients_needs_two_exact_matches(self):
        both = resolve_patient(self.conn, "Amit", 4)
        self.assertEqual(len(voice_context.ambiguous_patients(both)), 2)
        self.assertEqual(voice_context.ambiguous_patients(resolve_patient(self.conn, "Sunita", 4)), [])


class VoiceFlowTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db([("Amit Dua", "9811111111"), ("Amit Anand", "9822222222"), ("Sunita Devi", "9833333333"),
                             ("Amit Dua", "9855555555")])
        self.adapter = LocalSQLiteAdapter()
        self.tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.names = {"text": None}
        for target, value in (("clinic.nlu.parser.pick_intent", None),):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("clinic.nlu.parser.extract_name", side_effect=lambda text: self.names["text"])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctx = VoiceContext()

    def say(self, text, name=None):
        self.names["text"] = name
        return handle_turn(self.ctx, self.conn, text, self.adapter, self.adapter, "en-IN", DEFER)

    def test_a_booking_with_a_phone_number_goes_to_its_owner_whatever_name_was_heard(self):
        card = self.say("book Sunita tomorrow at 5 phone 9811111111", name="Sunita Devi")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.resolved["patient_id"], 1)
        self.assertTrue(any("belongs to Amit Dua" in n for n in card.resolved["notes"]))

    def test_a_booking_with_a_phone_number_and_no_name_finds_the_patient(self):
        card = self.say("book tomorrow at 5 phone 9822222222", name=None)
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.resolved["patient_id"], 2)

    def test_a_phone_nobody_has_books_a_new_patient_not_the_similar_name(self):
        card = self.say("book Sunita Devi tomorrow at 5 phone 9800000000", name="Sunita Devi")
        self.assertIsInstance(card, ParsedResult)
        self.assertNotIn("patient_id", card.resolved)
        self.assertIsNone(card.resolved.get("phone_problem"))

    def test_a_first_name_shared_by_two_patients_asks_which_one(self):
        ask = self.say("book an appointment for Amit", name="Amit")
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.kind, "choose_patient")
        self.assertEqual(len(ask.options), 3)

    def test_two_patients_with_the_same_full_name_are_told_apart_by_the_one_chosen(self):
        ask = self.say("book an appointment for Amit Dua", name="Amit Dua")
        self.assertEqual(ask.kind, "choose_patient")
        self.assertEqual([o["patient_id"] for o in ask.options], [1, 4])
        index = [o["patient_id"] for o in ask.options].index(4)
        _, result = handle_pick(self.ctx, self.conn, index, self.adapter, self.adapter, "en-IN", DEFER)
        self.assertEqual(result.kind, "date")                     # not "Which one?" again
        self.assertEqual(result.slots["patient_id"], 4)

    def test_a_unique_first_name_needs_no_question(self):
        card = self.say("book Sunita tomorrow at 5", name="Sunita")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.resolved["patient_id"], 3)

    def test_a_part_of_a_name_is_not_a_match(self):
        with self.assertRaises(PipelineError) as raised:
            self.say("show me the details of patient Suni", name="Suni")
        self.assertIn("No patient found exactly matching", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
