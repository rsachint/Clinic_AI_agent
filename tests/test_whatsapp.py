import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import core
from clinic.adapters.local_sqlite import LocalSQLiteAdapter
from clinic.intents import HANDLERS
from clinic.whatsapp import (
    acknowledgment_text, claim_acknowledgment, detect_message_language,
    is_stale, parse_webhook_payload, release_acknowledgment, verify_webhook,
)
from clinic.whatsapp_pipeline import classify_text_message

SCHEMA = (Path(__file__).resolve().parents[1] / "clinic" / "schema.sql").read_text()


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


class VerifyWebhookTests(unittest.TestCase):
    def test_matching_token_returns_challenge(self):
        args = {"hub.mode": "subscribe", "hub.verify_token": "secret123", "hub.challenge": "xyz"}
        import os
        os.environ["WHATSAPP_VERIFY_TOKEN"] = "secret123"
        self.assertEqual(verify_webhook(args), "xyz")

    def test_wrong_token_returns_none(self):
        import os
        os.environ["WHATSAPP_VERIFY_TOKEN"] = "secret123"
        args = {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "xyz"}
        self.assertIsNone(verify_webhook(args))


class ParseWebhookPayloadTests(unittest.TestCase):
    def _wrap(self, message):
        return {"entry": [{"changes": [{"value": {"messages": [message]}}]}]}

    def test_text_message(self):
        payload = self._wrap({"from": "919876543210", "id": "wamid.abc", "type": "text", "text": {"body": "haan aaunga"}})
        result = parse_webhook_payload(payload)
        self.assertEqual(result, {
            "wa_message_id": "wamid.abc", "wa_id": "919876543210",
            "message_type": "text", "text": "haan aaunga", "media_id": None,
            "timestamp": None,
        })

    def test_message_timestamp_is_parsed(self):
        payload = self._wrap({
            "from": "919876543210", "id": "wamid.abc", "type": "text",
            "timestamp": "1790000000", "text": {"body": "hi"},
        })
        self.assertEqual(parse_webhook_payload(payload)["timestamp"], 1790000000)

    def test_audio_message(self):
        payload = self._wrap({"from": "919876543210", "id": "wamid.def", "type": "audio", "audio": {"id": "media123"}})
        result = parse_webhook_payload(payload)
        self.assertEqual(result["message_type"], "audio")
        self.assertEqual(result["media_id"], "media123")
        self.assertIsNone(result["text"])

    def test_status_update_returns_none(self):
        payload = {"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.abc", "status": "delivered"}]}}]}]}
        self.assertIsNone(parse_webhook_payload(payload))

    def test_unsupported_message_type_returns_none(self):
        payload = self._wrap({"from": "919876543210", "id": "wamid.ghi", "type": "image", "image": {"id": "img1"}})
        self.assertIsNone(parse_webhook_payload(payload))

    def test_malformed_payload_returns_none(self):
        self.assertIsNone(parse_webhook_payload({}))


class ClassifyTextMessageTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.adapter = LocalSQLiteAdapter()
        pid = core.propose(self.conn, "register_patient", {"name": "Sunita Devi", "phone": "9876543210", "age": 34})
        _, self.patient_id = core.confirm(self.conn, pid, HANDLERS)
        fid = core.propose(self.conn, "set_followup", {"patient_id": self.patient_id, "due_date": "2026-10-09"})
        _, self.followup_id = core.confirm(self.conn, fid, HANDLERS)

    def test_confirm_resolves_patient_and_nearest_followup(self):
        result = classify_text_message(self.conn, "919876543210", "haan main aaunga", self.adapter)
        self.assertEqual(result["patient_id"], self.patient_id)
        self.assertEqual(result["intent"], "confirm_followup")
        self.assertEqual(result["slots"]["followup_id"], self.followup_id)

    def test_cancel_from_unknown_number_is_unclassified(self):
        result = classify_text_message(self.conn, "911111111111", "cancel karo", self.adapter)
        self.assertIsNone(result["patient_id"])
        self.assertIsNone(result["intent"])

    def test_new_registration_prefills_phone_from_wa_id(self):
        result = classify_text_message(self.conn, "919123456780", "I want to register", self.adapter)
        self.assertEqual(result["intent"], "register_patient")
        self.assertEqual(result["slots"]["phone"], "9123456780")

    def test_known_patient_with_no_pending_followup_is_unclassified(self):
        core.confirm(self.conn, core.propose(self.conn, "cancel_followup", {"followup_id": self.followup_id}), HANDLERS)
        result = classify_text_message(self.conn, "919876543210", "haan aaunga", self.adapter)
        self.assertEqual(result["patient_id"], self.patient_id)
        self.assertIsNone(result["intent"])

    def test_unrelated_message_is_unclassified_but_patient_still_identified(self):
        result = classify_text_message(self.conn, "919876543210", "what is the doctor timing today", self.adapter)
        self.assertEqual(result["patient_id"], self.patient_id)
        self.assertIsNone(result["intent"])


