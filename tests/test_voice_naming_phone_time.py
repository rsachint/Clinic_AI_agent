"""Three defects found on one live command, each pinned here:

  1. "book a consultation for Priya, her mobile number is ..." with another patient remembered was routed as a
     "book him" follow-up and filled the remembered patient. A sentence that names someone is never a pronoun
     follow-up, and a possessive ("her mobile number") is not a call-back to the remembered patient.
  2. A phone number said in words ("nine eight seven ...") was only understood as the answer to "What is the
     phone number?", not inside the first command.
  3. "at 11 p.m." / "11 P.M." was read as 11:00 while "11 pm" was 23:00.

No live model, WhatsApp or Sarvam call: the planner runs on a FakeBackend and the keyword rules are the old ones.
Dates are relative to the (injected) day the fixtures build, never to a fixed one."""
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.planner_support import PlannerCase  # noqa: E402
from tests.test_voice_dialog import DialogTestCase  # noqa: E402

from clinic import voice_context  # noqa: E402
from clinic.nlu import classify, date_guard, datetime_extract, extract  # noqa: E402
from clinic.nlu import tools  # noqa: E402
from clinic.nlu.parser import parse  # noqa: E402
from clinic.pipeline import ParsedResult  # noqa: E402
from clinic.voice_context import AskResult, VoiceContext  # noqa: E402

LIVE_COMMAND = ("I want to book a consultation for Priya, her mobile number is "
                "nine eight seven six five four three two one zero.")
PHONE = "9876543210"


class NamedPersonIsNotAPronounFollowUp(unittest.TestCase):
    def setUp(self):
        self.ctx = VoiceContext()
        self.ctx.remember_patient(1, "Rahul Sharma")

    def test_the_live_command_is_not_a_context_rule(self):
        self.assertIsNone(voice_context.contextual_intent(LIVE_COMMAND, self.ctx))

    def test_a_possessive_on_a_detail_is_not_a_reference_to_the_remembered_patient(self):
        for text in ("her mobile number is 9876543210", "uska number nau aath saat", "her phone number",
                     "उसका नंबर नौ आठ सात", "his new phone number", "uski age 40"):
            with self.subTest(text=text):
                self.assertFalse(voice_context.has_anaphoric_reference(text))

    def test_a_real_pronoun_still_is(self):
        for text in ("book her tomorrow at 5", "book him tomorrow at 5", "cancel him", "usko kal book karo",
                     "book her, her number is 9876543210", "उसे कल बुक करो", "book the same patient"):
            with self.subTest(text=text):
                self.assertTrue(voice_context.has_anaphoric_reference(text))

    def test_pronoun_follow_ups_still_use_the_remembered_patient(self):
        self.assertEqual(voice_context.contextual_intent("book her tomorrow at 5", self.ctx), "book_appointment")
        self.assertEqual(voice_context.contextual_intent("book him tomorrow at 5", self.ctx), "book_appointment")
        self.assertEqual(voice_context.contextual_intent("cancel him", self.ctx), "cancel_appointment")
        self.assertEqual(voice_context.contextual_intent("book him with Dr Mehta tomorrow", self.ctx), "book_appointment")
        self.assertEqual(voice_context.contextual_intent("book him for a checkup tomorrow", self.ctx), "book_appointment")
        self.assertEqual(voice_context.contextual_intent("usko kal 5 baje book karo", self.ctx), "book_appointment")

    def test_a_named_person_beats_the_pronoun(self):
        for text in ("book her, for Priya tomorrow", "book him for Sunita", "cancel him, named Amit",
                     "Priya ke liye book karo, uska number", "book Priya tomorrow, her number is 9876543210"):
            with self.subTest(text=text):
                if "ke liye" in text or "for" in text or "named" in text:
                    self.assertTrue(voice_context.names_a_person(text))
                self.assertIsNone(voice_context.contextual_intent(text, self.ctx))

    def test_a_registered_name_written_in_the_sentence_is_a_named_person(self):
        self.assertTrue(voice_context.names_a_person("book Priya tomorrow", known_names=["Priya"]))
        self.assertTrue(voice_context.names_a_person("book Mohan Lal, her number is 98", known_names=["Mohan Lal"]))
        self.assertFalse(voice_context.names_a_person("book him with Dr Mehta", known_names=["Mehta"]))
        self.assertFalse(voice_context.names_a_person("book her tomorrow at 5", known_names=["Priya Rao"]))

    def test_words_after_for_that_are_not_names(self):
        for text in ("book her for tomorrow", "book him for 5 pm", "book her for Saturday", "book him for a consultation",
                     "book her for the same day", "book him for evening", "book him for her"):
            with self.subTest(text=text):
                self.assertFalse(voice_context.names_a_person(text))

    def test_the_row_number_rule_is_untouched(self):
        self.ctx.remember_list([{"id": 11, "patient_name": "Amit Dua"}, {"id": 12, "patient_name": "Ravi Kumar"}], "today")
        self.assertEqual(voice_context.contextual_intent("cancel the second one", self.ctx), "cancel_appointment")
        self.assertEqual(voice_context.contextual_intent("cancel the second one for Priya", self.ctx), "cancel_appointment")
        self.assertEqual(voice_context.contextual_intent("reschedule number 1", self.ctx), "reschedule_appointment")

    def test_apply_context_never_overwrites_a_name_that_was_read(self):
        slots = voice_context.apply_context("book_appointment", {"patient_name": "Priya"}, "book her tomorrow", self.ctx)
        self.assertEqual(slots["patient_name"], "Priya")
        self.assertNotIn("patient_id", slots)
        # the model filling the remembered name back in for "him" still resolves to the remembered id
        slots = voice_context.apply_context("book_appointment", {"patient_name": "Rahul Sharma"}, "book him tomorrow", self.ctx)
        self.assertEqual(slots["patient_id"], 1)

    def test_apply_context_does_not_fill_the_remembered_patient_for_a_sentence_that_names_someone(self):
        slots = voice_context.apply_context("book_appointment", {}, LIVE_COMMAND, self.ctx)
        self.assertNotIn("patient_name", slots)
        self.assertNotIn("patient_id", slots)
        slots = voice_context.apply_context("book_appointment", {}, "book him tomorrow at 5", self.ctx)
        self.assertEqual((slots["patient_name"], slots["patient_id"]), ("Rahul Sharma", 1))

    def test_an_answer_to_a_question_never_carries_the_remembered_patient(self):
        slots = voice_context.apply_context("book_appointment", {"patient_name": "Priya"}, "her", self.ctx, fresh=False)
        self.assertEqual(slots, {"patient_name": "Priya"})


