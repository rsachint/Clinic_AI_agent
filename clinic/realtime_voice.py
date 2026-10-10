"""Realtime voice transport: a WebSocket relay between the browser and
Sarvam's raw realtime STT/TTS primitives, wired into app.py via
Flask-SocketIO.

This module changes ONLY how audio gets in and out of the assistant. It
never phrases an answer and never decides what gets written to the
database:

- Partial transcripts and VAD events are passed straight through to the
  browser for live captions / client-side barge-in -- the pipeline is never
  involved for those.
- Every `transcript.final` still goes through the existing, unmodified
  `clinic.pipeline.transcript_to_response()`, exactly as the old batch
  `/speak` route did.
- A write intent still comes back as a deferred `ParsedResult`: this module
  only forwards the parsed (and best-effort resolved) fields to the browser
  for the existing, unmodified `static/review_card.js` to render. A human
  must still tap Approve via the existing `/approve` HTTP POST route before
  anything is proposed or committed -- that endpoint is untouched and is
  not reachable from this socket.
- A read intent's answer is always the fixed-template answer text
  `clinic.nlu.answer.compose_answer()` already produced (via
  `transcript_to_response`) -- never LLM-phrased -- shown as text only; this
  module never speaks a reply back (no echo-back by design, see the
  `read_answer` event).

A second, separate mode exists for the "Need help" panel: DICTATION (see `_MODE_DICTATION`). It reuses the same
microphone stream and the same Sarvam connection code, but its final transcripts are emitted as `dictation_text`
and go NOWHERE else: never to `handle_turn`, the planner, review cards, voice memory or any intent. It has its own
socket events (`dictation_start` / `dictation_audio` / `dictation_stop` / `dictation_cancel` in, `dictation_ready`
/ `dictation_partial` / `dictation_text` / `dictation_error` / `dictation_end` out), so the assistant's
hold-to-talk handlers neither see nor change it.
"""

import base64
import contextlib
import logging
import queue
import threading
import time
import uuid

from flask import request
from sarvamai import SarvamAI
from websockets.exceptions import WebSocketException
from sarvamai.types.realtime_audio_input import RealtimeAudioInput
from sarvamai.types.realtime_end import RealtimeEnd
from sarvamai.types.realtime_error import RealtimeError as SarvamRealtimeError
from sarvamai.types.realtime_session_begin import RealtimeSessionBegin
from sarvamai.types.realtime_transcript_final import RealtimeTranscriptFinal
from sarvamai.types.realtime_transcript_partial import RealtimeTranscriptPartial
from sarvamai.types.realtime_vad_speech_end import RealtimeVadSpeechEnd
from sarvamai.types.realtime_vad_speech_start import RealtimeVadSpeechStart

from clinic import branches, network_health, planner_log
from clinic.nlu import planner
from clinic.pipeline import (ClosurePlanResult, NavigateResult, ParsedResult, PipelineError, ReadResult,
                             SwitchBranchResult, WriteResult)
from clinic.voice_context import AskResult, CardUpdate, Note, VoiceContext
from clinic.voice_turns import handle_pick, handle_turn

_logger = logging.getLogger(__name__)

_MAX_HELD_TRANSCRIPTS = 20      # unsent transcripts kept per tab
_MAX_TRANSCRIPT_CHARS = 500     # a spoken command is never longer than this

# "auto" mirrors the old batch /speak route's language_code="unknown" --
# let Saaras auto-detect rather than pinning a script, since clinic staff
# routinely code-mix Hindi/English/a regional language mid-sentence.
_STT_LANGUAGE_CODE = "auto"
_STT_MODEL = "saaras:v4"
_STT_SAMPLE_RATE = "16000"
_STT_ENCODING = "linear16"

# Hold-to-talk limits. A listen is only ever open while the user holds the
# talk key (or the on-screen orb); these bound the worst cases.
_MAX_LISTEN_S = 60.0     # hard server-side cap on one listen, however it ends up open
_FINAL_WAIT_S = 4.0      # after listen_stop, how long to wait for the final transcript
_TAIL_SILENCE_S = 0.8    # PCM silence appended on stop so the server VAD closes the utterance
# The speech service refuses one audio frame above 16000 bytes (stream_type "fast"); the 0.8 s of tail silence is
# 25600 bytes, so it goes out in frames of at most this size (0.25 s each).
_MAX_STT_FRAME_BYTES = 8000


