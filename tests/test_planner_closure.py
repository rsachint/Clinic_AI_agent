"""Closing a branch through the planner's close_branch / doctor_leave tools: a
length ("next one week") reaches the plan as an end date, a named destination
("move all appointments to Branch C") is tried first for every patient, and
nothing is written until a person presses Apply."""
import sys
import unittest
from datetime import timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.planner_support import PlannerCase  # noqa: E402
from tests.test_closures import ClosureCase, TUE  # noqa: E402

from clinic import closures  # noqa: E402
from clinic.pipeline import ClosurePlanResult, PipelineError  # noqa: E402
from clinic.voice_context import AskResult  # noqa: E402

A, B, C = 1, 2, 3


class PreferredDestination(ClosureCase):
    """closures.plan(preferred_branch_id=...): the named branch first, the usual nearest-branch order after."""

    def setUp(self):
        super().setUp()
        self.ten = self.appt("Pt Ten", B, TUE, "10:30")
        self.eleven = self.appt("Pt Eleven", B, TUE, "11:00")

    def destinations(self, plan):
        return [(m["name"], m["to"]["branch_id"] if m["to"] else None) for m in plan["moves"]]

    def test_the_named_branch_is_tried_first_for_every_patient(self):
        plan = self.plan(branch_id=B, preferred_branch_id=C)
        self.assertEqual(self.destinations(plan), [("Pt Ten", C), ("Pt Eleven", C)])
        self.assertEqual((plan["scope"]["preferred_branch_id"], plan["scope"]["preferred_branch"]), (C, "Branch C"))
        self.assertEqual(plan["counts"], {"total": 2, "movable": 2, "unresolved": 0, "to_preferred": 2})
        self.assertIsNone(plan["moves"][0]["note"])                    # same time, nothing to explain

    def test_without_a_destination_the_plan_is_what_it_always_was(self):
        plan = self.plan(branch_id=B)
        self.assertEqual(plan["counts"], {"total": 2, "movable": 2, "unresolved": 0})
        self.assertNotIn("preferred_branch", plan["scope"])
        self.assertEqual(plan["scope"].keys(), {"branch_id", "branch", "doctor_id", "doctor", "start_date", "end_date",
                                                "start_time", "end_time"})

    def test_it_is_the_default_choice_so_a_person_can_still_change_every_row(self):
        plan = self.plan(branch_id=B, preferred_branch_id=C)
        move = plan["moves"][0]
        self.assertEqual(move["action"], "move")
        self.assertTrue(len(move["options"]) > 1)                     # other branches remain on offer

    def test_a_destination_with_no_doctor_that_day_falls_back_and_says_so(self):
        self.conn.execute("DELETE FROM doctor_schedules WHERE branch_id = ? AND weekday = ?", (C, 1))   # Tuesday
        self.conn.commit()
        plan = self.plan(branch_id=B, preferred_branch_id=C)
        for move in plan["moves"]:
            self.assertEqual(move["action"], "move")
            self.assertNotEqual(move["to"]["branch_id"], C)
            self.assertIn("Branch C has no free time within 2 hours", move["note"])
            self.assertIn("Moved to {} instead.".format(move["to"]["branch"]), move["note"])
        self.assertEqual(plan["counts"]["to_preferred"], 0)

    def test_a_destination_whose_slot_is_taken_falls_back_for_that_patient_only(self):
        for hhmm in ("09:00", "09:30", "10:00", "10:30", "11:00", "11:30"):
            self.appt("Blocker " + hhmm, C, TUE, hhmm, phone="9" + hhmm.replace(":", "") + "0000")
        plan = self.plan(branch_id=B, preferred_branch_id=C)
        self.assertTrue(all(m["to"] is None or m["to"]["branch_id"] != C for m in plan["moves"]))
        self.assertTrue(all("Branch C has no free time" in m["note"] for m in plan["moves"]))

    def test_a_closed_destination_is_explained_once_and_the_usual_order_applies(self):
        self.conn.execute("UPDATE branches SET status = 'closed' WHERE id = ?", (C,))
        self.conn.commit()
        plan = self.plan(branch_id=B, preferred_branch_id=C)
        self.assertIn("Branch C is closed", plan["scope"]["preferred_note"])
        self.assertTrue(all(m["to"] and m["to"]["branch_id"] != C for m in plan["moves"]))

    def test_nonsense_destinations_are_refused(self):
        with self.assertRaises(closures.ClosureError):
            self.plan(branch_id=B, preferred_branch_id=B)              # to itself
        with self.assertRaises(closures.ClosureError):
            self.plan(branch_id=B, preferred_branch_id=99)
        with self.assertRaises(closures.ClosureError):
            self.plan(branch_id=B, preferred_branch_id="somewhere")

    def test_planning_writes_nothing(self):
        tables = ("appointments", "booking_blocks", "closures", "closure_moves", "proposals", "notifications")
        before = [self.conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0] for t in tables]
        self.plan(branch_id=B, preferred_branch_id=C)
        after = [self.conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0] for t in tables]
        self.assertEqual(before, after)


