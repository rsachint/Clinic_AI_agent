"""The WhatsApp conversation agent wired into the real Flask routes, driven
in-process with the Flask test client. The sender is a recording fake: these
tests assert exactly which messages WOULD be sent, and nothing can reach
WhatsApp (the real client functions are tripwired)."""
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ["WHATSAPP_NOTIFY_MODE"] = "dry_run"
os.environ.setdefault("SARVAM_API_KEY", "test-not-real")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_app_routes import RouteTestCase, clinic_app  # noqa: E402  (stubs load_dotenv, never reads .env)

from clinic import conv_templates as ct  # noqa: E402
from clinic import conversation as cv  # noqa: E402
from clinic import notify, settings, whatsapp as wa  # noqa: E402

CLOCK_NOW = datetime(2026, 10, 5, 10, 0)  # Monday 10:00, the clinic's local time for the agent
TOMORROW = "2026-10-06"
WA = "919876543210"
WA2 = "919111122223"


class AgentSender:
    """Records (wa_id, text, interactive) instead of sending anything."""

    def __init__(self, fail_with=None):
        self.calls = []
        self.fail_with = fail_with

    def __call__(self, wa_id, text, interactive=None):
        self.calls.append((wa_id, text, interactive))
        if self.fail_with:
            raise self.fail_with

    @property
    def texts(self):
        return [t for _, t, _ in self.calls]


class FakePicker:
    def __init__(self, label="unclear"):
        self.label, self.calls = label, []

    def __call__(self, text):
        self.calls.append(text)
        return self.label


