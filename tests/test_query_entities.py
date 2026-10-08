"""The generic read's record types beyond the first five (clinic/query_tool.py): staff, attendance,
branches, doctors, schedules, visits, expenses, reminders, closures, blocks, audit, activity and the
extended follow-ups -- plus sort / limit, the sum / average / min / max aggregates and group_by.

Everything uses a fixed calendar (Wednesday 2026-10-07 is "today"), never the real date. Each column
that must never be readable (visit notes, diagnosis, message bodies, phone numbers in logs, tokens,
paid_to ...) holds a SECRET marker, and the tests assert the marker can not come out of any entity."""
import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_conversation import make_db  # noqa: E402
from tests.test_conversation_branches import add_branches  # noqa: E402

from clinic import branches, query_tool  # noqa: E402
from clinic.query_tool import NotListed, QueryError  # noqa: E402

TODAY = "2026-10-07"            # a Wednesday
NOW = "11:00"
A, B, C = 1, 2, 3
SECRETS = ("SECRETNOTE", "SECRETDIAG", "SECRETBODY", "SECRETPAYEE", "SECRETMETA", "EAABsecrettokenvalue1234567890abcdef", "maps.example")
# phone numbers that sit in log payloads / WhatsApp ids: they must not come out of the log-like entities
LOG_PHONES = ("9000000001", "9000000004", "919000000001", "919000000002", "919000000003")
# column names that must not exist on ANY entity
FORBIDDEN_FIELDS = {"notes", "note", "diagnosis", "body", "paid_to", "maps_url", "wa_id", "payload_json", "payload", "meta_json",
                    "interactive_json", "template_json", "message", "closed_message", "token", "api_key", "raw_text", "slots_json",
                    "error_text", "dedup_key"}


