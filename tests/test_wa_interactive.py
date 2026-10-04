"""Interactive WhatsApp messages (reply buttons / lists): payload shapes,
limits, inbound parsing, and the outbox plumbing. Nothing here sends anything:
the builders are pure and the one network call is mocked."""
import json
import os
import sqlite3
import sys
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ["WHATSAPP_NOTIFY_MODE"] = "dry_run"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import conv_templates as ct
from clinic import conversation as cv
from clinic import notify, whatsapp
from clinic.notify import Now

SCHEMA = (Path(__file__).resolve().parents[1] / "clinic" / "schema.sql").read_text()
NOW = Now(local=datetime(2026, 10, 5, 10, 0), utc=datetime(2026, 10, 5, 4, 30))
WA = "919876543210"


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def open_window(conn, wa_id=WA, text="hello"):
    conn.execute(
        "INSERT INTO wa_messages (wa_message_id, wa_id, message_type, raw_text, received_at) VALUES (?, ?, 'text', ?, ?)",
        ("wamid.w" + wa_id + text, wa_id, text, (NOW.utc - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()


class ButtonBodyTests(unittest.TestCase):
    def test_exact_payload_shape(self):
        body = whatsapp.build_reply_buttons_body(
            WA, "Please confirm", [("confirm:yes", "Confirm request"), ("confirm:change", "Change")])
        self.assertEqual(body, {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": WA,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": "Please confirm"},
                "action": {"buttons": [
                    {"type": "reply", "reply": {"id": "confirm:yes", "title": "Confirm request"}},
                    {"type": "reply", "reply": {"id": "confirm:change", "title": "Change"}},
                ]},
            },
        })

    def test_accepts_dict_items_and_exactly_three_buttons(self):
        body = whatsapp.build_reply_buttons_body(
            WA, "Menu", [{"id": "menu:book", "title": "T" * 20}, ("menu:reschedule", "b"), ("menu:cancel", "c")])
        self.assertEqual(len(body["interactive"]["action"]["buttons"]), 3)

    def test_limits(self):
        build = whatsapp.build_reply_buttons_body
        with self.assertRaises(ValueError):
            build(WA, "x", [(str(i), "t") for i in range(4)])                # max 3
        with self.assertRaises(ValueError):
            build(WA, "x", [])
        with self.assertRaises(ValueError):
            build(WA, "x", [("a", "T" * 21)])                                 # title <= 20
        with self.assertRaises(ValueError):
            build(WA, "x", [("a", "")])
        with self.assertRaises(ValueError):
            build(WA, "x", [("a", "t"), ("a", "u")])                          # unique ids
        with self.assertRaises(ValueError):
            build(WA, "x", [("i" * 257, "t")])                                # id <= 256
        with self.assertRaises(ValueError):
            build(WA, "", [("a", "t")])
        with self.assertRaises(ValueError):
            build(WA, "x" * 1025, [("a", "t")])                               # body <= 1024
        build(WA, "x" * 1024, [("a", "t")])


class ListBodyTests(unittest.TestCase):
    def test_exact_payload_shape(self):
        body = whatsapp.build_list_body(
            WA, "Free times", "Choose time", [("slot:2026-10-06T09:00", "9:00 AM"), ("slot:2026-10-06T11:00", "11:00 AM")])
        self.assertEqual(body, {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": WA,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": "Free times"},
                "action": {"button": "Choose time", "sections": [{"rows": [
                    {"id": "slot:2026-10-06T09:00", "title": "9:00 AM"},
                    {"id": "slot:2026-10-06T11:00", "title": "11:00 AM"},
                ]}]},
            },
        })

    def test_section_title_and_row_description(self):
        body = whatsapp.build_list_body(WA, "x", "Open", [{"id": "a", "title": "A", "description": "d" * 72}], "Times")
        section = body["interactive"]["action"]["sections"][0]
        self.assertEqual(section["title"], "Times")
        self.assertEqual(section["rows"][0]["description"], "d" * 72)

    def test_limits(self):
        build = whatsapp.build_list_body
        ten = [(str(i), "row") for i in range(10)]
        build(WA, "x", "Open", ten)
        with self.assertRaises(ValueError):
            build(WA, "x", "Open", ten + [("10", "row")])                    # max 10 rows
        with self.assertRaises(ValueError):
            build(WA, "x", "Open", [])
        with self.assertRaises(ValueError):
            build(WA, "x", "Open", [("a", "T" * 25)])                        # row title <= 24
        build(WA, "x", "Open", [("a", "T" * 24)])
        with self.assertRaises(ValueError):
            build(WA, "x", "Open", [("a", "t", "d" * 73)])                   # description <= 72
        with self.assertRaises(ValueError):
            build(WA, "x", "L" * 21, [("a", "t")])                           # button label <= 20
        with self.assertRaises(ValueError):
            build(WA, "x", "", [("a", "t")])
        with self.assertRaises(ValueError):
            build(WA, "x", "Open", [("a", "t"), ("a", "u")])
        with self.assertRaises(ValueError):
            build(WA, "x", "Open", [("a", "t")], "S" * 25)