class AgentRouteCase(RouteTestCase):
    # Patient-initiated book / cancel / reschedule is now committed
    # automatically by default (tests/test_auto_appointments.py covers that
    # path). The tests in THIS file are about the staff-approval path -- the
    # inbox proposal, the slot hold, approve / reject -- which is exactly what
    # the app does when the automation switch is OFF (and for anything the
    # guard policy sends to staff), so they run with the switch off. See
    # AutomationOffTests for the explicit "OFF == as before" assertion.
    AUTOMATION = False

    def setUp(self):
        super().setUp()
        if not self.AUTOMATION:
            settings.set_auto_enabled(self.conn, False)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.tmp.name)          # the audio path writes a temp .ogg into the cwd
        for name in ("send_interactive",):
            p = mock.patch("clinic.whatsapp." + name, side_effect=AssertionError("real WhatsApp send attempted"))
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {"WHATSAPP_AGENT_ENABLED": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.sender = AgentSender()
        self.picker = FakePicker()
        clinic_app.NOTIFY_SENDER = self.sender
        clinic_app.BACKGROUND = lambda fn, *args: fn(*args)       # inline: deterministic tests
        clinic_app.CLOCK = lambda: CLOCK_NOW
        # The notification code reads its own clock. Freeze only its LOCAL day / time (what decides
        # "today", reminders and token wording) so these tests do not depend on what day it really
        # is; the UTC half stays real because message timestamps and the 24-hour window use it.
        real_now = mock.patch.object(
            notify.Now, "real",
            classmethod(lambda cls: cls(clinic_app._clinic_now(), datetime.now(timezone.utc).replace(tzinfo=None))))
        real_now.start()
        self.addCleanup(real_now.stop)
        clinic_app.AGENT_PICKER = self.picker
        self.addCleanup(setattr, clinic_app, "BACKGROUND", None)
        self.addCleanup(setattr, clinic_app, "CLOCK", None)
        self.addCleanup(setattr, clinic_app, "AGENT_PICKER", None)
        self._n = 0

    # -- driving the webhook ---------------------------------------------------------
    def post(self, message, expect=200):
        payload = {"entry": [{"changes": [{"value": {"messages": [message]}}]}]}
        response = self.client.post("/webhook/whatsapp", json=payload)
        self.assertEqual(response.status_code, expect)
        self.assertEqual(response.get_json(), {})
        return response

    def mid(self):
        self._n += 1
        return "wamid.agent{}".format(self._n)

    def say(self, text, wa_id=WA, **extra):
        message = {"from": wa_id, "id": self.mid(), "type": "text", "text": {"body": text}}
        message.update(extra)
        return self.post(message)

    def tap(self, choice_id, title="x", wa_id=WA, kind="button_reply"):
        return self.post({"from": wa_id, "id": self.mid(), "type": "interactive",
                          "interactive": {"type": kind, kind: {"id": choice_id, "title": title}}})

    def last_row(self):
        return self.conn.execute("SELECT * FROM wa_messages ORDER BY id DESC LIMIT 1").fetchone()

    def inbox(self):
        html = self.client.get("/").get_data(as_text=True)
        match = re.search(r'<script id="wa-inbox-data" type="application/json">(.*?)</script>', html, re.S)
        return json.loads(match.group(1))

    def events(self):
        return [r[0] for r in self.conn.execute("SELECT event FROM notifications ORDER BY id")]

    def book_through_conversation(self, wa_id=WA, name="Sunita Devi", day="tomorrow", time="4 pm"):
        self.say("I want to book an appointment", wa_id)
        self.say(name, wa_id)
        self.say(day, wa_id)
        self.say(time, wa_id)
        self.tap("confirm:yes", "Confirm request", wa_id)
        return self.last_row()


class ConversationThroughTheWebhookTests(AgentRouteCase):
    def test_greeting_gets_exactly_one_reply_with_buttons_and_no_generic_ack(self):
        self.say("hello")
        self.assertEqual(len(self.sender.calls), 1)
        wa_id, text, interactive = self.sender.calls[0]
        self.assertEqual(wa_id, WA)
        self.assertEqual(text, ct.text("menu", "en", greet=ct.greet("en")))
        self.assertEqual([b["id"] for b in interactive["buttons"]], ["menu:book", "menu:reschedule", "menu:cancel"])
        self.assertNotIn(wa.acknowledgment_text("en"), self.sender.texts)
        self.assertEqual(self.events(), ["conv_reply"])
        row = self.last_row()
        self.assertEqual((row["status"], row["agent_handled"], row["message_type"]), ("dismissed", 1, "text"))
        self.assertEqual(self.inbox(), [])                       # nothing for staff to do

    def test_agent_handled_rows_are_excluded_from_the_actionable_inbox_even_if_still_classified(self):
        self.say("hello")
        self.conn.execute("UPDATE wa_messages SET status = 'needs_human_reply' WHERE agent_handled = 1")
        self.conn.commit()
        self.assertEqual(self.inbox(), [])

    def test_a_full_booking_becomes_one_inbox_proposal_and_writes_nothing(self):
        row = self.book_through_conversation()
        self.assertEqual((row["intent"], row["status"], row["agent_handled"]), ("book_appointment", "classified", 0))
        slots = json.loads(row["slots_json"])
        self.assertEqual((slots["appt_date"], slots["start_time"], slots["patient_name"]), (TOMORROW, "16:00", "Sunita Devi"))
        self.assertEqual(slots["patient_phone"], "9876543210")
        self.assertEqual(slots["via"], "conversation")
        self.assertIn("WhatsApp conversation", slots["notes"])
        # The agent proposed; it created no appointment, no proposal, no audit entry.
        for table in ("appointments", "proposals", "audit_log"):
            self.assertEqual(self.count(table), 0, table)
        # The patient was told it's with the clinic -- interim reply last.
        self.assertEqual(self.sender.texts[-1], "Request received -- the clinic will confirm shortly.")
        # ...and staff see exactly one actionable card for it.
        inbox = self.inbox()
        self.assertEqual([i["id"] for i in inbox], [row["id"]])
        self.assertEqual(inbox[0]["intent"], "book_appointment")
        hold = self.conn.execute("SELECT * FROM slot_holds").fetchone()
        self.assertEqual((hold["wa_id"], hold["appt_date"], hold["start_time"], hold["wa_message_id"]),
                         (WA, TOMORROW, "16:00", row["id"]))

    def test_every_message_in_the_conversation_gets_exactly_one_reply(self):
        self.say("I want to book an appointment")
        self.assertEqual(len(self.sender.calls), 1)
        self.say("Sunita Devi")
        self.assertEqual(len(self.sender.calls), 2)
        self.say("tomorrow")
        self.assertEqual(len(self.sender.calls), 3)
        self.assertEqual(self.sender.calls[2][2]["type"], "list")              # five free times as a list
        self.assertEqual(len(self.sender.calls[2][2]["rows"]), 5)
        self.tap("slot:2026-10-06T11:00", "11:00 AM", kind="list_reply")
        self.assertEqual(self.sender.calls[3][2]["buttons"][0]["id"], "confirm:yes")
        self.assertNotIn(wa.acknowledgment_text("en"), self.sender.texts)

    def test_staff_approve_runs_the_existing_pipeline_and_releases_the_hold(self):
        row = self.book_through_conversation()
        self.sender.calls.clear()
        slots = json.loads(row["slots_json"])
        result = self.client.post("/wa/{}/approve".format(row["id"]), json={"slots": {
            "patient_id": None, "patient_name": slots["patient_name"], "patient_phone": slots["patient_phone"],
            "appt_date": slots["appt_date"], "start_time": slots["start_time"], "duration_minutes": None,
            "notes": slots["notes"]}}).get_json()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.count("appointments"), 1)                         # written only now, by the human tap
        self.assertEqual(self.count("audit_log"), 1)
        self.assertEqual(self.conn.execute("SELECT status FROM wa_messages WHERE id=?", (row["id"],)).fetchone()[0], "approved")
        self.assertEqual(self.count("slot_holds"), 0)
        self.assertEqual(len(self.sender.calls), 1)                             # booking_confirmed
        self.assertIn("appointment is booked", self.sender.texts[0])

    def test_reject_sends_the_fixed_decline_notice_and_releases_the_hold(self):
        row = self.book_through_conversation()
        self.sender.calls.clear()
        self.assertTrue(self.client.post("/wa/{}/reject".format(row["id"])).get_json()["ok"])
        self.assertEqual(self.sender.calls, [(WA, "We couldn't confirm that request. Please send another preferred time.", None)])
        self.assertEqual(self.events()[-1], "request_declined")
        self.assertEqual(self.count("slot_holds"), 0)
        self.assertEqual(self.count("appointments"), 0)
        # idempotent: rejecting again does not message the patient twice
        self.client.post("/wa/{}/reject".format(row["id"]))
        self.assertEqual(len(self.sender.calls), 1)

    def test_decline_notice_follows_the_patients_language(self):
        self.say("नमस्ते")
        self.say("मुझे अपॉइंटमेंट चाहिए")
        self.say("सुनीता देवी")
        self.say("कल")
        self.say("शाम 4 बजे")
        self.tap("confirm:yes", "कन्फर्म करें")
        row = self.last_row()
        self.sender.calls.clear()
        self.client.post("/wa/{}/reject".format(row["id"]))
        self.assertEqual(self.sender.texts, [notify.render("request_declined", "hi")])

    def test_reject_of_a_non_conversation_item_sends_nothing(self):
        cur = self.conn.execute(
            "INSERT INTO wa_messages (wa_message_id, wa_id, message_type, raw_text, intent, slots_json, status) "
            "VALUES ('legacy1', ?, 'text', 'appointment chahiye', 'book_appointment', ?, 'classified')",
            (WA, json.dumps({"appt_date": TOMORROW, "start_time": "09:00"})))
        self.conn.commit()
        self.client.post("/wa/{}/reject".format(cur.lastrowid))
        self.assertEqual(self.sender.calls, [])

    def test_dismiss_releases_the_hold_without_messaging(self):
        row = self.book_through_conversation()
        self.sender.calls.clear()
        self.client.post("/wa/{}/dismiss".format(row["id"]))
        self.assertEqual(self.count("slot_holds"), 0)
        self.assertEqual(self.sender.calls, [])

    def test_confirm_time_double_booking_guard_is_still_the_final_authority(self):
        row = self.book_through_conversation()
        # Someone else gets that slot by another route (a staff booking) before approval.
        self.conn.execute("INSERT INTO appointments (patient_name, patient_phone, appt_date, start_time) "
                          "VALUES ('Walk-in', '9000000000', ?, '16:00')", (TOMORROW,))
        self.conn.commit()
        slots = json.loads(row["slots_json"])
        result = self.client.post("/wa/{}/approve".format(row["id"]), json={"slots": {
            "patient_id": None, "patient_name": "Sunita Devi", "patient_phone": "9876543210",
            "appt_date": slots["appt_date"], "start_time": slots["start_time"], "duration_minutes": None, "notes": None}}).get_json()
        self.assertFalse(result["ok"])
        self.assertIn("no longer free", result["error"])
        self.assertEqual(self.count("appointments"), 1)                         # only the walk-in
        self.assertEqual(self.conn.execute("SELECT status FROM wa_messages WHERE id=?", (row["id"],)).fetchone()[0], "classified")

    def test_a_second_sender_cannot_be_offered_a_held_slot(self):
        self.book_through_conversation()
        self.sender.calls.clear()
        self.say("book tomorrow 4 pm", WA2)
        self.say("Bhavna", WA2)
        self.assertIn("not available", self.sender.texts[-1])

    def test_reschedule_and_cancel_hand_offs_are_proposals_too(self):
        pid = self.conn.execute("INSERT INTO patients (name, phone) VALUES ('Sunita Devi', '9876543210')").lastrowid
        aid = self.conn.execute("INSERT INTO appointments (patient_id, appt_date, start_time) VALUES (?, ?, '09:00')",
                                (pid, TOMORROW)).lastrowid
        self.conn.commit()
        self.say("cancel my appointment")
        self.tap("confirm:yes", "Yes")
        row = self.last_row()
        self.assertEqual((row["intent"], row["status"]), ("cancel_appointment", "classified"))
        self.assertEqual(json.loads(row["slots_json"])["appointment_id"], aid)
        self.assertEqual(self.conn.execute("SELECT status FROM appointments WHERE id=?", (aid,)).fetchone()[0], "booked")
        item = [i for i in self.inbox() if i["id"] == row["id"]][0]
        self.assertEqual([a["id"] for a in item["appointments"]], [aid])        # the card's dropdown still works

    def test_status_question_is_answered_with_the_status_template(self):
        self.approve_booking(appt_date=TOMORROW, start_time="09:00")
        self.sender.calls.clear()
        self.say("what is my token")
        self.assertEqual(len(self.sender.calls), 1)
        self.assertIn("T-01", self.sender.texts[0])
        self.assertEqual(self.events()[-1], "status_reply")
        row = self.last_row()
        self.assertEqual((row["status"], row["agent_handled"]), ("dismissed", 1))

    def test_status_with_nothing_booked_offers_to_book(self):
        self.say("what is my token")
        self.assertEqual(self.sender.texts, [ct.text("status_none", "en")])
        self.assertEqual(self.sender.calls[0][2]["buttons"][0]["id"], "menu:book")


