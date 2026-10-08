"""What the patient does with a follow-up reminder on WhatsApp: Reschedule (the
normal slot-picking chat), Already visited (done, the slot is freed, thanks), and
Cancel (asks "are you sure?" first). Plus STOP / START, a follow-up that is no
longer active, and typed words instead of a tap."""
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_conversation import NOW, WA, WA2, ConvCase  # noqa: E402
from tests.followup_fixtures import D2, D3, at, hooks  # noqa: E402

from clinic import auto_actions, conv_templates as ct, conversation as cv, followups, scheduling  # noqa: E402
from clinic.intents import HANDLERS  # noqa: E402


class FollowupChat(ConvCase):
    def setUp(self):
        super().setUp()
        self.pid = self.patient()                                        # Sunita Devi, 9876543210 = WA
        self.fid = self.make_followup()
        self.aid = self.followup()["appointment_id"]

    # -- fixtures ---------------------------------------------------------------------
    def make_followup(self, patient_id=None, due_date=D2, due_time="10:00"):
        result = followups.apply(
            self.conn, [{"patient_id": patient_id or self.pid, "due_date": due_date, "due_time": due_time, "doctor_id": 1,
                         "branch_id": 1, "diagnosis": "internal note"}],
            handlers=HANDLERS, after_commit=hooks(), now=NOW)
        self.assertEqual(result["counts"]["failed"], 0, result)
        return result["results"][0]["followup_id"]

    def followup(self, fid=None):
        return followups.get_followup(self.conn, fid or self.fid)

    def runner(self, conn, **kwargs):
        return followups.patient_action(conn, handlers=HANDLERS, after_commit=hooks(True), **kwargs)

    def auto(self, conn, **kwargs):
        return auto_actions.handle_request(conn, handlers=HANDLERS, after_commit=hooks(True), **kwargs)

    def go(self, text=None, choice=None, wa=WA, minutes=0, msg_id=None, automatic=True):
        self.clock += timedelta(minutes=minutes)
        result = cv.handle_inbound(self.conn, wa, text or "", choice_id=choice, now=self.clock, msg_id=msg_id,
                                   intent_picker=self.picker, auto=self.auto if automatic else None, followup=self.runner)
        self.check_limits(result)
        return result

    def tap(self, action, fid=None, **kw):
        return self.go(choice="followup:{}:{}".format(action, fid or self.fid), **kw)

    def appt_status(self):
        return self.conn.execute("SELECT status FROM appointments WHERE id = ?", (self.aid,)).fetchone()[0]

    def reminder_notes(self):
        return [r[0] for r in self.conn.execute("SELECT event FROM notifications WHERE event LIKE 'followup_reminder%'")]

    def unchanged(self):
        self.assertEqual((self.followup()["status"], self.appt_status()), ("pending", "booked"))

    def assertGone(self, result, lang="en"):
        self.assertEqual(result.replies[0].text, ct.text("followup_gone", lang))
        self.assertEqual([t for _, t in result.replies[0].buttons][:1], [ct.button("book", lang)])      # the menu: never a dead end
        self.assertEqual(result.activity, [])


class VisitedButton(FollowupChat):
    def test_already_visited_closes_the_followup_frees_the_slot_and_says_thanks(self):
        followups.process_due(self.conn, at(5, 10))                       # the early reminder is in the outbox
        self.assertEqual(self.reminder_notes(), ["followup_reminder_2d"])
        result = self.tap("visited", msg_id=7)
        self.assertEqual([r.text for r in result.replies], [ct.text("followup_visited", "en")])
        fu = self.followup()
        self.assertEqual((fu["status"], fu["completed_at"] is not None), ("done", True))
        self.assertEqual(self.appt_status(), "cancelled")                 # the slot is freed, and it is not a no-show
        self.assertTrue(scheduling.is_slot_free(self.conn, D2, "10:00", 30, branch_id=1))
        self.assertEqual(self.reminder_notes(), [])                       # queued reminders are gone
        self.assertEqual({r["state"] for r in self.conn.execute("SELECT state FROM followup_reminders")}, {"cancelled"})

    def test_it_is_a_staff_visible_event_and_audited(self):
        result = self.tap("visited", msg_id=7)
        self.assertEqual([(a["event"], a["appointment_id"]) for a in result.activity], [("followup_visited", self.aid)])
        self.assertEqual(result.activity[0]["meta"]["followup_id"], self.fid)
        intents = [(r["intent"], r["entity_type"]) for r in self.conn.execute("SELECT intent, entity_type FROM audit_log ORDER BY id")]
        self.assertEqual(intents[-2:], [("followup_visited", "followup"), ("cancel_appointment", "appointment")])
        proposal = self.conn.execute("SELECT source_text, status FROM proposals WHERE intent = 'followup_visited'").fetchone()
        self.assertTrue(proposal["source_text"].startswith("[auto:whatsapp-agent] wa_message #7"))   # so a failure after it is not re-handled
        self.assertEqual(proposal["status"], "confirmed")

    def test_no_appointment_cancelled_message_goes_out_the_thanks_is_the_only_reply(self):
        self.tap("visited")
        events = [r[0] for r in self.conn.execute("SELECT event FROM notifications")]
        self.assertNotIn("appointment_cancelled", events)

    def test_the_reply_is_in_the_patients_language(self):
        self.go("नमस्ते")                                                  # they write in Hindi first
        result = self.tap("visited")
        self.assertEqual(result.replies[0].text, ct.text("followup_visited", "hi"))
        result = self.go("namaste")
        hinglish = self.make_followup(due_date=D3)
        self.assertEqual(self.tap("visited", fid=hinglish).replies[0].text, ct.text("followup_visited", "hinglish"))


