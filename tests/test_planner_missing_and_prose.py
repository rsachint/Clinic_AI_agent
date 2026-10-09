"""Two defects found on one live command, "I want to book a consultation for Priya, her mobile number is nine eight seven six
five four three two one zero." (no day, no time), each pinned here in BOTH architectures (classic and model-first):

  1. The model answered with a correct booking call that lacked the day (and time). The validator threw the whole call
     away ("book_appointment needs date"), the keyword rules guessed (a consultation is not a booking, a one-word name
     was not read) and the user heard "I couldn't tell who the patient is". A call that is valid except for details the
     APP ITSELF ASKS FOR (the patient, the day, the time: clinic/voice_context.py next_question) is now accepted, so the
     existing questions are asked and what was said is kept (Tool.askable, clinic/nlu/tools.py).
  2. The model answered in plain words ("What date and time would you like?") with no tool call; the words were dropped
     and the rules guessed. A short plain question now becomes the assistant's own clarifying question and the raw words
     are logged (clinic/nlu/prose.py); a reply that claims an action, runs long, looks like code, a link or an id, or
     sends the user elsewhere is not shown, only logged.

Nothing here reaches a model, Sarvam or the network: the planner is a FakeBackend (or Sarvam's backend on a fake HTTP
transport). Dates are relative to the day the fixtures build ("tomorrow"), never to a fixed one."""
import json
import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.planner_support import PlannerCase  # noqa: E402

from clinic import architecture, voice_context  # noqa: E402
from clinic.nlu import planner, prose, sarvam, tools  # noqa: E402
from clinic.pipeline import ParsedResult, PipelineError  # noqa: E402
from clinic.voice_context import AskResult  # noqa: E402
from clinic.voice_turns import handle_pick  # noqa: E402

LIVE = ("I want to book a consultation for Priya, her mobile number is "
        "nine eight seven six five four three two one zero.")
PHONE = "9876543210"
QUESTION = "What date and time would you like?"
MODES = ("classic", "model_first")


# -- the validator -----------------------------------------------------------------------------