class SafetyThroughTheWebhookTests(AgentRouteCase):
    def test_emergency_gets_the_fixed_reply_and_a_highlighted_inbox_item(self):
        self.say("I have chest pain")
        self.assertEqual(self.sender.texts, [ct.text("emergency", "en")])
        row = self.last_row()
        self.assertEqual((row["status"], row["agent_handled"], row["intent"]), ("needs_human_reply", 0, None))
        self.assertEqual(json.loads(row["slots_json"])["flag"], "emergency")
        self.say("hello", WA2)                                                  # an older, ordinary item...
        self.conn.execute("UPDATE wa_messages SET status='needs_human_reply', agent_handled=0 WHERE wa_id=?", (WA2,))
        self.conn.commit()
        self.assertEqual(self.inbox()[0]["slots"]["flag"], "emergency")          # ...never ranks above an emergency

    def test_clinical_question_is_refused_and_flagged(self):
        self.say("which tablet should I take for fever")
        self.assertEqual(self.sender.texts, [ct.text("clinical", "en")])
        row = self.last_row()
        self.assertEqual((row["status"], json.loads(row["slots_json"])["flag"]), ("needs_human_reply", "clinical"))

    def test_escalation_creates_a_needs_human_reply_item(self):
        self.say("I want to talk to a person")
        self.assertEqual(self.sender.texts, [ct.text("escalate", "en")])
        row = self.last_row()
        self.assertEqual(row["status"], "needs_human_reply")
        self.assertEqual(json.loads(row["slots_json"]), {"flag": "escalation", "reason": "human_request"})

    def test_two_confused_turns_escalate(self):
        self.say("qwerty asdf")
        self.say("zxcv uiop")
        self.assertEqual(self.sender.texts[-1], ct.text("escalate", "en"))
        self.assertEqual(self.last_row()["status"], "needs_human_reply")
        self.assertEqual(self.picker.calls, ["qwerty asdf", "zxcv uiop"])

    def test_human_mode_keeps_the_agent_silent_and_lands_messages_for_staff(self):
        cv.set_mode(self.conn, WA, "human", now=CLOCK_NOW)
        self.say("book tomorrow 4 pm")
        self.assertEqual(self.sender.calls, [])
        row = self.last_row()
        self.assertEqual(row["status"], "needs_human_reply")
        self.assertEqual(json.loads(row["slots_json"])["flag"], "human_mode")

    def test_rate_limit_sends_everything_beyond_30_an_hour_to_staff(self):
        for i in range(cv.RATE_LIMIT_TURNS):
            self.say("hello")
        self.assertEqual(len(self.sender.calls), cv.RATE_LIMIT_TURNS)
        self.say("hello")
        self.assertEqual(self.sender.texts[-1], ct.text("escalate", "en"))
        self.say("hello")
        self.assertEqual(len(self.sender.calls), cv.RATE_LIMIT_TURNS + 1)       # silent after the first notice
        self.assertEqual(self.last_row()["status"], "needs_human_reply")


