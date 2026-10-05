"""The WhatsApp booking conversation with several branches: the patient is
asked which branch (nearest first by PIN, last-visited first otherwise), then
is only offered that branch's free times, and the summary, the booking and the
confirmation all name the branch and its doctor.

Part 1 drives the dialogue directly (clinic/conversation.py); part 2 goes
through the real webhook and the automatic-commit path.
"""
import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_conversation import ConvCase, FRI, NOW, SAY, TOMORROW, WA, WA2  # noqa: E402
from tests.test_auto_appointments import AutoCase  # noqa: E402

from clinic import auto_policy, booking_blocks, branches, conv_templates as ct, conversation as cv  # noqa: E402
from clinic.whatsapp_pipeline import last_branch_id  # noqa: E402

A, B, C = 1, 2, 3


def add_branches(conn):
    """Branch A (the seeded one: 09-13 and 16-20), B (10-14) and C (09-12), every day."""
    b = branches.add_branch(conn, "B", "Branch B", "Sector 56, Gurugram", maps_url="https://maps.example/b", pin_code="122011")
    c = branches.add_branch(conn, "C", "Branch C", "MG Road", pin_code="122018")
    rao = branches.add_doctor(conn, "Dr. Rao")
    iyer = branches.add_doctor(conn, "Dr. Iyer")
    for weekday in range(7):
        branches.add_schedule(conn, rao, b, weekday, "10:00", "14:00")
        branches.add_schedule(conn, iyer, c, weekday, "09:00", "12:00")
    assert (b, c) == (B, C)
    return b, c


