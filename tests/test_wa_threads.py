"""Staff view of WhatsApp conversations: thread data, Take over / Resume
agent, and the plain reply box (sent through the outbox, 24h-window gated).
Route tests use the Flask test client with a recording fake sender."""
import json
import os
import re
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["WHATSAPP_NOTIFY_MODE"] = "dry_run"
os.environ.setdefault("SARVAM_API_KEY", "test-not-real")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_wa_agent_routes import AgentRouteCase, AgentSender, CLOCK_NOW, TOMORROW, WA, WA2, clinic_app  # noqa: E402

from clinic import conversation as cv  # noqa: E402
from clinic import wa_threads  # noqa: E402

SCHEMA = (Path(__file__).resolve().parents[1] / "clinic" / "schema.sql").read_text()


def utc(minutes_ago):
    return (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


class ThreadDataTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.n = 0

    def inbound(self, wa_id, text, minutes_ago, status="dismissed", agent_handled=1, slots=None, mtype="text"):
        self.n += 1
        self.conn.execute(
            "INSERT INTO wa_messages (wa_message_id, wa_id, message_type, raw_text, status, agent_handled, slots_json, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("w{}".format(self.n), wa_id, mtype, text, status, agent_handled, json.dumps(slots) if slots else None, utc(minutes_ago)))
        self.conn.commit()

    def outbound(self, wa_id, body, minutes_ago, event="conv_reply", status="sent", interactive=None, error=None):
        self.n += 1
        self.conn.execute(
            "INSERT INTO notifications (wa_id, event, dedup_key, body, status, created_at, interactive_json, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (wa_id, event, "k{}".format(self.n), body, status, utc(minutes_ago), json.dumps(interactive) if interactive else None, error))
        self.conn.commit()

    def test_transcript_interleaves_inbound_and_outbound_chronologically(self):
        self.inbound("919876543210", "hello", 10)
        self.outbound("919876543210", "Hello, welcome", 9, interactive={"type": "button", "buttons": [{"id": "menu:book", "title": "Book appointment"}]})
        self.inbound("919876543210", "Book appointment", 8)
        self.outbound("919876543210", "We are running late", 1, event="staff_message")
        thread = wa_threads.conversation_threads(self.conn)[0]
        self.assertEqual([(i["dir"], i["text"]) for i in thread["items"]],
                         [("in", "hello"), ("out", "Hello, welcome"), ("in", "Book appointment"), ("out", "We are running late")])
        self.assertEqual(thread["items"][1]["options"], ["Book appointment"])
        self.assertEqual(thread["items"][3]["event"], "staff_message")
        self.assertEqual(thread["wa_id"], "919876543210")

    def test_replies_stay_with_their_message_when_everything_happens_in_one_second(self):
        same = utc(5)
        for text, mid in (("hello", 1), ("book", 2), ("tomorrow", 3)):
            self.conn.execute(
                "INSERT INTO wa_messages (id, wa_message_id, wa_id, message_type, raw_text, status, received_at) "
                "VALUES (?, ?, '919876543210', 'text', ?, 'dismissed', ?)", (mid, "s%d" % mid, text, same))
            self.conn.execute(
                "INSERT INTO notifications (wa_id, event, dedup_key, body, status, created_at) "
                "VALUES ('919876543210', 'conv_reply', ?, ?, 'sent', ?)", ("conv_reply:%d:0" % mid, "reply to " + text, same))
        self.conn.execute(
            "INSERT INTO notifications (wa_id, event, dedup_key, body, status, created_at) "
            "VALUES ('919876543210', 'staff_message', 'sm1', 'staff here', 'sent', ?)", (same,))
        self.conn.commit()
        items = wa_threads.conversation_threads(self.conn)[0]["items"]
        self.assertEqual([i["text"] for i in items],
                         ["hello", "reply to hello", "book", "reply to book", "tomorrow", "reply to tomorrow", "staff here"])

    def test_patient_name_mode_window_and_open_items(self):
        self.conn.execute("INSERT INTO patients (name, phone) VALUES ('Sunita Devi', '9876543210')")
        self.inbound("919876543210", "hello", 5)
        self.inbound("919876543210", "talk to a person", 4, status="needs_human_reply", agent_handled=0,
                     slots={"flag": "escalation"})
        self.inbound("919111122223", "hi", 5 * 24 * 60)
        cv.set_mode(self.conn, "919876543210", "human", now=datetime(2026, 10, 5, 10, 0))
        threads = {t["wa_id"]: t for t in wa_threads.conversation_threads(self.conn)}
        a, b = threads["919876543210"], threads["919111122223"]
        self.assertEqual((a["name"], a["mode"], a["window_open"], a["open_items"], a["emergency"]),
                         ("Sunita Devi", "human", True, 1, False))
        self.assertEqual((b["name"], b["mode"], b["window_open"], b["open_items"]), (None, "agent", False, 0))

    def test_emergency_threads_come_first_even_if_older(self):
        self.inbound("919111122223", "chest pain", 500, status="needs_human_reply", agent_handled=0, slots={"flag": "emergency"})
        self.inbound("919876543210", "hello", 1)
        threads = wa_threads.conversation_threads(self.conn)
        self.assertEqual([t["wa_id"] for t in threads], ["919111122223", "919876543210"])
        self.assertTrue(threads[0]["emergency"])
        self.assertEqual(threads[0]["items"][0]["flag"], "emergency")

    def test_resolved_emergency_no_longer_ranks_first(self):
        self.inbound("919111122223", "chest pain", 500, status="dismissed", agent_handled=0, slots={"flag": "emergency"})
        self.inbound("919876543210", "hello", 1)
        self.assertEqual(wa_threads.conversation_threads(self.conn)[0]["wa_id"], "919876543210")

    def test_same_person_in_two_wa_id_formats_is_one_thread(self):
        self.inbound("919876543210", "hello", 10)
        self.inbound("9876543210", "hi again", 5)
        self.outbound("919876543210", "reply", 4)
        threads = wa_threads.conversation_threads(self.conn)
        self.assertEqual(len(threads), 1)
        self.assertEqual(len(threads[0]["items"]), 3)

    def test_voice_notes_failed_sends_and_per_thread_limit(self):
        self.inbound("919876543210", "mujhe appointment chahiye", 30, mtype="audio")
        self.outbound("919876543210", "Hello", 29, status="failed", error="boom")
        self.outbound("919876543210", "Hello 2", 28, status="blocked_no_window")
        self.outbound("919876543210", "Hello 3", 27, status="sent")
        items = wa_threads.conversation_threads(self.conn)[0]["items"]
        self.assertTrue(items[0]["voice"])
        self.assertEqual([i["retry"] for i in items[1:]], [True, True, False])
        self.assertEqual(items[1]["error"], "boom")
        for i in range(10):
            self.outbound("919876543210", "m{}".format(i), 20 - i)
        limited = wa_threads.conversation_threads(self.conn, per_thread=5)[0]["items"]
        self.assertEqual(len(limited), 5)
        self.assertEqual(limited[-1]["text"], "m9")

    def test_max_threads(self):
        for i in range(5):
            self.inbound("91900000000{}".format(i), "hi", 10 - i)
        self.assertEqual(len(wa_threads.conversation_threads(self.conn, max_threads=3)), 3)

    def test_other_numbers_messages_never_leak_into_a_thread(self):
        self.inbound("919876543210", "mine", 5)
        self.inbound("919111122223", "theirs", 4)
        self.outbound("919111122223", "private reply", 3)
        thread = [t for t in wa_threads.conversation_threads(self.conn) if t["wa_id"] == "919876543210"][0]
        self.assertEqual([i["text"] for i in thread["items"]], ["mine"])

    def test_known_sender(self):
        self.inbound("919876543210", "hi", 1)
        self.assertTrue(wa_threads.known_sender(self.conn, "919876543210"))
        self.assertFalse(wa_threads.known_sender(self.conn, "910000000000"))


class ThreadRouteTests(AgentRouteCase):
    def test_threads_json_endpoint_shows_the_conversation(self):
        self.say("hello")
        threads = self.client.get("/wa/threads").get_json()
        self.assertEqual(len(threads), 1)
        t = threads[0]
        self.assertEqual((t["wa_id"], t["mode"], t["window_open"]), (WA, "agent", True))
        self.assertEqual([i["dir"] for i in t["items"]], ["in", "out"])
        self.assertEqual(t["items"][1]["event"], "conv_reply")
        self.assertEqual(t["items"][1]["options"], ["Book appointment", "Reschedule", "Cancel appointment"])
        self.assertEqual(t["items"][1]["status"], "sent")

    def test_dashboard_embeds_the_threads(self):
        self.say("hello")
        html = self.client.get("/").get_data(as_text=True)
        match = re.search(r'<script id="wa-threads-data" type="application/json">(.*?)</script>', html, re.S)
        self.assertEqual(json.loads(match.group(1))[0]["wa_id"], WA)
        self.assertIn('id="wa-threads-list"', html)
        self.assertIn("wa_threads.js", html)

    def test_patient_text_cannot_break_out_of_the_embedded_json(self):
        evil = '</script><script>alert(1)</script>'
        self.say(evil)
        self.conn.execute("UPDATE wa_messages SET status='needs_human_reply', agent_handled=0")
        self.conn.commit()
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn(evil, html)
        for script_id in ("wa-inbox-data", "wa-threads-data"):
            body = re.search(r'<script id="%s" type="application/json">(.*?)</script>' % script_id, html, re.S).group(1)
            self.assertIn(evil, json.dumps(json.loads(body)))                 # round-trips intact

    def test_take_over_silences_the_agent_and_resume_brings_it_back(self):
        self.say("hello")
        self.sender.calls.clear()
        response = self.client.post("/wa/threads/{}/mode".format(WA), json={"mode": "human"})
        self.assertEqual(response.get_json(), {"ok": True, "mode": "human"})
        self.assertEqual(cv.get_mode(self.conn, WA), "human")
        self.say("book an appointment tomorrow 4 pm")
        self.assertEqual(self.sender.calls, [])
        row = self.last_row()
        self.assertEqual(row["status"], "needs_human_reply")
        self.assertEqual(json.loads(row["slots_json"])["flag"], "human_mode")
        self.assertEqual(self.client.get("/wa/threads").get_json()[0]["mode"], "human")

        self.client.post("/wa/threads/{}/mode".format(WA), json={"mode": "agent"})
        self.say("hello")
        self.assertEqual(len(self.sender.calls), 1)

    def test_mode_validation(self):
        self.say("hello")
        self.assertEqual(self.client.post("/wa/threads/{}/mode".format(WA), json={"mode": "robot"}).status_code, 400)
        self.assertEqual(self.client.post("/wa/threads/{}/mode".format(WA), json={}).status_code, 400)
        self.assertEqual(self.client.post("/wa/threads/910000000000/mode", json={"mode": "human"}).status_code, 404)
        self.assertEqual(self.count("wa_sessions"), 1)           # nothing created for the unknown number

    def test_staff_reply_inside_the_window_is_sent_through_the_outbox(self):
        self.say("hello")
        self.sender.calls.clear()
        result = self.client.post("/wa/threads/{}/reply".format(WA), json={"text": "  Dr. Rao can see you at 5.  "}).get_json()
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "sent")
        self.assertTrue(result["window_open"])
        self.assertEqual(self.sender.calls, [(WA, "Dr. Rao can see you at 5.", None)])
        self.assertEqual(self.events()[-1], "staff_message")
        thread = self.client.get("/wa/threads").get_json()[0]
        self.assertEqual(thread["items"][-1]["event"], "staff_message")
        self.assertEqual(thread["items"][-1]["text"], "Dr. Rao can see you at 5.")

    def test_staff_reply_outside_the_window_is_blocked_and_not_sent(self):
        self.conn.execute(
            "INSERT INTO wa_messages (wa_message_id, wa_id, message_type, raw_text, status, received_at) "
            "VALUES ('old', ?, 'text', 'hi', 'dismissed', ?)", (WA2, utc(60 * 30)))
        self.conn.commit()
        result = self.client.post("/wa/threads/{}/reply".format(WA2), json={"text": "Hello?"}).get_json()
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "blocked_no_window")
        self.assertFalse(result["window_open"])
        self.assertEqual(self.sender.calls, [])
        thread = self.client.get("/wa/threads").get_json()[0]
        self.assertFalse(thread["window_open"])
        self.assertEqual(thread["items"][-1]["status"], "blocked_no_window")
        self.assertTrue(thread["items"][-1]["retry"])

    def test_staff_reply_failure_is_reported_not_raised(self):
        self.say("hello")
        clinic_app.NOTIFY_SENDER = AgentSender(fail_with=RuntimeError("whatsapp down"))
        result = self.client.post("/wa/threads/{}/reply".format(WA), json={"text": "hi"}).get_json()
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("whatsapp down", result["error"])

    def test_staff_reply_validation(self):
        self.say("hello")
        post = lambda body, wa=WA: self.client.post("/wa/threads/{}/reply".format(wa), json=body)
        self.assertEqual(post({"text": "   "}).status_code, 400)
        self.assertEqual(post({}).status_code, 400)
        self.assertEqual(post({"text": "x" * 1001}).status_code, 400)
        self.assertEqual(post({"text": "hi"}, "910000000000").status_code, 404)    # only numbers that wrote to us
        self.assertEqual(self.events().count("staff_message"), 0)

    def test_staff_reply_in_dry_run_records_without_sending(self):
        self.say("hello")
        clinic_app.NOTIFY_SENDER = None
        result = self.client.post("/wa/threads/{}/reply".format(WA), json={"text": "hi"}).get_json()
        self.assertEqual(result["status"], "dry_run")

    def test_identical_staff_messages_are_both_sent(self):
        self.say("hello")
        self.sender.calls.clear()
        for _ in range(2):
            self.client.post("/wa/threads/{}/reply".format(WA), json={"text": "one moment"})
        self.assertEqual(self.sender.texts, ["one moment", "one moment"])

    def test_retry_from_the_thread_resends_a_blocked_message(self):
        self.say("hello")
        clinic_app.NOTIFY_SENDER = AgentSender(fail_with=RuntimeError("down"))
        nid = self.client.post("/wa/threads/{}/reply".format(WA), json={"text": "hi"}).get_json()["notification_id"]
        clinic_app.NOTIFY_SENDER = self.sender
        self.sender.calls.clear()
        self.assertTrue(self.client.post("/notifications/{}/retry".format(nid)).get_json()["ok"])
        self.assertEqual(self.sender.texts, ["hi"])

    def test_queue_tab_notification_list_is_not_flooded_by_conversation_replies(self):
        self.say("hello")
        self.client.post("/wa/threads/{}/reply".format(WA), json={"text": "hi"})
        html = self.client.get("/queue/partial").get_data(as_text=True)
        self.assertNotIn("conv_reply", html)
        self.assertNotIn("staff_message", html)


if __name__ == "__main__":
    unittest.main()
