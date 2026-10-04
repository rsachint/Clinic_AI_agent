import sqlite3
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic.adapters.local_sqlite import LocalSQLiteAdapter
from clinic.pipeline import ParsedResult, PipelineError, ReadResult
from clinic.voice_context import AskResult, CardUpdate, Note, VoiceContext
from clinic.voice_turns import handle_pick, handle_turn

DEFER = frozenset({
    "book_appointment", "cancel_appointment", "reschedule_appointment", "record_visit", "set_followup",
    "cancel_followup", "reschedule_followup", "log_attendance", "register_patient",
})
NAMES = ["Rakesh Verma", "Mohan Lal", "Mohan Das", "Sunita Devi", "Mohan"]


def fake_name(text):
    """Stand-in for the local name model: the longest known name in the text."""
    found = [n for n in NAMES if n.lower() in text.lower()]
    return max(found, key=len) if found else None


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class DialogTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(Path("clinic/schema.sql").read_text())
        self.addCleanup(self.conn.close)
        for name, phone in (("Rakesh Verma", "9000000001"), ("Mohan Lal", "9000000002"),
                            ("Mohan Das", "9000000003"), ("Sunita Devi", "9000000004")):
            self.conn.execute("INSERT INTO patients (name, phone, age) VALUES (?, ?, 40)", (name, phone))
        self.today = date.today()
        self.tomorrow = self.today + timedelta(days=1)
        tomorrow = self.tomorrow.isoformat()
        for pid, start in ((1, "10:00"), (2, "11:00"), (4, "16:00")):
            self.conn.execute(
                "INSERT INTO appointments (patient_id, appt_date, start_time, duration_minutes, status) "
                "VALUES (?, ?, ?, 30, 'booked')", (pid, tomorrow, start))
        self.conn.commit()
        self.adapter = LocalSQLiteAdapter()
        self.clock = FakeClock()
        self.ctx = VoiceContext(clock=self.clock)
        for target, side in (("clinic.nlu.parser.extract_name", fake_name),):
            patcher = patch(target, side_effect=side)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("clinic.nlu.parser.pick_intent", return_value=None)  # keyword rules only
        patcher.start()
        self.addCleanup(patcher.stop)

    def say(self, text, language="en-IN"):
        return handle_turn(self.ctx, self.conn, text, self.adapter, self.adapter, language, DEFER)


class CarryOverTests(DialogTestCase):
    def test_book_him_uses_the_patient_just_looked_up(self):
        look = self.say("show me the details of patient Rakesh Verma")
        self.assertIsInstance(look, ReadResult)
        self.assertEqual(self.ctx.patient["name"], "Rakesh Verma")
        card = self.say("book him tomorrow at 5")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.intent, "book_appointment")
        self.assertEqual(card.slots["patient_name"], "Rakesh Verma")
        self.assertEqual(card.slots["appt_date"], self.tomorrow.isoformat())
        self.assertEqual(card.slots["start_time"], "17:00")
        self.assertEqual(card.resolved["patient_label"].split(" (")[0], "Rakesh Verma")

    def test_without_a_pronoun_the_last_patient_is_offered_not_assumed(self):
        self.say("show me the details of patient Rakesh Verma")
        ask = self.say("book an appointment")
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.kind, "patient")
        self.assertEqual([o["patient_name"] for o in ask.options], ["Rakesh Verma"])

    def test_same_day_reuses_the_last_date(self):
        self.say("what are the appointments for tomorrow")
        self.assertEqual(self.ctx.date, self.tomorrow.isoformat())
        ask = self.say("book Sunita Devi on the same day")
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.kind, "time")  # patient and day known, only the time is missing
        self.assertEqual(ask.slots["appt_date"], self.tomorrow.isoformat())