class EntityCase(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        add_branches(self.conn)
        c = self.conn
        branches.update_doctor(c, 1, specialty="General physician")
        branches.update_doctor(c, 2, title="Dr.", specialty="Paediatrics")
        for name, phone, age, registered in (
                ("Amit Sharma", "9000000001", 72, "2026-09-01 10:00:00"), ("Priya Shah", "9000000002", 28, "2026-09-02 10:00:00"),
                ("Ramesh Gupta", "9000000003", 64, "2026-10-01 10:00:00"), ("Seema Rao", "9000000004", 40, "2026-10-07 09:00:00")):
            c.execute("INSERT INTO patients (name, phone, age, registered_at) VALUES (?, ?, ?, ?)", (name, phone, age, registered))
        # staff and attendance
        for name, role, phone, branch in (("Sunita Devi", "Nurse", "9100000001", B), ("Ravi Kumar", "Receptionist", "9100000002", None),
                                          ("Meena Joshi", "Nurse", "9100000003", A), ("Karan Singh", "Compounder", "9100000004", C)):
            c.execute("INSERT INTO staff (name, role, phone, branch_id) VALUES (?, ?, ?, ?)", (name, role, phone, branch))
        for staff, day, status in ((1, TODAY, "present"), (2, TODAY, "absent"), (3, TODAY, "half_day"),
                                   (2, "2026-10-06", "present"), (1, "2026-10-06", "absent"), (3, "2026-09-30", "leave"),
                                   (2, "2026-10-01", "absent")):
            c.execute("INSERT INTO attendance (staff_id, attendance_date, status) VALUES (?, ?, ?)", (staff, day, status))
        # branches: C is switched off, A has a whole-day booking block on Thursday 8 Oct
        branches.set_status(c, C, "closed", "Renovation work")
        c.execute("INSERT INTO booking_blocks (start_date, end_date, reason, branch_id) VALUES ('2026-10-08', '2026-10-08', 'Maintenance', ?)", (A,))
        c.execute("INSERT INTO booking_blocks (start_date, end_date, start_time, end_time, reason, branch_id) "
                  "VALUES ('2026-10-07', '2026-10-07', '13:00', '14:00', 'Staff meeting', ?)", (B,))
        c.execute("INSERT INTO booking_blocks (start_date, end_date, reason, doctor_id) VALUES ('2026-10-10', '2026-10-11', 'Dr. Rao conference', 2)")
        c.execute("INSERT INTO booking_blocks (start_date, end_date, reason) VALUES ('2026-10-20', '2026-10-20', 'Diwali')")
        c.execute("INSERT INTO booking_blocks (start_date, end_date, reason, active) VALUES ('2026-10-14', '2026-10-14', 'Removed block', 0)")
        # visits (the notes column holds a secret) and expenses (so does paid_to)
        for patient, day, paise in ((1, "2026-10-01", 50000), (1, "2026-10-05", 30000), (2, "2026-10-05", 45050),
                                    (3, "2026-09-20", 100000), (4, "2026-10-07", 25000)):
            c.execute("INSERT INTO visits (patient_id, visit_date, fee_paise, notes) VALUES (?, ?, ?, 'SECRETNOTE chest pain')",
                      (patient, day, paise))
        for day, text, paise in (("2026-10-01", "Clinic rent", 5000000), ("2026-10-02", "electricity", 120000),
                                 ("2026-10-03", "Electricity bill", 80000), ("2026-09-15", "stationery", 25050)):
            c.execute("INSERT INTO expenses (expense_date, description, amount_paise, paid_to) VALUES (?, ?, ?, 'SECRETPAYEE')", (day, text, paise))
        # appointments (with doctors and a secret note) and follow-ups (with a secret diagnosis)
        appts = ((1, A, 1, "2026-10-07", "10:00", "booked", 30), (2, B, 2, "2026-10-07", "11:00", "booked", 30),
                 (3, B, 2, "2026-10-07", "12:00", "completed", 45), (1, A, 1, "2026-10-08", "09:30", "confirmed", 30),
                 (4, A, 1, "2026-10-08", "10:30", "cancelled", 30), (2, C, 3, "2026-10-14", "09:30", "booked", 30),
                 (3, B, 2, "2026-09-30", "10:30", "completed", 60))
        for patient, branch, doctor, day, start, status, minutes in appts:
            c.execute("INSERT INTO appointments (patient_id, branch_id, doctor_id, appt_date, start_time, status, duration_minutes, notes) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, 'SECRETNOTE appt')", (patient, branch, doctor, day, start, status, minutes))
        c.execute("INSERT INTO followups (patient_id, due_date, due_time, doctor_id, branch_id, appointment_id, diagnosis, status) "
                  "VALUES (1, '2026-10-08', '09:30', 1, ?, 4, 'SECRETDIAG', 'pending')", (A,))
        c.execute("INSERT INTO followups (patient_id, due_date, due_time, doctor_id, branch_id, appointment_id, diagnosis, status) "
                  "VALUES (3, '2026-10-09', '10:00', 2, ?, 7, 'SECRETDIAG', 'pending')", (B,))
        c.execute("INSERT INTO followups (patient_id, due_date, status) VALUES (2, '2026-10-12', 'pending')")           # an old date-only recall
        c.execute("INSERT INTO followups (patient_id, due_date, status, diagnosis) VALUES (4, '2026-09-01', 'done', 'SECRETDIAG')")
        # reminders: the notifications outbox
        for event, status, appt, wa_id, created, error, key in (
                ("reminder_day_before", "sent", 1, "919000000001", "2026-10-06 12:00:00", None, "k1"),
                ("followup_reminder_2d", "failed", 4, "919000000001", "2026-10-06 12:00:00",
                 "Meta error (#131047) to 919000000001 token EAABsecrettokenvalue1234567890abcdef " + "x" * 200, "k2"),
                ("booking_confirmed", "blocked_no_window", 2, "919000000002", "2026-10-05 12:00:00", None, "k3"),
                ("reminder_morning", "pending", 2, "919000000002", "2026-10-07 12:00:00", None, "k4"),
                ("registered", "sent", None, "919000000003", "2026-10-07 12:00:00", None, "k5"),
                ("conv_reply", "sent", None, "919000000003", "2026-10-07 12:00:00", None, "k6"),
                ("your_turn", "skipped_no_phone", 3, None, "2026-10-07 12:00:00", None, "k7")):
            c.execute("INSERT INTO notifications (appointment_id, wa_id, event, dedup_key, body, status, error, created_at) "
                      "VALUES (?, ?, ?, ?, 'SECRETBODY dear patient', ?, ?, ?)", (appt, wa_id, event, key, status, error, created))
        # closures: B for 12-14 Oct (two patients moved, one cancelled, one move put back), an undone one for A
        c.execute("INSERT INTO closures (branch_id, start_date, end_date, reason, message, status) "
                  "VALUES (?, '2026-10-12', '2026-10-14', 'Doctor on leave', 'SECRETMETA message', 'applied')", (B,))
        c.execute("INSERT INTO closures (branch_id, doctor_id, start_date, end_date, reason, status) "
                  "VALUES (?, 1, '2026-10-01', '2026-10-02', 'Conference', 'undone')", (A,))
        c.execute("INSERT INTO closures (branch_id, start_date, end_date, reason, status) VALUES (?, '2026-09-10', '2026-09-10', 'Power cut', 'applied')", (A,))
        for closure, appt, action, result in ((1, 2, "move", "done"), (1, 3, "move", "done"), (1, 6, "cancel", "done"),
                                              (1, 1, "move", "undone"), (3, 4, "move", "done")):
            c.execute("INSERT INTO closure_moves (closure_id, appointment_id, action, from_date, from_time, result) "
                      "VALUES (?, ?, ?, '2026-10-12', '10:00', ?)", (closure, appt, action, result))
        # audit log and patient activity (payloads and meta hold secrets)
        for logged, intent, entity, payload in (
                ("2026-10-07 12:00:00", "record_visit", "visit", {"patient_id": 1, "visit_date": "2026-10-07", "fee_paise": 50000, "notes": "SECRETNOTE chest pain"}),
                ("2026-10-07 13:00:00", "book_appointment", "appointment", {"patient_name": "Amit Sharma", "patient_phone": "9000000001",
                                                                           "appt_date": "2026-10-08", "start_time": "09:30", "notes": "SECRETNOTE appt"}),
                ("2026-10-06 12:00:00", "followup_diagnosis_edited", "followup", {"diagnosis_old": "SECRETDIAG", "diagnosis_new": "SECRETDIAG two"}),
                ("2026-10-06 13:00:00", "log_expense", "expense", {"expense_date": "2026-10-06", "description": "electricity", "amount_paise": 120000,
                                                                  "paid_to": "SECRETPAYEE"}),
                ("2026-10-05 12:00:00", "register_patient", "patient", {"name": "Seema Rao", "phone": "9000000004", "age": 40}),
                ("2026-10-05 13:00:00", "followup_synced", "followup", {"appointment_id": 4, "changes": {"status": "done", "due_date": "2026-10-09", "diagnosis": "SECRETDIAG"}})):
            c.execute("INSERT INTO audit_log (logged_at, intent, entity_type, entity_id, payload_json) VALUES (?, ?, ?, 1, ?)",
                      (logged, intent, entity, json.dumps(payload)))
        for patient, name, event, source, detail, created in (
                (1, None, "staff_booked", "staff", "Booked 2026-10-08 09:30 at Branch A", "2026-10-07 10:00:00"),
                (1, None, "escalated", "whatsapp-agent", "Sent to staff: call 919000000001 later", "2026-10-07 11:00:00"),
                (None, "Walk In", "auto_booked", "whatsapp-agent", "Booked 2026-10-09 11:00", "2026-10-06 09:00:00"),
                (2, None, "closure_moved", "staff", "Moved out of the closure: 2026-10-12 10:00", "2026-10-05 09:00:00")):
            c.execute("INSERT INTO patient_activity (patient_id, wa_id, patient_name, event, source, detail, created_at, meta_json) "
                      "VALUES (?, '919000000001', ?, ?, ?, ?, ?, '{\"secret\": \"SECRETMETA\"}')", (patient, name, event, source, detail, created))
        c.commit()

    def run_query(self, branch_id=None, today=TODAY, now=NOW, **spec):
        return query_tool.run(self.conn, spec, branch_id=branch_id, today=today, now=now)

    def rows(self, **spec):
        return self.run_query(**spec).rows

    def column(self, column, **spec):
        return [r[column] for r in self.rows(**spec)]

    def count(self, **spec):
        return self.run_query(aggregate="count", **spec).total


class Staff(EntityCase):
    def test_list_count_and_fields(self):
        result = self.run_query(entity="staff")
        self.assertEqual(result.columns, ["name", "role", "phone", "branch"])
        self.assertEqual([r["name"] for r in result.rows], ["Karan Singh", "Meena Joshi", "Ravi Kumar", "Sunita Devi"])
        self.assertEqual(self.count(entity="staff"), 4)
        self.assertEqual(self.rows(entity="staff", fields=["name"])[0], {"name": "Karan Singh"})

    def test_the_nurses_are_found_by_role_and_so_is_a_name(self):
        self.assertEqual(self.column("name", entity="staff", text="nurse"), ["Meena Joshi", "Sunita Devi"])
        self.assertEqual(self.column("role", entity="staff", text="sunita"), ["Nurse"])
        self.assertEqual(self.column("name", entity="staff", patient_name="receptionist"), ["Ravi Kumar"])     # patient_name is the same filter

    def test_who_works_at_a_branch_includes_people_who_work_anywhere(self):
        self.assertEqual(self.column("name", entity="staff", branch_id=B), ["Ravi Kumar", "Sunita Devi"])
        self.assertEqual(self.column("branch", entity="staff", branch_id=B), ["Any", "Branch B"])
        self.assertEqual(self.column("name", entity="staff", branch_id=None)[0], "Karan Singh")

    def test_group_by_role_and_branch(self):
        self.assertEqual(self.rows(entity="staff", group_by="role", order="name"),
                         [{"role": "compounder", "count": 1}, {"role": "nurse", "count": 2}, {"role": "receptionist", "count": 1}])
        by_branch = {r["branch"]: r["count"] for r in self.rows(entity="staff", group_by="branch")}
        self.assertEqual(by_branch, {"Any": 1, "Branch A": 1, "Branch B": 1, "Branch C": 1})


class Attendance(EntityCase):
    def test_whos_absent_today(self):
        rows = self.rows(entity="attendance", date=TODAY, status="absent")
        self.assertEqual([(r["staff"], r["status"]) for r in rows], [("Ravi Kumar", "absent")])
        self.assertEqual(list(rows[0]), ["staff", "role", "date", "status"])

    def test_is_ravi_in_today(self):
        self.assertEqual(self.column("status", entity="attendance", text="Ravi", date=TODAY), ["absent"])
        self.assertEqual(self.rows(entity="attendance", text="Karan", date=TODAY), [])            # not marked today

    def test_a_staff_members_month(self):
        rows = self.rows(entity="attendance", text="ravi", date="2026-10-01", date_to="2026-10-31", order="oldest")
        self.assertEqual([(r["date"], r["status"]) for r in rows], [("2026-10-01", "absent"), ("2026-10-06", "present"), ("2026-10-07", "absent")])

    def test_status_words(self):
        self.assertEqual(self.count(entity="attendance", status="half day"), 1)
        self.assertEqual(self.count(entity="attendance", status="on leave"), 1)
        with self.assertRaises(QueryError):
            query_tool.validate_spec({"entity": "attendance", "status": "sleeping"})

    def test_by_branch_group_by_status_and_staff(self):
        self.assertEqual(self.count(entity="attendance", branch_id=B, date=TODAY), 2)          # Sunita (B) and Ravi (any branch)
        by_status = {r["status"]: r["count"] for r in self.rows(entity="attendance", group_by="status")}
        self.assertEqual(by_status, {"absent": 3, "half day": 1, "leave": 1, "present": 2})
        by_staff = {r["staff"]: r["count"] for r in self.rows(entity="attendance", group_by="staff", status="absent")}
        self.assertEqual(by_staff, {"Ravi Kumar": 2, "Sunita Devi": 1})


class Branches(EntityCase):
    def test_which_branches_are_open_today(self):
        rows = self.rows(entity="branches", status="open", date=TODAY)
        self.assertEqual([r["name"] for r in rows], ["Branch A", "Branch B"])
        closed = self.rows(entity="branches", status="closed", date=TODAY)
        self.assertEqual([(r["name"], r["closed_reason"]) for r in closed], [("Branch C", "Renovation work")])

    def test_a_whole_day_block_closes_a_branch_for_that_day_only(self):
        thursday = self.rows(entity="branches", date="2026-10-08", fields=["name", "status", "closed_reason"])
        self.assertEqual([(r["name"], r["status"]) for r in thursday], [("Branch A", "closed"), ("Branch B", "open"), ("Branch C", "closed")])
        self.assertEqual(thursday[0]["closed_reason"], "Maintenance")
        # the part-day block (13:00-14:00 at B on the 7th) does not close the branch
        self.assertEqual(self.column("status", entity="branches", date=TODAY, branch_id=B), ["open"])

    def test_a_brand_wide_block_closes_every_branch(self):
        self.assertEqual({r["status"] for r in self.rows(entity="branches", date="2026-10-20")}, {"closed"})

    def test_no_doctor_that_weekday_means_closed(self):
        for row in self.conn.execute("SELECT id FROM doctor_schedules WHERE branch_id = ? AND weekday = 6", (B,)).fetchall():
            self.conn.execute("DELETE FROM doctor_schedules WHERE id = ?", (row[0],))
        sunday = self.rows(entity="branches", date="2026-10-11", fields=["name", "status", "closed_reason"])
        self.assertEqual((sunday[1]["name"], sunday[1]["status"], sunday[1]["closed_reason"]), ("Branch B", "closed", "no doctor scheduled that day"))

    def test_why_is_branch_c_closed_and_branch_bs_address(self):
        self.assertEqual(self.column("closed_reason", entity="branches", branch_id=C, date=TODAY), ["Renovation work"])
        row = self.rows(entity="branches", branch_id=B, fields=["name", "address", "pin", "phone"])[0]
        self.assertEqual(row, {"name": "Branch B", "address": "Sector 56, Gurugram", "pin": "122011", "phone": None})

    def test_text_matches_name_or_code_and_one_day_only(self):
        self.assertEqual(self.column("code", entity="branches", text="branch c"), ["C"])
        self.assertEqual(self.column("code", entity="branches", text="branch b"), ["B"])
        with self.assertRaises(QueryError):
            query_tool.validate_spec({"entity": "branches", "date": TODAY, "date_to": "2026-10-09"})

    def test_a_retired_branch_is_not_listed(self):
        self.conn.execute("UPDATE branches SET active = 0 WHERE id = ?", (C,))
        self.assertEqual(self.column("code", entity="branches"), ["A", "B"])

    def test_count_and_group(self):
        self.assertEqual(self.count(entity="branches", status="open", date=TODAY), 2)
        self.assertEqual({r["status"]: r["count"] for r in self.rows(entity="branches", date=TODAY, group_by="status")}, {"closed": 1, "open": 2})


class Doctors(EntityCase):
    def test_list(self):
        rows = self.rows(entity="doctors")
        self.assertEqual([(r["name"], r["title"], r["specialty"]) for r in rows],
                         [("Dr. Mehta", "Dr.", "General physician"), ("Dr. Rao", "Dr.", "Paediatrics"), ("Dr. Iyer", None, None)])
        self.assertEqual(list(rows[0]), ["name", "title", "specialty"])

    def test_text_finds_a_name_or_a_specialty_and_retired_doctors_are_left_out(self):
        self.assertEqual(self.column("name", entity="doctors", text="paed"), ["Dr. Rao"])
        self.assertEqual(self.column("name", entity="doctors", text="rao"), ["Dr. Rao"])
        branches.update_doctor(self.conn, 3, active=False)
        self.assertEqual(self.count(entity="doctors"), 2)

    def test_group_by_specialty(self):
        self.assertEqual({r["specialty"]: r["count"] for r in self.rows(entity="doctors", group_by="specialty")},
                         {"General physician": 1, "Paediatrics": 1, "none given": 1})


class Schedules(EntityCase):
    def test_when_is_dr_rao_at_branch_b(self):
        rows = self.rows(entity="schedules", doctor="Rao", branch_id=B, weekday="monday")
        self.assertEqual(rows, [{"doctor": "Dr. Rao", "branch": "Branch B", "weekday": "Monday", "start_time": "10:00", "end_time": "14:00"}])
        self.assertEqual(self.count(entity="schedules", text="rao"), 7)

    def test_which_doctor_is_at_branch_a_on_monday_and_a_date_becomes_its_weekday(self):
        self.assertEqual(self.column("doctor", entity="schedules", branch_id=A, weekday="Mon"), ["Dr. Mehta", "Dr. Mehta"])
        # 2026-10-12 is a Monday
        self.assertEqual(self.column("weekday", entity="schedules", branch_id=A, date="2026-10-12"), ["Monday", "Monday"])
        self.assertEqual(self.count(entity="schedules", date="2026-10-12"), 4)        # A twice, B once, C once

    def test_a_date_and_a_different_weekday_match_nothing(self):
        self.assertEqual(self.rows(entity="schedules", date="2026-10-12", weekday="tuesday"), [])

    def test_on_duty_now_uses_today_and_the_injected_clock(self):
        on_duty = lambda now, **kw: sorted(self.column("doctor", entity="schedules", date=TODAY, time="now", now=now, **kw))
        self.assertEqual(on_duty("11:00"), ["Dr. Iyer", "Dr. Mehta", "Dr. Rao"])
        self.assertEqual(on_duty("13:30"), ["Dr. Rao"])
        self.assertEqual(on_duty("14:00"), [])                       # the window is [start, end)
        self.assertEqual(on_duty("17:00"), ["Dr. Mehta"])
        self.assertEqual(on_duty("11:00", branch_id=A), ["Dr. Mehta"])
        self.assertEqual(self.column("doctor", entity="schedules", date=TODAY, time="17:00", now="03:00"), ["Dr. Mehta"])   # an explicit time ignores the clock

    def test_a_schedule_with_validity_dates_only_counts_inside_them(self):
        self.conn.execute("UPDATE doctor_schedules SET valid_to = '2026-10-06' WHERE branch_id = ?", (B,))
        self.assertEqual(self.rows(entity="schedules", date=TODAY, time="11:00", text="rao"), [])
        self.assertEqual(len(self.rows(entity="schedules", date="2026-10-05", time="11:00", text="rao")), 1)

    def test_weekday_is_validated(self):
        for bad in ("someday", "mo", 3, "m"):
            with self.subTest(weekday=bad):
                with self.assertRaises(QueryError):
                    query_tool.validate_spec({"entity": "schedules", "weekday": bad})
        self.assertEqual(query_tool.validate_spec({"entity": "schedules", "weekday": "Sun"})["weekday"], "sunday")
        for bad in ("25:00", "noon", "11", "now now"):
            with self.subTest(time=bad):
                with self.assertRaises(QueryError):
                    query_tool.validate_spec({"entity": "schedules", "time": bad})

    def test_group_by_weekday_and_doctor(self):
        by_doctor = {r["doctor"]: r["count"] for r in self.rows(entity="schedules", group_by="doctor")}
        self.assertEqual(by_doctor, {"Dr. Mehta": 14, "Dr. Rao": 7, "Dr. Iyer": 7})
        self.assertEqual([r["weekday"] for r in self.rows(entity="schedules", group_by="weekday")],
                         ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])


