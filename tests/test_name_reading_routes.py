"""Who a command is about is read the same way whichever route understood it.

The bug this guards: "Move Manju's appointment to branch C to tomorrow at eleven am." is decided by a precise rule
(a move), never by the planner, and the name reader then had nothing for a patient known only by one word, so the
assistant asked "Which patient?". Now
  1. a deterministic reader finds a one-word registered name as a whole word (with a possessive or a Hindi particle),
     and refuses when it is not sure (another name-like word next to it, two people fit, "new patient ...");
  2. when it is not sure and the hosted planner is selected, ONE small hosted call returns just the name words
     (the intent is never changed; the name goes through the normal exact matching);
  3. every command leaves a planner_log row, rule-routed ones included (route 'rules', route_detail "rule:move" ...);
  4. a missing name is asked as "I couldn't tell who the patient is. Which patient?".

Nothing here reaches Sarvam, Ollama or WhatsApp: the hosted calls go through a FAKE HTTP transport (the one the
Sarvam tests use) or a FakeBackend, and the local model is a tripwire."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from tests.planner_support import PlannerCase  # noqa: E402
from tests.test_planner_sarvam import KEY, Server, backend_for, reply  # noqa: E402
from tests.test_voice_dialog import DEFER  # noqa: E402

from clinic import db, planner_log, settings, voice_context  # noqa: E402
from clinic.nlu import llm_slots, parser, planner, sarvam  # noqa: E402
from clinic.nlu.llm_slots import match_known_name  # noqa: E402
from clinic.pipeline import ParsedResult  # noqa: E402
from clinic.voice_context import AskResult, VoiceContext  # noqa: E402
from clinic.voice_turns import handle_turn  # noqa: E402

MOVE = "Move Manju's appointment to branch C to tomorrow at eleven am."
HONEST = "I couldn't tell who the patient is. Which patient?"


def heard(name, **kw):
    return reply("heard_name", json.dumps({"name": name}), **kw)


class OneWordNameReader(unittest.TestCase):
    """clinic/nlu/llm_slots.match_known_name: the deterministic reader, on its own."""
    NAMES = ["Rakesh Verma", "Manju", "Seema", "Amit Dua", "Amit Rao", "Anil", "Anil", "Neeraj Kapoor"]

    def read(self, text, names=None):
        return match_known_name(text, self.NAMES if names is None else names)

    def test_a_one_word_registered_name_is_found_as_a_whole_word(self):
        for text in (MOVE, "Move Manju’s appointment to tomorrow at eleven am.", "move manju's appointment tomorrow",
                     "Manju ka appointment kal 11 baje shift karo", "Manju ki appointment cancel karo",
                     "Manju ko kal 5 baje bulao", "cancel manju", "book an appointment for Manju at 5 pm",
                     "Dr. Manju tomorrow", "please move MANJU to 5 pm"):
            with self.subTest(text=text):
                self.assertEqual(self.read(text), "Manju")

    def test_a_staff_first_name_is_found_the_same_way(self):
        for text in ("Seema aaj absent hai", "mark Seema absent today", "Seema ko half day", "Seema छुट्टी पर है"):
            with self.subTest(text=text):
                self.assertEqual(self.read(text), "Seema")

    def test_a_devanagari_spelling_of_a_roman_registered_name(self):
        self.assertEqual(self.read("मंजू का अपॉइंटमेंट कल 11 बजे शिफ्ट करो"), "Manju")
        self.assertIsNone(self.read("मंजूश्री का अपॉइंटमेंट कल"))       # a different name is not "close enough"

    def test_two_or_more_words_still_work_and_still_win(self):
        self.assertEqual(self.read("what is rakesh verma's phone number"), "Rakesh Verma")
        self.assertEqual(self.read("Seema Rakesh Verma"), "Rakesh Verma")        # the longest name wins
        self.assertEqual(self.read("Amit Dua ka appointment"), "Amit Dua")
        self.assertEqual(self.read("Manju Rakesh Verma ko bulao"), "Rakesh Verma")

    def test_a_new_person_who_shares_the_first_word_is_not_the_registered_one(self):
        for text in ("Book Manju Sharma tomorrow at 5", "Book Manju Sharma tomorrow at 5 pm", "register Manju Sharma 9876543210",
                     "move Manju Sharma's appointment to 5", "book Sharma Manju kal", "Manju Verma ka appointment"):
            with self.subTest(text=text):
                self.assertIsNone(self.read(text))

    def test_a_new_patient_is_not_a_known_name(self):
        for text in ("new patient Manju", "register Manju 9876543210", "naya patient Manju kal 5 baje", "Register a new patient Manju"):
            with self.subTest(text=text):
                self.assertIsNone(self.read(text))

    def test_two_registered_people_on_one_word_is_not_picked_here(self):
        self.assertIsNone(self.read("Move Amit's appointment to tomorrow"))            # Amit Dua and Amit Rao
        self.assertIsNone(self.read("cancel Anil's appointment"))                        # two patients both called Anil
        self.assertIsNone(self.read("Move Manju's appointment", ["Manju", "Manju Verma"]))      # one-word Manju AND Manju Verma
        self.assertIsNone(self.read("Move Manju's appointment", ["Manju", "Manju"]))

    def test_a_lone_first_name_of_a_two_word_patient_is_still_not_taken(self):
        self.assertIsNone(self.read("Rakesh ka number batao"))           # the exact matcher and "Which one?" handle it later

    def test_an_unknown_name_and_unsure_sentences_are_none(self):
        for text in ("move Zebra's appointment to tomorrow", "", "how many patients are registered", "Manju Neeraj ko bulao"):
            with self.subTest(text=text):
                self.assertIsNone(self.read(text))

    def test_two_different_one_word_names_in_one_sentence_are_unsure(self):
        self.assertIsNone(self.read("move Manju to Seema's slot"))

    def test_it_is_still_exact_a_nearby_spelling_is_not_a_hit(self):
        self.assertIsNone(self.read("move Manjoo's appointment", ["Manju"]))
        self.assertIsNone(self.read("move Manj's appointment", ["Manju"]))


class RouteCase(PlannerCase):
    """The branch test bed, the REAL name reader (the dialog test bed stubs it), the planner on, and registered
    one-word and shared-first-word people. `hosted()` selects PLANNER_BACKEND=sarvam with the local model off and a
    fake Sarvam transport; without it the planner is the default local one (a FakeBackend here)."""

    def setUp(self):
        super().setUp()
        for name, phone in (("Manju", "9000000018"), ("Amit Dua", "9000000021"), ("Amit Rao", "9000000022"),
                            ("Neeraj Kapoor", "9000000023")):
            self.conn.execute("INSERT INTO patients (name, phone, age) VALUES (?, ?, 40)", (name, phone))
        self.conn.execute("INSERT INTO staff (name, role) VALUES ('Seema', 'nurse')")
        self.conn.commit()
        self.manju_id = self.conn.execute("SELECT id FROM patients WHERE name = 'Manju'").fetchone()[0]
        self.book_at(1, "12:00", patient_id=self.manju_id)
        self.real_name = Mock(wraps=llm_slots.extract_name)
        for target in ("clinic.nlu.parser.extract_name", "clinic.voice_turns.extract_name"):
            patcher = patch(target, self.real_name)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = None

    # -- modes ---------------------------------------------------------------------------------------

    def hosted(self, *answers):
        """PLANNER_BACKEND=sarvam on a fake transport answering `answers` (default: no usable call)."""
        env = patch.dict(os.environ, {"PLANNER_BACKEND": "sarvam", "SARVAM_API_KEY": KEY})
        env.start()
        self.addCleanup(env.stop)
        self.server = Server(*(answers or (reply(finish="stop"),)))
        self.sarvam, _ = backend_for(self.server)
        planner.set_backend(self.sarvam)
        self.tripwire_local()

    def tripwire_local(self):
        patcher = patch.object(planner.OllamaBackend, "plan", side_effect=AssertionError("the local planner was used"))
        self.ollama_planner = patcher.start()
        self.addCleanup(patcher.stop)

    def reader_unsure(self):
        """Switch the deterministic reader off (as if it could not tell), everything else real."""
        patcher = patch("clinic.nlu.llm_slots.match_known_name", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def command(self, text, language="en-IN"):
        self.ctx = VoiceContext(clock=self.clock)
        self.ctx.set_client_branch(1, 1)
        return handle_turn(self.ctx, self.conn, text, self.adapter, self.adapter, language, DEFER)

    def assertNoLocalModel(self):
        self.live.assert_not_called()
        self.pick.assert_not_called()
        if self.server is not None:
            self.ollama_planner.assert_not_called()

    def only_row(self):
        rows = self.log_rows()
        self.assertEqual(len(rows), 1, rows)
        return rows[0]


class HostedMoveSentence(RouteCase):
    """The exact sentence from the bug, with the hosted planner selected and the local model off."""

    def test_a_deterministic_hit_makes_no_hosted_call_at_all(self):
        self.hosted(heard("Manju"))
        card = self.command(MOVE)
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual((card.intent, card.slots["patient_name"]), ("reschedule_appointment", "Manju"))
        self.assertEqual(self.server.calls, 0)
        self.assertEqual(self.backend.calls, [])
        row = self.only_row()
        self.assertEqual((row["route_taken"], row["route_detail"], row["planner_tool"], row["final_intent"]),
                         ("rules", "rule:move", None, "reschedule_appointment"))
        self.assertEqual((row["backend"], row["tokens_in"], row["tokens_out"], row["cost_paise"]), (None, None, None, 0))
        found = json.loads(row["planner_args_json"])
        self.assertIn("patient_name", found["slots_found"])
        self.assertNotIn("patient_name", found["slots_missing"])
        self.assertNoLocalModel()

    def test_when_the_reader_is_unsure_one_hosted_call_fills_only_the_name(self):
        self.reader_unsure()
        self.hosted(heard("Manju", prompt=210, completion=12))
        card = self.command(MOVE)
        self.assertEqual(self.server.calls, 1)
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual((card.intent, card.slots["patient_name"]), ("reschedule_appointment", "Manju"))     # the rule's intent stands
        self.assertEqual(card.slots["start_time"], "11:00")                                                   # the rest was read in code
        self.assertEqual(card.slots["appt_date"], self.tomorrow_iso)
        self.assertEqual(card.resolved["patient_label"].split(" (")[0], "Manju")                              # exact resolution found her
        row = self.only_row()
        self.assertEqual((row["route_taken"], row["route_detail"], row["planner_tool"]), ("rules", "rule:move+name_fill", None))
        self.assertEqual((row["backend"], row["tokens_in"], row["tokens_out"]), ("sarvam", 210, 12))
        self.assertEqual(row["cost_paise"], sarvam.cost_paise(sarvam.Usage(210, 12, 0)))
        self.assertGreater(row["cost_paise"], 0)
        self.assertIsNone(row["override_notes"])
        self.assertIsInstance(row["latency_ms"], int)
        self.assertEqual(row["transcript"], MOVE)
        self.assertNotIn("Manju", row["planner_args_json"])                  # slot NAMES only, never what was heard
        self.assertNoLocalModel()

    def test_the_hosted_call_is_small_one_tool_and_carries_no_patient_data(self):
        self.reader_unsure()
        self.hosted(heard("Manju"))
        self.command(MOVE)
        body = json.loads(self.server.requests[0].content)
        text = json.dumps(body)
        self.assertEqual([t["function"]["name"] for t in body["tools"]], ["heard_name"])
        self.assertEqual(body["messages"][1]["content"], MOVE)
        for private in ("Rakesh", "Mohan", "Sunita", "Amit", "9000000018", "Dr. Rao", KEY):
            self.assertNotIn(private, text)                                   # no registered names, doctors, numbers or key
        self.assertEqual(self.server.requests[0].headers["api-subscription-key"], KEY)
        self.assertLess(len(body["messages"][0]["content"]), 1500)

    def test_the_fill_is_counted_in_the_months_spend(self):
        self.reader_unsure()
        self.hosted(heard("Manju", prompt=1000, completion=20))
        self.command(MOVE)
        self.conn.execute("UPDATE planner_log SET ts = '2026-10-06 04:00:00'")          # a fixed day, not today's date
        usage = planner_log.sarvam_usage(self.conn, datetime(2026, 10, 15, 12, 0))
        self.assertEqual((usage["commands"], usage["tokens_in"], usage["tokens_out"]), (1, 1000, 20))
        self.assertEqual(usage["spend_paise"], sarvam.cost_paise(sarvam.Usage(1000, 20, 0)))

    def test_a_failing_hosted_call_falls_back_to_the_honest_question(self):
        self.reader_unsure()
        self.hosted(httpx.ConnectError("offline"))
        ask = self.command(MOVE)
        self.assertIsInstance(ask, AskResult)
        self.assertEqual((ask.intent, ask.kind, ask.question), ("reschedule_appointment", "patient", HONEST))
        self.assertIsNone(ask.slots["patient_name"])
        self.assertEqual((ask.slots["start_time"], ask.slots["appt_date"]), ("11:00", self.tomorrow_iso))     # the rest is kept
        self.assertEqual(self.server.calls, 1)
        row = self.only_row()
        self.assertEqual((row["route_taken"], row["route_detail"]), ("rules", "rule:move+name_fill"))
        self.assertEqual(row["override_notes"], "sarvam: network error (ConnectError)")
        self.assertEqual((row["backend"], row["tokens_in"], row["cost_paise"]), ("sarvam", None, 0))
        self.assertIn("patient_name", json.loads(row["planner_args_json"])["slots_missing"])
        self.assertNoLocalModel()

    def test_each_failure_is_the_same_question_and_never_a_crash(self):
        self.reader_unsure()
        for answer in (httpx.ReadTimeout("slow"), httpx.Response(429), httpx.Response(503), reply(finish="stop"),
                       reply("book_appointment", json.dumps({"patient_name": "Manju"})),          # the wrong tool
                       reply("heard_name", "not json"), reply("heard_name", json.dumps({"name": ""})),
                       reply("heard_name", json.dumps({"name": "Mohan Lal"})),                    # a name the sentence does not hold
                       reply("heard_name", json.dumps({"name": "Manju 5"}))):
            with self.subTest(answer=repr(answer)[:60]):
                self.hosted(answer)
                self.sarvam.breaker.record_success()
                ask = self.command(MOVE)
                self.assertIsInstance(ask, AskResult)
                self.assertEqual((ask.kind, ask.question), ("patient", HONEST))
                self.assertEqual(self.log_rows()[-1]["route_detail"], "rule:move+name_fill")
        self.assertNoLocalModel()

    def test_an_open_circuit_or_a_missing_key_is_instant_and_the_same_question(self):
        self.reader_unsure()
        self.hosted(httpx.ConnectError("offline"))
        for _ in range(3):
            self.command(MOVE)
        calls = self.server.calls
        ask = self.command(MOVE)
        self.assertEqual(self.server.calls, calls)                               # circuit open: no call, no wait
        self.assertEqual((ask.kind, ask.question), ("patient", HONEST))
        self.assertEqual(self.log_rows()[-1]["override_notes"], "sarvam: circuit open")
        with patch.dict(os.environ, {"SARVAM_API_KEY": ""}):
            self.sarvam._api_key = None
            self.sarvam.breaker.record_success()
            ask = self.command(MOVE)
        self.assertEqual(ask.kind, "patient")
        self.assertEqual(self.log_rows()[-1]["override_notes"], "sarvam: no key")

    def test_the_name_may_come_back_with_a_possessive_and_is_still_exact(self):
        self.reader_unsure()
        self.hosted(heard("Manju's"))
        card = self.command(MOVE)
        self.assertEqual(card.slots["patient_name"], "Manju")
        self.hosted(heard("Manjoo"))                                              # not in the sentence
        ask = self.command(MOVE)
        self.assertEqual(ask.kind, "patient")

    def test_the_hosted_name_is_matched_exactly_like_any_other(self):
        self.reader_unsure()
        self.hosted(heard("Amit"))                                                # two registered Amits
        result = self.command("Move Amit's appointment to tomorrow at eleven am.")
        self.assertIsInstance(result, AskResult)
        self.assertEqual((result.kind, result.question), ("choose_patient", "Which one?"))
        self.assertEqual(sorted(o["patient_name"] for o in result.options), ["Amit Dua", "Amit Rao"])
        self.assertEqual(self.only_row()["final_intent"], "reschedule_appointment")

    def test_an_unknown_name_is_one_call_and_the_intent_is_unchanged(self):
        self.reader_unsure()
        self.hosted(heard("Zebra"))
        try:
            self.command("Move Zebra's appointment to tomorrow at eleven am.")
        except Exception:
            pass                                                                   # "no appointment for Zebra" is a normal reply
        self.assertEqual(self.server.calls, 1)
        row = self.only_row()
        self.assertEqual((row["final_intent"], row["route_detail"]), ("reschedule_appointment", "rule:move+name_fill"))

    def test_a_new_person_who_shares_a_registered_first_word_is_not_the_registered_one(self):
        # "Manju Sharma" is not "Manju": the reader refuses, the hosted read hears the whole name.
        self.hosted(heard("Manju Sharma"))
        run = planner.PlannerRun(self.conn, "Move Manju Sharma's appointment to tomorrow at eleven am.")
        intent, slots = parser.parse("Move Manju Sharma's appointment to tomorrow at eleven am.",
                                     known_names=["Manju", "Rakesh Verma", "Seema"], planner=run)
        self.assertEqual((intent, slots["patient_name"]), ("reschedule_appointment", "Manju Sharma"))
        self.assertEqual(self.server.calls, 1)
        # and with no hosted help at all it is left blank, never the registered "Manju"
        self.hosted(httpx.ConnectError("offline"))
        run = planner.PlannerRun(self.conn, "Move Manju Sharma's appointment to tomorrow at eleven am.")
        intent, slots = parser.parse("Move Manju Sharma's appointment to tomorrow at eleven am.",
                                     known_names=["Manju", "Rakesh Verma", "Seema"], planner=run)
        self.assertIsNone(slots["patient_name"])

    def test_book_for_a_new_manju_sharma_is_never_the_registered_manju(self):
        self.hosted(reply("book_appointment", json.dumps({"patient_name": "Manju Sharma", "date": self.tomorrow_iso, "time": "17:00"})))
        result = self.command("Book Manju Sharma tomorrow at 5")
        self.assertEqual(self.server.calls, 1)                                    # the planner's own call, no extra name read
        self.assertNotEqual(result.slots.get("patient_name"), "Manju")
        self.assertEqual(result.slots.get("patient_name"), "Manju Sharma")
        self.assertEqual(self.only_row()["route_taken"], "planner")

    def test_the_hindi_sentence_reads_a_devanagari_name_without_a_hosted_call(self):
        self.hosted(heard("मंजू"))
        card = self.command("मंजू का अपॉइंटमेंट कल 11 बजे शिफ्ट करो")
        self.assertEqual((card.intent, card.slots["patient_name"]), ("reschedule_appointment", "Manju"))
        self.assertEqual(self.server.calls, 0)

    def test_a_hindi_name_the_reader_cannot_place_is_filled_by_the_hosted_read_and_asked_in_hindi_if_it_fails(self):
        self.reader_unsure()
        self.hosted(httpx.ConnectError("offline"))
        ask = self.command("मंजू का अपॉइंटमेंट कल 11 बजे शिफ्ट करो")
        self.assertEqual(ask.kind, "patient")
        self.assertEqual(ask.question, "मुझे समझ नहीं आया कि मरीज़ कौन है। कौन सा मरीज़?")
        self.hosted(heard("मंजू"))
        card = self.command("मंजू का अपॉइंटमेंट कल 11 बजे शिफ्ट करो")
        self.assertEqual(card.slots["patient_name"], "मंजू")                       # spoken text, resolved exactly by Roman spelling
        self.assertEqual(card.resolved["patient_label"].split(" (")[0], "Manju")


class HostedOtherRoutes(RouteCase):
    def test_a_planner_routed_command_has_no_extra_name_call(self):
        self.hosted()
        planner.set_backend(self.backend)                 # the planner is a FakeBackend; the hosted flag is still on
        self.call("cancel_appointment", patient_name="Manju")
        result = self.command("Cancel Manju's appointment.")
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.server.calls, 0)
        self.assertEqual(result.slots["patient_name"], "Manju")
        row = self.only_row()
        self.assertEqual((row["route_taken"], row["planner_tool"], row["route_detail"]), ("planner", "cancel_appointment", None))

    def test_a_planner_that_failed_does_not_trigger_a_second_hosted_call(self):
        self.hosted(httpx.ConnectError("offline"))
        ask = self.command("Cancel Zebra's appointment.")
        self.assertEqual(self.server.calls, 1)            # the planner's own call only: no second one for the name
        self.assertIsInstance(ask, AskResult)
        row = self.only_row()
        self.assertEqual((row["route_taken"], row["route_detail"]), ("rules", None))
        self.assertEqual(row["override_notes"], "sarvam: network error (ConnectError)")

    def test_a_count_needs_no_name_and_no_call(self):
        self.hosted(heard("Manju"))
        self.command("how many patients are registered")
        self.assertEqual(self.server.calls, 0)
        row = self.only_row()
        self.assertEqual((row["route_taken"], row["route_detail"], row["final_intent"]), ("rules", "rule:count", "patient_count"))

    def test_a_follow_up_on_the_screen_is_rule_routed_and_never_asks_for_a_name(self):
        self.hosted(heard("Manju"))
        planner.set_backend(self.backend)
        self.call("query", entity="appointments", aggregate="list", date=self.tomorrow_iso)
        self.ctx = VoiceContext(clock=self.clock)
        self.ctx.set_client_branch(1, 1)
        handle_turn(self.ctx, self.conn, "who all are coming in tomorrow", self.adapter, self.adapter, "en-IN", DEFER)
        self.assertTrue(self.ctx.list_rows)
        planner.set_backend(self.sarvam)
        self.reader_unsure()
        result = handle_turn(self.ctx, self.conn, "cancel the second one", self.adapter, self.adapter, "en-IN", DEFER)
        self.assertEqual(result.intent, "cancel_appointment")
        self.assertEqual(self.server.calls, 0)
        self.assertEqual(self.log_rows()[-1]["route_detail"], "rule:context")
        self.assertEqual(self.log_rows()[-1]["route_taken"], "rules")

    def test_switching_and_closing_branches_are_logged_as_rules(self):
        self.hosted()
        self.command("switch to Branch C")
        self.command("close Branch A tomorrow because the doctor is ill")
        self.assertEqual([(r["route_taken"], r["route_detail"]) for r in self.log_rows()],
                         [("rules", "rule:branch"), ("rules", "rule:closure")])
        self.assertEqual(self.server.calls, 0)

    def test_scope_unmatched_keyword_route_also_fills_a_missing_name(self):
        self.reader_unsure()
        self.hosted(heard("Manju"))
        with patch.dict(os.environ, {"INTENT_PLANNER_SCOPE": "unmatched"}):
            result = self.command("cancel Manju's appointment")
        self.assertEqual(result.intent, "cancel_appointment")
        self.assertEqual(result.slots["patient_name"], "Manju")
        self.assertEqual(self.server.calls, 1)
        self.assertEqual(self.only_row()["route_detail"], "rule:keywords+name_fill")

    def test_a_list_or_a_queue_command_never_makes_a_name_call(self):
        self.reader_unsure()
        self.hosted(heard("Manju"))
        with patch.dict(os.environ, {"INTENT_PLANNER_SCOPE": "unmatched"}):
            self.command("show tomorrow's appointments")
        self.assertEqual(self.server.calls, 0)


class LocalMode(RouteCase):
    """PLANNER_BACKEND=local (the test default): nothing changes except the sharper deterministic reader."""

    def test_the_deterministic_hit_never_touches_the_local_model(self):
        card = self.command(MOVE)
        self.assertEqual((card.intent, card.slots["patient_name"]), ("reschedule_appointment", "Manju"))
        self.live.assert_not_called()
        self.assertEqual(self.backend.calls, [])
        row = self.only_row()
        self.assertEqual((row["route_taken"], row["route_detail"], row["backend"]), ("rules", "rule:move", None))

    def test_when_unsure_the_local_model_is_asked_as_before_and_no_hosted_read_is_made(self):
        self.reader_unsure()
        with patch("clinic.nlu.llm_slots._ask_model_for_name", return_value="Manju") as ask_model, \
                patch.object(planner, "get_backend", side_effect=AssertionError("a hosted/planner backend was asked")):
            card = self.command(MOVE)
        ask_model.assert_called()
        self.assertEqual(card.slots["patient_name"], "Manju")
        row = self.only_row()
        self.assertEqual(row["route_detail"], "rule:move")            # no "+name_fill": the hosted read is a Sarvam-mode step
        self.assertIsNone(row["backend"])

    def test_when_the_local_model_is_down_the_honest_question_is_asked(self):
        self.reader_unsure()
        ask = self.command(MOVE)                                      # the live-model tripwire raises: "unavailable"
        self.assertEqual((ask.kind, ask.question), ("patient", HONEST))
        self.assertEqual(self.only_row()["route_detail"], "rule:move")

    def test_planner_routed_in_local_mode_is_unchanged(self):
        self.call("cancel_appointment", patient_name="Manju")
        result = self.command("Cancel Manju's appointment.")
        self.assertEqual(result.slots["patient_name"], "Manju")
        self.assertEqual(self.only_row()["route_taken"], "planner")

    def test_a_new_manju_sharma_is_not_the_registered_manju_in_local_mode_either(self):
        self.assertIsNone(llm_slots.match_known_name("Book Manju Sharma tomorrow at 5", ["Manju", "Seema"]))


class Wording(RouteCase):
    def test_the_question_is_honest_in_three_languages_and_the_chip_is_unchanged(self):
        for language, text, expected in (
                ("en-IN", "move the appointment to tomorrow at eleven am", HONEST),
                ("hi-IN", "move the appointment to tomorrow at eleven am", "Mujhe samajh nahi aaya ki patient kaun hai. Kaun sa patient?"),
                ("hi-IN", "अपॉइंटमेंट कल 11 बजे शिफ्ट करो", "मुझे समझ नहीं आया कि मरीज़ कौन है। कौन सा मरीज़?")):
            with self.subTest(language=language, text=text):
                self.ctx = VoiceContext(clock=self.clock)
                self.ctx.set_client_branch(1, 1)
                self.ctx.patient = {"id": 1, "name": "Rakesh Verma"}
                ask = handle_turn(self.ctx, self.conn, text, self.adapter, self.adapter, language, DEFER)
                self.assertEqual((ask.kind, ask.question), ("patient", expected))
                self.assertEqual(ask.options, [{"label": "Rakesh Verma", "patient_name": "Rakesh Verma"}])

    def test_only_the_missing_name_question_changed(self):
        self.assertEqual(voice_context.question_text("patient", "en-IN"), "Which patient?")            # the re-ask wording
        self.assertEqual(voice_context.question_text("choose_patient", "en-IN"), "Which one?")
        self.assertEqual(voice_context.question_text("choose_patient", "hi-IN"), "Kaun sa wala?")

    def test_answering_the_question_still_proceeds(self):
        ask = self.command("move the appointment to tomorrow at eleven am")
        self.assertEqual(ask.question, HONEST)
        card = handle_turn(self.ctx, self.conn, "Manju", self.adapter, self.adapter, "en-IN", DEFER)
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual((card.intent, card.slots["patient_name"]), ("reschedule_appointment", "Manju"))


class LoggingNeverGetsInTheWay(RouteCase):
    def test_a_rule_routed_command_still_works_when_logging_breaks(self):
        with patch.object(planner_log, "record", side_effect=RuntimeError("disk on fire")):
            card = self.command(MOVE)
        self.assertEqual((card.intent, card.slots["patient_name"]), ("reschedule_appointment", "Manju"))

    def test_a_locked_or_missing_table_is_swallowed(self):
        self.conn.execute("DROP TABLE planner_log")
        card = self.command(MOVE)
        self.assertEqual(card.intent, "reschedule_appointment")

    def test_an_old_table_without_the_new_column_is_swallowed_too(self):
        self.conn.execute("DROP TABLE planner_log")
        self.conn.execute("CREATE TABLE planner_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT DEFAULT (datetime('now')), "
                          "source TEXT DEFAULT 'voice', transcript TEXT NOT NULL, previous_turn TEXT, planner_tool TEXT, "
                          "planner_args_json TEXT, route_taken TEXT NOT NULL, final_intent TEXT, latency_ms INTEGER, "
                          "override_notes TEXT, outcome TEXT, backend TEXT, tokens_in INTEGER, tokens_out INTEGER, cost_paise INTEGER)")
        card = self.command(MOVE)
        self.assertEqual(card.intent, "reschedule_appointment")

    def test_the_log_switch_turns_rule_rows_off_too(self):
        settings.set_planner_log_enabled(self.conn, False)
        self.command(MOVE)
        self.assertEqual(self.log_rows(), [])

    def test_a_planner_switched_off_writes_nothing(self):
        with patch.dict(os.environ, {"INTENT_PLANNER_ENABLED": "0"}):
            self.command(MOVE)
        self.assertEqual(self.log_rows(), [])

    def test_the_outcome_of_a_rule_routed_card_can_be_recorded(self):
        self.command(MOVE)
        log_id = self.ctx.planner_log_id
        self.assertTrue(log_id)
        self.assertTrue(planner_log.set_outcome(self.conn, log_id, "approved"))
        self.assertEqual(self.only_row()["outcome"], "approved")

    def test_the_transcript_is_stored_exactly_as_for_planner_commands_and_nothing_more(self):
        self.command(MOVE)
        row = self.only_row()
        self.assertEqual(row["transcript"], MOVE)
        self.assertIsNone(row["previous_turn"])
        self.assertNotIn("Manju", " ".join(str(v) for k, v in row.items() if k != "transcript"))


class ExportAndOldDatabases(unittest.TestCase):
    def setUp(self):
        import importlib.util
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("export_planner_log", root / "scripts" / "export_planner_log.py")
        self.export = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.export)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "clinic_test.db")
        conn = db.connect(self.path)
        planner_log.record(conn, "voice", "Move Manju's appointment to tomorrow at eleven am.", None, None,
                           {"slots_found": ["appt_date", "start_time"], "slots_missing": ["patient_name"]}, "rules",
                           "reschedule_appointment", None, [], route_detail="rule:move")
        planner_log.record(conn, "voice", "Move Manju's appointment to tomorrow", None, None, {"slots_found": [], "slots_missing": []},
                           "rules", "reschedule_appointment", 800, ["sarvam: timeout"], backend="sarvam", route_detail="rule:move+name_fill")
        planner_log.record(conn, "voice", "book Amit tomorrow", None, "book_appointment", {"patient_name": "Amit"}, "planner",
                           "book_appointment", 900, [])
        conn.commit()
        conn.close()

    def rows(self, **kw):
        conn = self.export.open_read_only(self.path)
        self.addCleanup(conn.close)
        return [self.export.to_case(r) for r in self.export.fetch(conn, **kw)]

    def test_rule_rows_are_exported_without_a_tool_and_say_which_rule(self):
        cases = self.rows()
        self.assertEqual(len(cases), 3)
        self.assertEqual(cases[0][1:3], (None, {}))
        self.assertIn("route=rules/rule:move ", cases[0][4])
        self.assertIn("route=rules/rule:move+name_fill", cases[1][4])

    def test_problems_skip_plain_rule_rows_but_keep_a_failed_name_read(self):
        cases = self.rows(problems=True)
        self.assertEqual([c[0] for c in cases], ["Move Manju's appointment to tomorrow"])

    def test_problems_still_work_on_a_database_without_the_new_column(self):
        path = str(Path(self.tmp.name) / "old.db")
        old = sqlite3.connect(path)
        old.executescript("CREATE TABLE planner_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source TEXT, transcript TEXT, "
                          "previous_turn TEXT, planner_tool TEXT, planner_args_json TEXT, route_taken TEXT, final_intent TEXT, "
                          "latency_ms INTEGER, override_notes TEXT, outcome TEXT);"
                          "INSERT INTO planner_log (transcript, route_taken) VALUES ('x', 'rules');")
        old.commit()
        old.close()
        conn = self.export.open_read_only(path)
        self.addCleanup(conn.close)
        self.assertEqual(len(self.export.fetch(conn, problems=True)), 1)


if __name__ == "__main__":
    unittest.main()
