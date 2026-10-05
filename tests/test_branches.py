import sqlite3
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import branches, core, db, intents, scheduling, token_queue
from clinic.branches import BranchError

SCHEMA = (Path(__file__).resolve().parents[1] / "clinic" / "schema.sql").read_text()


def monday_on_or_after(day):
    return day + timedelta(days=(7 - day.weekday()) % 7)


class BranchTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.addCleanup(self.conn.close)
        self.monday = monday_on_or_after(date.today() + timedelta(days=1))
        self.iso = self.monday.isoformat()


class SingleBranchCompatibilityTests(BranchTestCase):
    """A database that only has Branch A behaves exactly as the app did before branches."""

    def test_branch_a_and_its_doctor_exist_with_todays_hours(self):
        self.assertEqual([b["code"] for b in branches.list_branches(self.conn)], ["A"])
        self.assertEqual(branches.hours_summary(self.conn, None, self.iso), "09:00-13:00, 16:00-20:00")
        sunday = (self.monday + timedelta(days=6)).isoformat()
        self.assertEqual(branches.hours_summary(self.conn, None, sunday), "09:00-13:00, 16:00-20:00")  # as before: every day

    def test_slots_match_the_old_clinic_hours(self):
        slots = scheduling.generate_slots(self.conn, self.iso)
        self.assertEqual(slots[0], "09:00")
        self.assertEqual(slots[-1], "19:30")
        self.assertNotIn("13:00", slots)
        self.assertEqual(len(slots), 16)
        self.assertEqual(slots, scheduling.slot_grid())

    def test_tokens_keep_the_plain_format_with_one_branch(self):
        self.conn.execute("INSERT INTO appointments (patient_name, appt_date, start_time, status) VALUES ('X', ?, '09:00', 'booked')", (self.iso,))
        self.assertEqual(token_queue.day_queue(self.conn, self.iso)[0]["token_label"], "T-01")