class Visits(EntityCase):
    def test_list_is_newest_first_in_rupees_and_has_no_notes(self):
        rows = self.rows(entity="visits")
        self.assertEqual(list(rows[0]), ["patient", "date", "fee_rupees"])
        self.assertEqual([(r["patient"], r["date"], r["fee_rupees"]) for r in rows][:2],
                         [("Seema Rao", "2026-10-07", 250.0), ("Priya Shah", "2026-10-05", 450.5)])
        self.assertEqual(self.column("fee_rupees", entity="visits", patient_name="priya"), [450.5])
        self.assertEqual(self.count(entity="visits", date="2026-10-01", date_to="2026-10-31"), 4)

    def test_when_did_amit_last_visit(self):
        rows = self.rows(entity="visits", patient_name="Amit", order="newest", limit=1)
        self.assertEqual([(r["date"]) for r in rows], ["2026-10-05"])

    def test_notes_cannot_be_asked_for(self):
        for spec in ({"fields": ["notes"]}, {"group_by": "notes"}, {"order": "notes desc"}, {"measure": "notes", "aggregate": "sum"}):
            with self.subTest(spec=spec):
                with self.assertRaises(NotListed):
                    query_tool.validate_spec(dict(spec, entity="visits"))


class Expenses(EntityCase):
    def test_what_did_we_spend_on_electricity(self):
        rows = self.rows(entity="expenses", text="electricity", order="oldest")
        self.assertEqual([(r["date"], r["description"], r["amount_rupees"]) for r in rows],
                         [("2026-10-02", "electricity", 1200.0), ("2026-10-03", "Electricity bill", 800.0)])
        self.assertEqual(list(rows[0]), ["date", "description", "amount_rupees"])

    def test_paid_to_is_not_a_column(self):
        self.assertNotIn("paid_to", query_tool.FIELDS["expenses"])
        with self.assertRaises(NotListed):
            query_tool.validate_spec({"entity": "expenses", "fields": ["paid_to"]})

    def test_the_cashbook_union_is_unchanged(self):
        rows = self.rows(entity="cashbook", date=TODAY)
        self.assertEqual([(r["type"], r["description"], r["amount_rupees"]) for r in rows], [("fee", "Seema Rao", 250.0)])
        self.assertEqual(list(rows[0]), ["date", "type", "description", "amount_rupees"])
        self.assertEqual(self.count(entity="cashbook", kind="expense"), 4)
        self.assertEqual(self.count(entity="cashbook", kind="fee"), 5)


