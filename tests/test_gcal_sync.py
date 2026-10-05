"""Google Calendar sync engine (clinic/gcal_sync.py), driven entirely by the
in-memory fake in tests/fake_gcal.py. No network, no threads, no sleeping, no
real clock: every time-dependent call takes an injected notify.Now."""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ["WHATSAPP_NOTIFY_MODE"] = "dry_run"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import core, db, gcal_config, gcal_sync, scheduler
from clinic.gcal_client import GcalApiError
from clinic.intents import HANDLERS
from clinic.notify import Now
from tests.fake_gcal import FakeCalendar

SCHEMA = (Path(__file__).resolve().parents[1] / "clinic" / "schema.sql").read_text()
CAL = "clinic-demo@example.com"
DAY1, DAY2, DAY3 = "2026-10-03", "2026-10-04", "2026-10-05"


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def at(day, hour=12, minute=0, second=0):
    """A Now whose local clock is `day` hour:minute and whose UTC is IST - 5:30."""
    local = datetime.strptime(day, "%Y-%m-%d").replace(hour=hour, minute=minute, second=second)
    return Now(local, local - timedelta(hours=5, minutes=30))


NOW = at("2026-10-02")


def run(conn, intent, slots):
    pid = core.propose(conn, intent, slots)
    return core.confirm(conn, pid, HANDLERS)[1]


def book(conn, name, day, start, duration=15, phone="9876543210", notes=None, patient_id=None):
    slots = {"appt_date": day, "start_time": start, "patient_name": name, "patient_phone": phone, "notes": notes}
    if duration:
        slots["duration_minutes"] = duration
    if patient_id:
        slots["patient_id"] = patient_id
    return run(conn, "book_appointment", slots)


def register(conn, name, phone="9000000001", age=42):
    return run(conn, "register_patient", {"name": name, "phone": phone, "age": age})


class SyncTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.fake = FakeCalendar()
        self.addCleanup(self.conn.close)

    def sync(self, day):
        return gcal_sync.resync_date(self.conn, self.fake, day, CAL, NOW)

    def mapping(self, appointment_id):
        return self.conn.execute("SELECT * FROM calendar_events WHERE appointment_id = ?", (appointment_id,)).fetchone()


