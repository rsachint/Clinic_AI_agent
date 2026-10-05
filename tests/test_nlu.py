import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic.nlu import datetime_extract, extract
from clinic.nlu.classify import _RULES as _STAGE1_RULES
from clinic.nlu.classify import classify
from clinic.nlu.intent_llm import KNOWN_INTENTS
from clinic.nlu.parser import UnrecognizedCommand, parse


class ClassifyTests(unittest.TestCase):
    def test_register_patient_devanagari(self):
        self.assertEqual(classify("नया पेशेंट सुनीता देवी, 34 साल"), "register_patient")

    def test_record_visit_english(self):
        self.assertEqual(classify("Sunita ji ka consultation 300 rupees"), "record_visit")

    def test_missed_followups(self):
        self.assertEqual(classify("aaj kaun nahi aaya"), "missed_followups")

    def test_unrecognized_returns_none(self):
        self.assertIsNone(classify("random unrelated sentence about the weather"))

    def test_registration_mentioning_mobile_number_is_not_a_lookup(self):
        text = "Can you register a new patient with the name Aman Raghu? His mobile number is."
        self.assertEqual(classify(text), "register_patient")

    def test_patient_lookup_general_phrasing(self):
        self.assertEqual(classify("Can you retrieve the patient details for Rakesh Verma?"), "patient_lookup")

    def test_patient_lookup_fallback_phrasing(self):
        text = "Can you retrieve the information about the patient Manjul Agarwal?"
        self.assertEqual(classify(text), "patient_lookup")

    # -- Part 1: voice-reachable follow-up cancel/reschedule --------------

    def test_cancel_followup_voice_english(self):
        self.assertEqual(classify("Cancel Sunita's follow-up please"), "cancel_followup")

    def test_cancel_followup_voice_hinglish(self):
        self.assertEqual(classify("Sunita ka follow up cancel karo"), "cancel_followup")

    def test_reschedule_followup_voice_english(self):
        self.assertEqual(classify("Reschedule the follow-up to next week"), "reschedule_followup")

    def test_reschedule_followup_voice_hindi(self):
        self.assertEqual(classify("फॉलो अप रीशेड्यूल करो"), "reschedule_followup")

    def test_confirm_followup_has_no_voice_rule(self):
        # Deliberately left unresolved (plan §13.1: does a confirmation
        # write anything at all?) -- must never become voice-reachable here.
        self.assertNotEqual(classify("Haan main aaunga"), "confirm_followup")
        self.assertIsNone(classify("Haan main aaunga"))

    # -- Part 2: appointment intents & the appointment-vs-followup split --

    def test_book_appointment_explicit_phrase(self):
        self.assertEqual(classify("Book an appointment for Sunita tomorrow at 11am"), "book_appointment")

    def test_book_appointment_bare_appointment_word(self):
        self.assertEqual(classify("Sunita ke liye ek appointment chahiye kal 11 baje"), "book_appointment")

    def test_cancel_appointment_wins_over_cancel_followup(self):
        self.assertEqual(classify("Cancel my appointment for tomorrow"), "cancel_appointment")

    def test_cancel_followup_still_wins_when_no_appointment_language(self):
        self.assertEqual(classify("Cancel my follow-up"), "cancel_followup")

    def test_reschedule_appointment_wins_over_reschedule_followup(self):
        self.assertEqual(classify("Reschedule my appointment to Monday"), "reschedule_appointment")

    def test_reschedule_followup_still_wins_when_no_appointment_language(self):
        self.assertEqual(classify("Reschedule the follow-up to next week"), "reschedule_followup")

    def test_cancel_appointment_via_time_hint_without_the_word_appointment(self):
        # The documented, only-partially-solved gap: a bare time reference is
        # enough to win the disambiguation even without saying "appointment".
        self.assertEqual(classify("Cancel the 11 baje wala"), "cancel_appointment")

    def test_check_availability_explicit_time(self):
        self.assertEqual(classify("Is 10am Tuesday free?"), "check_availability")

    def test_check_availability_generic_slots_phrase(self):
        self.assertEqual(classify("What slots are free tomorrow?"), "check_availability")

    def test_next_appointment_phrase(self):
        self.assertEqual(classify("What's Sunita's next appointment?"), "next_appointment")

    def test_list_appointments_today(self):
        self.assertEqual(classify("What's scheduled today?"), "list_appointments")

    def test_list_appointments_this_week(self):
        self.assertEqual(classify("What's scheduled this week?"), "list_appointments")

    def test_existing_intents_unaffected_by_appointment_rules(self):
        # Regression guard: none of the pre-existing 9 intents should ever
        # be preempted by the new appointment-vs-followup disambiguation.
        self.assertEqual(classify("Sunita ji ka consultation 300 rupees"), "record_visit")
        self.assertEqual(classify("15 din baad bulao Sunita ko"), "set_followup")