class WhichToolsAreLenient(unittest.TestCase):
    """Lenient = a required argument the app asks for. Everything else keeps the strict rejection."""

    LENIENT = {
        "book_appointment": {"patient_name", "date", "time"},
        "reschedule_appointment": {"patient_name"},
        "cancel_appointment": {"patient_name"},
        "record_visit": {"patient_name"},
        "set_followup": {"patient_name"},
        "cancel_followup": {"patient_name"},
        "reschedule_followup": {"patient_name"},
    }

    def validate(self, name, args):
        return tools.validate(name, args, tools.ToolContext(None, None, ""))

    def test_exactly_these_tools_are_lenient_about_exactly_these_arguments(self):
        found = {t.name: set(t.askable) for t in tools.TOOLS + tools.DIALOGUE_TOOLS if t.askable}
        self.assertEqual(found, self.LENIENT)
        for tool in tools.TOOLS:
            self.assertLessEqual(set(tool.askable), set(tool.required), tool.name)      # only a REQUIRED argument can be lenient

    def test_every_lenient_argument_is_one_the_app_really_asks_for(self):
        """next_question asks the patient for every patient intent, and the day and the time for a booking or move."""
        asked = {"patient_name": set(voice_context.PATIENT_INTENTS), "date": {"book_appointment", "reschedule_appointment"},
                 "time": {"book_appointment", "reschedule_appointment"}}
        for name, args in self.LENIENT.items():
            for arg in args:
                intents = tools.BY_NAME[name].intents
                self.assertTrue(intents <= asked[arg], (name, arg, intents))

    def test_a_call_missing_only_what_the_app_asks_for_is_accepted(self):
        for name, args in (("book_appointment", {"patient_name": "Priya", "phone": PHONE}),
                           ("book_appointment", {"patient_name": "Priya", "date": "2026-10-07"}),
                           ("book_appointment", {"patient_name": "Priya", "time": "17:00"}),
                           ("book_appointment", {"date": "2026-10-07", "time": "17:00"}),
                           ("book_appointment", {"phone": PHONE}),
                           ("reschedule_appointment", {"new_date": "2026-10-07"}),
                           ("cancel_appointment", {"date": "2026-10-07"}),
                           ("record_visit", {"fee": 300}),
                           ("set_followup", {"days": 5}),
                           ("reschedule_followup", {"new_date": "2026-10-07"})):
            with self.subTest(name=name, args=args):
                self.assertEqual(self.validate(name, args).keys(), args.keys())

    def test_a_call_with_nothing_in_it_is_still_rejected(self):
        for name in self.LENIENT:
            for empty in ({}, None, {"patient_name": "", "date": None, "time": []}):
                with self.subTest(name=name, args=empty):
                    with self.assertRaises(tools.ToolError) as caught:
                        self.validate(name, empty)
                    self.assertEqual(caught.exception.code, "missing_required")

    def test_what_the_app_has_no_question_for_stays_strict(self):
        for name, args in (("record_visit", {"patient_name": "Amit"}), ("set_followup", {"patient_name": "Amit"}),
                           ("log_expense", {"description": "tea"}), ("register_patient", {"phone": PHONE}),
                           ("register_staff", {"role": "nurse"}), ("log_attendance", {"staff_name": "Seema"}),
                           ("log_attendance", {"status": "present"}), ("queue_action", {"token": 3}),
                           ("close_branch", {"branch": "A"}), ("close_branch", {"start_date": "2026-10-07"}),
                           ("doctor_leave", {"doctor_name": "Rao"}), ("doctor_leave", {"start_date": "2026-10-07"}),
                           ("switch_branch", {}), ("clarify", {}), ("query", {"limit": 3})):
            with self.subTest(name=name, args=args):
                with self.assertRaises(tools.ToolError) as caught:
                    self.validate(name, args)
                self.assertEqual(caught.exception.code, "missing_required")

    def test_real_errors_are_still_rejected_even_when_a_detail_is_missing(self):
        for name, args, code in (
                ("book_appointment", {"patient_name": "Priya", "appointment_id": 3}, "unknown_arg"),     # an id, never
                ("book_appointment", {"patient_name": "Priya", "patient_id": 3}, "unknown_arg"),
                ("book_appointment", {"patient_name": "Priya", "mood": "happy"}, "unknown_arg"),
                ("book_appointment", {"patient_name": "Priya", "date": "tomorrow"}, "bad_value"),
                ("book_appointment", {"patient_name": "Priya", "time": "five pm"}, "bad_value"),
                ("book_appointment", {"patient_name": "Priya", "phone": "12"}, "bad_value"),
                ("book_appointment", {"patient_name": ["Priya"]}, "bad_value"),
                ("book_appointment", {"patient_name": 42}, "bad_value"),
                ("cancel_appointment", {"patient_name": "Amit", "appointment_id": 1}, "unknown_arg"),
                ("drop_everything", {"patient_name": "Priya"}, "unknown_tool")):
            with self.subTest(name=name, args=args):
                with self.assertRaises(tools.ToolError) as caught:
                    self.validate(name, args)
                self.assertEqual(caught.exception.code, code)

    def test_a_missing_detail_is_left_empty_never_invented(self):
        intent, slots = tools.to_parse_result("book_appointment", self.validate("book_appointment", {"patient_name": "Priya"}),
                                              tools.ToolContext(None, None, ""))
        self.assertEqual(intent, "book_appointment")
        self.assertEqual((slots["patient_name"], slots["appt_date"], slots["start_time"]), ("Priya", None, None))
        intent, slots = tools.to_parse_result("book_appointment", self.validate("book_appointment", {"date": "2026-10-07"}),
                                              tools.ToolContext(None, None, ""))
        self.assertIsNone(slots["patient_name"])

    def test_the_schema_the_model_sees_is_unchanged(self):
        schema = tools.BY_NAME["book_appointment"].schema()["function"]["parameters"]
        self.assertEqual(schema["required"], ["patient_name", "date", "time"])


# -- the live command, end to end ---------------------------------------------------------------

class TwoPriyas(PlannerCase):
    """The base fixtures plus two Priyas (Priya Shah and the Devanagari प्रिया शर्मा). Nobody has 9876543210."""

    def setUp(self):
        super().setUp()
        for name, phone in (("Priya Shah", "9123499999"), ("प्रिया शर्मा", "9123456780")):
            self.conn.execute("INSERT INTO patients (name, phone, age) VALUES (?, ?, 30)", (name, phone))
        self.conn.commit()
        self.day = self.tomorrow.isoformat()
        self.mode = "classic"

    def use(self, mode):
        self.mode = mode
        architecture.set_mode(self.conn, mode)
        self.ctx.pending = None

    def script(self, *answers):
        self.backend.script = list(answers)
        self.backend.calls = []

    def answer(self, tool, **args):
        """What the model says to the user's answer to an open question: in model-first it calls a conversation tool; in
        classic the keyword rules read the answer and the planner is not asked (this entry is then not consumed)."""
        return (tool, args)

    def last(self):
        return self.log_rows()[-1]

    def counts(self):
        return self.table_counts("appointments", "patients", "proposals", "audit_log")