class SpecDispatchTests(unittest.TestCase):
    def test_interactive_spec_round_trip(self):
        buttons = whatsapp.build_interactive_body(WA, "t", {"type": "button", "buttons": [{"id": "a", "title": "A"}]})
        self.assertEqual(buttons["interactive"]["type"], "button")
        rows = whatsapp.build_interactive_body(
            WA, "t", {"type": "list", "button": "Choose", "rows": [{"id": "a", "title": "A"}]})
        self.assertEqual(rows["interactive"]["action"]["button"], "Choose")
        for bad in (None, {}, {"type": "carousel"}):
            with self.assertRaises(ValueError):
                whatsapp.build_interactive_body(WA, "t", bad)

    def test_every_reply_of_every_flow_builds_a_valid_body(self):
        """The agent's own Reply.interactive() specs always satisfy WhatsApp's limits."""
        conn = make_db()
        conn.execute("INSERT INTO patients (name, phone) VALUES ('Sunita Devi', '9876543210')")
        for start in ("09:00", "09:15"):
            conn.execute("INSERT INTO appointments (patient_id, appt_date, start_time) VALUES (1, '2026-10-08', ?)", (start,))
        conn.commit()
        clock = datetime(2026, 10, 5, 10, 0)
        seen = {"button": 0, "list": 0}
        for lang_script in (
            ["hello", "book", "tomorrow", "4 pm", ("confirm:yes",), "reschedule", ("appt:1",), "friday", "11 am"],
            ["नमस्ते", "मुझे अपॉइंटमेंट चाहिए", "कल", "शाम 4 बजे", ("confirm:change",), "cancel", ("appt:2",)],
            ["namaste", "blorp", "blorp"],
        ):
            conn.execute("DELETE FROM wa_sessions")
            for step in lang_script:
                text, choice = (None, step[0]) if isinstance(step, tuple) else (step, None)
                result = cv.handle_inbound(conn, WA, text or "", choice_id=choice, now=clock,
                                           intent_picker=lambda t: "unclear")
                for reply in result.replies:
                    spec = reply.interactive()
                    if spec:
                        whatsapp.build_interactive_body(WA, reply.text, spec)
                        seen[spec["type"]] += 1
        self.assertGreater(seen["button"], 5)
        self.assertGreater(seen["list"], 1)


