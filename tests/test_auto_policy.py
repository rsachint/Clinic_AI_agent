"""Unit tests for the pieces under automatic WhatsApp appointment actions:
settings, booking blocks (and their effect on slot generation and the write
handlers), the patient activity log, and the guard policy matrix. Pure: an
in-memory database, an injected clock, no Flask, no network."""
import json
import sqlite3
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import auto_policy, booking_blocks, core, intents, patient_activity, scheduling, settings
from clinic.adapters.registry import build_write_handlers, get_adapters

SCHEMA = (Path(__file__).resolve().parents[1] / "clinic" / "schema.sql").read_text()
NOW = datetime(2026, 10, 5, 10, 0)   # Monday 10:00
TODAY, TOMORROW, WED = "2026-10-05", "2026-10-06", "2026-10-07"
WA = "919876543210"
WA_OTHER = "919111122223"
HANDLERS = build_write_handlers(*get_adapters())


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.pid = self.conn.execute("INSERT INTO patients (name, phone) VALUES ('Sunita Devi', '9876543210')").lastrowid
        self.conn.commit()

    def appt(self, date=TOMORROW, time="09:00", phone=None, patient_id=None, name=None, status="booked", queue_state=None):
        cur = self.conn.execute(
            "INSERT INTO appointments (patient_id, patient_name, patient_phone, appt_date, start_time, status, queue_state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (patient_id, name, phone, date, time, status, queue_state))
        self.conn.commit()
        return cur.lastrowid

    def mine(self, date=TOMORROW, time="09:00", **kw):
        return self.appt(date, time, patient_id=self.pid, **kw)

    def block(self, start, end=None, t0=None, t1=None, reason="Doctor on leave"):
        return booking_blocks.add_block(self.conn, start, end, t0, t1, reason)["id"]

    def auto_rows(self, n, day=TODAY):
        for _ in range(n):
            patient_activity.log(self.conn, event="auto_booked", source="whatsapp-agent", wa_id="x", now=datetime.fromisoformat(day + " 09:00:00"))


class SettingsTests(Base):
    def test_defaults_are_on_and_forty(self):
        self.assertTrue(settings.auto_enabled(self.conn))
        self.assertEqual(settings.auto_daily_cap(self.conn), 40)

    def test_round_trip_and_validation(self):
        settings.set_auto_enabled(self.conn, False)
        self.assertFalse(settings.auto_enabled(self.conn))
        settings.set_auto_enabled(self.conn, True)
        self.assertTrue(settings.auto_enabled(self.conn))
        settings.set_auto_daily_cap(self.conn, 12)
        self.assertEqual(settings.auto_daily_cap(self.conn), 12)
        settings.set_auto_daily_cap(self.conn, 0)
        self.assertEqual(settings.auto_daily_cap(self.conn), 0)
        for bad in (-1, 1001, "abc", None, "x", True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                settings.set_auto_daily_cap(self.conn, bad)
        self.assertEqual(settings.auto_daily_cap(self.conn), 0)

    def test_missing_table_reads_as_defaults(self):
        bare = sqlite3.connect(":memory:")
        self.assertTrue(settings.auto_enabled(bare))
        self.assertEqual(settings.auto_daily_cap(bare), 40)

    def test_garbage_stored_value_falls_back_to_the_default_cap(self):
        settings.set_value(self.conn, settings.AUTO_DAILY_CAP, "lots")
        self.assertEqual(settings.auto_daily_cap(self.conn), 40)


class BookingBlockTests(Base):
    def test_whole_day_block_removes_every_slot_that_day_only(self):
        self.block(TOMORROW)
        self.assertEqual(scheduling.generate_slots(self.conn, TOMORROW), [])
        self.assertTrue(scheduling.day_blocked(self.conn, TOMORROW))
        self.assertTrue(scheduling.generate_slots(self.conn, WED))
        self.assertFalse(scheduling.day_blocked(self.conn, WED))

    def test_time_range_block_removes_only_those_slots_on_every_date_in_the_span(self):
        self.block(TOMORROW, WED, "09:00", "10:00")
        for day in (TOMORROW, WED):
            slots = scheduling.generate_slots(self.conn, day)
            self.assertNotIn("09:00", slots)
            self.assertNotIn("09:45", slots)
            self.assertIn("10:00", slots)
            self.assertIn("16:00", slots)
            self.assertFalse(scheduling.day_blocked(self.conn, day))
        self.assertIn("09:00", scheduling.generate_slots(self.conn, "2026-10-08"))

    def test_is_slot_free_respects_blocks_unless_ignored(self):
        self.block(TOMORROW, t0="16:00", t1="17:00")
        self.assertFalse(scheduling.is_slot_free(self.conn, TOMORROW, "16:30", 15))
        self.assertTrue(scheduling.is_slot_free(self.conn, TOMORROW, "16:30", 15, ignore_blocks=True))
        self.assertTrue(scheduling.is_slot_free(self.conn, TOMORROW, "17:00", 15))
        self.assertEqual(scheduling.block_reason(self.conn, TOMORROW, "16:30"), "Doctor on leave")
        self.assertIsNone(scheduling.block_reason(self.conn, TOMORROW, "17:00"))

    def test_removed_block_no_longer_blocks(self):
        bid = self.block(TOMORROW)
        self.assertTrue(booking_blocks.remove_block(self.conn, bid))
        self.assertFalse(booking_blocks.remove_block(self.conn, bid))
        self.assertTrue(scheduling.generate_slots(self.conn, TOMORROW))

    def test_blocked_only_slots_lists_what_an_override_could_use(self):
        self.mine(TOMORROW, "09:00")
        self.block(TOMORROW, t0="09:00", t1="10:00")
        self.assertEqual(scheduling.blocked_only_slots(self.conn, TOMORROW), ["09:30"])   # 09:00 is booked, not merely blocked

    def test_existing_appointments_inside_a_new_block_are_reported_and_untouched(self):
        a1 = self.mine(TOMORROW, "09:00")
        a2 = self.appt(TOMORROW, "16:00", phone="9000000001", name="Walk-in")
        self.appt(TOMORROW, "09:15", phone="9000000002", name="Cancelled", status="cancelled")
        result = booking_blocks.add_block(self.conn, TOMORROW, TOMORROW, "09:00", "12:00", "Surgery")
        self.assertEqual([a["id"] for a in result["affected"]], [a1])
        whole = booking_blocks.add_block(self.conn, TOMORROW, reason="Closed")
        self.assertEqual([a["id"] for a in whole["affected"]], [a1, a2])
        for aid in (a1, a2):
            self.assertEqual(self.conn.execute("SELECT status FROM appointments WHERE id=?", (aid,)).fetchone()[0], "booked")
        listed = booking_blocks.list_blocks(self.conn, today=TODAY)
        self.assertEqual([len(b["affected"]) for b in listed], [2, 1])   # whole-day block first

    def test_input_validation(self):
        bad = [
            dict(start_date="05/10/2026"), dict(start_date="2026-13-01"), dict(start_date=TOMORROW, end_date=TODAY),
            dict(start_date=TOMORROW, start_time="09:00"), dict(start_date=TOMORROW, start_time="10:00", end_time="09:00"),
            dict(start_date=TOMORROW, start_time="9am", end_time="10:00"), dict(start_date=TOMORROW, start_time="25:00", end_time="26:00"),
            dict(start_date=TOMORROW, end_date="2028-01-01"), dict(start_date=TOMORROW, reason="x" * 201), dict(start_date=None),
        ]
        for kw in bad:
            with self.assertRaises(booking_blocks.BlockError, msg=str(kw)):
                booking_blocks.normalize(**kw)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM booking_blocks").fetchone()[0], 0)

    def test_describe(self):
        self.assertIn("whole day", booking_blocks.describe({"start_date": TOMORROW, "end_date": TOMORROW, "start_time": None, "end_time": None}))
        self.assertIn("09:00-12:00", booking_blocks.describe({"start_date": TOMORROW, "end_date": WED, "start_time": "09:00", "end_time": "12:00"}))

    def test_past_blocks_are_hidden_from_the_list(self):
        self.block("2026-09-01", "2026-09-02")
        self.block(TOMORROW)
        self.assertEqual(len(booking_blocks.list_blocks(self.conn, today=TODAY)), 1)
        self.assertEqual(len(booking_blocks.list_blocks(self.conn, today=TODAY, include_past=True)), 2)


class WriteHandlerBlockTests(Base):
    def confirm(self, intent, slots):
        pid = core.propose(self.conn, intent, slots, "test")
        return core.confirm(self.conn, pid, HANDLERS)

    def booking(self, **kw):
        slots = {"patient_name": "Walk-in", "patient_phone": "9000000001", "appt_date": TOMORROW, "start_time": "09:00"}
        slots.update(kw)
        return slots

    def test_booking_into_a_block_is_refused_server_side(self):
        self.block(TOMORROW, t0="09:00", t1="10:00")
        with self.assertRaises(scheduling.SlotBlockedError):
            self.confirm("book_appointment", self.booking())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], 0)

    def test_a_blocked_error_is_also_a_slot_conflict_error(self):
        self.assertTrue(issubclass(scheduling.SlotBlockedError, scheduling.SlotConflictError))

    def test_explicit_override_books_and_the_audit_row_says_so(self):
        self.block(TOMORROW, t0="09:00", t1="10:00")
        _, aid = self.confirm("book_appointment", self.booking(override_block=True))
        self.assertEqual(self.conn.execute("SELECT status FROM appointments WHERE id=?", (aid,)).fetchone()[0], "booked")
        payload = json.loads(self.conn.execute("SELECT payload_json FROM audit_log").fetchone()[0])
        self.assertTrue(payload["override_block"])

    def test_override_never_beats_the_double_booking_guard(self):
        self.block(TOMORROW, t0="09:00", t1="10:00")
        self.appt(TOMORROW, "09:00", phone="9000000009", name="Taken")
        with self.assertRaises(scheduling.SlotConflictError):
            self.confirm("book_appointment", self.booking(override_block=True))

    def test_an_unblocked_booking_audit_payload_is_unchanged(self):
        self.confirm("book_appointment", self.booking())
        payload = json.loads(self.conn.execute("SELECT payload_json FROM audit_log").fetchone()[0])
        self.assertNotIn("override_block", payload)

    def test_reschedule_into_a_block_is_refused_unless_overridden(self):
        aid = self.mine(TOMORROW, "09:00")
        self.block(WED, t0="16:00", t1="17:00")
        with self.assertRaises(scheduling.SlotBlockedError):
            self.confirm("reschedule_appointment", {"appointment_id": aid, "appt_date": WED, "start_time": "16:00"})
        self.assertEqual(self.conn.execute("SELECT appt_date FROM appointments WHERE id=?", (aid,)).fetchone()[0], TOMORROW)
        self.confirm("reschedule_appointment", {"appointment_id": aid, "appt_date": WED, "start_time": "16:00", "override_block": True})
        self.assertEqual(self.conn.execute("SELECT appt_date FROM appointments WHERE id=?", (aid,)).fetchone()[0], WED)

    def test_require_active_refuses_a_cancelled_appointment(self):
        aid = self.mine(TOMORROW, "09:00", status="cancelled")
        for intent, slots in (("cancel_appointment", {"appointment_id": aid}),
                              ("reschedule_appointment", {"appointment_id": aid, "appt_date": WED, "start_time": "09:00"})):
            with self.assertRaises(ValueError, msg=intent):
                self.confirm(intent, dict(slots, require_active=True))
        # ...while the old review-card behaviour (no flag) is untouched.
        self.confirm("cancel_appointment", {"appointment_id": aid})

    def test_restore_reinstates_only_a_cancelled_appointment_in_a_free_slot(self):
        aid = self.mine(TOMORROW, "09:00", status="cancelled")
        self.confirm("restore_appointment", {"appointment_id": aid})
        self.assertEqual(self.conn.execute("SELECT status FROM appointments WHERE id=?", (aid,)).fetchone()[0], "booked")
        with self.assertRaises(ValueError):                              # no longer cancelled
            self.confirm("restore_appointment", {"appointment_id": aid})
        gone = self.mine(WED, "09:00", status="cancelled")
        self.appt(WED, "09:00", phone="9000000007", name="Took it")
        with self.assertRaises(scheduling.SlotConflictError):
            self.confirm("restore_appointment", {"appointment_id": gone})
        self.assertEqual(self.conn.execute("SELECT status FROM appointments WHERE id=?", (gone,)).fetchone()[0], "cancelled")

    def test_restore_keeps_a_confirmed_status_and_rejects_nonsense(self):
        aid = self.mine(TOMORROW, "09:00", status="cancelled")
        with self.assertRaises(ValueError):
            self.confirm("restore_appointment", {"appointment_id": aid, "status": "completed"})
        self.confirm("restore_appointment", {"appointment_id": aid, "status": "confirmed"})
        self.assertEqual(self.conn.execute("SELECT status FROM appointments WHERE id=?", (aid,)).fetchone()[0], "confirmed")


