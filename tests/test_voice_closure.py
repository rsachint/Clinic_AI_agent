"""Closing a branch or taking a doctor off for some days by voice. The result is
only the batch review card (a plan); nothing is closed, moved or sent until a
person presses Apply."""
import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_voice_branches import BranchVoiceCase  # noqa: E402

from clinic import voice_branch, voice_closure  # noqa: E402
from clinic.pipeline import ClosurePlanResult, PipelineError  # noqa: E402
from clinic.voice_context import AskResult, Note  # noqa: E402

A, B, C = 1, 2, 3


class Recognising(BranchVoiceCase):
    def closes(self, text):
        return voice_closure.is_close_command(self.conn, text, voice_branch.find(self.conn, text))

    def test_close_requests_in_three_languages(self):
        for text in (
            "close Branch A tomorrow because the doctor is ill", "Branch B will be closed on Friday",
            "shut down Branch C for 3 days", "close the clinic tomorrow", "ब्रांच बी कल बंद रहेगी", "कल ब्रांच ए बंद करो",
            "Branch A kal band karo", "Dr. Rao is on leave tomorrow", "Rao is sick from Monday to Wednesday",
            "Dr. Rao ki kal chutti hai",
        ):
            with self.subTest(text=text):
                self.assertTrue(self.closes(text))

    def test_questions_and_other_commands_are_not_closures(self):
        for text in (
            "what time does Branch B close", "show me the closing time for branch A", "how many branches are closed",
            "book Amit at Branch B tomorrow", "cancel Amit's appointment", "switch to branch C", "close the calendar",
            "move Amit to Branch B", "kal ke appointments dikhao", "Seema is absent today",
        ):
            with self.subTest(text=text):
                self.assertFalse(self.closes(text))

    def test_a_one_branch_clinic_has_no_closures(self):
        self.conn.execute("UPDATE branches SET active = 0 WHERE id IN (2, 3)")
        self.conn.commit()
        self.assertFalse(self.closes("close the clinic tomorrow"))