class PayloadTests(SyncTestCase):
    def test_event_is_minimal_and_in_ist(self):
        pid = register(self.conn, "Sunita Devi", phone="9811122233", age=61)
        aid = book(self.conn, None, DAY1, "09:15", duration=30, phone=None, notes="chest pain, diabetic",
                   patient_id=pid)
        self.sync(DAY1)
        (event,) = self.fake.events.values()
        self.assertEqual(
            set(event), {"id", "summary", "start", "end", "extendedProperties"})
        self.assertEqual(event["summary"], "T-01 · Sunita D.")
        self.assertEqual(event["start"], {"dateTime": "2026-10-03T09:15:00+05:30", "timeZone": "Asia/Kolkata"})
        self.assertEqual(event["end"], {"dateTime": "2026-10-03T09:45:00+05:30", "timeZone": "Asia/Kolkata"})
        self.assertEqual(
            event["extendedProperties"], {"private": {"source": "clinic-copilot", "appointment_id": str(aid)}})
        blob = json.dumps(event)
        for private_detail in ("9811122233", "61", "chest pain", "diabetic", "Devi"):
            self.assertNotIn(private_detail, blob)

    def test_default_duration_is_the_appointment_row_value(self):
        book(self.conn, "Ravi", DAY1, "16:00")
        self.sync(DAY1)
        (event,) = self.fake.events.values()
        self.assertEqual(event["end"]["dateTime"], "2026-10-03T16:15:00+05:30")

    def test_event_end_may_cross_midnight(self):
        entry = {"id": 1, "appt_date": DAY1, "start_time": "23:50", "duration_minutes": 30, "status": "booked",
                 "token_label": "T-01", "name": "Late Patient"}
        body = gcal_sync.build_event_body(entry)
        self.assertEqual(body["end"]["dateTime"], "2026-10-04T00:20:00+05:30")

    def test_name_formatting(self):
        cases = {
            "Sunita Devi": "Sunita D.", "sunita devi": "sunita D.", "Sunita": "Sunita",
            "  Ramesh   Kumar  Singh ": "Ramesh S.", "": "Patient", None: "Patient", "   ": "Patient",
            "सुनीता देवी": "सुनीता द.",
        }
        for name, expected in cases.items():
            self.assertEqual(gcal_sync.display_name(name), expected, repr(name))

    def test_unregistered_booker_uses_patient_name(self):
        book(self.conn, "Asha Verma", DAY1, "09:00")
        self.sync(DAY1)
        self.assertEqual(self.fake.titles(), ["T-01 · Asha V."])

    def test_unknown_name_is_just_patient(self):
        book(self.conn, None, DAY1, "09:00")
        self.sync(DAY1)
        self.assertEqual(self.fake.titles(), ["T-01 · Patient"])

    def test_registered_patient_name_wins_over_booking_name(self):
        pid = register(self.conn, "Meena Kapoor")
        book(self.conn, "someone else", DAY1, "09:00", patient_id=pid)
        self.sync(DAY1)
        self.assertEqual(self.fake.titles(), ["T-01 · Meena K."])

    def test_completed_and_no_show_are_coloured_and_stay_on_the_calendar(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        b = book(self.conn, "Bela Shah", DAY1, "09:15")
        c = book(self.conn, "Chitra Rao", DAY1, "09:30")
        self.sync(DAY1)
        self.assertFalse(any("colorId" in e for e in self.fake.events.values()))
        run(self.conn, "queue_mark_done", {"appointment_id": a})
        run(self.conn, "queue_mark_no_show", {"appointment_id": b})
        self.sync(DAY1)
        self.assertEqual(len(self.fake.events), 3)
        self.assertEqual(self.fake.find(a)[0]["colorId"], "8")
        self.assertEqual(self.fake.find(b)[0]["colorId"], "11")
        self.assertNotIn("colorId", self.fake.find(c)[0])
        # tokens keep their rank once someone is done (clinic/token_queue.py)
        self.assertEqual(self.fake.titles(), ["T-01 · Asha V.", "T-02 · Bela S.", "T-03 · Chitra R."])

    def test_unreadable_time_is_reported_but_does_not_block_the_day(self):
        book(self.conn, "Asha Verma", DAY1, "09:00")
        self.conn.execute(
            "INSERT INTO appointments (patient_name, appt_date, start_time) VALUES ('Broken', ?, 'soon')", (DAY1,))
        self.conn.commit()
        result = self.sync(DAY1)
        self.assertEqual(len(self.fake.events), 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("cannot build event", result["errors"][0])


class ReconcileTests(SyncTestCase):
    def test_creates_events_and_mappings_then_is_idempotent(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        b = book(self.conn, "Bela Shah", DAY1, "09:15")
        first = self.sync(DAY1)
        self.assertEqual((first["created"], first["errors"]), (2, []))
        self.assertEqual(self.fake.titles(), ["T-01 · Asha V.", "T-02 · Bela S."])
        for aid in (a, b):
            row = self.mapping(aid)
            self.assertEqual((row["calendar_id"], row["appt_date"], row["status"]), (CAL, DAY1, "synced"))
            self.assertEqual(row["gcal_event_id"], self.fake.find(aid)[0]["id"])
        self.fake.clear_calls()
        second = self.sync(DAY1)
        self.assertEqual((second["created"], second["patched"], second["deleted"], second["unchanged"]), (0, 0, 0, 2))
        self.assertEqual([c[0] for c in self.fake.calls], ["list"])  # nothing written

    def test_time_change_patches_the_same_event(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        self.sync(DAY1)
        event_id = self.fake.find(a)[0]["id"]
        run(self.conn, "reschedule_appointment", {"appointment_id": a, "appt_date": DAY1, "start_time": "10:30"})
        result = self.sync(DAY1)
        self.assertEqual((result["patched"], result["created"]), (1, 0))
        (event,) = self.fake.events.values()
        self.assertEqual(event["id"], event_id)
        self.assertEqual(event["start"]["dateTime"], "2026-10-03T10:30:00+05:30")
        self.assertEqual(event["end"]["dateTime"], "2026-10-03T10:45:00+05:30")

    def test_cancel_deletes_the_event_and_retitles_everyone_behind(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        b = book(self.conn, "Bela Shah", DAY1, "09:15")
        c = book(self.conn, "Chitra Rao", DAY1, "09:30")
        self.sync(DAY1)
        self.assertEqual(self.fake.titles(), ["T-01 · Asha V.", "T-02 · Bela S.", "T-03 · Chitra R."])
        run(self.conn, "cancel_appointment", {"appointment_id": a})
        result = self.sync(DAY1)
        self.assertEqual((result["deleted"], result["patched"]), (1, 2))
        self.assertEqual(self.fake.titles(), ["T-01 · Bela S.", "T-02 · Chitra R."])
        self.assertEqual(self.fake.find(a), [])
        self.assertIsNone(self.mapping(a))
        self.assertIsNotNone(self.mapping(b))

    def test_an_earlier_booking_renumbers_the_people_after_it(self):
        book(self.conn, "Bela Shah", DAY1, "10:00")
        book(self.conn, "Chitra Rao", DAY1, "10:15")
        self.sync(DAY1)
        book(self.conn, "Asha Verma", DAY1, "09:00")
        self.sync(DAY1)
        self.assertEqual(self.fake.titles(), ["T-01 · Asha V.", "T-02 · Bela S.", "T-03 · Chitra R."])

    def test_reschedule_to_another_day_moves_the_event_whichever_day_syncs_first(self):
        for first, second in ((DAY1, DAY2), (DAY2, DAY1)):
            with self.subTest(first_synced=first):
                self.conn.close()
                self.setUp()
                a = book(self.conn, "Asha Verma", DAY1, "09:00")
                b = book(self.conn, "Bela Shah", DAY1, "09:15")
                self.sync(DAY1)
                self.assertEqual(self.fake.titles(DAY1), ["T-01 · Asha V.", "T-02 · Bela S."])
                run(self.conn, "reschedule_appointment", {"appointment_id": a, "appt_date": DAY2, "start_time": "11:00"})
                self.sync(first)
                self.sync(second)
                self.assertEqual(self.fake.titles(DAY1), ["T-01 · Bela S."])      # Bela moved up
                self.assertEqual(self.fake.titles(DAY2), ["T-01 · Asha V."])
                self.assertEqual(len(self.fake.find(a)), 1)
                self.assertEqual(self.mapping(a)["appt_date"], DAY2)
                self.assertEqual(self.mapping(a)["gcal_event_id"], self.fake.find(a)[0]["id"])

    def test_the_queued_appointment_job_reconciles_both_the_old_and_the_new_day(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        book(self.conn, "Bela Shah", DAY1, "09:15")
        self.sync(DAY1)
        run(self.conn, "reschedule_appointment", {"appointment_id": a, "appt_date": DAY2, "start_time": "11:00"})
        gcal_sync.enqueue(self.conn, "appointment", a, NOW)
        summary = gcal_sync.drain(self.conn, self.fake, NOW, CAL)
        self.assertEqual((summary["done"], summary["errors"]), (1, []))
        self.assertEqual(self.fake.titles(DAY1), ["T-01 · Bela S."])
        self.assertEqual(self.fake.titles(DAY2), ["T-01 · Asha V."])

    def test_event_deleted_by_hand_is_recreated(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        self.sync(DAY1)
        old_id = self.fake.find(a)[0]["id"]
        self.fake.external_delete(old_id)
        result = self.sync(DAY1)
        self.assertEqual(result["created"], 1)
        (event,) = self.fake.events.values()
        self.assertNotEqual(event["id"], old_id)
        self.assertEqual(self.mapping(a)["gcal_event_id"], event["id"])

    def test_event_edited_by_hand_is_put_back(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        self.sync(DAY1)
        event = self.fake.find(a)[0]
        event["summary"] = "renamed by someone"
        event["start"]["dateTime"] = "2026-10-03T14:00:00+05:30"
        event["end"]["dateTime"] = "2026-10-03T14:15:00+05:30"
        self.sync(DAY1)
        self.assertEqual(self.fake.titles(), ["T-01 · Asha V."])
        self.assertEqual(self.fake.find(a)[0]["start"]["dateTime"], "2026-10-03T09:00:00+05:30")

    def test_patch_404_because_the_event_vanished_recreates_it(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        self.sync(DAY1)
        run(self.conn, "reschedule_appointment", {"appointment_id": a, "appt_date": DAY1, "start_time": "10:00"})
        # The event disappears between the list and the patch.
        self.fake.before_patch = self.fake.external_delete
        result = self.sync(DAY1)
        self.assertEqual((result["created"], result["errors"]), (1, []))
        (event,) = self.fake.events.values()
        self.assertEqual(event["start"]["dateTime"], "2026-10-03T10:00:00+05:30")
        self.assertEqual(self.mapping(a)["gcal_event_id"], event["id"])

    def test_delete_404_and_410_count_as_done(self):
        for status in (404, 410):
            with self.subTest(status=status):
                self.conn.close()
                self.setUp()
                a = book(self.conn, "Asha Verma", DAY1, "09:00")
                self.sync(DAY1)
                run(self.conn, "cancel_appointment", {"appointment_id": a})
                self.fake.fail_next("delete", GcalApiError(status, "gone"))
                result = self.sync(DAY1)
                self.assertEqual(result["errors"], [])
                self.assertIsNone(self.mapping(a))
                # the (fake) event survived our injected failure; the next pass removes it for real
                self.sync(DAY1)
                self.assertEqual(self.fake.events, {})

    def test_cancelled_appointment_whose_event_was_already_deleted_by_hand(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        self.sync(DAY1)
        self.fake.external_delete(self.fake.find(a)[0]["id"])
        run(self.conn, "cancel_appointment", {"appointment_id": a})
        result = self.sync(DAY1)
        self.assertEqual(result["errors"], [])
        self.assertIsNone(self.mapping(a))   # the stale mapping is cleared

    def test_a_duplicate_event_is_removed(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        self.sync(DAY1)
        original = self.fake.find(a)[0]
        self.fake.external_add(original["summary"], original["start"]["dateTime"], original["end"]["dateTime"],
                               private={"source": "clinic-copilot", "appointment_id": str(a)})
        self.assertEqual(len(self.fake.find(a)), 2)
        self.sync(DAY1)
        self.assertEqual([e["id"] for e in self.fake.find(a)], [original["id"]])
        self.assertEqual(self.mapping(a)["gcal_event_id"], original["id"])

    def test_an_event_with_no_mapping_is_adopted_not_duplicated(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        self.sync(DAY1)
        self.conn.execute("DELETE FROM calendar_events")
        self.conn.commit()
        self.sync(DAY1)
        self.assertEqual(len(self.fake.find(a)), 1)
        self.assertIsNotNone(self.mapping(a))

    def test_events_that_are_not_ours_are_never_touched(self):
        book(self.conn, "Asha Verma", DAY1, "09:00")
        manual = self.fake.external_add("Doctor lunch", "2026-10-03T13:00:00+05:30", "2026-10-03T14:00:00+05:30")
        other_app = self.fake.external_add(
            "Other", "2026-10-03T15:00:00+05:30", "2026-10-03T16:00:00+05:30", private={"source": "someone-else"})
        unattributable = self.fake.external_add(
            "Marker but no id", "2026-10-03T17:00:00+05:30", "2026-10-03T18:00:00+05:30",
            private={"source": "clinic-copilot"})
        self.sync(DAY1)
        self.sync(DAY1)
        for event_id in (manual, other_app, unattributable):
            self.assertIn(event_id, self.fake.events)

    def test_an_event_spanning_midnight_belongs_to_its_start_day_only(self):
        a = book(self.conn, "Late Patient", DAY1, "23:45", duration=30)
        self.sync(DAY1)
        self.sync(DAY2)   # the event overlaps DAY2's window but starts on DAY1
        self.assertEqual(len(self.fake.find(a)), 1)

    def test_one_failure_does_not_stop_the_rest_and_the_next_pass_heals_it(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        b = book(self.conn, "Bela Shah", DAY1, "09:15")
        self.fake.fail_next("insert", GcalApiError(500, "backend error"))
        result = self.sync(DAY1)
        self.assertEqual((result["created"], len(result["errors"])), (1, 1))
        self.assertIn("appointment #{}".format(a), result["errors"][0])
        self.assertEqual(len(self.fake.events), 1)
        self.assertEqual(self.fake.find(b)[0]["summary"], "T-02 · Bela S.")
        healed = self.sync(DAY1)
        self.assertEqual((healed["created"], healed["errors"]), (1, []))
        self.assertEqual(len(self.fake.events), 2)

    def test_a_failed_update_is_noted_on_the_mapping(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        self.sync(DAY1)
        run(self.conn, "reschedule_appointment", {"appointment_id": a, "appt_date": DAY1, "start_time": "10:00"})
        self.fake.fail_next("patch", GcalApiError(503, "try later"))
        result = self.sync(DAY1)
        self.assertEqual(len(result["errors"]), 1)
        row = self.mapping(a)
        self.assertEqual(row["status"], "error")
        self.assertIn("try later", row["last_error"])
        self.sync(DAY1)
        self.assertEqual((self.mapping(a)["status"], self.mapping(a)["last_error"]), ("synced", None))

    def test_listing_failure_raises_from_resync_date(self):
        book(self.conn, "Asha Verma", DAY1, "09:00")
        self.fake.fail_always("list", GcalApiError(404, "Not Found"))
        with self.assertRaises(GcalApiError):
            self.sync(DAY1)
        self.assertEqual(self.fake.events, {})

    def test_a_different_calendar_id_starts_fresh(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        self.sync(DAY1)
        gcal_sync.resync_date(self.conn, self.fake, DAY1, "other@example.com", NOW)
        self.assertEqual(self.mapping(a)["calendar_id"], "other@example.com")
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "insert"]), 2)

    def test_resync_range_uses_one_list_call(self):
        book(self.conn, "Asha Verma", DAY1, "09:00")
        book(self.conn, "Bela Shah", DAY3, "09:00")
        results = gcal_sync.resync_range(self.conn, self.fake, DAY1, DAY3, CAL, NOW)
        self.assertEqual(sorted(results), [DAY1, DAY2, DAY3])
        self.assertEqual(len(self.fake.ops("list")), 1)
        self.assertEqual(len(self.fake.events), 2)

    def test_resync_range_removes_an_orphan_and_recreates_a_dragged_event(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        gcal_sync.resync_range(self.conn, self.fake, DAY1, DAY3, CAL, NOW)
        event = self.fake.find(a)[0]
        event["start"]["dateTime"] = "2026-10-05T09:00:00+05:30"   # someone drags it to another day
        event["end"]["dateTime"] = "2026-10-05T09:15:00+05:30"
        gcal_sync.resync_range(self.conn, self.fake, DAY1, DAY3, CAL, NOW)
        self.assertEqual(self.fake.titles(DAY3), [])
        self.assertEqual(self.fake.titles(DAY1), ["T-01 · Asha V."])
        self.assertEqual(len(self.fake.find(a)), 1)


class QueueAndHookTests(SyncTestCase):
    def test_enqueue_deduplicates_pending_rows_and_validates_kind(self):
        self.assertIsNotNone(gcal_sync.enqueue(self.conn, "appointment", 5, NOW))
        self.assertIsNone(gcal_sync.enqueue(self.conn, "appointment", 5, NOW))
        self.assertIsNotNone(gcal_sync.enqueue(self.conn, "appointment", 6, NOW))
        self.assertIsNotNone(gcal_sync.enqueue(self.conn, "full", "", NOW))
        self.assertIsNone(gcal_sync.enqueue(self.conn, "full", "", NOW))
        with self.assertRaises(ValueError):
            gcal_sync.enqueue(self.conn, "bogus", "", NOW)

    def rows(self):
        return [(r["kind"], r["target"], r["status"]) for r in
                self.conn.execute("SELECT * FROM calendar_sync_queue ORDER BY id")]

    def test_hook_queues_the_affected_appointment_for_calendar_intents_only(self):
        kicks = []
        with patch.object(gcal_config, "is_configured", return_value=True):
            for intent, slots, entity in (
                ("book_appointment", {"appt_date": DAY1}, 11),
                ("cancel_appointment", {"appointment_id": 12}, 12),
                ("reschedule_appointment", {"appointment_id": "13"}, 13),
                ("queue_mark_done", {"appointment_id": 14}, 14),
                ("queue_mark_no_show", {"appointment_id": 15}, 15),
            ):
                gcal_sync.post_commit(self.conn, intent, slots, entity, kick=lambda: kicks.append(1), now=NOW)
            for intent, slots in (
                ("queue_check_in", {"appointment_id": 16}), ("queue_call_next", {"appointment_id": 16}),
                ("register_patient", {}), ("log_expense", {}), ("record_visit", {}),
            ):
                gcal_sync.post_commit(self.conn, intent, slots, 99, kick=lambda: kicks.append(1), now=NOW)
        self.assertEqual(self.rows(), [("appointment", str(i), "pending") for i in (11, 12, 13, 14, 15)])
        self.assertEqual(len(kicks), 5)

    def test_hook_does_nothing_when_not_configured(self):
        kicks = []
        with patch.object(gcal_config, "is_configured", return_value=False):
            gcal_sync.post_commit(self.conn, "book_appointment", {}, 1, kick=lambda: kicks.append(1), now=NOW)
        self.assertEqual((self.rows(), kicks), ([], []))

    def test_hook_never_raises(self):
        def boom():
            raise RuntimeError("cannot start worker")
        with patch.object(gcal_config, "is_configured", return_value=True):
            gcal_sync.post_commit(self.conn, "book_appointment", {}, 1, kick=boom, now=NOW)   # kick fails
            gcal_sync.post_commit(self.conn, "book_appointment", {}, "not a number", kick=None, now=NOW)
            closed = make_db()
            closed.close()
            gcal_sync.post_commit(closed, "book_appointment", {}, 2, kick=None, now=NOW)   # database unusable

    def test_drain_processes_queued_appointments_and_updates_status(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        gcal_sync.enqueue(self.conn, "appointment", a, NOW)
        summary = gcal_sync.drain(self.conn, self.fake, NOW, CAL)
        self.assertEqual((summary["rows"], summary["done"], summary["errors"]), (1, 1, []))
        self.assertEqual(self.fake.titles(), ["T-01 · Asha V."])
        self.assertEqual(self.rows(), [("appointment", str(a), "done")])
        with patch.object(gcal_config, "is_configured", return_value=True):
            status = gcal_sync.sync_status(self.conn)
        self.assertEqual((status["pending"], status["failed"], status["last_error"]), (0, 0, None))
        self.assertEqual(status["last_sync_at"], "2026-10-02 06:30:00")
        self.assertEqual(status["last_sync_local"], "2026-10-02 12:00")

    def test_two_changes_to_one_day_cost_one_reconcile(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        b = book(self.conn, "Bela Shah", DAY1, "09:15")
        gcal_sync.enqueue(self.conn, "appointment", a, NOW)
        gcal_sync.enqueue(self.conn, "appointment", b, NOW)
        gcal_sync.drain(self.conn, self.fake, NOW, CAL)
        self.assertEqual(len(self.fake.ops("list")), 1)
        self.assertEqual(len(self.fake.events), 2)

    def test_failures_are_retried_a_bounded_number_of_times(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        gcal_sync.enqueue(self.conn, "appointment", a, NOW)
        self.fake.fail_always("list", GcalApiError(503, "Google is down"))
        for attempt in range(1, gcal_sync.MAX_ATTEMPTS):
            summary = gcal_sync.drain(self.conn, self.fake, NOW, CAL)
            row = self.conn.execute("SELECT * FROM calendar_sync_queue").fetchone()
            self.assertEqual((row["status"], row["attempts"]), ("pending", attempt))
            self.assertEqual((summary["retry"], summary["failed"]), (1, 0))
            self.assertIn("Google is down", row["error"])
        gave_up = gcal_sync.drain(self.conn, self.fake, NOW, CAL)
        self.assertEqual(gave_up["failed"], 1)
        row = self.conn.execute("SELECT * FROM calendar_sync_queue").fetchone()
        self.assertEqual((row["status"], row["attempts"]), ("failed", gcal_sync.MAX_ATTEMPTS))
        calls_before = len(self.fake.calls)
        gcal_sync.drain(self.conn, self.fake, NOW, CAL)       # a failed row is not touched again
        self.assertEqual(len(self.fake.calls), calls_before)
        with patch.object(gcal_config, "is_configured", return_value=True):
            status = gcal_sync.sync_status(self.conn)
        self.assertEqual((status["pending"], status["failed"]), (0, 1))
        self.assertIn("Google is down", status["last_error"])

    def test_a_later_clean_pass_clears_the_error(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        gcal_sync.enqueue(self.conn, "appointment", a, NOW)
        self.fake.fail_next("list", GcalApiError(500, "hiccup"))
        gcal_sync.drain(self.conn, self.fake, NOW, CAL)
        self.assertIn("hiccup", gcal_sync.sync_status(self.conn)["last_error"])
        gcal_sync.drain(self.conn, self.fake, NOW, CAL)
        status = gcal_sync.sync_status(self.conn)
        self.assertIsNone(status["last_error"])
        self.assertIsNotNone(status["last_sync_at"])
        self.assertEqual(len(self.fake.events), 1)

    def test_when_google_cannot_be_listed_the_remaining_rows_are_not_charged_an_attempt(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        b = book(self.conn, "Bela Shah", DAY3, "09:00")
        gcal_sync.enqueue(self.conn, "appointment", a, NOW)
        gcal_sync.enqueue(self.conn, "appointment", b, NOW)
        self.fake.fail_always("list", GcalApiError(404, "Not Found"))
        gcal_sync.drain(self.conn, self.fake, NOW, CAL)
        self.assertEqual(len(self.fake.ops("list")), 1)        # stopped after the first failure
        attempts = [r["attempts"] for r in self.conn.execute("SELECT attempts FROM calendar_sync_queue ORDER BY id")]
        self.assertEqual(attempts, [1, 0])

    def test_a_non_google_exception_is_contained(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        gcal_sync.enqueue(self.conn, "appointment", a, NOW)
        self.fake.fail_always("list", RuntimeError("unexpected"))
        summary = gcal_sync.drain(self.conn, self.fake, NOW, CAL)      # does not raise
        self.assertEqual(summary["retry"], 1)

    def test_claims_left_by_a_crashed_worker_are_picked_up(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        row_id = gcal_sync.enqueue(self.conn, "appointment", a, NOW)
        self.conn.execute("UPDATE calendar_sync_queue SET status = 'processing' WHERE id = ?", (row_id,))
        self.conn.commit()
        gcal_sync.drain(self.conn, self.fake, NOW, CAL)
        self.assertEqual(self.rows(), [("appointment", str(a), "done")])

    def test_a_second_drain_while_one_is_running_returns_at_once(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        gcal_sync.enqueue(self.conn, "appointment", a, NOW)
        self.assertTrue(gcal_sync._DRAIN_LOCK.acquire(blocking=False))
        try:
            summary = gcal_sync.drain(self.conn, self.fake, NOW, CAL)
        finally:
            gcal_sync._DRAIN_LOCK.release()
        self.assertEqual(summary["skipped"], "busy")
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.rows(), [("appointment", str(a), "pending")])

    def test_drain_without_a_client_is_a_no_op(self):
        gcal_sync.enqueue(self.conn, "full", "", NOW)
        summary = gcal_sync.drain(self.conn, None, NOW)
        self.assertEqual(summary["skipped"], "not configured")
        self.assertEqual(self.rows(), [("full", "", "pending")])

    def test_rows_enqueued_while_a_batch_runs_are_picked_up_in_the_same_drain(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        b = book(self.conn, "Bela Shah", DAY3, "09:00")
        gcal_sync.enqueue(self.conn, "appointment", a, NOW)
        original = self.fake.list_events

        def list_then_enqueue(*args):
            events = original(*args)
            if len(self.fake.ops("list")) == 1:
                gcal_sync.enqueue(self.conn, "appointment", b, NOW)   # a write lands mid-drain
            return events
        self.fake.list_events = list_then_enqueue
        gcal_sync.drain(self.conn, self.fake, NOW, CAL)
        self.assertEqual(len(self.fake.events), 2)
        self.assertEqual({r[2] for r in self.rows()}, {"done"})

    def test_a_full_reconcile_covers_today_through_thirty_days_with_one_list_call(self):
        today = NOW.today.isoformat()
        inside = book(self.conn, "Asha Verma", "2026-10-02", "09:00")
        edge = book(self.conn, "Bela Shah", "2026-11-01", "09:00")          # today + 30
        outside = book(self.conn, "Chitra Rao", "2026-11-02", "09:00")      # today + 31
        gcal_sync.enqueue(self.conn, "full", "", NOW)
        summary = gcal_sync.drain(self.conn, self.fake, NOW, CAL)
        self.assertEqual((summary["done"], summary["errors"]), (1, []))
        self.assertEqual(len(self.fake.ops("list")), 1)
        self.assertEqual(self.fake.ops("list")[0][2:], ("2026-10-02T00:00:00+05:30", "2026-11-02T00:00:00+05:30"))
        self.assertEqual(len(self.fake.find(inside)), 1)
        self.assertEqual(len(self.fake.find(edge)), 1)
        self.assertEqual(self.fake.find(outside), [])
        self.assertEqual(today, "2026-10-02")


class TickTests(SyncTestCase):
    def test_tick_without_a_client_does_nothing(self):
        summary = gcal_sync.tick(self.conn, None, NOW)
        self.assertEqual(summary, {"configured": False, "enqueued_full": False, "drain": None})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM calendar_sync_queue").fetchone()[0], 0)

    def test_first_tick_queues_and_runs_a_full_reconcile(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        summary = gcal_sync.tick(self.conn, self.fake, NOW)
        self.assertTrue(summary["enqueued_full"])
        self.assertEqual(summary["drain"]["done"], 1)
        self.assertEqual(len(self.fake.find(a)), 1)

    def test_periodic_full_reconcile_runs_every_ten_minutes_not_more_often(self):
        book(self.conn, "Asha Verma", DAY1, "09:00")
        gcal_sync.tick(self.conn, self.fake, NOW)
        self.fake.clear_calls()
        for minutes in (1, 5, 9):
            later = at("2026-10-02", 12, minutes)
            self.assertFalse(gcal_sync.tick(self.conn, self.fake, later)["enqueued_full"])
        self.assertEqual(self.fake.calls, [])
        again = gcal_sync.tick(self.conn, self.fake, at("2026-10-02", 12, 10))
        self.assertTrue(again["enqueued_full"])
        self.assertEqual(len(self.fake.ops("list")), 1)

    def test_the_periodic_reconcile_heals_drift_and_earlier_failures(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        self.fake.fail_next("insert", GcalApiError(500, "backend error"))
        gcal_sync.tick(self.conn, self.fake, NOW)
        self.assertEqual(self.fake.events, {})
        gcal_sync.tick(self.conn, self.fake, at("2026-10-02", 12, 11))
        self.assertEqual(len(self.fake.find(a)), 1)

    def test_tick_never_raises_even_when_everything_fails(self):
        book(self.conn, "Asha Verma", DAY1, "09:00")
        self.fake.fail_always("list", RuntimeError("network exploded"))
        gcal_sync.tick(self.conn, self.fake, NOW)
        with patch.object(gcal_sync, "drain", side_effect=RuntimeError("bug")):
            gcal_sync.tick(self.conn, self.fake, at("2026-10-02", 13))

    def test_old_finished_rows_are_pruned(self):
        old = gcal_sync.enqueue(self.conn, "date", DAY1, at("2026-09-28"))
        keep = gcal_sync.enqueue(self.conn, "date", DAY2, at("2026-10-01"))
        self.conn.execute("UPDATE calendar_sync_queue SET status = 'done'")
        self.conn.commit()
        gcal_sync.prune(self.conn, NOW)
        ids = [r["id"] for r in self.conn.execute("SELECT id FROM calendar_sync_queue")]
        self.assertEqual(ids, [keep])
        self.assertNotIn(old, ids)

    def test_scheduler_tick_runs_the_calendar_step_last(self):
        a = book(self.conn, "Asha Verma", DAY1, "09:00")
        summary = scheduler.tick(self.conn, None, dry_run=True, now=NOW, calendar_client=self.fake)
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(summary["calendar"]["drain"]["done"], 1)
        self.assertEqual(len(self.fake.find(a)), 1)

    def test_scheduler_tick_skips_the_calendar_without_a_client(self):
        book(self.conn, "Asha Verma", DAY1, "09:00")
        summary = scheduler.tick(self.conn, None, dry_run=True, now=NOW)
        self.assertIsNone(summary["calendar"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM calendar_sync_queue").fetchone()[0], 0)

    def test_a_failing_calendar_step_does_not_affect_notifications_or_kill_the_tick(self):
        book(self.conn, "Asha Verma", DAY1, "09:00")
        with patch.object(gcal_sync, "tick", side_effect=RuntimeError("calendar bug")):
            summary = scheduler.tick(self.conn, None, dry_run=True, now=NOW, calendar_client=self.fake)
        self.assertEqual(summary["errors"], 1)
        self.assertIsNone(summary["calendar"])
        self.assertIn("flushed", summary)

    def test_the_scheduler_thread_survives_a_client_factory_that_raises(self):
        # start() is never called with a real thread in tests: drive one loop turn by hand.
        stop = threading.Event()
        turns = []

        def factory():
            turns.append(1)
            stop.set()      # one turn only
            raise RuntimeError("cannot build client")

        with patch.object(scheduler.threading, "Thread") as fake_thread:
            scheduler.start(make_db, calendar_client=factory, stop_event=stop)
            loop = fake_thread.call_args.kwargs["target"]
        loop()      # returns normally: the exception was logged and swallowed
        self.assertEqual(turns, [1])


class StatusTests(SyncTestCase):
    def test_status_when_not_configured(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GOOGLE_SERVICE_ACCOUNT_FILE", None)
            status = gcal_sync.sync_status(self.conn)
        self.assertEqual(
            status,
            {"configured": False, "calendar_id": "clinic-demo@example.com", "last_sync_at": None,
             "last_sync_local": None, "pending": 0, "failed": 0, "last_error": None})

    def test_status_counts_pending_processing_and_failed(self):
        for target in (1, 2, 3, 4):
            gcal_sync.enqueue(self.conn, "appointment", target, NOW)
        self.conn.execute("UPDATE calendar_sync_queue SET status = 'processing' WHERE target = '2'")
        self.conn.execute("UPDATE calendar_sync_queue SET status = 'failed' WHERE target = '3'")
        self.conn.execute("UPDATE calendar_sync_queue SET status = 'done' WHERE target = '4'")
        self.conn.commit()
        status = gcal_sync.sync_status(self.conn)
        self.assertEqual((status["pending"], status["failed"]), (2, 1))

    def test_status_uses_the_environment_for_configuration(self):
        with tempfile.NamedTemporaryFile(suffix=".json") as key:
            with patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_FILE": key.name, "GOOGLE_CALENDAR_ID": "clinic@example.com"}):
                status = gcal_sync.sync_status(self.conn)
        self.assertTrue(status["configured"])
        self.assertEqual(status["calendar_id"], "clinic@example.com")


class ConfigTests(unittest.TestCase):
    def test_configured_needs_an_existing_key_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key.json"
            for env, expected in (
                ({}, False),
                ({"GOOGLE_SERVICE_ACCOUNT_FILE": ""}, False),
                ({"GOOGLE_SERVICE_ACCOUNT_FILE": str(key)}, False),        # path set, file missing
                ({"GOOGLE_SERVICE_ACCOUNT_FILE": tmp}, False),             # a directory is not a key file
            ):
                with patch.dict(os.environ, env, clear=True):
                    self.assertEqual(gcal_config.is_configured(), expected, env)
            key.write_text("{}")
            # a key file alone is not enough: the calendar address must be set too
            with patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_FILE": str(key)}, clear=True):
                self.assertFalse(gcal_config.is_configured())
            with patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_FILE": str(key), "GOOGLE_CALENDAR_ID": CAL, "GOOGLE_CALENDAR_SYNC": "1"}, clear=True):
                self.assertTrue(gcal_config.is_configured())

    def test_sync_is_off_unless_switched_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key.json"
            key.write_text("{}")
            base = {"GOOGLE_SERVICE_ACCOUNT_FILE": str(key), "GOOGLE_CALENDAR_ID": CAL}
            for flag, expected in ((None, False), ("", False), ("0", False), ("no", False), ("1", True), ("true", True), ("ON", True)):
                env = dict(base, **({"GOOGLE_CALENDAR_SYNC": flag} if flag is not None else {}))
                with patch.dict(os.environ, env, clear=True):
                    self.assertEqual(gcal_config.is_configured(), expected, flag)

    def test_the_key_file_is_never_opened_to_decide(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key.json"
            key.write_text("not read")
            key.chmod(0)
            try:
                with patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_FILE": str(key), "GOOGLE_CALENDAR_ID": CAL, "GOOGLE_CALENDAR_SYNC": "1"}, clear=True):
                    self.assertTrue(gcal_config.is_configured())
            finally:
                key.chmod(0o600)

    def test_there_is_no_built_in_calendar_and_the_id_comes_from_the_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gcal_config.calendar_id(), "")
        with patch.dict(os.environ, {"GOOGLE_CALENDAR_ID": " other@example.com "}, clear=True):
            self.assertEqual(gcal_config.calendar_id(), "other@example.com")

    def test_embed_urls(self):
        with patch.dict(os.environ, {"GOOGLE_CALENDAR_ID": CAL}, clear=True):
            for mode in ("WEEK", "MONTH", "AGENDA"):
                url = gcal_config.embed_url(mode)
                self.assertTrue(url.startswith("https://calendar.google.com/calendar/embed?src=clinic-demo%40example.com&"))
                self.assertIn("ctz=Asia%2FKolkata", url)
                self.assertIn("mode=" + mode, url)
            self.assertIn("mode=WEEK", gcal_config.embed_url("quarter"))      # not available in the embed
            self.assertIn("mode=MONTH", gcal_config.embed_url("month"))


class SchemaTests(unittest.TestCase):
    def test_the_new_tables_are_additive_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "scratch.db")
            old = sqlite3.connect(path)
            old.executescript(
                "CREATE TABLE appointments (id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id INTEGER, patient_name TEXT, "
                "patient_phone TEXT, appt_date TEXT NOT NULL, start_time TEXT NOT NULL, "
                "duration_minutes INTEGER NOT NULL DEFAULT 15, status TEXT NOT NULL DEFAULT 'booked', notes TEXT, "
                "created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT);"
                "INSERT INTO appointments (patient_name, appt_date, start_time) VALUES ('Old Row', '2026-10-03', '09:00');")
            old.commit()
            old.close()
            for _ in range(2):
                conn = db.connect(path)
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                self.assertTrue({"calendar_events", "calendar_sync_queue", "calendar_sync_state"} <= tables)
                self.assertEqual(conn.execute("SELECT patient_name FROM appointments").fetchone()[0], "Old Row")
                conn.close()


if __name__ == "__main__":
    unittest.main()