class FollowupsExtended(EntityCase):
    def test_new_fields_and_the_default_stays_pending_only(self):
        rows = self.rows(entity="followups", fields=["patient", "due_date", "time", "doctor", "branch", "has_slot"], order="oldest")
        self.assertEqual([(r["patient"], r["time"], r["doctor"], r["branch"], r["has_slot"]) for r in rows],
                         [("Amit Sharma", "09:30", "Dr. Mehta", "Branch A", "yes"), ("Ramesh Gupta", "10:00", "Dr. Rao", "Branch B", "yes"),
                          ("Priya Shah", None, None, None, "no")])
        self.assertEqual(self.count(entity="followups"), 3)
        self.assertEqual(self.count(entity="followups", status="done"), 1)

    def test_this_week_branch_and_doctor_filters(self):
        self.assertEqual(self.column("patient", entity="followups", date=TODAY, date_to="2026-10-13", order="oldest"),
                         ["Amit Sharma", "Ramesh Gupta", "Priya Shah"])
        self.assertEqual(self.column("patient", entity="followups", branch_id=B), ["Ramesh Gupta"])
        self.assertEqual(self.column("patient", entity="followups", doctor="mehta"), ["Amit Sharma"])
        self.assertEqual(self.column("doctor", entity="followups", patient_name="ramesh"), ["Dr. Rao"])
        self.assertEqual(self.column("patient", entity="followups", text="amit"), ["Amit Sharma"])    # text is the patient filter here

    def test_diagnosis_is_never_exposed(self):
        self.assertNotIn("diagnosis", query_tool.FIELDS["followups"])
        for spec in ({"fields": ["diagnosis"]}, {"group_by": "diagnosis"}, {"order": "diagnosis asc"}):
            with self.assertRaises(NotListed):
                query_tool.validate_spec(dict(spec, entity="followups"))

    def test_group_by_doctor_and_status(self):
        self.assertEqual({r["doctor"]: r["count"] for r in self.rows(entity="followups", group_by="doctor")},
                         {"Dr. Mehta": 1, "Dr. Rao": 1, "No doctor": 1})
        self.assertEqual({r["status"]: r["count"] for r in self.rows(entity="followups", group_by="status", status="done")}, {"done": 1})


class Reminders(EntityCase):
    def test_kinds_and_statuses_are_plain_words(self):
        rows = self.rows(entity="reminders", fields=["patient", "kind", "status"], order="oldest")
        pairs = [(r["patient"], r["kind"], r["status"]) for r in rows]
        self.assertIn(("Amit Sharma", "appointment reminder (day before)", "sent"), pairs)
        self.assertIn(("Priya Shah", "booking confirmation", "blocked: outside the 24-hour window"), pairs)
        self.assertIn(("Ramesh Gupta", "your-turn call", "skipped: no phone number"), pairs)
        self.assertIn(("Priya Shah", "appointment reminder (morning)", "queued"), pairs)
        self.assertNotIn("conv_reply", json.dumps(rows))
        self.assertEqual(len(rows), 6)                      # the conversation agent's replies are not reminders

    def test_a_patient_with_no_appointment_is_found_by_the_number_they_wrote_from(self):
        self.conn.execute("UPDATE patients SET phone = '9000000003' WHERE id = 3")
        self.assertEqual(self.column("kind", entity="reminders", patient_name="ramesh", status="sent"), ["registration welcome"])

    def test_did_amit_get_his_reminder(self):
        rows = self.rows(entity="reminders", patient_name="amit", kind="reminder")
        self.assertEqual([(r["kind"], r["status"]) for r in rows],
                         [("follow-up reminder (2 days before)", "failed"), ("appointment reminder (day before)", "sent")])

    def test_failed_or_blocked(self):
        self.assertEqual(self.count(entity="reminders", status="failed"), 1)
        self.assertEqual(self.count(entity="reminders", status="blocked"), 1)
        self.assertEqual(self.count(entity="reminders", status="queued"), 1)
        self.assertEqual(self.count(entity="reminders", status="skipped"), 1)
        with self.assertRaises(QueryError):
            query_tool.validate_spec({"entity": "reminders", "status": "exploded"})

    def test_kind_words(self):
        self.assertEqual(self.count(entity="reminders", kind="follow-up reminders"), 1)
        self.assertEqual(self.count(entity="reminders", kind="appointment reminder"), 2)
        self.assertEqual(self.count(entity="reminders", kind="booking confirmation"), 1)
        with self.assertRaises(QueryError):
            query_tool.validate_spec({"entity": "reminders", "kind": "ransom note"})

    def test_when_is_a_date_and_dates_filter_in_local_time(self):
        rows = self.rows(entity="reminders", date="2026-10-06", fields=["when"])
        self.assertTrue(rows and all(r["when"].startswith("2026-10-0") for r in rows))
        self.assertEqual(self.count(entity="reminders", date="2026-10-06"), 2)

    def test_the_error_is_short_and_has_no_numbers_or_tokens(self):
        error = self.column("error", entity="reminders", status="failed")[0]
        self.assertLessEqual(len(error), 80)
        self.assertTrue(error.startswith("Meta error"))
        for secret in ("919000000001", "EAABsecrettokenvalue1234567890abcdef"):
            self.assertNotIn(secret, error)
        self.assertIn("[number]", error)

    def test_no_body_phone_or_token_column(self):
        self.assertEqual(set(query_tool.FIELDS["reminders"]), {"patient", "kind", "status", "when", "error"})

    def test_group_by_status_and_kind(self):
        by_status = {r["status"]: r["count"] for r in self.rows(entity="reminders", group_by="status")}
        self.assertEqual(by_status, {"sent": 2, "failed": 1, "blocked: outside the 24-hour window": 1, "queued": 1, "skipped: no phone number": 1})
        self.assertEqual(len(self.rows(entity="reminders", group_by="kind")), 6)


class Closures(EntityCase):
    def test_default_is_applied_closures_and_the_counts_are_per_closure(self):
        rows = self.rows(entity="closures", fields=["branch", "doctor", "start_date", "end_date", "reason", "status", "patients_moved", "appointments_cancelled"])
        self.assertEqual([(r["branch"], r["start_date"], r["end_date"], r["reason"], r["patients_moved"], r["appointments_cancelled"]) for r in rows],
                         [("Branch A", "2026-09-10", "2026-09-10", "Power cut", 1, 0), ("Branch B", "2026-10-12", "2026-10-14", "Doctor on leave", 2, 1)])
        undone = self.rows(entity="closures", status="undone")
        self.assertEqual([(r["branch"], r["doctor"], r["status"], r["patients_moved"]) for r in undone], [("Branch A", "Dr. Mehta", "undone", 0)])

    def test_which_branches_are_closed_next_week(self):
        self.assertEqual(self.column("branch", entity="closures", date="2026-10-12", date_to="2026-10-18"), ["Branch B"])
        self.assertEqual(self.column("branch", entity="closures", date="2026-10-13"), ["Branch B"])          # inside the stretch
        self.assertEqual(self.rows(entity="closures", date="2026-10-15", date_to="2026-10-18"), [])

    def test_branch_and_doctor_filters(self):
        self.assertEqual(self.column("reason", entity="closures", branch_id=A), ["Power cut"])
        self.assertEqual(self.column("reason", entity="closures", doctor="mehta", status="undone"), ["Conference"])

    def test_how_many_patients_were_moved(self):
        result = self.run_query(entity="closures", aggregate="sum", measure="moved")
        self.assertEqual((result.value, result.total), (3, 2))
        by_branch = {r["branch"]: r["total_patients_moved"] for r in self.rows(entity="closures", aggregate="sum", measure="moved", group_by="branch")}
        self.assertEqual(by_branch, {"Branch B": 2, "Branch A": 1})

    def test_the_patient_message_is_not_a_column(self):
        self.assertNotIn("message", query_tool.FIELDS["closures"])
        self.assertNotIn("SECRETMETA", json.dumps(self.rows(entity="closures", fields=list(query_tool.FIELDS["closures"]), status="applied")))


class Blocks(EntityCase):
    def test_only_active_blocks_and_the_branch_wide_ones_show_for_a_branch(self):
        every = self.rows(entity="blocks")
        self.assertEqual([r["reason"] for r in every], ["Staff meeting", "Maintenance", "Dr. Rao conference", "Diwali"])
        self.assertEqual([r["reason"] for r in self.rows(entity="blocks", branch_id=B)], ["Staff meeting", "Dr. Rao conference", "Diwali"])
        self.assertEqual(self.rows(entity="blocks")[0]["start_time"], "13:00")

    def test_dates_overlap_and_doctor_filter(self):
        self.assertEqual(self.column("reason", entity="blocks", date="2026-10-11"), ["Dr. Rao conference"])
        self.assertEqual(self.column("doctor", entity="blocks", doctor="rao"), ["Dr. Rao"])
        self.assertEqual(self.column("branch", entity="blocks", date="2026-10-20"), ["All branches"])

    def test_count(self):
        self.assertEqual(self.count(entity="blocks", date="2026-10-07", date_to="2026-10-31"), 4)