class CancelButton(FollowupChat):
    def test_cancel_asks_first_and_changes_nothing_yet(self):
        result = self.tap("cancel")
        self.assertEqual(len(result.replies), 1)
        reply = result.replies[0]
        self.assertEqual(reply.text, ct.text("followup_cancel_ask", "en", date=self.fd(D2, "en"), time=self.ft("10:00", "en")))
        self.assertEqual(reply.buttons, [("followup:cancel_confirm:{}".format(self.fid), "Yes, cancel"),
                                         ("followup:cancel_keep:{}".format(self.fid), "No, keep it")])
        self.unchanged()
        self.assertEqual(result.activity, [])

    def test_yes_cancels_the_appointment_and_the_followup_and_the_queued_reminders(self):
        followups.process_due(self.conn, at(5, 10))
        self.tap("cancel")
        result = self.tap("cancel_confirm", msg_id=9)
        self.assertEqual(result.replies[0].text, ct.text("followup_cancelled", "en", date=self.fd(D2, "en"), time=self.ft("10:00", "en")))
        self.assertIn("book again", result.replies[0].text)
        self.assertEqual((self.followup()["status"], self.appt_status()), ("cancelled", "cancelled"))
        self.assertEqual(self.reminder_notes(), [])
        self.assertEqual([a["event"] for a in result.activity], ["followup_cancelled"])
        self.assertTrue(scheduling.is_slot_free(self.conn, D2, "10:00", 30, branch_id=1))
        events = [r[0] for r in self.conn.execute("SELECT event FROM notifications")]
        self.assertNotIn("appointment_cancelled", events)                  # one reply, not two

    def test_no_keeps_everything(self):
        self.tap("cancel")
        result = self.tap("cancel_keep")
        self.assertEqual(result.replies[0].text, ct.text("followup_kept", "en", date=self.fd(D2, "en"), time=self.ft("10:00", "en")))
        self.unchanged()
        self.assertEqual(result.activity, [])

    def test_the_confirm_button_still_works_after_the_chat_session_timed_out(self):
        self.tap("cancel")
        result = self.tap("cancel_confirm", minutes=240)
        self.assertEqual(self.followup()["status"], "cancelled")
        self.assertEqual(result.replies[0].text, ct.text("followup_cancelled", "en", date=self.fd(D2, "en"), time=self.ft("10:00", "en")))

    def test_a_double_tap_on_yes_is_harmless(self):
        self.tap("cancel")
        self.tap("cancel_confirm")
        again = self.tap("cancel_confirm")
        self.assertGone(again)
        self.assertEqual(len(self.conn.execute("SELECT 1 FROM audit_log WHERE intent = 'followup_cancelled_by_patient'").fetchall()), 1)

    def test_a_typed_yes_after_the_question_is_the_normal_cancel(self):
        self.tap("cancel")
        result = self.go("yes", msg_id=11)
        self.assertEqual((self.followup()["status"], self.appt_status()), ("cancelled", "cancelled"))     # via the ordinary path + sync
        self.assertEqual(result.auto.kind, "committed")

    def test_a_typed_no_after_the_question_keeps_it(self):
        self.tap("cancel")
        result = self.go("no")
        self.assertEqual(result.replies[0].text, ct.text("nothing_cancelled", "en"))
        self.unchanged()

    def test_stop_at_the_question_still_means_no_not_opt_out(self):
        self.tap("cancel")
        result = self.go("stop")
        self.assertEqual(result.replies[0].text, ct.text("nothing_cancelled", "en"))
        self.assertFalse(followups.is_opted_out(self.conn, WA))


