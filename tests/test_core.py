import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import core, queries, scheduling
from clinic.booking_phone import PhoneRequiredError
from clinic.entity_resolution import resolve_patient, resolve_patient_by_phone, resolve_staff
from clinic.intents import HANDLERS

SCHEMA = (Path(__file__).resolve().parents[1] / "clinic" / "schema.sql").read_text()


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


class ProposalConfirmRejectTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def test_confirm_writes_entity_and_audit_log(self):
        pid = core.propose(self.conn, "register_patient", {"name": "Sunita Devi", "phone": "9876543210", "age": 34})
        entity_type, entity_id = core.confirm(self.conn, pid, HANDLERS)

        self.assertEqual(entity_type, "patient")
        patient = self.conn.execute("SELECT * FROM patients WHERE id = ?", (entity_id,)).fetchone()
        self.assertEqual(patient["name"], "Sunita Devi")

        audit_rows = self.conn.execute("SELECT * FROM audit_log WHERE entity_id = ?", (entity_id,)).fetchall()
        self.assertEqual(len(audit_rows), 1)
        self.assertEqual(audit_rows[0]["intent"], "register_patient")

        proposal = self.conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
        self.assertEqual(proposal["status"], "confirmed")

    def test_reject_leaves_no_entity(self):
        pid = core.propose(self.conn, "register_patient", {"name": "Ghost", "phone": "0000000000"})
        core.reject(self.conn, pid)

        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM patients").fetchone()["c"], 0)
        proposal = self.conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
        self.assertEqual(proposal["status"], "rejected")

    def test_cannot_confirm_twice(self):
        pid = core.propose(self.conn, "register_patient", {"name": "Ramesh", "phone": "1111111111"})
        core.confirm(self.conn, pid, HANDLERS)
        with self.assertRaises(core.ProposalAlreadyResolved):
            core.confirm(self.conn, pid, HANDLERS)

    def test_audit_log_is_immutable(self):
        pid = core.propose(self.conn, "register_patient", {"name": "Ramesh", "phone": "1111111111"})
        core.confirm(self.conn, pid, HANDLERS)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE audit_log SET payload_json = '{}' WHERE id = 1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM audit_log WHERE id = 1")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        pid = core.propose(self.conn, "register_patient", {"name": "Sunita Devi", "phone": "9876543210", "age": 34})
        _, self.patient_id = core.confirm(self.conn, pid, HANDLERS)

    def test_visit_and_followup_and_missed_worklist(self):
        vid = core.propose(
            self.conn, "record_visit",
            {"patient_id": self.patient_id, "fee_rupees": 300, "visit_date": "2026-01-01"},
        )
        _, visit_id = core.confirm(self.conn, vid, HANDLERS)

        fid = core.propose(
            self.conn, "set_followup",
            {"patient_id": self.patient_id, "visit_id": visit_id, "due_date": "2026-01-05"},
        )
        core.confirm(self.conn, fid, HANDLERS)

        worklist = queries.missed_followups(self.conn, as_of="2026-01-10")
        self.assertEqual(len(worklist), 1)
        self.assertEqual(worklist[0]["name"], "Sunita Devi")

        worklist_early = queries.missed_followups(self.conn, as_of="2026-01-02")
        self.assertEqual(len(worklist_early), 0)

    def test_day_end_cashbook_nets_fees_and_expenses(self):
        vid = core.propose(
            self.conn, "record_visit",
            {"patient_id": self.patient_id, "fee_rupees": 300, "visit_date": "2026-01-01"},
        )
        core.confirm(self.conn, vid, HANDLERS)

        eid = core.propose(
            self.conn, "log_expense",
            {"description": "Jhadu wala", "amount_rupees": 50, "expense_date": "2026-01-01"},
        )
        core.confirm(self.conn, eid, HANDLERS)

        totals = queries.day_end_cashbook(self.conn, on_date="2026-01-01")
        self.assertEqual(totals["fees_paise"], 30000)
        self.assertEqual(totals["expenses_paise"], 5000)
        self.assertEqual(totals["net_paise"], 25000)

    def test_attendance_upsert_on_same_day(self):
        aid = core.propose(self.conn, "log_attendance", {"staff_id": 1, "attendance_date": "2026-01-01", "status": "present"})
        # staff row doesn't exist yet in this test; insert one so the FK holds
        self.conn.execute("INSERT INTO staff (id, name) VALUES (1, 'Ramesh')")
        self.conn.commit()
        core.confirm(self.conn, aid, HANDLERS)

        aid2 = core.propose(self.conn, "log_attendance", {"staff_id": 1, "attendance_date": "2026-01-01", "status": "half_day"})
        core.confirm(self.conn, aid2, HANDLERS)

        rows = self.conn.execute("SELECT * FROM attendance WHERE staff_id = 1").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "half_day")

    def test_cancel_followup_sets_cancelled_status(self):
        fid = core.propose(self.conn, "set_followup", {"patient_id": self.patient_id, "due_date": "2026-01-05"})
        _, followup_id = core.confirm(self.conn, fid, HANDLERS)

        cid = core.propose(self.conn, "cancel_followup", {"followup_id": followup_id})
        core.confirm(self.conn, cid, HANDLERS)

        row = self.conn.execute("SELECT status FROM followups WHERE id = ?", (followup_id,)).fetchone()
        self.assertEqual(row["status"], "cancelled")

        # a cancelled follow-up should not show up in the recall worklist
        worklist = queries.missed_followups(self.conn, as_of="2026-01-10")
        self.assertEqual(len(worklist), 0)

    def test_reschedule_followup_updates_due_date(self):
        fid = core.propose(self.conn, "set_followup", {"patient_id": self.patient_id, "due_date": "2026-01-05"})
        _, followup_id = core.confirm(self.conn, fid, HANDLERS)

        rid = core.propose(self.conn, "reschedule_followup", {"followup_id": followup_id, "new_due_date": "2026-01-20"})
        core.confirm(self.conn, rid, HANDLERS)

        row = self.conn.execute("SELECT due_date, status FROM followups WHERE id = ?", (followup_id,)).fetchone()
        self.assertEqual(row["due_date"], "2026-01-20")
        self.assertEqual(row["status"], "pending")


class AppointmentWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        pid = core.propose(self.conn, "register_patient", {"name": "Sunita Devi", "phone": "9876543210", "age": 34})
        _, self.patient_id = core.confirm(self.conn, pid, HANDLERS)

    def test_book_appointment_creates_row(self):
        bid = core.propose(self.conn, "book_appointment", {
            "patient_id": self.patient_id, "appt_date": "2026-10-01", "start_time": "09:00",
            "duration_minutes": 15,
        })
        entity_type, appt_id = core.confirm(self.conn, bid, HANDLERS)

        self.assertEqual(entity_type, "appointment")
        row = self.conn.execute("SELECT * FROM appointments WHERE id = ?", (appt_id,)).fetchone()
        self.assertEqual(row["patient_id"], self.patient_id)
        self.assertEqual(row["appt_date"], "2026-10-01")
        self.assertEqual(row["start_time"], "09:00")
        self.assertEqual(row["status"], "booked")

    def book_raw(self, start="09:30", **who):
        bid = core.propose(self.conn, "book_appointment", dict(who, appt_date="2026-10-01", start_time=start,
                                                               duration_minutes=15))
        _, appt_id = core.confirm(self.conn, bid, HANDLERS)
        return self.conn.execute("SELECT * FROM appointments WHERE id = ?", (appt_id,)).fetchone(), bid

    def patients(self):
        return [tuple(r) for r in self.conn.execute("SELECT name, phone FROM patients ORDER BY id")]

    def test_a_walk_in_with_a_name_only_is_refused_now_that_a_phone_is_required(self):
        with self.assertRaises(PhoneRequiredError):
            self.book_raw(patient_name="Walk-in Ramesh")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0], 0)
        self.assertEqual(self.patients(), [("Sunita Devi", "9876543210")])

    def test_booking_a_new_person_with_name_and_phone_registers_them_as_a_patient(self):
        row, bid = self.book_raw(patient_name="Kavita", patient_phone="9876500301")
        self.assertEqual(self.patients()[-1], ("Kavita", "9876500301"))
        new_id = self.conn.execute("SELECT id FROM patients WHERE name = 'Kavita'").fetchone()[0]
        self.assertEqual((row["patient_id"], row["patient_name"], row["patient_phone"]), (new_id, None, None))
        audit = self.conn.execute("SELECT payload_json FROM audit_log WHERE proposal_id = ?", (bid,)).fetchone()[0]
        self.assertEqual(json.loads(audit)["registered_patient_id"], new_id)     # the audit trail says the booking made them

    def test_the_same_person_on_the_same_phone_is_not_registered_twice(self):
        self.book_raw(patient_name="Kavita", patient_phone="9876500301")
        row, bid = self.book_raw("10:00", patient_name="Kavita", patient_phone="+91 98765 00301")   # same name, same phone
        self.assertEqual(len(self.patients()), 2)
        self.assertEqual(row["patient_id"], self.conn.execute("SELECT id FROM patients WHERE name = 'Kavita'").fetchone()[0])
        audit = self.conn.execute("SELECT payload_json FROM audit_log WHERE proposal_id = ?", (bid,)).fetchone()[0]
        self.assertNotIn("registered_patient_id", json.loads(audit))

    def test_a_different_spelling_on_the_same_phone_is_not_the_same_person_at_this_level(self):
        # Names are matched exactly. (Spoken commands match the phone first, in pipeline/voice_context;
        # this is only the approve-time fallback for a raw name + phone, which stays cautious.)
        self.book_raw(patient_name="Kavita", patient_phone="9876500301")
        self.book_raw("10:00", patient_name="Kavitha", patient_phone="9876500301")
        self.assertEqual([p[0] for p in self.patients()], ["Sunita Devi", "Kavita", "Kavitha"])

    def test_a_relative_on_a_shared_phone_gets_their_own_record(self):
        self.book_raw(patient_name="Kavita", patient_phone="9876500301")
        self.book_raw("10:00", patient_name="Ravi Kumar", patient_phone="9876500301")
        self.assertEqual([p[0] for p in self.patients()], ["Sunita Devi", "Kavita", "Ravi Kumar"])

    def test_a_phone_that_is_not_ten_digits_is_refused_and_makes_no_patient(self):
        with self.assertRaises(PhoneRequiredError):
            self.book_raw(patient_name="Short Number", patient_phone="12345")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0], 0)
        self.assertEqual(len(self.patients()), 1)

    def test_an_unattended_booking_never_registers_anyone(self):
        row, _ = self.book_raw(patient_name="Auto Caller", patient_phone="9111122223", unattended=True)
        self.assertIsNone(row["patient_id"])
        self.assertEqual((row["patient_name"], row["patient_phone"]), ("Auto Caller", "9111122223"))
        self.assertEqual(len(self.patients()), 1)

    def test_double_booking_is_rejected_at_confirm_time(self):
        first = core.propose(self.conn, "book_appointment", {
            "patient_id": self.patient_id, "appt_date": "2026-10-01", "start_time": "09:00",
            "duration_minutes": 15,
        })
        core.confirm(self.conn, first, HANDLERS)

        # A second proposal for the exact same slot is allowed to exist (the
        # human hasn't approved it yet) -- it's only rejected when actually
        # confirmed, against whatever is booked *right now*.
        second = core.propose(self.conn, "book_appointment", {
            "patient_id": self.patient_id, "appt_date": "2026-10-01", "start_time": "09:00",
            "duration_minutes": 15,
        })
        with self.assertRaises(scheduling.SlotConflictError):
            core.confirm(self.conn, second, HANDLERS)

        # The failed confirm must not have left a half-written proposal/audit
        # trail -- core.confirm()'s `with conn:` wraps the handler and the
        # bookkeeping updates in one transaction.
        proposal = self.conn.execute("SELECT status FROM proposals WHERE id = ?", (second,)).fetchone()
        self.assertEqual(proposal["status"], "pending")
        appt_count = self.conn.execute("SELECT COUNT(*) c FROM appointments").fetchone()["c"]
        self.assertEqual(appt_count, 1)

    def test_cancel_appointment_sets_cancelled_status(self):
        bid = core.propose(self.conn, "book_appointment", {
            "patient_id": self.patient_id, "appt_date": "2026-10-01", "start_time": "09:00",
            "duration_minutes": 15,
        })
        _, appt_id = core.confirm(self.conn, bid, HANDLERS)

        cid = core.propose(self.conn, "cancel_appointment", {"appointment_id": appt_id})
        core.confirm(self.conn, cid, HANDLERS)

        row = self.conn.execute("SELECT status FROM appointments WHERE id = ?", (appt_id,)).fetchone()
        self.assertEqual(row["status"], "cancelled")

    def test_reschedule_appointment_updates_date_and_time(self):
        bid = core.propose(self.conn, "book_appointment", {
            "patient_id": self.patient_id, "appt_date": "2026-10-01", "start_time": "09:00",
            "duration_minutes": 15,
        })
        _, appt_id = core.confirm(self.conn, bid, HANDLERS)

        rid = core.propose(self.conn, "reschedule_appointment", {
            "appointment_id": appt_id, "appt_date": "2026-10-02", "start_time": "10:00",
        })
        core.confirm(self.conn, rid, HANDLERS)

        row = self.conn.execute("SELECT appt_date, start_time, status FROM appointments WHERE id = ?", (appt_id,)).fetchone()
        self.assertEqual(row["appt_date"], "2026-10-02")
        self.assertEqual(row["start_time"], "10:00")
        self.assertEqual(row["status"], "booked")  # in-place update, not a new row/status

    def test_reschedule_appointment_rejects_conflicting_new_slot(self):
        first = core.propose(self.conn, "book_appointment", {
            "patient_id": self.patient_id, "appt_date": "2026-10-01", "start_time": "09:00",
            "duration_minutes": 15,
        })
        _, first_id = core.confirm(self.conn, first, HANDLERS)

        second = core.propose(self.conn, "book_appointment", {
            "patient_id": self.patient_id, "appt_date": "2026-10-01", "start_time": "09:15",
            "duration_minutes": 15,
        })
        _, second_id = core.confirm(self.conn, second, HANDLERS)

        rid = core.propose(self.conn, "reschedule_appointment", {
            "appointment_id": second_id, "appt_date": "2026-10-01", "start_time": "09:00",
        })
        with self.assertRaises(scheduling.SlotConflictError):
            core.confirm(self.conn, rid, HANDLERS)

    def test_next_appointment_for_patient_query(self):
        # "Upcoming" is judged against the real clock, so book a day ahead: a fixed
        # date would stop counting as upcoming once its time of day had passed.
        from datetime import date, timedelta
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        core.confirm(self.conn, core.propose(self.conn, "book_appointment", {
            "patient_id": self.patient_id, "appt_date": tomorrow, "start_time": "11:00",
            "duration_minutes": 15,
        }), HANDLERS)

        row = queries.next_appointment_for_patient(self.conn, self.patient_id)
        self.assertEqual(row["appt_date"], tomorrow)
        self.assertEqual(row["start_time"], "11:00")

    def test_scheduled_appointments_query(self):
        core.confirm(self.conn, core.propose(self.conn, "book_appointment", {
            "patient_id": self.patient_id, "appt_date": "2026-10-05", "start_time": "11:00",
            "duration_minutes": 15,
        }), HANDLERS)

        rows = queries.scheduled_appointments(self.conn, "2026-10-05", "2026-10-05")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["patient_name"], "Sunita Devi")


class EntityResolutionTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        for name, phone in [("Sunita Devi", "9876543210"), ("Sunil Kumar", "9998887776")]:
            pid = core.propose(self.conn, "register_patient", {"name": name, "phone": phone})
            core.confirm(self.conn, pid, HANDLERS)

    def test_exact_phone_match_wins(self):
        candidates = resolve_patient(self.conn, "9876543210")
        self.assertEqual(candidates[0].label.split(" (")[0], "Sunita Devi")
        self.assertEqual(candidates[0].score, 1.0)

    def test_a_first_name_matches_exactly(self):
        candidates = resolve_patient(self.conn, "Sunita")
        self.assertEqual([c.label.split(" (")[0] for c in candidates], ["Sunita Devi"])
        self.assertEqual(candidates[0].score, 1.0)

    def test_resolve_staff_empty_when_none_registered(self):
        self.assertEqual(resolve_staff(self.conn, "Ramesh"), [])

    def test_resolve_patient_by_phone_with_country_code(self):
        candidate = resolve_patient_by_phone(self.conn, "919876543210")
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.label.split(" (")[0], "Sunita Devi")

    def test_resolve_patient_by_phone_without_country_code(self):
        candidate = resolve_patient_by_phone(self.conn, "9876543210")
        self.assertEqual(candidate.label.split(" (")[0], "Sunita Devi")

    def test_resolve_patient_by_phone_no_match_returns_none(self):
        self.assertIsNone(resolve_patient_by_phone(self.conn, "911111111111"))


if __name__ == "__main__":
    unittest.main()