class ByVoice(PlannerCase):
    SENTENCE = "Branch B will be closed for next one week, move all appointments to Branch C"

    def setUp(self):
        super().setUp()
        self.clear_appointments()
        self.book_at(B, "10:30", name="Pt Ten", phone="9000011111")
        self.book_at(B, "11:00", name="Pt Eleven", phone="9000022222")
        self.book_at(A, "10:00", name="Pt Elsewhere", phone="9000033333")
        self.write_tables = ("booking_blocks", "closures", "closure_moves", "proposals", "notifications")

    def planner_says(self, **args):
        self.call("close_branch", **args)

    def test_the_planner_reads_the_length_and_the_destination_into_the_plan(self):
        # the model is a day off on the start and has no end: the safety net counts seven days from today
        self.planner_says(branch="B", start_date=(self.today + timedelta(days=1)).isoformat(), preferred_destination="C")
        before = self.table_counts(*self.write_tables)
        result = self.say(self.SENTENCE)
        self.assertIsInstance(result, ClosurePlanResult)
        scope = result.plan["scope"]
        self.assertEqual((scope["branch_id"], scope["branch"], scope["preferred_branch"]), (B, "Branch B", "Branch C"))
        self.assertEqual((scope["start_date"], scope["end_date"]), (self.today.isoformat(), (self.today + timedelta(days=6)).isoformat()))
        self.assertEqual(result.plan["counts"], {"total": 2, "movable": 2, "unresolved": 0, "to_preferred": 2})
        self.assertEqual({m["to"]["branch"] for m in result.plan["moves"]}, {"Branch C"})
        self.assertIn("Branch C was tried first", result.answer_text)
        self.assertIn("nothing changes until you press Apply", result.answer_text)
        self.assertEqual(self.table_counts(*self.write_tables), before)               # a plan, nothing else
        row = self.log_rows()[0]
        self.assertEqual((row["route_taken"], row["planner_tool"], row["final_intent"]), ("planner", "close_branch", "close_branch"))
        self.assertIn("counted from today", row["override_notes"])

    def test_the_rules_alone_would_have_closed_the_wrong_branch_the_planner_does_not(self):
        # the rule path names the LAST branch in a sentence (the destination of a move): C
        from clinic import voice_branch, voice_closure
        mention = voice_branch.find(self.conn, self.SENTENCE)
        self.assertEqual(mention.branch["id"], C)
        self.assertEqual(voice_closure.parse(self.conn, self.SENTENCE, mention.text, mention, today=self.today)["branch_id"], C)
        self.planner_says(branch="B", start_date=self.today.isoformat(), preferred_destination="C")
        self.assertEqual(self.say(self.SENTENCE).plan["scope"]["branch_id"], B)

    def test_when_the_planner_cannot_help_the_reading_rules_still_get_it_right(self):
        self.backend.script = httpx.ConnectError("refused")
        result = self.say(self.SENTENCE)
        self.assertIsInstance(result, ClosurePlanResult)
        scope = result.plan["scope"]
        self.assertEqual((scope["branch_id"], scope["preferred_branch_id"]), (B, C))
        self.assertEqual((scope["start_date"], scope["end_date"]), (self.today.isoformat(), (self.today + timedelta(days=6)).isoformat()))
        self.assertEqual(self.log_rows()[0]["route_taken"], "rules")

    def test_a_planner_that_thinks_it_is_something_else_leaves_the_rules_reading_alone(self):
        self.call("unsupported")
        result = self.say("close Branch B for 3 days and send them to Branch C")
        self.assertIsInstance(result, ClosurePlanResult)
        self.assertEqual(result.plan["scope"]["branch_id"], B)
        self.assertEqual(self.log_rows()[0]["route_taken"], "rules")

    def test_a_plain_closing_never_asks_the_planner(self):
        result = self.say("close Branch A tomorrow because the doctor is ill")
        self.assertIsInstance(result, ClosurePlanResult)
        self.assertEqual(self.backend.calls, [])

    def test_a_missing_day_is_still_asked_when_nobody_can_read_it(self):
        self.backend.script = None
        ask = self.say("close Branch A please")
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.kind, "date")

    def test_doctor_leave_goes_through_the_doctor_day_path(self):
        tomorrow = self.tomorrow.isoformat()
        rao = self.conn.execute("SELECT id FROM doctors WHERE name = 'Dr. Rao'").fetchone()[0]
        self.conn.execute("UPDATE appointments SET doctor_id = ? WHERE branch_id = ?", (rao, B))     # whose patients they are
        self.conn.commit()
        self.call("doctor_leave", doctor_name="Rao", start_date=tomorrow, end_date=(self.tomorrow + timedelta(days=2)).isoformat())
        result = self.say("Rao won't be around from tomorrow for three days")
        self.assertIsInstance(result, ClosurePlanResult)
        scope = result.plan["scope"]
        self.assertEqual((scope["doctor"], scope["branch"]), ("Dr. Rao", "Branch B"))     # Dr. Rao works at Branch B
        self.assertEqual({m["name"] for m in result.plan["moves"]}, {"Pt Ten", "Pt Eleven"})
        self.assertEqual(self.table_counts(*self.write_tables), [0] * len(self.write_tables))

    def test_a_doctor_nobody_has_heard_of_is_not_a_closure(self):
        self.call("doctor_leave", doctor_name="Dr. Nobody", start_date=self.tomorrow.isoformat())
        with self.assertRaises(PipelineError):
            self.say("Nobody won't be around tomorrow")


if __name__ == "__main__":
    unittest.main()