class BranchModelTests(BranchTestCase):
    def test_add_and_edit_a_branch(self):
        branch = branches.add_branch(self.conn, "b", "Branch B", pin_code="122011")
        self.assertEqual(branches.get_branch(self.conn, branch)["code"], "B")
        branches.update_branch(self.conn, branch, name="Sector 56", address="Somewhere", pin_code="122002")
        updated = branches.get_branch(self.conn, branch)
        self.assertEqual((updated["name"], updated["pin_code"]), ("Sector 56", "122002"))

    def test_bad_input_is_refused_with_a_readable_reason(self):
        for kwargs in ({"code": "", "name": "x"}, {"code": "A", "name": "Dup"}, {"code": "TOOLONGCODE", "name": "x"},
                       {"code": "Z", "name": "x", "pin_code": "12"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(BranchError):
                    branches.add_branch(self.conn, **kwargs)

    def test_a_branch_with_upcoming_appointments_cannot_be_retired(self):
        b = branches.add_branch(self.conn, "B", "Branch B")
        self.conn.execute("INSERT INTO appointments (patient_name, appt_date, start_time, status, branch_id) VALUES ('X', ?, '09:00', 'booked', ?)",
                          ((date.today() + timedelta(days=2)).isoformat(), b))
        with self.assertRaises(BranchError) as raised:
            branches.deactivate_branch(self.conn, b)
        self.assertIn("upcoming", str(raised.exception))

    def test_the_only_branch_cannot_be_retired(self):
        with self.assertRaises(BranchError):
            branches.deactivate_branch(self.conn, 1)

    def test_default_branch_follows_the_setting_when_it_is_valid(self):
        b = branches.add_branch(self.conn, "B", "Branch B")
        self.assertEqual(branches.default_branch_id(self.conn), 1)
        branches.set_default_branch(self.conn, b)
        self.assertEqual(branches.default_branch_id(self.conn), b)
        self.assertEqual(branches.resolve(self.conn, None), b)
        self.assertEqual(branches.resolve(self.conn, 1), 1)


class ScheduleRuleTests(BranchTestCase):
    def setUp(self):
        super().setUp()
        self.b = branches.add_branch(self.conn, "B", "Branch B", pin_code="122011")
        self.c = branches.add_branch(self.conn, "C", "Branch C", pin_code="122018")
        self.rao = branches.add_doctor(self.conn, "Dr. Rao")
        self.iyer = branches.add_doctor(self.conn, "Dr. Iyer")

    def test_one_doctor_per_branch_at_a_time(self):
        branches.add_schedule(self.conn, self.rao, self.b, 0, "09:00", "13:00")
        with self.assertRaises(BranchError) as raised:
            branches.add_schedule(self.conn, self.iyer, self.b, 0, "12:00", "14:00")
        self.assertIn("only one doctor per branch", str(raised.exception))
        branches.add_schedule(self.conn, self.iyer, self.b, 0, "13:00", "17:00")      # back to back is fine
        branches.add_schedule(self.conn, self.iyer, self.b, 1, "09:00", "13:00")      # another day is fine

    def test_a_doctor_cannot_be_at_two_branches_at_once(self):
        branches.add_schedule(self.conn, self.rao, self.b, 0, "09:00", "13:00")
        with self.assertRaises(BranchError) as raised:
            branches.add_schedule(self.conn, self.rao, self.c, 0, "11:00", "15:00")
        self.assertIn("two branches at once", str(raised.exception))
        branches.add_schedule(self.conn, self.rao, self.c, 0, "13:00", "17:00")       # different hours
        branches.add_schedule(self.conn, self.rao, self.c, 1, "09:00", "13:00")       # different day

    def test_a_doctor_can_work_at_more_than_one_branch(self):
        branches.add_schedule(self.conn, self.rao, self.b, 0, "09:00", "13:00")
        branches.add_schedule(self.conn, self.rao, self.c, 1, "09:00", "13:00")
        monday, tuesday = self.iso, (self.monday + timedelta(days=1)).isoformat()
        self.assertEqual(branches.doctor_at(self.conn, self.b, monday, "10:00"), self.rao)
        self.assertEqual(branches.doctor_at(self.conn, self.c, tuesday, "10:00"), self.rao)
        self.assertIsNone(branches.doctor_at(self.conn, self.c, monday, "10:00"))

    def test_bad_times_are_refused(self):
        for start, end in (("13:00", "09:00"), ("9", "13:00"), ("09:00", "09:00"), ("25:00", "26:00")):
            with self.subTest(start=start, end=end):
                with self.assertRaises(BranchError):
                    branches.add_schedule(self.conn, self.rao, self.b, 0, start, end)

    def test_date_limited_schedules_only_apply_inside_their_dates(self):
        branches.add_schedule(self.conn, self.rao, self.b, 0, "09:00", "13:00", valid_from=self.iso, valid_to=self.iso)
        self.assertEqual(branches.doctor_at(self.conn, self.b, self.iso, "10:00"), self.rao)
        next_monday = (self.monday + timedelta(days=7)).isoformat()
        self.assertIsNone(branches.doctor_at(self.conn, self.b, next_monday, "10:00"))


class SlotsPerBranchTests(BranchTestCase):
    def setUp(self):
        super().setUp()
        self.b = branches.add_branch(self.conn, "B", "Branch B", pin_code="122011")
        self.rao = branches.add_doctor(self.conn, "Dr. Rao")
        branches.add_schedule(self.conn, self.rao, self.b, 0, "10:00", "12:00")

    def book(self, branch, time, status="booked"):
        self.conn.execute("INSERT INTO appointments (patient_name, appt_date, start_time, status, branch_id) VALUES ('P', ?, ?, ?, ?)",
                          (self.iso, time, status, branch))

    def test_each_branch_offers_its_own_doctors_hours(self):
        self.assertEqual(scheduling.generate_slots(self.conn, self.iso, branch_id=self.b), ["10:00", "10:30", "11:00", "11:30"])
        self.assertEqual(scheduling.generate_slots(self.conn, self.iso, branch_id=1)[0], "09:00")

    def test_a_branch_with_no_doctor_that_day_has_no_slots(self):
        tuesday = (self.monday + timedelta(days=1)).isoformat()
        self.assertEqual(scheduling.generate_slots(self.conn, tuesday, branch_id=self.b), [])

    def test_a_booking_blocks_only_its_own_branch(self):
        self.book(self.b, "10:00")
        self.assertNotIn("10:00", scheduling.generate_slots(self.conn, self.iso, branch_id=self.b))
        self.assertIn("10:00", scheduling.generate_slots(self.conn, self.iso, branch_id=1))

    def test_the_same_time_can_be_booked_at_two_branches(self):
        self.assertTrue(scheduling.is_slot_free(self.conn, self.iso, "10:00", 30, branch_id=self.b))
        self.book(1, "10:00")
        self.assertTrue(scheduling.is_slot_free(self.conn, self.iso, "10:00", 30, branch_id=self.b))
        self.assertFalse(scheduling.is_slot_free(self.conn, self.iso, "10:00", 30, branch_id=1))

    def test_a_closed_branch_has_no_slots_and_reopens(self):
        branches.set_status(self.conn, self.b, "closed", reason="Renovation", message="Closed for renovation")
        self.assertEqual(scheduling.generate_slots(self.conn, self.iso, branch_id=self.b), [])
        branches.set_status(self.conn, self.b, "open")
        self.assertTrue(scheduling.generate_slots(self.conn, self.iso, branch_id=self.b))

    def test_blocks_can_apply_to_one_branch_or_all(self):
        self.conn.execute("INSERT INTO booking_blocks (start_date, end_date, reason, branch_id) VALUES (?, ?, 'Renovation', ?)", (self.iso, self.iso, self.b))
        self.assertEqual(scheduling.generate_slots(self.conn, self.iso, branch_id=self.b), [])
        self.assertTrue(scheduling.generate_slots(self.conn, self.iso, branch_id=1))           # A unaffected
        self.conn.execute("INSERT INTO booking_blocks (start_date, end_date, reason) VALUES (?, ?, 'Holiday')", (self.iso, self.iso))
        self.assertEqual(scheduling.generate_slots(self.conn, self.iso, branch_id=1), [])       # brand-wide

    def test_a_doctors_leave_blocks_that_doctors_slots_only(self):
        self.conn.execute("INSERT INTO booking_blocks (start_date, end_date, reason, doctor_id) VALUES (?, ?, 'Leave', ?)", (self.iso, self.iso, self.rao))
        self.assertEqual(scheduling.generate_slots(self.conn, self.iso, branch_id=self.b), [])
        self.assertTrue(scheduling.generate_slots(self.conn, self.iso, branch_id=1))           # another doctor's branch

    def test_within_doctor_hours(self):
        self.assertEqual(scheduling.within_doctor_hours(self.conn, self.iso, "10:00", 30, self.b), self.rao)
        self.assertIsNone(scheduling.within_doctor_hours(self.conn, self.iso, "08:00", 30, self.b))
        self.assertIsNone(scheduling.within_doctor_hours(self.conn, self.iso, "11:45", 30, self.b))   # spills past the window


class TokensPerBranchTests(BranchTestCase):
    def setUp(self):
        super().setUp()
        self.b = branches.add_branch(self.conn, "B", "Branch B")
        for name, time, branch in (("A1", "09:00", 1), ("A2", "09:30", 1), ("B1", "09:00", self.b)):
            self.conn.execute("INSERT INTO appointments (patient_name, appt_date, start_time, status, branch_id) VALUES (?, ?, ?, 'booked', ?)",
                              (name, self.iso, time, branch))

    def test_each_branch_has_its_own_numbering_and_labels_carry_the_branch(self):
        a = [(e["name"], e["token_label"]) for e in token_queue.day_queue(self.conn, self.iso, 1)]
        b = [(e["name"], e["token_label"]) for e in token_queue.day_queue(self.conn, self.iso, self.b)]
        self.assertEqual(a, [("A1", "A-T01"), ("A2", "A-T02")])
        self.assertEqual(b, [("B1", "B-T01")])

    def test_a_cancellation_renumbers_only_its_own_branch(self):
        self.conn.execute("UPDATE appointments SET status = 'cancelled' WHERE patient_name = 'A1'")
        self.assertEqual([e["token_label"] for e in token_queue.day_queue(self.conn, self.iso, 1)], ["A-T01"])
        self.assertEqual([e["token_label"] for e in token_queue.day_queue(self.conn, self.iso, self.b)], ["B-T01"])

    def test_one_patient_in_consultation_per_branch(self):
        ids = {r["patient_name"]: r["id"] for r in self.conn.execute("SELECT id, patient_name FROM appointments")}
        token_queue.check_in(self.conn, ids["A1"])
        token_queue.check_in(self.conn, ids["B1"])
        token_queue.start_consultation(self.conn, ids["A1"])
        token_queue.start_consultation(self.conn, ids["B1"])        # a different branch: allowed
        token_queue.check_in(self.conn, ids["A2"])
        with self.assertRaises(token_queue.QueueActionError):
            token_queue.start_consultation(self.conn, ids["A2"])    # same branch: refused

    def test_queue_entry_finds_the_right_branch_queue(self):
        b1 = self.conn.execute("SELECT id FROM appointments WHERE patient_name = 'B1'").fetchone()[0]
        self.assertEqual(token_queue.queue_entry(self.conn, b1)["token_label"], "B-T01")
        self.assertEqual(token_queue.queue_snapshot(self.conn, self.iso, self.b)["branch"], "Branch B")


class WriteHandlerTests(BranchTestCase):
    def setUp(self):
        super().setUp()
        self.b = branches.add_branch(self.conn, "B", "Branch B", pin_code="122011")
        self.rao = branches.add_doctor(self.conn, "Dr. Rao")
        branches.add_schedule(self.conn, self.rao, self.b, 0, "10:00", "12:00")
        self.handlers = {"book_appointment": intents.book_appointment, "reschedule_appointment": intents.reschedule_appointment,
                         "cancel_appointment": intents.cancel_appointment}

    def run_intent(self, intent, slots):
        pid = core.propose(self.conn, intent, slots, source_text="test")
        return core.confirm(self.conn, pid, self.handlers)

    def appt(self, appointment_id):
        return dict(self.conn.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,)).fetchone())

    def test_a_booking_records_the_branch_and_the_doctor_on_duty(self):
        _, appt_id = self.run_intent("book_appointment", {"patient_name": "Pt", "patient_phone": "9876500001",
                                                          "appt_date": self.iso, "start_time": "10:30", "branch_id": self.b})
        row = self.appt(appt_id)
        self.assertEqual((row["branch_id"], row["doctor_id"]), (self.b, self.rao))

    def test_no_branch_means_the_default_branch_as_before(self):
        _, appt_id = self.run_intent("book_appointment", {"patient_name": "Pt", "patient_phone": "9876500001",
                                                          "appt_date": self.iso, "start_time": "15:00"})
        self.assertEqual(self.appt(appt_id)["branch_id"], 1)

    def test_a_branch_with_no_doctor_then_is_refused_but_can_be_overridden(self):
        slots = {"patient_name": "Pt", "patient_phone": "9876500001", "appt_date": self.iso, "start_time": "08:00", "branch_id": self.b}
        with self.assertRaises(scheduling.SlotBlockedError) as raised:
            self.run_intent("book_appointment", slots)
        self.assertIn("no doctor on duty", str(raised.exception))
        _, appt_id = self.run_intent("book_appointment", dict(slots, override_block=True))
        self.assertIsNone(self.appt(appt_id)["doctor_id"])

    def test_double_booking_is_per_branch(self):
        base = {"patient_name": "Pt", "patient_phone": "9876500001", "appt_date": self.iso, "start_time": "10:00"}
        self.run_intent("book_appointment", dict(base, branch_id=self.b))
        with self.assertRaises(scheduling.SlotConflictError):
            self.run_intent("book_appointment", dict(base, branch_id=self.b))
        self.run_intent("book_appointment", dict(base, branch_id=1))      # same time at Branch A is fine

    def test_reschedule_can_move_an_appointment_to_another_branch(self):
        _, appt_id = self.run_intent("book_appointment", {"patient_name": "Pt", "patient_phone": "9876500001",
                                                          "appt_date": self.iso, "start_time": "09:00", "branch_id": 1})
        self.run_intent("reschedule_appointment", {"appointment_id": appt_id, "appt_date": self.iso, "start_time": "10:00",
                                                   "branch_id": self.b})
        row = self.appt(appt_id)
        self.assertEqual((row["branch_id"], row["start_time"], row["doctor_id"]), (self.b, "10:00", self.rao))

    def test_reschedule_without_a_branch_keeps_the_appointments_branch(self):
        _, appt_id = self.run_intent("book_appointment", {"patient_name": "Pt", "patient_phone": "9876500001",
                                                          "appt_date": self.iso, "start_time": "10:00", "branch_id": self.b})
        self.run_intent("reschedule_appointment", {"appointment_id": appt_id, "appt_date": self.iso, "start_time": "10:30"})
        self.assertEqual(self.appt(appt_id)["branch_id"], self.b)


