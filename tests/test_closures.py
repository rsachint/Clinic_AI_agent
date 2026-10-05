"""Closing a branch for a stretch of time and moving its patients as one
reviewed batch (clinic/closures.py): the plan, applying it through the audited
write path, the patient notices, undoing it, and the patient's answer."""
import json
import sqlite3
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_conversation import make_db  # noqa: E402
from tests.test_conversation_branches import add_branches  # noqa: E402

from clinic import branches, closure_notify, closures, scheduling  # noqa: E402
from clinic.intents import HANDLERS  # noqa: E402

A, B, C = 1, 2, 3
NOW = datetime(2026, 10, 5, 10, 0)           # Monday
TUE, WED = "2026-10-06", "2026-10-07"


class ClosureCase(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        add_branches(self.conn)             # A 09-13 & 16-20 (Dr. Mehta), B 10-14 (Dr. Rao), C 09-12 (Dr. Iyer)
        self.hooks = []

    def appt(self, name, branch, day, start, phone="9876500001", status="booked", queue_state=None, patient_id=None):
        doctor = branches.doctor_at(self.conn, branch, day, start)
        cur = self.conn.execute(
            "INSERT INTO appointments (patient_id, patient_name, patient_phone, appt_date, start_time, duration_minutes, "
            "status, branch_id, doctor_id, queue_state) VALUES (?, ?, ?, ?, ?, 30, ?, ?, ?, ?)",
            (patient_id, None if patient_id else name, None if patient_id else phone, day, start, status, branch, doctor, queue_state))
        self.conn.commit()
        return cur.lastrowid

    def after_commit(self, conn, intent, slots, entity_id, wa_id=None, language=None):
        self.hooks.append((intent, dict(slots)))

    def plan(self, **kw):
        args = dict(branch_id=A, start_date=TUE, end_date=TUE, now=NOW)
        args.update(kw)
        return closures.plan(self.conn, **args)

    def apply(self, plan, reason="Doctor on leave", message="", **overrides):
        moves = [{"appointment_id": m["appointment_id"], "action": m["action"],
                  "to_branch_id": (m["to"] or {}).get("branch_id"), "to_date": (m["to"] or {}).get("date"),
                  "to_time": (m["to"] or {}).get("time")} for m in plan["moves"]]
        scope = plan["scope"]
        args = dict(branch_id=scope["branch_id"], start_date=scope["start_date"], end_date=scope["end_date"],
                    start_time=scope["start_time"], end_time=scope["end_time"], doctor_id=scope["doctor_id"],
                    moves=moves, handlers=HANDLERS, after_commit=self.after_commit, now=NOW, reason=reason, message=message)
        args.update(overrides)
        return closures.apply(self.conn, **args)

    def row(self, appointment_id):
        return self.conn.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,)).fetchone()

    def notices(self, event=None):
        sql = "SELECT * FROM notifications" + (" WHERE event = ?" if event else "") + " ORDER BY id"
        return self.conn.execute(sql, (event,) if event else ()).fetchall()


