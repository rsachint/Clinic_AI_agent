"""Server side of hold-to-talk: the VoiceSession listen lifecycle, driven by a
fake STT connection. No microphone, Sarvam, WhatsApp or Google call is ever
made -- `stt_factory` is injected everywhere, and the module's default factory
is replaced by a tripwire for the whole file."""

import base64
import os
import queue
import sqlite3
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("SARVAM_API_KEY", "test-not-real")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Flask
from flask_socketio import SocketIO

from clinic.adapters.local_sqlite import LocalSQLiteAdapter
from clinic import realtime_voice
from clinic.realtime_voice import VoiceSession, register_realtime_voice
from sarvamai.types.realtime_session_begin import RealtimeSessionBegin
from sarvamai.types.realtime_transcript_final import RealtimeTranscriptFinal
from sarvamai.types.realtime_transcript_partial import RealtimeTranscriptPartial

SCHEMA = (Path(__file__).resolve().parents[1] / "clinic" / "schema.sql").read_text()
_CLOSE = object()


def begin_msg():
    return RealtimeSessionBegin(request_id="r1")


def final_msg(text, language="hi-IN"):
    return RealtimeTranscriptFinal(utterance_idx=0, text=text, language=language)


class FakeSocket:
    """Stands in for Sarvam's sync STT socket client."""

    def __init__(self, stt):
        self.stt = stt
        self.audio = []
        self.ended = False
        self._incoming = queue.Queue()

    def push(self, message):
        self._incoming.put(message)

    def send_realtime_audio_input(self, message):
        if self.stt.fail_sends:
            raise RuntimeError("socket gone")
        self.audio.append(message.audio)

    def send_realtime_end(self, message):
        self.ended = True
        if self.stt.final_on_end is not None:
            self.push(final_msg(self.stt.final_on_end))
        if self.stt.close_on_end:
            self.push(_CLOSE)

    def __iter__(self):
        while True:
            message = self._incoming.get()
            if message is _CLOSE:
                return
            yield message


class FakeSTT:
    """The injected `stt_factory`: records every connection it opens/closes."""

    def __init__(self, final_on_end=None, close_on_end=True, begin=True, fail_sends=False, raises=None):
        self.final_on_end = final_on_end    # transcript the "server" finalizes when it gets `end`
        self.close_on_end = close_on_end
        self.begin = begin
        self.fail_sends = fail_sends
        self.raises = raises
        self.opened = 0
        self.closed = 0
        self.sockets = []
        self._lock = threading.Lock()

    def __call__(self, api_key):
        if self.raises:
            raise self.raises
        return _Conn(self)


class _Conn:
    def __init__(self, stt):
        self.stt = stt
        self.socket = None

    def __enter__(self):
        self.socket = FakeSocket(self.stt)
        with self.stt._lock:
            self.stt.opened += 1
            self.stt.sockets.append(self.socket)
        if self.stt.begin:
            self.socket.push(begin_msg())
        return self.socket

    def __exit__(self, *exc):
        with self.stt._lock:
            self.stt.closed += 1
        self.socket.push(_CLOSE)
        return False