class TheLiveCommandInBothArchitectures(TwoPriyas):
    def run_live_sentence(self, mode):
        self.use(mode)
        before = self.counts()
        self.script(("book_appointment", {"patient_name": "Priya", "phone": PHONE}),
                    ("answer_slot", {"slot": "date", "value": self.day}),
                    ("answer_slot", {"slot": "time", "value": "17:00"}))
        ask = self.say(LIVE)
        return before, ask

    def test_it_asks_which_day_and_keeps_what_was_said(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                _, ask = self.run_live_sentence(mode)
                self.assertIsInstance(ask, AskResult)
                self.assertEqual((ask.intent, ask.kind, ask.question), ("book_appointment", "date", "Which day?"))
                self.assertEqual(ask.slots["patient_name"], "Priya")
                self.assertEqual(ask.slots["patient_phone"], PHONE)                  # read from the sentence by code
                self.assertEqual((ask.slots["appt_date"], ask.slots["start_time"]), (None, None))
                self.assertEqual(self.ctx.pending["kind"], "date")

    def test_then_the_time_then_the_card_with_the_phone_filled(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                before, ask = self.run_live_sentence(mode)
                time_question = self.say("tomorrow")
                self.assertEqual((time_question.kind, time_question.question), ("time", "What time?"))
                self.assertEqual(time_question.slots["appt_date"], self.day)
                card = self.say("5 pm")
                self.assertIsInstance(card, ParsedResult)
                self.assertEqual(card.intent, "book_appointment")
                self.assertEqual((card.slots["patient_name"], card.slots["patient_phone"], card.slots["appt_date"],
                                  card.slots["start_time"]), ("Priya", PHONE, self.day, "17:00"))
                self.assertIsNone(self.ctx.pending)
                self.assertEqual(self.counts(), before)                              # a card is not a write: Approve is
                self.assertEqual(self.ctx.open_card["intent"], "book_appointment")
                self.conn.execute("DELETE FROM planner_log")

    def test_the_turn_is_a_planner_turn_not_a_fallback(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.conn.execute("DELETE FROM planner_log")
                self.run_live_sentence(mode)
                row = self.last()
                self.assertEqual((row["route_taken"], row["planner_tool"], row["final_intent"]),
                                 ("planner", "book_appointment", "book_appointment"))
                self.assertEqual(row["route_detail"], "mf:book_appointment" if mode == "model_first" else None)
                self.assertFalse(row["override_notes"])                              # nothing was rejected or overridden
                self.assertEqual(json.loads(row["planner_args_json"]), {"patient_name": "Priya", "phone": PHONE})
                self.assertEqual(len(self.backend.calls), 1)                         # one model call, no second asking

    def test_two_priyas_and_no_number_ask_which_one_then_the_day_then_the_time(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.use(mode)
                self.script(("book_appointment", {"patient_name": "Priya"}),
                            ("answer_slot", {"slot": "date", "value": self.day}),
                            ("answer_slot", {"slot": "time", "value": "17:00"}))
                ask = self.say("I want to book a consultation for Priya")
                self.assertEqual((ask.kind, ask.question), ("choose_patient", "Which one?"))
                self.assertEqual(sorted(o["patient_name"] for o in ask.options), ["Priya Shah", "प्रिया शर्मा"])
                picked = handle_pick(self.ctx, self.conn, 0, self.adapter, self.adapter, "en-IN", frozenset({"book_appointment"}))
                self.assertEqual(picked[1].question, "Which day?")
                self.assertEqual(self.say("tomorrow").question, "What time?")
                card = self.say("5 pm")
                self.assertIsInstance(card, ParsedResult)
                self.assertEqual(card.resolved["patient_label"], "Priya Shah (9123499999)")

    def test_a_missing_time_only_asks_what_time(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.use(mode)
                self.script(("book_appointment", {"patient_name": "Rakesh Verma", "date": self.day}))
                ask = self.say("book Rakesh Verma tomorrow")
                self.assertEqual((ask.kind, ask.question), ("time", "What time?"))
                self.assertEqual(ask.slots["appt_date"], self.day)
                self.assertEqual(self.last()["route_taken"], "planner")

    def test_a_missing_day_only_asks_which_day(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.use(mode)
                self.script(("book_appointment", {"patient_name": "Rakesh Verma", "time": "17:00"}))
                ask = self.say("book Rakesh Verma at 5 pm")
                self.assertEqual((ask.kind, ask.question), ("date", "Which day?"))
                self.assertEqual(ask.slots["start_time"], "17:00")

    def test_a_missing_patient_asks_which_patient_and_keeps_the_day_and_time(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.use(mode)
                self.script(("book_appointment", {"date": self.day, "time": "17:00"}))
                ask = self.say("book an appointment tomorrow at 5 pm")
                self.assertEqual(ask.kind, "patient")
                self.assertIn("Which patient?", ask.question)
                self.assertEqual((ask.slots["appt_date"], ask.slots["start_time"]), (self.day, "17:00"))

    def test_other_patient_tools_ask_which_patient_when_it_is_missing(self):
        for mode in MODES:
            for name, args, text in (("cancel_appointment", {"date": self.tomorrow.isoformat()}, "cancel tomorrow's appointment"),
                                     ("reschedule_appointment", {"new_date": self.tomorrow.isoformat()}, "move it to tomorrow"),
                                     ("record_visit", {"fee": 300}, "visit fee 300"),
                                     ("set_followup", {"days": 5}, "follow up after 5 days")):
                with self.subTest(mode=mode, tool=name):
                    self.use(mode)
                    self.script((name, args))
                    result = self.say(text)
                    self.assertIsInstance(result, AskResult)
                    self.assertEqual(result.kind, "patient")

    def test_a_visit_without_a_fee_is_still_thrown_away_and_the_rules_take_over(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.conn.execute("DELETE FROM planner_log")
                self.use(mode)
                self.script(("record_visit", {"patient_name": "Rakesh Verma"}))
                self.say("book Rakesh Verma tomorrow at 5 pm")
                row = self.last()
                self.assertNotEqual((row["route_taken"], row["planner_tool"]), ("planner", "record_visit"))
                self.assertIn("missing_required", row["override_notes"])

    def test_a_call_with_nothing_in_it_is_thrown_away(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.conn.execute("DELETE FROM planner_log")
                self.use(mode)
                self.script(("book_appointment", {}))
                with self.assertRaises(PipelineError):
                    self.say("hello there")
                self.assertIn("missing_required", self.last()["override_notes"])
                self.assertNotEqual(self.last()["route_taken"], "planner")

    def test_an_id_from_the_model_is_still_refused_when_a_detail_is_missing(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.conn.execute("DELETE FROM planner_log")
                self.use(mode)
                before = self.counts()
                self.script(("book_appointment", {"patient_name": "Rakesh Verma", "patient_id": 1}))
                result = self.say("book Rakesh Verma tomorrow at 5 pm")
                self.assertIn("unknown_arg", self.last()["override_notes"])
                self.assertNotEqual(self.last()["route_taken"], "planner")
                self.assertEqual(self.counts(), before)
                self.assertNotIn("patient_id", getattr(result, "slots", {}) or {})


class TheDeterministicReadersStillWin(TwoPriyas):
    def run_book(self, text, args):
        run = planner.PlannerRun(self.conn, text, self.ctx, today=self.today, backend=planner.FakeBackend(("book_appointment", args)))
        return run, run.ask()

    def test_a_day_and_a_time_in_the_sentence_fill_what_the_model_left_out(self):
        run, planned = self.run_book("book Rakesh Verma tomorrow at 5 pm", {"patient_name": "Rakesh Verma"})
        self.assertEqual((planned.slots["appt_date"], planned.slots["start_time"]), (self.day, "17:00"))
        self.assertEqual(len(run.notes), 2)                                          # both are logged as read by code

    def test_the_sentence_outvotes_a_wrong_value_from_the_model(self):
        _, planned = self.run_book("book Rakesh Verma tomorrow at 5 pm",
                                   {"patient_name": "Rakesh Verma", "date": (self.today + timedelta(days=5)).isoformat(),
                                    "time": "09:00"})
        self.assertEqual((planned.slots["appt_date"], planned.slots["start_time"]), (self.day, "17:00"))

    def test_with_no_day_or_time_in_the_sentence_nothing_is_invented(self):
        run, planned = self.run_book(LIVE, {"patient_name": "Priya", "phone": PHONE})
        self.assertEqual((planned.slots["appt_date"], planned.slots["start_time"]), (None, None))
        self.assertEqual(planned.slots["patient_phone"], PHONE)
        self.assertEqual(run.notes, [])

    def test_so_a_sentence_with_both_is_never_asked_for_them(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.use(mode)
                self.script(("book_appointment", {"patient_name": "Rakesh Verma"}))
                card = self.say("book Rakesh Verma tomorrow at 5 pm")
                self.assertIsInstance(card, ParsedResult)
                self.assertEqual((card.slots["appt_date"], card.slots["start_time"]), (self.day, "17:00"))


# -- the plain-words reply ------------------------------------------------------------------------

class WhatMayBeShownAsAQuestion(unittest.TestCase):
    def test_short_plain_questions_are_accepted(self):
        for text in (QUESTION, "Which patient is this for?", "What time?", "Which day would you like to book?",
                     "Please tell me the date and time.", "Tell me the day, please", "What is the patient's phone number?",
                     "आप किस दिन और कितने बजे आना चाहेंगे?", "कौन सी तारीख और कितने बजे?", "Kis din aur kitne baje aana hai?",
                     "Kab aana chahte hain?", "Please batao kaun sa din chahiye.", "  What  date\n and time?  "):
            with self.subTest(text=text):
                self.assertEqual(prose.as_question(text), " ".join(text.split()))

    def test_a_claim_that_something_was_done_is_never_a_question(self):
        for text in ("Sure, I've booked it for you", "Done! What time?", "Your appointment is confirmed. Anything else?",
                     "I have booked Priya for tomorrow at 5 pm.", "I'll book it now. Which day?", "Appointment cancelled?",
                     "Saved. What next?", "All set! Which patient?", "Priya has been registered. What is her number?",
                     "I went ahead and booked 5 pm, is that right?", "Should I confirm it?", "Shall I save this?",
                     "Priya ke liye appointment book kar di gayi hai.", "Ho gaya, aur kuch?", "Maine cancel kar diya, kaun sa din?",
                     "booking ho gayi, kab aana hai?", "Pakka kar diya?",
                     "आपकी अपॉइंटमेंट बुक कर दी गई है।", "हो गया, कौन सा दिन?", "मैंने कैंसिल कर दिया, क्या आप कुछ और चाहते हैं?",
                     "अपॉइंटमेंट कन्फर्म हो गई?", "सफलतापूर्वक बुक हो गया, कौन सा दिन?"):
            with self.subTest(text=text):
                self.assertTrue(prose.claims_action(text))
                self.assertIsNone(prose.as_question(text))

    def test_the_claim_detector_leaves_plain_questions_alone(self):
        for text in (QUESTION, "Which patient?", "What is the phone number?", "कौन सा दिन?", "Kis din?", "Which branch?"):
            with self.subTest(text=text):
                self.assertFalse(prose.claims_action(text))

    def test_long_odd_or_unsafe_text_is_not_a_question(self):
        for text in ("What date and time would you like? " * 8,                       # too long
                     "x" * 300,
                     "Hello there", "Sure thing.", "", "   ", None, 42,
                     "```What date?```", "What `date` would you like?", "What date?\n- today\n- tomorrow",
                     "# Booking\nWhat date?", "What date?\nWhat time?\nWhich branch?",
                     '{"name": "book_appointment", "arguments": {"patient_name": "Priya"}}',
                     "book_appointment(patient_name='Priya', date='2026-10-10')?", "Call cancel_appointment for Priya?",
                     "Please open https://example.com/book to pick a day?", "Visit www.clinic.in and tell me the date?",
                     "Email me at a@b.co and tell me the date?", "Please click Approve and tell me the date?",
                     "Please log in to the portal, which day?", "Please send the OTP, which day?",
                     "Pay Rs 500 now, which day?", "Please call 100 for the day?",
                     "What is the patient_id? patient_id=4", "Which one, #2?", "What is your password: hunter2?",
                     "Here is my key sk_live_abcdefghijklmnop, which day?", "<b>Which day?</b>", "Which day? [link]"):
            with self.subTest(text=text):
                self.assertIsNone(prose.as_question(text))

    def test_small_talk_questions_are_not_shown(self):
        for text in ("How can I help you today?", "What can I do for you?", "Is there anything else I can help with?",
                     "Would you like to see the list?", "Anything else?", "मैं आपकी क्या मदद कर सकता हूँ?",
                     "Main aapki kya madad kar sakta hoon?"):
            with self.subTest(text=text):
                self.assertIsNone(prose.as_question(text))

    def test_a_statement_that_asks_for_nothing_is_not_a_question(self):
        for text in ("The clinic opens at nine.", "I can help with appointments.", "Priya is a registered patient."):
            with self.subTest(text=text):
                self.assertIsNone(prose.as_question(text))

    def test_scrub_removes_keys_and_caps_the_length(self):
        for text, bad in (("my key is sk_live_abcdefghijklmnop ok", "sk_live_abcdefghijklmnop"),
                          ("api-subscription-key: ABCDEF123456", "ABCDEF123456"),
                          ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
                          ("token=zzz123456", "zzz123456"),
                          ("here is {}".format("A1" * 20), "A1" * 20)):
            with self.subTest(text=text):
                out = prose.scrub(text)
                self.assertNotIn(bad, out)
                self.assertIn("***", out)
        self.assertNotIn("PLAINLYSECRET", prose.scrub("the value is PLAINLYSECRET", secrets=("PLAINLYSECRET",)))
        long = prose.scrub("word " * 400)
        self.assertLessEqual(len(long), prose.LOG_CAP)
        self.assertNotIn("\n", prose.scrub("a\nb\r\nc"))
        self.assertEqual(prose.scrub(None), "")
        self.assertEqual(prose.scrub("Which patient?"), "Which patient?")                # ordinary words are untouched


class TheBackendsKeepTheWords(unittest.TestCase):
    def test_the_fake_backend_scripts_a_plain_words_reply(self):
        fake = planner.FakeBackend(["What time?", ("query", {"entity": "patients"}), None])
        self.assertIsNone(fake.plan("s", "u", []))
        self.assertEqual(fake.last_text, "What time?")
        self.assertEqual(fake.plan("s", "u", []).name, "query")
        self.assertIsNone(fake.last_text)
        self.assertIsNone(fake.plan("s", "u", []))
        self.assertIsNone(fake.last_text)

    def sarvam_backend(self, message, finish="stop"):
        body = {"choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        backend = sarvam.SarvamBackend(api_key="k", transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
        return backend

    def test_sarvam_keeps_the_words_of_a_stop_reply_and_none_for_a_tool_call(self):
        backend = self.sarvam_backend({"role": "assistant", "content": QUESTION})
        self.assertIsNone(backend.plan("s", "u", tools.schemas()))
        self.assertEqual(backend.last_text, QUESTION)
        call = {"id": "c", "type": "function", "function": {"name": "query", "arguments": json.dumps({"entity": "patients"})}}
        backend = self.sarvam_backend({"role": "assistant", "content": "ok", "tool_calls": [call]}, "tool_calls")
        self.assertEqual(backend.plan("s", "u", tools.schemas()).name, "query")
        self.assertIsNone(backend.last_text)

    def test_a_reply_cut_off_by_the_token_limit_or_empty_is_not_kept(self):
        self.assertIsNone(self.sarvam_backend({"role": "assistant", "content": "What date and"}, "length").plan("s", "u", tools.schemas()))
        backend = self.sarvam_backend({"role": "assistant", "content": "What date and"}, "length")
        backend.plan("s", "u", tools.schemas())
        self.assertIsNone(backend.last_text)
        for content in (None, "", "   ", ["What date?"], {"a": 1}):
            backend = self.sarvam_backend({"role": "assistant", "content": content})
            backend.plan("s", "u", tools.schemas())
            self.assertIsNone(backend.last_text)

    def test_the_words_are_reset_on_every_call_and_capped_in_memory(self):
        answers = [{"role": "assistant", "content": "x" * 5000}, {"role": "assistant", "content": None}]
        calls = []

        def handler(request):
            calls.append(1)
            body = {"choices": [{"index": 0, "message": answers[len(calls) - 1], "finish_reason": "stop"}], "usage": {}}
            return httpx.Response(200, json=body)
        backend = sarvam.SarvamBackend(api_key="k", transport=httpx.MockTransport(handler))
        backend.plan("s", "u", tools.schemas())
        self.assertEqual(len(backend.last_text), planner.MAX_PROSE_CHARS)
        backend.plan("s", "u", tools.schemas())
        self.assertIsNone(backend.last_text)

    def test_the_local_backend_keeps_the_words_too(self):
        reply = httpx.Response(200, json={"message": {"role": "assistant", "content": "Which day?"}},
                               request=httpx.Request("POST", "http://localhost"))
        with patch("clinic.nlu.planner.httpx.post", return_value=reply):
            backend = planner.OllamaBackend()
            self.assertIsNone(backend.plan("s", "u", tools.schemas()))
        self.assertEqual(backend.last_text, "Which day?")

    def test_the_replay_scripted_backend_can_say_words(self):
        from scripts.replay import engine
        backend = engine.ScriptedBackend()
        backend.golden = {"prose": QUESTION}
        self.assertIsNone(backend.plan("s", "u", tools.schemas()))
        self.assertEqual(backend.last_text, QUESTION)
        backend.golden = ("query", {"entity": "patients"})
        backend.plan("s", "u", tools.schemas())
        self.assertIsNone(backend.last_text)


class AReplyInWordsBecomesAQuestion(TwoPriyas):
    def ask_with_words(self, mode, words, text=LIVE):
        self.conn.execute("DELETE FROM planner_log")
        self.use(mode)
        self.script(words)
        return self.say(text)

    def test_a_short_question_is_the_assistants_clarifying_question(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                before = self.counts()
                ask = self.ask_with_words(mode, QUESTION)
                self.assertIsInstance(ask, AskResult)
                self.assertEqual((ask.intent, ask.kind, ask.question), ("clarify", "clarify", QUESTION))
                self.assertEqual(self.ctx.pending["kind"], "clarify")
                self.assertEqual(len(self.backend.calls), 1)                         # the words ARE the answer: no second call
                self.assertEqual(self.counts(), before)
                row = self.last()
                self.assertEqual((row["route_taken"], row["planner_tool"], row["final_intent"]), ("planner", "clarify", "clarify"))
                self.assertEqual(row["route_detail"], "mf:clarify" if mode == "model_first" else None)
                self.assertEqual(json.loads(row["planner_args_json"]), {"question": QUESTION})

    def test_the_words_are_logged_in_the_notes(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.ask_with_words(mode, QUESTION)
                self.assertEqual(self.last()["override_notes"], "prose reply used as the question: " + QUESTION)

    def test_the_next_sentence_is_planned_with_that_question_as_the_previous_turn(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.ask_with_words(mode, QUESTION)
                self.script(("book_appointment", {"patient_name": "Priya", "date": self.day, "time": "17:00", "phone": PHONE}))
                card = self.say("tomorrow at 5 pm")
                self.assertIsInstance(card, ParsedResult)
                self.assertEqual((card.slots["appt_date"], card.slots["start_time"], card.slots["patient_phone"]),
                                 (self.day, "17:00", PHONE))
                self.assertIn(QUESTION, self.backend.calls[-1][1])                  # it reached the model with the next sentence
                self.assertIn("clarify(", self.backend.calls[-1][1])

    def test_a_hindi_question_is_shown_as_it_is(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                question = "आप किस दिन और कितने बजे आना चाहेंगे?"
                ask = self.ask_with_words(mode, question)
                self.assertEqual((ask.kind, ask.question), ("clarify", question))

    def test_a_claim_of_an_action_is_not_shown_and_nothing_is_written(self):
        for mode in MODES:
            for words in ("Sure, I've booked it for you", "Done. I have booked Priya for tomorrow.",
                          "Priya ke liye appointment book kar di gayi hai.", "आपकी अपॉइंटमेंट बुक कर दी गई है।"):
                with self.subTest(mode=mode, words=words):
                    before = self.counts()
                    try:
                        result = self.ask_with_words(mode, words)
                    except PipelineError as exc:
                        result = exc
                    self.assertNotIn(words.rstrip("."), str(getattr(result, "question", result)))
                    self.assertFalse(isinstance(result, AskResult) and result.kind == "clarify")
                    self.assertNotEqual((self.last()["route_taken"], self.last()["planner_tool"]), ("planner", "clarify"))
                    self.assertIn("prose reply (not shown): " + prose.scrub(words), self.last()["override_notes"])   # still logged
                    self.assertIn("planner made no tool call", self.last()["override_notes"])
                    self.assertEqual(self.counts(), before)
                    self.assertNotEqual(getattr(self.ctx.pending, "get", lambda k: None)("kind"), "clarify")

    def test_long_code_link_and_off_app_replies_are_ignored_but_logged(self):
        for words in (QUESTION + " " * 3 + "Thank you for your patience. " * 12,
                      "```json\n{\"name\": \"book_appointment\"}\n```", "book_appointment(patient_name='Priya')?",
                      "Please open https://example.com to pick a day?", "- today\n- tomorrow\nWhich?",
                      "Hello there, how can I help"):
            for mode in MODES:
                with self.subTest(mode=mode, words=words[:40]):
                    result = self.ask_with_words(mode, words)
                    self.assertFalse(isinstance(result, AskResult) and result.kind == "clarify")
                    notes = self.last()["override_notes"]
                    self.assertIn("prose reply (not shown): ", notes)
                    self.assertLessEqual(len(notes), len("planner made no tool call; prose reply (not shown): ") + prose.LOG_CAP)
                    self.assertNotIn("\n", notes)

    def test_a_key_looking_string_is_scrubbed_from_the_log_and_the_reply_is_not_shown(self):
        secret = "sk_live_0123456789abcdefghij"
        with patch.dict(os.environ, {"SARVAM_API_KEY": "Plain-Key-Value-77"}):
            for words in ("Which day? my key is " + secret, "Which day? Plain-Key-Value-77", "api-subscription-key: abc123xyz Which day?"):
                for mode in MODES:
                    with self.subTest(mode=mode, words=words):
                        result = self.ask_with_words(mode, words)
                        self.assertFalse(isinstance(result, AskResult) and result.kind == "clarify")
                        notes = self.last()["override_notes"]
                        for leaked in (secret, "Plain-Key-Value-77", "abc123xyz"):
                            self.assertNotIn(leaked, notes)
                        self.assertIn("***", notes)

    def test_the_words_are_never_read_for_slots_or_ids(self):
        """Only a question is shown; a reply full of values and ids fills nothing and carries nothing."""
        for mode in MODES:
            with self.subTest(mode=mode):
                words = "Which one, Priya Shah on 2026-10-12 at 17:00 with patient_id=4?"
                result = self.ask_with_words(mode, words)
                self.assertFalse(isinstance(result, AskResult) and result.kind == "clarify")
                self.assertIsNone(self.ctx.open_card)
                self.assertNotIn("appt_date", getattr(result, "slots", {}) or {})

    def test_nothing_is_approved_or_written_by_words(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                before = self.counts()
                self.ask_with_words(mode, "Should I go ahead and approve it?")
                self.ask_with_words(mode, "Shall I confirm it?")
                self.assertEqual(self.counts(), before)
                self.assertIsNone(self.ctx.open_card)

    def test_the_words_of_a_failed_name_read_are_not_a_question(self):
        """The small hosted name read ignores plain words: they are not a question for the user."""
        run = planner.PlannerRun(self.conn, "x", self.ctx, today=self.today, backend=planner.FakeBackend(QUESTION))
        with patch("clinic.nlu.planner.staff_uses_sarvam", return_value=True):
            self.assertIsNone(run.fill_name())
        self.assertFalse(any("prose reply" in note for note in run.notes))

    def test_through_sarvams_own_backend_on_a_fake_transport(self):
        body = {"choices": [{"index": 0, "message": {"role": "assistant", "content": QUESTION}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3700, "completion_tokens": 9}}
        backend = sarvam.SarvamBackend(api_key="k", transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
        for mode in MODES:
            with self.subTest(mode=mode):
                self.conn.execute("DELETE FROM planner_log")
                self.use(mode)
                planner.set_backend(backend)
                ask = self.say(LIVE)
                self.assertEqual((ask.kind, ask.question), ("clarify", QUESTION))
                row = self.last()
                self.assertEqual((row["backend"], row["tokens_in"], row["route_taken"]), ("sarvam", 3700, "planner"))
                planner.set_backend(self.backend)


if __name__ == "__main__":
    unittest.main()