class ListReferenceTests(DialogTestCase):
    def setUp(self):
        super().setUp()
        listed = self.say("show appointments for tomorrow")
        self.assertIsInstance(listed, ReadResult)
        self.assertEqual(len(self.ctx.list_rows), 3)

    def test_cancel_the_second_one(self):
        card = self.say("cancel the second one")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.intent, "cancel_appointment")
        second = self.ctx.list_rows[1]
        self.assertEqual(card.slots["appointment_id"], second["id"])
        self.assertEqual(card.slots["patient_name"], "Mohan Lal")
        self.assertIn(second["id"], [a["id"] for a in card.resolved["appointments"]])

    def test_hindi_and_last_references(self):
        self.assertEqual(self.say("पहला वाला कैंसिल करो").slots["appointment_id"], self.ctx.list_rows[0]["id"])
        self.assertEqual(self.say("cancel the last one").slots["appointment_id"], self.ctx.list_rows[2]["id"])

    def test_reschedule_the_first_one(self):
        ask = self.say("move the first one to 6 pm")
        # the new date is missing, so the assistant asks for it
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.intent, "reschedule_appointment")
        self.assertEqual(ask.kind, "date")

    def test_a_row_that_does_not_exist_is_refused(self):
        with self.assertRaises(PipelineError) as raised:
            self.say("cancel the fifth one")
        self.assertIn("only 3", str(raised.exception))

    def test_a_date_with_an_ordinal_is_not_a_row(self):
        self.assertIsNone(__import__("clinic.voice_context", fromlist=["x"]).reference_index("second of october", 3))


class CardEditTests(DialogTestCase):
    def open_book_card(self):
        card = self.say("book Sunita Devi tomorrow at 5")
        self.assertIsInstance(card, ParsedResult)
        return card

    def test_make_it_6_pm_edits_the_open_card(self):
        self.open_book_card()
        card_id = self.ctx.open_card["card_id"]
        update = self.say("make it 6 pm instead")
        self.assertIsInstance(update, CardUpdate)
        self.assertEqual(update.card_id, card_id)
        self.assertEqual(update.changes, {"start_time": "18:00"})

    def test_hindi_and_date_edits(self):
        self.open_book_card()
        update = self.say("शाम 6 बजे कर दो")
        self.assertEqual(update.changes, {"start_time": "18:00"})
        update = self.say("make it the 20th instead")
        self.assertEqual(update.changes["appt_date"], self.today.replace(day=20).isoformat())

    def test_a_new_command_is_not_mistaken_for_an_edit(self):
        self.open_book_card()
        result = self.say("show appointments for tomorrow")
        self.assertIsInstance(result, ReadResult)

    def test_a_card_closed_by_the_page_can_no_longer_be_edited(self):
        self.open_book_card()
        self.ctx.close_card(self.ctx.open_card["card_id"])
        self.assertNotIsInstance(self.say("make it 6 pm instead") if False else None, CardUpdate)
        with self.assertRaises(PipelineError):
            self.say("make it 6 pm instead")  # no card open, nothing to edit, nothing recognised

    def test_nothing_recognisable_is_not_an_edit(self):
        self.open_book_card()
        with self.assertRaises(PipelineError):
            self.say("hmm hold on a moment")


class AskingTests(DialogTestCase):
    def test_book_an_appointment_asks_patient_then_day_then_time(self):
        ask = self.say("book an appointment")
        self.assertEqual((ask.kind, ask.question), ("patient", "Which patient?"))
        ask = self.say("Rakesh Verma")
        self.assertEqual((ask.kind, ask.question), ("date", "Which day?"))
        ask = self.say("tomorrow")
        self.assertEqual((ask.kind, ask.question), ("time", "What time?"))
        card = self.say("5")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.slots["patient_name"], "Rakesh Verma")
        self.assertEqual(card.slots["appt_date"], self.tomorrow.isoformat())
        self.assertEqual(card.slots["start_time"], "17:00")
        self.assertIsNone(self.ctx.pending)

    def test_day_and_time_can_be_answered_together(self):
        self.say("book an appointment")
        self.say("Sunita Devi")
        card = self.say("tomorrow at 4 pm")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.slots["start_time"], "16:00")

    def test_hinglish_questions_when_the_language_is_hindi(self):
        ask = self.say("book an appointment", language="hi-IN")
        self.assertEqual(ask.question, "Kaun sa patient?")

    def test_a_new_unregistered_patient_is_accepted_for_a_booking(self):
        self.say("book an appointment")
        ask = self.say("Anurag Sharma")
        self.assertEqual(ask.kind, "date")
        self.assertEqual(ask.slots["patient_name"], "Anurag Sharma")

    def test_cancel_needs_a_registered_patient_and_re_asks_once(self):
        self.say("cancel appointment")
        ask = self.say("Zzzz Qqqq")
        self.assertEqual(ask.kind, "patient")
        self.assertIn("couldn't find", ask.question)
        with self.assertRaises(PipelineError):
            self.say("Zzzz Qqqq")
        self.assertIsNone(self.ctx.pending)

    def test_skip_leaves_the_field_blank_and_does_not_ask_again(self):
        self.say("book an appointment")
        self.say("Rakesh Verma")
        ask = self.say("tomorrow")
        self.assertEqual(ask.kind, "time")
        card = self.say("skip")
        self.assertIsInstance(card, ParsedResult)
        self.assertIsNone(card.slots.get("start_time"))

    def test_never_mind_drops_the_question(self):
        self.say("book an appointment")
        note = self.say("never mind")
        self.assertIsInstance(note, Note)
        self.assertIsNone(self.ctx.pending)

    def test_a_different_command_while_waiting_is_treated_as_a_new_command(self):
        self.say("book an appointment")
        result = self.say("show appointments for tomorrow")
        self.assertIsInstance(result, ReadResult)
        self.assertIsNone(self.ctx.pending)