class RescheduleButton(FollowupChat):
    def test_reschedule_starts_the_normal_slot_picking_chat_for_that_visit(self):
        result = self.tap("reschedule")
        self.assertEqual(len(result.replies), 1)
        self.assertEqual(result.replies[0].text, ct.text("ask_day_resched", "en", date=self.fd(D2, "en"), time=self.ft("10:00", "en")))
        self.assertTrue(result.replies[0].buttons)                           # days to pick
        session = self.session()
        self.assertEqual((session["goal"], session["step"], session["slots"]["appointment_id"], session["slots"]["branch_id"]),
                         ("reschedule", "day", self.aid, 1))
        self.unchanged()

    def test_picking_a_new_slot_moves_the_appointment_and_the_followup_and_its_reminders(self):
        followups.process_due(self.conn, at(5, 10))
        self.tap("reschedule")
        result = self.go(choice="day:{}".format(D3))
        self.assertTrue(result.replies[0].buttons or result.replies[0].rows)            # free times to pick from
        result = self.go("5 pm")
        self.assertEqual(result.replies[0].buttons[0][0], "confirm:yes")
        self.go(choice="confirm:yes")
        appt = self.conn.execute("SELECT appt_date, start_time, status FROM appointments WHERE id = ?", (self.aid,)).fetchone()
        self.assertEqual(tuple(appt), (D3, "17:00", "booked"))
        fu = self.followup()
        self.assertEqual((fu["due_date"], fu["due_time"], fu["status"]), (D3, "17:00", "pending"))
        slots = {(r["slot_date"], r["slot_time"], r["kind"]): r["state"] for r in self.conn.execute("SELECT * FROM followup_reminders")}
        self.assertEqual(slots[(D2, "10:00", "2d")], "cancelled")
        self.assertEqual(slots[(D3, "17:00", "2d")], "scheduled")
        self.assertEqual(self.reminder_notes(), [])                             # the old slot's queued reminder is gone

    def test_the_patient_can_say_no_and_keep_the_visit(self):
        self.tap("reschedule")
        self.go(choice="day:{}".format(D3))
        self.go("5 pm")
        self.go(choice="confirm:no")
        self.unchanged()
        self.assertEqual(self.followup()["due_date"], D2)


class NoLongerActive(FollowupChat):
    ACTIONS = ("reschedule", "visited", "cancel", "cancel_confirm", "cancel_keep")

    def assertAllGone(self, fid=None, wa=WA):
        for action in self.ACTIONS:
            with self.subTest(action=action):
                before = self.counts()
                self.assertGone(self.tap(action, fid=fid, wa=wa))
                self.assertEqual(self.counts(), before)

    def test_a_done_followup(self):
        self.conn.execute("UPDATE appointments SET status = 'completed' WHERE id = ?", (self.aid,))
        self.conn.commit()
        followups.sync_appointment(self.conn, self.aid, at(5, 10))
        self.assertEqual(self.followup()["status"], "done")
        self.assertAllGone()

    def test_a_cancelled_followup(self):
        self.tap("cancel")
        self.tap("cancel_confirm")
        self.assertAllGone()

    def test_a_visit_whose_time_has_passed(self):
        self.clock = self.clock.replace(day=7, hour=10, minute=0)
        self.assertAllGone()

    def test_a_follow_up_the_patient_already_said_they_visited(self):
        self.tap("visited")
        self.assertAllGone()

    def test_an_unknown_follow_up(self):
        self.assertAllGone(fid=9999)

    def test_a_date_only_followup_has_no_buttons_to_answer(self):
        legacy = self.conn.execute("INSERT INTO followups (patient_id, due_date) VALUES (?, ?)", (self.pid, D3)).lastrowid
        self.conn.commit()
        self.assertAllGone(fid=legacy)

    def test_somebody_elses_followup_is_gone_to_them_and_untouched(self):
        for action in self.ACTIONS:
            with self.subTest(action=action):
                self.assertGone(self.tap(action, wa=WA2))
        self.unchanged()
        self.assertEqual(self.reminder_notes(), [])

    def test_a_patient_already_checked_in_is_not_offered_the_buttons(self):
        self.conn.execute("UPDATE appointments SET queue_state = 'checked_in' WHERE id = ?", (self.aid,))
        self.conn.commit()
        self.assertAllGone()

    def test_the_gone_reply_is_in_the_patients_language(self):
        self.go("नमस्ते")
        self.assertGone(self.tap("visited", fid=9999), "hi")


