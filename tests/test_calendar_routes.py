"""Appointments tab + Google Calendar sync through the Flask routes (Flask's
test client only -- never the running app), with the in-memory fake calendar
in place of Google. A scratch database per test; a tripwire on the real
WhatsApp client and on the real Google client."""
import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import test_app_routes
from tests.fake_gcal import FakeCalendar

from clinic import gcal_client, gcal_config, gcal_sync
from clinic.gcal_client import GcalApiError

clinic_app = test_app_routes.clinic_app
TODAY = date.today().isoformat()


class CalendarRouteCase(test_app_routes.RouteTestCase):
    configured = True
    inline = True       # run the drain inside the request, so tests are deterministic

    def setUp(self):
        super().setUp()
        self.fake = FakeCalendar()
        env = {"GOOGLE_SERVICE_ACCOUNT_FILE": "/tmp/not-a-real-key.json"} if self.configured else {}
        env_patch = mock.patch.dict(os.environ, env, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        if not self.configured:
            os.environ.pop("GOOGLE_SERVICE_ACCOUNT_FILE", None)
        # "Configured" means the key file exists; no real file is needed because the
        # fake client is injected and nothing ever opens the path.
        configured_patch = mock.patch.object(gcal_config, "is_configured", return_value=self.configured)
        configured_patch.start()
        self.addCleanup(configured_patch.stop)
        # Tripwire: the real Google client must never be built or used.
        tripwire = mock.patch.object(
            gcal_client, "get_client", side_effect=AssertionError("real Google client requested"))
        tripwire.start()
        self.addCleanup(tripwire.stop)
        clinic_app.GCAL_CLIENT = self.fake
        self.addCleanup(setattr, clinic_app, "GCAL_CLIENT", None)
        clinic_app.GCAL_BACKGROUND = (lambda fn: fn()) if self.inline else (lambda fn: None)
        self.addCleanup(setattr, clinic_app, "GCAL_BACKGROUND", None)

    def queue_rows(self):
        return [(r["kind"], r["target"], r["status"]) for r in
                self.conn.execute("SELECT * FROM calendar_sync_queue ORDER BY id")]

    def approve(self, intent, slots):
        return self.client.post("/approve", json={"intent": intent, "slots": slots, "language": "en-IN"}).get_json()


class SyncAfterApprovalTests(CalendarRouteCase):
    def test_an_approved_booking_appears_on_the_calendar(self):
        result = self.approve_booking(patient_name="Sunita Devi")
        self.assertTrue(result["ok"])
        self.assertIn("Token T-01", result["message"])
        self.assertEqual(self.fake.titles(), ["T-01 · Sunita D."])
        self.assertEqual(self.queue_rows(), [("appointment", str(result["entity_id"]), "done")])

    def test_cancel_and_reschedule_through_approve_update_the_calendar(self):
        first = self.approve_booking(patient_name="Asha Verma", start_time="09:00")["entity_id"]
        second = self.approve_booking(patient_name="Bela Shah", start_time="09:30")["entity_id"]
        self.assertEqual(self.fake.titles(), ["T-01 · Asha V.", "T-02 · Bela S."])
        self.assertTrue(self.approve("cancel_appointment", {"appointment_id": first})["ok"])
        self.assertEqual(self.fake.titles(), ["T-01 · Bela S."])
        self.assertTrue(self.approve("reschedule_appointment",
                                     {"appointment_id": second, "appt_date": TODAY, "start_time": "11:00"})["ok"])
        (event,) = self.fake.events.values()
        self.assertTrue(event["start"]["dateTime"].endswith("T11:00:00+05:30"))

    def test_queue_buttons_that_change_status_update_the_calendar(self):
        appt = self.approve_booking(patient_name="Asha Verma")["entity_id"]
        for action in ("check_in", "call"):
            self.assertTrue(self.client.post("/queue/{}/{}".format(appt, action)).get_json()["ok"])
        self.assertEqual(self.fake.ops("patch"), [])           # arrival / consultation are not shown on the calendar
        self.assertTrue(self.client.post("/queue/{}/done".format(appt)).get_json()["ok"])
        self.assertEqual(self.fake.find(appt)[0]["colorId"], "8")

    def test_unrelated_writes_do_not_queue_calendar_work(self):
        self.approve("log_expense", {"description": "tea", "amount_rupees": 40})
        self.approve("register_patient", {"name": "Ravi", "phone": "9000000009", "age": 30})
        self.assertEqual(self.queue_rows(), [])
        self.assertEqual(self.fake.calls, [])

    def test_whatsapp_approval_also_syncs(self):
        self.conn.execute(
            "INSERT INTO wa_messages (wa_message_id, wa_id, message_type, raw_text, intent, slots_json, status) "
            "VALUES ('wamid.cal', '919876543210', 'text', 'book me', 'book_appointment', '{}', 'classified')")
        self.conn.commit()
        msg_id = self.conn.execute("SELECT id FROM wa_messages").fetchone()[0]
        result = self.client.post("/wa/{}/approve".format(msg_id), json={"slots": {
            "patient_name": "Meera Iyer", "patient_phone": "9876543210", "appt_date": TODAY, "start_time": "09:30"}}).get_json()
        self.assertTrue(result["ok"])
        self.assertEqual(self.fake.titles(), ["T-01 · Meera I."])


class SyncNeverAffectsTheWriteTests(CalendarRouteCase):
    def assert_booking_saved(self, result):
        self.assertTrue(result["ok"], result)
        self.assertIn("Saved", result["message"])
        self.assertEqual(self.count("appointments"), 1)
        row = self.conn.execute("SELECT status FROM appointments").fetchone()
        self.assertEqual(row["status"], "booked")

    def test_google_down_does_not_fail_the_save(self):
        for op in ("list", "insert", "patch", "delete"):
            self.fake.fail_always(op, GcalApiError(503, "Google is down"))
        self.assert_booking_saved(self.approve_booking())
        self.assertEqual(self.queue_rows(), [("appointment", "1", "pending")])    # kept for a retry

    def test_unexpected_exception_from_the_client_does_not_fail_the_save(self):
        self.fake.fail_always("list", RuntimeError("something nobody expected"))
        self.assert_booking_saved(self.approve_booking())

    def test_a_failing_hook_does_not_fail_the_save(self):
        with mock.patch.object(gcal_sync, "post_commit", side_effect=RuntimeError("hook bug")):
            self.assert_booking_saved(self.approve_booking())

    def test_a_worker_that_cannot_start_does_not_fail_the_save(self):
        def cannot_start(fn):
            raise RuntimeError("cannot start thread")
        clinic_app.GCAL_BACKGROUND = cannot_start
        self.assert_booking_saved(self.approve_booking())
        self.assertEqual(self.queue_rows(), [("appointment", "1", "pending")])

    def test_a_broken_calendar_table_does_not_fail_the_save(self):
        self.conn.execute("DROP TABLE calendar_sync_queue")
        self.conn.commit()
        self.assert_booking_saved(self.approve_booking())

    def test_the_response_does_not_wait_for_google(self):
        clinic_app.GCAL_BACKGROUND = lambda fn: None       # the worker has not run at all
        self.assert_booking_saved(self.approve_booking())
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.queue_rows(), [("appointment", "1", "pending")])

    def test_patient_notifications_are_unaffected(self):
        self.open_window("919876543210")
        self.fake.fail_always("list", GcalApiError(500, "down"))
        result = self.approve_booking()
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.sender.calls), 1)
        self.assertIn("T-01", self.sender.calls[0][1])


