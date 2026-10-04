import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


class FrontEndWiringTests(unittest.TestCase):
    """The page side of the conversation memory: the chip, the questions with
    option buttons, live card edits, and telling the server when a card closes."""

    def setUp(self):
        self.js = (ROOT / "static" / "live_voice.js").read_text()
        self.card = (ROOT / "static" / "review_card.js").read_text()
        self.html = (ROOT / "templates" / "dashboard.html").read_text()

    def test_there_is_no_talking_about_chip(self):
        for needle in ("context-chip", "Talking about", "renderContext"):
            self.assertNotIn(needle, self.html + self.js)

    def test_page_handles_every_new_server_event(self):
        for event in ("assistant_question", "card_update", "assistant_note"):
            self.assertIn('socket.on("%s"' % event, self.js)

    def test_page_tells_the_server_about_its_own_actions(self):
        for event in ("card_closed", "pick_option"):
            self.assertIn('socket.emit("%s"' % event, self.js)

    def test_a_card_is_closed_on_both_approve_and_reject(self):
        self.assertEqual(self.js.count("cardClosed(cardId);"), 2)  # once on approve, once on reject

    def test_cards_carry_an_id_and_can_be_updated_in_place(self):
        self.assertIn("data-card-id", self.card)
        self.assertIn("applyChanges", self.card)
        self.assertIn("ReviewCard.applyChanges", self.js)

    def test_there_is_no_voice_approval_anywhere(self):
        # Approve stays a button: nothing in the page or the server turns speech into an approval.
        self.assertNotIn('emit("approve"', self.js)
        server = (ROOT / "clinic" / "voice_turns.py").read_text() + (ROOT / "clinic" / "voice_context.py").read_text()
        for forbidden in ("core.confirm", "core.propose", "/approve"):
            self.assertNotIn(forbidden, server)


class ServerWiringTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("SARVAM_API_KEY", "test-not-real")

    def test_session_events_flow_through_the_context(self):
        import sqlite3
        from unittest.mock import patch
        from clinic.adapters.local_sqlite import LocalSQLiteAdapter
        from clinic.realtime_voice import VoiceSession

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript((ROOT / "clinic" / "schema.sql").read_text())
        self.addCleanup(conn.close)
        conn.execute("INSERT INTO patients (name, phone, age) VALUES ('Mohan Lal', '9000000002', 40)")
        conn.execute("INSERT INTO patients (name, phone, age) VALUES ('Mohan Das', '9000000003', 40)")
        conn.commit()
        emitted = []
        adapter = LocalSQLiteAdapter()
        session = VoiceSession("sid", "key", lambda e, d: emitted.append((e, d)), lambda: conn, adapter, adapter,
                               frozenset(("book_appointment",)))

        def names(event):
            return [d for e, d in emitted if e == event]

        def fake_name(text):
            return "Mohan" if "Mohan" in text else None

        with patch("clinic.nlu.parser.pick_intent", return_value=None), \
                patch("clinic.nlu.parser.extract_name", side_effect=fake_name):
            session._handle_final_transcript("book an appointment for Mohan")
            question = names("assistant_question")[-1]
            self.assertEqual(question["kind"], "choose_patient")
            self.assertEqual(sorted(o["label"].split(" (")[0] for o in question["options"]), ["Mohan Das", "Mohan Lal"])
            self.assertEqual(names("context_update")[-1]["awaiting"], "choose_patient")

            session._handle_final_transcript("Lal")
            self.assertEqual(names("assistant_question")[-1]["kind"], "date")
            session._handle_final_transcript("kal")
            self.assertEqual(names("assistant_question")[-1]["kind"], "time")
            session._handle_final_transcript("5 baje")
            card = names("review_card")[-1]
            self.assertEqual(card["slots"]["patient_name"], "Mohan Lal")
            self.assertEqual(card["slots"]["start_time"], "05:00")
            self.assertTrue(card["card_id"])
            self.assertEqual(names("context_update")[-1]["patient"], "Mohan Lal")

            session._handle_final_transcript("make it 6 pm instead")
            update = names("card_update")[-1]
            self.assertEqual(update["card_id"], card["card_id"])
            self.assertEqual(update["changes"], {"start_time": "18:00"})

            session.card_closed(card["card_id"])
            self.assertIsNone(session.context.open_card)

            session.context_clear()
            self.assertIsNone(names("context_update")[-1]["patient"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0], 0)

    def test_tapping_an_option_works_and_a_stale_tap_is_ignored(self):
        import sqlite3
        from unittest.mock import patch
        from clinic.adapters.local_sqlite import LocalSQLiteAdapter
        from clinic.realtime_voice import VoiceSession

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript((ROOT / "clinic" / "schema.sql").read_text())
        self.addCleanup(conn.close)
        for name, phone in (("Mohan Lal", "9000000002"), ("Mohan Das", "9000000003")):
            conn.execute("INSERT INTO patients (name, phone, age) VALUES (?, ?, 40)", (name, phone))
        conn.commit()
        emitted = []
        adapter = LocalSQLiteAdapter()
        session = VoiceSession("sid", "key", lambda e, d: emitted.append((e, d)), lambda: conn, adapter, adapter,
                               frozenset(("book_appointment",)))
        session.pick_option(0)  # nothing is being asked: ignored, no crash
        self.assertEqual([e for e, _ in emitted if e == "assistant_question"], [])
        with patch("clinic.nlu.parser.pick_intent", return_value=None), \
                patch("clinic.nlu.parser.extract_name", return_value="Mohan"):
            session._handle_final_transcript("book an appointment for Mohan")
            session.pick_option(1)
        last = [d for e, d in emitted if e == "assistant_question"][-1]
        self.assertEqual(last["kind"], "date")


if __name__ == "__main__":
    unittest.main()