class NearestBranchTests(BranchTestCase):
    def setUp(self):
        super().setUp()
        branches.add_branch(self.conn, "B", "Branch B", pin_code="122011")
        branches.add_branch(self.conn, "C", "Branch C", pin_code="110001")
        self.conn.execute("UPDATE branches SET pin_code = '122001' WHERE id = 1")

    def codes(self, pin):
        return [b["code"] for b in branches.nearest_branches(self.conn, pin)]

    def test_same_pin_first_then_same_region_then_the_rest(self):
        self.assertEqual(self.codes("122001"), ["A", "B", "C"])
        self.assertEqual(self.codes("122011"), ["B", "A", "C"])
        self.assertEqual(self.codes("110002")[0], "C")

    def test_a_longer_shared_prefix_beats_a_nearer_number(self):
        # 122008 shares five digits with 122001 (the same delivery area) but only three with 122011
        self.assertEqual(self.codes("122008")[:2], ["A", "B"])

    def test_numeric_closeness_breaks_ties_between_equal_prefixes(self):
        self.conn.execute("UPDATE branches SET pin_code = '122041' WHERE code = 'B'")      # A=122001, B=122041: both share 3 digits
        self.assertEqual(self.codes("122030")[:2], ["B", "A"])

    def test_a_missing_or_bad_pin_keeps_the_fixed_order(self):
        self.assertEqual(self.codes(None), ["A", "B", "C"])
        self.assertEqual(self.codes("12"), ["A", "B", "C"])

    def test_branches_without_a_pin_go_last(self):
        self.conn.execute("UPDATE branches SET pin_code = NULL WHERE code = 'B'")
        self.assertEqual(self.codes("122001"), ["A", "C", "B"])

    def test_the_phrase_says_how_near(self):
        nearest = branches.nearest_branches(self.conn, "122001")
        self.assertEqual(nearest[0]["near"], "your PIN code")
        self.assertEqual(nearest[0]["rank"], 1)