class NotConfiguredTests(CalendarRouteCase):
    configured = False

    def test_writes_do_not_queue_or_call_google(self):
        result = self.approve_booking()
        self.assertTrue(result["ok"])
        self.assertEqual(self.queue_rows(), [])
        self.assertEqual(self.fake.calls, [])

    def test_sync_now_says_not_configured(self):
        response = self.client.post("/calendar/sync")
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()["ok"])
        self.assertEqual(self.queue_rows(), [])

    def test_status_partial(self):
        html = self.client.get("/calendar/status/partial").get_data(as_text=True)
        self.assertIn("Not configured", html)
        self.assertIn("never", html)


class ConfiguredPanelTests(CalendarRouteCase):
    def test_google_sync_is_hidden_from_the_page_even_when_configured(self):
        # The Appointments tab is the in-app calendar now; the Google integration is dormant.
        html = self.client.get("/").get_data(as_text=True)
        calendar_card = html.split("Google Calendar</div>")[1].split("connector-desc")[0]
        self.assertNotIn("is-configured", calendar_card)
        self.assertNotIn("cal-frame", html)
        self.assertNotIn("calendar.google.com", html)

    def test_status_partial_reports_pending_last_sync_and_errors(self):
        self.client.post("/calendar/sync")                          # ran inline: synced
        html = self.client.get("/calendar/status/partial").get_data(as_text=True)
        self.assertIn("Configured &middot; syncing", html.replace("·", "&middot;"))
        self.assertIn(" IST", html)
        self.assertNotIn("Last error", html)
        gcal_sync.set_state(self.conn, "last_error", "Google Calendar API returned 403: <script>alert(1)</script>")
        self.conn.execute("INSERT INTO calendar_sync_queue (kind, target, status) VALUES ('full', '', 'pending')")
        self.conn.commit()
        html = self.client.get("/calendar/status/partial").get_data(as_text=True)
        self.assertIn("Last error", html)
        self.assertIn("403", html)
        self.assertNotIn("<script>alert(1)</script>", html)         # escaped
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("had errors", html)
        self.assertRegex(html, r"Pending changes</span>\s*<span>1")