class AuditAndActivity(EntityCase):
    def test_audit_has_readable_actions_and_a_safe_summary(self):
        rows = self.rows(entity="audit")
        self.assertEqual(list(rows[0]), ["when", "action", "record", "summary"])
        by_action = {r["action"]: r for r in rows}
        self.assertEqual(by_action["recorded a visit"]["record"], "visit")
        self.assertEqual(by_action["recorded a visit"]["summary"], "2026-10-07 · fee Rs 500")
        self.assertEqual(by_action["booked an appointment"]["summary"], "Amit Sharma · 2026-10-08 09:30")
        self.assertEqual(by_action["logged an expense"]["summary"], "2026-10-06 · Rs 1,200")
        self.assertIsNone(by_action["edited a follow-up (details not shown)"]["summary"])
        self.assertEqual(by_action["follow-up updated with its appointment"]["summary"], "status done, due date 2026-10-09")
        self.assertEqual(by_action["registered a patient"]["summary"], "Seema Rao")

    def test_what_did_i_approve_today(self):
        self.assertEqual(self.column("action", entity="audit", date="2026-10-07", order="oldest"), ["recorded a visit", "booked an appointment"])
        self.assertEqual(self.column("action", entity="audit", kind="follow-up", order="newest"),
                         ["edited a follow-up (details not shown)", "follow-up updated with its appointment"])
        self.assertEqual(self.count(entity="audit", kind="expense"), 1)

    def test_audit_is_newest_first_and_groups_by_action(self):
        self.assertEqual(self.column("action", entity="audit")[0], "booked an appointment")
        self.assertEqual(len(self.rows(entity="audit", group_by="action")), 6)
        self.assertEqual(self.rows(entity="audit", group_by="date", order="newest")[0], {"date": "2026-10-07", "count": 2})

    def test_activity(self):
        rows = self.rows(entity="activity", patient_name="amit")
        self.assertEqual([(r["event"], r["patient"], r["source"]) for r in rows],
                         [("Sent to staff", "Amit Sharma", "WhatsApp assistant"), ("Booked by staff", "Amit Sharma", "staff")])
        self.assertEqual(self.column("patient", entity="activity", kind="booking", order="oldest"), ["Walk In", "Amit Sharma"])
        self.assertEqual(self.count(entity="activity", date="2026-10-07"), 2)
        self.assertEqual(list(rows[0]), ["when", "event", "patient", "source", "detail"])

    def test_activity_detail_has_no_phone_numbers(self):
        detail = self.column("detail", entity="activity", kind="escalation")[0]
        self.assertNotIn("919000000001", detail)
        self.assertIn("[number]", detail)

    def test_activity_group_by(self):
        self.assertEqual({r["source"]: r["count"] for r in self.rows(entity="activity", group_by="source")}, {"WhatsApp assistant": 2, "staff": 2})
        self.assertEqual(len(self.rows(entity="activity", group_by="event")), 4)

    def test_audit_summary_only_reads_safe_keys(self):
        payload = json.dumps({"patient_name": "Amit", "diagnosis": "SECRETDIAG", "notes": "SECRETNOTE", "patient_phone": "9000000001",
                              "appt_date": "2026-10-08", "start_time": "10:00", "message": "SECRETBODY", "changes": {"diagnosis": "SECRETDIAG"}})
        summary = query_tool.audit_summary("book_appointment", payload)
        self.assertEqual(summary, "Amit · 2026-10-08 10:00")
        self.assertIsNone(query_tool.audit_summary("x", "not json"))
        self.assertIsNone(query_tool.audit_summary("x", json.dumps(["list"])))
        self.assertIsNone(query_tool.audit_summary("x", json.dumps({"notes": "SECRETNOTE"})))


class NothingSecretCanComeOut(EntityCase):
    def test_no_entity_has_a_forbidden_column(self):
        for entity, fields in query_tool.FIELDS.items():
            self.assertFalse(set(fields) & FORBIDDEN_FIELDS, (entity, set(fields) & FORBIDDEN_FIELDS))
        for entity, groups in query_tool.ENTITY_GROUPS.items():
            self.assertFalse(set(groups) & FORBIDDEN_FIELDS, entity)

    def test_every_column_of_every_entity_for_every_branch_scope_is_free_of_secrets(self):
        for entity in query_tool.ENTITIES:
            if entity == "availability":
                continue
            for branch_id in (None, A, B):
                for extra in ({}, {"date": "2026-09-01", "date_to": "2026-10-31"}):
                    spec = dict(extra) if "date" in query_tool.ENTITY_FILTERS[entity] else {}
                    if "date_to" not in query_tool.ENTITY_FILTERS[entity]:
                        spec.pop("date_to", None)
                    if entity == "followups" or entity == "closures":
                        spec["status"] = "pending" if entity == "followups" else "applied"
                    result = self.run_query(entity=entity, fields=list(query_tool.FIELDS[entity]), branch_id=branch_id, **spec)
                    dump = json.dumps(result.rows, ensure_ascii=False)
                    for secret in SECRETS + (LOG_PHONES if entity in ("audit", "activity", "reminders") else ()):
                        self.assertNotIn(secret, dump, (entity, branch_id, secret))

    def test_a_secret_cannot_be_found_by_searching_for_it_either(self):
        self.assertEqual(self.run_query(entity="visits", patient_name="chest").total, 0)          # the visit note is not searched
        for entity, key in (("visits", "patient_name"), ("expenses", "text"), ("staff", "text"), ("reminders", "patient_name"),
                            ("followups", "patient_name"), ("activity", "patient_name"), ("closures", "doctor")):
            self.assertEqual(self.run_query(entity=entity, **{key: "SECRET"}).total, 0, entity)

    def test_every_sort_group_and_measure_runs_and_leaks_nothing(self):
        for entity in query_tool.ENTITIES:
            if entity == "availability":
                continue
            for word in query_tool.ORDER_WORDS:
                try:
                    result = self.run_query(entity=entity, order=word, fields=list(query_tool.FIELDS[entity]))
                except QueryError:
                    continue
                for secret in SECRETS:
                    self.assertNotIn(secret, json.dumps(result.rows), (entity, word))
            for group in query_tool.ENTITY_GROUPS[entity]:
                result = self.run_query(entity=entity, group_by=group)
                self.assertEqual(result.columns, [group, "count"])
            for measure in query_tool.ENTITY_MEASURES[entity]:
                for aggregate in query_tool.MEASURE_AGGREGATES:
                    self.run_query(entity=entity, aggregate=aggregate, measure=measure)
                    for group in query_tool.ENTITY_GROUPS[entity]:
                        self.run_query(entity=entity, aggregate=aggregate, measure=measure, group_by=group)