class Planning(ClosureCase):
    def test_the_same_time_at_the_nearest_other_branch_is_the_default(self):
        a = self.appt("Pt One", A, TUE, "10:00")
        move = self.plan()["moves"][0]
        self.assertEqual(move["appointment_id"], a)
        self.assertEqual((move["action"], move["to"]["branch_id"], move["to"]["time"]), ("move", B, "10:00"))     # B is nearer A than C
        self.assertEqual(move["to"]["doctor"], "Dr. Rao")
        self.assertIsNone(move["note"])

    def test_the_closest_free_time_is_used_when_the_exact_one_is_taken(self):
        self.appt("Pt One", A, TUE, "11:00")
        self.appt("Blocker", B, TUE, "11:00", phone="9111100001")
        move = self.plan()["moves"][0]
        self.assertEqual(move["action"], "move")
        self.assertIn(move["to"]["time"], ("10:30", "11:30"))
        self.assertIn("Closest free time", move["note"])

    def test_two_patients_are_never_planned_into_the_same_slot(self):
        self.appt("X", A, TUE, "11:00", phone="9111100001")
        self.appt("Y", A, TUE, "11:30", phone="9111100002")
        self.appt("Blocker", B, TUE, "11:30", phone="9111100003")        # Y's exact time at B is gone
        moves = self.plan()["moves"]
        slots = [(m["to"]["branch_id"], m["to"]["date"], m["to"]["time"]) for m in moves]
        self.assertEqual(len(slots), len(set(slots)))
        self.assertEqual(moves[0]["to"]["time"], "11:00")                # X keeps its exact time

    def test_no_fit_means_leave_it_alone_and_say_so_not_a_silent_cancel(self):
        self.appt("Evening", A, TUE, "17:00")                            # no other branch has a doctor at 17:00
        move = self.plan()["moves"][0]
        self.assertEqual((move["action"], move["to"]), ("leave", None))
        self.assertIn("No free time", move["note"])
        self.assertTrue(any(o["date"] != TUE for o in move["options"]))   # later days are offered

    def test_only_active_unstarted_appointments_in_scope_are_touched(self):
        keep = self.appt("In", A, TUE, "10:00")
        self.appt("Cancelled", A, TUE, "10:30", status="cancelled", phone="9111100002")
        self.appt("Checked in", A, TUE, "11:00", queue_state="checked_in", phone="9111100003")
        self.appt("Other branch", B, TUE, "10:00", phone="9111100004")
        self.appt("Other day", A, WED, "10:00", phone="9111100005")
        self.assertEqual([m["appointment_id"] for m in self.plan()["moves"]], [keep])
        past = self.appt("Already over", A, "2026-10-05", "09:00", phone="9111100006")
        moves = closures.plan(self.conn, A, "2026-10-05", "2026-10-05", now=NOW)["moves"]
        self.assertNotIn(past, [m["appointment_id"] for m in moves])

    def test_a_time_range_only_touches_that_range(self):
        self.appt("Morning", A, TUE, "10:00")
        evening = self.appt("Evening", A, TUE, "17:00", phone="9111100002")
        moves = self.plan(start_time="16:00", end_time="20:00")["moves"]
        self.assertEqual([m["appointment_id"] for m in moves], [evening])

    def test_a_doctors_leave_touches_only_that_doctors_patients(self):
        mine = self.appt("Mehta pt", A, TUE, "10:00")
        other = self.appt("Other doctor pt", A, TUE, "11:00", phone="9111100002")
        self.conn.execute("UPDATE appointments SET doctor_id = 2 WHERE id = ?", (other,))
        self.conn.commit()
        self.assertEqual([m["appointment_id"] for m in self.plan(doctor_id=1)["moves"]], [mine])

    def test_input_is_checked(self):
        for kw in (dict(branch_id=None), dict(branch_id=99), dict(end_date="2026-10-01"), dict(start_date="x"),
                   dict(doctor_id=99), dict(start_time="10:00")):
            with self.subTest(kw=kw), self.assertRaises(closures.ClosureError):
                self.plan(**kw)

    def test_a_clinic_with_no_other_branch_has_nowhere_to_move_people(self):
        self.conn.execute("UPDATE branches SET status = 'closed' WHERE id IN (2, 3)")
        self.conn.commit()
        self.appt("Pt", A, TUE, "10:00")
        move = self.plan()["moves"][0]
        self.assertEqual(move["action"], "leave")
        self.assertIn("no other open branch", move["note"])