class TypedWordsInsteadOfTaps(FollowupChat):
    def test_free_text_never_crashes_or_changes_the_followup(self):
        for text in ("I already visited", "visited", "cancel kar do follow up", "ok", "👍", "blorp", "reschedule", "मैं आ चुका हूँ", "followup:cancel:1",
                     "followup:visited:" + str(self.fid)):
            with self.subTest(text=text):
                self.go(text, automatic=False)
        self.assertEqual(self.followup()["status"], "pending")
        self.assertEqual(self.appt_status(), "booked")

    def test_typing_reschedule_is_the_ordinary_reschedule_chat(self):
        result = self.go("reschedule my appointment")
        self.assertEqual(result.replies[0].text, ct.text("ask_day_resched", "en", date=self.fd(D2, "en"), time=self.ft("10:00", "en")))

    def test_hello_after_pressing_cancel_gives_the_menu_and_leaves_things_alone(self):
        self.tap("cancel")
        result = self.go("hello")
        self.assertEqual(result.replies[0].buttons[0][0], "menu:book")
        self.unchanged()

    def test_a_clinical_question_about_the_follow_up_gets_the_usual_refusal(self):
        result = self.go("what medicine should I take for the follow up")
        self.assertEqual(result.replies[0].text, ct.text("clinical", "en"))


class StopAndStart(FollowupChat):
    def test_stop_opts_out_replies_and_takes_queued_reminders_back(self):
        followups.process_due(self.conn, at(5, 10))
        result = self.go("STOP")
        self.assertEqual(result.replies[0].text, ct.text("optout_done", "en"))
        self.assertTrue(followups.is_opted_out(self.conn, "9876543210"))
        self.assertEqual(self.reminder_notes(), [])
        self.assertEqual(followups.process_due(self.conn, at(7, 9)), 0)

    def test_each_wording(self):
        for text in ("stop", "Stop.", "STOP reminders", "unsubscribe", "opt out", " stop  "):
            with self.subTest(text=text):
                followups.set_opted_out(self.conn, WA, False)
                self.assertEqual(self.go(text).replies[0].text, ct.text("optout_done", "en"))
                self.assertTrue(followups.is_opted_out(self.conn, WA))

    def test_a_sentence_that_merely_contains_stop_is_not_an_opt_out(self):
        for text in ("please do not stop the clinic", "bus stop near the clinic", "stopping by tomorrow"):
            self.go(text)
        self.assertFalse(followups.is_opted_out(self.conn, WA))

    def test_start_turns_reminders_back_on(self):
        self.go("stop")
        result = self.go("START")
        self.assertEqual(result.replies[0].text, ct.text("optin_done", "en"))
        self.assertFalse(followups.is_opted_out(self.conn, WA))
        states = {r["state"] for r in self.conn.execute("SELECT state FROM followup_reminders")}
        self.assertEqual(states, {"scheduled"})

    def test_start_from_someone_who_never_stopped_is_just_a_greeting(self):
        result = self.go("start")
        self.assertEqual(result.replies[0].buttons[0][0], "menu:book")

    def test_stop_in_hindi_and_hinglish_conversations_is_answered_in_that_language(self):
        self.go("नमस्ते")
        self.assertEqual(self.go("stop").replies[0].text, ct.text("optout_done", "hi"))

    def test_stop_while_staff_have_taken_over_is_still_recorded_but_not_answered(self):
        cv.set_mode(self.conn, WA, "human", self.clock)
        result = self.go("stop")
        self.assertTrue(followups.is_opted_out(self.conn, WA))
        self.assertEqual(result.replies, [])
        self.assertTrue(result.silent)

    def test_stop_from_a_number_that_is_nobodys_patient_is_harmless(self):
        result = self.go("stop", wa="919000000099")
        self.assertEqual(result.replies[0].text, ct.text("optout_done", "en"))


class Limits(FollowupChat):
    def test_every_new_reply_fits_whatsapps_limits_in_every_language(self):
        for lang in ("en", "hi", "hinglish"):
            for key in ("followup_cancel_ask", "followup_cancelled", "followup_kept", "followup_visited", "followup_gone",
                        "optout_done", "optin_done"):
                text = ct.text(key, lang, date=self.fd(D2, lang), time=self.ft("10:00", lang))
                self.assertLessEqual(len(text), 1024)
            for key in ("followup_cancel_yes", "followup_cancel_no"):
                self.assertLessEqual(len(ct.button(key, lang)), 20, (key, lang))
            self.assertTrue(ct.text("followup_cancel_ask", lang, date="D", time="T"))


if __name__ == "__main__":
    unittest.main()