class DateTimeExtractTests(unittest.TestCase):
    def test_kal_resolves_to_tomorrow(self):
        today = date(2026, 9, 26)
        self.assertEqual(
            datetime_extract.extract_appt_date("kal aana", today=today),
            (today + timedelta(days=1)).isoformat(),
        )

    def test_aaj_resolves_to_today(self):
        today = date(2026, 9, 26)
        self.assertEqual(datetime_extract.extract_appt_date("aaj aana", today=today), today.isoformat())

    def test_aaj_with_weekday_is_today_not_next_occurrence(self):
        # 2026-09-28 is a Monday.
        today = date(2026, 9, 28)
        self.assertEqual(datetime_extract.extract_appt_date("aaj Monday hai", today=today), today.isoformat())

    def test_bare_weekday_is_next_occurrence_not_today(self):
        # Said on a Monday, a bare "Monday" (no "aaj") means next Monday.
        today = date(2026, 9, 28)
        expected = (today + timedelta(days=7)).isoformat()
        self.assertEqual(datetime_extract.extract_appt_date("book on Monday", today=today), expected)

    def test_bare_weekday_later_this_week(self):
        # 2026-09-28 is Monday; "Thursday" should resolve to that same week.
        today = date(2026, 9, 28)
        expected = (today + timedelta(days=3)).isoformat()
        self.assertEqual(datetime_extract.extract_appt_date("Thursday please", today=today), expected)

    def test_hindi_weekday(self):
        today = date(2026, 9, 28)  # Monday
        expected = (today + timedelta(days=1)).isoformat()
        self.assertEqual(datetime_extract.extract_appt_date("मंगलवार को", today=today), expected)

    def test_absolute_date_day_month_word(self):
        today = date(2026, 8, 1)
        self.assertEqual(datetime_extract.extract_appt_date("25 September please", today=today), "2026-09-25")

    def test_absolute_date_day_month_word_rolls_to_next_year_if_already_past(self):
        # "25 September" said when today is already past Sept 25 this year
        # -- rather than guess it means a year-old date, roll to next year's
        # occurrence (same rule bare weekday names get: never resolve into
        # the past for a future booking).
        today = date(2026, 9, 26)
        self.assertEqual(datetime_extract.extract_appt_date("25 September please", today=today), "2027-09-25")

    def test_absolute_date_slash_form(self):
        today = date(2026, 9, 1)
        self.assertEqual(datetime_extract.extract_appt_date("25/09", today=today), "2026-09-25")

    def test_absolute_date_with_year(self):
        today = date(2026, 9, 1)
        self.assertEqual(datetime_extract.extract_appt_date("25-09-2027", today=today), "2027-09-25")

    def test_no_date_returns_none(self):
        self.assertIsNone(datetime_extract.extract_appt_date("book an appointment please"))

    def test_baje_bare_hour(self):
        self.assertEqual(datetime_extract.extract_appt_time("11 baje aana"), "11:00")

    def test_am_explicit(self):
        self.assertEqual(datetime_extract.extract_appt_time("11am"), "11:00")

    def test_pm_with_minutes(self):
        self.assertEqual(datetime_extract.extract_appt_time("3:30 pm"), "15:30")

    def test_shaam_qualifier_shifts_to_pm(self):
        self.assertEqual(datetime_extract.extract_appt_time("शाम 4 बजे"), "16:00")

    def test_subah_qualifier_stays_am(self):
        self.assertEqual(datetime_extract.extract_appt_time("सुबह 4 बजे"), "04:00")

    def test_dopahar_qualifier_shifts_to_pm(self):
        self.assertEqual(datetime_extract.extract_appt_time("दोपहर 2 बजे"), "14:00")

    def test_raat_qualifier_shifts_to_pm(self):
        self.assertEqual(datetime_extract.extract_appt_time("raat 9 baje"), "21:00")

    def test_twelve_baje_is_noon_not_midnight(self):
        for text in ("सुबह 12 बजे", "subah 12 baje", "दोपहर 12 बजे", "12 बजे", "12 baje", "12:30 बजे सुबह"):
            with self.subTest(text=text):
                self.assertTrue(datetime_extract.extract_appt_time(text).startswith("12:"), text)

    def test_raat_twelve_baje_is_midnight(self):
        self.assertEqual(datetime_extract.extract_appt_time("रात 12 बजे"), "00:00")
        self.assertEqual(datetime_extract.extract_appt_time("raat 12 baje"), "00:00")

    def test_explicit_twelve_am_and_pm_keep_clock_meaning(self):
        self.assertEqual(datetime_extract.extract_appt_time("12 pm"), "12:00")
        self.assertEqual(datetime_extract.extract_appt_time("12 am"), "00:00")

    def test_other_hours_with_qualifiers_are_unchanged(self):
        self.assertEqual(datetime_extract.extract_appt_time("सुबह 11 बजे"), "11:00")
        self.assertEqual(datetime_extract.extract_appt_time("शाम 4 बजे"), "16:00")
        self.assertEqual(datetime_extract.extract_appt_time("सुबह 4 बजे"), "04:00")

    def test_no_time_returns_none(self):
        self.assertIsNone(datetime_extract.extract_appt_time("book an appointment for tomorrow"))


