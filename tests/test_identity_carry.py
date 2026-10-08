"""A person is resolved once; from then on the card carries their id and a name is only for display.
A name search never picks one of several people: it asks, or it lists everyone with the phone's last
four digits and pre-selects nothing. Dates here are relative to today, never fixed."""

import copy
import sqlite3
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from clinic import voice_context
from clinic.adapters.local_sqlite import LocalSQLiteAdapter
from clinic.nlu.parser import QUEUE_WRITE_INTENTS
from clinic.pipeline import ParsedResult, PipelineError, respond_to_intent
from clinic.voice_context import AskResult, CardUpdate, VoiceContext
from clinic.voice_turns import _try_card_edit, handle_pick, handle_turn

SCHEMA = (Path(__file__).resolve().parents[1] / "clinic" / "schema.sql").read_text()
DEFER = frozenset({"book_appointment", "cancel_appointment", "reschedule_appointment", "record_visit",
                   "set_followup", "log_attendance", "register_patient"}) | QUEUE_WRITE_INTENTS
TODAY = date.today()


def day(offset):
    return (TODAY + timedelta(days=offset)).isoformat()


class IdentityTestCase(unittest.TestCase):
    """Patients 1 and 2 share the full name Amit Dua (phones ...0301 and ...0402); 3 is Ravi Kumar, who is
    alone with that name; Sunita Walk is a walk-in with no patient row."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.addCleanup(self.conn.close)
        for name, phone in (("Amit Dua", "9876500301"), ("Amit Dua", "9876500402"), ("Ravi Kumar", "9876500503")):
            self.conn.execute("INSERT INTO patients (name, phone, age) VALUES (?, ?, 40)", (name, phone))
        self.first = self.book(1, None, 3, "10:00")          # Amit Dua ...0301, in 3 days
        self.second = self.book(2, None, 5, "11:00")         # Amit Dua ...0402, in 5 days
        self.second_nearest = self.book(2, None, 4, "09:30")   # ...0402 again, nearer than the first one's
        self.ravi_far = self.book(3, None, 8, "10:00")
        self.ravi_near = self.book(3, None, 2, "10:00")
        self.walk_in = self.book(None, "Sunita Walk", 1, "15:00")
        self.conn.commit()
        self.adapter = LocalSQLiteAdapter()
        self.names = {"text": None}
        for target, kwargs in (("clinic.nlu.parser.pick_intent", {"return_value": None}),
                               ("clinic.nlu.parser.extract_name", {"side_effect": lambda text: self.names["text"]})):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def book(self, patient_id, name, days, start, status="booked"):
        cur = self.conn.execute(
            "INSERT INTO appointments (patient_id, patient_name, appt_date, start_time, duration_minutes, status) "
            "VALUES (?, ?, ?, ?, 30, ?)", (patient_id, name, day(days), start, status))
        return cur.lastrowid

    def respond(self, intent, slots, context=None):
        return respond_to_intent(self.conn, intent, dict(slots), "", self.adapter, self.adapter, "en-IN",
                                 DEFER, context=context)

    def say(self, ctx, text, name=None):
        self.names["text"] = name
        return handle_turn(ctx, self.conn, text, self.adapter, self.adapter, "en-IN", DEFER)

    @staticmethod
    def ids(card):
        return [o["id"] for o in card.resolved["appointments"]]


class CancelAndRescheduleTests(IdentityTestCase):
    def test_cancel_after_choosing_the_second_amit_lists_only_the_seconds_appointments(self):
        ctx = VoiceContext()
        ask = self.say(ctx, "cancel Amit Dua's appointment", name="Amit Dua")
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.kind, "choose_patient")
        index = [o["patient_id"] for o in ask.options].index(2)
        _, card = handle_pick(ctx, self.conn, index, self.adapter, self.adapter, "en-IN", DEFER)
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(self.ids(card), [self.second_nearest, self.second])        # not the first Amit's
        self.assertEqual(card.slots["appointment_id"], self.second_nearest)         # the nearest of his own
        self.assertEqual(card.resolved["patient_id"], 2)

    def test_cancel_by_name_with_no_id_shows_both_people_and_selects_nothing(self):
        card = self.respond("cancel_appointment", {"patient_name": "Amit Dua"})
        self.assertEqual(sorted(self.ids(card)), sorted([self.first, self.second, self.second_nearest]))
        self.assertNotIn("appointment_id", card.slots)
        self.assertNotIn("patient_id", card.resolved)
        labels = {o["id"]: o["label"] for o in card.resolved["appointments"]}
        self.assertIn("…0301", labels[self.first])
        self.assertIn("…0402", labels[self.second])
        self.assertIn("…0402", labels[self.second_nearest])
        self.assertEqual(len(set(labels.values())), 3)

    def test_reschedule_by_name_with_no_id_shows_both_people_and_selects_nothing(self):
        card = self.respond("reschedule_appointment", {"patient_name": "Amit Dua", "appt_date": day(9), "start_time": "10:00"})
        self.assertEqual(len(self.ids(card)), 3)
        self.assertNotIn("appointment_id", card.slots)
        self.assertTrue(all("…" in o["label"] for o in card.resolved["appointments"]))

    def test_reschedule_with_a_chosen_id_lists_only_that_patient(self):
        card = self.respond("reschedule_appointment", {"patient_name": "Amit Dua", "patient_id": 1,
                                                       "appt_date": day(9), "start_time": "10:00"})
        self.assertEqual(self.ids(card), [self.first])
        self.assertEqual(card.slots["appointment_id"], self.first)
        self.assertEqual(card.resolved["patient_id"], 1)

    def test_a_phone_number_settles_who_and_lists_only_their_appointments(self):
        card = self.respond("cancel_appointment", {"patient_name": "Amit Dua", "patient_phone": "9876500402"})
        self.assertEqual(self.ids(card), [self.second_nearest, self.second])
        self.assertEqual(card.slots["appointment_id"], self.second_nearest)

    def test_a_single_person_name_still_preselects_the_nearest(self):
        card = self.respond("cancel_appointment", {"patient_name": "Ravi Kumar"})
        self.assertEqual(self.ids(card), [self.ravi_near, self.ravi_far])
        self.assertEqual(card.slots["appointment_id"], self.ravi_near)
        self.assertTrue(all("…" not in o["label"] for o in card.resolved["appointments"]))     # no phone clutter
        self.assertEqual(card.resolved["patient_id"], 3)

    def test_a_walk_in_with_no_patient_row_is_still_found_by_name(self):
        card = self.respond("cancel_appointment", {"patient_name": "Sunita Walk"})
        self.assertEqual(self.ids(card), [self.walk_in])
        self.assertEqual(card.slots["appointment_id"], self.walk_in)
        self.assertNotIn("patient_id", card.resolved)

    def test_nothing_found_keeps_the_note(self):
        card = self.respond("cancel_appointment", {"patient_name": "Nobody Atall"})
        self.assertEqual(card.resolved["appointments"], [])
        self.assertIn("No upcoming appointment found for Nobody Atall", card.resolved["note"])

    def test_a_chosen_patient_with_nothing_upcoming_is_not_replaced_by_the_namesake(self):
        self.conn.execute("UPDATE appointments SET status = 'cancelled' WHERE patient_id = 2")
        card = self.respond("cancel_appointment", {"patient_name": "Amit Dua", "patient_id": 2})
        self.assertEqual(card.resolved["appointments"], [])
        self.assertIn("No upcoming appointment found", card.resolved["note"])
        self.assertNotIn("appointment_id", card.slots)

    def test_an_appointment_picked_from_the_screen_settles_whose_it_is(self):
        # "Cancel the second one" from a list that showed both Amits: the appointment says whose it is.
        ctx = VoiceContext()
        ctx.remember_list([{"id": self.first, "patient_name": "Amit Dua"}, {"id": self.second, "patient_name": "Amit Dua"}], "x")
        card = self.say(ctx, "cancel the second one", name=None)
        self.assertEqual(card.slots["appointment_id"], self.second)
        self.assertEqual(card.resolved["patient_id"], 2)
        self.assertEqual(self.ids(card), [self.second_nearest, self.second])


class QueueTests(IdentityTestCase):
    def setUp(self):
        super().setUp()
        self.conn.execute("DELETE FROM appointments")
        self.amit1 = self.book(1, None, 0, "10:00")
        self.amit2 = self.book(2, None, 0, "11:00")
        self.ravi = self.book(3, None, 0, "12:00")
        self.walk = self.book(None, "Sunita Walk", 0, "13:00")
        self.conn.commit()

    def queue(self, slots, intent="queue_check_in"):
        return self.respond(intent, slots)

    def test_two_same_named_people_waiting_are_not_guessed(self):
        card = self.queue({"patient_name": "Amit Dua"})
        self.assertIsNone(card.slots["appointment_id"])
        self.assertIn("More than one person named 'Amit Dua'", card.resolved["note"])
        self.assertEqual(len(card.resolved["appointments"]), 4)          # everyone waiting, for a human to pick

    def test_a_chosen_id_picks_that_persons_entry(self):
        card = self.queue({"patient_name": "Amit Dua", "patient_id": 2})
        self.assertEqual(card.slots["appointment_id"], self.amit2)
        self.assertNotIn("note", card.resolved)

    def test_a_unique_name_and_a_walk_in_are_still_picked(self):
        self.assertEqual(self.queue({"patient_name": "Ravi Kumar"}, "queue_call_next").slots["appointment_id"], self.ravi)
        self.assertEqual(self.queue({"patient_name": "Sunita Walk"}, "queue_mark_done").slots["appointment_id"], self.walk)

    def test_a_walk_in_and_a_patient_with_the_same_name_are_not_guessed_either(self):
        self.book(None, "Ravi Kumar", 0, "14:00")
        card = self.queue({"patient_name": "Ravi Kumar"})
        self.assertIsNone(card.slots["appointment_id"])
        self.assertIn("More than one person", card.resolved["note"])

    def test_a_name_nobody_waiting_has_says_so(self):
        card = self.queue({"patient_name": "Zed Nobody"})
        self.assertIsNone(card.slots["appointment_id"])
        self.assertIn("No one named 'Zed Nobody'", card.resolved["note"])

    def test_a_token_still_wins(self):
        card = self.queue({"patient_name": "Amit Dua", "token": 3})
        self.assertEqual(card.slots["appointment_id"], self.ravi)


class NameChangedMidFlowTests(IdentityTestCase):
    def test_a_new_name_answering_which_patient_drops_the_old_id(self):
        ctx = VoiceContext()
        ctx.ask("book_appointment", {"patient_id": 1, "patient_name": "Amit Dua"}, "patient")
        ask = self.say(ctx, "Ravi Kumar", name="Ravi Kumar")
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.kind, "date")
        self.assertEqual(ask.slots["patient_name"], "Ravi Kumar")
        self.assertEqual(ask.slots["patient_id"], 3)              # Ravi's, resolved once; never Amit's 1

    def test_a_name_nobody_has_leaves_no_id_at_all(self):
        ctx = VoiceContext()
        ctx.ask("book_appointment", {"patient_id": 1, "patient_name": "Amit Dua"}, "patient")
        ask = self.say(ctx, "Zed Newcomer", name="Zed Newcomer")
        self.assertEqual(ask.slots["patient_name"], "Zed Newcomer")
        self.assertNotIn("patient_id", ask.slots)

    def test_an_unregistered_name_for_a_cancel_drops_the_old_id_too(self):
        ctx = VoiceContext()
        ctx.ask("cancel_appointment", {"patient_id": 1, "patient_name": "Amit Dua"}, "patient")
        card = self.say(ctx, "Sunita Walk", name="Sunita Walk")
        self.assertEqual(self.ids(card), [self.walk_in])
        self.assertNotIn("patient_id", card.slots)


class CardEditTests(IdentityTestCase):
    def open_card(self, ctx, slots):
        ctx.open_card_for("c1", "book_appointment", slots)
        ctx.remember_patient(slots["patient_id"], slots["patient_name"])

    def test_a_time_edit_keeps_the_id(self):
        ctx = VoiceContext()
        self.open_card(ctx, {"patient_id": 2, "patient_name": "Amit Dua", "appt_date": day(2), "start_time": "10:00"})
        update = _try_card_edit(ctx, self.conn, "make it 6 pm instead")
        self.assertIsInstance(update, CardUpdate)
        self.assertEqual(ctx.open_card["slots"]["start_time"], "18:00")
        self.assertEqual(ctx.open_card["slots"]["patient_id"], 2)
        self.assertEqual(ctx.patient["id"], 2)

    def test_an_edit_that_changes_the_name_clears_the_id_and_the_remembered_patient(self):
        ctx = VoiceContext()
        self.open_card(ctx, {"patient_id": 1, "patient_name": "Amit Dua", "appointment_id": self.first})
        with patch("clinic.voice_turns.extract_card_edits", return_value={"patient_name": "Ravi Kumar"}):
            update = _try_card_edit(ctx, self.conn, "make it Ravi Kumar instead")
        self.assertIsInstance(update, CardUpdate)
        slots = ctx.open_card["slots"]
        self.assertEqual(slots["patient_name"], "Ravi Kumar")
        self.assertNotIn("patient_id", slots)
        self.assertNotIn("appointment_id", slots)         # that appointment was Amit's
        self.assertIsNone(ctx.patient)

    def test_the_same_name_said_again_is_not_a_change(self):
        slots = {"patient_id": 1, "patient_name": "Amit Dua"}
        self.assertIsNone(voice_context.drop_stale_identity(self.conn, slots, {"patient_name": "amit dua"}))
        self.assertEqual(slots["patient_id"], 1)

    def test_a_phone_that_is_not_the_patients_own_clears_the_id(self):
        slots = {"patient_id": 1, "patient_name": "Amit Dua"}
        self.assertEqual(voice_context.drop_stale_identity(self.conn, slots, {"patient_phone": "9811122233"}), 1)
        self.assertNotIn("patient_id", slots)

    def test_the_patients_own_phone_or_a_first_number_keeps_the_id(self):
        slots = {"patient_id": 1}
        self.assertIsNone(voice_context.drop_stale_identity(self.conn, slots, {"patient_phone": "98765 00301"}))
        self.conn.execute("UPDATE patients SET phone = '' WHERE id = 1")      # no number on file: this one is added to them
        self.assertIsNone(voice_context.drop_stale_identity(self.conn, slots, {"patient_phone": "9811122233"}))
        self.assertEqual(slots["patient_id"], 1)


class CardIsTheAnchorTests(IdentityTestCase):
    def test_the_idle_timeout_cannot_change_a_card_already_on_screen(self):
        clock = [1000.0]
        ctx = VoiceContext(clock=lambda: clock[0])
        ctx.ask("cancel_appointment", {"patient_name": "Amit Dua"}, "patient")
        card = self.say(ctx, "Ravi Kumar", name="Ravi Kumar")
        self.assertIsInstance(card, ParsedResult)
        before = copy.deepcopy((card.intent, card.slots, card.resolved))
        clock[0] += voice_context.IDLE_SECONDS + 1
        ctx.expire_if_idle()
        self.assertIsNone(ctx.open_card)                    # the server's memory of it is gone ...
        self.assertEqual((card.intent, card.slots, card.resolved), before)       # ... the card itself is as it was
        self.assertEqual(card.slots["appointment_id"], self.ravi_near)
        # and a later "make it 6 pm" can no longer edit it (it is not a card edit any more)
        self.assertIsNone(_try_card_edit(ctx, self.conn, "make it 6 pm instead"))


class StaffTests(IdentityTestCase):
    def setUp(self):
        super().setUp()
        for name in ("Ravi Singh", "Ravi Rao", "Sunita"):
            self.conn.execute("INSERT INTO staff (name, role) VALUES (?, 'nurse')", (name,))
        self.conn.commit()

    def test_two_staff_with_the_same_first_name_are_not_silently_resolved_on_a_card(self):
        card = self.respond("log_attendance", {"staff_name": "Ravi", "status": "present"})
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.resolved, {})                  # the dropdown is left blank for a human

    def test_two_staff_with_the_same_first_name_refuse_a_direct_write(self):
        with self.assertRaises(PipelineError) as raised:
            respond_to_intent(self.conn, "log_attendance", {"staff_name": "Ravi", "status": "present"}, "",
                              self.adapter, self.adapter, "en-IN", frozenset())
        self.assertIn("More than one staff member", str(raised.exception))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM attendance").fetchone()[0], 0)

    def test_the_full_name_or_a_unique_first_name_is_resolved(self):
        full = self.respond("log_attendance", {"staff_name": "Ravi Rao", "status": "present"})
        self.assertEqual(full.resolved["staff_label"], "Ravi Rao")
        only = self.respond("log_attendance", {"staff_name": "Sunita", "status": "present"})
        self.assertEqual(only.resolved["staff_label"], "Sunita")


class RememberedPatientTests(IdentityTestCase):
    def test_book_him_after_looking_up_one_of_two_same_named_patients_uses_that_id(self):
        ctx = VoiceContext()
        ask = self.say(ctx, "show me the details of patient Amit Dua", name="Amit Dua")
        self.assertEqual(ask.kind, "choose_patient")
        index = [o["patient_id"] for o in ask.options].index(2)
        handle_pick(ctx, self.conn, index, self.adapter, self.adapter, "en-IN", DEFER)
        self.assertEqual(ctx.patient["id"], 2)
        card = self.say(ctx, "book him tomorrow at 5 pm", name=None)
        self.assertIsInstance(card, ParsedResult)              # no "Which one?" again
        self.assertEqual(card.intent, "book_appointment")
        self.assertEqual(card.slots["patient_id"], 2)
        self.assertEqual(card.resolved["patient_id"], 2)

    def test_the_remembered_id_is_used_when_a_model_fills_the_name_back_in_for_him(self):
        ctx = VoiceContext()
        ctx.remember_patient(2, "Amit Dua (9876500402)")
        slots = voice_context.apply_context("book_appointment", {"patient_name": "Amit Dua"}, "book him tomorrow", ctx)
        self.assertEqual(slots["patient_id"], 2)

    def test_a_different_name_spoken_is_a_new_person_not_the_remembered_one(self):
        ctx = VoiceContext()
        ctx.remember_patient(2, "Amit Dua")
        slots = voice_context.apply_context("book_appointment", {"patient_name": "Ravi Kumar"}, "book him, Ravi Kumar", ctx)
        self.assertNotIn("patient_id", slots)
        self.assertEqual(slots["patient_name"], "Ravi Kumar")

    def test_without_him_nothing_is_carried_over(self):
        ctx = VoiceContext()
        ctx.remember_patient(2, "Amit Dua")
        slots = voice_context.apply_context("book_appointment", {}, "book an appointment tomorrow", ctx)
        self.assertNotIn("patient_id", slots)
        self.assertNotIn("patient_name", slots)


if __name__ == "__main__":
    unittest.main()
