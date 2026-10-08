import sqlite3
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import entity_resolution
from clinic.adapters.local_sqlite import LocalSQLiteAdapter
from clinic.pipeline import ParsedResult, transcript_to_response
from clinic.queries import upcoming_appointments_named
from clinic.translit import cross_script_similarity, has_devanagari, phonetic_key, to_latin
from clinic.voice_context import VoiceContext
from clinic.voice_turns import handle_turn

DEFER = frozenset(("cancel_appointment", "reschedule_appointment", "book_appointment"))


class TransliterationTests(unittest.TestCase):
    def test_devanagari_names_sound_like_their_roman_spelling(self):
        for devanagari, roman in (("अमित दुआ", "Amit Dua"), ("आकाश", "Akash"), ("सोनी", "Soni"), ("मंजू", "Manju"),
                                  ("प्रिया शाह", "Priya Shah"), ("राहुल शर्मा", "Rahul Sharma"),
                                  ("सुनीता देवी", "Sunita Devi"), ("अमिताभ घोष", "Amitabh Ghosh")):
            with self.subTest(name=roman):
                self.assertEqual(phonetic_key(devanagari), phonetic_key(roman))
                self.assertAlmostEqual(cross_script_similarity(devanagari, roman), 0.95)

    def test_different_names_score_low(self):
        self.assertLess(cross_script_similarity("अमित दुआ", "Rakesh Verma"), 0.4)
        self.assertLess(cross_script_similarity("अमित दुआ", "Amitabh Ghosh"), 0.6)

    def test_same_script_is_not_this_modules_business(self):
        self.assertEqual(cross_script_similarity("Amit Dua", "Amit Dua"), 0.0)
        self.assertEqual(cross_script_similarity("अमित", "अमित"), 0.0)

    def test_too_short_to_compare_scores_zero(self):
        self.assertEqual(cross_script_similarity("आ", "A"), 0.0)

    def test_helpers(self):
        self.assertTrue(has_devanagari("अमित"))
        self.assertFalse(has_devanagari("Amit"))
        self.assertEqual(to_latin("अमित"), "amit")  # the final "a" is dropped, as in Hindi

    def test_a_cross_script_match_is_exact_or_nothing(self):
        self.assertEqual(entity_resolution.similarity("अमित दुआ", "Amit Dua"), 1.0)     # the same name, in the other script
        self.assertEqual(entity_resolution.similarity("Amit Dua", "Amit Dua"), 1.0)
        self.assertEqual(entity_resolution.similarity("अमित दुआ", "Amit Duaa Rao"), 0.0)  # not "nearly"
        self.assertEqual(entity_resolution.similarity("अमित", "Amita"), 0.0)


class WalkInTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(Path("clinic/schema.sql").read_text())
        self.addCleanup(self.conn.close)
        self.adapter = LocalSQLiteAdapter()
        self.today = date.today()
        self.conn.execute("INSERT INTO patients (name, phone, age) VALUES ('Amit Anand', '9876500101', 40)")
        self.conn.execute("INSERT INTO patients (name, phone, age) VALUES ('राहुल शर्मा', '9876543210', 30)")
        self.add_appt(None, "Amit Dua", 0, "14:00")           # id 1: a walk-in, today
        self.add_appt(1, None, 1, "11:00")                    # id 2: registered Amit Anand, tomorrow
        self.add_appt(None, "Akash", 2, "14:00")              # id 3: walk-in
        self.add_appt(None, "Old Timer", -3, "10:00")         # id 4: in the past
        self.add_appt(None, "Amit Dua", 3, "10:00", "cancelled")  # id 5: cancelled
        self.add_appt(2, None, 4, "15:00")                    # id 6: Devanagari registered patient
        self.conn.commit()

    def add_appt(self, patient_id, name, days, start, status="booked"):
        self.conn.execute(
            "INSERT INTO appointments (patient_id, patient_name, appt_date, start_time, duration_minutes, status) "
            "VALUES (?, ?, ?, ?, 30, ?)",
            (patient_id, name, (self.today + timedelta(days=days)).isoformat(), start, status))