class RobustnessTests(AgentRouteCase):
    def test_a_raising_sender_never_breaks_the_webhook_and_the_row_is_still_recorded(self):
        clinic_app.NOTIFY_SENDER = AgentSender(fail_with=RuntimeError("whatsapp is down"))
        self.say("hello")                                   # post() asserts a 200 + {}
        n = self.conn.execute("SELECT status, error FROM notifications").fetchone()
        self.assertEqual(n["status"], "failed")
        self.assertIn("whatsapp is down", n["error"])
        self.assertEqual(self.last_row()["status"], "dismissed")
        # A failing send does not lose the hand-off either.
        clinic_app.NOTIFY_SENDER = AgentSender(fail_with=RuntimeError("down"))
        row = self.book_through_conversation()
        self.assertEqual((row["intent"], row["status"]), ("book_appointment", "classified"))

    def test_a_raising_sender_does_not_mask_a_successful_approval(self):
        row = self.book_through_conversation()
        clinic_app.NOTIFY_SENDER = AgentSender(fail_with=RuntimeError("down"))
        slots = json.loads(row["slots_json"])
        result = self.client.post("/wa/{}/approve".format(row["id"]), json={"slots": {
            "patient_name": "Sunita Devi", "patient_phone": "9876543210", "appt_date": slots["appt_date"],
            "start_time": slots["start_time"]}}).get_json()
        self.assertTrue(result["ok"])
        self.assertEqual(self.count("appointments"), 1)

    def test_a_raising_sender_does_not_break_reject(self):
        row = self.book_through_conversation()
        clinic_app.NOTIFY_SENDER = AgentSender(fail_with=RuntimeError("down"))
        self.assertTrue(self.client.post("/wa/{}/reject".format(row["id"])).get_json()["ok"])
        self.assertEqual(self.conn.execute("SELECT status FROM wa_messages WHERE id=?", (row["id"],)).fetchone()[0], "rejected")
        self.assertEqual(self.count("slot_holds"), 0)

    def test_if_the_agent_itself_breaks_the_message_falls_back_to_staff_with_one_ack(self):
        with mock.patch("clinic.conversation.handle_inbound", side_effect=RuntimeError("boom")):
            response = self.say("mujhe appointment chahiye")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.sender.texts, [wa.acknowledgment_text("hinglish")])   # the old generic ack, once
        row = self.last_row()
        self.assertEqual(row["status"], "classified")                          # the old path still produced a proposal
        self.assertEqual(row["intent"], "book_appointment")

    def test_a_redelivered_webhook_message_is_not_answered_twice(self):
        message = {"from": WA, "id": "wamid.same", "type": "text", "text": {"body": "hello"}}
        self.post(message)
        self.post(message)
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(self.count("wa_messages"), 1)

    def test_reprocessing_the_same_message_never_duplicates_a_reply(self):
        self.say("hello")
        row = self.last_row()
        clinic_app._process_text(row["id"], WA, "hello", None, False)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notifications WHERE event='conv_reply' AND dedup_key LIKE ?",
                                           ("conv_reply:{}:%".format(row["id"]),)).fetchone()[0], 1)

    def test_non_message_events_and_garbage_still_get_200(self):
        for payload in ({}, {"entry": [{"changes": [{"value": {"statuses": [{"id": "x", "status": "read"}]}}]}]}):
            response = self.client.post("/webhook/whatsapp", json=payload)
            self.assertEqual((response.status_code, response.get_json()), (200, {}))
        response = self.client.post("/webhook/whatsapp", data="not json", content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.sender.calls, [])

    def test_an_unexpected_failure_inside_the_webhook_still_returns_200(self):
        with mock.patch.object(clinic_app, "_receive_message", side_effect=RuntimeError("db exploded")):
            response = self.client.post("/webhook/whatsapp", json={"entry": [{"changes": [{"value": {"messages": [
                {"from": WA, "id": "w1", "type": "text", "text": {"body": "hi"}}]}}]}]})
        self.assertEqual((response.status_code, response.get_json()), (200, {}))

    def test_dry_run_mode_queues_replies_but_sends_nothing(self):
        clinic_app.NOTIFY_SENDER = None                      # WHATSAPP_NOTIFY_MODE=dry_run (set at the top of this file)
        self.say("hello")
        self.assertEqual(self.sender.calls, [])
        self.assertEqual(self.conn.execute("SELECT status FROM notifications").fetchone()[0], "dry_run")

    def test_stale_backlog_is_not_answered_conversationally_or_acknowledged(self):
        self.say("mujhe appointment chahiye", timestamp="1000000000")       # years old
        self.assertEqual(self.sender.calls, [])
        row = self.last_row()
        self.assertEqual((row["status"], row["agent_handled"]), ("classified", 0))   # old staff-review path
        self.assertEqual(self.count("wa_sessions"), 0)

    def test_background_dispatch_does_not_block_the_webhook_thread(self):
        queued = []
        clinic_app.BACKGROUND = lambda fn, *args: queued.append((fn, args))
        self.say("hello")
        self.assertEqual(self.sender.calls, [])             # nothing happened on the request thread
        self.assertEqual(self.last_row()["status"], "received")
        fn, args = queued[0]
        fn(*args)                                           # the worker thread's part
        self.assertEqual(len(self.sender.calls), 1)

    def test_real_whatsapp_functions_are_never_reached(self):
        self.book_through_conversation()
        self.say("I have chest pain")
        # The tripwires in setUp (send_message / send_interactive) would have raised; the sender saw it all.
        self.assertTrue(self.sender.calls)


