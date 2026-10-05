"""What the patient does with a closure notice on WhatsApp: Accept (or just type
"ok"), or Choose another -- the normal reschedule chat, but with the branch
question, because the old branch may be the one that closed."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_conversation import ConvCase, NOW, TOMORROW, WA, WA2, FRI  # noqa: E402
from tests.test_conversation_branches import add_branches  # noqa: E402
from tests.test_auto_appointments import AutoCase  # noqa: E402

from clinic import branches, closures, conv_templates as ct, conversation as cv  # noqa: E402
from clinic.intents import HANDLERS  # noqa: E402

A, B, C = 1, 2, 3


class ClosureChat(ConvCase):
    def setUp(self):
        super().setUp()
        add_branches(self.conn)
        self.pid = self.patient()                                   # Sunita Devi, 9876543210 = WA
        self.aid = self.appt(TOMORROW, "10:00", patient_id=self.pid)
        self.conn.execute("UPDATE appointments SET branch_id = ?, doctor_id = 1 WHERE id = ?", (A, self.aid))
        self.conn.commit()
        plan = closures.plan(self.conn, A, TOMORROW, TOMORROW, now=NOW)
        scope = plan["scope"]
        move = plan["moves"][0]
        result = closures.apply(
            self.conn, branch_id=A, start_date=TOMORROW, end_date=TOMORROW, handlers=HANDLERS, after_commit=None, now=NOW,
            reason="Doctor on leave", moves=[{"appointment_id": self.aid, "action": "move", "to_branch_id": move["to"]["branch_id"],
                                              "to_date": move["to"]["date"], "to_time": move["to"]["time"]}])
        self.move_id = result["results"][0]["move_id"]

    def response(self):
        return self.conn.execute("SELECT response FROM closure_moves WHERE id = ?", (self.move_id,)).fetchone()[0]

    # -- Accept ---------------------------------------------------------------------
    def test_accept_confirms_the_new_slot_and_names_the_branch_and_doctor(self):
        r = self.send(choice="closure:accept:{}".format(self.move_id))
        text = r.replies[0].text
        self.assertTrue(text.startswith("Thank you. Your appointment is confirmed for"), text)
        self.assertIn("Branch: Branch B", text)
        self.assertIn("Doctor: Dr. Rao", text)
        self.assertEqual(self.response(), "accepted")
        self.assertEqual([a["event"] for a in r.activity], ["closure_accepted"])

    def test_typing_ok_to_the_notice_is_an_accept(self):
        r = self.send("okay")
        self.assertTrue(r.replies[0].text.startswith("Thank you. Your appointment is confirmed"))
        self.assertEqual(self.response(), "accepted")

    def test_a_plain_yes_with_nothing_pending_is_handled_as_before(self):
        closures.respond(self.conn, self.move_id, WA, "accepted", NOW)
        r = self.send("yes")
        self.assertNotIn("Thank you. Your appointment is confirmed", r.replies[0].text)

    def test_someone_elses_button_does_nothing(self):
        r = self.send(choice="closure:accept:{}".format(self.move_id), wa=WA2)
        self.assertEqual(r.replies[0].text, ct.text("closure_gone", "en"))
        self.assertEqual(self.response(), "none")

    def test_a_button_for_a_move_that_no_longer_exists(self):
        r = self.send(choice="closure:accept:9999")
        self.assertEqual(r.replies[0].text, ct.text("closure_gone", "en"))
        self.assertTrue(r.replies[0].buttons)                      # the menu, so they are not stuck

    # -- Choose another -----------------------------------------------------------------
    def test_choose_another_starts_a_reschedule_that_asks_for_the_branch(self):
        r = self.send(choice="closure:change:{}".format(self.move_id))
        self.assertEqual(self.response(), "changed")
        self.assertTrue(r.replies[0].text.startswith("No problem, let's find another time for your appointment on"))
        self.assertEqual(r.replies[1].text, ct.text("ask_branch", "en"))
        self.assertEqual([row[0] for row in r.replies[1].rows], ["branch:1", "branch:2", "branch:3"])
        s = self.session()
        self.assertEqual((s["goal"], s["step"], s["slots"]["appointment_id"]), ("reschedule", "branch", self.aid))

    def test_picking_a_branch_then_a_time_hands_off_a_move_to_that_branch(self):
        self.send(choice="closure:change:{}".format(self.move_id))
        self.send(choice="branch:3")                                # Branch C: 09:00-12:00
        r = self.send(choice="day:{}".format(FRI))
        r = self.send("10 am")
        self.assertIn("Branch: Branch C", r.replies[0].text)
        self.assertIn("Doctor: Dr. Iyer", r.replies[0].text)
        r = self.send(choice="confirm:yes")
        self.assertEqual(r.handoff.intent, "reschedule_appointment")
        self.assertEqual((r.handoff.slots["appointment_id"], r.handoff.slots["branch_id"],
                          r.handoff.slots["appt_date"], r.handoff.slots["start_time"]), (self.aid, C, FRI, "10:00"))

    def test_staying_at_the_new_branch_sends_no_branch_change(self):
        self.send(choice="closure:change:{}".format(self.move_id))
        self.send(choice="branch:2")                                # the branch they were moved to
        self.send(choice="day:{}".format(FRI))
        self.send("11 am")
        r = self.send(choice="confirm:yes")
        self.assertNotIn("branch_id", r.handoff.slots)

    def test_a_reschedule_not_started_from_a_notice_still_stays_put(self):
        # the ordinary reschedule is unchanged: no branch question
        r = self.send("reschedule my appointment")
        self.assertNotEqual(r.replies[0].text, ct.text("ask_branch", "en"))
        self.assertNotIn("pick_branch", self.session()["slots"])

    def test_the_notice_buttons_are_valid_choice_ids(self):
        for choice in ("closure:accept:12", "closure:change:3"):
            self.assertEqual(cv.parse_choice(choice), ("closure", choice.split(":", 1)[1]))
        for bad in ("closure:accept:", "closure:maybe:1", "closure:accept:x"):
            self.assertIsNone(cv.parse_choice(bad))


class ClosureChatAutomatic(AutoCase):
    """Through the webhook and the automatic path: the patient's new choice is
    committed at the branch they picked."""

    def setUp(self):
        super().setUp()
        add_branches(self.conn)
        pid = self.patient()
        self.aid = self.appt(pid, date_="2026-10-06", time="10:00")
        self.conn.execute("UPDATE appointments SET branch_id = ?, doctor_id = 1 WHERE id = ?", (A, self.aid))
        self.conn.commit()
        from datetime import datetime
        plan = closures.plan(self.conn, A, "2026-10-06", "2026-10-06", now=datetime(2026, 10, 5, 10, 0))
        move = plan["moves"][0]
        result = closures.apply(
            self.conn, branch_id=A, start_date="2026-10-06", end_date="2026-10-06", handlers=HANDLERS, after_commit=None,
            now=datetime(2026, 10, 5, 10, 0), reason="Renovation",
            moves=[{"appointment_id": self.aid, "action": "move", "to_branch_id": move["to"]["branch_id"],
                    "to_date": move["to"]["date"], "to_time": move["to"]["time"]}])
        self.move_id = result["results"][0]["move_id"]

    def test_choose_another_ends_with_the_appointment_at_the_new_branch(self):
        self.tap("closure:change:{}".format(self.move_id), "Choose another")
        self.tap("branch:3", "Branch C", kind="list_reply")
        self.say("Friday")
        self.say("10 am")
        self.tap("confirm:yes", "Confirm request")
        row = self.conn.execute("SELECT * FROM appointments WHERE id = ?", (self.aid,)).fetchone()
        self.assertEqual((row["branch_id"], row["appt_date"], row["start_time"]), (C, "2026-10-09", "10:00"))
        self.assertEqual(branches.doctor_label(self.conn, row["doctor_id"]), "Dr. Iyer")
        body = self.conn.execute("SELECT body FROM notifications WHERE event = 'appointment_rescheduled'").fetchone()["body"]
        self.assertIn("Branch C", body)
        self.assertEqual(self.conn.execute("SELECT response FROM closure_moves").fetchone()[0], "changed")

    def test_accept_through_the_webhook_replies_and_records(self):
        self.tap("closure:accept:{}".format(self.move_id), "Accept")
        self.assertTrue(any(t.startswith("Thank you. Your appointment is confirmed") for t in self.sender.texts), self.sender.texts)
        self.assertEqual(self.conn.execute("SELECT response FROM closure_moves").fetchone()[0], "accepted")
        events = [r[0] for r in self.conn.execute("SELECT event FROM patient_activity")]
        self.assertIn("closure_accepted", events)


if __name__ == "__main__":
    unittest.main()