class SortAndLimit(EntityCase):
    def test_newest_oldest_and_name(self):
        self.assertEqual(self.column("name", entity="patients", order="newest", limit=2), ["Seema Rao", "Ramesh Gupta"])
        self.assertEqual(self.column("name", entity="patients", order="oldest", limit=2), ["Amit Sharma", "Priya Shah"])
        self.assertEqual(self.column("name", entity="patients", order="name")[:2], ["Amit Sharma", "Priya Shah"])
        self.assertEqual(self.column("name", entity="patients", order="highest", limit=1), ["Amit Sharma"])        # by age
        self.assertEqual(self.column("name", entity="patients", order="lowest", limit=1), ["Priya Shah"])

    def test_the_oldest_pending_follow_up_and_the_biggest_expense(self):
        self.assertEqual(self.column("patient", entity="followups", order="oldest", limit=1), ["Amit Sharma"])
        self.assertEqual(self.column("patient", entity="followups", order="newest", limit=1), ["Priya Shah"])
        self.assertEqual(self.rows(entity="expenses", order="highest", limit=1), [{"date": "2026-10-01", "description": "Clinic rent", "amount_rupees": 50000.0}])
        self.assertEqual(self.column("description", entity="expenses", order="lowest", limit=1), ["stationery"])

    def test_highest_fee_and_longest_appointment(self):
        self.assertEqual(self.column("patient", entity="visits", order="highest", limit=1), ["Ramesh Gupta"])
        self.assertEqual(self.column("patient", entity="appointments", order="highest", limit=1, status="completed"), ["Ramesh Gupta"])

    def test_words_the_model_may_use(self):
        for said, word in (("latest", "newest"), ("Most Recent", None), ("biggest", "highest"), ("smallest", "lowest"), ("alphabetical", "name"),
                           ("earliest", "oldest"), ("top", "highest")):
            if word is None:
                with self.assertRaises(NotListed):
                    query_tool.validate_spec({"entity": "visits", "order": said})
                continue
            self.assertEqual(query_tool.validate_spec({"entity": "visits", "order": said})["order"], word, said)

    def test_a_field_and_a_direction(self):
        rows = self.rows(entity="visits", order="fee_rupees desc", limit=2)
        self.assertEqual([r["fee_rupees"] for r in rows], [1000.0, 500.0])
        self.assertEqual(query_tool.validate_spec({"entity": "visits", "order": "date asc"})["order"], "date:asc")
        for bad in ("fee_rupees sideways", "password desc", "1; DROP TABLE visits", "notes asc", "(select 1) desc"):
            with self.subTest(order=bad):
                with self.assertRaises(NotListed):
                    query_tool.validate_spec({"entity": "visits", "order": bad})

    def test_a_sort_the_entity_does_not_have_is_not_listed(self):
        with self.assertRaises(QueryError):
            query_tool.validate_spec({"entity": "doctors", "order": "newest"})
        with self.assertRaises(QueryError):
            query_tool.validate_spec({"entity": "attendance", "order": "highest"})

    def test_defaults_are_unchanged_for_the_old_entities(self):
        self.assertEqual(self.column("name", entity="patients"), ["Amit Sharma", "Priya Shah", "Ramesh Gupta", "Seema Rao"])
        self.assertEqual(self.column("due_date", entity="followups"), sorted(self.column("due_date", entity="followups")))

    def test_limit_applies_after_the_sort(self):
        self.assertEqual(len(self.rows(entity="visits", limit=3)), 3)
        result = self.run_query(entity="visits", limit=3)
        self.assertEqual((result.total, result.truncated), (5, True))


class Aggregates(EntityCase):
    OCT = {"date": "2026-10-01", "date_to": "2026-10-31"}

    def test_how_much_did_we_collect_this_month(self):
        result = self.run_query(entity="visits", aggregate="sum", measure="fee", **self.OCT)
        self.assertEqual((result.value, result.total, result.value_key, result.rows), (1500.5, 4, "total_fee_rupees", []))

    def test_average_min_max(self):
        base = dict(entity="visits", measure="fee_rupees", **self.OCT)
        self.assertEqual(self.run_query(aggregate="average", **base).value, 375.13)       # 1500.50 / 4, to the paisa
        self.assertEqual(self.run_query(aggregate="min", **base).value, 250.0)
        self.assertEqual(self.run_query(aggregate="max", **base).value, 500.0)
        self.assertEqual(self.run_query(aggregate="average", entity="patients", measure="age").value, 51.0)
        self.assertEqual(self.run_query(aggregate="sum", entity="appointments", measure="duration", date="2026-10-07").value, 105)
        self.assertEqual(self.run_query(aggregate="average", entity="appointments", measure="duration", date="2026-10-07").value, 35)

    def test_money_is_exact_to_the_paisa_and_rounded_to_two_places(self):
        self.conn.execute("DELETE FROM visits")
        for paise in (10001, 10002, 1):
            self.conn.execute("INSERT INTO visits (patient_id, visit_date, fee_paise) VALUES (1, '2026-10-01', ?)", (paise,))
        self.assertEqual(self.run_query(entity="visits", aggregate="sum", measure="fee").value, 200.04)
        average = self.run_query(entity="visits", aggregate="average", measure="fee").value
        self.assertEqual(average, round(average, 2))
        self.assertAlmostEqual(average, 66.68, places=2)
        self.assertEqual(self.run_query(entity="visits", aggregate="min", measure="fee").value, 0.01)

    def test_expenses_and_cashbook(self):
        self.assertEqual(self.run_query(entity="expenses", aggregate="sum", measure="amount", **self.OCT).value, 52000.0)
        self.assertEqual(self.run_query(entity="expenses", aggregate="max", measure="amount").value, 50000.0)
        self.assertEqual(self.run_query(entity="cashbook", aggregate="sum", measure="amount", kind="fee", **self.OCT).value, 1500.5)
        self.assertEqual(self.run_query(entity="cashbook", aggregate="sum", measure="amount", kind="expense", **self.OCT).value, 52000.0)

    def test_nothing_to_measure_has_no_value(self):
        result = self.run_query(entity="visits", aggregate="sum", measure="fee", date="2030-01-01")
        self.assertEqual((result.value, result.total), (None, 0))

    def test_group_by_with_a_count(self):
        by_doctor = self.rows(entity="appointments", group_by="doctor", date="2026-10-07", date_to="2026-10-14")
        self.assertEqual(by_doctor, [{"doctor": "Dr. Mehta", "count": 2}, {"doctor": "Dr. Rao", "count": 2}, {"doctor": "Dr. Iyer", "count": 1}])
        self.assertEqual({r["branch"]: r["count"] for r in self.rows(entity="appointments", group_by="branch")},
                         {"Branch A": 2, "Branch B": 3, "Branch C": 1})
        self.assertEqual({r["status"]: r["count"] for r in self.rows(entity="appointments", group_by="status", status="completed")}, {"completed": 2})
        by_date = self.rows(entity="appointments", group_by="date")
        self.assertEqual([r["date"] for r in by_date], sorted(r["date"] for r in by_date))

    def test_appointments_per_day_the_busiest_day_and_weekday(self):
        busiest = self.rows(entity="appointments", group_by="date", order="highest", limit=1)
        self.assertEqual(busiest, [{"date": "2026-10-07", "count": 3}])
        by_weekday = self.rows(entity="appointments", group_by="weekday")
        self.assertEqual(by_weekday[0], {"weekday": "Wednesday", "count": 5})            # 30 Sep, 7 Oct (three) and 14 Oct
        by_month = self.rows(entity="appointments", group_by="month")
        self.assertEqual(by_month, [{"month": "Sep 2026", "count": 1}, {"month": "Oct 2026", "count": 5}])

    def test_group_by_with_sums_and_the_group_order_words(self):
        by_month = self.rows(entity="visits", aggregate="sum", measure="fee", group_by="month")
        self.assertEqual(by_month, [{"month": "Sep 2026", "total_fee_rupees": 1000.0}, {"month": "Oct 2026", "total_fee_rupees": 1500.5}])
        self.assertEqual(self.rows(entity="visits", aggregate="sum", measure="fee", group_by="month", order="highest", limit=1)[0]["month"], "Oct 2026")
        self.assertEqual(self.rows(entity="visits", aggregate="sum", measure="fee", group_by="month", order="lowest", limit=1)[0]["month"], "Sep 2026")
        by_patient = self.rows(entity="visits", aggregate="max", measure="fee", group_by="patient")
        self.assertEqual(by_patient[0], {"patient": "Ramesh Gupta", "highest_fee_rupees": 1000.0})

    def test_expenses_by_category_and_cashbook_by_type(self):
        by_category = self.rows(entity="expenses", aggregate="sum", measure="amount", group_by="category")
        self.assertEqual(by_category[0], {"description": "clinic rent", "total_amount_rupees": 50000.0})
        self.assertEqual(sum(1 for r in by_category if r["description"] == "electricity bill"), 1)
        by_type = self.rows(entity="cashbook", aggregate="sum", measure="amount", group_by="type")
        self.assertEqual({r["type"]: r["total_amount_rupees"] for r in by_type}, {"expense": 52250.5, "fee": 2500.5})
        self.assertEqual(self.run_query(entity="cashbook", aggregate="count", group_by="type").total, 9)

    def test_group_keys_are_sorted_when_the_group_is_a_date(self):
        by_day = self.rows(entity="expenses", aggregate="count", group_by="date")
        self.assertEqual([r["date"] for r in by_day], sorted(r["date"] for r in by_day))

    def test_the_group_limit_and_the_truncated_flag(self):
        result = self.run_query(entity="visits", aggregate="sum", measure="fee", group_by="date", limit=2)
        self.assertEqual((len(result.rows), result.truncated), (2, True))
        self.assertFalse(self.run_query(entity="visits", aggregate="sum", measure="fee", group_by="month", limit=5).truncated)

    def test_a_list_with_a_group_by_is_a_count_per_group(self):
        spec = query_tool.validate_spec({"entity": "appointments", "aggregate": "list", "group_by": "doctor"})
        self.assertEqual(spec["aggregate"], "count")
        self.assertEqual(query_tool.validate_spec({"entity": "appointments", "group_by": "by month"})["group_by"], "month")

    def test_aggregates_and_measures_are_whitelisted(self):
        for spec in (
            {"entity": "patients", "aggregate": "sum"},                                         # no measure, and none is obvious
            {"entity": "attendance", "aggregate": "sum"},
            {"entity": "visits", "aggregate": "median", "measure": "fee"},
            {"entity": "visits", "aggregate": "sum", "measure": "notes"},
            {"entity": "visits", "aggregate": "sum", "measure": "amount"},                      # amount is an expense measure
            {"entity": "doctors", "aggregate": "sum", "measure": "fee"},
            {"entity": "followups", "aggregate": "average", "measure": "age"},
            {"entity": "visits", "aggregate": "sum", "measure": "fee_paise"},
            {"entity": "visits", "aggregate": "sum", "measure": "fee; DROP TABLE visits"},
            {"entity": "visits", "group_by": "notes"},
            {"entity": "visits", "group_by": "fee_paise"},
            {"entity": "visits", "group_by": "patient); DROP TABLE visits; --"},
            {"entity": "staff", "group_by": "salary"},
            {"entity": "audit", "group_by": "payload_json"},
        ):
            with self.subTest(spec=spec):
                with self.assertRaises(QueryError):
                    query_tool.validate_spec(spec)

    def test_a_missing_measure_is_the_obvious_one_where_there_is_only_one(self):
        for entity, aggregate, measure in (("visits", "sum", "fee_rupees"), ("expenses", "average", "amount_rupees"), ("cashbook", "max", "amount_rupees"),
                                           ("closures", "sum", "patients_moved"), ("appointments", "average", "duration_minutes")):
            self.assertEqual(query_tool.validate_spec({"entity": entity, "aggregate": aggregate})["measure"], measure)
        self.assertEqual(self.run_query(entity="expenses", aggregate="sum", **self.OCT).value, 52000.0)
        with self.assertRaises(QueryError):
            query_tool.validate_spec({"entity": "patients", "aggregate": "average"})       # patient age is never assumed

    def test_a_measure_on_a_list_or_a_count_is_just_ignored(self):
        spec = query_tool.validate_spec({"entity": "visits", "aggregate": "count", "measure": "fee"})
        self.assertNotIn("measure", spec)
        self.assertEqual(self.count(entity="visits", measure="fee"), 5)