class VoiceNoteTests(AgentRouteCase):
    def voice(self, transcript=None, fail=None):
        message = {"from": WA, "id": self.mid(), "type": "audio", "audio": {"id": "media1"}}
        patches = [mock.patch.object(clinic_app.wa, "download_media",
                                     side_effect=fail or None, return_value=b"ogg")]
        heard = mock.Mock(text=transcript)
        patches.append(mock.patch.object(clinic_app, "transcribe", return_value=heard))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.post(message)

    def test_transcript_goes_through_the_same_agent_and_no_generic_ack_is_sent(self):
        self.voice("mujhe appointment chahiye")
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(self.sender.texts[0][:15], ct.text("ask_name", "hinglish")[:15])
        self.assertNotIn(wa.acknowledgment_text("bilingual"), self.sender.texts)
        row = self.last_row()
        self.assertEqual((row["message_type"], row["raw_text"], row["agent_handled"]), ("audio", "mujhe appointment chahiye", 1))
        self.assertEqual(cv.load_session(self.conn, WA, CLOCK_NOW)["goal"], "book")

    def test_voice_emergency_is_still_an_emergency(self):
        self.voice("mujhe seene mein dard ho raha hai")
        self.assertEqual(self.sender.texts, [ct.text("emergency", "hinglish")])
        self.assertEqual(json.loads(self.last_row()["slots_json"])["flag"], "emergency")

    def test_failed_transcription_keeps_the_generic_ack_once(self):
        self.voice(fail=RuntimeError("asr down"))
        self.assertEqual(self.sender.texts, [wa.acknowledgment_text("bilingual")])
        row = self.last_row()
        self.assertEqual(row["status"], "error")
        self.assertIn("asr down", row["error_text"])
        self.voice(fail=RuntimeError("asr down"))                       # throttled: no second ack in the cooldown
        self.assertEqual(len(self.sender.calls), 1)


