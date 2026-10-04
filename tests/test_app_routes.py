import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

os.environ["WHATSAPP_NOTIFY_MODE"] = "dry_run"
os.environ.setdefault("SARVAM_API_KEY", "test-not-real")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Importing app calls load_dotenv(); stub it so a test run never reads .env
# (which holds the live WhatsApp credentials).
with mock.patch("dotenv.load_dotenv"):
    import app as clinic_app

from clinic import db, notify

TODAY = date.today().isoformat()


class FakeSender:
    def __init__(self, fail_with=None):
        self.calls = []
        self.fail_with = fail_with

    def __call__(self, wa_id, text):
        self.calls.append((wa_id, text))
        if self.fail_with:
            raise self.fail_with


class RouteTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Never the live clinic.db.
        patcher = mock.patch.object(clinic_app, "DB_PATH", str(Path(self.tmp.name) / "test.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        # Tripwire: the real WhatsApp client must never be reached from tests.
        tripwire = mock.patch("clinic.whatsapp.send_message", side_effect=AssertionError("real WhatsApp send attempted"))
        tripwire.start()
        self.addCleanup(tripwire.stop)
        self.sender = FakeSender()
        clinic_app.NOTIFY_SENDER = self.sender
        self.addCleanup(setattr, clinic_app, "NOTIFY_SENDER", None)
        self.client = clinic_app.app.test_client()
        self.conn = db.connect(clinic_app.DB_PATH)
        self.addCleanup(self.conn.close)

    def open_window(self, wa_id, text="hello"):
        self.conn.execute(
            "INSERT INTO wa_messages (wa_message_id, wa_id, message_type, raw_text) VALUES (?, ?, 'text', ?)",
            ("wamid.route" + wa_id + text, wa_id, text),
        )
        self.conn.commit()

    def approve_booking(self, **overrides):
        slots = {"patient_name": "Sunita", "patient_phone": "9876543210", "appt_date": TODAY, "start_time": "09:00"}
        slots.update(overrides)
        return self.client.post("/approve", json={"intent": "book_appointment", "slots": slots, "language": "en-IN"}).get_json()

    def count(self, table):
        return self.conn.execute("SELECT COUNT(*) FROM {}".format(table)).fetchone()[0]


class ApproveTests(RouteTestCase):
    def test_booking_message_includes_token_and_patient_is_notified(self):
        self.open_window("919876543210")
        result = self.approve_booking()
        self.assertTrue(result["ok"])
        self.assertIn("Token T-01", result["message"])
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(self.sender.calls[0][0], "919876543210")
        self.assertIn("T-01", self.sender.calls[0][1])

    def test_failing_sender_does_not_change_the_write_result(self):
        self.open_window("919876543210")
        clinic_app.NOTIFY_SENDER = FakeSender(fail_with=RuntimeError("WhatsApp is down"))
        result = self.approve_booking()
        self.assertTrue(result["ok"])
        self.assertEqual(self.count("appointments"), 1)
        self.assertEqual(self.conn.execute("SELECT status FROM notifications").fetchone()[0], "failed")

    def test_raising_notifier_does_not_change_the_write_result(self):
        with mock.patch("clinic.notify.after_write", side_effect=ValueError("template bug")):
            result = self.approve_booking()
        self.assertTrue(result["ok"])
        self.assertEqual(self.count("appointments"), 1)

    def test_raising_post_commit_does_not_change_the_write_result(self):
        with mock.patch("clinic.notify.post_commit", side_effect=RuntimeError("contract broken")):
            result = self.approve_booking()
        self.assertTrue(result["ok"])
        self.assertEqual(self.count("appointments"), 1)

    def test_raising_describe_does_not_report_a_committed_write_as_failed(self):
        with mock.patch.object(clinic_app, "describe_proposal", side_effect=KeyError("age")):
            result = self.approve_booking()
        self.assertTrue(result["ok"])
        self.assertEqual(self.count("appointments"), 1)

    def test_no_window_means_blocked_not_sent(self):
        result = self.approve_booking()
        self.assertTrue(result["ok"])
        self.assertEqual(self.sender.calls, [])
        self.assertEqual(self.conn.execute("SELECT status FROM notifications").fetchone()[0], "blocked_no_window")

    def test_failed_write_still_reports_failure(self):
        self.approve_booking()
        result = self.approve_booking(patient_name="Other", patient_phone="9111111111")  # same slot
        self.assertFalse(result["ok"])


class QueueEndpointTests(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.approve_booking()
        self.appt = self.conn.execute("SELECT id FROM appointments").fetchone()[0]
        self.audit_before = self.count("audit_log")

    def state(self):
        row = self.conn.execute("SELECT status, queue_state FROM appointments WHERE id=?", (self.appt,)).fetchone()
        return tuple(row)

    def test_buttons_run_propose_and_confirm_and_are_audited(self):
        for action, expected in (("check_in", ("booked", "checked_in")), ("call", ("booked", "in_consultation")),
                                 ("done", ("completed", None))):
            result = self.client.post("/queue/{}/{}".format(self.appt, action)).get_json()
            self.assertTrue(result["ok"], result)
            self.assertEqual(self.state(), expected)
        self.assertEqual(self.count("audit_log"), self.audit_before + 3)
        intents = [r[0] for r in self.conn.execute("SELECT intent FROM audit_log ORDER BY id DESC LIMIT 3")]
        self.assertEqual(intents, ["queue_mark_done", "queue_call_next", "queue_check_in"])

    def test_message_names_the_token(self):
        result = self.client.post("/queue/{}/check_in".format(self.appt)).get_json()
        self.assertIn("T-01", result["message"])
        self.assertIn("checked in", result["message"])

    def test_call_notifies_your_turn_when_window_open(self):
        self.open_window("919876543210")
        self.sender.calls.clear()
        self.client.post("/queue/{}/call".format(self.appt))
        self.assertTrue(any("your turn" in text for _, text in self.sender.calls))

    def test_invalid_transition_is_a_clean_error_and_leaves_no_pending_proposal(self):
        self.client.post("/queue/{}/done".format(self.appt))
        result = self.client.post("/queue/{}/done".format(self.appt)).get_json()
        self.assertFalse(result["ok"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM proposals WHERE status='pending'").fetchone()[0], 0)

    def test_unknown_action_and_unknown_appointment(self):
        self.assertEqual(self.client.post("/queue/{}/explode".format(self.appt)).status_code, 400)
        self.assertEqual(self.client.post("/queue/99999/check_in").status_code, 404)

    def test_only_todays_appointments(self):
        self.conn.execute("UPDATE appointments SET appt_date = '2020-01-01' WHERE id = ?", (self.appt,))
        self.conn.commit()
        result = self.client.post("/queue/{}/check_in".format(self.appt)).get_json()
        self.assertFalse(result["ok"])
        self.assertIsNone(self.state()[1])

    def test_failing_sender_does_not_fail_the_button(self):
        self.open_window("919876543210")
        clinic_app.NOTIFY_SENDER = FakeSender(fail_with=RuntimeError("down"))
        result = self.client.post("/queue/{}/call".format(self.appt)).get_json()
        self.assertTrue(result["ok"])
        self.assertEqual(self.state()[1], "in_consultation")


class PagesAndRetryTests(RouteTestCase):
    def test_dashboard_and_partial_render_the_queue(self):
        self.approve_booking()
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'data-tab="queue"', page.data)
        self.assertIn(b"T-01", page.data)
        partial = self.client.get("/queue/partial")
        self.assertEqual(partial.status_code, 200)
        self.assertIn(b"Today's queue", partial.data)
        self.assertIn(b"data-queue-action=\"check_in\"", partial.data)
        self.assertIn(b"Recent notifications", partial.data)
        self.assertIn(b"blocked no window", partial.data)

    def test_retry_resends_once_the_window_is_open(self):
        self.approve_booking()
        nid = self.conn.execute("SELECT id FROM notifications").fetchone()[0]
        self.assertEqual(self.sender.calls, [])
        self.open_window("919876543210")
        result = self.client.post("/notifications/{}/retry".format(nid)).get_json()
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "sent")
        self.assertEqual(len(self.sender.calls), 1)
        self.assertFalse(self.client.post("/notifications/{}/retry".format(nid)).get_json()["ok"])


class WhatsAppApproveTests(RouteTestCase):
    def add_inbox(self, wa_id, text, intent, slots):
        import json
        cur = self.conn.execute(
            "INSERT INTO wa_messages (wa_message_id, wa_id, message_type, raw_text, intent, slots_json, status) "
            "VALUES (?, ?, 'text', ?, ?, ?, 'classified')",
            ("wamid.in" + text, wa_id, text, intent, json.dumps(slots)),
        )
        self.conn.commit()
        return cur.lastrowid

    def test_register_patient_sends_registered_to_the_sender_without_claiming_an_appointment(self):
        msg = self.add_inbox("919123456780", "I want to register", "register_patient", {"name": "Neeta", "phone": "9123456780"})
        result = self.client.post("/wa/{}/approve".format(msg), json={"slots": {"name": "Neeta", "phone": "9123456780", "age": None}}).get_json()
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.sender.calls), 1)
        wa_id, text = self.sender.calls[0]
        self.assertEqual(wa_id, "919123456780")
        self.assertIn("registered with us", text)
        self.assertNotIn("appointment", text.lower())

    def test_notifier_failure_does_not_fail_wa_approve(self):
        msg = self.add_inbox("919123456780", "I want to register", "register_patient", {"name": "Neeta", "phone": "9123456780"})
        clinic_app.NOTIFY_SENDER = FakeSender(fail_with=RuntimeError("down"))
        result = self.client.post("/wa/{}/approve".format(msg), json={"slots": {"name": "Neeta", "phone": "9123456780"}}).get_json()
        self.assertTrue(result["ok"])
        self.assertEqual(self.count("patients"), 1)

    def test_wa_booking_approve_notifies_with_token(self):
        msg = self.add_inbox("919876543210", "mujhe appointment chahiye", "book_appointment",
                             {"patient_name": "Sunita", "patient_phone": "9876543210", "appt_date": TODAY, "start_time": "09:00"})
        result = self.client.post("/wa/{}/approve".format(msg), json={"slots": {
            "patient_id": None, "patient_name": "Sunita", "patient_phone": "9876543210",
            "appt_date": TODAY, "start_time": "09:00", "duration_minutes": None, "notes": None}}).get_json()
        self.assertTrue(result["ok"], result)
        self.assertIn("Token T-01", result["message"])
        self.assertEqual(len(self.sender.calls), 1)
        self.assertIn("aapki appointment", self.sender.calls[0][1])  # Hinglish, from the patient's own message


class WhatsAppInboundTests(RouteTestCase):
    """_classify_and_store is what the webhook calls after saving a message."""

    def receive(self, wa_id, text):
        cur = self.conn.execute(
            "INSERT INTO wa_messages (wa_message_id, wa_id, message_type, raw_text, status) VALUES (?, ?, 'text', ?, 'received')",
            ("wamid.inb" + wa_id + text, wa_id, text),
        )
        self.conn.commit()
        clinic_app._classify_and_store(cur.lastrowid, wa_id, text)
        return self.conn.execute("SELECT * FROM wa_messages WHERE id = ?", (cur.lastrowid,)).fetchone()

    def test_status_question_is_answered_automatically_to_the_sender_only(self):
        self.approve_booking()                      # Sunita, 9876543210, today 09:00
        self.sender.calls.clear()
        row = self.receive("919876543210", "what is my token")
        self.assertEqual(row["intent"], "my_status")
        self.assertEqual(row["status"], "dismissed")      # nothing for staff to approve
        self.assertEqual(len(self.sender.calls), 1)
        wa_id, text = self.sender.calls[0]
        self.assertEqual(wa_id, "919876543210")
        self.assertIn("T-01", text)
        self.assertEqual(self.count("proposals"), 1)      # only the booking: no write was proposed for the question
        self.assertEqual(self.conn.execute("SELECT event FROM notifications ORDER BY id DESC").fetchone()[0], "status_reply")

    def test_status_question_from_a_number_with_no_appointment_goes_to_staff(self):
        self.approve_booking()
        self.sender.calls.clear()
        row = self.receive("919111111111", "what is my token")
        self.assertEqual(row["status"], "needs_human_reply")
        self.assertEqual(self.sender.calls, [])

    def test_status_reply_failure_never_breaks_the_webhook_path(self):
        self.approve_booking()
        clinic_app.NOTIFY_SENDER = FakeSender(fail_with=RuntimeError("down"))
        row = self.receive("919876543210", "what is my token")
        self.assertEqual(row["status"], "dismissed")
        self.assertEqual(self.conn.execute("SELECT status FROM notifications WHERE event='status_reply'").fetchone()[0], "failed")

    @mock.patch("clinic.whatsapp_pipeline.extract_name", return_value="Neeta")
    def test_booking_request_becomes_a_proposal_awaiting_approval_and_writes_nothing(self, _n):
        row = self.receive("919123456780", "mujhe appointment chahiye")
        self.assertEqual((row["intent"], row["status"]), ("book_appointment", "classified"))
        self.assertEqual(self.count("appointments"), 0)
        self.assertEqual(self.sender.calls, [])
        import json
        slots = json.loads(row["slots_json"])
        self.assertTrue(slots["suggestion_note"])
        self.assertEqual(slots["patient_phone"], "9123456780")


if __name__ == "__main__":
    unittest.main()
