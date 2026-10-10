"""Dictation (the "Need help" box): the speech stream has a second mode whose final transcript only goes back to
the page as `dictation_text`. It must NEVER reach the command path (handle_turn, the planner, review cards, voice
memory, any intent), and the normal hold-to-talk path must behave exactly as before. A FAKE speech service is
used everywhere (the real Sarvam client is tripwired by the base class): no microphone, no network."""

import socket
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_hold_to_talk import FakeSTT, HoldToTalkBase, final_msg, wait_for  # noqa: E402

from flask import Flask  # noqa: E402
from flask_socketio import SocketIO  # noqa: E402

from clinic import pipeline, realtime_voice  # noqa: E402
from clinic.adapters.local_sqlite import LocalSQLiteAdapter  # noqa: E402
from clinic.voice_context import Note  # noqa: E402
from clinic.realtime_voice import CONNECT_FAILED_MESSAGE, VoiceSession, register_realtime_voice  # noqa: E402

SENTENCE = "cancel Rahul's appointment"
# Everything the command path can put on the wire.
COMMAND_EVENTS = ("transcript_final", "transcript_review", "thinking", "review_card", "read_answer", "assistant_question",
                  "assistant_note", "card_update", "closure_plan", "switch_branch", "navigate", "pipeline_error",
                  "context_update", "session_ready", "vad_speech_start", "vad_speech_end", "transcript_partial", "listen_end",
                  "voice_error")


class DictationCase(HoldToTalkBase):
    def tripwire_the_command_path(self):
        """Any call into the command path fails the test loudly (and is recorded)."""
        self.command_calls = []

        def boom(*args, **kwargs):
            self.command_calls.append(args)
            raise AssertionError("the command path was reached by dictation")

        for target in ((realtime_voice, "handle_turn"), (pipeline, "transcript_to_response")):
            patcher = mock.patch.object(target[0], target[1], side_effect=boom)
            patcher.start()
            self.addCleanup(patcher.stop)

    def dictate(self, session, stt, listen_id=1):
        session.dictation_start(listen_id)
        self.assertTrue(wait_for(lambda: self.events("dictation_ready")))
        self.assertTrue(session.dictation_audio(listen_id, "chunk-1"))
        session.dictation_stop(listen_id)
        self.assertTrue(wait_for(lambda: self.events("dictation_end")))


class DictationNeverBecomesACommand(DictationCase):
    def test_a_dictated_sentence_comes_back_as_text_and_never_becomes_a_command(self):
        self.tripwire_the_command_path()
        stt = FakeSTT(final_on_end=SENTENCE)
        session = self.make_session(stt)
        before = session.context.snapshot()
        self.dictate(session, stt)

        self.assertEqual(self.events("dictation_text"), [{"id": 1, "text": SENTENCE}])
        self.assertEqual(self.events("dictation_end"), [{"id": 1, "heard": True, "reason": "stopped"}])
        self.assertEqual(self.command_calls, [])
        for name in COMMAND_EVENTS:
            self.assertEqual(self.events(name), [], name)
        self.assertEqual(session.context.snapshot(), before)          # voice memory untouched
        for table in ("audit_log", "unanswered_questions", "planner_log", "proposals", "appointments"):
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM {}".format(table)).fetchone()[0], 0, table)
        self.assertFalse(session._held)                                # nothing waiting for review either
        self.assertNoLeakedThreads()

    def test_it_also_holds_with_transcript_review_on(self):
        self.tripwire_the_command_path()
        stt = FakeSTT(final_on_end=SENTENCE)
        session = self.make_session(stt, review_transcripts=True)
        self.dictate(session, stt)
        self.assertEqual([d["text"] for d in self.events("dictation_text")], [SENTENCE])
        self.assertEqual(self.events("transcript_review"), [])
        self.assertFalse(session._held)

    def test_the_same_words_in_normal_mode_still_reach_the_command_path(self):
        stt = FakeSTT(final_on_end=SENTENCE)
        session = self.make_session(stt)
        with mock.patch.object(realtime_voice, "handle_turn", return_value=Note("done")) as spy:
            session.listen_start(3)
            session.send_audio("x")
            session.listen_stop()
            self.assertTrue(wait_for(lambda: self.events("listen_end")))
        self.assertEqual([call.args[2] for call in spy.call_args_list], [SENTENCE])      # (context, conn, TEXT, ...)
        self.assertEqual([d["text"] for d in self.events("transcript_final")], [SENTENCE])
        self.assertEqual(self.events("assistant_note"), [{"transcript": SENTENCE, "message": "done"}])
        self.assertEqual(self.events("dictation_text"), [])
        self.assertEqual(self.events("dictation_end"), [])
        self.assertNoLeakedThreads()

    def test_normal_mode_with_nothing_given_is_the_old_behaviour(self):
        """No mode argument anywhere: the same events as before, in the same shape."""
        stt = FakeSTT(final_on_end="kal ka calendar")
        session = self.make_session(stt)
        session.listen_start(7)
        self.assertTrue(wait_for(lambda: self.events("session_ready")))
        session.send_audio("a")
        session.listen_stop()
        self.assertTrue(wait_for(lambda: self.events("listen_end")))
        self.assertEqual(self.events("session_ready"), [{"id": 7}])
        self.assertEqual(self.events("navigate")[0]["tab"], "appointments")
        self.assertEqual(self.events("listen_end"), [{"id": 7, "heard": True, "reason": "stopped"}])
        for name in ("dictation_ready", "dictation_text", "dictation_end", "dictation_partial", "dictation_error"):
            self.assertEqual(self.events(name), [], name)

    def test_partials_and_several_sentences_come_back_as_dictation_events(self):
        self.tripwire_the_command_path()
        stt = FakeSTT()
        session = self.make_session(stt)
        session.dictation_start(4)
        self.assertTrue(wait_for(lambda: stt.sockets and self.events("dictation_ready")))
        sock = stt.sockets[0]
        from sarvamai.types.realtime_transcript_partial import RealtimeTranscriptPartial
        sock.push(RealtimeTranscriptPartial(utterance_idx=0, text="the book"))
        sock.push(final_msg("the booking screen is confusing"))
        sock.push(final_msg("   "))                                    # blank: ignored
        sock.push(final_msg("I could not find the save button"))
        self.assertTrue(wait_for(lambda: len(self.events("dictation_text")) == 2))
        self.assertEqual([d["text"] for d in self.events("dictation_text")],
                         ["the booking screen is confusing", "I could not find the save button"])
        self.assertEqual(self.events("dictation_partial"), [{"id": 4, "text": "the book"}])
        session.dictation_stop(4)
        self.assertTrue(wait_for(lambda: self.events("dictation_end")))
        self.assertEqual(self.command_calls, [])

    def test_a_dictated_text_handed_to_the_page_is_capped(self):
        stt = FakeSTT(final_on_end="x" * 5000)
        session = self.make_session(stt)
        self.dictate(session, stt)
        self.assertEqual(len(self.events("dictation_text")[0]["text"]), realtime_voice._MAX_DICTATION_CHARS)