class PatientActivityTests(Base):
    def test_log_and_timeline_by_patient_id_and_by_phone_fallback(self):
        patient_activity.log(self.conn, event="requested", source="whatsapp-agent", wa_id=WA, patient_id=self.pid, patient_name="Sunita Devi", now=NOW)
        patient_activity.log(self.conn, event="auto_booked", source="whatsapp-agent", wa_id="9876543210", patient_name="Sunita (before registering)", now=NOW)
        patient_activity.log(self.conn, event="auto_booked", source="whatsapp-agent", wa_id=WA_OTHER, patient_name="Someone else", now=NOW)
        timeline = patient_activity.timeline_for_patient(self.conn, self.pid)
        self.assertEqual([t["event"] for t in timeline], ["auto_booked", "requested"])
        self.assertEqual(patient_activity.timeline_for_patient(self.conn, 999), [])

    def test_log_never_raises(self):
        with self.assertLogs("clinic.patient_activity", level="ERROR"):
            self.assertIsNone(patient_activity.log(self.conn, event="nonsense", source="whatsapp-agent"))
            self.assertIsNone(patient_activity.log(self.conn, event="requested", source="somewhere"))
            self.conn.execute("DROP TABLE patient_activity")
            self.assertIsNone(patient_activity.log(self.conn, event="requested", source="staff"))

    def test_count_is_per_local_calendar_day(self):
        self.auto_rows(3, TODAY)
        self.auto_rows(2, "2026-10-04")
        self.assertEqual(patient_activity.automated_bookings_on(self.conn, TODAY), 3)
        self.assertEqual(patient_activity.automated_bookings_on(self.conn, "2026-10-04"), 2)

    def test_feed_filters_by_name_or_number_and_hides_the_bare_requested_marker(self):
        patient_activity.log(self.conn, event="requested", source="whatsapp-agent", wa_id=WA, patient_name="Sunita Devi", now=NOW)
        patient_activity.log(self.conn, event="escalated", source="whatsapp-agent", wa_id=WA, patient_name="Sunita Devi", detail="x", now=NOW)
        patient_activity.log(self.conn, event="escalated", source="whatsapp-agent", wa_id=WA_OTHER, patient_name="Bhavna", detail="y", now=NOW)
        patient_activity.log(self.conn, event="staff_booked", source="staff", wa_id=WA, patient_name="Sunita Devi", now=NOW)
        self.assertEqual([i["event"] for i in patient_activity.feed(self.conn)], ["escalated", "escalated"])
        self.assertEqual([i["patient_name"] for i in patient_activity.feed(self.conn, "sunita")], ["Sunita Devi"])
        self.assertEqual([i["patient_name"] for i in patient_activity.feed(self.conn, "11122")], ["Bhavna"])
        self.assertEqual(patient_activity.feed(self.conn, "nobody"), [])