class AutomationOffTests(AgentRouteCase):
    def test_switch_off_means_the_staff_approval_path_exactly_as_before(self):
        row = self.book_through_conversation()
        self.assertEqual((row["intent"], row["status"], row["agent_handled"]), ("book_appointment", "classified", 0))
        for table in ("appointments", "proposals", "audit_log"):
            self.assertEqual(self.count(table), 0, table)
        self.assertEqual(self.sender.texts[-1], "Request received -- the clinic will confirm shortly.")
        self.assertNotIn("needs_staff_reason", json.loads(row["slots_json"]))   # no new note when simply switched off
        self.assertEqual(self.count("slot_holds"), 1)


class AgentSwitchTests(AgentRouteCase):
    def test_agent_off_restores_the_previous_behaviour(self):
        with mock.patch.dict(os.environ, {"WHATSAPP_AGENT_ENABLED": "0"}):
            self.say("mujhe appointment chahiye")
        self.assertEqual(self.sender.texts, [wa.acknowledgment_text("hinglish")])   # generic ack, no menu
        row = self.last_row()
        self.assertEqual((row["intent"], row["status"], row["agent_handled"]), ("book_appointment", "classified", 0))
        self.assertNotIn("via", json.loads(row["slots_json"]))
        self.assertTrue(json.loads(row["slots_json"])["suggestion_note"])         # the old pre-filled suggestion
        self.assertEqual(self.count("wa_sessions"), 0)
        self.assertEqual(self.events(), [])

    def test_agent_off_still_answers_status_questions_the_old_way(self):
        self.approve_booking(appt_date=datetime.now().date().isoformat(), start_time="09:00")
        self.sender.calls.clear()
        with mock.patch.dict(os.environ, {"WHATSAPP_AGENT_ENABLED": "0"}):
            self.say("what is my token")
        self.assertEqual(self.last_row()["status"], "dismissed")
        self.assertTrue(any("T-01" in t for t in self.sender.texts))

    def test_legacy_ack_respects_dry_run(self):
        clinic_app.NOTIFY_SENDER = None
        with mock.patch.dict(os.environ, {"WHATSAPP_AGENT_ENABLED": "0"}):
            self.say("hello there")
        self.assertEqual(self.sender.calls, [])
        # ...and the claim was released, so a later live message can still be acknowledged
        self.assertEqual(self.count("wa_acks"), 0)


if __name__ == "__main__":
    unittest.main()