class Applying(ClosureCase):
    def test_a_batch_moves_everyone_through_the_audited_path_and_blocks_new_bookings(self):
        one = self.appt("Pt One", A, TUE, "10:00", phone="9111100001")
        two = self.appt("Pt Two", A, TUE, "11:00", phone="9111100002")
        result = self.apply(self.plan())
        self.assertEqual(result["counts"], {"moved": 2, "cancelled": 0, "failed": 0, "left": 0})
        for aid, start in ((one, "10:00"), (two, "11:00")):
            row = self.row(aid)
            self.assertEqual((row["branch_id"], row["appt_date"], row["start_time"]), (B, TUE, start))
            self.assertEqual(branches.doctor_label(self.conn, row["doctor_id"]), "Dr. Rao")
        # audited like every other write
        audits = self.conn.execute("SELECT intent FROM audit_log WHERE intent = 'reschedule_appointment'").fetchall()
        self.assertEqual(len(audits), 2)
        sources = {r[0] for r in self.conn.execute("SELECT source_text FROM proposals")}
        self.assertTrue(all(s.startswith("[staff:closure] closure #") for s in sources), sources)
        # the closed branch cannot be booked into during the window
        self.assertFalse(scheduling.is_slot_free(self.conn, TUE, "10:00", 30, branch_id=A))
        block = self.conn.execute("SELECT * FROM booking_blocks WHERE id = ?", (result["block_id"],)).fetchone()
        self.assertEqual((block["branch_id"], block["reason"], block["active"]), (A, "Doctor on leave", 1))
        # the queue / calendar hooks ran for each, marked quiet so the usual "moved" text is not also sent
        self.assertEqual(len(self.hooks), 2)
        self.assertTrue(all(slots.get("quiet") for _, slots in self.hooks))

    def test_each_patient_gets_one_notice_with_accept_and_choose_another(self):
        one = self.appt("Pt One", A, TUE, "10:00", phone="9111100001")
        result = self.apply(self.plan(), reason="Doctor on leave", message="Sorry for the trouble.")
        notices = self.notices("closure_moved")
        self.assertEqual(len(notices), 1)
        body = notices[0]["body"]
        self.assertIn("Branch A", body)
        self.assertIn("(Doctor on leave)", body)
        self.assertIn("Sorry for the trouble.", body)
        self.assertIn("\U0001F4CD Branch B, ", body)
        self.assertIn("Dr. Rao", body)
        spec = json.loads(notices[0]["interactive_json"])
        move_id = result["results"][0]["move_id"]
        self.assertEqual([b["id"] for b in spec["buttons"]],
                         ["closure:accept:{}".format(move_id), "closure:change:{}".format(move_id)])
        self.assertTrue(all(len(b["title"]) <= 20 for b in spec["buttons"]))
        self.assertEqual(self.notices("appointment_rescheduled"), [])       # not also the generic message

    def test_a_cancel_choice_cancels_and_offers_to_book_again(self):
        aid = self.appt("Pt", A, TUE, "17:00")
        plan = self.plan()
        plan["moves"][0]["action"] = "cancel"
        result = self.apply(plan)
        self.assertEqual(result["counts"]["cancelled"], 1)
        self.assertEqual(self.row(aid)["status"], "cancelled")
        notice = self.notices("closure_cancelled")[0]
        self.assertIn("has been cancelled", notice["body"])
        self.assertEqual(json.loads(notice["interactive_json"])["buttons"][0]["id"], "menu:book")
        self.assertEqual(self.notices("appointment_cancelled_by_clinic"), [])

    def test_a_left_alone_patient_is_untouched_and_listed(self):
        aid = self.appt("Evening", A, TUE, "17:00")
        result = self.apply(self.plan())                       # the default for no fit is "leave"
        self.assertEqual(result["counts"], {"moved": 0, "cancelled": 0, "failed": 0, "left": 1})
        self.assertEqual(result["left"][0]["appointment_id"], aid)
        self.assertEqual(self.row(aid)["branch_id"], A)
        self.assertEqual(self.notices(), [])

    def test_one_failure_does_not_stop_the_rest(self):
        one = self.appt("Pt One", A, TUE, "10:00", phone="9111100001")
        two = self.appt("Pt Two", A, TUE, "11:00", phone="9111100002")
        plan = self.plan()
        self.appt("Late booker", B, TUE, "10:00", phone="9111100009")      # someone takes Pt One's target after the plan
        result = self.apply(plan)
        by_id = {r["appointment_id"]: r for r in result["results"]}
        self.assertEqual(by_id[one]["result"], "failed")
        self.assertIn("just taken", by_id[one]["error"])
        self.assertEqual(by_id[two]["result"], "done")
        self.assertEqual(self.row(one)["branch_id"], A)                    # nothing half-done
        self.assertEqual(result["counts"]["failed"], 1)
        self.assertEqual(len(self.notices("closure_moved")), 1)            # only the one who actually moved is told

    def test_appointments_outside_the_closure_cannot_be_smuggled_in(self):
        inside = self.appt("In", A, TUE, "10:00")
        outside = self.appt("Out", C, TUE, "10:00", phone="9111100002")
        plan = self.plan()
        moves = [{"appointment_id": inside, "action": "move", "to_branch_id": B, "to_date": TUE, "to_time": "10:00"},
                 {"appointment_id": inside, "action": "cancel"},                         # a duplicate is ignored
                 {"appointment_id": outside, "action": "cancel"},
                 {"appointment_id": "nope", "action": "cancel"}]
        result = self.apply(plan, moves=moves)
        self.assertEqual([r["appointment_id"] for r in result["results"]], [inside])
        self.assertEqual(self.row(outside)["status"], "booked")

    def test_a_move_without_a_target_fails_cleanly(self):
        aid = self.appt("Pt", A, TUE, "10:00")
        result = self.apply(self.plan(), moves=[{"appointment_id": aid, "action": "move"}])
        self.assertEqual(result["results"][0]["result"], "failed")
        self.assertEqual(self.row(aid)["branch_id"], A)

    def test_an_over_long_message_is_refused_before_anything_changes(self):
        self.appt("Pt", A, TUE, "10:00")
        with self.assertRaises(closures.ClosureError):
            self.apply(self.plan(), message="x" * 301)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM booking_blocks").fetchone()[0], 0)

    def test_the_activity_log_records_each_move(self):
        self.appt("Pt", A, TUE, "10:00")
        self.apply(self.plan())
        events = [r["event"] for r in self.conn.execute("SELECT event FROM patient_activity")]
        self.assertEqual(events, ["closure_moved"])