class MicrophoneSharing(DictationCase):
    def test_dictation_audio_for_another_listen_or_without_one_is_dropped(self):
        session = self.make_session(FakeSTT())
        self.assertFalse(session.dictation_audio(1, "a"))                 # nothing running
        session.dictation_start(1)
        self.assertTrue(wait_for(lambda: self.events("dictation_ready")))
        self.assertFalse(session.dictation_audio(2, "a"))                 # wrong id
        self.assertTrue(session.dictation_audio(1, "a"))
        session.dictation_cancel(1)
        self.assertTrue(wait_for(lambda: self.events("dictation_end")))

    def test_the_hold_to_talk_calls_do_not_touch_a_dictation(self):
        stt = FakeSTT()
        session = self.make_session(stt)
        session.dictation_start(1)
        self.assertTrue(wait_for(lambda: self.events("dictation_ready")))
        self.assertFalse(session.send_audio("ptt-audio"))                 # a command chunk is not dictation audio
        self.assertFalse(session.listen_stop())                           # releasing a talk key does not stop it
        session.listen_cancel()                                           # ... and Escape does not cancel it
        time.sleep(0.1)
        self.assertEqual(self.events("dictation_end"), [])
        self.assertNotIn("ptt-audio", stt.sockets[0].audio)
        session.dictation_cancel()
        self.assertTrue(wait_for(lambda: self.events("dictation_end")))

    def test_dictation_is_refused_while_a_spoken_command_is_held(self):
        stt = FakeSTT()
        session = self.make_session(stt)
        session.listen_start(1)
        self.assertTrue(wait_for(lambda: stt.opened == 1))
        self.assertFalse(session.dictation_start(2))
        errors = self.events("dictation_error")
        self.assertEqual(len(errors), 1)
        self.assertIn("talk key", errors[0]["message"])
        self.assertEqual(self.events("voice_error"), [])                  # the assistant's line stays quiet
        self.assertEqual(stt.opened, 1)
        session.listen_cancel()
        self.assertNoLeakedThreads()

    def test_pressing_the_talk_key_takes_over_and_the_dictation_is_told_it_was_cancelled(self):
        stt = FakeSTT()
        session = self.make_session(stt)
        session.dictation_start(1)
        self.assertTrue(wait_for(lambda: stt.opened == 1))
        session.listen_start(2)
        self.assertTrue(wait_for(lambda: self.events("dictation_end")))
        self.assertEqual(self.events("dictation_end"), [{"id": 1, "heard": False, "reason": "cancelled"}])
        self.assertTrue(session.send_audio("now-a-command"))
        session.listen_cancel()
        self.assertNoLeakedThreads()

    def test_a_cancelled_dictation_discards_its_text(self):
        stt = FakeSTT(final_on_end=SENTENCE)
        session = self.make_session(stt)
        session.dictation_start(1)
        self.assertTrue(wait_for(lambda: self.events("dictation_ready")))
        session.dictation_cancel(1)
        self.assertTrue(wait_for(lambda: self.events("dictation_end")))
        self.assertEqual(self.events("dictation_text"), [])
        self.assertFalse(stt.sockets[0].ended)