class ExtractTests(unittest.TestCase):
    def test_phone_extraction_strips_spaces(self):
        self.assertEqual(extract.extract_phone("call me at 9 8 7 6 5 4 3 2 1 0 please"), "9876543210")

    def test_phone_extraction_ignores_short_runs(self):
        self.assertIsNone(extract.extract_phone("room number 42"))

    def test_age_extraction_devanagari(self):
        self.assertEqual(extract.extract_age("34 साल"), 34)

    def test_age_extraction_after_age_is_phrasing(self):
        # Regression: a phone number's trailing digits sitting right before
        # the word "age" used to be misread as the age itself.
        text = "Register a new patient Manjul Agarwal mobile number is 9876500401 age is 59."
        self.assertEqual(extract.extract_age(text), 59)

    def test_age_extraction_hindi_word_form(self):
        self.assertEqual(extract.extract_age("चालीस साल"), 40)

    def test_amount_extraction(self):
        self.assertEqual(extract.extract_amount("consultation 300 rupees"), 300.0)

    def test_amount_extraction_rupee_symbol_prefix(self):
        self.assertEqual(extract.extract_amount("Priyasha ka consultation ₹500."), 500.0)

    def test_days_extraction(self):
        self.assertEqual(extract.extract_days("pandrah din baad, 15 din baad bulao"), 15)


