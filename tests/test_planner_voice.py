"""The planner on the voice page: the "thinking" state while it works, the
card's outcome landing in the planner log, branch-aware generic reads, and the
page wiring (static/live_voice.js)."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.planner_support import PlannerCase  # noqa: E402

from clinic.pipeline import ReadResult  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
A, B, C = 1, 2, 3


class VoiceEvents(PlannerCase):
    def setUp(self):
        super().setUp()
        os.environ.setdefault("SARVAM_API_KEY", "test-not-real")
        from clinic.realtime_voice import VoiceSession
        self.emitted = []
        self.session = VoiceSession("sid", "key", lambda event, data: self.emitted.append((event, data)),
                                    lambda: self.conn, self.adapter, self.adapter,
                                    frozenset({"book_appointment", "cancel_appointment", "reschedule_appointment"}))
        self.session.context = self.ctx

    def events(self):
        return [e for e, _ in self.emitted]

    def test_the_page_is_told_the_planner_is_thinking_before_the_answer_arrives(self):
        self.call("query", entity="patients", aggregate="list", fields=["name"])
        self.session._handle_final_transcript("who all do we have on file")
        names = self.events()
        self.assertIn("thinking", names)
        self.assertLess(names.index("thinking"), names.index("read_answer"))
        thinking = dict(self.emitted)["thinking"]
        self.assertEqual(thinking, {"transcript": "who all do we have on file", "stage": "planner"})

    def test_a_rule_answered_command_has_no_thinking_state(self):
        self.session._handle_final_transcript("how many patients are registered")
        self.assertNotIn("thinking", self.events())
        self.assertIn("read_answer", self.events())

    def test_thinking_is_shown_once_per_command_even_when_the_planner_fails(self):
        import httpx
        self.backend.script = httpx.ReadTimeout("slow")
        self.session._handle_final_transcript("the weather is nice today")
        self.assertEqual(self.events().count("thinking"), 1)
        self.assertIn("pipeline_error", self.events())

    def test_no_thinking_when_the_planner_is_off(self):
        with patch.dict(os.environ, {"INTENT_PLANNER_ENABLED": "0"}):
            self.pick.return_value = "list_appointments"
            self.session._handle_final_transcript("kuch dikhao tomorrow")
        self.assertNotIn("thinking", self.events())
        self.assertEqual(self.backend.calls, [])

    def test_the_cards_outcome_is_recorded_on_the_planner_log_row(self):
        self.call("book_appointment", patient_name="Rakesh Verma", date=self.tomorrow.isoformat(), time="17:00")
        self.session._handle_final_transcript("get Rakesh a seat tomorrow evening")
        card = dict(self.emitted)["review_card"]
        self.assertTrue(card["card_id"])
        self.session.card_closed(card["card_id"], "approved")
        self.assertEqual(self.log_rows()[0]["outcome"], "approved")

        self.call("cancel_appointment", patient_name="Mohan Lal")
        self.session._handle_final_transcript("Mohan Lal ko mat bulao kal")
        second = [d for e, d in self.emitted if e == "review_card"][1]
        self.session.card_closed(second["card_id"], "rejected")
        self.assertEqual([r["outcome"] for r in self.log_rows()], ["approved", "rejected"])

    def test_an_unknown_outcome_or_none_changes_nothing(self):
        self.call("book_appointment", patient_name="Rakesh Verma", date=self.tomorrow.isoformat(), time="17:00")
        self.session._handle_final_transcript("get Rakesh a seat tomorrow evening")
        card_id = dict(self.emitted)["review_card"]["card_id"]
        self.session.card_closed(card_id, "exploded")
        self.session.card_closed("no-such-card", "approved")
        self.assertIsNone(self.log_rows()[0]["outcome"])


class BranchAwareQueries(PlannerCase):
    def setUp(self):
        super().setUp()
        self.book_at(B, "10:00", name="Amit at B", phone="9111111111")
        self.book_at(C, "10:00", name="Amit at C", phone="9222222222")
        self.day = self.tomorrow_iso

    def listing(self, **extra):
        self.call("query", entity="appointments", aggregate="list", status="booked", date=self.day, **extra)
        return self.say("who all have a booking tomorrow, in a table")

    def patients(self, result):
        return sorted(r["patient"] for r in result.data)

    def test_it_defaults_to_this_computers_branch(self):
        result = self.listing()
        self.assertIsInstance(result, ReadResult)
        self.assertEqual(result.intent, "query")
        self.assertEqual({r["branch"] for r in result.data}, {"Branch A"})
        self.assertEqual(result.scope_caption, "Branch A")
        self.assertTrue(result.answer_text.startswith("3 appointments (booked) on"), result.answer_text)
        self.assertIn("at Branch A", result.answer_text)

    def test_a_named_branch_and_all_branches(self):
        self.assertEqual(self.patients(self.listing(branch="B")), ["Amit at B"])
        everything = self.listing(branch="all")
        self.assertEqual(len(everything.data), 5)
        self.assertIsNone(everything.scope_caption)

    def test_a_branch_named_in_the_words_is_kept_for_the_next_command(self):
        self.listing()
        self.say("and the same at Branch C")
        self.assertEqual(self.ctx.branch, C)

    def test_one_persons_appointments_are_found_wherever_they_are(self):
        self.call("query", entity="appointments", aggregate="list", patient_name="Amit", status="booked")
        result = self.say("every Amit we have a booking for")
        self.assertEqual(self.patients(result), ["Amit at B", "Amit at C"])

    def test_the_existing_read_is_used_when_it_already_answers(self):
        self.call("query", entity="appointments", aggregate="list", patient_name="Amit")
        result = self.say("Amit ke saare appointments")
        self.assertEqual(result.intent, "list_appointments")
        self.assertEqual(len(result.data), 2)

    def test_a_single_branch_clinic_hears_nothing_about_branches(self):
        self.conn.execute("UPDATE branches SET active = 0 WHERE id IN (2, 3)")
        self.conn.commit()
        result = self.listing(branch="B")
        self.assertIsNone(result.scope_caption)
        self.assertNotIn("Branch", result.answer_text)

    def test_follow_ups_use_the_previous_turn(self):
        self.call("query", entity="appointments", aggregate="count", date=self.day)
        counted = self.say("how many bookings are there tomorrow, all told")
        self.assertIn("3 appointment(s) on", counted.answer_text)
        self.call("query", entity="appointments", aggregate="list", date=self.day, fields=["patient", "time"])
        listed = self.say("and show them")
        self.assertIn("you called query(entity=appointments, aggregate=count, date={})".format(self.day), self.backend.calls[-1][1])
        self.assertEqual(list(listed.data[0]), ["patient", "time"])

    def test_the_generic_table_goes_through_the_page_table_renderer(self):
        source = (ROOT / "static" / "live_voice.js").read_text()
        self.assertIn("bubble-wide", source)
        self.assertIn("window.orderColumns(Object.keys(rows[0])", source)


class PageWiring(unittest.TestCase):
    def test_the_page_handles_the_thinking_event_and_reports_how_a_card_ended(self):
        source = (ROOT / "static" / "live_voice.js").read_text()
        self.assertIn('socket.on("thinking"', source)
        self.assertIn("assistant-thinking", source)
        self.assertIn('cardClosed(cardId, "approved")', source)
        self.assertIn('cardClosed(cardId, "rejected")', source)
        self.assertIn("outcome: outcome", source)
        self.assertIn(".assistant-thinking", (ROOT / "static" / "style.css").read_text())

    def test_the_safety_timer_outlasts_the_planner_and_its_fallback(self):
        source = (ROOT / "static" / "live_voice.js").read_text()
        self.assertIn("THINKING_SAFETY_MS = 45000", source)
        self.assertNotIn("}, 25000);", source)


if __name__ == "__main__":
    unittest.main()