class WhenSpeechIsUnavailable(DictationCase):
    def test_a_missing_key_shows_the_existing_text_on_the_dictation_line_only(self):
        session = VoiceSession("sid", "", self.emit, lambda: self.conn, LocalSQLiteAdapter(), LocalSQLiteAdapter(),
                               frozenset(), stt_factory=None)             # the real factory, no key
        self.addCleanup(session.stop)
        self.assertFalse(session.dictation_start(1))
        self.assertEqual(self.events("dictation_error"),
                         [{"message": "Voice is off: add SARVAM_API_KEY to the .env file and restart the app."}])
        self.assertFalse(session.listen_start(2))
        self.assertEqual(self.events("voice_error")[0]["message"], self.events("dictation_error")[0]["message"])

    def test_an_unreachable_speech_service_reports_plain_wording(self):
        stt = FakeSTT(raises=socket.timeout("timed out"))
        session = self.make_session(stt)
        with mock.patch.object(realtime_voice, "_CONNECT_PAUSE_S", 0.01):
            session.dictation_start(1)
            self.assertTrue(wait_for(lambda: self.events("dictation_end")))
        self.assertEqual([e["message"] for e in self.events("dictation_error")], [CONNECT_FAILED_MESSAGE])
        self.assertEqual(self.events("dictation_end")[0]["reason"], "error")
        self.assertEqual(self.events("voice_error"), [])
        self.assertNoLeakedThreads()

    def test_the_dictation_safety_cap_ends_it_and_keeps_what_was_said(self):
        stt = FakeSTT(final_on_end=SENTENCE)
        session = self.make_session(stt, max_dictation_s=0.2)
        session.dictation_start(1)
        self.assertTrue(wait_for(lambda: self.events("dictation_end")))
        self.assertEqual(self.events("dictation_end")[0]["reason"], "timeout")
        self.assertEqual([d["text"] for d in self.events("dictation_text")], [SENTENCE])


class OverSocketIO(HoldToTalkBase):
    """The real Socket.IO handlers through Flask-SocketIO's test client."""

    def setUp(self):
        super().setUp()
        self.stt = FakeSTT(final_on_end=SENTENCE)
        app = Flask(__name__)
        self.socketio = SocketIO(app, async_mode="threading")
        adapter = LocalSQLiteAdapter()
        self.sessions = register_realtime_voice(
            self.socketio, "key", lambda: self.conn, adapter, adapter, frozenset(("book_appointment",)),
            stt_factory=self.stt, review_transcripts=False)
        self.client = self.socketio.test_client(app)
        self.addCleanup(lambda: self.client.is_connected() and self.client.disconnect())

    def collect(self, until):
        got = {}
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and until not in got:
            for m in self.client.get_received():
                got.setdefault(m["name"], []).append(m["args"][0])
            time.sleep(0.01)
        return got

    def test_full_dictation_cycle_over_the_socket(self):
        with mock.patch.object(realtime_voice, "handle_turn", side_effect=AssertionError("command path")):
            self.client.emit("dictation_start", {"id": 9})
            self.assertTrue(wait_for(lambda: self.stt.sockets))
            self.client.emit("dictation_audio", {"id": 9, "audio": "AAAA"})
            self.client.emit("dictation_stop", {"id": 9})
            got = self.collect("dictation_end")
        self.assertEqual(got["dictation_ready"], [{"id": 9}])
        self.assertEqual(got["dictation_text"], [{"id": 9, "text": SENTENCE}])
        self.assertEqual(got["dictation_end"], [{"id": 9, "heard": True, "reason": "stopped"}])
        for name in COMMAND_EVENTS:
            self.assertNotIn(name, got, name)
        self.assertEqual(self.stt.sockets[0].audio[0], "AAAA")

    def test_malformed_dictation_events_are_harmless(self):
        for name in ("dictation_start", "dictation_audio", "dictation_stop", "dictation_cancel"):
            self.client.emit(name)
            self.client.emit(name, "nope")
            self.client.emit(name, {"id": "x", "audio": 5})
        time.sleep(0.2)
        self.assertEqual([m["name"] for m in self.client.get_received() if m["name"] in ("dictation_text", "navigate")], [])


class ClientSource(unittest.TestCase):
    """live_voice.js only GAINED the dictation block: the hold-to-talk handlers are the same text as at HEAD."""

    def test_the_dictation_client_exists_and_the_ptt_machine_is_untouched(self):
        js = (Path(__file__).resolve().parents[1] / "static" / "live_voice.js").read_text()
        for needle in ("window.Dictation", '"dictation_start"', '"dictation_audio"', '"dictation_stop"', '"dictation_cancel"',
                       'socket.on("dictation_text"', 'socket.on("dictation_end"', 'socket.on("dictation_error"'):
            self.assertIn(needle, js)
        # dictation never uses the assistant's events
        block = js[js.index("// ---- dictation"):]
        for forbidden in ('"listen_start"', '"audio_chunk"', '"listen_stop"', "machine.", "setState(", "flashCaption("):
            self.assertNotIn(forbidden, block)


if __name__ == "__main__":
    unittest.main()