class ParseTests(unittest.TestCase):
    @patch("clinic.nlu.parser.extract_name", return_value="Sunita Devi")
    def test_register_patient_parses_full_slots(self, _mock_name):
        intent, slots = parse("naya patient Sunita Devi, 34 साल, 9876543210")
        self.assertEqual(intent, "register_patient")
        self.assertEqual(slots["name"], "Sunita Devi")
        self.assertEqual(slots["phone"], "9876543210")
        self.assertEqual(slots["age"], 34)

    @patch("clinic.nlu.parser.extract_name", return_value="Sunita")
    def test_record_visit_parses_fee(self, _mock_name):
        intent, slots = parse("Sunita ji ka consultation 300 rupees")
        self.assertEqual(intent, "record_visit")
        self.assertEqual(slots["fee_rupees"], 300.0)

    @patch("clinic.nlu.parser.pick_intent", return_value=None)
    def test_unrecognized_command_raises(self, _mock_pick_intent):
        # Stage 1 (classify) and the phone fallback both miss here, so
        # parse() falls to Stage 2 (clinic.nlu.parser.pick_intent) -- mocked
        # to "unclear"/None here rather than hitting the real local Ollama
        # model, per this codebase's existing convention of never making a
        # live LLM call from the automated test suite.
        with self.assertRaises(UnrecognizedCommand):
            parse("the weather is nice today")

    def test_phone_number_alone_falls_back_to_register_patient(self):
        # no intent keyword present -- classifier alone would fail, but a
        # bare phone number is registration-only, so parse() should recover
        intent, slots = parse("\u091a\u094c\u0902\u0924\u0940\u0938 \u0938\u093e\u0932, 9876543210\u0964")
        self.assertEqual(intent, "register_patient")
        self.assertEqual(slots["phone"], "9876543210")
        self.assertEqual(slots["age"], 34)

    @patch("clinic.nlu.parser.extract_name", return_value="Sunita")
    def test_cancel_followup_parses_patient_name_only(self, _mock_name):
        intent, slots = parse("Cancel Sunita's follow-up please")
        self.assertEqual(intent, "cancel_followup")
        self.assertEqual(slots, {"patient_name": "Sunita"})

    @patch("clinic.nlu.parser.extract_name", return_value="Sunita")
    def test_reschedule_followup_parses_name_and_date(self, _mock_name):
        intent, slots = parse("Reschedule Sunita's follow-up to kal")
        self.assertEqual(intent, "reschedule_followup")
        self.assertEqual(slots["patient_name"], "Sunita")
        self.assertEqual(slots["new_due_date"], (date.today() + timedelta(days=1)).isoformat())

    @patch("clinic.nlu.parser.extract_name", return_value="Sunita")
    def test_book_appointment_parses_date_time_and_phone(self, _mock_name):
        intent, slots = parse("Book an appointment for Sunita tomorrow 11am, phone 9876543210")
        self.assertEqual(intent, "book_appointment")
        self.assertEqual(slots["patient_name"], "Sunita")
        self.assertEqual(slots["patient_phone"], "9876543210")
        self.assertEqual(slots["start_time"], "11:00")
        self.assertEqual(slots["appt_date"], (date.today() + timedelta(days=1)).isoformat())

    @patch("clinic.nlu.parser.extract_name", return_value="Sunita")
    def test_cancel_appointment_parses_patient_name_only(self, _mock_name):
        intent, slots = parse("Cancel my appointment, this is Sunita")
        self.assertEqual(intent, "cancel_appointment")
        self.assertEqual(slots, {"patient_name": "Sunita"})

    def test_check_availability_parses_date(self):
        intent, slots = parse("What slots are free tomorrow?")
        self.assertEqual(intent, "check_availability")
        self.assertEqual(slots["appt_date"], (date.today() + timedelta(days=1)).isoformat())

    @patch("clinic.nlu.parser.extract_name", return_value=None)
    def test_list_appointments_defaults_to_today(self, _mock_name):
        intent, slots = parse("What's scheduled today?")
        self.assertEqual(intent, "list_appointments")
        self.assertEqual(slots["range"], "today")

    @patch("clinic.nlu.parser.extract_name", return_value=None)
    def test_list_appointments_detects_week(self, _mock_name):
        intent, slots = parse("What's scheduled this week?")
        self.assertEqual(intent, "list_appointments")
        self.assertEqual(slots["range"], "week")

    @patch("clinic.nlu.parser.extract_name", return_value="Sunita")
    def test_next_appointment_parses_patient_name(self, _mock_name):
        intent, slots = parse("What's Sunita's next appointment?")
        self.assertEqual(intent, "next_appointment")
        self.assertEqual(slots, {"patient_name": "Sunita"})