class NamedAppointmentQueryTests(WalkInTestCase):
    def ids(self, name):
        return [r["id"] for r in upcoming_appointments_named(self.conn, name)]

    def test_a_walk_in_is_found_by_the_name_written_on_the_appointment(self):
        self.assertEqual(self.ids("Amit Dua"), [1])

    def test_devanagari_finds_a_roman_name_and_the_reverse(self):
        self.assertEqual(self.ids("अमित दुआ"), [1])
        self.assertEqual(self.ids("Rahul Sharma"), [6])
        self.assertEqual(self.ids("आकाश"), [3])

    def test_only_the_best_fitting_person_is_kept(self):
        self.assertEqual(self.ids("अमित दुआ"), [1])         # not Amit Anand's booking too
        self.assertEqual(self.ids("अमित आनंद"), [2])

    def test_todays_earlier_appointments_count_but_past_days_and_cancelled_do_not(self):
        self.assertEqual(self.ids("Old Timer"), [])
        self.assertEqual([r["id"] for r in upcoming_appointments_named(self.conn, "Amit Dua")], [1])  # id 5 is cancelled

    def test_nothing_for_an_unknown_or_empty_name(self):
        self.assertEqual(self.ids("राजेश"), [])
        self.assertEqual(self.ids(""), [])
        self.assertEqual(self.ids(None), [])


class CancelCardTests(WalkInTestCase):
    def ask(self, text, name):
        with patch("clinic.nlu.parser.pick_intent", return_value="cancel_appointment"), \
                patch("clinic.nlu.parser.extract_name", return_value=name):
            return transcript_to_response(self.conn, text, self.adapter, self.adapter, "hi-IN", defer_intents=DEFER)

    def test_the_walk_in_cancel_card_is_filled_in(self):
        card = self.ask("अमित दुआ का अपॉइंटमेंट कैंसिल कर दो।", "अमित दुआ")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.slots["appointment_id"], 1)
        options = card.resolved["appointments"]
        self.assertEqual([o["id"] for o in options], [1])
        self.assertIn("Amit Dua", options[0]["label"])

    def test_an_english_request_for_the_same_walk_in(self):
        card = self.ask("cancel Amit Dua's appointment", "Amit Dua")
        self.assertEqual(card.slots["appointment_id"], 1)

    def test_a_registered_patient_still_works(self):
        card = self.ask("Amit Anand ka appointment cancel karo", "Amit Anand")
        self.assertEqual(card.slots["appointment_id"], 2)

    def test_no_match_says_so_and_leaves_the_choice_blank(self):
        card = self.ask("राजेश का अपॉइंटमेंट कैंसिल करो", "राजेश")
        self.assertEqual(card.resolved["appointments"], [])
        self.assertIn("No upcoming appointment found", card.resolved["note"])
        self.assertNotIn("appointment_id", card.slots)

    def test_two_people_with_the_same_first_name_get_no_preselected_choice(self):
        card = self.ask("Amit cancel", "Amit")      # Amit Dua and Amit Anand both fit exactly
        self.assertGreaterEqual(len(card.resolved["appointments"]), 2)
        self.assertNotIn("appointment_id", card.slots)

    def test_one_person_with_two_bookings_preselects_the_nearest(self):
        self.add_appt(None, "Akash", 6, "09:30")
        self.conn.commit()
        card = self.ask("आकाश का अपॉइंटमेंट कैंसिल करो", "आकाश")
        self.assertEqual(card.slots["appointment_id"], 3)       # the nearer of the two
        self.assertEqual(len(card.resolved["appointments"]), 2)


class CancelDialogTests(WalkInTestCase):
    def test_asking_for_the_patient_accepts_a_walk_in_name(self):
        ctx = VoiceContext()
        with patch("clinic.nlu.parser.pick_intent", return_value=None):
            ask = handle_turn(ctx, self.conn, "cancel appointment", self.adapter, self.adapter, "en-IN", DEFER)
            self.assertEqual(ask.kind, "patient")
            card = handle_turn(ctx, self.conn, "अमित दुआ", self.adapter, self.adapter, "en-IN", DEFER)
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.slots["appointment_id"], 1)


if __name__ == "__main__":
    unittest.main()