class Planning(BranchVoiceCase):
    def setUp(self):
        super().setUp()
        self.clear_appointments()
        self.book_at(A, "10:00", name="Pt One", phone="9000011111")
        self.book_at(A, "11:00", name="Pt Two", phone="9000022222")
        self.book_at(B, "10:00", name="Pt Elsewhere", phone="9000033333")

    def counts(self):
        return [self.conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0]
                for t in ("booking_blocks", "closures", "closure_moves", "proposals", "notifications")]

    def test_close_a_branch_tomorrow_gives_the_batch_card_with_the_reason(self):
        result = self.say("close Branch A tomorrow because the doctor is ill")
        self.assertIsInstance(result, ClosurePlanResult)
        scope = result.plan["scope"]
        self.assertEqual((scope["branch_id"], scope["start_date"], scope["end_date"]), (A, self.tomorrow_iso, self.tomorrow_iso))
        self.assertEqual(result.reason, "the doctor is ill")
        self.assertEqual([m["name"] for m in result.plan["moves"]], ["Pt One", "Pt Two"])      # not Branch B's patient
        self.assertEqual([m["to"]["branch"] for m in result.plan["moves"]], ["Branch B", "Branch B"])
        self.assertIn("2 patients booked at Branch A", result.answer_text)
        self.assertIn("nothing changes until you press Apply", result.answer_text)

    def test_nothing_is_written_by_asking(self):
        before = self.counts()
        self.say("close Branch A tomorrow")
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.counts()[:3], [0, 0, 0])

    def test_the_plan_can_go_to_the_page_as_json(self):
        result = self.say("close Branch A tomorrow")
        json.dumps(result.plan)

    def test_a_missing_day_is_asked_and_the_answer_completes_it(self):
        ask = self.say("close Branch A because of renovation")
        self.assertIsInstance(ask, AskResult)
        self.assertEqual((ask.kind, ask.slots["branch_id"]), ("date", A))
        result = self.say("tomorrow")
        self.assertIsInstance(result, ClosurePlanResult)
        self.assertEqual(result.plan["scope"]["start_date"], self.tomorrow_iso)
        self.assertEqual(result.reason, "renovation")

    def test_the_branch_defaults_to_my_branch_and_is_shown_on_the_card(self):
        self.ctx.set_client_branch(B, B)
        result = self.say("close the clinic tomorrow")
        self.assertEqual(result.plan["scope"]["branch_id"], B)
        self.assertEqual([m["name"] for m in result.plan["moves"]], ["Pt Elsewhere"])

    def test_without_any_branch_it_asks_which_and_an_answer_will_do(self):
        self.ctx.set_client_branch(None, None)
        ask = self.say("close the clinic tomorrow")
        self.assertIsInstance(ask, AskResult)
        self.assertEqual((ask.kind, [o["label"] for o in ask.options]), ("branch", ["Branch A", "Branch B", "Branch C"]))
        result = self.say("bee")
        self.assertIsInstance(result, ClosurePlanResult)
        self.assertEqual(result.plan["scope"]["branch_id"], B)

    def test_a_range_of_days(self):
        monday = self.today + timedelta(days=(7 - self.today.weekday()) % 7 or 7)
        result = self.say("close Branch B from {} to {}".format(monday.strftime("%d %B"), (monday + timedelta(days=2)).strftime("%d %B")))
        scope = result.plan["scope"]
        self.assertEqual((scope["start_date"], scope["end_date"]), (monday.isoformat(), (monday + timedelta(days=2)).isoformat()))

    def test_for_three_days(self):
        result = self.say("close Branch C for 3 days from tomorrow")
        scope = result.plan["scope"]
        self.assertEqual(scope["start_date"], self.tomorrow_iso)
        self.assertEqual(scope["end_date"], (self.tomorrow + timedelta(days=2)).isoformat())

    def test_a_doctors_leave_closes_that_doctor_at_the_branch_they_are_at(self):
        self.conn.execute("UPDATE appointments SET doctor_id = 2 WHERE patient_name = 'Pt Elsewhere'")      # Dr. Rao's patient
        self.conn.commit()
        result = self.say("Dr. Rao is on leave tomorrow")
        scope = result.plan["scope"]
        self.assertEqual((scope["doctor"], scope["branch_id"]), ("Dr. Rao", B))            # Rao is scheduled at Branch B
        self.assertEqual(result.reason, "Dr. Rao is on leave")
        self.assertEqual([m["name"] for m in result.plan["moves"]], ["Pt Elsewhere"])

    def test_an_unreadable_day_is_refused_not_guessed(self):
        with self.assertRaises(PipelineError):
            self.say("close Branch A tareekh ko")

    def test_the_closing_branch_is_not_remembered_for_the_next_command(self):
        self.say("close Branch C tomorrow")
        self.assertIsNone(self.ctx.branch)

    def test_a_one_branch_clinic_gets_a_gentle_no(self):
        self.conn.execute("UPDATE branches SET active = 0 WHERE id IN (2, 3)")
        self.conn.commit()
        with self.assertRaises(PipelineError):          # not recognised as a closure: no other branch to move anyone to
            self.say("close the clinic tomorrow")


class ThroughTheSession(BranchVoiceCase):
    def test_the_session_sends_the_plan_to_the_page_and_writes_nothing(self):
        import os
        os.environ.setdefault("SARVAM_API_KEY", "test-not-real")
        from clinic.realtime_voice import VoiceSession
        emitted = []
        session = VoiceSession("sid", "key", lambda e, d: emitted.append((e, d)), lambda: self.conn, self.adapter, self.adapter, DEFER_ALL)
        self.book_at(A, "10:00", name="Pt One", phone="9000011111")
        session._handle_final_transcript("close Branch A tomorrow because the doctor is ill")
        event = [d for e, d in emitted if e == "closure_plan"][-1]
        json.dumps(event)
        self.assertEqual(event["reason"], "the doctor is ill")
        self.assertEqual(event["plan"]["scope"]["branch"], "Branch A")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM booking_blocks").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], 0)

    def test_the_page_builds_the_card_and_has_no_voice_approval(self):
        js = (Path(__file__).resolve().parents[1] / "static" / "live_voice.js").read_text()
        self.assertIn('socket.on("closure_plan"', js)
        self.assertIn("ClosureCard.build(data.plan", js)
        self.assertNotIn('emit("approve', js)


DEFER_ALL = frozenset({"book_appointment", "cancel_appointment", "reschedule_appointment"})

if __name__ == "__main__":
    unittest.main()