class Stage2IntentPickerTests(unittest.TestCase):
    """Stage 2 (clinic.nlu.intent_llm.pick_intent) is mocked at the
    clinic.nlu.parser.pick_intent call site throughout -- never a live
    Ollama call from the automated test suite, per this codebase's existing
    convention (see llm_slots.extract_name, which is mocked the same way in
    ParseTests above)."""

    @patch("clinic.nlu.parser.pick_intent")
    @patch("clinic.nlu.parser.extract_name", return_value="Sunita")
    def test_stage1_miss_triggers_stage2_call(self, _mock_name, mock_pick_intent):
        mock_pick_intent.return_value = "record_visit"
        # No Stage 1 keyword (from any _RULES list, the appointment-vs-
        # followup combo checks, or the patient_lookup fallback) appears in
        # this text, and it has no 8-10 digit run either -- classify() and
        # the phone fallback both genuinely miss, so parse() must reach
        # Stage 2.
        text = "kuch bhi ajeeb sentence bolo jisme koi bhi jaana pehchana keyword na ho"
        parse(text)
        mock_pick_intent.assert_called_once_with(text, hint=None)

    @patch("clinic.nlu.parser.pick_intent", return_value="record_visit")
    @patch("clinic.nlu.parser.extract_name", return_value="Sunita")
    def test_stage2_known_intent_flows_through_same_slot_extraction(self, _mock_name, _mock_pick_intent):
        intent, slots = parse("koi bhi ajeeb sentence jisme keyword na ho \u20b9500")
        self.assertEqual(intent, "record_visit")
        self.assertEqual(slots["patient_name"], "Sunita")
        self.assertEqual(slots["fee_rupees"], 500.0)

    @patch("clinic.nlu.parser.pick_intent", return_value="unclear")
    def test_stage2_unclear_falls_through_to_unrecognized(self, _mock_pick_intent):
        # pick_intent() itself never returns the literal string "unclear"
        # (see intent_llm.py -- it maps that to None), but parse() must
        # still treat anything outside the known label set the same way:
        # as no match, not a crash.
        with self.assertRaises(UnrecognizedCommand):
            parse("something totally unrelated to any clinic action")

    @patch("clinic.nlu.parser.pick_intent", return_value=None)
    def test_stage2_failure_falls_through_without_crashing(self, _mock_pick_intent):
        # pick_intent()'s OWN contract (see clinic/nlu/intent_llm.py) is to
        # catch any httpx/JSON/timeout failure itself and return None --
        # parser.py never sees a raised exception from it, only None or a
        # known label. This test covers parser.py's side of that contract;
        # tests/test_intent_llm.py covers pick_intent() actually catching a
        # mocked httpx.post failure and turning it into None.
        with self.assertRaises(UnrecognizedCommand):
            parse("something totally unrelated to any clinic action")

    def test_known_intents_cover_every_classify_producible_label(self):
        # Sanity check that the closed enum Stage 2 is constrained to is
        # actually a superset of everything Stage 1 can produce, plus
        # "unclear" -- if a new intent is ever added to classify.py without
        # updating intent_llm.py's constant, this fails loudly.
        stage1_intents = {intent for intent, _ in _STAGE1_RULES}
        stage1_intents |= {
            "cancel_appointment", "reschedule_appointment", "check_availability",
            "book_appointment", "list_appointments", "next_appointment",
        }
        self.assertTrue(stage1_intents.issubset(set(KNOWN_INTENTS)))
        self.assertIn("unclear", KNOWN_INTENTS)


if __name__ == "__main__":
    unittest.main()