class Describe(EntityCase):
    def sentence(self, today=TODAY, branch_name=None, **spec):
        spec = query_tool.validate_spec(spec)
        result = query_tool.run(self.conn, spec, today=today, now=NOW)
        return query_tool.describe(spec, result, branch_name, today=today)

    def test_the_example_sentences(self):
        self.assertEqual(self.sentence(entity="visits", aggregate="sum", measure="fee", date="2026-10-01", date_to="2026-10-31"),
                         "Total fees collected this month: Rs 1,500.50 (4 visits).")
        self.assertEqual(self.sentence(entity="visits", aggregate="sum", measure="fee", date="2026-09-01", date_to="2026-09-30"),
                         "Total fees collected last month: Rs 1,000 (1 visit).")
        self.assertEqual(self.sentence(entity="visits", aggregate="average", measure="fee", date="2026-10-01", date_to="2026-10-31"),
                         "Average fee this month: Rs 375.13 (4 visits).")
        self.assertEqual(self.sentence(entity="expenses", aggregate="max", measure="amount"), "Highest expense: Rs 50,000 (4 expenses).")
        self.assertEqual(self.sentence(entity="expenses", aggregate="min", measure="amount", text="electricity"),
                         "Lowest expense matching electricity: Rs 800 (2 expenses).")
        self.assertEqual(self.sentence(entity="patients", aggregate="average", measure="age"), "Average age: 51 years (4 patients).")
        self.assertEqual(self.sentence(entity="closures", aggregate="sum", measure="moved"), "Total patients moved (applied): 3 (2 closures).")

    def test_a_month_that_is_neither_this_nor_last_names_the_month(self):
        self.assertEqual(self.sentence(entity="visits", aggregate="sum", measure="fee", date="2026-01-01", date_to="2026-01-31"),
                         "No visits found in January 2026.")
        self.assertEqual(self.sentence(today="2026-11-20", entity="visits", aggregate="sum", measure="fee", date="2026-09-01", date_to="2026-09-30"),
                         "Total fees collected in September 2026: Rs 1,000 (1 visit).")
        self.assertIn("from Wed 7 Oct to Wed 14 Oct", self.sentence(entity="visits", date=TODAY, date_to="2026-10-14"))

    def test_group_and_list_and_count_sentences(self):
        self.assertEqual(self.sentence(entity="appointments", group_by="doctor", date=TODAY), "3 appointments by doctor on Wed 7 Oct.")
        self.assertEqual(self.sentence(entity="visits", aggregate="sum", measure="fee", group_by="month"), "Total fees collected by month.")
        self.assertEqual(self.sentence(entity="visits", aggregate="count", group_by="patient", limit=1), "5 visits by patient. Showing the first 1.")
        self.assertEqual(self.sentence(entity="staff", text="nurse"), "2 staff members matching nurse.")
        self.assertEqual(self.sentence(entity="staff", text="zzz"), "No staff members found matching zzz.")
        self.assertEqual(self.sentence(entity="attendance", status="absent", date=TODAY), "1 attendance entry (absent) on Wed 7 Oct.")
        self.assertEqual(self.sentence(entity="branches", status="open", date=TODAY), "2 branches (open) on Wed 7 Oct.")
        self.assertEqual(self.sentence(entity="schedules", doctor="rao", weekday="monday", time="now"),
                         "1 schedule entry with rao on Mondays at this time.")
        self.assertEqual(self.sentence(entity="followups", doctor="rao"), "1 follow-up (pending) with rao.")
        self.assertEqual(self.sentence(entity="reminders", kind="appointment reminder"), "2 messages of kind appointment reminder.")
        self.assertEqual(self.sentence(entity="closures"), "2 closures (applied).")
        self.assertEqual(self.sentence(entity="visits", aggregate="count", branch_name="Branch B"), "5 visits at Branch B.")

    def test_the_old_sentences_are_exactly_as_before(self):
        self.assertEqual(self.sentence(entity="patients", age_min=60), "2 patients aged 60 or more.")
        self.assertEqual(self.sentence(entity="patients", patient_name="zzz"), "No patients found matching zzz.")
        self.assertEqual(self.sentence(entity="followups"), "3 follow-ups (pending).")
        self.assertEqual(self.sentence(entity="appointments", aggregate="count", date=TODAY), "3 appointments on Wed 7 Oct.")

    def test_rupees_use_indian_grouping(self):
        for value, text in ((12400, "Rs 12,400"), (0, "Rs 0"), (999.5, "Rs 999.50"), (124000, "Rs 1,24,000"), (12400000, "Rs 1,24,00,000"),
                            (1234567.89, "Rs 12,34,567.89"), (50, "Rs 50"), (-500, "Rs -500"), (0.05, "Rs 0.05")):
            self.assertEqual(query_tool.rupees(value), text)


class Caps(EntityCase):
    def test_the_two_hundred_row_cap_holds_for_the_new_entities(self):
        self.conn.executemany("INSERT INTO expenses (expense_date, description, amount_paise) VALUES ('2026-10-01', ?, 100)",
                              [("Bulk %03d" % i,) for i in range(250)])
        result = self.run_query(entity="expenses")
        self.assertEqual((len(result.rows), result.total, result.truncated), (200, 254, True))
        self.assertEqual(self.run_query(entity="expenses", aggregate="count").total, 254)
        self.assertEqual(self.run_query(entity="expenses", aggregate="sum", measure="amount").total, 254)
        spec = query_tool.validate_spec({"entity": "expenses"})
        self.assertIn("Showing the first 200", query_tool.describe(spec, result))
        grouped = self.run_query(entity="expenses", aggregate="count", group_by="description")
        self.assertEqual((len(grouped.rows), grouped.truncated), (200, True))