class BranchDialogue(ConvCase):
    def setUp(self):
        super().setUp()
        add_branches(self.conn)

    def row_ids(self, result):
        return [row[0] for row in result.replies[0].rows]

    def start_booking(self, registered=True):
        if registered:
            self.patient()
        return self.send("I want to book an appointment")

    # -- asking --------------------------------------------------------------------
    def test_a_booking_asks_which_branch_with_the_address_under_each_name(self):
        r = self.start_booking()
        self.assertEqual(r.replies[0].text, ct.text("ask_branch", "en"))
        self.assertEqual(self.row_ids(r), ["branch:1", "branch:2", "branch:3"])
        self.assertEqual([row[1] for row in r.replies[0].rows], ["Branch A", "Branch B", "Branch C"])
        self.assertEqual(r.replies[0].rows[1][2], "Sector 56, Gurugram")
        self.assertEqual(r.replies[0].list_button, ct.button("choose_branch", "en"))
        self.assertEqual(self.session()["step"], "branch")

    def test_an_unregistered_patient_gives_a_name_first_then_picks_a_branch(self):
        r = self.start_booking(registered=False)
        self.assertEqual(r.replies[0].text, ct.text("ask_name", "en"))
        r = self.send("Sunita Devi")
        self.assertEqual(r.replies[0].text, ct.text("ask_branch", "en"))

    def test_a_single_branch_clinic_is_never_asked(self):
        self.conn.execute("UPDATE branches SET active = 0 WHERE id IN (2, 3)")
        self.conn.commit()
        self.patient()
        r = self.send("I want to book an appointment")
        self.assertEqual(r.replies[0].text, ct.text("ask_day", "en"))
        self.assertNotIn("branch_id", self.session()["slots"])

    def test_only_one_branch_able_to_take_bookings_is_used_without_asking(self):
        self.conn.execute("UPDATE branches SET status = 'closed' WHERE id IN (2, 3)")
        self.conn.commit()
        self.patient()
        r = self.send("I want to book an appointment")
        self.assertIn("Branch A", r.replies[0].text)              # the day question, headed with the branch
        self.assertEqual(self.session()["slots"]["branch_id"], A)

    def test_a_branch_without_a_doctor_scheduled_is_not_offered(self):
        branches.add_branch(self.conn, "D", "Branch D", pin_code="122099")
        r = self.start_booking()
        self.assertEqual(self.row_ids(r), ["branch:1", "branch:2", "branch:3"])

    # -- nearest by PIN, then last visit ---------------------------------------------
    def test_typing_a_pin_code_lists_the_nearest_branch_first_with_why(self):
        self.start_booking()
        r = self.send("my pin is 122019")
        self.assertEqual(r.replies[0].text, ct.text("ask_branch_nearest", "en", pin="122019"))
        self.assertEqual(self.row_ids(r), ["branch:3", "branch:2", "branch:1"])
        self.assertTrue(r.replies[0].rows[0][2].startswith("Very close"))
        r = self.send(choice="branch:3")
        self.assertEqual(self.session()["slots"]["branch_id"], C)

    def test_a_pin_in_the_first_message_orders_the_list(self):
        self.patient()
        r = self.send("book an appointment, my PIN is 122012")
        self.assertEqual(self.row_ids(r)[0], "branch:2")

    def test_the_branch_of_their_last_visit_is_suggested_first(self):
        pid = self.patient()
        self.conn.execute("UPDATE appointments SET branch_id = 1")
        cur = self.conn.execute(
            "INSERT INTO appointments (patient_id, appt_date, start_time, status, branch_id) VALUES (?, '2026-09-20', '10:00', 'completed', ?)",
            (pid, C))
        self.conn.commit()
        self.assertEqual(last_branch_id(self.conn, WA, pid), C)
        r = self.send("I want to book an appointment")
        self.assertEqual(self.row_ids(r), ["branch:3", "branch:1", "branch:2"])
        self.assertTrue(r.replies[0].rows[0][2].startswith("Your last visit"))

    def test_an_upcoming_booking_is_not_a_last_visit(self):
        pid = self.patient()
        self.conn.execute("INSERT INTO appointments (patient_id, appt_date, start_time, status, branch_id) VALUES (?, '2099-01-01', '10:00', 'booked', ?)",
                          (pid, C))
        self.conn.commit()
        self.assertIsNone(last_branch_id(self.conn, WA, pid))
        r = self.send("I want to book an appointment")
        self.assertEqual(self.row_ids(r), ["branch:1", "branch:2", "branch:3"])
        self.assertNotIn("Your last visit", " ".join(row[2] or "" for row in r.replies[0].rows))

    def test_last_visit_is_never_taken_from_someone_elses_booking(self):
        self.patient()
        self.conn.execute("INSERT INTO appointments (patient_name, patient_phone, appt_date, start_time, status, branch_id) "
                          "VALUES ('Other', '9111122223', '2026-09-20', '10:00', 'completed', 3)")
        self.conn.commit()
        self.assertIsNone(last_branch_id(self.conn, WA, None))

    # -- choosing ------------------------------------------------------------------
    def test_naming_the_branch_in_text_works(self):
        for reply, expected in (("Branch C", C), ("C", C), ("branch b please", B), ("b", B)):
            with self.subTest(reply=reply):
                self.setUp()
                self.start_booking()
                self.send(reply)
                self.assertEqual(self.session()["slots"]["branch_id"], expected)

    def test_naming_the_branch_when_asking_to_book_skips_the_question(self):
        self.patient()
        r = self.send("book me at Branch B tomorrow")
        self.assertEqual(self.session()["slots"]["branch_id"], B)
        self.assertEqual(self.session()["slots"]["appt_date"], TOMORROW)
        self.assertEqual(r.replies[0].text.splitlines()[0], "\U0001F4CD Branch B")

    def test_an_unreadable_branch_reply_asks_again_then_escalates(self):
        self.start_booking()
        r = self.send("somewhere nice")
        self.assertEqual(r.replies[0].text, ct.text("ask_branch_retry", "en"))
        self.assertTrue(r.replies[0].rows)
        r = self.send("not sure")
        self.assertEqual(r.escalate, "confusion")

    def test_a_stale_branch_button_outside_a_booking_is_ignored(self):
        r = self.send(choice="branch:2")
        self.assertEqual(r.replies[0].text, ct.text("session_expired", "en"))

    # -- the chosen branch decides everything after ---------------------------------
    def test_free_times_are_only_the_chosen_branchs_doctor_hours(self):
        self.start_booking()
        self.send(choice="branch:3")                  # Branch C: 09:00-12:00
        r = self.send("tomorrow")
        times = [row[0].split("T")[1] for row in r.replies[0].rows]
        self.assertTrue(times and all("09:00" <= t < "12:00" for t in times), times)
        self.assertEqual(r.replies[0].text.splitlines()[0], "\U0001F4CD Branch C")

    def test_another_branchs_evening_hours_are_not_available_at_this_one(self):
        self.start_booking()
        self.send(choice="branch:2")                  # Branch B: 10:00-14:00 (A has evenings)
        self.send("tomorrow")
        r = self.send("5 pm")
        self.assertEqual(r.replies[0].text.splitlines()[1], ct.text("time_closed", "en", hours="10:00 AM-2:00 PM"))

    def test_the_summary_names_the_branch_doctor_and_address(self):
        self.start_booking()
        self.send(choice="branch:2")
        self.send("tomorrow")
        r = self.send("11 am")
        text = r.replies[0].text
        self.assertIn("Branch: Branch B", text)
        self.assertIn("Doctor: Dr. Rao", text)
        self.assertIn("\U0001F4CD Sector 56, Gurugram", text)
        self.assertEqual(self.session()["step"], "confirm")

    def test_changing_the_branch_at_the_summary_re_checks_the_time(self):
        self.start_booking()
        self.send(choice="branch:2")
        self.send("tomorrow")
        self.send("1 pm")                               # fine at B (10-14), not at C (09-12)
        r = self.send("Branch C")
        self.assertEqual(self.session()["slots"]["branch_id"], C)
        self.assertNotIn("start_time", self.session()["slots"])
        self.assertEqual(r.replies[0].text.splitlines()[0], "\U0001F4CD Branch C")     # times offered again, at C

    def test_tapping_another_branch_mid_booking_switches_it(self):
        self.start_booking()
        self.send(choice="branch:2")
        self.send("tomorrow")
        self.send(choice="branch:3")
        self.assertEqual(self.session()["slots"]["branch_id"], C)

    def test_a_branch_with_no_free_times_sends_the_patient_to_the_others(self):
        booking_blocks.add_block(self.conn, "2026-10-05", "2026-11-30", None, None, "Renovation", branch_id=B)
        self.start_booking()
        r = self.send(choice="branch:2")
        self.assertEqual(r.replies[0].text, ct.text("branch_no_slots", "en", branch="Branch B", days=cv.BOOKING_HORIZON_DAYS))
        self.assertEqual(self.row_ids(r), ["branch:1", "branch:3"])                    # B is no longer listed
        self.assertNotIn("branch_id", self.session()["slots"])

    def test_every_branch_full_goes_to_a_person(self):
        for branch in (A, B, C):
            booking_blocks.add_block(self.conn, "2026-10-05", "2026-11-30", None, None, "Closed", branch_id=branch)
        self.start_booking()
        r = self.send(choice="branch:2")
        r = self.send(choice="branch:1")
        r = self.send(choice="branch:3")
        self.assertEqual(r.escalate, "no_availability")

    # -- holds are per branch --------------------------------------------------------
    def test_a_hold_at_one_branch_does_not_hide_the_same_time_at_another(self):
        cv.create_hold(self.conn, WA2, TOMORROW, "10:00", NOW, branch_id=B)
        free = lambda branch, wa: cv.free_times(self.conn, TOMORROW, NOW, for_wa_id=wa, branch_id=branch)
        self.assertNotIn("10:00", free(B, WA))      # held for someone else at B
        self.assertIn("10:00", free(B, WA2))        # the holder still sees their own time
        self.assertIn("10:00", free(A, WA))         # not held at A

    def test_an_old_hold_with_no_branch_belongs_to_the_default_branch(self):
        self.conn.execute("INSERT INTO slot_holds (wa_id, appt_date, start_time, created_at, expires_at) VALUES "
                          "(?, ?, '10:00', '2026-10-05 09:00:00', '2026-10-05 23:00:00')", (WA2, TOMORROW))
        self.conn.commit()
        self.assertNotIn("10:00", cv.free_times(self.conn, TOMORROW, NOW, for_wa_id=WA, branch_id=A))
        self.assertIn("10:00", cv.free_times(self.conn, TOMORROW, NOW, for_wa_id=WA, branch_id=B))

    # -- hand-off ----------------------------------------------------------------------
    def test_the_request_for_staff_carries_the_branch_and_holds_that_branchs_slot(self):
        self.start_booking()
        self.send(choice="branch:2")
        self.send("tomorrow")
        self.send("11 am")
        r = self.send(choice="confirm:yes")
        self.assertEqual(r.handoff.slots["branch_id"], B)
        self.assertIn("Branch B", r.handoff.note)
        hold = self.conn.execute("SELECT * FROM slot_holds").fetchone()
        self.assertEqual((hold["branch_id"], hold["start_time"]), (B, "11:00"))

    # -- reschedule and cancel stay with the appointment's branch --------------------------
    def appointment_at(self, branch, start="11:00", day=TOMORROW):
        pid = self.patient()
        cur = self.conn.execute("INSERT INTO appointments (patient_id, appt_date, start_time, status, branch_id) VALUES (?, ?, ?, 'booked', ?)",
                                (pid, day, start, branch))
        self.conn.commit()
        return cur.lastrowid

    def test_a_reschedule_is_offered_only_the_appointments_own_branch(self):
        self.appointment_at(C, "10:00")
        r = self.send("reschedule my appointment")
        self.assertEqual(self.session()["slots"]["branch_id"], C)
        r = self.send("Friday")
        times = [row[0].split("T")[1] for row in r.replies[0].rows]
        self.assertTrue(times and all("09:00" <= t < "12:00" for t in times), times)
        self.assertEqual(r.replies[0].text.splitlines()[0], "\U0001F4CD Branch C")

    def test_the_reschedule_summary_names_the_branch_and_does_not_ask_for_one(self):
        self.appointment_at(B, "11:00")
        self.send("reschedule my appointment")
        self.send("Friday")
        r = self.send("12 pm")
        self.assertIn("Branch: Branch B", r.replies[0].text)
        self.assertIn("Doctor: Dr. Rao", r.replies[0].text)
        r = self.send(choice="confirm:yes")
        self.assertNotIn("branch_id", r.handoff.slots)               # the appointment keeps its own branch

    def test_the_cancel_question_names_the_branch(self):
        self.appointment_at(C, "10:00")
        r = self.send("cancel my appointment")
        self.assertIn("Branch: Branch C", r.replies[0].text)
        self.assertIn("Doctor: Dr. Iyer", r.replies[0].text)

    def test_choosing_between_appointments_shows_each_ones_branch(self):
        self.appointment_at(B, "11:00")
        self.appointment_at(C, "10:00", day=FRI)
        r = self.send("cancel my appointment")
        self.assertEqual([row[2] for row in r.replies[0].rows], ["Branch B", "Branch C"])

    # -- languages --------------------------------------------------------------------------
    def test_the_branch_questions_work_in_every_language(self):
        for lang, say in SAY.items():
            with self.subTest(lang=lang):
                self.setUp()
                wa = "9198000000{}".format(len(lang))
                self.patient(phone=wa[-10:])
                r = self.send(say["book"], wa=wa)
                self.assertEqual(r.replies[0].text, ct.text("ask_branch", lang))
                self.assertEqual(r.replies[0].list_button, ct.button("choose_branch", lang))
                r = self.send("122012", wa=wa)
                self.assertEqual(r.replies[0].text, ct.text("ask_branch_nearest", lang, pin="122012"))
                self.assertTrue(r.replies[0].rows[0][2].startswith(ct.row_note("very close", lang)), r.replies[0].rows[0][2])
                r = self.send(choice="branch:2", wa=wa)
                self.assertTrue(r.replies[0].text.startswith("\U0001F4CD Branch B"))