class SyncNowTests(CalendarRouteCase):
    inline = False

    def test_sync_now_queues_a_full_reconcile_and_returns_immediately(self):
        self.approve_booking()
        response = self.client.post("/calendar/sync")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["pending"], 2)                      # the booking's row + the full reconcile
        self.assertIn(("full", "", "pending"), self.queue_rows())
        self.assertEqual(self.fake.calls, [])                     # nothing ran on the request thread

    def test_clicking_twice_queues_one_full_reconcile(self):
        self.client.post("/calendar/sync")
        self.client.post("/calendar/sync")
        self.assertEqual(self.queue_rows().count(("full", "", "pending")), 1)

    def test_the_worker_then_syncs_the_whole_window(self):
        self.approve_booking(patient_name="Asha Verma")
        self.client.post("/calendar/sync")
        clinic_app._gcal_drain_job()                              # what the background thread runs
        self.assertEqual(self.fake.titles(), ["T-01 · Asha V."])
        self.assertEqual({r[2] for r in self.queue_rows()}, {"done"})

    def test_a_queueing_failure_is_reported_not_raised(self):
        with mock.patch.object(gcal_sync, "request_full_sync", side_effect=RuntimeError("db locked")):
            response = self.client.post("/calendar/sync")
        self.assertEqual(response.status_code, 500)
        self.assertFalse(response.get_json()["ok"])

    def test_the_drain_job_without_a_client_is_a_no_op(self):
        clinic_app.GCAL_CLIENT = None
        with mock.patch.object(gcal_client, "get_client", return_value=None):
            clinic_app._gcal_drain_job()                          # must not raise


class NoRealGoogleTests(unittest.TestCase):
    def test_default_seams_are_unset(self):
        self.assertIsNone(clinic_app.GCAL_CLIENT)
        self.assertIsNone(clinic_app.GCAL_BACKGROUND)

    def test_open_calendar_is_navigation_not_a_deferred_write(self):
        self.assertNotIn("open_calendar", clinic_app.DEFERRED_INTENTS)


if __name__ == "__main__":
    unittest.main()