class TheLiveCommandOnTheRulesRoute(DialogTestCase):
    """Planner off: the keyword rules and the deterministic readers."""

    def setUp(self):
        super().setUp()
        self.ctx.remember_patient(1, "Rakesh Verma")

    def test_the_named_person_wins_over_memory_and_the_phone_is_read(self):
        result = self.say("book an appointment for Priya tomorrow at 5 pm, her mobile number is "
                          "nine eight seven six five four three two one zero.")
        slots = result.slots
        self.assertNotEqual(slots.get("patient_name"), "Rakesh Verma")
        self.assertNotIn("patient_id", slots)
        self.assertEqual(slots["patient_phone"], PHONE)
        self.assertEqual(slots["start_time"], "17:00")

    def test_book_her_still_uses_the_remembered_patient(self):
        card = self.say("book her tomorrow at 5")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.slots["patient_name"], "Rakesh Verma")
        self.assertEqual(card.slots["start_time"], "17:00")

    def test_her_mobile_number_alone_does_not_pick_the_remembered_patient(self):
        result = self.say("book an appointment tomorrow at 5 pm, her mobile number is " + " ".join(
            "nine eight seven six five four three two one zero".split()))
        self.assertIsInstance(result, AskResult)
        self.assertEqual(result.kind, "patient")           # asked who, offering the remembered one as an option
        self.assertEqual(result.slots["patient_phone"], PHONE)
        self.assertNotIn("patient_id", result.slots)

    def test_the_answer_for_the_missing_day_keeps_the_named_patient_and_phone(self):
        ask = self.say("book an appointment for Mohan Lal at 5 pm, his mobile number is "
                       "nine eight seven six five four three two one zero")
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.kind, "date")
        self.assertEqual(ask.slots["patient_name"], "Mohan Lal")
        card = self.say("Coming Saturday at 11 p.m.")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.slots["patient_name"], "Mohan Lal")
        self.assertEqual(card.slots["patient_phone"], PHONE)
        self.assertEqual(card.slots["start_time"], "23:00")
        self.assertEqual(card.slots["appt_date"], datetime_extract.extract_appt_date("coming saturday", date.today()))


