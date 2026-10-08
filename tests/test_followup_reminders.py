"""The two WhatsApp reminders of a follow-up: when they are queued (including the
catch-up after downtime and the follow-up made late), who they go to, what they
say (never anything clinical), and how they travel: free-form with three buttons
inside WhatsApp's 24-hour window, an approved Meta template outside it, otherwise
blocked and listed for staff to send by hand."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.followup_fixtures import (D1, D2, D3, STAFF_PHONE, STAFF_WA, WA, FollowupCase, PlainSender, Sender, at,  # noqa: E402
                                     hooks)

from clinic import (branches, conversation, followup_notify, followups, notify, scheduler, settings,  # noqa: E402
                    whatsapp)

ROOT = Path(__file__).resolve().parents[1]
SLOT_16 = dict(due_time="16:00")          # Wed 7 Oct 16:00: early reminder Mon 5 Oct 10:00, second one Wed 12:00


class WhenTheyAreQueued(FollowupCase):
    def test_nothing_is_due_before_the_early_reminder_time(self):
        self.make(**SLOT_16)
        self.assertEqual(followups.process_due(self.conn, at(5, 9, 59)), 0)
        self.assertEqual(followups.process_due(self.conn, at(5, 10, 0)), 1)
        self.assertEqual([n["event"] for n in self.notes()], ["followup_reminder_2d"])

    def test_the_second_reminder_is_queued_at_its_own_time(self):
        fid = self.make(**SLOT_16)
        followups.process_due(self.conn, at(5, 10))
        self.assertEqual(followups.process_due(self.conn, at(7, 11, 59)), 0)
        self.assertEqual(followups.process_due(self.conn, at(7, 12, 0)), 1)
        self.assertEqual([n["event"] for n in self.notes()], ["followup_reminder_2d", "followup_reminder_4h"])
        self.assertEqual(self.states(fid), {"2d": "enqueued", "4h": "enqueued"})

    def test_a_morning_visit_is_reminded_at_seven_not_earlier(self):
        self.make(due_time="09:00")
        followups.process_due(self.conn, at(5, 10))
        self.assertEqual(followups.process_due(self.conn, at(7, 6, 59)), 0)
        self.assertEqual(followups.process_due(self.conn, at(7, 7, 0)), 1)

    def test_the_dedup_key_is_per_followup_slot_and_kind(self):
        fid = self.make(**SLOT_16)
        followups.process_due(self.conn, at(5, 10))
        followups.process_due(self.conn, at(7, 12))
        keys = sorted(n["dedup_key"] for n in self.notes())
        self.assertEqual(keys, ["followup_reminder_2d:{}:{}@16:00".format(fid, D2), "followup_reminder_4h:{}:{}@16:00".format(fid, D2)])

    def test_repeated_ticks_never_send_twice(self):
        self.make(**SLOT_16)
        sender = Sender()
        for minute in range(0, 40):
            scheduler.tick(self.conn, sender, now=at(5, 10 + minute // 60, minute % 60))
        self.assertEqual(len(sender.calls), 1)
        self.inbound(WA, at(7, 11))                           # (they wrote again, so the window is open for the second one)
        for minute in range(0, 40):
            scheduler.tick(self.conn, sender, now=at(7, 12, minute))
        self.assertEqual(len(sender.followup_calls()), 2)
        self.assertEqual(len(self.notes("followup_reminder_2d")) + len(self.notes("followup_reminder_4h")), 2)

    def test_the_reminder_row_is_only_enqueued_once_even_if_the_state_write_was_lost(self):
        fid = self.make(**SLOT_16)
        followups.process_due(self.conn, at(5, 10))
        self.conn.execute("UPDATE followup_reminders SET state = 'scheduled', notification_id = NULL WHERE kind = '2d'")   # a crash
        self.conn.commit()
        followups.process_due(self.conn, at(5, 11))
        self.assertEqual(len(self.notes("followup_reminder_2d")), 1)
        row = [r for r in self.reminder_rows(fid) if r["kind"] == "2d"][0]
        self.assertEqual((row["state"], row["notification_id"]), ("enqueued", self.notes()[0]["id"]))

    def test_the_scheduler_tick_queues_and_delivers_them(self):
        self.make(**SLOT_16)
        sender = Sender()
        summary = scheduler.tick(self.conn, sender, now=at(5, 10, 1))
        self.assertEqual((summary["followup_reminders"], summary["errors"]), (1, 0))
        self.assertEqual(summary["flushed"], {"sent": 1})
        self.assertEqual(len(sender.followup_calls()), 1)

    def test_a_failing_sender_is_retried_through_the_outbox_not_requeued(self):
        self.make(**SLOT_16)
        flaky = Sender(fail_with=RuntimeError("down"))
        scheduler.tick(self.conn, flaky, now=at(5, 10, 1))
        good = Sender()
        scheduler.tick(self.conn, good, now=at(5, 10, 2))
        self.assertEqual(len(good.calls), 1)
        self.assertEqual(self.notes()[0]["status"], "sent")
        self.assertEqual(len(self.notes()), 1)


class CatchUp(FollowupCase):
    def fu_events(self):
        """The follow-up reminders only (the ordinary appointment reminders run in the same tick)."""
        return [n["event"] for n in self.notes() if n["event"].startswith("followup_")]

    def test_after_an_outage_that_covered_both_only_the_latest_is_sent_and_the_early_one_superseded(self):
        fid = self.make(due_time="10:00")                  # early: Mon 10:00; second: Wed 07:00
        self.inbound(WA, at(7, 7, 30))                     # the patient wrote this morning, so the window is open
        sender = Sender()
        summary = scheduler.tick(self.conn, sender, now=at(7, 8, 0))           # the first tick after the app was down for days
        self.assertEqual(summary["followup_reminders"], 1)
        self.assertEqual(self.fu_events(), ["followup_reminder_4h"])
        self.assertEqual(len(sender.followup_calls()), 1)
        rows = {r["kind"]: r for r in self.reminder_rows(fid)}
        self.assertEqual((rows["2d"]["state"], rows["2d"]["reason"]), ("skipped", "superseded by a later reminder"))
        self.assertEqual(rows["4h"]["state"], "enqueued")
        scheduler.tick(self.conn, sender, now=at(7, 8, 1))
        self.assertEqual(len(sender.followup_calls()), 1)

    def test_an_outage_that_covered_only_the_early_one_still_sends_it_late(self):
        fid = self.make(due_time="16:00")
        sender = Sender()
        scheduler.tick(self.conn, sender, now=at(6, 14))                         # a day late, visit still a day ahead
        self.assertEqual(self.fu_events(), ["followup_reminder_2d"])
        scheduler.tick(self.conn, sender, now=at(7, 12))
        self.assertEqual(self.fu_events(), ["followup_reminder_2d", "followup_reminder_4h"])
        self.assertEqual(self.states(fid), {"2d": "enqueued", "4h": "enqueued"})

    def test_a_followup_made_inside_the_two_day_window_gets_the_early_reminder_at_once(self):
        fid = self.make(now=at(6, 12), due_time="17:00")                         # made Tue 12:00 for Wed 17:00 (> 4 hours away)
        self.assertEqual(followups.process_due(self.conn, at(6, 12)), 1)
        self.assertEqual([n["event"] for n in self.notes()], ["followup_reminder_2d"])
        self.assertEqual(followups.process_due(self.conn, at(7, 12, 59)), 0)
        self.assertEqual(followups.process_due(self.conn, at(7, 13, 0)), 1)         # and the second one at its own time
        self.assertEqual(self.states(fid), {"2d": "enqueued", "4h": "enqueued"})

    def test_a_followup_made_within_four_hours_of_the_visit_gets_only_the_late_reminder(self):
        fid = self.make(now=at(7, 8), due_time="10:00")                         # made Wed 08:00 for Wed 10:00
        self.assertEqual(followups.process_due(self.conn, at(7, 8)), 1)
        self.assertEqual([n["event"] for n in self.notes()], ["followup_reminder_4h"])
        rows = {r["kind"]: r for r in self.reminder_rows(fid)}
        self.assertEqual(rows["2d"]["reason"], "superseded by a later reminder")

    def test_a_followup_made_on_the_day_but_hours_ahead_gets_both_in_order_of_the_rule(self):
        # made Wed 05:00 for Wed 16:00: early reminder is long past, second is due 12:00 -> early one now, second later
        self.make(now=at(7, 5), due_time="16:00")
        self.assertEqual(followups.process_due(self.conn, at(7, 5)), 1)
        self.assertEqual([n["event"] for n in self.notes()], ["followup_reminder_2d"])
        self.assertEqual(followups.process_due(self.conn, at(7, 12)), 1)

    def test_nothing_is_ever_sent_once_the_slot_has_started(self):
        fid = self.make(due_time="10:00")
        self.assertEqual(followups.process_due(self.conn, at(7, 10, 0)), 0)             # the slot start itself
        self.assertEqual(followups.process_due(self.conn, at(7, 15)), 0)
        self.assertEqual(self.notes(), [])
        reasons = {r["reason"] for r in self.reminder_rows(fid)}
        self.assertEqual(reasons, {"the visit time had already started"})

    def test_one_minute_before_the_slot_the_late_reminder_still_goes(self):
        self.make(due_time="10:00")
        self.assertEqual(followups.process_due(self.conn, at(7, 9, 59)), 1)
        self.assertEqual([n["event"] for n in self.notes()], ["followup_reminder_4h"])

    def test_a_visit_the_patient_is_already_at_gets_no_reminder(self):
        fid = self.make(due_time="10:00")
        self.change_appointment("queue_check_in", {"appointment_id": self.followup(fid)["appointment_id"]})
        self.assertEqual(followups.process_due(self.conn, at(7, 8)), 0)
        self.assertEqual(self.notes(), [])

    def test_a_late_start_does_not_resend_what_already_went_out(self):
        self.make(due_time="16:00")
        followups.process_due(self.conn, at(5, 10))
        followups.process_due(self.conn, at(7, 15))          # the second is sent late; the early one is NOT sent again
        self.assertEqual([n["event"] for n in self.notes()], ["followup_reminder_2d", "followup_reminder_4h"])


class WhoGetsThem(FollowupCase):
    def test_a_patient_who_wrote_on_whatsapp_is_reached_on_that_number(self):
        self.make(**SLOT_16)
        followups.process_due(self.conn, at(5, 10))
        self.assertEqual(self.notes()[0]["wa_id"], WA)

    def test_a_patient_staff_booked_by_phone_number_only_is_reached_too(self):
        self.make(patient_id=self.staff_patient, **SLOT_16)           # "98765 43211": no WhatsApp message from them yet
        followups.process_due(self.conn, at(5, 10))
        self.assertEqual(self.notes()[0]["wa_id"], STAFF_WA)
        sender = Sender()
        notify.flush(self.conn, sender, now=at(5, 10))
        self.assertEqual(sender.calls, [])                              # no 24-hour window: it waits, it is not lost
        self.assertEqual(self.notes()[0]["status"], "blocked_no_window")

    def test_the_other_ways_a_number_is_written_all_resolve(self):
        for phone, expected in (("+91 98765 43299", "919876543299"), ("09876543298", "919876543298"), ("9876543297", "919876543297")):
            pid = self.add_patient("P " + phone, phone)
            self.make(patient_id=pid, due_time="16:00" if phone.endswith("99") else "16:30" if phone.endswith("98") else "17:00")
            followups.process_due(self.conn, at(5, 10))
        self.assertEqual(sorted(n["wa_id"] for n in self.notes()), ["919876543297", "919876543298", "919876543299"])

    def test_a_window_opened_by_the_staff_booked_patient_lets_it_through(self):
        self.make(patient_id=self.staff_patient, **SLOT_16)
        self.inbound(STAFF_WA, at(5, 9, 30))                            # they wrote once the booking was made
        sender = Sender()
        scheduler.tick(self.conn, sender, now=at(5, 10, 1))
        self.assertEqual([c["wa_id"] for c in sender.calls], [STAFF_WA])

    def test_a_patient_who_asked_to_stop_gets_nothing(self):
        fid = self.make(**SLOT_16)
        followups.set_opted_out(self.conn, "919876543210", True)
        self.assertEqual(followups.process_due(self.conn, at(5, 10)), 0)
        self.assertEqual(self.notes(), [])
        self.assertEqual({r["reason"] for r in self.reminder_rows(fid)}, {"the patient asked to stop reminders"})

    def test_stopping_takes_back_a_reminder_already_queued_and_starting_again_restores_the_rest(self):
        fid = self.make(**SLOT_16)
        followups.process_due(self.conn, at(5, 10))
        self.assertEqual(len(self.notes()), 1)
        followups.set_opted_out(self.conn, WA, True)
        self.assertEqual(self.notes(), [])
        self.assertTrue(followups.is_opted_out(self.conn, "9876543210"))
        followups.set_opted_out(self.conn, WA, False)
        self.assertFalse(followups.is_opted_out(self.conn, WA))
        self.assertEqual(self.states(fid), {"2d": "scheduled", "4h": "scheduled"})

    def test_opting_out_one_number_leaves_everyone_else_alone(self):
        a = self.make(**SLOT_16)
        b = self.make(patient_id=self.staff_patient, due_time="16:30")
        followups.set_opted_out(self.conn, WA, True)
        followups.process_due(self.conn, at(5, 10))
        self.assertEqual([n["wa_id"] for n in self.notes()], [STAFF_WA])
        self.assertEqual(self.states(a)["2d"], "cancelled")
        self.assertEqual(self.states(b)["2d"], "enqueued")

    def test_opting_out_needs_a_real_number(self):
        self.assertFalse(followups.set_opted_out(self.conn, "123", True))
        self.assertFalse(followups.is_opted_out(self.conn, "123"))


class HowTheyTravel(FollowupCase):
    def queue(self, patient_id=None, due_time="16:00", when=None, **kw):
        fid = self.make(patient_id=patient_id, due_time=due_time, **kw) if patient_id else self.make(due_time=due_time, **kw)
        followups.process_due(self.conn, when or at(5, 10))
        return fid

    def approve(self, *names):
        settings.set_approved_templates(self.conn, list(names), followup_notify.all_template_names())

    def test_inside_the_window_it_is_a_normal_message_with_three_reply_buttons(self):
        fid = self.queue()
        sender = Sender()
        notify.flush(self.conn, sender, now=at(5, 10))
        call = sender.calls[0]
        self.assertIsNone(call["template"])
        self.assertEqual(call["wa_id"], WA)
        self.assertEqual([b["id"] for b in call["interactive"]["buttons"]],
                         ["followup:reschedule:{}".format(fid), "followup:visited:{}".format(fid), "followup:cancel:{}".format(fid)])
        self.assertEqual([b["title"] for b in call["interactive"]["buttons"]], ["Reschedule", "Already visited", "Cancel"])
        for needle in ("Sunita Devi", "Dr. Mehta", "Branch A", "Wednesday, 7 Oct 2026", "4:00 PM", "STOP"):
            self.assertIn(needle, call["text"])
        self.assertEqual(self.notes()[0]["status"], "sent")

    def test_outside_the_window_with_an_approved_template_it_is_sent_as_the_template(self):
        fid = self.queue(patient_id=self.staff_patient)
        self.approve("followup_reminder_2d_en")
        sender = Sender()
        notify.flush(self.conn, sender, now=at(5, 10))
        call = sender.calls[0]
        self.assertEqual(call["wa_id"], STAFF_WA)
        self.assertEqual(call["template"], {
            "name": "followup_reminder_2d_en", "language": "en",
            "params": ["Rakesh Verma", "Dr. Mehta", "Branch A", "Wednesday, 7 Oct 2026", "4:00 PM"],
            "buttons": ["followup:reschedule:{}".format(fid), "followup:visited:{}".format(fid), "followup:cancel:{}".format(fid)]})
        self.assertEqual(self.notes()[0]["status"], "sent")

    def test_the_template_goes_out_as_a_real_cloud_api_template_body(self):
        fid = self.queue(patient_id=self.staff_patient)
        self.approve("followup_reminder_2d_en")
        posted = []
        with mock.patch.object(whatsapp, "_post_message", side_effect=lambda body: posted.append(body) or {}):
            notify.flush(self.conn, notify.live_sender, now=at(5, 10))
        body = posted[0]
        self.assertEqual((body["type"], body["to"], body["template"]["name"], body["template"]["language"]),
                         ("template", STAFF_WA, "followup_reminder_2d_en", {"code": "en"}))
        components = body["template"]["components"]
        self.assertEqual(components[0], {"type": "body", "parameters": [
            {"type": "text", "text": t} for t in ("Rakesh Verma", "Dr. Mehta", "Branch A", "Wednesday, 7 Oct 2026", "4:00 PM")]})
        self.assertEqual([(c["type"], c["sub_type"], c["index"], c["parameters"][0]["payload"]) for c in components[1:]],
                         [("button", "quick_reply", "0", "followup:reschedule:{}".format(fid)),
                          ("button", "quick_reply", "1", "followup:visited:{}".format(fid)),
                          ("button", "quick_reply", "2", "followup:cancel:{}".format(fid))])

    def test_the_template_follows_the_patients_language(self):
        hindi = self.add_patient("सुनीता देवी", "9811100001")
        hinglish = self.add_patient("Mohan Lal", "9811100002")
        self.inbound("919811100001", at(1, 9), "नमस्ते")          # wrote in Hindi, days ago: outside the window
        self.inbound("919811100002", at(1, 9), "haan ji theek hai")
        self.queue(patient_id=hindi, due_time="16:00")
        self.queue(patient_id=hinglish, due_time="16:30")
        self.approve(*followup_notify.all_template_names())
        sender = Sender()
        notify.flush(self.conn, sender, now=at(5, 10))
        by_wa = {c["wa_id"]: c["template"] for c in sender.calls}
        self.assertEqual((by_wa["919811100001"]["name"], by_wa["919811100001"]["language"]), ("followup_reminder_2d_hi", "hi"))
        self.assertEqual((by_wa["919811100002"]["name"], by_wa["919811100002"]["language"]), ("followup_reminder_2d_hinglish", "en"))
        self.assertIn("सुनीता देवी", by_wa["919811100001"]["params"][0])
        self.assertIn("अक्टूबर", by_wa["919811100001"]["params"][3])

    def test_only_the_approved_template_is_ever_used(self):
        self.queue(patient_id=self.staff_patient)
        followups.process_due(self.conn, at(7, 12))
        self.approve("followup_reminder_4h_en")                     # only the second reminder's template is approved
        sender = Sender()
        notify.flush(self.conn, sender, now=at(7, 12))
        self.assertEqual([c["template"]["name"] for c in sender.calls], ["followup_reminder_4h_en"])
        self.assertEqual({n["event"]: n["status"] for n in self.notes()},
                         {"followup_reminder_2d": "blocked_no_window", "followup_reminder_4h": "sent"})

    def test_outside_the_window_with_no_approved_template_it_is_blocked_and_listed_for_staff(self):
        self.queue(patient_id=self.staff_patient)
        sender = Sender()
        notify.flush(self.conn, sender, now=at(5, 10))
        self.assertEqual(sender.calls, [])
        note = self.notes()[0]
        self.assertEqual(note["status"], "blocked_no_window")
        self.assertIn("template followup_reminder_2d_en is not marked approved", note["error"])
        manual = followups.manual_list(self.conn, "all")
        self.assertEqual(len(manual), 1)
        self.assertEqual((manual[0]["patient_name"], manual[0]["phone"], manual[0]["status"], manual[0]["text"]),
                         ("Rakesh Verma", STAFF_PHONE, "blocked", note["body"]))

    def test_the_default_is_that_nothing_is_approved(self):
        self.assertEqual(settings.approved_templates(self.conn), set())
        self.assertFalse(settings.template_approved(self.conn, "followup_reminder_2d_en"))

    def test_a_sender_that_cannot_send_templates_leaves_the_message_blocked(self):
        self.queue(patient_id=self.staff_patient)
        self.approve("followup_reminder_2d_en")
        plain = PlainSender()
        notify.flush(self.conn, plain, now=at(5, 10))
        self.assertEqual(plain.calls, [])
        self.assertEqual(self.notes()[0]["status"], "blocked_no_window")
        self.assertIn("cannot send templates", self.notes()[0]["error"])

    def test_dry_run_records_a_template_send_without_sending(self):
        self.queue(patient_id=self.staff_patient)
        self.approve("followup_reminder_2d_en")
        notify.flush(self.conn, None, now=at(5, 10), dry_run=True)
        self.assertEqual(self.notes()[0]["status"], "dry_run")

    def test_approving_the_template_later_and_retrying_sends_it(self):
        self.queue(patient_id=self.staff_patient)
        notify.flush(self.conn, Sender(), now=at(5, 10))
        reminder = self.reminder_rows()[0]
        self.approve("followup_reminder_2d_en")
        self.assertTrue(followups.retry_reminder(self.conn, reminder["id"]))
        sender = Sender()
        notify.flush(self.conn, sender, now=at(5, 11))
        self.assertEqual(sender.calls[0]["template"]["name"], "followup_reminder_2d_en")
        self.assertEqual(self.notes()[0]["status"], "sent")
        self.assertFalse(followups.retry_reminder(self.conn, reminder["id"]))          # sent: nothing to retry

    def test_a_tap_on_the_first_reminder_opens_the_window_so_the_second_goes_free_form(self):
        self.queue(patient_id=self.staff_patient)
        self.inbound(STAFF_WA, at(7, 6), "Reschedule")             # they replied on the morning of the visit
        followups.process_due(self.conn, at(7, 12))
        sender = Sender()
        notify.flush(self.conn, sender, now=at(7, 12))
        last = [c for c in sender.calls if "today" in c["text"]][0]
        self.assertIsNone(last["template"])
        self.assertEqual(len(last["interactive"]["buttons"]), 3)

    def test_blocked_and_failed_reminders_are_listed_and_can_be_marked_sent_by_hand(self):
        fid = self.queue(patient_id=self.staff_patient)
        notify.flush(self.conn, Sender(), now=at(5, 10))
        reminder = self.reminder_rows()[0]
        self.assertEqual([m["reminder_id"] for m in followups.manual_list(self.conn, "all")], [reminder["id"]])
        self.assertTrue(followups.mark_sent_manually(self.conn, reminder["id"], at(5, 10, 30)))
        note = self.notes()[0]
        self.assertEqual((note["status"], note["error"], note["sent_at"]), ("sent", "Sent by hand by staff", "2026-10-05 05:00:00"))
        self.assertEqual(self.reminder_rows()[0]["state"], "manual")
        self.assertEqual(followups.manual_list(self.conn, "all"), [])
        self.assertEqual(self.audit("followup_reminder_sent_by_hand")[0]["entity_id"], fid)
        sender = Sender()
        notify.flush(self.conn, sender, now=at(5, 11))                 # never retried, never double-sent
        self.assertEqual(sender.calls, [])
        self.assertFalse(followups.mark_sent_manually(self.conn, reminder["id"]))      # and not twice
        status = followups.list_followups(self.conn, "all", at(5, 11))[0]["reminders"][0]
        self.assertEqual((status["status"], status["detail"]), ("sent", "sent by hand"))

    def test_a_failed_reminder_is_listed_too_and_a_sent_one_is_not(self):
        self.queue()
        notify.flush(self.conn, Sender(fail_with=RuntimeError("WhatsApp is down")), now=at(5, 10))
        self.assertEqual([(m["status"], m["error"]) for m in followups.manual_list(self.conn, "all")], [("failed", "WhatsApp is down")])
        notify.flush(self.conn, Sender(), now=at(5, 10, 5))
        self.assertEqual(followups.manual_list(self.conn, "all"), [])

    def test_the_manual_list_follows_the_viewed_branch_and_drops_finished_followups(self):
        branches.ensure_seed(self.conn)
        rao = [d["id"] for d in branches.list_doctors(self.conn) if d["name"] == "Dr. Rao"][0]
        self.queue(patient_id=self.staff_patient, due_time="16:00")
        notify.flush(self.conn, Sender(), now=at(5, 10, 1))
        self.queue(patient_id=self.add_patient("Other", "9811100077"), due_date="2026-10-12", branch_id=2, doctor_id=rao,
                   when=at(10, 10, 5))
        notify.flush(self.conn, Sender(), now=at(10, 10, 6))
        self.assertEqual(len(followups.manual_list(self.conn, "all")), 2)
        self.assertEqual([m["patient_name"] for m in followups.manual_list(self.conn, 2)], ["Other"])
        self.assertEqual([m["patient_name"] for m in followups.manual_list(self.conn, 1)], ["Rakesh Verma"])
        fid = [f["id"] for f in followups.list_followups(self.conn, 1, at(5, 11))][0]
        self.change_appointment("cancel_appointment", {"appointment_id": self.followup(fid)["appointment_id"]})
        self.assertEqual(followups.manual_list(self.conn, 1), [])

    def test_the_status_of_each_reminder_for_the_list(self):
        self.queue(patient_id=self.staff_patient, due_time="16:00")
        followups.process_due(self.conn, at(7, 12))
        notify.flush(self.conn, Sender(), now=at(7, 12))                 # no window, nothing approved
        item = followups.list_followups(self.conn, "all", at(7, 12))[0]
        statuses = [(r["kind"], r["status"], r["retryable"]) for r in item["reminders"]]
        self.assertEqual(statuses, [("2d", "blocked", True), ("4h", "blocked", True)])
        self.assertIn("24-hour window", item["reminders"][0]["detail"])


class WhatTheyNeverSay(FollowupCase):
    SECRET = "Type 2 diabetes mellitus with neuropathy"

    def everything_stored_for_patients(self):
        out = []
        for table in ("notifications", "wa_messages", "patient_activity", "wa_sessions", "slot_holds"):
            out.append(json.dumps([dict(r) for r in self.conn.execute("SELECT * FROM {}".format(table))], ensure_ascii=False))
        out.append(json.dumps([dict(r) for r in self.conn.execute("SELECT * FROM appointments")], ensure_ascii=False))
        out.append(json.dumps([dict(r) for r in self.conn.execute("SELECT * FROM followup_reminders")], ensure_ascii=False))
        return "\n".join(out)

    def test_the_diagnosis_never_reaches_a_message_a_button_or_a_template(self):
        fid = self.make(diagnosis=self.SECRET, **SLOT_16)
        self.make(patient_id=self.staff_patient, diagnosis="Hypertension", due_time="16:30")
        sender = Sender()
        for day, hour in ((5, 10), (7, 12)):
            scheduler.tick(self.conn, sender, now=at(day, hour, 1))
        settings.set_approved_templates(self.conn, followup_notify.all_template_names(), followup_notify.all_template_names())
        notify.retry(self.conn, self.notes()[-1]["id"])
        scheduler.tick(self.conn, sender, now=at(7, 12, 5))
        self.assertGreaterEqual(len(self.notes()), 3)
        self.assertEqual(self.followup(fid)["diagnosis"], self.SECRET)            # it IS stored, for staff
        sent = json.dumps(sender.calls, ensure_ascii=False)
        stored = self.everything_stored_for_patients()
        for blob in (sent, stored):
            for secret in (self.SECRET, "diabetes", "neuropathy", "Hypertension", "hypertension"):
                self.assertNotIn(secret, blob)

    def test_the_message_builder_does_not_even_read_the_diagnosis(self):
        fid = self.make(diagnosis=self.SECRET)
        ctx = followup_notify.context(self.conn, fid)
        self.assertNotIn("diagnosis", ctx)
        self.assertNotIn(self.SECRET, json.dumps(ctx))
        for lang in ("en", "hi", "hinglish", "bilingual"):
            for kind in followup_notify.KINDS:
                text, interactive, template = followup_notify.compose(self.conn, ctx, kind, lang)
                self.assertNotIn(self.SECRET, text + json.dumps(interactive) + json.dumps(template))

    def test_no_template_or_button_text_mentions_anything_clinical(self):
        clinical = ("diagnos", "treatment", "medicine", "prescription", "symptom", "disease", "tablet", "dose", "test result",
                    "report", "blood", "sugar", "निदान", "इलाज", "दवा", "बीमारी", "रिपोर्ट", "nidan", "ilaaj", "dawai", "dawa", "bimari")
        pieces = []
        for kind in followup_notify.KINDS:
            for lang in followup_notify.LANGUAGES:
                pieces.append(followup_notify.BODY[kind][lang])
                pieces.append(followup_notify.FOOTER[lang])
        for action in followup_notify.ACTIONS:
            pieces.extend(followup_notify.BUTTONS[action].values())
        for definition in followup_notify.meta_templates():
            pieces.append(json.dumps(definition, ensure_ascii=False))
        for piece in pieces:
            for word in clinical:
                self.assertNotIn(word, piece.lower(), piece)

    def test_no_patient_facing_template_anywhere_has_a_place_for_a_diagnosis(self):
        from clinic import closure_notify, conv_templates
        everything = []
        for table in (notify.TEMPLATES, conv_templates.MSG, closure_notify.TEMPLATES):
            for key, by_language in table.items():
                everything.extend((key, text) for text in by_language.values())
        everything.extend((t["name"], t["body"]) for t in notify.META_TEMPLATES)
        self.assertGreater(len(everything), 100)
        for key, text in everything:
            self.assertNotIn("diagnos", text.lower(), key)
            self.assertNotIn("{diagnosis}", text, key)

    def test_the_reminder_says_exactly_the_planned_things_and_nothing_else(self):
        fid = self.make(diagnosis=self.SECRET, **SLOT_16)
        ctx = followup_notify.context(self.conn, fid)
        text, _, _ = followup_notify.compose(self.conn, ctx, "2d", "en")
        self.assertEqual(text, (
            "Hello Sunita Devi, this is a reminder from the clinic about your follow-up visit with Dr. Mehta at Branch A "
            "on Wednesday, 7 Oct 2026 at 4:00 PM. If your plans have changed, please tap a button below.\n\n"
            "Reply STOP to stop these reminders"))


class WithOtherBranches(FollowupCase):
    def test_with_several_branches_the_message_says_where(self):
        branches.ensure_seed(self.conn)
        branches.update_branch(self.conn, 2, address="Sector 56, Gurugram", maps_url="https://maps.example/b")
        rao = [d["id"] for d in branches.list_doctors(self.conn) if d["name"] == "Dr. Rao"][0]
        fid = self.make(due_date="2026-10-12", branch_id=2, doctor_id=rao)
        ctx = followup_notify.context(self.conn, fid)
        text, _, template = followup_notify.compose(self.conn, ctx, "2d", "en")
        self.assertIn("Dr. Rao at Branch B", text)
        self.assertIn("\U0001F4CD Branch B, Sector 56, Gurugram\nhttps://maps.example/b", text)
        self.assertEqual(template["params"][1:3], ["Dr. Rao", "Branch B"])

    def test_with_one_branch_there_is_no_address_line(self):
        fid = self.make()
        text, _, _ = followup_notify.compose(self.conn, followup_notify.context(self.conn, fid), "2d", "en")
        self.assertNotIn("\U0001F4CD", text)


class MessageShape(FollowupCase):
    def test_every_message_fits_whatsapps_limits_in_every_language(self):
        branches.ensure_seed(self.conn)
        branches.update_branch(self.conn, 2, address="Plot 14, Sector 56, Golf Course Road, Gurugram, Haryana", maps_url="https://maps.example/" + "x" * 80)
        rao = [d["id"] for d in branches.list_doctors(self.conn) if d["name"] == "Dr. Rao"][0]
        fid = self.make(due_date="2026-10-12", branch_id=2, doctor_id=rao)
        ctx = followup_notify.context(self.conn, fid)
        for kind in followup_notify.KINDS:
            for lang in ("en", "hi", "hinglish", "bilingual"):
                text, interactive, template = followup_notify.compose(self.conn, ctx, kind, lang)
                self.assertLessEqual(len(text), whatsapp.BODY_MAX, (kind, lang))
                body = whatsapp.build_interactive_body("919876543210", text, interactive)      # raises on a bad title / id
                self.assertEqual(len(body["interactive"]["action"]["buttons"]), 3)
                for button in interactive["buttons"]:
                    self.assertLessEqual(len(button["title"]), whatsapp.BUTTON_TITLE_MAX)
                    kind_, value = conversation.parse_choice(button["id"])
                    self.assertEqual(kind_, "followup")
                whatsapp.build_template_body("919876543210", template)                          # raises on a bad parameter

    def test_the_language_picks_the_script(self):
        fid = self.make()
        ctx = followup_notify.context(self.conn, fid)
        hi = followup_notify.compose(self.conn, ctx, "2d", "hi")
        hinglish = followup_notify.compose(self.conn, ctx, "2d", "hinglish")
        both = followup_notify.compose(self.conn, ctx, "2d", "bilingual")
        self.assertIn("नमस्ते Sunita Devi", hi[0])
        self.assertEqual([b["title"] for b in hi[1]["buttons"]], ["समय बदलें", "विज़िट हो चुकी", "रद्द करें"])
        self.assertTrue(hinglish[0].startswith("Namaste Sunita Devi, clinic ki taraf se"))
        self.assertIn("Hello Sunita Devi", both[0])
        self.assertIn("Namaste Sunita Devi", both[0])
        self.assertEqual(both[2]["name"], "followup_reminder_2d_en")


class ExistingRemindersAreUntouched(FollowupCase):
    def test_a_followup_appointment_still_gets_the_day_before_and_morning_reminders(self):
        fid = self.make(due_date=D1, due_time="10:00")                  # Tue 10:00
        aid = self.followup(fid)["appointment_id"]
        created = notify.generate_reminders(self.conn, at(5, 18, 5))
        self.assertEqual(created, 1)
        created += notify.generate_reminders(self.conn, at(6, 8, 5))
        self.assertEqual(created, 2)
        events = [(n["event"], n["appointment_id"]) for n in self.notes()]
        self.assertEqual(events, [("reminder_day_before", aid), ("reminder_morning", aid)])
        for note in self.notes():
            self.assertNotIn("follow", note["body"].lower())             # the ordinary appointment text, unchanged

    def test_both_kinds_can_be_sent_to_the_same_patient_so_up_to_four_messages_arrive(self):
        # An observation for the owner, pinned here so it cannot change silently: 2-day, day-before, morning, 4-hour.
        self.inbound(WA, at(5, 9))
        self.make(due_date=D1, due_time="10:00")
        sender = Sender()
        for when in (at(4, 10, 1), at(5, 10, 1), at(5, 18, 5), at(6, 8, 5), at(6, 8, 10), at(6, 7, 1)):
            scheduler.tick(self.conn, sender, now=when)
        events = [n["event"] for n in self.notes()]
        self.assertEqual(sorted(events), sorted(["followup_reminder_2d", "followup_reminder_4h", "reminder_day_before", "reminder_morning"]))

    def test_an_ordinary_appointment_gets_no_followup_reminders(self):
        self.conn.execute("INSERT INTO appointments (patient_id, appt_date, start_time, branch_id, doctor_id) VALUES (?, ?, '10:00', 1, 1)",
                          (self.wa_patient, D2))
        self.conn.commit()
        sender = Sender()
        scheduler.tick(self.conn, sender, now=at(5, 10, 1))
        scheduler.tick(self.conn, sender, now=at(7, 8, 1))
        self.assertEqual([n for n in self.notes() if n["event"].startswith("followup_")], [])


class MetaTemplateFiles(unittest.TestCase):
    def definitions(self):
        return json.loads((ROOT / "docs" / "meta_templates" / "followup_templates.json").read_text(encoding="utf-8"))["templates"]

    def test_the_json_file_is_exactly_what_the_code_generates(self):
        # Regenerate with:  python -m clinic.followup_notify > docs/meta_templates/followup_templates.json
        self.assertEqual(self.definitions(), followup_notify.meta_templates())

    def test_six_templates_with_the_expected_names_and_language_codes(self):
        defs = self.definitions()
        self.assertEqual([d["name"] for d in defs], [
            "followup_reminder_2d_en", "followup_reminder_2d_hi", "followup_reminder_2d_hinglish",
            "followup_reminder_4h_en", "followup_reminder_4h_hi", "followup_reminder_4h_hinglish"])
        self.assertEqual([d["language"] for d in defs], ["en", "hi", "en", "en", "hi", "en"])
        self.assertEqual({d["category"] for d in defs}, {"UTILITY"})

    def test_each_one_follows_metas_rules(self):
        for d in self.definitions():
            self.assertEqual(followup_notify.meta_rule_problems(d), [], d["name"])
            body = d["components"]["body"]
            self.assertEqual([f"{{{{{i}}}}}" in body["text"] for i in range(1, 6)], [True] * 5)
            self.assertNotIn("{{6}}", body["text"])
            self.assertEqual(len(body["example"]), 5)
            self.assertEqual([b["text"] for b in d["components"]["buttons"]],
                             [followup_notify.BUTTONS[a][{"en": "en", "hi": "hi"}.get(d["name"].rsplit("_", 1)[1], "hinglish")]
                              for a in followup_notify.ACTIONS])
            for button in d["components"]["buttons"]:
                self.assertLessEqual(len(button["text"]), 25)
                self.assertEqual(button["type"], "QUICK_REPLY")

    def test_the_hindi_ones_are_in_devanagari_and_the_hinglish_ones_are_not(self):
        for d in self.definitions():
            text = d["components"]["body"]["text"]
            has_devanagari = any("ऀ" <= ch <= "ॿ" for ch in text)
            self.assertEqual(has_devanagari, d["name"].endswith("_hi"), d["name"])

    def test_the_template_text_is_the_message_text(self):
        # What a patient reads is the same whether it arrives free-form or as the template.
        values = {"{{1}}": "A", "{{2}}": "B", "{{3}}": "C", "{{4}}": "D", "{{5}}": "E"}
        for kind in followup_notify.KINDS:
            for lang in followup_notify.LANGUAGES:
                positional = followup_notify.positional_body(kind, lang)
                for key, value in values.items():
                    positional = positional.replace(key, value)
                filled = followup_notify.BODY[kind][lang].format(name="A", doctor="B", branch="C", date="D", time="E")
                self.assertEqual(positional, filled)

    def test_meta_rule_checker_catches_the_mistakes_it_is_for(self):
        good = followup_notify.meta_templates()[0]
        for text, problem in (("{{1}} hello there, this is a long enough sentence with {{2}} and {{3}} and {{4}} and {{5}} ok", "starts"),
                              ("Hello there, this is a long enough sentence with {{1}} {{2}} and {{3}} and {{4}} and {{5}}", "next to each other"),
                              ("Hello there, this is a long enough sentence with {{1}} and {{3}} and {{4}} and {{5}} ok", "without gaps"),
                              ("Hello there, this is a long enough sentence with {{1}} and {{2}} and {{3}} and {{4}} and {{5}}", "ends")):
            bad = json.loads(json.dumps(good))
            bad["components"]["body"]["text"] = text
            self.assertTrue(any(problem in p for p in followup_notify.meta_rule_problems(bad)), (text, followup_notify.meta_rule_problems(bad)))

    def test_the_readme_covers_what_the_owner_has_to_do(self):
        readme = (ROOT / "docs" / "meta_templates" / "README.md").read_text(encoding="utf-8")
        for needle in ("UTILITY", "followup_reminder_2d_en", "followup_reminder_4h_hinglish", "Quick reply", "Reschedule",
                       "Already visited", "Cancel", "sample", "pricing", "Hinglish", "English", "Approved", "Settings"):
            self.assertIn(needle, readme)


class WhatsappPieces(unittest.TestCase):
    def test_template_body_shape(self):
        body = whatsapp.build_template_body("919876543210", {
            "name": "followup_reminder_2d_en", "language": "en", "params": ["A", "B"], "buttons": ["followup:cancel:7"]})
        self.assertEqual(body, {
            "messaging_product": "whatsapp", "recipient_type": "individual", "to": "919876543210", "type": "template",
            "template": {"name": "followup_reminder_2d_en", "language": {"code": "en"}, "components": [
                {"type": "body", "parameters": [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]},
                {"type": "button", "sub_type": "quick_reply", "index": "0",
                 "parameters": [{"type": "payload", "payload": "followup:cancel:7"}]}]}})

    def test_parameters_are_made_safe_for_meta(self):
        body = whatsapp.build_template_body("1", {"name": "n", "language": "en", "params": ["Line one\nline   two\t!"]})
        self.assertEqual(body["template"]["components"][0]["parameters"][0]["text"], "Line one line two !")

    def test_bad_templates_are_refused_before_anything_is_sent(self):
        for spec in ({"language": "en"}, {"name": "n"}, {"name": "n", "language": "en", "params": [""]},
                     {"name": "n", "language": "en", "params": ["x" * 2000]},
                     {"name": "n", "language": "en", "buttons": ["followup:cancel:" + "9" * 200]}):
            with self.assertRaises(ValueError):
                whatsapp.build_template_body("1", spec)
        with mock.patch("httpx.post", side_effect=AssertionError("network")):
            with self.assertRaises(ValueError):
                whatsapp.send_template("1", {"language": "en"})

    def test_a_tap_on_a_template_button_arrives_as_a_choice(self):
        message = {"from": "919876543210", "id": "wamid.1", "timestamp": "1790000000", "type": "button",
                   "button": {"payload": "followup:visited:12", "text": "Already visited"}}
        parsed = whatsapp.parse_webhook_payload({"entry": [{"changes": [{"value": {"messages": [message]}}]}]})
        self.assertEqual((parsed["message_type"], parsed["text"], parsed["choice_id"]), ("text", "Already visited", "followup:visited:12"))
        message["button"] = {"text": "x"}
        self.assertIsNone(whatsapp.parse_webhook_payload({"entry": [{"changes": [{"value": {"messages": [message]}}]}]}))

    def test_the_new_choice_ids_parse(self):
        for action in ("reschedule", "visited", "cancel", "cancel_confirm", "cancel_keep"):
            self.assertEqual(conversation.parse_choice("followup:{}:12".format(action)), ("followup", "{}:12".format(action)))
        for bad in ("followup:visited", "followup:visited:x", "followup:delete:3", "followup:visited:1234567890"):
            self.assertIsNone(conversation.parse_choice(bad))


if __name__ == "__main__":
    unittest.main()