def _silence_frames(seconds):
    """The PCM16 silence of `seconds` as base64 frames, none over _MAX_STT_FRAME_BYTES."""
    total = int(int(_STT_SAMPLE_RATE) * 2 * seconds)
    total -= total % 2                                      # whole 16-bit samples
    frames = []
    while total > 0:
        size = min(_MAX_STT_FRAME_BYTES, total)
        frames.append(base64.b64encode(b"\x00" * size).decode("ascii"))
        total -= size
    return frames
_READER_JOIN_S = 15.0    # let an in-flight pipeline call finish before reporting the listen done

_AUDIO, _STOP, _CANCEL = "audio", "stop", "cancel"

# A listen is either a spoken COMMAND (hold-to-talk: the transcript is handled by the pipeline) or DICTATION (the
# transcript is only handed back as text for a text box and is never interpreted). Dictation is click-to-start /
# click-to-stop, so it gets a longer safety cap.
_MODE_COMMAND, _MODE_DICTATION = "command", "dictation"
_MAX_DICTATION_S = 120.0
_MAX_DICTATION_CHARS = 1000     # one dictated sentence handed to the page is never longer than this
_NO_KEY_MESSAGE = "Voice is off: add SARVAM_API_KEY to the .env file and restart the app."


def _sarvam_stt_connect(api_key):
    """Default STT connection factory: a context manager that yields Sarvam's
    realtime STT socket client. Replaceable (see VoiceSession's `stt_factory`)
    so the listen lifecycle can be tested with a fake and no network."""
    client = SarvamAI(api_subscription_key=api_key)
    return client.speech_to_text_realtime_streaming.connect(
        language_code=_STT_LANGUAGE_CODE,
        model=_STT_MODEL,
        stream_type="fast",
        endpointing="vad",
        encoding=_STT_ENCODING,
        sample_rate=_STT_SAMPLE_RATE,
    )


# Opening the speech connection can fail for a moment (a Wi-Fi blip, a slow TLS handshake: the library gives
# up after ~10 s). Audio spoken meanwhile waits in the listen's queue, so one quick retry costs nothing but time.
_CONNECT_ATTEMPTS = 2
_CONNECT_PAUSE_S = 0.5
CONNECT_FAILED_MESSAGE = "Couldn't reach the speech service. Check the internet connection and try again."


def is_network_error(exc):
    """A failure to reach the speech service (timeout, refused/reset connection, TLS or websocket handshake
    problem), as opposed to a refused key or a bug."""
    return isinstance(exc, (OSError, TimeoutError, WebSocketException))


@contextlib.contextmanager
def _connect_with_retry(factory, api_key, attempts=None, pause_s=None, cancelled=lambda: False, wait=time.sleep):
    """Open the speech connection, trying again after a short pause when it cannot be reached. Only network
    errors are retried; any other error (a refused key, a bug) is raised at once. Errors raised while the
    connection is in use are never retried here."""
    attempts = attempts or _CONNECT_ATTEMPTS
    pause_s = _CONNECT_PAUSE_S if pause_s is None else pause_s
    stack = contextlib.ExitStack()
    client = None
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            client = stack.enter_context(factory(api_key))
            network_health.record("voice", True, (time.monotonic() - started) * 1000)
            break
        except Exception as exc:
            # Only a real network failure counts for the connection chip; a refused key or a bug does not.
            kind = network_health.classify(exc, "connect")
            if kind:
                network_health.record("voice", False, (time.monotonic() - started) * 1000, kind)
            if not is_network_error(exc) or attempt >= attempts or cancelled():
                raise
            _logger.warning("Speech connection attempt %d/%d failed (%s); retrying", attempt, attempts, exc)
            wait(pause_s)
            if cancelled():
                raise
    with stack:
        yield client


class _Listen:
    """One hold-to-talk utterance: its own outbound queue, STT connection and
    writer/reader threads. Nothing here outlives the listen."""

    def __init__(self, listen_id, mode=_MODE_COMMAND):
        self.id = listen_id
        self.mode = mode
        self.queue = queue.Queue()
        self.cancelled = threading.Event()   # discard everything, close now
        self.stopping = threading.Event()    # key released (or cap hit): no more audio accepted
        self.end_sent = threading.Event()    # silence + `end` have gone to the STT service
        self.settled = threading.Event()     # final transcript handled, or the STT socket closed
        self.done = threading.Event()        # writer thread finished; STT connection closed
        self.heard = False                   # at least one non-empty final transcript
        self.reason = "stopped"
        self.thread = None


