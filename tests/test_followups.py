"""Creating follow-ups (plan / apply / undo) and keeping them in step with their
appointment: the slot is really booked and linked, every way a row can be wrong is
named (with the next open day proposed), one bad row never stops the others, and
moving / cancelling / completing the appointment by any existing path updates the
follow-up and its reminders."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.followup_fixtures import (D1, D2, D3, D4, SUNDAY, TODAY, FollowupCase, Sender, at, hooks)  # noqa: E402

from clinic import auto_actions, booking_blocks, booking_phone, branches, core, followups, notify, scheduling  # noqa: E402
from clinic.intents import HANDLERS  # noqa: E402

NOW = at(5, 9)           # Monday 09:00


class PlanTests(FollowupCase):
    def plan(self, *rows, now=NOW):
        return followups.plan(self.conn, list(rows), now)

    def only(self, *rows, **kw):
        return self.plan(*rows, **kw)["rows"][0]

    def test_a_good_row_is_ready_and_nothing_is_written(self):
        planned = self.plan(self.row())
        row = planned["rows"][0]
        self.assertTrue(row["ok"])
        self.assertEqual((row["patient"], row["doctor"], row["branch"]), ("Sunita Devi", "Dr. Mehta", "Branch A"))
        self.assertEqual(planned["counts"], {"total": 1, "valid": 1, "invalid": 0})
        for table in ("appointments", "followups", "followup_reminders", "followup_batches", "proposals"):
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM {}".format(table)).fetchone()[0], 0, table)

    def test_the_doctor_and_branch_default_to_whoever_is_on_duty_and_the_default_branch(self):
        row = self.only(self.row(doctor_id=None, branch_id=None))
        self.assertTrue(row["ok"], row)
        self.assertEqual((row["doctor"], row["branch"]), ("Dr. Mehta", "Branch A"))

    def test_a_slot_that_is_already_taken_is_named_and_the_nearest_free_time_is_proposed(self):
        self.schedule(self.row(self.staff_patient, due_time="10:00"))
        row = self.only(self.row(due_time="10:00"))
        self.assertFalse(row["ok"])
        self.assertIn("already booked", row["errors"][0])
        self.assertEqual((row["suggestion"]["date"], row["suggestion"]["time"]), (D2, "09:30"))

    def test_a_time_outside_the_doctors_hours_is_refused(self):
        for hhmm in ("08:00", "13:00", "14:30", "20:00", "23:30"):
            with self.subTest(time=hhmm):
                row = self.only(self.row(due_time=hhmm))
                self.assertFalse(row["ok"])
                self.assertIn("no doctor on duty at {}".format(hhmm), row["errors"][0])
                self.assertIn("09:00-13:00", row["errors"][0])

    def test_a_time_that_is_not_on_the_slot_grid_is_refused(self):
        self.assertFalse(self.only(self.row(due_time="10:15"))["ok"])

    def test_a_blocked_time_range_is_refused_with_a_free_time_nearby(self):
        booking_blocks.add_block(self.conn, D2, D2, "10:00", "11:00", reason="Staff meeting")
        row = self.only(self.row(due_time="10:30"))
        self.assertFalse(row["ok"])
        self.assertIn("blocked at that time (Staff meeting)", row["errors"][0])
        self.assertEqual((row["suggestion"]["date"], row["suggestion"]["time"]), (D2, "11:00"))

    def test_a_whole_day_block_refuses_the_day_and_proposes_the_next_open_day(self):
        booking_blocks.add_block(self.conn, D2, D2, reason="Clinic closed for renovation", branch_id=1)
        row = self.only(self.row(due_time="10:30"))
        self.assertFalse(row["ok"])
        self.assertIn("blocked that day (Clinic closed for renovation)", row["errors"][0])
        self.assertEqual((row["suggestion"]["date"], row["suggestion"]["time"]), (D3, "10:30"))

    def test_the_next_open_day_skips_a_run_of_blocked_days(self):
        booking_blocks.add_block(self.conn, D2, D4, reason="Holiday")
        row = self.only(self.row(due_time="10:00"))
        self.assertEqual(row["suggestion"]["date"], "2026-10-10")

    def test_a_doctors_leave_blocks_that_doctor_but_not_the_clinic(self):
        branches.ensure_seed(self.conn)                       # Branch B / C with Dr. Rao and Dr. Iyer
        rao = [d["id"] for d in branches.list_doctors(self.conn) if d["name"] == "Dr. Rao"][0]
        booking_blocks.add_block(self.conn, "2026-10-12", "2026-10-12", reason="On leave", doctor_id=rao)   # a Monday at B
        row = self.only(self.row(due_date="2026-10-12", branch_id=2, doctor_id=rao))
        self.assertFalse(row["ok"])
        self.assertIn("blocked that day (On leave)", row["errors"][0])
        self.assertEqual(row["suggestion"]["date"], "2026-10-14")       # Dr. Rao's next day at B
        self.assertTrue(self.only(self.row(due_date="2026-10-12", branch_id=1))["ok"])       # Dr. Mehta at A is unaffected

    def test_a_non_working_day_proposes_the_next_one(self):
        branches.ensure_seed(self.conn)
        row = self.only(self.row(due_date=SUNDAY, branch_id=2, doctor_id=None))
        self.assertFalse(row["ok"])
        self.assertIn("Branch B has no doctor on duty on Sundays", row["errors"][0])
        self.assertEqual(row["suggestion"]["date"], "2026-10-12")
        self.assertEqual(row["suggestion"]["doctor"], "Dr. Rao")

    def test_a_doctor_who_does_not_work_there_that_day_is_refused(self):
        branches.ensure_seed(self.conn)
        iyer = [d["id"] for d in branches.list_doctors(self.conn) if d["name"] == "Dr. Iyer"][0]
        row = self.only(self.row(due_date="2026-10-12", branch_id=2, doctor_id=iyer))      # Dr. Iyer is at B on Tue / Thu / Sat
        self.assertFalse(row["ok"])
        self.assertIn("Dr. Iyer does not work at Branch B on Mondays", row["errors"][0])
        self.assertEqual(row["suggestion"]["date"], "2026-10-13")

    def test_a_doctor_who_is_off_duty_at_that_hour_is_named(self):
        sharma = branches.add_doctor(self.conn, "Dr. Sharma")
        wednesday_evening = [s for s in branches.list_schedule(self.conn, 1) if s["weekday"] == 2 and s["start_time"] == "16:00"][0]
        branches.remove_schedule(self.conn, wednesday_evening["id"])
        branches.add_schedule(self.conn, sharma, 1, 2, "16:00", "20:00")
        row = self.only(self.row(due_time="17:00", doctor_id=1))
        self.assertFalse(row["ok"])
        self.assertEqual(row["errors"][0], "Dr. Mehta is not on duty then; Dr. Sharma is.")
        ok = self.only(self.row(due_time="17:00", doctor_id=sharma))
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(ok["doctor"], "Dr. Sharma")

    def test_a_closed_branch_is_refused(self):
        branches.ensure_seed(self.conn)
        branches.set_status(self.conn, 2, "closed", "Renovation", "Closed")
        row = self.only(self.row(due_date="2026-10-12", branch_id=2, doctor_id=None))
        self.assertIn("Branch B is marked closed", row["errors"][0])

    def test_the_past_and_the_far_future_are_refused(self):
        self.assertIn("already passed", self.only(self.row(due_date=TODAY, due_time="09:00"))["errors"][0])
        self.assertIn("already passed", self.only(self.row(due_date="2026-10-04", due_time="10:00"))["errors"][0])
        self.assertTrue(self.only(self.row(due_date=TODAY, due_time="09:30"))["ok"])
        self.assertIn("more than a year", self.only(self.row(due_date="2028-01-03", due_time="10:00"))["errors"][0])

    def test_bad_input_is_named_not_crashed_on(self):
        cases = ({"patient_id": None}, {"patient_id": 9999}, {"due_date": "someday"}, {"due_date": ""}, {"due_time": ""},
                 {"due_time": "25:00"}, {"branch_id": 99}, {"doctor_id": 99})
        for change in cases:
            with self.subTest(change=change):
                row = self.row()
                row.update(change)
                planned = self.only(row)
                self.assertFalse(planned["ok"])
                self.assertTrue(planned["errors"])
        garbage = self.plan({"patient_id": "x", "due_date": 5, "due_time": None, "diagnosis": 7})["rows"][0]
        self.assertFalse(garbage["ok"])

    def test_two_rows_cannot_take_the_same_slot(self):
        planned = self.plan(self.row(self.wa_patient), self.row(self.staff_patient))
        self.assertEqual([r["ok"] for r in planned["rows"]], [True, False])
        self.assertIn("Another row of this batch", planned["rows"][1]["errors"][0])

    def test_a_patient_without_a_valid_phone_is_an_error_not_a_warning(self):
        # A follow-up books a real appointment, and a new booking needs a phone (clinic/booking_phone.py).
        for phone in ("12345", ""):
            row = self.only(self.row(self.add_patient("No Phone", phone)))
            self.assertFalse(row["ok"], phone)
            self.assertEqual(row["errors"], [booking_phone.PATIENT_NO_PHONE])

    def test_warnings_are_not_errors(self):
        followups.set_opted_out(self.conn, "9876543210", True)
        self.assertIn("asked to stop reminders", self.only(self.row(self.wa_patient, due_time="11:00"))["warnings"][0])
        self.schedule(self.row(self.staff_patient, due_time="12:00"))
        again = self.only(self.row(self.staff_patient, due_time="12:30"))
        self.assertIn("already has a follow-up", again["warnings"][0])

    def test_the_batch_size_is_limited(self):
        with self.assertRaises(followups.FollowupError):
            followups.plan(self.conn, [], NOW)
        with self.assertRaises(followups.FollowupError):
            followups.plan(self.conn, "nope", NOW)
        with self.assertRaises(followups.FollowupError):
            followups.plan(self.conn, [self.row()] * (followups.MAX_BATCH_ROWS + 1), NOW)

    def test_a_long_diagnosis_is_trimmed_not_refused_at_plan_time(self):
        self.assertTrue(self.only(self.row(diagnosis="  note  "))["ok"])
        self.assertEqual(self.only(self.row(diagnosis="  note  "))["row"]["diagnosis"], "note")


class ApplyTests(FollowupCase):
    def test_booking_a_followup_books_the_slot_and_links_it(self):
        result = self.schedule(self.row(diagnosis="Review of blood sugar"))
        self.assertEqual(result["counts"], {"created": 1, "failed": 0})
        row = result["results"][0]
        appt = self.appointment(row["appointment_id"])
        self.assertEqual((appt["appt_date"], appt["start_time"], appt["status"], appt["branch_id"], appt["doctor_id"],
                          appt["patient_id"], appt["notes"]), (D2, "10:00", "booked", 1, 1, self.wa_patient, "Follow-up visit"))
        fu = self.followup(row["followup_id"])
        self.assertEqual((fu["patient_id"], fu["due_date"], fu["due_time"], fu["doctor_id"], fu["branch_id"],
                          fu["appointment_id"], fu["status"], fu["batch_id"], fu["diagnosis"]),
                         (self.wa_patient, D2, "10:00", 1, 1, appt["id"], "pending", result["batch_id"], "Review of blood sugar"))
        # the slot is really held: nobody else can take it
        self.assertFalse(scheduling.is_slot_free(self.conn, D2, "10:00", 30, branch_id=1))
        self.assertEqual(sorted(self.states(fu["id"]).items()), [("2d", "scheduled"), ("4h", "scheduled")])

    def test_every_write_is_audited_and_visible_to_staff(self):
        result = self.schedule(self.row(diagnosis="internal"))
        fid, aid = result["results"][0]["followup_id"], result["results"][0]["appointment_id"]
        audit = self.audit()
        self.assertEqual([(a["intent"], a["entity_type"], a["entity_id"]) for a in audit],
                         [("book_appointment", "appointment", aid), ("schedule_followup", "followup", fid)])
        payload = json.loads(audit[1]["payload_json"])
        self.assertEqual((payload["appointment_id"], payload["batch_id"], payload["due_time"]), (aid, result["batch_id"], "10:00"))
        activity = self.conn.execute("SELECT event, source, appointment_id, detail FROM patient_activity").fetchall()
        self.assertEqual([tuple(a)[:3] for a in activity], [("followup_scheduled", "staff", aid)])
        self.assertNotIn("internal", activity[0]["detail"])
        proposals = [r[0] for r in self.conn.execute("SELECT status FROM proposals")]
        self.assertEqual(proposals, ["confirmed", "confirmed"])

    def test_the_booking_goes_through_the_normal_notification_path(self):
        self.schedule(self.row(), with_notify=True)
        events = [n["event"] for n in self.notes()]
        self.assertEqual(events, ["booking_confirmed"])             # what any staff booking sends; reminders come later

    def test_several_rows_become_one_batch(self):
        result = self.schedule(self.row(self.wa_patient, due_time="10:00"), self.row(self.staff_patient, due_time="10:30"))
        self.assertEqual(result["counts"]["created"], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM followups WHERE batch_id = ?", (result["batch_id"],)).fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM followup_batches").fetchone()[0], 1)

    def test_a_failing_row_never_stops_the_others_and_is_never_dropped(self):
        rows = [self.row(self.wa_patient, due_time="10:00"), self.row(self.staff_patient, due_time="14:00"),
                self.row(self.wa_patient, due_date="not a date"), self.row(self.staff_patient, due_time="11:00")]
        result = self.schedule(*rows, expect=False)
        self.assertEqual(result["counts"], {"created": 2, "failed": 2})
        self.assertEqual([r["ok"] for r in result["results"]], [True, False, False, True])
        self.assertEqual([r["index"] for r in result["results"]], [0, 1, 2, 3])
        self.assertIn("no doctor on duty at 14:00", result["results"][1]["error"])
        self.assertTrue(result["results"][2]["error"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM followups").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0], 2)

    def test_a_slot_taken_after_the_review_fails_only_that_row(self):
        rows = [self.row(self.wa_patient, due_time="10:00"), self.row(self.staff_patient, due_time="10:30")]
        followups.plan(self.conn, rows, NOW)              # both looked fine at review time...
        self.conn.execute("INSERT INTO appointments (patient_name, patient_phone, appt_date, start_time, branch_id) "
                          "VALUES ('Walk In', '9000000000', ?, '10:00', 1)", (D2,))
        self.conn.commit()                                # ...then the front desk booked the first slot
        result = self.schedule(*rows, expect=False)
        self.assertEqual([r["ok"] for r in result["results"]], [False, True])
        self.assertIn("already booked", result["results"][0]["error"])
        self.assertEqual(result["results"][0]["suggestion"]["time"], "09:30")

    def test_a_refused_booking_is_reported_and_isolated(self):
        real = auto_actions.staff_action
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise auto_actions.ActionError("The write was refused.")
            return real(*args, **kwargs)
        rows = [self.row(self.wa_patient, due_time="10:00"), self.row(self.staff_patient, due_time="10:30"),
                self.row(self.wa_patient, due_time="11:00")]
        with mock.patch.object(auto_actions, "staff_action", flaky):
            result = self.schedule(*rows, expect=False)
        self.assertEqual([r["ok"] for r in result["results"]], [True, False, True])
        self.assertEqual(result["results"][1]["error"], "The write was refused.")

    def test_if_the_followup_cannot_be_saved_the_booked_slot_is_given_back(self):
        def broken(now, appointment_id, batch_id):
            def handler(conn, slots):
                raise RuntimeError("disk full")
            return handler
        with mock.patch.object(followups, "_create_handler", broken), self.assertLogs("clinic.followups", "ERROR"):
            result = self.schedule(self.row(), expect=False)
        self.assertEqual(result["counts"], {"created": 0, "failed": 1})
        self.assertIn("Could not save the follow-up", result["results"][0]["error"])
        self.assertEqual(self.conn.execute("SELECT status FROM appointments").fetchone()[0], "cancelled")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM followups").fetchone()[0], 0)
        self.assertIsNone(result["batch_id"])                     # nothing was made, so there is no batch to undo
        self.assertTrue(scheduling.is_slot_free(self.conn, D2, "10:00", 30, branch_id=1))

    def test_nothing_valid_makes_no_batch(self):
        result = self.schedule(self.row(due_time="14:00"), expect=False)
        self.assertEqual((result["batch_id"], result["counts"]), (None, {"created": 0, "failed": 1}))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM followup_batches").fetchone()[0], 0)

    def test_the_appointment_notes_never_carry_the_diagnosis(self):
        self.schedule(self.row(diagnosis="Hypothyroidism"))
        dump = json.dumps([dict(r) for r in self.conn.execute("SELECT * FROM appointments")])
        self.assertNotIn("Hypothyroidism", dump)

    def test_a_patient_whose_number_has_become_unusable_gets_no_reminders(self):
        # booked while the number was good; it has since become unusable (a new booking would be refused)
        no_phone = self.add_patient("No Phone", "9876543299")
        fid = self.make(patient_id=no_phone)
        self.conn.execute("UPDATE patients SET phone = '12345' WHERE id = ?", (no_phone,))
        self.conn.commit()
        followups.process_due(self.conn, at(5, 12))
        self.assertEqual(self.notes("followup_reminder_2d"), [])
        reasons = {r["kind"]: r["reason"] for r in self.reminder_rows(fid)}
        self.assertEqual(reasons["2d"], "no usable phone number")

    def test_the_same_slot_can_be_used_at_two_branches(self):
        branches.ensure_seed(self.conn)
        rao = [d["id"] for d in branches.list_doctors(self.conn) if d["name"] == "Dr. Rao"][0]
        result = self.schedule(self.row(self.wa_patient, due_date="2026-10-12", branch_id=1),
                               self.row(self.staff_patient, due_date="2026-10-12", branch_id=2, doctor_id=rao))
        self.assertEqual(result["counts"]["created"], 2)
        branch_ids = [self.followup(r["followup_id"])["branch_id"] for r in result["results"]]
        self.assertEqual(branch_ids, [1, 2])


class UndoTests(FollowupCase):
    def undo(self, batch_id, now=NOW):
        return followups.undo_batch(self.conn, batch_id, handlers=HANDLERS, after_commit=hooks(True), now=now)

    def test_undo_cancels_the_appointments_and_followups_and_frees_the_slots(self):
        result = self.schedule(self.row(self.wa_patient, due_time="10:00"), self.row(self.staff_patient, due_time="10:30"))
        undone = self.undo(result["batch_id"])
        self.assertEqual((undone["ok"], undone["cancelled"], undone["skipped"]), (True, 2, []))
        for r in result["results"]:
            self.assertEqual(self.followup(r["followup_id"])["status"], "cancelled")
            self.assertEqual(self.appointment(r["appointment_id"])["status"], "cancelled")
        self.assertTrue(scheduling.is_slot_free(self.conn, D2, "10:00", 30, branch_id=1))
        self.assertEqual(self.conn.execute("SELECT status FROM followup_batches").fetchone()[0], "undone")
        events = [r[0] for r in self.conn.execute("SELECT event FROM patient_activity ORDER BY id")]
        self.assertEqual(events.count("followup_undone"), 2)
        intents = [a["intent"] for a in self.audit()]
        self.assertEqual(intents.count("followup_batch_undone"), 2)
        self.assertEqual(intents.count("cancel_appointment"), 2)

    def test_undo_takes_queued_reminders_back_out_of_the_outbox(self):
        result = self.schedule(self.row(due_time="16:00"))
        fid = result["results"][0]["followup_id"]
        followups.process_due(self.conn, at(5, 12))                      # the early reminder is queued
        self.assertEqual(len(self.notes("followup_reminder_2d")), 1)
        self.undo(result["batch_id"])
        self.assertEqual(self.notes("followup_reminder_2d"), [])
        self.assertEqual(self.states(fid), {"2d": "cancelled", "4h": "cancelled"})
        followups.process_due(self.conn, at(7, 13))                      # and nothing is ever sent later
        self.assertEqual(self.notes("followup_reminder_4h"), [])

    def test_the_patient_is_only_told_cancelled_if_they_were_told_booked(self):
        told = self.schedule(self.row(self.wa_patient, due_time="10:00"), with_notify=True)
        self.conn.execute("UPDATE notifications SET status = 'sent'")        # the booking message did go out
        self.conn.commit()
        self.undo(told["batch_id"])
        self.assertEqual([n["event"] for n in self.notes()], ["booking_confirmed", "appointment_cancelled"])
        # a batch whose booking message never went out (blocked, no 24-hour window) sends no cancellation
        other = self.schedule(self.row(self.staff_patient, due_time="11:00"), with_notify=True)
        self.undo(other["batch_id"])
        self.assertEqual([n["event"] for n in self.notes()].count("appointment_cancelled"), 1)

    def test_a_batch_can_only_be_undone_once(self):
        result = self.schedule(self.row())
        self.assertTrue(self.undo(result["batch_id"])["ok"])
        again = self.undo(result["batch_id"])
        self.assertFalse(again["ok"])
        self.assertIn("already undone", again["error"])
        self.assertFalse(self.undo(9999)["ok"])

    def test_anything_that_moved_on_is_left_and_listed(self):
        result = self.schedule(self.row(self.wa_patient, due_time="10:00"), self.row(self.staff_patient, due_time="10:30"),
                               self.row(self.wa_patient, due_date=D3, due_time="10:00"))
        done, checked_in, fine = [r["appointment_id"] for r in result["results"]]
        self.change_appointment("queue_mark_done", {"appointment_id": done})
        self.conn.execute("UPDATE appointments SET queue_state = 'checked_in' WHERE id = ?", (checked_in,))
        self.conn.commit()
        undone = self.undo(result["batch_id"])
        self.assertEqual(undone["cancelled"], 1)
        names = {s["name"]: s["why"] for s in undone["skipped"]}
        self.assertIn("already done", names["Sunita Devi"])
        self.assertIn("already been seen or checked in", names["Rakesh Verma"])
        self.assertEqual(self.appointment(fine)["status"], "cancelled")
        self.assertEqual(self.appointment(done)["status"], "completed")


class SyncTests(FollowupCase):
    def test_a_rescheduled_appointment_moves_the_followup_and_its_reminders(self):
        fid = self.make(due_time="16:00")
        aid = self.followup(fid)["appointment_id"]
        followups.process_due(self.conn, at(5, 12))                       # early reminder for Wed 16:00 queued
        self.assertEqual(len(self.notes("followup_reminder_2d")), 1)
        self.change_appointment("reschedule_appointment", {"appointment_id": aid, "appt_date": D4, "start_time": "17:00"})
        fu = self.followup(fid)
        self.assertEqual((fu["due_date"], fu["due_time"], fu["status"]), (D4, "17:00", "pending"))
        # the old slot's unsent reminder is gone; the new slot has its own pair
        self.assertEqual(self.notes("followup_reminder_2d"), [])
        slots = {(r["slot_date"], r["slot_time"], r["kind"]): r["state"] for r in self.reminder_rows(fid)}
        self.assertEqual(slots[(D2, "16:00", "2d")], "cancelled")
        self.assertEqual(slots[(D4, "17:00", "2d")], "scheduled")
        self.assertEqual(slots[(D4, "17:00", "4h")], "scheduled")
        self.assertEqual(self.audit("followup_synced")[-1]["entity_id"], fid)

    def test_a_reminder_that_already_went_out_stays_sent_and_the_new_slot_gets_its_own(self):
        fid = self.make(due_time="16:00")
        aid = self.followup(fid)["appointment_id"]
        sender = Sender()
        followups.process_due(self.conn, at(5, 12))
        notify.flush(self.conn, sender, now=at(5, 12))
        self.assertEqual(len(sender.calls), 1)
        self.change_appointment("reschedule_appointment", {"appointment_id": aid, "appt_date": D4, "start_time": "17:00"})
        self.assertEqual(followups.process_due(self.conn, at(5, 13)), 0)    # the new slot's early reminder is not due yet
        self.assertEqual(followups.process_due(self.conn, at(7, 10, 1)), 1)
        keys = [n["dedup_key"] for n in self.notes()]
        self.assertEqual(keys, ["followup_reminder_2d:{}:{}@16:00".format(fid, D2),
                                "followup_reminder_2d:{}:{}@17:00".format(fid, D4)])
        self.assertEqual([n["status"] for n in self.notes()], ["sent", "pending"])      # the old one stays sent

    def test_moving_the_visit_to_another_branch_updates_branch_and_doctor(self):
        branches.ensure_seed(self.conn)
        rao = [d["id"] for d in branches.list_doctors(self.conn) if d["name"] == "Dr. Rao"][0]
        fid = self.make(due_date="2026-10-12")
        aid = self.followup(fid)["appointment_id"]
        self.change_appointment("reschedule_appointment", {"appointment_id": aid, "appt_date": "2026-10-12", "start_time": "10:00",
                                                          "branch_id": 2})
        fu = self.followup(fid)
        self.assertEqual((fu["branch_id"], fu["doctor_id"]), (2, rao))

    def test_cancelling_the_appointment_cancels_the_followup_and_its_reminders(self):
        fid = self.make(due_time="16:00")
        followups.process_due(self.conn, at(5, 12))
        self.assertEqual(len(self.notes("followup_reminder_2d")), 1)
        self.change_appointment("cancel_appointment", {"appointment_id": self.followup(fid)["appointment_id"]})
        self.assertEqual(self.followup(fid)["status"], "cancelled")
        self.assertEqual(self.notes("followup_reminder_2d"), [])
        self.assertEqual(self.states(fid), {"2d": "cancelled", "4h": "cancelled"})
        self.assertEqual(followups.process_due(self.conn, at(7, 13)), 0)

    def test_completing_the_visit_marks_the_followup_done_and_stops_reminders(self):
        fid = self.make(due_time="16:00")
        self.change_appointment("queue_mark_done", {"appointment_id": self.followup(fid)["appointment_id"]})
        fu = self.followup(fid)
        self.assertEqual(fu["status"], "done")
        self.assertIsNotNone(fu["completed_at"])
        self.assertEqual(self.states(fid), {"2d": "cancelled", "4h": "cancelled"})
        self.assertEqual(followups.process_due(self.conn, at(7, 13)), 0)

    def test_a_no_show_keeps_the_followup_pending_so_it_shows_as_missed(self):
        fid = self.make(due_time="16:00")
        self.change_appointment("queue_mark_no_show", {"appointment_id": self.followup(fid)["appointment_id"]})
        self.assertEqual(self.followup(fid)["status"], "pending")
        self.assertEqual(self.states(fid), {"2d": "cancelled", "4h": "cancelled"})
        self.assertEqual([r["id"] for r in self.conn.execute("SELECT id FROM followups")], [fid])
        from clinic import queries
        self.assertEqual([r["id"] for r in queries.missed_followups(self.conn, D2)], [fid])        # missed right away

    def test_an_undone_cancellation_brings_the_followup_back(self):
        fid = self.make(due_time="16:00")
        aid = self.followup(fid)["appointment_id"]
        self.change_appointment("cancel_appointment", {"appointment_id": aid})
        self.change_appointment("restore_appointment", {"appointment_id": aid})
        self.assertEqual(self.followup(fid)["status"], "pending")
        self.assertEqual(self.states(fid), {"2d": "scheduled", "4h": "scheduled"})

    def test_a_cancelled_followup_is_not_revived_by_the_periodic_sync(self):
        fid = self.make(due_time="16:00")
        self.conn.execute("UPDATE followups SET status = 'cancelled' WHERE id = ?", (fid,))      # staff cancelled just the follow-up
        self.conn.commit()
        followups.reconcile(self.conn, at(5, 12))
        self.assertEqual(self.followup(fid)["status"], "cancelled")
        self.assertEqual(followups.process_due(self.conn, at(5, 12)), 0)

    def test_the_periodic_sync_heals_a_write_that_bypassed_the_hook(self):
        fid = self.make(due_time="16:00")
        aid = self.followup(fid)["appointment_id"]
        self.conn.execute("UPDATE appointments SET appt_date = ?, start_time = '17:30' WHERE id = ?", (D4, aid))
        self.conn.commit()
        followups.reconcile(self.conn, at(5, 12))
        fu = self.followup(fid)
        self.assertEqual((fu["due_date"], fu["due_time"]), (D4, "17:30"))
        self.assertEqual(self.reminder_rows(fid)[-1]["slot_time"], "17:30")

    def test_the_periodic_sync_leaves_old_history_alone(self):
        fid = self.make(due_time="16:00")
        aid = self.followup(fid)["appointment_id"]
        self.conn.execute("UPDATE appointments SET status = 'completed' WHERE id = ?", (aid,))
        self.conn.commit()
        followups.reconcile(self.conn, at(20, 12))                   # two weeks on: not looked at any more
        self.assertEqual(self.followup(fid)["status"], "pending")
        followups.reconcile(self.conn, at(8, 12))                    # the day after the visit: still healed
        self.assertEqual(self.followup(fid)["status"], "done")

    def test_the_periodic_sync_changes_nothing_when_nothing_changed(self):
        fid = self.make()
        before = (len(self.audit()), self.reminder_rows(fid))
        followups.reconcile(self.conn, at(5, 12))
        followups.reconcile(self.conn, at(5, 12))
        self.assertEqual((len(self.audit()), self.reminder_rows(fid)), before)

    def test_a_voice_cancel_or_reschedule_of_a_slot_followup_is_pointed_at_its_appointment(self):
        fid = self.make()
        for intent, slots in (("cancel_followup", {"followup_id": fid}),
                              ("reschedule_followup", {"followup_id": fid, "new_due_date": D4})):
            proposal = core.propose(self.conn, intent, slots)
            with self.assertRaises(ValueError) as caught:
                core.confirm(self.conn, proposal, HANDLERS)
            self.assertIn("booked appointment", str(caught.exception))
        self.assertEqual(self.followup(fid)["status"], "pending")
        self.assertEqual(self.followup(fid)["due_date"], D2)

    def test_the_old_date_only_followup_still_works_exactly_as_before(self):
        proposal = core.propose(self.conn, "set_followup", {"patient_id": self.wa_patient, "days_from_now": 7})
        _, fid = core.confirm(self.conn, proposal, HANDLERS)
        fu = self.followup(fid)
        self.assertEqual((fu["appointment_id"], fu["due_time"], fu["status"]), (None, None, "pending"))
        self.assertEqual(self.reminder_rows(fid), [])
        self.assertEqual(followups.tick(self.conn, at(5, 12)), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0], 0)


class ClosureTests(FollowupCase):
    """A branch closure moves or cancels appointments through the same handlers; the follow-up follows."""

    def close_branch_a(self, action, followup_id):
        from clinic import closures
        aid = self.followup(followup_id)["appointment_id"]
        plan = closures.plan(self.conn, 1, D2, D2, now=NOW.local)
        move = [m for m in plan["moves"] if m["appointment_id"] == aid][0]
        to = move["to"] or {}
        result = closures.apply(
            self.conn, branch_id=1, start_date=D2, end_date=D2, handlers=HANDLERS, after_commit=hooks(), now=NOW.local, reason="Doctor ill",
            moves=[{"appointment_id": aid, "action": action, "to_branch_id": to.get("branch_id"), "to_date": to.get("date"), "to_time": to.get("time")}])
        self.assertEqual(result["counts"]["failed"], 0, result)
        return result

    def test_a_closure_that_moves_the_visit_moves_the_followup_and_its_undo_puts_it_back(self):
        from clinic import closures
        branches.ensure_seed(self.conn)
        fid = self.make(due_time="16:00")
        result = self.close_branch_a("move", fid)
        fu = self.followup(fid)
        self.assertEqual((fu["branch_id"], fu["due_date"], fu["due_time"], fu["status"]), (2, D2, "16:00", "pending"))
        self.assertEqual(branches.doctor_label(self.conn, fu["doctor_id"]), "Dr. Rao")
        self.assertEqual(self.states(fid), {"2d": "scheduled", "4h": "scheduled"})
        closures.undo(self.conn, result["closure_id"], HANDLERS, hooks(), NOW.local)
        fu = self.followup(fid)
        self.assertEqual((fu["branch_id"], fu["doctor_id"]), (1, 1))

    def test_a_closure_that_cancels_the_visit_cancels_the_followup_and_its_undo_brings_it_back(self):
        from clinic import closures
        branches.ensure_seed(self.conn)
        fid = self.make(due_time="16:00")
        followups.process_due(self.conn, at(5, 10))
        result = self.close_branch_a("cancel", fid)
        self.assertEqual(self.followup(fid)["status"], "cancelled")
        self.assertEqual(self.notes("followup_reminder_2d"), [])
        closures.undo(self.conn, result["closure_id"], HANDLERS, hooks(), NOW.local)
        self.assertEqual(self.followup(fid)["status"], "pending")
        self.assertEqual(self.states(fid), {"2d": "scheduled", "4h": "scheduled"})


class DiagnosisTests(FollowupCase):
    def test_staff_can_edit_the_diagnosis_and_each_edit_is_audited(self):
        fid = self.make(diagnosis="first note")
        self.assertEqual(followups.edit_diagnosis(self.conn, fid, "  second note "), "second note")
        self.assertEqual(self.followup(fid)["diagnosis"], "second note")
        edits = self.audit("followup_diagnosis_edited")
        self.assertEqual(len(edits), 1)
        payload = json.loads(edits[0]["payload_json"])
        self.assertEqual((payload["diagnosis_old"], payload["diagnosis_new"]), ("first note", "second note"))
        self.assertIsNone(followups.edit_diagnosis(self.conn, fid, ""))        # clearing it is an edit too
        self.assertEqual(len(self.audit("followup_diagnosis_edited")), 2)
        followups.edit_diagnosis(self.conn, fid, None)                         # no change, no audit row
        self.assertEqual(len(self.audit("followup_diagnosis_edited")), 2)

    def test_bad_edits_are_refused(self):
        fid = self.make()
        with self.assertRaises(followups.FollowupError):
            followups.edit_diagnosis(self.conn, fid, "x" * 501)
        with self.assertRaises(followups.FollowupError):
            followups.edit_diagnosis(self.conn, fid, 123)
        with self.assertRaises(followups.FollowupError):
            followups.edit_diagnosis(self.conn, 9999, "x")


class ListTests(FollowupCase):
    def test_the_list_and_the_branch_filter(self):
        branches.ensure_seed(self.conn)
        rao = [d["id"] for d in branches.list_doctors(self.conn) if d["name"] == "Dr. Rao"][0]
        a = self.make(patient_id=self.wa_patient, due_time="10:00")
        b = self.make(patient_id=self.staff_patient, due_date="2026-10-12", branch_id=2, doctor_id=rao)
        everyone = followups.list_followups(self.conn, "all", NOW)
        self.assertEqual([f["id"] for f in everyone], [a, b])
        self.assertEqual([f["id"] for f in followups.list_followups(self.conn, 1, NOW)], [a])
        self.assertEqual([f["id"] for f in followups.list_followups(self.conn, 2, NOW)], [b])
        item = everyone[0]
        self.assertEqual((item["patient_name"], item["doctor"], item["branch"], item["due_time"], item["has_slot"]),
                         ("Sunita Devi", "Dr. Mehta", "Branch A", "10:00", True))
        self.assertEqual([r["status"] for r in item["reminders"]], ["queued", "queued"])

    def test_old_finished_followups_drop_off_but_pending_ones_stay(self):
        fid = self.make()
        old = self.conn.execute("INSERT INTO followups (patient_id, due_date, status) VALUES (?, '2026-08-01', 'done')", (self.wa_patient,)).lastrowid
        stale = self.conn.execute("INSERT INTO followups (patient_id, due_date, status) VALUES (?, '2026-08-01', 'pending')", (self.wa_patient,)).lastrowid
        self.conn.commit()
        ids = [f["id"] for f in followups.list_followups(self.conn, "all", NOW)]
        self.assertIn(fid, ids)
        self.assertIn(stale, ids)
        self.assertNotIn(old, ids)
        legacy = [f for f in followups.list_followups(self.conn, "all", NOW) if f["id"] == stale][0]
        self.assertEqual((legacy["has_slot"], legacy["reminders"]), (False, []))

    def test_the_missed_list_keeps_old_behaviour_for_date_only_followups(self):
        from clinic import queries
        legacy = self.conn.execute("INSERT INTO followups (patient_id, due_date) VALUES (?, ?)", (self.wa_patient, TODAY)).lastrowid
        slot = self.make(due_date=TODAY, due_time="11:00")
        self.conn.commit()
        # date-only: overdue from its due date (as before); a booked slot later today is not missed yet
        self.assertEqual([r["id"] for r in queries.missed_followups(self.conn, TODAY)], [legacy])
        self.assertEqual(sorted(r["id"] for r in queries.missed_followups(self.conn, D1)), sorted([legacy, slot]))


class FreeSlotTests(FollowupCase):
    def test_the_time_dropdown_offers_free_times_for_that_doctor(self):
        self.schedule(self.row(due_time="10:00"))
        out = followups.free_slots(self.conn, D2, 1, 1, NOW)
        self.assertNotIn("10:00", out["free"])
        self.assertIn("09:30", out["free"])
        self.assertEqual((out["doctor"], out["problem"]), ("Dr. Mehta", None))

    def test_a_closed_day_says_why_and_proposes_another(self):
        booking_blocks.add_block(self.conn, D2, D2, reason="Holiday")
        out = followups.free_slots(self.conn, D2, 1, None, NOW)
        self.assertEqual(out["free"], [])
        self.assertIn("blocked that day (Holiday)", out["problem"])
        self.assertEqual(out["suggestion"]["date"], D3)

    def test_todays_times_in_the_past_are_not_offered(self):
        out = followups.free_slots(self.conn, TODAY, 1, None, at(5, 10, 15))
        self.assertEqual(out["free"][0], "10:30")


if __name__ == "__main__":
    unittest.main()