def wait_for(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class HoldToTalkBase(unittest.TestCase):
    def setUp(self):
        # Tripwire: the real Sarvam connection must never be reached from tests.
        tripwire = mock.patch.object(realtime_voice, "SarvamAI",
                                     side_effect=AssertionError("real Sarvam client constructed"))
        tripwire.start()
        self.addCleanup(tripwire.stop)
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.addCleanup(self.conn.close)
        self.emitted = []
        self._emit_lock = threading.Lock()
        self.threads_before = set(threading.enumerate())

    def emit(self, event, data):
        with self._emit_lock:
            self.emitted.append((event, data))

    def events(self, name):
        with self._emit_lock:
            return [d for e, d in self.emitted if e == name]

    def names(self):
        with self._emit_lock:
            return [e for e, _ in self.emitted]

    def make_session(self, stt, **kwargs):
        adapter = LocalSQLiteAdapter()
        kwargs.setdefault("final_wait_s", 1.0)
        session = VoiceSession("sid", "key", self.emit, lambda: self.conn, adapter, adapter,
                               frozenset(("book_appointment",)), stt_factory=stt, **kwargs)
        self.addCleanup(session.stop)
        return session

    def assertNoLeakedThreads(self, timeout=3.0):
        def leaked():
            return [t for t in threading.enumerate()
                    if t not in self.threads_before and t.is_alive() and t.daemon]
        self.assertTrue(wait_for(lambda: not leaked(), timeout), "leaked threads: %r" % leaked())


class ListenLifecycleTests(HoldToTalkBase):
    def test_idle_session_never_opens_stt_and_drops_audio(self):
        stt = FakeSTT()
        session = self.make_session(stt)
        self.assertFalse(session.send_audio("AAAA"))
        session.listen_stop()      # stop/cancel with nothing open are harmless no-ops
        session.listen_cancel()
        time.sleep(0.05)
        self.assertEqual(stt.opened, 0)
        self.assertEqual(self.emitted, [])
        self.assertFalse(session.is_listening())

    def test_start_chunks_stop_runs_the_real_pipeline_then_closes_stt(self):
        stt = FakeSTT(final_on_end="kal ka calendar")
        session = self.make_session(stt)
        session.listen_start(7)
        self.assertTrue(wait_for(lambda: self.events("session_ready")))
        self.assertEqual(self.events("session_ready"), [{"id": 7}])
        self.assertTrue(session.send_audio("chunk-1"))
        self.assertTrue(session.send_audio("chunk-2"))
        session.listen_stop()
        self.assertTrue(wait_for(lambda: self.events("listen_end")))

        # transcript went through the unchanged pipeline (a navigate here)
        self.assertEqual([d["text"] for d in self.events("transcript_final")], ["kal ka calendar"])
        nav = self.events("navigate")
        self.assertEqual(len(nav), 1)
        self.assertEqual(nav[0]["tab"], "appointments")
        self.assertEqual(self.events("listen_end"), [{"id": 7, "heard": True, "reason": "stopped"}])

        # audio went out in order, then silence padding, then `end`
        sock = stt.sockets[0]
        self.assertEqual(sock.audio[:2], ["chunk-1", "chunk-2"])
        padding = sock.audio[2:]                          # + the tail silence, in frames the speech service accepts
        self.assertTrue(padding)
        raw = [base64.b64decode(frame) for frame in padding]
        self.assertTrue(all(0 < len(chunk) <= 16000 for chunk in raw))     # never above the per-frame cap
        self.assertEqual(sum(len(chunk) for chunk in raw), 25600)          # still 0.8 s of 16 kHz PCM16
        self.assertTrue(all(set(chunk) == {0} for chunk in raw))
        self.assertTrue(sock.ended)
        # ... and the STT session is closed again
        self.assertEqual((stt.opened, stt.closed), (1, 1))
        self.assertFalse(session.is_listening())
        self.assertNoLeakedThreads()

    def test_audio_after_stop_is_dropped(self):
        stt = FakeSTT(final_on_end="kal ka calendar")
        session = self.make_session(stt)
        session.listen_start(1)
        session.send_audio("a")
        session.listen_stop()
        self.assertFalse(session.send_audio("late"))
        self.assertTrue(wait_for(lambda: self.events("listen_end")))
        self.assertNotIn("late", stt.sockets[0].audio)

    def test_second_listen_after_the_first_works(self):
        stt = FakeSTT(final_on_end="kal ka calendar")
        session = self.make_session(stt)
        for n in (1, 2):
            session.listen_start(n)
            session.send_audio("x")
            session.listen_stop()
            self.assertTrue(wait_for(lambda n=n: len(self.events("listen_end")) == n))
        self.assertEqual((stt.opened, stt.closed), (2, 2))
        self.assertEqual(len(self.events("navigate")), 2)
        self.assertEqual([d["id"] for d in self.events("listen_end")], [1, 2])
        self.assertNoLeakedThreads()

    def test_new_listen_right_after_release_while_the_old_one_is_still_finishing(self):
        stt = FakeSTT(final_on_end="kal ka calendar", close_on_end=False)  # old one lingers until its final
        session = self.make_session(stt)
        session.listen_start(1)
        session.send_audio("a")
        session.listen_stop()
        session.listen_start(2)                       # immediately, before listen 1 has settled
        self.assertTrue(session.send_audio("b"))      # goes to the NEW listen only
        session.listen_stop()
        self.assertTrue(wait_for(lambda: len(self.events("listen_end")) == 2))
        self.assertEqual(sorted(d["id"] for d in self.events("listen_end")), [1, 2])
        self.assertEqual(stt.opened, 2)
        self.assertEqual(stt.closed, 2)
        self.assertNotIn("b", stt.sockets[0].audio)
        self.assertNoLeakedThreads()

    def test_starting_a_listen_while_one_is_active_cancels_the_old_one(self):
        stt = FakeSTT()
        session = self.make_session(stt)
        session.listen_start(1)
        self.assertTrue(wait_for(lambda: stt.opened == 1))
        session.listen_start(2)
        self.assertTrue(wait_for(lambda: stt.opened == 2))
        self.assertTrue(wait_for(lambda: stt.closed == 1))   # the first was closed, discarded
        session.listen_cancel()
        self.assertTrue(wait_for(lambda: stt.closed == 2))
        self.assertEqual(self.events("listen_end"), [])      # cancelled listens report nothing
        self.assertNoLeakedThreads()

    def test_listen_cancel_discards_everything(self):
        stt = FakeSTT(final_on_end="kal ka calendar")
        session = self.make_session(stt)
        session.listen_start(1)
        self.assertTrue(wait_for(lambda: self.events("session_ready")))
        session.send_audio("a")
        session.listen_cancel()
        self.assertTrue(wait_for(lambda: stt.closed == 1))
        time.sleep(0.05)
        self.assertFalse(stt.sockets[0].ended)               # never asked the server to finalize
        self.assertEqual(self.events("transcript_final"), [])
        self.assertEqual(self.events("navigate"), [])
        self.assertEqual(self.events("listen_end"), [])
        self.assertFalse(session.send_audio("after-cancel"))
        self.assertNoLeakedThreads()

    def test_cancel_by_id_leaves_other_listens_alone(self):
        stt = FakeSTT(final_on_end="kal ka calendar", close_on_end=False)
        session = self.make_session(stt, final_wait_s=30)
        session.listen_start(1)
        session.send_audio("a")
        session.listen_stop()                               # listen 1 is finishing ("Thinking...")
        self.assertTrue(wait_for(lambda: stt.sockets and stt.sockets[0].ended))
        session.listen_start(2)                             # an accidental tap...
        self.assertTrue(wait_for(lambda: stt.opened == 2))
        session.listen_cancel(2)                            # ...cancelled by id
        self.assertTrue(wait_for(lambda: stt.closed == 1))
        # listen 1 still delivers its result (the fake answers `end` with a final)
        self.assertTrue(wait_for(lambda: self.events("listen_end")))
        self.assertEqual(self.events("listen_end"), [{"id": 1, "heard": True, "reason": "stopped"}])
        self.assertEqual(len(self.events("navigate")), 1)

    def test_cancel_during_the_stop_wait_drops_the_late_result(self):
        stt = FakeSTT(close_on_end=False)   # never answers `end`
        session = self.make_session(stt, final_wait_s=30)
        session.listen_start(1)
        session.send_audio("a")
        session.listen_stop()
        self.assertTrue(wait_for(lambda: stt.sockets and stt.sockets[0].ended))
        session.listen_cancel()             # Escape while "Thinking..."
        self.assertTrue(wait_for(lambda: stt.closed == 1))
        self.assertEqual(self.events("listen_end"), [])
        self.assertNoLeakedThreads()

    def test_stop_with_no_speech_closes_quietly_and_says_nothing_was_heard(self):
        stt = FakeSTT(final_on_end=None)    # server finalizes nothing, just closes
        session = self.make_session(stt)
        session.listen_start(3)
        session.send_audio("hiss")
        session.listen_stop()
        self.assertTrue(wait_for(lambda: self.events("listen_end")))
        self.assertEqual(self.events("listen_end"), [{"id": 3, "heard": False, "reason": "stopped"}])
        self.assertEqual(self.events("transcript_final"), [])
        self.assertEqual(self.events("navigate") + self.events("review_card") + self.events("read_answer"), [])
        self.assertEqual(self.events("voice_error"), [])
        self.assertEqual((stt.opened, stt.closed), (1, 1))
        self.assertNoLeakedThreads()

    def test_empty_final_transcript_counts_as_nothing_heard(self):
        stt = FakeSTT(final_on_end="   ")
        session = self.make_session(stt)
        session.listen_start(1)
        session.listen_stop()
        self.assertTrue(wait_for(lambda: self.events("listen_end")))
        self.assertFalse(self.events("listen_end")[0]["heard"])
        self.assertEqual(self.events("transcript_final"), [])

    def test_no_answer_to_end_still_closes_after_the_wait(self):
        stt = FakeSTT(close_on_end=False)   # server never replies and never closes
        session = self.make_session(stt, final_wait_s=0.2)
        session.listen_start(1)
        session.listen_stop()
        self.assertTrue(wait_for(lambda: self.events("listen_end")))
        self.assertEqual(stt.closed, 1)
        self.assertFalse(self.events("listen_end")[0]["heard"])
        self.assertNoLeakedThreads()

    def test_hard_cap_closes_a_listen_and_still_processes_what_was_said(self):
        stt = FakeSTT(final_on_end="kal ka calendar")
        session = self.make_session(stt, max_listen_s=0.15)
        session.listen_start(1)
        session.send_audio("a")
        # the key is "stuck" -- no listen_stop ever arrives
        self.assertTrue(wait_for(lambda: self.events("listen_end")))
        self.assertEqual(self.events("listen_end"), [{"id": 1, "heard": True, "reason": "timeout"}])
        self.assertEqual(len(self.events("navigate")), 1)
        self.assertEqual((stt.opened, stt.closed), (1, 1))
        self.assertFalse(session.send_audio("late"))
        self.assertNoLeakedThreads()

    def test_default_cap_is_sixty_seconds(self):
        self.assertEqual(realtime_voice._MAX_LISTEN_S, 60.0)
        self.assertEqual(self.make_session(FakeSTT())._max_listen_s, 60.0)

    def test_disconnect_mid_listen_closes_cleanly(self):
        stt = FakeSTT(final_on_end="kal ka calendar")
        session = self.make_session(stt)
        session.listen_start(1)
        self.assertTrue(wait_for(lambda: self.events("session_ready")))
        session.send_audio("a")
        session.stop()   # what the Socket.IO `disconnect` handler does
        self.assertTrue(wait_for(lambda: stt.closed == 1))
        time.sleep(0.05)
        self.assertEqual(self.events("navigate"), [])
        self.assertEqual(self.events("listen_end"), [])
        self.assertFalse(session.listen_start(2))   # a dead session never reopens STT
        self.assertEqual(stt.opened, 1)
        self.assertNoLeakedThreads()

    def test_transcripts_that_arrive_while_idle_are_ignored(self):
        session = self.make_session(FakeSTT())
        session._handle_stt_event(final_msg("kal ka calendar"))
        session._handle_stt_event(RealtimeTranscriptPartial(utterance_idx=0, text="kal"))
        session._handle_stt_event(begin_msg())
        self.assertEqual(self.emitted, [])

    def test_transcripts_for_a_cancelled_or_finished_listen_are_ignored(self):
        stt = FakeSTT()
        session = self.make_session(stt)
        session.listen_start(1)
        listen = session._active
        session.listen_cancel()
        self.assertTrue(wait_for(lambda: stt.closed == 1))
        before = list(self.emitted)     # the connection may have announced itself before the cancel
        session._handle_stt_event(final_msg("kal ka calendar"), listen)
        self.assertEqual(self.emitted, before)
        self.assertEqual(self.events("navigate"), [])

    def test_partials_pass_through_while_listening(self):
        stt = FakeSTT()
        session = self.make_session(stt)
        session.listen_start(1)
        self.assertTrue(wait_for(lambda: stt.sockets))
        stt.sockets[0].push(RealtimeTranscriptPartial(utterance_idx=0, text="kal ka"))
        self.assertTrue(wait_for(lambda: self.events("transcript_partial")))
        self.assertEqual(self.events("transcript_partial"), [{"text": "kal ka"}])
        session.listen_cancel()

    def test_a_final_mid_hold_is_processed_when_it_arrives(self):
        # VAD can close an utterance while the key is still down (a pause).
        stt = FakeSTT()
        session = self.make_session(stt)
        session.listen_start(1)
        self.assertTrue(wait_for(lambda: stt.sockets))
        stt.sockets[0].push(final_msg("kal ka calendar"))
        self.assertTrue(wait_for(lambda: self.events("navigate")))
        session.listen_stop()
        self.assertTrue(wait_for(lambda: self.events("listen_end")))
        self.assertTrue(self.events("listen_end")[0]["heard"])


class ErrorHandlingTests(HoldToTalkBase):
    def test_raising_factory_emits_voice_error_and_session_stays_usable(self):
        stt = FakeSTT(raises=RuntimeError("no network"))
        session = self.make_session(stt)
        session.listen_start(1)
        self.assertTrue(wait_for(lambda: self.events("listen_end")))
        errors = self.events("voice_error")
        self.assertEqual(len(errors), 1)
        self.assertIn("no network", errors[0]["message"])
        self.assertEqual(self.events("listen_end"), [{"id": 1, "heard": False, "reason": "error"}])
        self.assertFalse(session.is_listening())
        self.assertFalse(session.send_audio("x"))

        # the session is still usable: a later listen works once the factory recovers
        stt.raises = None
        stt.final_on_end = "kal ka calendar"
        session.listen_start(2)
        session.send_audio("x")
        session.listen_stop()
        self.assertTrue(wait_for(lambda: len(self.events("listen_end")) == 2))
        self.assertEqual(len(self.events("navigate")), 1)
        self.assertNoLeakedThreads()

    def test_audio_sent_before_a_failed_connect_does_not_leak_threads(self):
        stt = FakeSTT(raises=RuntimeError("down"))
        session = self.make_session(stt)
        session.listen_start(1)
        session.send_audio("a")
        self.assertTrue(wait_for(lambda: self.events("listen_end")))
        self.assertNoLeakedThreads()

    def test_a_dropped_socket_mid_listen_reports_an_error_and_closes(self):
        stt = FakeSTT(fail_sends=True)
        session = self.make_session(stt)
        session.listen_start(1)
        session.send_audio("a")
        self.assertTrue(wait_for(lambda: self.events("listen_end")))
        self.assertEqual(len(self.events("voice_error")), 1)
        self.assertEqual(self.events("listen_end")[0]["reason"], "error")
        self.assertEqual(stt.closed, 1)
        self.assertNoLeakedThreads()


class SocketIOReviewTests(HoldToTalkBase):
    """The same real Socket.IO handlers with transcript review on (the app's
    default): what is heard is held until the page sends it back."""

    def setUp(self):
        super().setUp()
        self.stt = FakeSTT(final_on_end="kal ka kalendar")  # a mis-heard word to correct
        app = Flask(__name__)
        self.socketio = SocketIO(app, async_mode="threading")
        adapter = LocalSQLiteAdapter()
        self.sessions = register_realtime_voice(
            self.socketio, "key", lambda: self.conn, adapter, adapter, frozenset(("book_appointment",)),
            stt_factory=self.stt)
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

    def hear(self):
        self.client.emit("listen_start", {"id": 1})
        self.assertTrue(wait_for(lambda: self.stt.sockets))
        self.client.emit("audio_chunk", {"audio": "AAAA"})
        self.client.emit("listen_stop")
        got = self.collect("transcript_review")
        self.assertEqual([d["text"] for d in got["transcript_review"]], ["kal ka kalendar"])
        self.assertNotIn("transcript_final", got)
        self.assertNotIn("navigate", got)  # nothing ran yet
        return got["transcript_review"][0]["id"]

    def test_heard_text_waits_for_the_page_and_the_corrected_text_runs(self):
        review_id = self.hear()
        self.client.emit("submit_transcript", {"id": review_id, "text": "kal ka calendar"})
        got = self.collect("navigate")
        self.assertEqual(got["navigate"][0]["transcript"], "kal ka calendar")
        self.assertEqual(got["navigate"][0]["tab"], "appointments")

    def test_discard_runs_nothing_and_malformed_submits_are_harmless(self):
        review_id = self.hear()
        self.client.emit("submit_transcript")
        self.client.emit("submit_transcript", "nope")
        self.client.emit("submit_transcript", {"id": 7, "text": "kal ka calendar"})
        self.client.emit("discard_transcript", {"id": review_id})
        self.client.emit("submit_transcript", {"id": review_id, "text": "kal ka calendar"})
        time.sleep(0.2)
        self.assertEqual([m["name"] for m in self.client.get_received() if m["name"] in ("navigate", "review_card")], [])


class SocketIOWiringTests(HoldToTalkBase):
    """The real Socket.IO handlers, through Flask-SocketIO's test client."""

    def setUp(self):
        super().setUp()
        self.stt = FakeSTT(final_on_end="kal ka calendar")
        app = Flask(__name__)
        self.socketio = SocketIO(app, async_mode="threading")
        adapter = LocalSQLiteAdapter()
        self.sessions = register_realtime_voice(
            self.socketio, "key", lambda: self.conn, adapter, adapter, frozenset(("book_appointment",)),
            stt_factory=self.stt, review_transcripts=False)
        self.client = self.socketio.test_client(app)
        self.addCleanup(lambda: self.client.is_connected() and self.client.disconnect())

    def received(self, name):
        return [m["args"][0] for m in self.client.get_received() if m["name"] == name]

    def test_connect_creates_a_session_but_opens_no_stt(self):
        self.assertEqual(len(self.sessions), 1)
        time.sleep(0.05)
        self.assertEqual(self.stt.opened, 0)

    def test_audio_chunk_before_listen_start_is_dropped(self):
        self.client.emit("audio_chunk", {"audio": "AAAA"})
        time.sleep(0.05)
        self.assertEqual(self.stt.opened, 0)

    def test_full_cycle_over_socketio(self):
        self.client.emit("listen_start", {"id": 5})
        self.assertTrue(wait_for(lambda: self.stt.sockets))
        self.client.emit("audio_chunk", {"audio": "AAAA"})
        self.client.emit("listen_stop")
        self.assertTrue(wait_for(lambda: self.stt.closed == 1))
        got = {}
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and "listen_end" not in got:
            for m in self.client.get_received():
                got.setdefault(m["name"], []).append(m["args"][0])
            time.sleep(0.01)
        self.assertEqual(got["session_ready"], [{"id": 5}])
        self.assertEqual(got["navigate"][0]["tab"], "appointments")
        self.assertEqual(got["listen_end"], [{"id": 5, "heard": True, "reason": "stopped"}])
        self.assertEqual(self.stt.sockets[0].audio[0], "AAAA")

    def test_listen_cancel_event_closes_stt(self):
        self.client.emit("listen_start", {"id": 1})
        self.assertTrue(wait_for(lambda: self.stt.sockets))
        self.client.emit("listen_cancel")
        self.assertTrue(wait_for(lambda: self.stt.closed == 1))
        self.assertFalse(self.stt.sockets[0].ended)

    def test_client_disconnect_mid_listen_closes_stt_and_forgets_the_session(self):
        self.client.emit("listen_start", {"id": 1})
        self.assertTrue(wait_for(lambda: self.stt.sockets))
        self.client.disconnect()
        self.assertTrue(wait_for(lambda: self.stt.closed == 1))
        self.assertTrue(wait_for(lambda: not self.sessions))
        self.assertNoLeakedThreads()

    def test_malformed_payloads_are_harmless(self):
        self.client.emit("listen_start")                 # no payload at all
        self.assertTrue(wait_for(lambda: self.stt.sockets))
        self.client.emit("audio_chunk")                  # no payload
        self.client.emit("audio_chunk", "not-a-dict")
        self.client.emit("audio_chunk", {"audio": ""})
        self.client.emit("listen_cancel")
        self.assertTrue(wait_for(lambda: self.stt.closed == 1))
        self.assertEqual(self.stt.sockets[0].audio, [])


if __name__ == "__main__":
    unittest.main()