class WordedDateTests(unittest.TestCase):
    """Spelled-out and month-first dates -- the sentence that left the date
    blank on the review card: 'fourth of October at 1 PM'."""

    TODAY = date(2026, 10, 3)

    def _date(self, text):
        return datetime_extract.extract_appt_date(text, today=self.TODAY)

    def test_the_sentence_that_left_the_date_blank(self):
        text = "Can you book an appointment for a patient name Amit for fourth of October at 1 PM?"
        self.assertEqual(self._date(text), "2026-10-04")
        self.assertEqual(datetime_extract.extract_appt_time(text), "13:00")

    def test_spelled_out_days_with_and_without_of(self):
        self.assertEqual(self._date("fourth october"), "2026-10-04")
        self.assertEqual(self._date("first of November"), "2026-11-01")
        self.assertEqual(self._date("thirty first of December"), "2026-12-31")

    def test_compound_days(self):
        self.assertEqual(self._date("twenty fifth of October"), "2026-10-25")
        self.assertEqual(self._date("twenty-fifth october"), "2026-10-25")
        self.assertEqual(self._date("twenty five october at 4 pm"), "2026-10-25")

    def test_month_first_order(self):
        self.assertEqual(self._date("October 4"), "2026-10-04")
        self.assertEqual(self._date("oct 4th"), "2026-10-04")
        self.assertEqual(self._date("October the fourth"), "2026-10-04")

    def test_hinglish_and_devanagari(self):
        self.assertEqual(self._date("chaar october ko"), "2026-10-04")
        self.assertEqual(self._date("pandrah october"), "2026-10-15")
        self.assertEqual(self._date("5 अक्टूबर को"), "2026-10-05")
        self.assertEqual(self._date("अक्टूबर 4 को"), "2026-10-04")
        self.assertEqual(self._date("पांच अक्टूबर"), "2026-10-05")

    def test_past_day_rolls_to_next_year(self):
        self.assertEqual(self._date("second of September"), "2027-09-02")

    def test_impossible_and_incomplete_dates_stay_blank(self):
        self.assertIsNone(self._date("february 30"))
        # A day with no month now means the current month (clinic rule).
        self.assertEqual(self._date("the 4th"), "2026-10-04")
        self.assertIsNone(self._date("I may book it later"))

    def test_existing_phrasings_are_unchanged(self):
        self.assertEqual(self._date("4th October"), "2026-10-04")
        self.assertEqual(self._date("4/10"), "2026-10-04")
        self.assertEqual(self._date("kal subah"), "2026-10-04")
        self.assertEqual(self._date("Monday"), "2026-10-05")