class PolicyMatrixTests(Base):
    def decide(self, intent, slots, wa_id=WA, now=NOW):
        return auto_policy.evaluate(self.conn, intent, slots, wa_id, now)

    def book(self, date=TOMORROW, time="16:00", **kw):
        slots = {"patient_id": self.pid, "patient_name": None, "patient_phone": None, "appt_date": date, "start_time": time}
        slots.update(kw)
        return slots

    def unknown_book(self, date=TOMORROW, time="16:00", name="Raju", wa_id=WA_OTHER):
        return {"patient_id": None, "patient_name": name, "patient_phone": wa_id[-10:], "appt_date": date, "start_time": time}

    def assertAuto(self, decision):
        self.assertTrue(decision.auto, decision)
        self.assertIsNone(decision.reason)
        self.assertIsNone(decision.code)

    def assertDenied(self, decision, code, fragment):
        self.assertFalse(decision.auto, decision)
        self.assertEqual(decision.code, code, decision)
        self.assertIn(fragment, decision.reason)

    # -- book --------------------------------------------------------------------
    def test_a_clean_booking_is_automatic(self):
        self.assertAuto(self.decide("book_appointment", self.book()))

    def test_an_unknown_number_with_a_name_is_automatic_too(self):
        self.assertAuto(self.decide("book_appointment", self.unknown_book(), wa_id=WA_OTHER))

    def test_an_unknown_number_without_a_name_goes_to_staff(self):
        self.assertDenied(self.decide("book_appointment", self.unknown_book(name=""), wa_id=WA_OTHER), "no_name", "no patient name")
        self.assertDenied(self.decide("book_appointment", self.unknown_book(name=None), wa_id=WA_OTHER), "no_name", "no patient name")
        self.assertDenied(self.decide("book_appointment", self.unknown_book(name="x" * 80), wa_id=WA_OTHER), "no_name", "too long")

    def test_switch_off(self):
        settings.set_auto_enabled(self.conn, False)
        self.assertDenied(self.decide("book_appointment", self.book()), "switch_off", "switched off")
        self.assertDenied(self.decide("cancel_appointment", {"appointment_id": self.mine()}), "switch_off", "switched off")

    def test_human_takeover(self):
        self.conn.execute("INSERT INTO wa_sessions (wa_id, mode) VALUES (?, 'human')", (WA,))
        self.assertDenied(self.decide("book_appointment", self.book()), "human_mode", "taken over")
        self.assertAuto(self.decide("book_appointment", self.unknown_book(), wa_id=WA_OTHER))   # other chats unaffected

    def test_only_three_intents_can_ever_be_automatic(self):
        for intent in ("register_patient", "record_visit", "set_followup", "cancel_followup", "reschedule_followup",
                       "log_expense", "log_attendance", "register_staff", "queue_check_in", "restore_appointment"):
            self.assertDenied(self.decide(intent, {}), "not_automatable", "staff member")

    def test_past_times_and_dates(self):
        self.assertDenied(self.decide("book_appointment", self.book(TODAY, "09:00")), "past", "already passed")
        self.assertDenied(self.decide("book_appointment", self.book(TODAY, "10:00")), "past", "already passed")   # starting now is past
        self.assertDenied(self.decide("book_appointment", self.book("2026-10-04", "16:00")), "past", "already passed")
        self.assertAuto(self.decide("book_appointment", self.book(TODAY, "10:30")))

    def test_booking_horizon(self):
        self.assertAuto(self.decide("book_appointment", self.book("2026-11-04", "16:00")))      # exactly 30 days
        self.assertDenied(self.decide("book_appointment", self.book("2026-11-05", "16:00")), "too_far", "30 days")

    def test_clinic_hours_and_the_slot_grid(self):
        # 30-minute slots: 12:45 / 19:45 would run past the end of the shift, 09:15 is off the grid
        for t in ("08:45", "12:45", "13:00", "14:00", "19:45", "20:00", "09:15", "09:20", "23:00"):
            self.assertDenied(self.decide("book_appointment", self.book(time=t)), "outside_hours", "outside clinic hours")
        for t in ("09:00", "12:30", "16:00", "19:30"):
            self.assertAuto(self.decide("book_appointment", self.book(time=t)))

    def test_malformed_slots(self):
        for kw in (dict(date="tomorrow"), dict(date="2026-02-30"), dict(date=None), dict(time="4pm"), dict(time=None), dict(time="9:0")):
            self.assertDenied(self.decide("book_appointment", self.book(**kw)), "invalid_slot", "not valid")

    def test_booking_blocks(self):
        self.block(TOMORROW, t0="16:00", t1="17:00", reason="Doctor on leave")
        d = self.decide("book_appointment", self.book(time="16:30"))
        self.assertDenied(d, "blocked", "booking block (Doctor on leave)")
        self.assertAuto(self.decide("book_appointment", self.book(time="17:00")))
        self.block(WED, reason="")
        self.assertDenied(self.decide("book_appointment", self.book(WED, "09:00")), "blocked", "booking block")

    def test_a_taken_slot(self):
        self.appt(TOMORROW, "16:00", phone="9000000001", name="Other")
        self.assertDenied(self.decide("book_appointment", self.book()), "slot_taken", "already booked")

    def test_cancelled_appointments_free_their_slot(self):
        self.appt(TOMORROW, "16:00", phone="9000000001", name="Other", status="cancelled")
        self.assertAuto(self.decide("book_appointment", self.book()))

    def test_per_number_cap_of_three_active_bookings(self):
        for t in ("09:00", "09:15"):
            self.mine(TOMORROW, t)
        self.assertAuto(self.decide("book_appointment", self.book()))                           # 2 active -> a 3rd is fine
        self.mine(TOMORROW, "09:30")
        self.assertDenied(self.decide("book_appointment", self.book()), "number_cap", "3 active bookings")

    def test_cap_counts_the_numbers_unregistered_bookings_by_phone_and_ignores_cancelled_and_past(self):
        for t in ("09:00", "09:15", "09:30"):
            self.appt(TOMORROW, t, phone="9111122223", name="Raju")
        self.assertDenied(self.decide("book_appointment", self.unknown_book(), wa_id=WA_OTHER), "number_cap", "3 active")
        self.conn.execute("UPDATE appointments SET status='cancelled' WHERE start_time='09:30'")
        self.assertAuto(self.decide("book_appointment", self.unknown_book(), wa_id=WA_OTHER))

    def test_daily_automation_cap(self):
        settings.set_auto_daily_cap(self.conn, 3)
        self.auto_rows(2)
        self.assertAuto(self.decide("book_appointment", self.book()))
        self.auto_rows(1)
        self.assertDenied(self.decide("book_appointment", self.book()), "daily_cap", "daily automation cap reached")
        self.auto_rows(5, "2026-10-04")                                                          # yesterday's don't count
        settings.set_auto_daily_cap(self.conn, 4)
        self.assertAuto(self.decide("book_appointment", self.book()))
        settings.set_auto_daily_cap(self.conn, 0)
        self.assertDenied(self.decide("book_appointment", self.book()), "daily_cap", "cap reached")

    def test_the_daily_cap_counts_bookings_created_today_not_appointment_dates(self):
        settings.set_auto_daily_cap(self.conn, 1)
        self.auto_rows(1, "2026-10-04")                                                          # made yesterday, for any date
        self.assertAuto(self.decide("book_appointment", self.book(WED)))

    # -- cancel ------------------------------------------------------------------
    def test_cancel_own_appointment_is_automatic(self):
        self.assertAuto(self.decide("cancel_appointment", {"appointment_id": self.mine()}))

    def test_cancel_by_phone_for_an_unregistered_booker(self):
        aid = self.appt(TOMORROW, "09:00", phone="9111122223", name="Raju")
        self.assertAuto(self.decide("cancel_appointment", {"appointment_id": aid}, wa_id=WA_OTHER))

    def test_a_very_late_cancel_is_still_automatic(self):
        aid = self.mine(TODAY, "10:05")                                                          # five minutes away
        self.assertAuto(self.decide("cancel_appointment", {"appointment_id": aid}))
        aid2 = self.mine(TODAY, "10:00")                                                         # starting this minute
        self.assertAuto(self.decide("cancel_appointment", {"appointment_id": aid2}))

    def test_cancel_someone_elses_appointment_is_refused(self):
        aid = self.appt(TOMORROW, "09:00", phone="9000000001", name="Other")
        self.assertDenied(self.decide("cancel_appointment", {"appointment_id": aid}), "not_own", "not under the sender")
        self.assertDenied(self.decide("cancel_appointment", {"appointment_id": self.mine()}, wa_id=WA_OTHER), "not_own", "not under")

    def test_cancel_missing_inactive_checked_in_or_past(self):
        self.assertDenied(self.decide("cancel_appointment", {"appointment_id": 999}), "not_found", "could not be found")
        self.assertDenied(self.decide("cancel_appointment", {"appointment_id": None}), "not_found", "could not be found")
        self.assertDenied(self.decide("cancel_appointment", {"appointment_id": self.mine(status="cancelled")}), "not_active", "already cancelled")
        self.assertDenied(self.decide("cancel_appointment", {"appointment_id": self.mine(status="completed")}), "not_active", "already completed")
        self.assertDenied(self.decide("cancel_appointment", {"appointment_id": self.mine(TODAY, "10:30", queue_state="checked_in")}), "checked_in", "checked in")
        self.assertDenied(self.decide("cancel_appointment", {"appointment_id": self.mine(TODAY, "09:00")}), "started", "already passed")

    def test_cancels_and_reschedules_do_not_count_toward_the_daily_cap(self):
        settings.set_auto_daily_cap(self.conn, 0)
        aid = self.mine()
        self.assertAuto(self.decide("cancel_appointment", {"appointment_id": aid}))
        self.assertAuto(self.decide("reschedule_appointment", {"appointment_id": aid, "appt_date": WED, "start_time": "16:00"}))

    # -- reschedule ----------------------------------------------------------------
    def test_reschedule_own_appointment_to_a_free_slot(self):
        aid = self.mine(TOMORROW, "09:00")
        self.assertAuto(self.decide("reschedule_appointment", {"appointment_id": aid, "appt_date": WED, "start_time": "16:00"}))

    def test_moving_within_ones_own_slot_is_not_a_self_conflict(self):
        aid = self.mine(TOMORROW, "09:00")
        self.assertAuto(self.decide("reschedule_appointment", {"appointment_id": aid, "appt_date": TOMORROW, "start_time": "09:00"}))

    def test_reschedule_target_checks(self):
        aid = self.mine(TOMORROW, "09:00")
        self.appt(WED, "16:00", phone="9000000001", name="Other")
        self.block("2026-10-08", t0="09:00", t1="10:00")
        move = lambda d, t: self.decide("reschedule_appointment", {"appointment_id": aid, "appt_date": d, "start_time": t})
        self.assertDenied(move(WED, "16:00"), "slot_taken", "already booked")
        self.assertDenied(move("2026-10-08", "09:00"), "blocked", "booking block")
        self.assertDenied(move(TODAY, "09:00"), "past", "already passed")
        self.assertDenied(move(WED, "13:00"), "outside_hours", "clinic hours")
        self.assertDenied(move("2026-12-01", "09:00"), "too_far", "30 days")
        self.assertDenied(move(None, "09:00"), "invalid_slot", "not valid")

    def test_reschedule_ownership_and_state(self):
        other = self.appt(TOMORROW, "09:00", phone="9000000001", name="Other")
        mv = lambda aid: self.decide("reschedule_appointment", {"appointment_id": aid, "appt_date": WED, "start_time": "16:00"})
        self.assertDenied(mv(other), "not_own", "not under the sender")
        self.assertDenied(mv(self.mine(status="no_show")), "not_active", "already no_show")
        self.assertDenied(mv(self.mine(TOMORROW, "09:15", queue_state="checked_in")), "checked_in", "checked in")

    def test_decision_is_pure_it_writes_nothing(self):
        before = [self.conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0]
                  for t in ("appointments", "proposals", "audit_log", "patient_activity", "booking_blocks", "slot_holds")]
        self.decide("book_appointment", self.book())
        self.decide("cancel_appointment", {"appointment_id": self.mine()})
        after = [self.conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0]
                 for t in ("appointments", "proposals", "audit_log", "patient_activity", "booking_blocks", "slot_holds")]
        self.assertEqual(after[1:], before[1:])
        self.assertEqual(after[0], before[0] + 1)      # only the fixture appointment this test itself created


if __name__ == "__main__":
    unittest.main()