class Injection(EntityCase):
    ATTACKS = ("'; DROP TABLE patients; --", "x' OR '1'='1", "%", "_", "\\", "' UNION SELECT name, sql FROM sqlite_master --",
               "Robert'); DELETE FROM appointments;--", "\x00", "a" * 79)
    NAME_ENTITIES = {"patient_name": ("patients", "appointments", "followups", "cashbook", "visits", "reminders", "activity"),
                     "text": ("staff", "attendance", "branches", "doctors", "schedules", "expenses")}

    def counts(self):
        return tuple(self.conn.execute("SELECT (SELECT COUNT(*) FROM patients), (SELECT COUNT(*) FROM appointments), "
                                       "(SELECT COUNT(*) FROM visits), (SELECT COUNT(*) FROM staff), (SELECT COUNT(*) FROM notifications)").fetchone())

    def test_values_in_every_text_filter_are_only_text_to_look_for(self):
        before = self.counts()
        for key, entities in self.NAME_ENTITIES.items():
            for entity in entities:
                for attack in self.ATTACKS:
                    with self.subTest(entity=entity, key=key, attack=attack):
                        result = self.run_query(entity=entity, **{key: attack})
                        self.assertEqual((result.rows, result.total), ([], 0))
        for entity in ("followups", "appointments", "closures", "blocks", "schedules"):
            for attack in self.ATTACKS:
                with self.subTest(entity=entity, key="doctor", attack=attack):
                    self.assertEqual(self.run_query(entity=entity, doctor=attack).total, 0)
        self.assertEqual(before, self.counts())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'patients'").fetchone()[0], 1)

    def test_nothing_but_the_whitelisted_words_is_accepted_for_the_other_filters(self):
        for attack in self.ATTACKS:
            for entity, key in (("reminders", "kind"), ("reminders", "status"), ("attendance", "status"), ("schedules", "weekday"),
                                ("schedules", "time"), ("branches", "status"), ("closures", "status"), ("audit", "kind"),
                                ("activity", "kind"), ("cashbook", "kind")):
                with self.subTest(entity=entity, key=key, attack=attack):
                    with self.assertRaises(QueryError):
                        query_tool.validate_spec({"entity": entity, key: attack})
            for key in ("order", "measure", "group_by"):
                with self.subTest(key=key, attack=attack):
                    with self.assertRaises(QueryError):
                        query_tool.validate_spec({"entity": "visits", "aggregate": "sum", "measure": "fee", key: attack})
            with self.assertRaises(QueryError):
                query_tool.validate_spec({"entity": "visits", "fields": [attack]})
            with self.assertRaises(QueryError):
                query_tool.validate_spec({"entity": attack})

    def test_no_user_value_is_ever_formatted_into_the_sql_for_the_new_entities(self):
        class Recording:
            def __init__(self, conn):
                self.conn, self.seen = conn, []

            def execute(self, sql, params=()):
                self.seen.append((sql, list(params)))
                return self.conn.execute(sql, params)

        for attack in (a for a in self.ATTACKS if len(a) > 3):
            for key, entities in self.NAME_ENTITIES.items():
                for entity in entities:
                    recorder = Recording(self.conn)
                    query_tool.run(recorder, {"entity": entity, key: attack, "aggregate": "count"}, today=TODAY, now=NOW)
                    self.assertFalse(any(attack.casefold() in sql.casefold() for sql, _ in recorder.seen), (entity, attack))
                    self.assertTrue(any(attack.casefold() in [str(p).casefold() for p in params] for _, params in recorder.seen),
                                    "the value must travel as a bound parameter")
            recorder = Recording(self.conn)
            query_tool.run(recorder, {"entity": "schedules", "doctor": attack, "aggregate": "count"}, today=TODAY, now=NOW)
            self.assertFalse(any(attack.casefold() in sql.casefold() for sql, _ in recorder.seen), attack)

    def test_every_statement_runs_with_query_only_on_and_the_old_setting_comes_back(self):
        seen = []
        original = query_tool._where

        def spy(*args, **kwargs):
            seen.append(self.conn.execute("PRAGMA query_only").fetchone()[0])
            return original(*args, **kwargs)

        query_tool._where = spy
        self.addCleanup(setattr, query_tool, "_where", original)
        for entity in ("staff", "reminders", "audit", "closures"):
            self.rows(entity=entity)
        self.assertEqual(self.conn.execute("PRAGMA query_only").fetchone()[0], 0)
        self.conn.execute("PRAGMA query_only = ON")
        self.rows(entity="visits", aggregate="sum", measure="fee")
        self.assertEqual(self.conn.execute("PRAGMA query_only").fetchone()[0], 1)
        with self.assertRaises(sqlite3.OperationalError):
            self.conn.execute("INSERT INTO staff (name) VALUES ('x')")

    def test_a_write_cannot_ride_along_in_any_value(self):
        before = self.counts()
        for entity in ("staff", "visits", "reminders"):
            self.run_query(entity=entity, **{self.NAME_KEY.get(entity, "patient_name"): "x'); INSERT INTO staff (name) VALUES ('hacked'); --"})
        self.assertEqual(before, self.counts())

    NAME_KEY = {"staff": "text"}


class Aliases(EntityCase):
    def test_patient_name_and_text_are_one_filter_whichever_the_entity_calls_it(self):
        self.assertEqual(query_tool.validate_spec({"entity": "staff", "patient_name": "Ravi"})["text"], "Ravi")
        self.assertEqual(query_tool.validate_spec({"entity": "visits", "text": "Amit"})["patient_name"], "Amit")
        self.assertEqual(query_tool.validate_spec({"entity": "visits", "text": "Amit", "patient_name": "amit"})["patient_name"], "amit")
        with self.assertRaises(QueryError):
            query_tool.validate_spec({"entity": "visits", "text": "Amit", "patient_name": "Priya"})
        with self.assertRaises(QueryError):                    # an entity with no name to search: a slip, not an unknown thing
            query_tool.validate_spec({"entity": "audit", "patient_name": "Amit"})
        with self.assertRaises(QueryError):
            query_tool.validate_spec({"entity": "closures", "text": "x"})

    def test_a_validated_spec_validates_again_unchanged(self):
        for spec in ({"entity": "visits", "aggregate": "sum", "measure": "fee", "group_by": "month", "order": "highest", "limit": 3},
                     {"entity": "schedules", "time": "NOW", "weekday": "Mon", "doctor": "Rao"},
                     {"entity": "visits", "order": "fee_rupees desc"}, {"entity": "reminders", "kind": "Follow-up reminder", "status": "Blocked"},
                     {"entity": "staff", "patient_name": "Ravi", "branch": "B"}):
            once = query_tool.validate_spec(spec)
            self.assertEqual(query_tool.validate_spec(once), once)

    def test_unknown_things_are_not_listed_but_bad_values_are_just_errors(self):
        for spec in ({"entity": "salaries"}, {"entity": "visits", "fields": ["tax"]}, {"entity": "visits", "colour": "red"},
                     {"entity": "visits", "group_by": "mood"}, {"entity": "staff", "aggregate": "median"},
                     {"entity": "visits", "aggregate": "sum", "measure": "tips"}, {"entity": "visits", "order": "mood desc"}):
            with self.assertRaises(NotListed):
                query_tool.validate_spec(spec)
        # a name some OTHER record type has, used on the wrong one, is a slip: refused, but not "the app cannot answer that yet"
        for spec in ({"entity": "doctors", "branch": "A"}, {"entity": "staff", "date": TODAY}, {"entity": "visits", "group_by": "branch"},
                     {"entity": "cashbook", "aggregate": "sum", "measure": "fee"}, {"entity": "patients", "fields": ["address"]},
                     {"entity": "attendance", "order": "highest"}, {"entity": "visits", "status": "open"}):
            with self.assertRaises(QueryError) as caught:
                query_tool.validate_spec(spec)
            self.assertNotIsInstance(caught.exception, NotListed, spec)
        for spec in ({"entity": "visits", "date": "yesterday"}, {"entity": "visits", "limit": 0}, {"entity": "reminders", "kind": "x"},
                     {"entity": "visits", "text": "x" * 81}):
            with self.assertRaises(QueryError) as caught:
                query_tool.validate_spec(spec)
            self.assertNotIsInstance(caught.exception, NotListed, spec)


class Counts(unittest.TestCase):
    def test_the_whitelist_lists_what_the_planner_prompt_says(self):
        self.assertEqual(len(query_tool.ENTITIES), 17)
        self.assertEqual(set(query_tool.ENTITIES) - {"availability"}, set(query_tool.FIELDS) - {"availability"})
        self.assertEqual(query_tool.AGGREGATES, ("list", "count", "sum", "average", "min", "max"))
        self.assertEqual(query_tool.ENTITY_MEASURES["visits"], ("fee_rupees",))
        self.assertTrue({"staff", "attendance", "branches", "doctors", "schedules", "visits", "expenses", "reminders", "closures",
                         "blocks", "audit", "activity"} <= set(query_tool.ENTITIES))

    def test_branch_entities(self):
        self.assertEqual(query_tool.BRANCH_ENTITIES, {"appointments", "followups", "staff", "attendance", "branches", "schedules",
                                                      "closures", "blocks", "availability"})
        self.assertEqual(query_tool.MY_BRANCH_DEFAULT, {"appointments", "availability"})


if __name__ == "__main__":
    unittest.main()