class Undoing(ClosureCase):
    def test_undo_puts_everyone_back_and_reopens_the_branch(self):
        one = self.appt("Pt One", A, TUE, "10:00", phone="9111100001")
        two = self.appt("Pt Two", A, TUE, "17:00", phone="9111100002")
        plan = self.plan()
        plan["moves"][1]["action"] = "cancel"
        result = self.apply(plan)
        undone = closures.undo(self.conn, result["closure_id"], HANDLERS, self.after_commit, NOW)
        self.assertEqual((undone["ok"], undone["restored"], undone["skipped"]), (True, 2, []))
        self.assertEqual((self.row(one)["branch_id"], self.row(one)["start_time"]), (A, "10:00"))
        self.assertEqual(self.row(two)["status"], "booked")
        self.assertTrue(scheduling.is_slot_free(self.conn, TUE, "12:00", 30, branch_id=A))     # open for new bookings again
        self.assertEqual(self.conn.execute("SELECT active FROM booking_blocks").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT status FROM closures").fetchone()[0], "undone")

    def test_undo_leaves_alone_what_the_patient_changed_since(self):
        one = self.appt("Pt One", A, TUE, "10:00", phone="9111100001")
        two = self.appt("Pt Two", A, TUE, "11:00", phone="9111100002")
        result = self.apply(self.plan())
        # Pt One picked another time themselves after the notice
        self.conn.execute("UPDATE appointments SET start_time = '12:00' WHERE id = ?", (one,))
        self.conn.commit()
        undone = closures.undo(self.conn, result["closure_id"], HANDLERS, self.after_commit, NOW)
        self.assertEqual(undone["restored"], 1)
        self.assertEqual([s["name"] for s in undone["skipped"]], ["Pt One"])
        self.assertIn("changed it", undone["skipped"][0]["why"])
        self.assertEqual(self.row(one)["start_time"], "12:00")
        self.assertEqual(self.row(one)["branch_id"], B)
        self.assertEqual(self.row(two)["branch_id"], A)

    def test_undo_puts_back_an_appointment_that_was_booked_outside_the_doctors_hours(self):
        # 15:00 at Branch A is between its two doctor shifts (old data); closing and undoing must not strand it.
        gap = self.appt("Gap pt", A, TUE, "15:00", phone="9111100003")
        plan = self.plan()
        move = plan["moves"][0]
        move["action"], move["to"] = "move", move["options"][0]
        result = self.apply(plan)
        self.assertEqual(result["counts"]["moved"], 1)
        undone = closures.undo(self.conn, result["closure_id"], HANDLERS, self.after_commit, NOW)
        self.assertEqual((undone["restored"], undone["skipped"]), (1, []))
        self.assertEqual((self.row(gap)["branch_id"], self.row(gap)["start_time"]), (A, "15:00"))

    def test_a_closure_can_only_be_undone_once(self):
        self.appt("Pt", A, TUE, "10:00")
        result = self.apply(self.plan())
        self.assertTrue(closures.undo(self.conn, result["closure_id"], HANDLERS, self.after_commit, NOW)["ok"])
        again = closures.undo(self.conn, result["closure_id"], HANDLERS, self.after_commit, NOW)
        self.assertFalse(again["ok"])
        self.assertFalse(closures.undo(self.conn, 999, HANDLERS, self.after_commit, NOW)["ok"])


class ThePatientsAnswer(ClosureCase):
    def setUp(self):
        super().setUp()
        self.aid = self.appt("Pt One", A, TUE, "10:00", phone="9111100001")
        self.result = self.apply(self.plan())
        self.move_id = self.result["results"][0]["move_id"]

    def test_accepting_is_recorded_for_the_owner_only(self):
        self.assertIsNone(closures.respond(self.conn, self.move_id, "919222200002", "accepted", NOW))
        move = closures.respond(self.conn, self.move_id, "919111100001", "accepted", NOW)
        self.assertEqual(move["response"], "accepted")
        self.assertIsNone(closures.pending_for_sender(self.conn, "919111100001"))

    def test_choosing_another_is_recorded_and_bad_input_ignored(self):
        self.assertEqual(closures.respond(self.conn, self.move_id, "919111100001", "changed", NOW)["response"], "changed")
        self.assertIsNone(closures.respond(self.conn, self.move_id, "919111100001", "banana", NOW))
        self.assertIsNone(closures.respond(self.conn, 9999, "919111100001", "accepted", NOW))

    def test_the_newest_unanswered_notice_is_found_for_a_typed_ok(self):
        found = closures.pending_for_sender(self.conn, "919111100001")
        self.assertEqual(found["id"], self.move_id)
        self.assertIsNone(closures.pending_for_sender(self.conn, "919222200002"))

    def test_an_undone_closure_has_nothing_left_to_answer(self):
        closures.undo(self.conn, self.result["closure_id"], HANDLERS, self.after_commit, NOW)
        self.assertIsNone(closures.pending_for_sender(self.conn, "919111100001"))
        self.assertIsNone(closures.respond(self.conn, self.move_id, "919111100001", "accepted", NOW))


class TheNotice(ClosureCase):
    def test_every_language_renders_with_short_button_titles(self):
        aid = self.appt("Pt One", A, TUE, "10:00", phone="9111100001")
        result = self.apply(self.plan(), reason="Renovation", message="Parking is closed.")
        move = self.conn.execute("SELECT * FROM closure_moves WHERE id = ?", (result["results"][0]["move_id"],)).fetchone()
        closure = self.conn.execute("SELECT * FROM closures").fetchone()
        from clinic import notify
        appointment = notify._appointment_context(self.conn, aid)
        for lang in ("en", "hi", "hinglish"):
            text, spec = closure_notify.compose(self.conn, move, closure, appointment, lang)
            self.assertIn("Renovation", text)
            self.assertIn("Parking is closed.", text)
            self.assertNotIn("{", text)
            self.assertLessEqual(len(text), 1024)
            self.assertTrue(all(len(b["title"]) <= 20 for b in spec["buttons"]), spec)
            self.assertEqual(len(spec["buttons"]), 2)

    def test_the_list_view_counts_what_patients_answered(self):
        self.appt("Pt One", A, TUE, "10:00", phone="9111100001")
        self.appt("Pt Two", A, TUE, "11:00", phone="9111100002")
        result = self.apply(self.plan())
        ids = [r["move_id"] for r in result["results"]]
        closures.respond(self.conn, ids[0], "919111100001", "accepted", NOW)
        listed = closures.list_closures(self.conn)[0]
        self.assertEqual(listed["counts"], {"moved": 2, "cancelled": 0, "failed": 0, "accepted": 1, "changed": 0, "waiting": 1,
                                          "undone": 0, "skipped": 0})
        self.assertEqual(listed["branch"], "Branch A")
        self.assertEqual(len(listed["moves"]), 2)


if __name__ == "__main__":
    unittest.main()