class VoiceSession:
    """One browser tab's live voice session, hold-to-talk style.

    Nothing is opened at construction. The microphone only streams while the
    user holds the talk key, and the Sarvam STT websocket exists only for the
    life of one such "listen":

        listen_start -> (audio chunks) -> listen_stop   (or listen_cancel)

    `listen_start` spawns a writer thread that opens the STT connection (via
    `stt_factory`, default: the real Sarvam realtime STT) and drains that
    listen's outgoing queue; a reader thread iterates incoming events. On
    `listen_stop` the writer appends ~0.8 s of PCM silence and sends the
    SDK's `end` event (the server finalizes any pending utterance), waits up
    to `final_wait_s` for the final transcript to be handled, then closes the
    socket. Cancel, disconnect, error and the `max_listen_s` cap all close it
    too. A new listen can start while a previous one is still finishing; each
    has its own queue and threads.

    Threading model: a `websockets.sync` connection supports exactly one
    concurrent reader thread and one concurrent writer thread (the library's
    own thread-safety guarantee), so per listen a dedicated writer thread owns
    the STT `connect()` context manager and a second reader thread iterates
    incoming events for as long as the writer's `with` block keeps the socket
    open.
    """

    def __init__(self, sid, api_key, emit, get_conn, clinical_adapter, ops_adapter, deferred_intents,
                 stt_factory=None, max_listen_s=_MAX_LISTEN_S, final_wait_s=_FINAL_WAIT_S,
                 tail_silence_s=_TAIL_SILENCE_S, review_transcripts=False, max_dictation_s=_MAX_DICTATION_S):
        self.sid = sid
        self._api_key = api_key
        self._emit = emit  # emit(event, data) -- already scoped to this session's room
        self._get_conn = get_conn
        self._clinical_adapter = clinical_adapter
        self._ops_adapter = ops_adapter
        self._deferred_intents = deferred_intents
        self._stt_factory = stt_factory or _sarvam_stt_connect
        self._max_listen_s = max_listen_s
        self._max_dictation_s = max_dictation_s
        self._final_wait_s = final_wait_s
        self._tail_silence_s = tail_silence_s

        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._active = None      # the listen currently accepting audio (key held), or None
        self._listens = []       # every listen whose STT connection is still open or closing
        self._language_hint = "hi-IN"  # updated once a transcript reports a detected language
        # Conversation memory for this browser tab (see clinic/voice_context.py):
        # in memory only, forgotten after 10 idle minutes or on a page reload.
        self.context = VoiceContext()
        self._turn_lock = threading.Lock()  # one conversational turn at a time
        # With review on, a heard transcript is NOT processed: it is held and
        # shown to the person in an editable box, and only what they send back
        # (corrected or not) goes through the pipeline.
        self._review = review_transcripts
        self._held = {}  # review id -> transcript text, in the order heard
        self._card_logs = {}  # review card id -> planner_log row, to record how the card ended

    # -- listen lifecycle (called from the Socket.IO handlers) -----------

    def listen_start(self, listen_id=None):
        """Open an STT session now. One listen at a time accepts audio: a
        still-active earlier listen is cancelled first. Returns False once the
        browser has disconnected."""
        return self._start_listen(listen_id, _MODE_COMMAND)

    def dictation_start(self, listen_id=None):
        """Open an STT session whose text only goes back to the page (the Need help box). Refused while a
        spoken command is being held (the microphone is in use); a dictation already running is replaced."""
        return self._start_listen(listen_id, _MODE_DICTATION)

    def _start_listen(self, listen_id, mode):
        if not self._api_key and self._stt_factory is _sarvam_stt_connect:
            # Everything else in the app works without a speech key; only the
            # microphone needs it. Say so instead of failing mysteriously.
            self._emit_error(mode, _NO_KEY_MESSAGE)
            return False
        with self._lock:
            if self._closed.is_set():
                return False
            if self._active is not None:
                if mode == _MODE_DICTATION and self._active.mode == _MODE_COMMAND:
                    self._emit_error(mode, "The microphone is in use for a spoken command. Let go of the talk key and try again.")
                    return False
                self._cancel_locked(self._active)
            listen = _Listen(listen_id, mode)
            listen.thread = threading.Thread(target=self._run_listen, args=(listen,), daemon=True)
            self._listens.append(listen)
            self._active = listen
            listen.thread.start()
            return True

    def _emit_error(self, mode, message):
        """The error line for the page that is listening: the assistant's, or the dictation box's own."""
        self._emit("dictation_error" if mode == _MODE_DICTATION else "voice_error", {"message": message})

    def send_audio(self, audio_b64):
        """Queue one audio chunk. Dropped unless a listen is active -- the
        server never forwards audio the user did not hold the key for."""
        with self._lock:
            listen = self._active
            if listen is None or listen.mode != _MODE_COMMAND or listen.stopping.is_set() or listen.cancelled.is_set():
                return False
            listen.queue.put((_AUDIO, audio_b64))
            return True

    def dictation_audio(self, listen_id, audio_b64):
        """One chunk for the running dictation. Dropped unless THAT dictation is the active listen."""
        with self._lock:
            listen = self._active
            if (listen is None or listen.mode != _MODE_DICTATION or listen.id != listen_id
                    or listen.stopping.is_set() or listen.cancelled.is_set()):
                return False
            listen.queue.put((_AUDIO, audio_b64))
            return True

    def listen_stop(self):
        """Key released: finish the utterance (final transcript -> pipeline)."""
        with self._lock:
            listen = self._active
            if listen is None or listen.mode != _MODE_COMMAND:
                return False
            self._begin_stop_locked(listen)
            return True

    def dictation_stop(self, listen_id):
        """Click to stop dictating: finish the utterance (its text comes back as `dictation_text`)."""
        with self._lock:
            listen = self._active
            if listen is None or listen.mode != _MODE_DICTATION or listen.id != listen_id:
                return False
            self._begin_stop_locked(listen)
            return True

    def listen_cancel(self, listen_id=None):
        """Escape / discard: close STT sessions now, process nothing. With a
        `listen_id` only that listen is cancelled (a too-short tap must not
        kill an earlier listen that is still finishing); otherwise all."""
        with self._lock:
            for listen in list(self._listens):
                if listen.mode == _MODE_COMMAND and (listen_id is None or listen.id == listen_id):
                    self._cancel_locked(listen)

    def dictation_cancel(self, listen_id=None):
        """Throw the dictation away (closing the box, leaving the page): its text is dropped."""
        with self._lock:
            for listen in list(self._listens):
                if listen.mode == _MODE_DICTATION and (listen_id is None or listen.id == listen_id):
                    self._cancel_locked(listen)

    def stop(self):
        """Browser disconnected: close everything, accept nothing further."""
        with self._lock:
            self._closed.set()
            for listen in list(self._listens):
                self._cancel_locked(listen)

    def is_listening(self):
        with self._lock:
            return self._active is not None

    def _begin_stop_locked(self, listen):
        listen.stopping.set()
        if self._active is listen:
            self._active = None
        listen.queue.put((_STOP, None))

    def _cancel_locked(self, listen):
        listen.cancelled.set()
        listen.settled.set()
        if self._active is listen:
            self._active = None
        listen.queue.put((_CANCEL, None))

    # -- STT websocket lifecycle (one connection per listen) --------------

    def _run_listen(self, listen):
        reader = None
        graceful = False
        deadline = time.monotonic() + (self._max_dictation_s if listen.mode == _MODE_DICTATION else self._max_listen_s)
        try:
            with _connect_with_retry(self._stt_factory, self._api_key, cancelled=listen.cancelled.is_set,
                                     wait=listen.cancelled.wait) as socket_client:
                reader = threading.Thread(target=self._run_reader, args=(socket_client, listen), daemon=True)
                reader.start()
                while not listen.cancelled.is_set():
                    try:
                        kind, payload = listen.queue.get(timeout=max(0.0, deadline - time.monotonic()))
                    except queue.Empty:
                        # Hard cap: whatever was said so far still gets processed.
                        listen.reason = "timeout"
                        with self._lock:
                            self._begin_stop_locked(listen)
                        continue
                    if kind == _AUDIO:
                        if listen.cancelled.is_set():
                            break
                        try:
                            socket_client.send_realtime_audio_input(RealtimeAudioInput(audio=payload))
                        except Exception as exc:
                            if not listen.cancelled.is_set():
                                listen.reason = "error"
                                if network_health.classify(exc):      # lost mid-use, not a refused key or a bug
                                    network_health.record("voice", False, kind="dropped")
                                self._emit_error(listen.mode, "Lost the connection to the speech service.")
                            break
                    elif kind == _STOP:
                        graceful = True
                        break
                    else:  # _CANCEL
                        break

                if graceful and not listen.cancelled.is_set():
                    try:
                        if self._tail_silence_s > 0:
                            for frame in _silence_frames(self._tail_silence_s):
                                socket_client.send_realtime_audio_input(RealtimeAudioInput(audio=frame))
                        socket_client.send_realtime_end(RealtimeEnd())
                        listen.end_sent.set()
                        listen.settled.wait(self._final_wait_s)
                    except Exception:
                        if not listen.cancelled.is_set():
                            _logger.exception("Could not finish STT utterance for sid=%s", self.sid)
            # Leaving the `with` closed the STT socket, which ends the reader.
            if reader is not None:
                reader.join(timeout=_READER_JOIN_S)
        except Exception as exc:
            listen.reason = "error"
            if not listen.cancelled.is_set():
                _logger.exception("STT realtime connection failed for sid=%s", self.sid)
                self._emit_error(listen.mode, CONNECT_FAILED_MESSAGE if is_network_error(exc)
                                 else "Could not reach the speech service: {}".format(exc))
        finally:
            with self._lock:
                if self._active is listen:
                    self._active = None
                if listen in self._listens:
                    self._listens.remove(listen)
            listen.done.set()
            if listen.mode == _MODE_DICTATION:
                # The dictation box always hears how its listen ended (a cancelled one too: the page must give
                # the microphone back and reset its button).
                if not self._closed.is_set():
                    self._emit("dictation_end", {"id": listen.id, "heard": listen.heard,
                                                 "reason": "cancelled" if listen.cancelled.is_set() else listen.reason})
            elif not listen.cancelled.is_set() and not self._closed.is_set():
                self._emit("listen_end", {"id": listen.id, "heard": listen.heard, "reason": listen.reason})

    def _run_reader(self, socket_client, listen):
        try:
            for message in socket_client:
                if listen.cancelled.is_set():
                    return
                self._handle_stt_event(message, listen)
        except Exception:
            if not listen.cancelled.is_set() and not self._closed.is_set():
                _logger.exception("STT realtime read loop ended unexpectedly for sid=%s", self.sid)
        finally:
            listen.settled.set()

    def _handle_stt_event(self, message, listen=None):
        if listen is None:
            listen = self._active
        # Nothing is processed unless a listen is open (and not discarded).
        if listen is None or listen.cancelled.is_set() or listen.done.is_set() or self._closed.is_set():
            return
        if listen.mode == _MODE_DICTATION:
            self._handle_dictation_event(message, listen)
            return
        if isinstance(message, RealtimeSessionBegin):
            self._emit("session_ready", {"id": listen.id})
        elif isinstance(message, RealtimeVadSpeechStart):
            self._emit("vad_speech_start", {})
        elif isinstance(message, RealtimeVadSpeechEnd):
            self._emit("vad_speech_end", {})
        elif isinstance(message, RealtimeTranscriptPartial):
            self._emit("transcript_partial", {"text": message.text})
        elif isinstance(message, RealtimeTranscriptFinal):
            if message.language:
                self._language_hint = message.language
            if message.text and message.text.strip():
                listen.heard = True
                self._on_final_text(message.text)
            if listen.end_sent.is_set():
                listen.settled.set()
        elif isinstance(message, SarvamRealtimeError):
            self._emit("voice_error", {"message": message.message})

    def _handle_dictation_event(self, message, listen):
        """Speech events of a dictation listen. The ONLY thing a final transcript does here is go back to the page
        as `dictation_text`: it is never handed to handle_turn, the planner, a review card, the voice memory, the
        language hint or any intent, whatever the words say ("cancel Rahul's appointment" is just text)."""
        if isinstance(message, RealtimeSessionBegin):
            self._emit("dictation_ready", {"id": listen.id})
        elif isinstance(message, RealtimeTranscriptPartial):
            self._emit("dictation_partial", {"id": listen.id, "text": message.text})
        elif isinstance(message, RealtimeTranscriptFinal):
            text = (message.text or "").strip()
            if text:
                listen.heard = True
                self._emit("dictation_text", {"id": listen.id, "text": text[:_MAX_DICTATION_CHARS]})
            if listen.end_sent.is_set():
                listen.settled.set()
        elif isinstance(message, SarvamRealtimeError):
            self._emit("dictation_error", {"message": message.message})
        # voice-activity events are not forwarded: the assistant's screen has nothing to do with dictation

    # -- transcript review (staff can correct what was heard) -----------

    def _on_final_text(self, text):
        """A final transcript arrived. Review off: process it straight away.
        Review on: hold it and let the person correct it first."""
        if not self._review:
            self._emit("transcript_final", {"text": text})
            self._handle_final_transcript(text)
            return
        with self._lock:
            review_id = uuid.uuid4().hex[:10]
            self._held[review_id] = text
            while len(self._held) > _MAX_HELD_TRANSCRIPTS:
                self._held.pop(next(iter(self._held)))
        self._emit("transcript_review", {"id": review_id, "text": text})

    def submit_transcript(self, review_id, text):
        """The person pressed Send on a held transcript, possibly after editing
        it. The text they sent is what is parsed, shown and recorded."""
        with self._lock:
            held = self._held.pop(review_id, None)
        if held is None:
            return  # unknown, already sent or discarded: ignore
        text = (text or "").strip()[:_MAX_TRANSCRIPT_CHARS]
        if text:
            if text != held:
                _logger.info("Transcript corrected by staff: %r -> %r", held, text)
            self._handle_final_transcript(text)

    def discard_transcript(self, review_id):
        with self._lock:
            self._held.pop(review_id, None)

    # -- pipeline handoff (unchanged deterministic pipeline) -----------

    def _handle_final_transcript(self, text):
        if not text or not text.strip():
            return
        conn = self._get_conn()
        with self._turn_lock:
            try:
                # The planner can take several seconds: tell the page it is thinking.
                with planner.on_thinking(lambda: self._emit("thinking", {"transcript": text, "stage": "planner"})):
                    result = handle_turn(
                        self.context, conn, text, self._clinical_adapter, self._ops_adapter,
                        self._language_hint, defer_intents=self._deferred_intents,
                    )
            except PipelineError as e:
                self._emit("pipeline_error", {"transcript": text, "error": str(e)})
                self._emit_context()
                return
            self._deliver(text, result)
            self._emit_context()

    def _emit_context(self):
        """Tell the page what the assistant is currently 'talking about', so the
        chip above the conversation always shows what "him" or "same day" means."""
        self._emit("context_update", self.context.snapshot())

    # -- page -> server: things the person did with the mouse -------------

    def card_closed(self, card_id, outcome=None):
        """A review card was approved or rejected: it can no longer be edited by voice.
        `outcome` ("approved" / "rejected") is added to the planner log row of the
        command that produced the card, when there is one."""
        with self._turn_lock:
            self.context.close_card(card_id)
            self.context.touch()
            log_id = self._card_logs.pop(card_id, None)
        if log_id and outcome in planner_log.OUTCOMES:
            planner_log.set_outcome(self._get_conn(), log_id, outcome)
        self._emit_context()

    def set_branch(self, mine, view):
        """The page's My branch / switcher changed (or was just loaded). Only a
        branch that exists is taken: this is a hint for defaults, never trusted
        for anything else."""
        known = {b["id"] for b in branches.list_branches(self._get_conn(), include_inactive=True)}
        mine = mine if isinstance(mine, int) and mine in known else None
        view = view if view == "all" or (isinstance(view, int) and view in known) else None
        with self._turn_lock:
            self.context.set_client_branch(mine, view)

    def context_clear(self):
        """The person pressed the chip's clear button: forget everything."""
        with self._turn_lock:
            self.context.clear()
        self._emit_context()

    def pick_option(self, index):
        """The person tapped one of the options the assistant offered."""
        conn = self._get_conn()
        with self._turn_lock:
            try:
                picked = handle_pick(self.context, conn, index, self._clinical_adapter, self._ops_adapter,
                                     self._language_hint, defer_intents=self._deferred_intents)
            except PipelineError as e:
                self._emit("pipeline_error", {"transcript": "", "error": str(e)})
                self._emit_context()
                return
            if picked is None:
                return
            label, result = picked
            self._deliver(label, result)
            self._emit_context()

    def _deliver(self, text, result):
        if isinstance(result, AskResult):
            self._emit("assistant_question", {
                "transcript": text,
                "question": result.question,
                "kind": result.kind,
                "options": [{"label": o["label"]} for o in result.options],
            })
            return

        if isinstance(result, CardUpdate):
            self._emit("card_update", {
                "transcript": text,
                "card_id": result.card_id,
                "intent": result.intent,
                "changes": result.changes,
                "summary": result.summary,
            })
            return

        if isinstance(result, Note):
            self._emit("assistant_note", {"transcript": text, "message": result.message})
            return

        if isinstance(result, ParsedResult):
            # Every write intent is deferred -- never proposed or committed
            # here. This only hands the browser the parsed (and
            # best-effort resolved) fields for the existing, unmodified
            # ReviewCard.build() to render; a human must still tap Approve
            # via the existing /approve HTTP POST route before anything is
            # written, exactly as the old click-to-record flow worked.
            card_id = self.context.open_card["card_id"] if self.context.open_card else None
            if card_id and self.context.planner_log_id:
                self._card_logs[card_id] = self.context.planner_log_id
                while len(self._card_logs) > _MAX_HELD_TRANSCRIPTS:
                    self._card_logs.pop(next(iter(self._card_logs)))
            self._emit("review_card", {
                "intent": result.intent,
                "slots": result.slots,
                "resolved": result.resolved,
                "transcript": text,
                "language": self._language_hint,
                "card_id": card_id,
            })
            return

        if isinstance(result, ClosurePlanResult):
            # "Close Branch A tomorrow": the batch review card. It is a plan only;
            # the page's Apply button (a human tap) is what closes and moves anyone.
            self._emit("closure_plan", {
                "transcript": text,
                "plan": result.plan,
                "reason": result.reason,
                "answer_text": result.answer_text,
            })
            return

        if isinstance(result, SwitchBranchResult):
            # "Switch to Branch C": the browser changes this computer's My
            # branch. A setting on this screen only; no clinic data is touched.
            self._emit("switch_branch", {
                "transcript": text,
                "branch_id": result.branch_id,
                "branch_name": result.branch_name,
                "answer_text": result.answer_text,
            })
            return

        if isinstance(result, NavigateResult):
            # "Open the calendar": the browser switches tab / view. Nothing is
            # read from or written to the database, and nothing needs approval.
            self._emit("navigate", {
                "transcript": text,
                "tab": result.tab,
                "mode": result.mode,
                "answer_text": result.answer_text,
            })
            return

        if isinstance(result, ReadResult):
            json_data = None
            if result.data is not None:
                json_data = result.data if isinstance(result.data, dict) else [dict(row) for row in result.data]
            self._emit("read_answer", {
                "intent": result.intent,
                "transcript": text,
                "answer_text": result.answer_text,
                "data": json_data,
                "citation": {"source": result.citation.source, "as_of": result.citation.as_of},
                "scope_caption": result.scope_caption,
                # a question that goes with the answer (the booking offer): buttons, tapping one is saying it
                "options": [{"label": o["label"]} for o in (result.options or [])],
            })
            return

        if isinstance(result, WriteResult):
            # Should never happen: every write intent name is in
            # DEFERRED_INTENTS, so transcript_to_response() never reaches
            # its write-and-return branches when called from here. Guard
            # anyway rather than silently auto-committing or auto-speaking
            # a number nobody approved.
            _logger.error(
                "Unexpected undeferred WriteResult for intent %s (sid=%s) -- refusing to auto-commit",
                result.intent, self.sid,
            )