class SendInteractiveTests(unittest.TestCase):
    def test_invalid_spec_fails_before_credentials_are_read_or_anything_is_sent(self):
        with patch.dict(os.environ, {}, clear=True), patch("clinic.whatsapp.httpx.post") as post:
            with self.assertRaises(ValueError):          # not KeyError: validation comes first
                whatsapp.send_interactive(WA, "x", {"type": "button", "buttons": []})
        post.assert_not_called()

    def test_posts_the_built_body_to_the_messages_endpoint(self):
        env = {"WHATSAPP_ACCESS_TOKEN": "test-token", "WHATSAPP_PHONE_NUMBER_ID": "12345"}
        spec = {"type": "button", "buttons": [{"id": "a", "title": "A"}]}
        with patch.dict(os.environ, env), patch("clinic.whatsapp.httpx.post") as post:
            post.return_value.json.return_value = {"messages": [{"id": "x"}]}
            result = whatsapp.send_interactive(WA, "hello", spec)
        self.assertEqual(result, {"messages": [{"id": "x"}]})
        args, kwargs = post.call_args
        self.assertTrue(args[0].endswith("/12345/messages"))
        self.assertEqual(kwargs["json"], whatsapp.build_interactive_body(WA, "hello", spec))
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer test-token"})

    def test_plain_send_message_body_is_unchanged(self):
        self.assertEqual(whatsapp.build_text_body(WA, "hi"),
                         {"messaging_product": "whatsapp", "to": WA, "type": "text", "text": {"body": "hi"}})


class InboundInteractiveParsingTests(unittest.TestCase):
    def wrap(self, message):
        return {"entry": [{"changes": [{"value": {"messages": [message]}}]}]}

    def test_button_reply(self):
        parsed = whatsapp.parse_webhook_payload(self.wrap({
            "from": WA, "id": "wamid.b1", "type": "interactive", "timestamp": "1790000000",
            "interactive": {"type": "button_reply", "button_reply": {"id": "confirm:yes", "title": "Confirm request"}}}))
        self.assertEqual(parsed, {
            "wa_message_id": "wamid.b1", "wa_id": WA, "message_type": "text", "text": "Confirm request",
            "media_id": None, "timestamp": 1790000000, "choice_id": "confirm:yes"})

    def test_list_reply(self):
        parsed = whatsapp.parse_webhook_payload(self.wrap({
            "from": WA, "id": "wamid.l1", "type": "interactive",
            "interactive": {"type": "list_reply",
                            "list_reply": {"id": "slot:2026-10-06T09:00", "title": "9:00 AM", "description": ""}}}))
        self.assertEqual(parsed["message_type"], "text")        # stored as text: the CHECK constraint is unchanged
        self.assertEqual((parsed["text"], parsed["choice_id"]), ("9:00 AM", "slot:2026-10-06T09:00"))

    def test_malformed_interactive_is_ignored(self):
        for interactive in ({}, {"type": "nfm_reply"}, {"type": "button_reply", "button_reply": {}},
                            {"type": "list_reply", "list_reply": "x"}):
            self.assertIsNone(whatsapp.parse_webhook_payload(self.wrap(
                {"from": WA, "id": "w", "type": "interactive", "interactive": interactive})))

    def test_plain_text_has_no_choice_id_key(self):
        parsed = whatsapp.parse_webhook_payload(self.wrap({"from": WA, "id": "w", "type": "text", "text": {"body": "hi"}}))
        self.assertNotIn("choice_id", parsed)

    def test_tapped_choice_ids_are_understood_by_the_dialog_manager(self):
        for choice in ("menu:book", "day:2026-10-06", "slot:2026-10-06T09:00", "appt:7", "confirm:yes"):
            self.assertIsNotNone(cv.parse_choice(choice))


class OutboxInteractiveTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        open_window(self.conn)
        self.spec = {"type": "button", "buttons": [{"id": "menu:book", "title": "Book appointment"}]}

    def pending(self, **kw):
        return notify.enqueue_conv_reply(self.conn, WA, "Hello", "5", 0, interactive=self.spec, now=NOW, **kw)

    def test_interactive_spec_is_stored_with_the_row(self):
        nid = self.pending()
        row = self.conn.execute("SELECT * FROM notifications WHERE id = ?", (nid,)).fetchone()
        self.assertEqual((row["event"], row["status"], row["wa_id"]), ("conv_reply", "pending", WA))
        self.assertEqual(json.loads(row["interactive_json"]), self.spec)

    def test_a_sender_that_accepts_interactive_gets_the_spec(self):
        calls = []
        self.pending()
        notify.flush(self.conn, lambda wa_id, text, interactive=None: calls.append((wa_id, text, interactive)), now=NOW)
        self.assertEqual(calls, [(WA, "Hello", self.spec)])
        self.assertEqual(self.conn.execute("SELECT status FROM notifications").fetchone()[0], "sent")

    def test_a_plain_sender_gets_the_options_as_text(self):
        calls = []
        self.pending()
        notify.flush(self.conn, lambda wa_id, text: calls.append((wa_id, text)), now=NOW)
        self.assertEqual(calls, [(WA, "Hello\n\n- Book appointment")])

    def test_plain_rows_still_call_the_sender_with_two_arguments(self):
        calls = []
        notify.enqueue(self.conn, event="your_turn", dedup_key="k", body="x", wa_id=WA, now=NOW)
        notify.flush(self.conn, lambda wa_id, text: calls.append((wa_id, text)), now=NOW)
        self.assertEqual(calls, [(WA, "x")])

    def test_live_sender_routes_interactive_and_plain_separately(self):
        with patch("clinic.whatsapp.send_interactive") as interactive, patch("clinic.whatsapp.send_message") as plain:
            notify.live_sender(WA, "hi", interactive=self.spec)
            notify.live_sender(WA, "hi")
        interactive.assert_called_once_with(WA, "hi", self.spec)
        plain.assert_called_once_with(WA, "hi")

    def test_conv_reply_is_deduplicated_per_inbound_message_and_position(self):
        first = self.pending()
        again = self.pending()
        other_position = notify.enqueue_conv_reply(self.conn, WA, "Two", "5", 1, now=NOW)
        other_message = notify.enqueue_conv_reply(self.conn, WA, "Hello", "6", 0, now=NOW)
        self.assertIsNotNone(first)
        self.assertIsNone(again)
        self.assertIsNotNone(other_position)
        self.assertIsNotNone(other_message)

    def test_conv_replies_obey_the_24_hour_window(self):
        self.conn.execute("DELETE FROM wa_messages")
        self.conn.commit()
        self.pending()
        calls = []
        notify.flush(self.conn, lambda w, t: calls.append(t), now=NOW)
        self.assertEqual(calls, [])
        self.assertEqual(self.conn.execute("SELECT status FROM notifications").fetchone()[0], "blocked_no_window")

    def test_failed_conv_reply_is_retried_briefly_but_not_when_stale(self):
        def boom(wa_id, text):
            raise RuntimeError("down")
        self.pending()
        notify.flush(self.conn, boom, now=NOW)
        self.assertEqual(self.conn.execute("SELECT status, attempts FROM notifications").fetchone()[:], ("failed", 1))
        calls = []
        later = Now(local=NOW.local + timedelta(minutes=2), utc=NOW.utc + timedelta(minutes=2))
        notify.flush(self.conn, lambda w, t: calls.append(t), now=later)
        self.assertEqual(calls, ["Hello\n\n- Book appointment"])        # retried within 5 minutes

        self.conn.execute("DELETE FROM notifications")
        self.conn.commit()
        self.pending()
        notify.flush(self.conn, boom, now=NOW)
        stale = Now(local=NOW.local + timedelta(minutes=6), utc=NOW.utc + timedelta(minutes=6))
        calls = []
        notify.flush(self.conn, lambda w, t: calls.append(t), now=stale)
        self.assertEqual(calls, [])                                       # a late "which day?" is not sent

    def test_other_events_keep_the_two_hour_retry_window(self):
        def boom(wa_id, text):
            raise RuntimeError("down")
        notify.enqueue(self.conn, event="your_turn", dedup_key="k", body="x", wa_id=WA, now=NOW)
        notify.flush(self.conn, boom, now=NOW)
        calls = []
        later = Now(local=NOW.local + timedelta(minutes=30), utc=NOW.utc + timedelta(minutes=30))
        notify.flush(self.conn, lambda w, t: calls.append(t), now=later)
        self.assertEqual(calls, ["x"])

    def test_flush_can_wait_for_a_delivery_in_progress(self):
        self.pending()
        notify._FLUSH_LOCK.acquire()
        released = threading.Timer(0.15, notify._FLUSH_LOCK.release)
        try:
            self.assertEqual(notify.flush(self.conn, lambda w, t, interactive=None: None, now=NOW), {})   # default: never waits
            released.start()
            counts = notify.flush(self.conn, lambda w, t, interactive=None: None, now=NOW, lock_timeout=3)
        finally:
            released.join()
            if notify._FLUSH_LOCK.locked():
                notify._FLUSH_LOCK.release()
        self.assertEqual(counts, {"sent": 1})

    def test_flush_gives_up_after_the_timeout(self):
        self.pending()
        notify._FLUSH_LOCK.acquire()
        try:
            self.assertEqual(notify.flush(self.conn, lambda w, t, interactive=None: None, now=NOW, lock_timeout=0.05), {})
        finally:
            notify._FLUSH_LOCK.release()

    def test_conv_and_staff_messages_are_kept_out_of_the_queue_tab_list(self):
        self.pending()
        notify.enqueue_staff_message(self.conn, WA, "We are running late", now=NOW)
        notify.enqueue(self.conn, event="your_turn", dedup_key="k", body="x", wa_id=WA, now=NOW)
        self.assertEqual([n["event"] for n in notify.recent_notifications(self.conn)], ["your_turn"])


class NewEventTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        open_window(self.conn, text="hello")

    def test_events_are_registered(self):
        for event in ("conv_reply", "staff_message", "request_declined"):
            self.assertIn(event, notify.EVENTS)

    def test_request_declined_is_the_fixed_text_in_each_language(self):
        self.assertEqual(notify.render("request_declined", "en"),
                         "We couldn't confirm that request. Please send another preferred time.")
        self.assertIn("पुष्टि नहीं कर सके", notify.render("request_declined", "hi"))
        self.assertIn("confirm nahi kar sake", notify.render("request_declined", "hinglish"))
        for msg_id, lang in enumerate(("en", "hi", "hinglish"), start=100):
            nid = notify.notify_request_declined(self.conn, WA, msg_id, language=lang, now=NOW)
            body = self.conn.execute("SELECT body FROM notifications WHERE id = ?", (nid,)).fetchone()[0]
            self.assertEqual(body, notify.render("request_declined", lang))

    def test_request_declined_is_once_per_inbox_item(self):
        self.assertIsNotNone(notify.notify_request_declined(self.conn, WA, 7, language="en", now=NOW))
        self.assertIsNone(notify.notify_request_declined(self.conn, WA, 7, language="en", now=NOW))

    def test_request_declined_uses_the_patients_language_when_none_is_given(self):
        self.conn.execute("DELETE FROM wa_messages")
        open_window(self.conn, text="mujhe appointment chahiye")
        nid = notify.notify_request_declined(self.conn, WA, 8, now=NOW)
        self.assertEqual(self.conn.execute("SELECT language FROM notifications WHERE id = ?", (nid,)).fetchone()[0], "hinglish")

    def test_staff_messages_are_never_deduplicated(self):
        first = notify.enqueue_staff_message(self.conn, WA, "same words", now=NOW)
        second = notify.enqueue_staff_message(self.conn, WA, "same words", now=NOW)
        self.assertTrue(first and second and first != second)
        sent = []
        notify.flush(self.conn, lambda w, t: sent.append(t), now=NOW)
        self.assertEqual(sent, ["same words", "same words"])

    def test_meta_templates_still_well_formed_with_the_new_template(self):
        names = [t["name"] for t in notify.META_TEMPLATES if "request_declined" in t["name"]]
        self.assertEqual(sorted(names), ["clinic_request_declined_en", "clinic_request_declined_hi",
                                         "clinic_request_declined_hinglish"])


if __name__ == "__main__":
    unittest.main()