if __name__ == "__main__":
    unittest.main()


class DetectMessageLanguageTests(unittest.TestCase):
    def test_devanagari_is_hindi(self):
        self.assertEqual(detect_message_language("हाँ, मैं आऊंगा"), "hi")

    def test_roman_script_hindi_is_hinglish(self):
        self.assertEqual(detect_message_language("Haan, main kal aaunga"), "hinglish")
        self.assertEqual(detect_message_language("mujhe appointment chahiye"), "hinglish")

    def test_plain_english(self):
        self.assertEqual(detect_message_language("I want to book an appointment"), "en")

    def test_text_with_no_language_signal_is_english(self):
        self.assertEqual(detect_message_language("Test12345"), "en")

    def test_other_indian_script_falls_back_to_bilingual(self):
        self.assertEqual(detect_message_language("வணக்கம்"), "bilingual")

    def test_no_text_falls_back_to_bilingual(self):
        self.assertEqual(detect_message_language(None), "bilingual")
        self.assertEqual(detect_message_language(""), "bilingual")

    def test_each_language_has_its_own_reply(self):
        replies = {acknowledgment_text(lang) for lang in ("en", "hi", "hinglish", "bilingual")}
        self.assertEqual(len(replies), 4)
        self.assertIn("received", acknowledgment_text("en"))
        self.assertIn("धन्यवाद", acknowledgment_text("hi"))
        self.assertIn("Dhanyawad", acknowledgment_text("hinglish"))


class AcknowledgmentThrottleTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.now = 1790000000.0

    def test_first_message_is_claimed(self):
        self.assertIsNotNone(claim_acknowledgment(self.conn, "919876500101", now=self.now))

    def test_second_message_within_cooldown_is_not(self):
        claim_acknowledgment(self.conn, "919876500101", now=self.now)
        self.assertIsNone(claim_acknowledgment(self.conn, "919876500101", now=self.now + 120))

    def test_message_after_cooldown_is_claimed_again(self):
        claim_acknowledgment(self.conn, "919876500101", now=self.now)
        self.assertIsNotNone(claim_acknowledgment(self.conn, "919876500101", now=self.now + 11 * 60))

    def test_cooldown_is_per_sender(self):
        claim_acknowledgment(self.conn, "919876500101", now=self.now)
        self.assertIsNotNone(claim_acknowledgment(self.conn, "919876543210", now=self.now))

    def test_released_claim_lets_the_next_message_retry(self):
        ack_id = claim_acknowledgment(self.conn, "919876500101", now=self.now)
        release_acknowledgment(self.conn, ack_id)
        self.assertIsNotNone(claim_acknowledgment(self.conn, "919876500101", now=self.now + 5))


class StaleMessageTests(unittest.TestCase):
    def test_recent_message_is_not_stale(self):
        self.assertFalse(is_stale(1790000000, now=1790000000 + 60))

    def test_message_older_than_an_hour_is_stale(self):
        self.assertTrue(is_stale(1790000000, now=1790000000 + 2 * 3600))

    def test_missing_timestamp_is_not_treated_as_stale(self):
        self.assertFalse(is_stale(None, now=1790000000))