def register_realtime_voice(socketio, api_key, get_conn, clinical_adapter, ops_adapter, deferred_intents,
                            stt_factory=None, review_transcripts=True):
    """Wires the Socket.IO event handlers into `socketio` (an already
    created flask_socketio.SocketIO bound to the Flask app). Called once
    from app.py at startup. `stt_factory` defaults to the real Sarvam realtime
    STT connection; tests pass a fake.

    Hold-to-talk protocol: connecting creates a session but opens NO speech
    connection. The browser sends `listen_start` ({id}) when the talk key goes
    down, `audio_chunk` while it is held, then `listen_stop` (or
    `listen_cancel`, optionally {id}). The server answers `session_ready` when the STT
    connection is open and `listen_end` ({id, heard, reason}) when it has
    closed."""
    sessions = {}

    @socketio.on("connect")
    def _on_connect():
        sid = request.sid

        def emit(event, data):
            socketio.emit(event, data, room=sid)

        sessions[sid] = VoiceSession(sid, api_key, emit, get_conn, clinical_adapter, ops_adapter,
                                     deferred_intents, stt_factory=stt_factory,
                                     review_transcripts=review_transcripts)
        # Opening the page is the moment to load the model's prompt, so the first spoken
        # command does not wait for a cold start (a no-op when the planner is off).
        planner.warm_up_async(get_conn)

    @socketio.on("disconnect")
    def _on_disconnect():
        session = sessions.pop(request.sid, None)
        if session:
            session.stop()

    @socketio.on("listen_start")
    def _on_listen_start(data=None):
        session = sessions.get(request.sid)
        if session:
            session.listen_start((data or {}).get("id") if isinstance(data, dict) else None)

    @socketio.on("audio_chunk")
    def _on_audio_chunk(data=None):
        session = sessions.get(request.sid)
        if session and isinstance(data, dict) and data.get("audio"):
            session.send_audio(data["audio"])

    @socketio.on("listen_stop")
    def _on_listen_stop(data=None):
        session = sessions.get(request.sid)
        if session:
            session.listen_stop()

    @socketio.on("submit_transcript")
    def _on_submit_transcript(data=None):
        session = sessions.get(request.sid)
        if session and isinstance(data, dict) and isinstance(data.get("id"), str):
            session.submit_transcript(data["id"], data.get("text") if isinstance(data.get("text"), str) else "")

    @socketio.on("discard_transcript")
    def _on_discard_transcript(data=None):
        session = sessions.get(request.sid)
        if session and isinstance(data, dict) and isinstance(data.get("id"), str):
            session.discard_transcript(data["id"])

    @socketio.on("card_closed")
    def _on_card_closed(data=None):
        session = sessions.get(request.sid)
        if session and isinstance(data, dict):
            session.card_closed(data.get("card_id"), data.get("outcome") if isinstance(data.get("outcome"), str) else None)

    @socketio.on("set_branch")
    def _on_set_branch(data=None):
        session = sessions.get(request.sid)
        if session and isinstance(data, dict):
            session.set_branch(data.get("mine"), data.get("view"))

    @socketio.on("context_clear")
    def _on_context_clear(data=None):
        session = sessions.get(request.sid)
        if session:
            session.context_clear()

    @socketio.on("pick_option")
    def _on_pick_option(data=None):
        session = sessions.get(request.sid)
        if session and isinstance(data, dict) and isinstance(data.get("index"), int):
            session.pick_option(data["index"])

    @socketio.on("listen_cancel")
    def _on_listen_cancel(data=None):
        session = sessions.get(request.sid)
        if session:
            session.listen_cancel(data.get("id") if isinstance(data, dict) else None)

    # Dictation (the Need help box): same audio and speech connection, its own events. See VoiceSession.
    @socketio.on("dictation_start")
    def _on_dictation_start(data=None):
        session = sessions.get(request.sid)
        if session:
            session.dictation_start(data.get("id") if isinstance(data, dict) else None)

    @socketio.on("dictation_audio")
    def _on_dictation_audio(data=None):
        session = sessions.get(request.sid)
        if session and isinstance(data, dict) and data.get("audio"):
            session.dictation_audio(data.get("id"), data["audio"])

    @socketio.on("dictation_stop")
    def _on_dictation_stop(data=None):
        session = sessions.get(request.sid)
        if session and isinstance(data, dict):
            session.dictation_stop(data.get("id"))

    @socketio.on("dictation_cancel")
    def _on_dictation_cancel(data=None):
        session = sessions.get(request.sid)
        if session:
            session.dictation_cancel(data.get("id") if isinstance(data, dict) else None)

    return sessions