class BranchTemplatesFit(unittest.TestCase):
    def test_every_language_has_every_new_text_and_the_titles_fit(self):
        for key in ("ask_branch", "ask_branch_nearest", "ask_branch_retry", "branch_no_slots", "where_branch", "where_doctor"):
            for lang in ct.LANGUAGES:
                self.assertTrue(ct.MSG[key][lang].strip(), (key, lang))
        for lang in ct.LANGUAGES:
            self.assertLessEqual(len(ct.button("choose_branch", lang)), 20)
            for note in ct.ROW_NOTE:
                self.assertTrue(ct.row_note(note, lang))


class BranchMatching(unittest.TestCase):
    CANDIDATES = [{"id": 1, "code": "A", "name": "Branch A"}, {"id": 2, "code": "B", "name": "Branch B"},
                  {"id": 3, "code": "C", "name": "Sector 56 Clinic"}]

    def test_names_and_codes(self):
        match = lambda text, **kw: (cv.match_branch(text, self.CANDIDATES, **kw) or {}).get("id")
        self.assertEqual(match("Branch B"), 2)
        self.assertEqual(match("book at branch a tomorrow"), 1)
        self.assertEqual(match("the sector 56 clinic please"), 3)
        self.assertEqual(match("B", bare_code=True), 2)
        self.assertIsNone(match("B"))                              # a lone letter only counts when answering the question
        self.assertIsNone(match("I want a booking", bare_code=True))
        self.assertIsNone(match("branch a or branch b"))           # two named: ambiguous
        self.assertIsNone(match(""))

    def test_pin_codes(self):
        self.assertEqual(cv.find_pin("my pin is 122019 thanks"), "122019")
        self.assertIsNone(cv.find_pin("call 9876543210"))
        self.assertIsNone(cv.find_pin("12201"))


