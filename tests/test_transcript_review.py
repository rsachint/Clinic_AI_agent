import os
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


class ReviewSessionTests(unittest.TestCase):
    """With review on, what the microphone heard is held, shown in an editable
    box, and only the text the person sends back goes through the pipeline."""

    def setUp(self):
        os.environ.setdefault("SARVAM_API_KEY", "test-not-real")
        from clinic.adapters.local_sqlite import LocalSQLiteAdapter
        from clinic.realtime_voice import VoiceSession
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript((ROOT / "clinic" / "schema.sql").read_text())
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO patients (name, phone, age) VALUES ('Manju Rao', '9000000009', 40)")
        self.conn.commit()
        self.emitted = []
        adapter = LocalSQLiteAdapter()
        self.session = VoiceSession("sid", "key", lambda e, d: self.emitted.append((e, d)), lambda: self.conn,
                                    adapter, adapter, frozenset(("book_appointment",)), review_transcripts=True)
        for target, kwargs in (("clinic.nlu.parser.pick_intent", {"return_value": None}),
                               ("clinic.nlu.parser.extract_name", {"side_effect": lambda t: "Manju Rao" if "Manju" in t else None})):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def events(self, name):
        return [d for e, d in self.emitted if e == name]

    def test_a_heard_transcript_is_held_not_processed(self):
        self.session._on_final_text("Book appointment for Manju on aath October for teen PM")
        review = self.events("transcript_review")
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["text"], "Book appointment for Manju on aath October for teen PM")
        for name in ("review_card", "read_answer", "assistant_question", "pipeline_error", "transcript_final"):
            self.assertEqual(self.events(name), [], name)

    def test_the_corrected_text_is_what_gets_parsed(self):
        self.session._on_final_text("Book appointment for Manju on aath October for teen PM")
        review_id = self.events("transcript_review")[0]["id"]
        self.session.submit_transcript(review_id, "Book appointment for Manju on 8 October at 3 PM")
        card = self.events("review_card")[0]
        self.assertEqual(card["transcript"], "Book appointment for Manju on 8 October at 3 PM")
        self.assertTrue(card["slots"]["appt_date"].endswith("-10-08"))
        self.assertEqual(card["slots"]["start_time"], "15:00")

    def test_an_unedited_send_works_too(self):
        self.session._on_final_text("book an appointment")
        review_id = self.events("transcript_review")[0]["id"]
        self.session.submit_transcript(review_id, "book an appointment")
        self.assertEqual(self.events("assistant_question")[0]["kind"], "patient")

    def test_an_id_can_only_be_used_once(self):
        self.session._on_final_text("book an appointment")
        review_id = self.events("transcript_review")[0]["id"]
        self.session.submit_transcript(review_id, "book an appointment")
        before = len(self.emitted)
        self.session.submit_transcript(review_id, "book an appointment")
        self.assertEqual(len(self.emitted), before)

    def test_an_unknown_id_is_ignored(self):
        self.session.submit_transcript("nope", "book an appointment")
        self.assertEqual(self.emitted, [])

    def test_discard_forgets_the_transcript(self):
        self.session._on_final_text("book an appointment")
        review_id = self.events("transcript_review")[0]["id"]
        self.session.discard_transcript(review_id)
        self.session.submit_transcript(review_id, "book an appointment")
        self.assertEqual(self.events("assistant_question"), [])

    def test_an_emptied_box_sends_nothing(self):
        self.session._on_final_text("book an appointment")
        review_id = self.events("transcript_review")[0]["id"]
        self.session.submit_transcript(review_id, "   ")
        self.assertEqual(self.events("assistant_question"), [])

    def test_a_huge_text_is_cut_off(self):
        self.session._on_final_text("book an appointment")
        review_id = self.events("transcript_review")[0]["id"]
        with patch.object(self.session, "_handle_final_transcript") as handle:
            self.session.submit_transcript(review_id, "x" * 5000)
        self.assertEqual(len(handle.call_args.args[0]), 500)

    def test_only_the_latest_twenty_unsent_transcripts_are_kept(self):
        for n in range(25):
            self.session._on_final_text("command %d" % n)
        self.assertEqual(len(self.session._held), 20)

    def test_nothing_is_written_by_reviewing_or_sending(self):
        self.session._on_final_text("book appointment for Manju tomorrow at 5")
        self.session.submit_transcript(self.events("transcript_review")[0]["id"], "book appointment for Manju tomorrow at 5 pm")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0], 0)

    def test_review_off_still_runs_straight_away(self):
        from clinic.adapters.local_sqlite import LocalSQLiteAdapter
        from clinic.realtime_voice import VoiceSession
        emitted = []
        adapter = LocalSQLiteAdapter()
        session = VoiceSession("sid", "key", lambda e, d: emitted.append((e, d)), lambda: self.conn, adapter, adapter,
                               frozenset(("book_appointment",)))
        session._on_final_text("book an appointment")
        names = [e for e, _ in emitted]
        self.assertIn("transcript_final", names)
        self.assertIn("assistant_question", names)
        self.assertNotIn("transcript_review", names)

    def test_the_real_app_registers_review_on_by_default(self):
        import inspect
        from clinic.realtime_voice import register_realtime_voice
        self.assertTrue(inspect.signature(register_realtime_voice).parameters["review_transcripts"].default)


class ReviewPageTests(unittest.TestCase):
    def setUp(self):
        self.js = (ROOT / "static" / "live_voice.js").read_text()

    def test_page_shows_the_box_and_sends_back_the_corrected_text(self):
        for needle in ('socket.on("transcript_review"', 'socket.emit("submit_transcript"', 'socket.emit("discard_transcript"',
                       "buildTranscriptReview", "You said (corrected)"):
            self.assertIn(needle, self.js)

    def test_results_are_matched_to_their_bubble_by_text(self):
        self.assertIn("takeBubble", self.js)
        self.assertNotIn("pendingBubble", self.js)


if __name__ == "__main__":
    unittest.main()


class NoSpeechKeyTests(unittest.TestCase):
    """The app starts without SARVAM_API_KEY (a friend can look at everything
    else); only the microphone says it needs the key."""

    def setUp(self):
        from clinic.adapters.local_sqlite import LocalSQLiteAdapter
        self.adapter = LocalSQLiteAdapter()

    def session(self, key, **kwargs):
        from clinic.realtime_voice import VoiceSession
        self.emitted = []
        return VoiceSession("sid", key, lambda e, d: self.emitted.append((e, d)), lambda: None,
                            self.adapter, self.adapter, frozenset(), **kwargs)

    def test_pressing_the_talk_key_without_a_key_explains_what_to_do(self):
        session = self.session("")
        self.assertFalse(session.listen_start(1))
        errors = [d for e, d in self.emitted if e == "voice_error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("SARVAM_API_KEY", errors[0]["message"])
        self.assertIsNone(session._active)

    def test_a_key_or_a_test_speech_factory_still_opens_a_listen(self):
        session = self.session("a-key", stt_factory=lambda *a, **k: None)
        self.assertTrue(session.listen_start(1))
        session.stop()

    def test_the_app_no_longer_requires_the_key_at_import(self):
        source = (ROOT / "app.py").read_text()
        self.assertNotIn('os.environ["SARVAM_API_KEY"]', source)