class TheLiveCommandOnThePlannerRoute(PlannerCase):
    def setUp(self):
        super().setUp()
        self.ctx.remember_patient(1, "Rakesh Verma")
        self.when = {"date": self.tomorrow.isoformat(), "time": "17:00"}

    def test_the_planner_is_asked_and_the_card_is_for_the_named_person_with_the_phone(self):
        self.call("book_appointment", patient_name="Priya", **self.when)
        ask = self.say(LIVE_COMMAND)
        self.assertEqual(len(self.backend.calls), 1)                 # the planner read the sentence
        self.assertEqual(ask.slots["patient_name"], "Priya")
        self.assertNotEqual(ask.slots.get("patient_id"), 1)
        self.assertEqual(ask.slots["patient_phone"], PHONE)          # the reader fills it; the fake sent none
        row = self.log_rows()[-1]
        self.assertEqual(row["route_taken"], "planner")
        self.assertIsNone(row["route_detail"])

    def test_the_deterministic_phone_outranks_the_models(self):
        self.call("book_appointment", patient_name="Priya", phone="9111111111", **self.when)
        ask = self.say(LIVE_COMMAND)
        self.assertEqual(ask.slots["patient_phone"], PHONE)

    def test_two_registered_priyas_ask_which_one(self):
        for name, phone in (("Priya Sharma", "9000000011"), ("Priya Nair", "9000000012")):
            self.conn.execute("INSERT INTO patients (name, phone, age) VALUES (?, ?, 30)", (name, phone))
        self.conn.commit()
        self.call("book_appointment", patient_name="Priya", **self.when)
        # no phone said (a phone nobody has settles "who" by itself): the name alone is ambiguous
        ask = self.say("I want to book a consultation for Priya, her second visit this month.")
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.kind, "choose_patient")
        self.assertEqual(sorted(o["patient_name"] for o in ask.options), ["Priya Nair", "Priya Sharma"])
        self.assertEqual(self.log_rows()[-1]["route_taken"], "planner")

    def test_a_pronoun_follow_up_is_still_a_rule_and_skips_the_planner(self):
        self.call("book_appointment", patient_name="Somebody Else")
        card = self.say("book him tomorrow at 5")
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(card.slots["patient_name"], "Rakesh Verma")
        row = self.log_rows()[-1]
        self.assertEqual((row["route_taken"], row["route_detail"]), ("rules", "rule:context"))


class SpokenPhoneNumbers(unittest.TestCase):
    def test_ten_digits_said_one_by_one(self):
        for text in ("nine eight seven six five four three two one zero",
                     "plus nine one 98765 43210", "+91 98765 43210", "zero 98765 43210",
                     "नौ आठ सात छह पांच चार तीन दो एक शून्य",
                     "nau aath saat chhe paanch chaar teen do ek shunya",
                     "my number is nine eight seven six five four three two one zero, thanks",
                     "plus nine one nine eight seven six five four three two one zero"):
            with self.subTest(text=text):
                self.assertEqual(extract.extract_phone(text), PHONE)
                self.assertEqual(extract.read_phone_exact(text), PHONE)

    def test_double_and_triple(self):
        self.assertEqual(extract.extract_phone("double nine eight seven six five four three two one"), "9987654321")
        self.assertEqual(extract.extract_phone("triple nine eight seven six five four three two"), "9998765432")
        self.assertEqual(extract.extract_phone("nine eight seven six five four three two double one"), "9876543211")
        self.assertEqual(extract.extract_phone("डबल नौ आठ सात छह पांच चार तीन दो एक"), "9987654321")

    def test_fewer_or_more_digits_are_not_a_number(self):
        for text in ("nine eight seven six five four three two one",               # 9 digits
                     "nine eight seven six five four three two one zero zero",       # 11, no leading 0 / 91
                     "nine eight seven six five four three two one zero one two"):   # 13
            with self.subTest(text=text):
                self.assertIsNone(extract.extract_phone(text))
                self.assertIsNone(extract.phone_in_words(text))

    def test_other_number_words_in_a_sentence_are_not_a_phone(self):
        for text in ("book at five pm tomorrow", "book Priya at eleven p.m. on Saturday",
                     "book at 5 nine eight seven six five four three two one zero",    # one run of 11: never guessed
                     "I want two appointments, one at five and one at six",
                     "token number three", "book for 15 October at five"):
            with self.subTest(text=text):
                self.assertIsNone(extract.extract_phone(text))

    def test_a_run_broken_by_another_word_is_not_joined(self):
        self.assertIsNone(extract.phone_in_words("nine eight seven six five at four three two one zero"))
        self.assertIsNone(extract.phone_in_words("nine eight seven 5 pm six five four three two one zero"))

    def test_two_different_numbers_are_ambiguous(self):
        self.assertIsNone(extract.phone_in_words(
            "nine eight seven six five four three two one zero or nine eight seven six five four three two one one"))

    def test_digits_still_read_as_before(self):
        self.assertEqual(extract.extract_phone("number is 9876543210"), PHONE)
        self.assertEqual(extract.extract_phone("number is 98765 43210"), PHONE)
        self.assertEqual(extract.extract_phone("number 98765432"), "98765432")       # a short run still comes through
        self.assertIsNone(extract.extract_phone("age 40 token 5"))

    def test_the_answer_reader_keeps_its_name_and_behaviour(self):
        self.assertEqual(voice_context.spoken_phone("zero nine eight seven six five four three two one zero"), PHONE)
        self.assertIsNone(voice_context.spoken_phone("98765"))

    def test_the_phone_is_filled_by_the_parser_on_the_rules_route(self):
        text = "book an appointment for Priya, mobile number is nine eight seven six five four three two one zero"
        intent, slots = parse(text)
        self.assertEqual(intent, "book_appointment")
        self.assertEqual(slots["patient_phone"], PHONE)

    def test_the_tool_mapping_prefers_the_deterministic_reader(self):
        ctx = tools.ToolContext(None, date.today(), LIVE_COMMAND)
        _, slots = tools.to_parse_result("book_appointment", {"patient_name": "Priya"}, ctx)
        self.assertEqual(slots["patient_phone"], PHONE)
        _, slots = tools.to_parse_result("book_appointment", {"patient_name": "Priya", "phone": "9111111111"}, ctx)
        self.assertEqual(slots["patient_phone"], PHONE)
        plain = tools.ToolContext(None, date.today(), "book Priya tomorrow")
        _, slots = tools.to_parse_result("book_appointment", {"patient_name": "Priya", "phone": "9111111111"}, plain)
        self.assertEqual(slots["patient_phone"], "9111111111")                 # nothing said: the model's stands