class BranchAutoBooking(AutoCase):
    """Through the real webhook: the automatic path books at the chosen branch."""

    def setUp(self):
        super().setUp()
        add_branches(self.conn)

    def book_at(self, branch_tap, time="11 am", name="Sunita Devi"):
        self.say("I want to book an appointment")
        self.say(name)
        self.tap(branch_tap, "Branch", kind="list_reply")
        self.say("tomorrow")
        self.say(time)
        self.tap("confirm:yes", "Confirm request")

    def test_the_appointment_is_written_at_the_chosen_branch_with_its_doctor(self):
        self.book_at("branch:2")
        row = self.rows("SELECT * FROM appointments")[0]
        self.assertEqual((row["branch_id"], row["start_time"]), (B, "11:00"))
        self.assertEqual(branches.doctor_label(self.conn, row["doctor_id"]), "Dr. Rao")
        self.assertEqual(self.autos()[0]["status"], "confirmed")

    def test_the_confirmation_message_names_the_branch_doctor_and_address(self):
        self.book_at("branch:2")
        body = self.rows("SELECT body FROM notifications WHERE event = 'booking_confirmed'")[0]["body"]
        self.assertIn("B-T01", body)
        self.assertIn("\U0001F4CD Branch B, Sector 56, Gurugram", body)
        self.assertIn("https://maps.example/b", body)
        self.assertIn("Dr. Rao", body)

    def test_the_same_time_can_be_booked_at_two_branches(self):
        self.book_at("branch:2", time="11 am", name="Sunita Devi")
        self.say("I want to book an appointment", WA2)
        self.say("Meena", WA2)
        self.tap("branch:3", "Branch C", WA2, kind="list_reply")
        self.say("tomorrow", WA2)
        self.say("11 am", WA2)
        self.tap("confirm:yes", "Confirm request", WA2)
        self.assertEqual(sorted((r["branch_id"], r["start_time"]) for r in self.rows("SELECT * FROM appointments")),
                         [(B, "11:00"), (C, "11:00")])

    def test_a_reschedule_through_the_automatic_path_keeps_the_branch(self):
        self.book_at("branch:3", time="10 am")
        self.say("reschedule my appointment")
        self.say("Friday")
        self.say("11 am")
        self.tap("confirm:yes", "Confirm request")
        row = self.rows("SELECT * FROM appointments")[0]
        self.assertEqual((row["branch_id"], row["appt_date"], row["start_time"]), (C, "2026-10-09", "11:00"))
        self.assertEqual(branches.doctor_label(self.conn, row["doctor_id"]), "Dr. Iyer")

    def test_policy_refuses_a_time_outside_the_branchs_doctor_hours(self):
        from datetime import datetime
        now = datetime(2026, 10, 5, 10, 0)
        slots = {"appt_date": "2026-10-06", "start_time": "13:00", "patient_name": "X"}
        self.assertTrue(auto_policy.evaluate(self.conn, "book_appointment", dict(slots, branch_id=B), WA, now).auto)
        refused = auto_policy.evaluate(self.conn, "book_appointment", dict(slots, branch_id=C), WA, now)
        self.assertEqual((refused.auto, refused.code), (False, "outside_hours"))
        self.assertIn("doctor hours", refused.reason)
        # A's afternoon gap is also refused.
        gap = auto_policy.evaluate(self.conn, "book_appointment", dict(slots, branch_id=A), WA, now)
        self.assertEqual((gap.auto, gap.code), (False, "outside_hours"))


if __name__ == "__main__":
    unittest.main()