class SeedTests(unittest.TestCase):
    def test_a_real_database_gets_branches_b_and_c_once(self):
        conn = db.connect(":memory:")
        self.assertEqual([b["code"] for b in branches.list_branches(conn)], ["A"])     # connect() alone never seeds
        self.assertTrue(branches.ensure_seed(conn))
        self.assertEqual([b["code"] for b in branches.list_branches(conn)], ["A", "B", "C"])
        self.assertFalse(branches.ensure_seed(conn))                                   # once only
        self.assertEqual(len(branches.list_branches(conn)), 3)

    def test_every_branch_is_open_every_day_with_one_doctor_and_a_doctor_is_shared(self):
        conn = db.connect(":memory:")
        branches.ensure_seed(conn)
        monday = monday_on_or_after(date.today())
        for offset in range(6):                       # Mon-Sat
            day = (monday + timedelta(days=offset)).isoformat()
            for branch in branches.list_branches(conn):
                self.assertNotEqual(branches.hours_summary(conn, branch["id"], day), "closed", (branch["code"], day))
        sharing = {r["doctor_id"] for r in branches.list_schedule(conn, branch_id=2)} & {r["doctor_id"] for r in branches.list_schedule(conn, branch_id=3)}
        self.assertTrue(sharing, "one doctor should work at both Branch B and Branch C")

    def test_a_database_that_already_has_several_branches_is_left_alone(self):
        conn = db.connect(":memory:")
        branches.add_branch(conn, "B", "My own branch")
        self.assertFalse(branches.ensure_seed(conn))
        self.assertEqual([b["code"] for b in branches.list_branches(conn)], ["A", "B"])


if __name__ == "__main__":
    unittest.main()