class BareDayAndWordedTimeTests(unittest.TestCase):
    """A day with no month means the CURRENT month; Hindi number words work
    for times as well as digits (speech-to-text writes either)."""

    TODAY = __import__("datetime").date(2026, 10, 4)

    def date(self, text):
        return datetime_extract.extract_appt_date(text, today=self.TODAY)

    def test_day_with_tareekh_is_the_current_month(self):
        self.assertEqual(self.date("सात तारीख को अपॉइंटमेंट"), "2026-10-07")
        self.assertEqual(self.date("7 तारीख के सारे अपॉइंटमेंट"), "2026-10-07")
        self.assertEqual(self.date("saat tareekh ko appointment"), "2026-10-07")
        self.assertEqual(self.date("7th ko appointment"), "2026-10-07")

    def test_a_past_day_still_means_this_month(self):
        self.assertEqual(self.date("दो तारीख के अपॉइंटमेंट"), "2026-10-02")

    def test_a_day_that_does_not_exist_this_month_is_not_guessed(self):
        september = __import__("datetime").date(2026, 9, 10)  # September has 30 days
        self.assertIsNone(datetime_extract.extract_appt_date("इकतीस तारीख", today=september))
        self.assertEqual(self.date("इकतीस तारीख"), "2026-10-31")  # October has 31

    def test_a_named_month_still_wins(self):
        self.assertEqual(self.date("सात नवंबर को"), "2026-11-07")

    def test_ordinary_words_are_not_dates(self):
        self.assertIsNone(self.date("the first patient and the second patient"))

    def test_hindi_number_words_work_for_times(self):
        for text, expected in (("सुबह दस बजे", "10:00"), ("शाम चार बजे", "16:00"), ("एक बजे", "01:00"),
                               ("सुबह ग्यारह बजे", "11:00"), ("दोपहर बारह बजे", "12:00"),
                               ("teen baje", "03:00"), ("shaam paanch baje", "17:00")):
            with self.subTest(text=text):
                self.assertEqual(datetime_extract.extract_appt_time(text), expected)

    def test_spoken_fractions(self):
        for text, expected in (("साढ़े तीन बजे", "03:30"), ("साढ़े तीन बजे शाम", "15:30"), ("डेढ़ बजे", "01:30"),
                               ("ढाई बजे", "02:30"), ("सवा दो बजे शाम", "14:15"), ("पौने पांच बजे शाम", "16:45"),
                               ("saade teen baje", "03:30")):
            with self.subTest(text=text):
                self.assertEqual(datetime_extract.extract_appt_time(text), expected)

    def test_digit_times_are_unchanged(self):
        self.assertEqual(datetime_extract.extract_appt_time("11 baje aana"), "11:00")
        self.assertEqual(datetime_extract.extract_appt_time("3:30 pm"), "15:30")

    def test_the_original_sentence_gets_both_date_and_time(self):
        text = "एक नया पेशेंट अनुराग का सात तारीख को सुबह एक बजे का अपॉइंटमेंट फिक्स करो।"
        self.assertEqual(self.date(text), "2026-10-07")
        self.assertEqual(datetime_extract.extract_appt_time(text), "01:00")

    def test_unreadable_date_detector(self):
        today = self.TODAY
        self.assertTrue(datetime_extract.mentions_unreadable_date("tareekh ke appointments batao", today))
        september = __import__("datetime").date(2026, 9, 10)
        self.assertTrue(datetime_extract.mentions_unreadable_date("इकतीस तारीख के अपॉइंटमेंट", september))
        self.assertFalse(datetime_extract.mentions_unreadable_date("सात तारीख के अपॉइंटमेंट", today))
        self.assertFalse(datetime_extract.mentions_unreadable_date("show appointments", today))
        self.assertFalse(datetime_extract.mentions_unreadable_date("may I see the appointments", today))


class EnglishWordedTimeTests(unittest.TestCase):
    """Times spoken in English words ("one PM"), which the digit and Hindi
    patterns used to miss, so the voice flow asked "What time?" again."""

    def time(self, text):
        return datetime_extract.extract_appt_time(text)

    def test_the_reported_sentence(self):
        text = "Create an appointment for a patient called Naman on 6th of October at one PM."
        self.assertEqual(self.time(text), "13:00")

    def test_hour_words_with_am_pm(self):
        for text, expected in (
            ("at two pm", "14:00"), ("at one p.m.", "13:00"), ("at ten am", "10:00"),
            ("at twelve pm", "12:00"), ("at twelve am", "00:00"),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.time(text), expected)

    def test_minutes_and_part_of_day(self):
        for text, expected in (
            ("at one thirty PM", "13:30"), ("one forty-five pm", "13:45"),
            ("one in the afternoon", "13:00"), ("six in the evening", "18:00"),
            ("nine in the morning", "09:00"),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.time(text), expected)

    def test_half_and_quarter(self):
        for text, expected in (
            ("quarter to five pm", "16:45"), ("quarter past nine in the morning", "09:15"),
            ("half past four pm", "16:30"),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.time(text), expected)

    def test_a_number_word_that_is_not_a_time_is_left_alone(self):
        for text in ("book one patient tomorrow", "ten patients are waiting", "at one", "two appointments"):
            with self.subTest(text=text):
                self.assertIsNone(self.time(text))
