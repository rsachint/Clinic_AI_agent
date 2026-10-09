"""Model-first routing (clinic/nlu/dialogue.py): the five conversation tools mapped onto the voice machinery, the rules
code keeps for itself (a named person beats the remembered one, ids never come from the model, the deterministic readers
win), no approval by voice, the fall back to classic when the planner cannot answer, a read that interrupts a question,
the Nalin case end to end, and what lands in the planner log. Nothing here reaches a model: the planner is a fake."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.planner_support import PlannerCase  # noqa: E402

from clinic import architecture, core  # noqa: E402
from clinic.adapters.registry import build_write_handlers  # noqa: E402
from clinic.nlu import dialogue, planner, sarvam, tools  # noqa: E402
from clinic.pipeline import ParsedResult, PipelineError, ReadResult  # noqa: E402
from clinic.voice_context import AskResult, CardUpdate, Note  # noqa: E402
from clinic.voice_turns import handle_pick  # noqa: E402

A, B, C = 1, 2, 3


class ModelFirstCase(PlannerCase):
    def setUp(self):
        super().setUp()
        architecture.set_mode(self.conn, "model_first")
        self.day = self.tomorrow.isoformat()

    def script(self, *calls):
        """The planner answers the next commands with these calls, in order."""
        self.backend.script = list(calls)

    def book(self, name="Rakesh Verma", time="17:00", **extra):
        return ("book_appointment", dict({"patient_name": name, "date": self.day, "time": time}, **extra))

    def last_log(self):
        return self.log_rows()[-1]

    def user_message(self, index=-1):
        return self.backend.calls[index][1]


class TheCallCarriesTheStateCard(ModelFirstCase):
    def test_the_sentence_goes_with_the_state_card_and_the_24_tools(self):
        self.script(self.book())
        self.say("book Rakesh Verma tomorrow at 5 pm")
        system, user, schemas = self.backend.calls[0]
        self.assertTrue(user.startswith("STATE CARD"))
        self.assertTrue(user.endswith("New command: book Rakesh Verma tomorrow at 5 pm"))
        self.assertEqual(len(schemas), 24)
        self.assertEqual([t["function"]["name"] for t in schemas][-5:],
                         ["choose_option", "answer_slot", "correct_card", "new_patient", "cancel_task"])
        self.assertIn("STATE CARD", system)
        self.assertIn("no tool does it", system)                     # the prompt says nothing is approved by voice

    def test_the_open_question_and_the_card_reach_the_next_call(self):
        self.script(("book_appointment", {"patient_name": "Mohan", "date": self.day, "time": "17:00"}),
                    ("choose_option", {"index": 2}))
        self.say("book Mohan tomorrow at 5 pm")
        self.say("the second one")
        card = self.user_message(1)
        self.assertIn('"Which one?" (choose_patient)', card)
        self.assertIn("  1. Mohan Lal ...0002", card)
        self.assertIn("  2. Mohan Das ...0003", card)
        for hidden in ("9000000002", "9000000003"):
            self.assertNotIn(hidden, card)


class ChooseOption(ModelFirstCase):
    def test_the_number_picks_the_option_and_the_id_comes_from_the_app(self):
        self.script(("book_appointment", {"patient_name": "Mohan", "date": self.day, "time": "17:00"}),
                    ("choose_option", {"index": 2}))
        ask = self.say("book Mohan tomorrow at 5 pm")
        self.assertEqual([o["patient_name"] for o in ask.options], ["Mohan Lal", "Mohan Das"])
        card = self.say("the second one")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.resolved["patient_label"], "Mohan Das (9000000003)")
        self.assertEqual(card.slots["patient_id"], 3)
        self.assertIsNone(self.ctx.pending)

    def test_a_number_that_is_not_an_option_asks_again(self):
        self.script(("book_appointment", {"patient_name": "Mohan", "date": self.day, "time": "17:00"}),
                    ("choose_option", {"index": 9}))
        self.say("book Mohan tomorrow at 5 pm")
        again = self.say("the ninth one")
        self.assertIsInstance(again, AskResult)
        self.assertEqual(again.kind, "choose_patient")
        self.assertEqual(self.ctx.pending["kind"], "choose_patient")

    def test_with_no_question_open_the_classic_routing_takes_the_turn(self):
        self.script(("choose_option", {"index": 1}))
        with self.assertRaises(PipelineError):
            self.say("the first one")
        self.assertEqual(self.last_log()["route_detail"], "mf_fallback_classic")

    def test_a_tap_still_works_in_model_first_mode(self):
        self.script(("book_appointment", {"patient_name": "Mohan", "date": self.day, "time": "17:00"}))
        self.say("book Mohan tomorrow at 5 pm")
        _, card = handle_pick(self.ctx, self.conn, 0, self.adapter, self.adapter, "en-IN", self.deferred())
        self.assertEqual(card.slots["patient_id"], 2)

    def deferred(self):
        return frozenset({"book_appointment", "cancel_appointment", "reschedule_appointment"})


class AnswerSlot(ModelFirstCase):
    def test_a_day_a_time_and_the_task_carries_on(self):
        self.script(("reschedule_appointment", {"patient_name": "Rakesh Verma"}),
                    ("answer_slot", {"slot": "date", "value": self.day}),
                    ("answer_slot", {"slot": "time", "value": "18:00"}))
        self.assertEqual(self.say("move Rakesh Verma's appointment").kind, "date")
        self.assertEqual(self.say("tomorrow").kind, "time")
        card = self.say("six pm")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual((card.slots["appt_date"], card.slots["start_time"]), (self.day, "18:00"))

    def test_the_date_code_reads_wins_over_the_models(self):
        from datetime import timedelta
        wrong = (self.tomorrow + timedelta(days=3)).isoformat()
        self.script(("reschedule_appointment", {"patient_name": "Rakesh Verma"}), ("answer_slot", {"slot": "date", "value": wrong}))
        self.say("move Rakesh Verma's appointment")
        ask = self.say("tomorrow")
        self.assertEqual(ask.slots["appt_date"], self.day)                       # "tomorrow" is read by code, not taken from the model

    def test_a_date_code_cannot_read_is_taken_from_the_model(self):
        self.script(("reschedule_appointment", {"patient_name": "Rakesh Verma"}), ("answer_slot", {"slot": "date", "value": self.day}))
        self.say("move Rakesh Verma's appointment")
        ask = self.say("the day after the clinic reopens")
        self.assertEqual(ask.slots["appt_date"], self.day)

    def test_a_day_answer_that_also_says_the_time_fills_both(self):
        self.script(("reschedule_appointment", {"patient_name": "Rakesh Verma"}),
                    ("answer_slot", {"slot": "date", "value": self.day}))
        self.say("move Rakesh Verma's appointment")
        card = self.say("tomorrow at 5 pm")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.slots["start_time"], "17:00")

    def test_the_phone_is_read_from_the_sentence_not_the_models_digits(self):
        self.script(self.book("Kavita Rao"), ("answer_slot", {"slot": "phone", "value": "9111223399"}))
        self.assertEqual(self.say("book Kavita Rao tomorrow at 5 pm").kind, "phone")
        card = self.say("nine eight seven six five zero zero three zero one")
        self.assertEqual(card.slots["patient_phone"], "9876500301")

    def test_a_phone_with_too_few_digits_is_asked_again(self):
        self.script(self.book("Kavita Rao"), ("answer_slot", {"slot": "phone", "value": "98765432"}))
        self.say("book Kavita Rao tomorrow at 5 pm")
        again = self.say("98765432")
        self.assertEqual(again.kind, "phone")

    def test_a_branch_answer(self):
        self.script(("close_branch", {"branch": "B", "start_date": self.day}))
        plan = self.say("close the branch tomorrow")
        self.assertEqual(plan.intent, "close_branch")

    def test_a_name_answers_which_patient_and_ambiguity_is_a_question(self):
        self.script(("reschedule_appointment", {"patient_name": "Someone", "new_date": self.day, "new_time": "10:00"}),
                    ("answer_slot", {"slot": "patient_name", "value": "Mohan"}),
                    ("answer_slot", {"slot": "patient_name", "value": "Mohan Das"}))
        ask = self.say("move the appointment to tomorrow at 10")
        self.assertEqual(ask.kind, "patient")
        self.assertEqual(self.say("Mohan").kind, "choose_patient")                # two Mohans: never one of them guessed
        self.assertEqual(self.ctx.pending["kind"], "choose_patient")
        card = self.say("Mohan Das")
        self.assertEqual(card.slots["patient_name"], "Mohan Das")

    def test_nothing_open_is_not_an_answer(self):
        self.script(("answer_slot", {"slot": "time", "value": "17:00"}))
        with self.assertRaises(PipelineError):
            self.say("5 pm")
        self.assertEqual(self.last_log()["route_detail"], "mf_fallback_classic")


class CorrectCard(ModelFirstCase):
    def open_card(self, name="Rakesh Verma"):
        self.script(self.book(name))
        card = self.say("book {} tomorrow at 5 pm".format(name))
        self.assertIsInstance(card, ParsedResult)
        return card

    def test_the_time_and_the_reader_wins(self):
        self.open_card()
        self.script(("correct_card", {"field": "time", "value": "07:00"}))
        update = self.say("actually make it 7")
        self.assertIsInstance(update, CardUpdate)
        self.assertEqual(update.changes, {"start_time": "19:00"})                # "7" is read in clinic hours by code
        self.assertEqual(self.ctx.open_card["slots"]["start_time"], "19:00")

    def test_the_date_and_the_branch(self):
        self.open_card()
        self.script(("correct_card", {"field": "date", "value": "2026-10-14"}), ("correct_card", {"field": "branch", "value": "B"}))
        self.assertEqual(self.say("change the date to the fourteenth").changes, {"appt_date": "2026-10-14"})
        update = self.say("move it to Branch B")
        self.assertEqual(update.changes, {"branch_id": B})
        self.assertIn("Branch", update.summary)

    def test_a_field_the_card_does_not_have(self):
        self.script(("query", {"entity": "appointments", "aggregate": "list", "date": self.day}))
        self.say("who is booked tomorrow")
        self.script(("correct_card", {"field": "fee", "value": "300"}))
        self.assertIsInstance(self.say("make the fee 300"), Note)                # no card is open at all
        self.open_card()
        self.script(("correct_card", {"field": "fee", "value": "300"}))
        note = self.say("make the fee 300")
        self.assertIsInstance(note, Note)
        self.assertIn("no fee", note.message)

    def test_a_new_patient_name_drops_the_old_id_and_selects_the_new_one(self):
        self.open_card("Rakesh Verma")
        self.ctx.open_card["slots"]["patient_id"] = 1                           # as a card that carried the old patient's id
        self.script(("correct_card", {"field": "patient_name", "value": "Sunita Devi"}))
        update = self.say("no, it's for Sunita Devi")
        self.assertEqual(update.changes["patient_name"], "Sunita Devi")
        self.assertEqual(update.changes["patient_id"], 4)                        # the registered Sunita, never Rakesh
        self.assertEqual(self.ctx.open_card["slots"]["patient_id"], 4)
        self.assertEqual(self.ctx.patient["name"], "Sunita Devi")

    def test_an_unregistered_name_clears_the_id_and_forgets_the_remembered_patient(self):
        self.open_card("Rakesh Verma")
        self.ctx.open_card["slots"]["patient_id"] = 1
        self.script(("correct_card", {"field": "patient_name", "value": "Kavita Rao"}))
        update = self.say("sorry, Kavita Rao")
        self.assertIsNone(update.changes["patient_id"])
        self.assertNotIn("patient_id", {k: v for k, v in self.ctx.open_card["slots"].items() if v})
        self.assertIsNone(self.ctx.patient)

    def test_two_patients_with_that_name_are_not_guessed(self):
        self.conn.execute("INSERT INTO patients (name, phone, age) VALUES ('Sunita Devi', '9000000044', 30)")
        self.conn.commit()
        self.open_card()
        self.script(("correct_card", {"field": "patient_name", "value": "Sunita Devi"}))
        note = self.say("no, it's for Sunita Devi")
        self.assertIsInstance(note, Note)
        self.assertIn("More than one", note.message)

    def test_a_new_phone_for_another_person_drops_the_registered_id(self):
        self.open_card()
        self.ctx.open_card["slots"]["patient_id"] = 1
        self.script(("correct_card", {"field": "phone", "value": "9111223344"}))
        self.say("no, the phone is 9111223344")
        self.assertNotIn("patient_id", self.ctx.open_card["slots"])


class NewPatient(ModelFirstCase):
    def test_the_booking_keeps_its_slots_and_asks_for_the_phone(self):
        self.script(self.book("Mohan"), ("new_patient", {"name": "Nalin"}))
        self.assertEqual(self.say("book Mohan tomorrow at 5 pm").kind, "choose_patient")
        ask = self.say("no, it's a new patient called Nalin")
        self.assertEqual(ask.kind, "phone")
        self.assertEqual((ask.slots["patient_name"], ask.slots["appt_date"], ask.slots["start_time"]), ("Nalin", self.day, "17:00"))
        self.assertNotIn("patient_id", ask.slots)
        self.assertEqual((self.ctx.pending["kind"], self.ctx.pending["intent"]), ("phone", "book_appointment"))

    def test_a_phone_already_heard_is_not_asked_for_again(self):
        self.script(self.book("Someone", phone="9988776655"), ("new_patient", {"name": "Nalin"}))
        self.assertEqual(self.say("book an appointment tomorrow at 5 pm, phone 9988776655").kind, "patient")
        card = self.say("it's a new patient called Nalin")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual((card.slots["patient_name"], card.slots["patient_phone"]), ("Nalin", "9988776655"))

    def test_a_name_that_is_registered_exactly_is_that_registered_patient(self):
        self.script(self.book("Mohan"), ("new_patient", {"name": "Rakesh Verma"}))
        self.say("book Mohan tomorrow at 5 pm")
        ask = self.say("it's a new patient called Rakesh Verma")
        self.assertIsInstance(ask, ParsedResult)
        self.assertEqual(ask.resolved["patient_label"], "Rakesh Verma (9000000001)")        # no second Rakesh is created

    def test_only_a_booking_can_take_a_new_patient(self):
        self.script(("cancel_appointment", {"patient_name": "Zebra"}), ("new_patient", {"name": "Nalin"}))
        self.say("cancel Zebra's appointment")
        self.ctx.ask("cancel_appointment", {"patient_name": "Zebra"}, "patient")
        note = self.say("it's a new patient called Nalin")
        self.assertIsInstance(note, Note)
        self.assertIn("no appointment", note.message)

    def test_the_name_must_be_one_the_user_said(self):
        self.script(self.book("Mohan"), ("new_patient", {"name": "Invented Name"}))
        self.say("book Mohan tomorrow at 5 pm")
        again = self.say("it's a new patient")
        self.assertIsInstance(again, AskResult)
        self.assertEqual(again.kind, "choose_patient")


class NalinEndToEnd(ModelFirstCase):
    """The bug: after 'Which one?' the reply 'it's a new patient called Nalin' was read as a new command and the
    booking was lost. Model-first: the booking continues, the phone is asked, and Approve registers Nalin once."""

    def test_the_whole_conversation(self):
        self.script(self.book("Mohan"), ("new_patient", {"name": "Nalin"}),
                    ("answer_slot", {"slot": "phone", "value": "9988776655"}))
        self.assertEqual(self.say("book Mohan tomorrow at 5 pm").kind, "choose_patient")
        self.assertEqual(self.say("no, it's a new patient called Nalin").kind, "phone")
        card = self.say("9 9 8 8 7 7 6 6 5 5")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(self.table_counts("patients", "appointments"), [4, 3])         # nothing written yet
        handlers = build_write_handlers(self.adapter, self.adapter)
        slots = dict(self.ctx.open_card["slots"])
        proposal = core.propose(self.conn, "book_appointment", slots, source_text="approve")
        core.confirm(self.conn, proposal, handlers)                                     # the Approve button
        self.assertEqual(self.table_counts("patients", "appointments"), [5, 4])
        nalin = self.conn.execute("SELECT * FROM patients WHERE name = 'Nalin'").fetchall()
        self.assertEqual(len(nalin), 1)
        self.assertEqual(nalin[0]["phone"], "9988776655")
        booked = self.conn.execute("SELECT * FROM appointments WHERE patient_id = ?", (nalin[0]["id"],)).fetchone()
        self.assertEqual((booked["appt_date"], booked["start_time"]), (self.day, "17:00"))


class CancelTask(ModelFirstCase):
    def test_a_question_is_dropped(self):
        self.script(("reschedule_appointment", {"patient_name": "Rakesh Verma"}), ("cancel_task", {}))
        self.say("move Rakesh Verma's appointment")
        note = self.say("never mind")
        self.assertIsInstance(note, Note)
        self.assertIn("dropped", note.message)
        self.assertIsNone(self.ctx.pending)

    def test_with_a_card_open_nothing_is_changed_and_the_card_stays(self):
        self.script(self.book(), ("cancel_task", {}))
        self.say("book Rakesh Verma tomorrow at 5 pm")
        note = self.say("never mind")
        self.assertIn("press Reject", note.message)
        self.assertIsNotNone(self.ctx.open_card)

    def test_in_hindi_session_it_answers_in_hinglish(self):
        self.script(("reschedule_appointment", {"patient_name": "Rakesh Verma"}), ("cancel_task", {}))
        self.say("move Rakesh Verma's appointment")
        from clinic.voice_turns import handle_turn
        note = handle_turn(self.ctx, self.conn, "rehne do", self.adapter, self.adapter, "hi-IN", self.deferred_all())
        self.assertEqual(note.message, "Theek hai, chhod diya.")

    def deferred_all(self):
        return frozenset({"book_appointment", "cancel_appointment", "reschedule_appointment"})


class NoApprovalByVoice(ModelFirstCase):
    def test_no_tool_can_approve(self):
        names = [t.name for t in tools.TOOLS + tools.DIALOGUE_TOOLS]
        for name in names:
            self.assertFalse(dialogue.approval_like_tool(name), name)               # none of the real tools looks like an approval
        for name in ("approve", "approve_card", "confirm", "confirm_booking", "save_card", "submit", "accept", "commit",
                     "finalize", "execute", "apply_changes", "yes", "go_ahead", "press_approve"):
            self.assertTrue(dialogue.approval_like_tool(name), name)

    def test_a_tool_the_model_invents_is_refused_and_nothing_runs(self):
        self.script(self.book(), ("approve_card", {"card": "open"}))
        self.say("book Rakesh Verma tomorrow at 5 pm")
        before = self.table_counts("appointments", "proposals", "audit_log")
        note = self.say("go on then, make it official")
        self.assertIsInstance(note, Note)
        self.assertIn("press Approve", note.message)
        self.assertEqual(self.table_counts("appointments", "proposals", "audit_log"), before)
        self.assertIsNotNone(self.ctx.open_card)                                    # the card is still waiting for a person
        self.assertEqual(self.last_log()["planner_tool"], "approve_card")
        self.assertIn("refused", self.last_log()["override_notes"])

    def test_an_approval_sentence_with_a_card_open_never_reaches_the_planner(self):
        self.script(self.book())
        self.say("book Rakesh Verma tomorrow at 5 pm")
        calls = len(self.backend.calls)
        for text in ("yes", "ok approve it", "confirm", "haan kar do", "yes confirm it please", "ठीक है कन्फर्म"):
            with self.subTest(text=text):
                note = self.say(text)
                self.assertIsInstance(note, Note)
                self.assertIn("Approve", note.message)
        self.assertEqual(len(self.backend.calls), calls)
        self.assertEqual(self.table_counts("appointments"), [3])

    def test_the_refusal_phrase_detector_is_narrow(self):
        for text in ("yes", "approve it", "ok save it", "haan kar do", "confirm"):
            self.assertTrue(dialogue.is_approval_phrase(text), text)
        for text in ("confirm Amit's appointment tomorrow", "book Rakesh Verma", "yes I want to book Amit tomorrow at 5",
                     "do it", "", "the second one", "cancel the appointment"):
            self.assertFalse(dialogue.is_approval_phrase(text), text)

    def test_an_approval_phrase_with_no_card_open_goes_to_the_planner_as_a_normal_sentence(self):
        self.script(("unsupported", {"reason": "nothing to approve"}))
        with self.assertRaises(PipelineError):
            self.say("yes approve it")
        self.assertEqual(len(self.backend.calls), 1)

    def test_the_registry_has_exactly_five_conversation_tools_and_none_writes(self):
        self.assertEqual([t.name for t in tools.DIALOGUE_TOOLS], ["choose_option", "answer_slot", "correct_card", "new_patient", "cancel_task"])
        self.assertTrue(all(not t.writes for t in tools.DIALOGUE_TOOLS))
        self.assertTrue(set(tools.BY_NAME).isdisjoint(tools.DIALOGUE_BY_NAME))


class CodeDecidesWhoTheyMean(ModelFirstCase):
    def remember_rakesh(self):
        self.script(("query", {"entity": "patients", "aggregate": "list", "patient_name": "Rakesh Verma"}))
        self.say("show me the details of patient Rakesh Verma")
        self.assertEqual(self.ctx.patient["name"], "Rakesh Verma")

    def test_a_person_named_in_the_sentence_beats_the_remembered_patient(self):
        self.remember_rakesh()
        self.script(self.book("Sunita Devi"))
        card = self.say("book Sunita Devi tomorrow at 5 pm")
        self.assertEqual(card.resolved["patient_label"], "Sunita Devi (9000000004)")

    def test_a_model_that_copies_the_remembered_patient_is_overruled_by_the_name_in_the_sentence(self):
        self.remember_rakesh()
        self.script(self.book("Rakesh Verma"))                                       # the model took it from the state card
        card = self.say("book Sunita Devi tomorrow at 5 pm")
        self.assertEqual(card.resolved["patient_label"], "Sunita Devi (9000000004)")

    def test_when_the_sentence_names_nobody_we_know_the_model_answer_is_refused_and_the_user_is_asked(self):
        self.remember_rakesh()
        self.script(self.book("Rakesh Verma"))
        ask = self.say("book Kamal tomorrow at 5 pm")
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.kind, "patient")
        self.assertIsNone(ask.slots.get("patient_name"))
        self.assertEqual([o["patient_name"] for o in ask.options], ["Rakesh Verma"])      # offered, never assumed

    def test_him_means_the_remembered_patient_with_its_id(self):
        self.remember_rakesh()
        self.script(self.book("Rakesh Verma"))
        card = self.say("book him tomorrow at 5 pm")
        self.assertEqual(card.resolved["patient_label"], "Rakesh Verma (9000000001)")
        self.assertEqual(card.slots["patient_id"], 1)

    def test_a_pronoun_that_only_owns_a_phone_number_does_not_bring_back_the_remembered_patient(self):
        self.remember_rakesh()
        self.script(self.book("Priya", phone="9111122233"))
        card = self.say("book a consultation for Priya, her mobile number is 9111122233, tomorrow at 5 pm")
        self.assertEqual(card.slots["patient_name"], "Priya")
        self.assertNotIn("patient_id", card.slots)

    def test_the_model_expanding_a_name_from_memory_is_cut_back_to_what_was_said(self):
        self.script(("book_appointment", {"patient_name": "Sunita Devi", "date": self.day, "time": "17:00"}))
        ask = self.say("book Sunita tomorrow at 5 pm")
        self.assertEqual(ask.slots["patient_name"], "Sunita")                          # the spoken word, not the card's longer name

    def test_a_row_of_the_list_on_screen_is_a_legitimate_source_of_the_name(self):
        self.script(("query", {"entity": "appointments", "aggregate": "list", "date": self.day}),
                    ("cancel_appointment", {"patient_name": "Mohan Lal"}))
        self.say("who is booked tomorrow")
        card = self.say("cancel the second one")
        self.assertEqual(card.slots["patient_name"], "Mohan Lal")

    def test_a_devanagari_sentence_with_a_roman_model_name(self):
        self.script(("cancel_appointment", {"patient_name": "Sunita Devi"}))
        card = self.say("सुनीता देवी की अपॉइंटमेंट रद्द करो")
        self.assertEqual(card.slots["patient_name"], "सुनीता देवी")
        self.assertEqual(card.resolved["patient_label"], "Sunita Devi (9000000004)")

    def test_spoken_name_is_the_users_words(self):
        self.assertEqual(dialogue.spoken_name("Priya Shah", "book Priya tomorrow"), "Priya")
        self.assertEqual(dialogue.spoken_name("Rahul Sharma", "राहुल शर्मा को बुक करो"), "राहुल शर्मा")
        self.assertEqual(dialogue.spoken_name("Invented", "book Priya tomorrow"), "")
        self.assertEqual(dialogue.spoken_name("Manju", "move Manju's appointment"), "Manju")

    def test_an_id_from_the_model_rejects_the_whole_call_and_the_rules_decide(self):
        self.script(("cancel_appointment", {"patient_name": "Sunita Devi", "patient_id": 4}))
        card = self.say("cancel Sunita Devi's appointment")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(self.last_log()["route_detail"], "mf_fallback_classic")
        self.assertIn("unknown_arg", self.last_log()["override_notes"])


class FallbackToClassic(ModelFirstCase):
    def test_a_timeout_hands_the_turn_to_the_classic_routing(self):
        self.backend.script = sarvam.SarvamError("timeout")
        card = self.say("book Rakesh Verma tomorrow at 5 pm")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.resolved["patient_label"], "Rakesh Verma (9000000001)")
        self.assertEqual(len(self.backend.calls), 1)                                  # the planner was NOT asked a second time
        row = self.last_log()
        self.assertEqual((row["route_taken"], row["route_detail"], row["planner_tool"]), ("rules", "mf_fallback_classic", None))
        self.assertIn("sarvam: timeout", row["override_notes"])
        self.assertTrue(row["state_card"].startswith("STATE CARD"))
        self.assertEqual(len(self.log_rows()), 1)                                     # one row for the turn, not two

    def test_no_tool_call_an_invalid_call_and_an_unknown_tool_all_fall_back(self):
        # (a booking without its time is no longer "invalid": the app asks for it, so a visit without a fee stands in)
        for answer in (None, ("record_visit", {"patient_name": "Rakesh Verma"}), ("not_a_tool", {}),
                       ("answer_slot", {"slot": "date", "value": "tomorrow-ish"})):
            with self.subTest(answer=answer):
                self.conn.execute("DELETE FROM planner_log")
                self.backend.script = answer
                card = self.say("book Rakesh Verma tomorrow at 5 pm")
                self.assertIsInstance(card, ParsedResult)
                self.assertEqual(self.last_log()["route_detail"], "mf_fallback_classic")

    def test_an_open_circuit_breaker_falls_back_without_a_call(self):
        sarvam_backend = sarvam.SarvamBackend(api_key="k", transport=None)
        for _ in range(3):
            sarvam_backend.breaker.record_failure()
        planner.set_backend(sarvam_backend)
        with patch.object(sarvam_backend, "_post", side_effect=AssertionError("a call went out with the breaker open")):
            card = self.say("book Rakesh Verma tomorrow at 5 pm")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(self.last_log()["route_detail"], "mf_fallback_classic")
        self.assertIn("circuit open", self.last_log()["override_notes"])

    def test_no_key_falls_back(self):
        with patch.dict(os.environ, {"SARVAM_API_KEY": "", "PLANNER_BACKEND": "sarvam"}):
            planner.set_backend(sarvam.SarvamBackend())
            card = self.say("book Rakesh Verma tomorrow at 5 pm")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(self.last_log()["route_detail"], "mf_fallback_classic")

    def test_planner_switched_off_means_classic_with_no_model_first_row(self):
        with patch.dict(os.environ, {"INTENT_PLANNER_ENABLED": "0"}):
            card = self.say("book Rakesh Verma tomorrow at 5 pm")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.log_rows(), [])

    def test_an_unsupported_call_is_final_and_keeps_the_open_question(self):
        self.script(("reschedule_appointment", {"patient_name": "Rakesh Verma"}), ("unsupported", {"reason": "weather"}))
        self.say("move Rakesh Verma's appointment")
        with self.assertRaises(PipelineError):
            self.say("how is the weather")
        self.assertEqual(self.ctx.pending["kind"], "date")                              # still waiting for the day

    def test_an_unanswerable_read_is_saved_for_a_person(self):
        self.script(("unsupported", {"reason": "no such data", "wanted": "profit this month"}))
        note = self.say("what is our profit this month")
        self.assertIsInstance(note, Note)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM unanswered_questions").fetchone()[0], 1)

    def test_the_branch_switch_and_closing_a_branch_stay_rules(self):
        self.script(("switch_branch", {"branch": "B"}))
        result = self.say("switch to branch B")
        self.assertEqual(result.__class__.__name__, "SwitchBranchResult")
        self.assertEqual(self.backend.calls, [])                                       # the planner was not asked at all
        plan = self.say("close branch B tomorrow")
        self.assertEqual(plan.__class__.__name__, "ClosurePlanResult")
        self.assertEqual(self.backend.calls, [])


class ReadInterruptsTheTask(ModelFirstCase):
    def test_the_question_survives_the_read_and_is_asked_again(self):
        self.script(("reschedule_appointment", {"patient_name": "Rakesh Verma"}),
                    ("query", {"entity": "appointments", "aggregate": "list", "date": self.day}),
                    ("answer_slot", {"slot": "date", "value": self.day}))
        self.assertEqual(self.say("move Rakesh Verma's appointment").kind, "date")
        read = self.say("who is booked tomorrow")
        self.assertIsInstance(read, ReadResult)
        self.assertIn("Still waiting for your answer: Which day?", read.answer_text)
        self.assertTrue(read.answer_text.rstrip().endswith("."))
        self.assertEqual((self.ctx.pending["kind"], self.ctx.pending["intent"]), ("date", "reschedule_appointment"))
        self.assertIsNone(self.ctx.resume_pending)
        self.assertEqual(self.say("tomorrow").kind, "time")                              # the task goes on

    def test_the_state_card_after_the_read_still_shows_the_question(self):
        self.script(("reschedule_appointment", {"patient_name": "Rakesh Verma"}),
                    ("patient_lookup_is_not_a_tool", {}))
        self.say("move Rakesh Verma's appointment")
        self.script(("query", {"entity": "patients", "aggregate": "count"}), ("cancel_task", {}))
        self.say("how many patients are registered")
        self.say("never mind")
        self.assertIn("Waiting for the user's answer", self.user_message(-1))
        self.assertIn("Task in progress: reschedule_appointment", self.user_message(-1))

    def test_a_new_write_command_replaces_the_question(self):
        self.script(("reschedule_appointment", {"patient_name": "Rakesh Verma"}),
                    ("cancel_appointment", {"patient_name": "Sunita Devi"}))
        self.say("move Rakesh Verma's appointment")
        card = self.say("cancel Sunita Devi's appointment")
        self.assertEqual(card.intent, "cancel_appointment")
        self.assertIsNone(self.ctx.pending)

    def test_a_failed_read_does_not_leave_a_dangling_question(self):
        self.script(("reschedule_appointment", {"patient_name": "Rakesh Verma"}),
                    ("query", {"entity": "appointments", "aggregate": "list", "patient_name": "Zebra", "limit": 1}))
        self.say("move Rakesh Verma's appointment")
        with self.assertRaises(PipelineError):
            self.say("when is Zebra's next appointment")
        self.assertIsNone(self.ctx.resume_pending)
        self.assertEqual(self.ctx.pending["kind"], "date")


class LoggingAndLatencyFields(ModelFirstCase):
    def test_a_row_carries_the_tool_the_detail_and_the_state_card(self):
        self.script(self.book())
        self.say("book Rakesh Verma tomorrow at 5 pm")
        row = self.last_log()
        self.assertEqual((row["route_taken"], row["route_detail"], row["planner_tool"], row["final_intent"]),
                         ("planner", "mf:book_appointment", "book_appointment", "book_appointment"))
        self.assertTrue(row["state_card"].startswith("STATE CARD"))
        self.assertIn("Today:", row["state_card"])
        self.assertEqual(row["backend"], "fake")

    def test_the_second_turns_row_holds_the_card_that_was_sent_with_it(self):
        self.script(("reschedule_appointment", {"patient_name": "Rakesh Verma"}), ("answer_slot", {"slot": "date", "value": self.day}))
        self.say("move Rakesh Verma's appointment")
        self.say("tomorrow")
        first, second = self.log_rows()
        self.assertNotIn("Waiting for", first["state_card"])
        self.assertIn('Waiting for the user\'s answer to: "Which day?" (date)', second["state_card"])
        self.assertEqual(second["state_card"], self.user_message(1).split("\n\nNew command:")[0])

    def test_no_id_or_full_phone_number_is_logged_in_the_card(self):
        self.script(self.book("Mohan"))
        self.say("book Mohan tomorrow at 5 pm")
        self.script(("choose_option", {"index": 1}))
        self.say("the first")
        for row in self.log_rows():
            self.assertNotRegex(row["state_card"], r"\d{5,}")

    def test_the_cards_outcome_is_recorded_on_that_row(self):
        self.script(self.book())
        self.say("book Rakesh Verma tomorrow at 5 pm")
        from clinic import planner_log
        planner_log.set_outcome(self.conn, self.ctx.planner_log_id, "approved")
        self.assertEqual(self.last_log()["outcome"], "approved")

    def test_logging_off_writes_nothing_and_does_not_get_in_the_way(self):
        from clinic import settings
        settings.set_planner_log_enabled(self.conn, False)
        self.script(self.book())
        self.assertIsInstance(self.say("book Rakesh Verma tomorrow at 5 pm"), ParsedResult)
        self.assertEqual(self.log_rows(), [])

    def test_an_old_log_table_without_the_column_never_breaks_a_command(self):
        self.conn.execute("ALTER TABLE planner_log RENAME TO planner_log_new")
        self.conn.execute("CREATE TABLE planner_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL DEFAULT (datetime('now')), "
                          "source TEXT NOT NULL DEFAULT 'voice', transcript TEXT NOT NULL, previous_turn TEXT, planner_tool TEXT, "
                          "planner_args_json TEXT, route_taken TEXT NOT NULL, final_intent TEXT, latency_ms INTEGER, "
                          "override_notes TEXT, outcome TEXT)")
        self.script(self.book())
        with self.assertLogs("clinic.planner_log", level="WARNING"):
            self.assertIsInstance(self.say("book Rakesh Verma tomorrow at 5 pm"), ParsedResult)     # the failed INSERT is swallowed


class OneCommandPerTurn(ModelFirstCase):
    def test_only_the_first_call_of_several_is_used_and_it_is_one_per_turn(self):
        self.script(self.book(), self.book("Sunita Devi"))
        self.say("book Rakesh Verma tomorrow at 5 pm")
        self.assertEqual(len(self.backend.calls), 1)                                    # one planner call for the sentence
        self.assertEqual(len(self.log_rows()), 1)


if __name__ == "__main__":
    unittest.main()