class DottedMeridiem(unittest.TestCase):
    def test_every_spelling_reads_the_same(self):
        cases = {
            "at 11 p.m.": "23:00", "11 P.M.": "23:00", "11 pm": "23:00", "11p.m.": "23:00", "11 p.m": "23:00",
            "11 pm.": "23:00", "11 p m": "23:00", "at 11 a.m.": "11:00", "11 A.M.": "11:00", "11 a m": "11:00",
            "11am": "11:00", "eleven am": "11:00", "eleven p.m.": "23:00", "11:30 p.m.": "23:30", "11:30 pm": "23:30",
            "12 a.m.": "00:00", "12 am": "00:00", "12 p.m.": "12:00", "12 pm": "12:00", "5 p.m.": "17:00",
            "Coming Saturday at 11 p.m.": "23:00", "at 5": "17:00", "5 baje": "05:00",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(voice_context.spoken_time(text), expected)
                self.assertEqual(voice_context.answer_time(text), expected)

    def test_ordinary_words_are_not_turned_into_meridiems(self):
        self.assertEqual(datetime_extract.normalize_meridiem("I am at a mall at 5"), "i am at a mall at 5")
        self.assertEqual(voice_context.spoken_time("a morning at 7"), "19:00")          # unchanged: the clinic-hours "at 7"

    def test_the_answer_to_which_day_carries_the_date_and_the_time(self):
        text = "Coming Saturday at 11 p.m."
        self.assertEqual(datetime_extract.extract_appt_date(text, date(2026, 10, 7)), "2026-10-10")
        self.assertEqual(voice_context.answer_time(text), "23:00")

    def test_the_card_edit_and_the_planner_cross_check_read_it_too(self):
        self.assertEqual(voice_context.extract_card_edits("book_appointment", "make it 11 p.m.")["start_time"], "23:00")
        args, notes = date_guard.crosscheck("book_appointment", {"patient_name": "Priya", "time": "11:00"},
                                            "book Priya at 11 p.m.", date(2026, 10, 7))
        self.assertEqual(args["time"], "23:00")
        self.assertEqual(date_guard.time_phrase_count("at 11 p.m."), 1)

    def test_the_keyword_router_sees_a_time_in_dotted_forms(self):
        for text in ("cancel the 11 p.m. one", "cancel the 11 P.M. one", "cancel the 11 pm one", "cancel the 5 a.m. one",
                     "cancel the 11 p m one"):
            with self.subTest(text=text):
                self.assertTrue(classify._has_time_hint(text.lower()))
                self.assertEqual(classify.classify(text), "cancel_appointment")
        self.assertFalse(classify._has_time_hint("cancel the follow-up for a month"))


if __name__ == "__main__":
    unittest.main()