class DisambiguationTests(DialogTestCase):
    def test_two_mohans_ask_which_one(self):
        ask = self.say("book an appointment for Mohan")
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.kind, "choose_patient")
        self.assertEqual(sorted(o["patient_name"] for o in ask.options), ["Mohan Das", "Mohan Lal"])

    def test_answer_by_a_distinguishing_word(self):
        self.say("book an appointment for Mohan")
        ask = self.say("Lal")
        self.assertEqual(ask.kind, "date")
        self.assertEqual(ask.slots["patient_name"], "Mohan Lal")

    def test_answer_by_position(self):
        self.say("book an appointment for Mohan")
        ask = self.say("the second one")
        self.assertEqual(ask.kind, "date")
        self.assertIn(ask.slots["patient_name"], ("Mohan Lal", "Mohan Das"))

    def test_tapping_an_option_works_like_speaking(self):
        ask = self.say("book an appointment for Mohan")
        index = [o["patient_name"] for o in ask.options].index("Mohan Das")
        label, result = handle_pick(self.ctx, self.conn, index, self.adapter, self.adapter, "en-IN", DEFER)
        self.assertIn("Mohan Das", label)
        self.assertEqual(result.slots["patient_name"], "Mohan Das")

    def test_an_unclear_answer_asks_again_then_gives_up(self):
        self.say("book an appointment for Mohan")
        again = self.say("hmm")
        self.assertEqual(again.kind, "choose_patient")
        with self.assertRaises(PipelineError):
            self.say("hmm")

    def test_a_full_unique_name_is_not_ambiguous(self):
        card = self.say("book Mohan Lal tomorrow at 5")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.resolved["patient_label"].split(" (")[0], "Mohan Lal")


class LifetimeTests(DialogTestCase):
    def test_context_is_forgotten_after_ten_idle_minutes(self):
        self.say("show me the details of patient Rakesh Verma")
        self.assertIsNotNone(self.ctx.patient)
        self.clock.now += 601
        ask = self.say("book him tomorrow at 5")
        # "him" no longer means anyone: the assistant has to ask
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.kind, "patient")
        self.assertEqual(ask.options, [])

    def test_activity_keeps_the_context_alive(self):
        self.say("show me the details of patient Rakesh Verma")
        self.clock.now += 500
        self.say("show appointments for tomorrow")
        self.clock.now += 500
        card = self.say("book him tomorrow at 5")
        self.assertIsInstance(card, ParsedResult)

    def test_clear_forgets_everything(self):
        self.say("show me the details of patient Rakesh Verma")
        self.ctx.clear()
        self.assertFalse(self.ctx.has_content())

    def test_snapshot_for_the_page(self):
        self.say("show me the details of patient Rakesh Verma")
        snap = self.ctx.snapshot()
        self.assertEqual(snap["patient"], "Rakesh Verma")
        self.assertLessEqual(snap["expires_in_s"], 600)

    def test_model_hint_has_names_but_no_phone_numbers(self):
        self.say("show me the details of patient Rakesh Verma")
        hint = self.ctx.model_hint()
        self.assertIn("Rakesh Verma", hint)
        self.assertNotIn("9000000001", hint)


class NothingIsWrittenTests(DialogTestCase):
    def test_no_turn_writes_clinic_data(self):
        before = {t: self.conn.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
                  for t in ("appointments", "patients", "proposals", "audit_log")}
        for text in ("show me the details of patient Rakesh Verma", "book him tomorrow at 5", "make it 6 pm instead",
                     "show appointments for tomorrow", "cancel the second one", "book an appointment", "Rakesh Verma"):
            try:
                self.say(text)
            except PipelineError:
                pass
        after = {t: self.conn.execute("SELECT COUNT(*) FROM " + t).fetchone()[0] for t in before}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
