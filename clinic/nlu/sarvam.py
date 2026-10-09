"""The planner on Sarvam's hosted chat model (PLANNER_BACKEND=sarvam).

The same one tool call per command as the local model (clinic/nlu/planner.py): this is only
another Backend. What leaves the machine is the planner prompt (rules, tool schemas, calendar,
branch and doctor names) and the spoken sentence -- no patient record, phone number or diagnosis.
One exception, only in the "Model does all read operations" mode (clinic/nlu/sql_reads.py): a LOOKUP step of a
multi-step read sends back, in `plan_with_history`, a compact copy of the rows the model's own query returned
(names, dates and amounts of the read-only views; phone numbers cut to their last four digits, no ids, at most 30 rows).
The final answer's rows are never sent.

When it cannot help, the planner gets an exception (SarvamError) and the staff command falls
straight to the keyword rules and the deterministic date / time readers; nothing local-model is
asked afterwards (clinic/nlu/parser.py). A time limit (SARVAM_TIMEOUT_S, default 5 s) covers the
whole call including its one retry, and a circuit breaker stops an offline Mac from making every
command wait: after 3 failures in a row Sarvam is skipped for 30 s.

The key (SARVAM_API_KEY, the same one the speech-to-text uses) is read at call time, goes only into
the request header, and is scrubbed from anything that could be logged. Request and response
bodies are never logged.
"""

import json
import logging
import os
import threading
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from decimal import ROUND_HALF_UP, Decimal

import httpx

from clinic import network_health
from clinic.nlu.planner import MAX_PROSE_CHARS, Backend, ToolCall, history_user_text

_logger = logging.getLogger(__name__)

URL = "https://api.sarvam.ai/v1/chat/completions"
MODEL = "sarvam-105b-conversations"
DEFAULT_TIMEOUT_S = 5.0
RETRY_DELAY_S = 0.4
MIN_ATTEMPT_S = 1.0             # a retry needs this much of the budget left after the pause (p95 of a call is 1-2 s)
RETRY_STATUSES = (429, 500, 502, 503, 504)
ENCODING_FALLBACK_STATUSES = (400, 422)     # the request body was refused: try the other way to send tool results
BREAKER_FAILURES = 3
BREAKER_PAUSE_S = 30.0

# Sarvam's published price list (sarvam.ai/api-pricing, read in October 2026), Rs per 1M tokens.
# Failed calls (429 / 5xx) are not billed. Cached input has never been reported by the API so far.
RATE_INPUT_RS_PER_M = Decimal("29.28")
RATE_CACHED_INPUT_RS_PER_M = Decimal("10.98")
RATE_OUTPUT_RS_PER_M = Decimal("73.20")

Usage = namedtuple("Usage", ["prompt_tokens", "completion_tokens", "cached_tokens"])


def sarvam_timeout():
    try:
        return max(0.5, float(os.environ.get("SARVAM_TIMEOUT_S", DEFAULT_TIMEOUT_S)))
    except ValueError:
        return DEFAULT_TIMEOUT_S


def api_key():
    return os.environ.get("SARVAM_API_KEY", "").strip()


def cost_paise(usage):
    """What one call cost in whole paise at the published rates (0 for no usage)."""
    if usage is None:
        return 0
    cached = min(max(usage.cached_tokens, 0), usage.prompt_tokens)
    rupees = ((usage.prompt_tokens - cached) * RATE_INPUT_RS_PER_M + cached * RATE_CACHED_INPUT_RS_PER_M
              + usage.completion_tokens * RATE_OUTPUT_RS_PER_M) / Decimal(1000000)
    return int((rupees * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


class SarvamError(Exception):
    """A failed Sarvam call. `reason` is a few safe words (never a body, header or key); the
    planner puts `planner_note` in the command's log row."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason
        self.planner_note = "sarvam: {}".format(reason)


class CircuitBreaker:
    """After `failures` failures in a row, calls are refused for `pause` seconds. Then ONE call is
    let through (half-open): its success closes the breaker, its failure opens it for another pause."""

    def __init__(self, failures=BREAKER_FAILURES, pause=BREAKER_PAUSE_S, clock=time.monotonic):
        self.failures, self.pause, self.clock = failures, pause, clock
        self._count = 0
        self._open_until = None
        self._lock = threading.Lock()

    def allow(self):
        with self._lock:
            if self._open_until is None:
                return True
            now = self.clock()
            if now < self._open_until:
                return False
            self._open_until = now + self.pause          # the probe; others wait for its verdict
            return True

    def record_success(self):
        with self._lock:
            self._count, self._open_until = 0, None

    def record_failure(self):
        with self._lock:
            self._count += 1
            if self._count >= self.failures:
                self._open_until = self.clock() + self.pause
                if self._count == self.failures:
                    _logger.warning("Sarvam planner failed %d times in a row; using the keyword rules for %.0f s",
                                    self._count, self.pause)

    @property
    def is_open(self):
        with self._lock:
            return self._open_until is not None and self.clock() < self._open_until


_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="sarvam-planner")
_warned = {"no_key": False}


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def parse_usage(raw):
    """Usage from the reply's `usage` object, or None when it carries none."""
    if not isinstance(raw, dict):
        return None
    details = raw.get("prompt_tokens_details")
    cached = (details.get("cached_tokens") if isinstance(details, dict) else None) or raw.get("cached_tokens")
    return Usage(_int(raw.get("prompt_tokens")), _int(raw.get("completion_tokens")), _int(cached))


class SarvamBackend(Backend):
    """`plan()` on the hosted model, with reasoning off, temperature 0 and the same OpenAI-style tool
    schemas Ollama gets. `last_usage` is the token count of this thread's latest call (None when it
    failed before a reply), so the planner can log it. Safe to share between threads."""

    name = "sarvam"

    def __init__(self, api_key=None, model=MODEL, timeout=None, transport=None, clock=time.monotonic,
                 sleep=time.sleep, breaker=None, url=URL, reasoning_effort=None, retry_delay=RETRY_DELAY_S):
        self._api_key = api_key
        self.model, self.url, self._timeout = model, url, timeout
        self.reasoning_effort, self.retry_delay = reasoning_effort, retry_delay
        self.clock, self.sleep = clock, sleep
        self.breaker = breaker or CircuitBreaker(clock=clock)
        self._client = httpx.Client(transport=transport)
        self._local = threading.local()

    @property
    def last_usage(self):
        return getattr(self._local, "usage", None)

    @property
    def last_text(self):
        """The words the model said instead of calling a tool on this thread's latest call, else None."""
        return getattr(self._local, "text", None)

    @property
    def last_encoding(self):
        """How this thread's latest plan_with_history() call sent the earlier tool results: "tool" (OpenAI-style
        `tool` role messages) or "user_message" (plain text after the user's message), None for a plain plan()."""
        return getattr(self._local, "encoding", None)

    def _key(self):
        return (self._api_key or api_key()).strip()

    def plan(self, system, user, tool_schemas):
        return self._plan(system, user, tool_schemas, None)

    def plan_with_history(self, system, user, tool_schemas, history):
        """The next call after earlier tool calls whose results the model should see (model-reads mode). The results
        go as OpenAI-style assistant `tool_calls` + `tool` messages; Sarvam's acceptance of that shape is unverified,
        so a 400 / 422 reply makes the same call again with the results as plain text after the user's message, and
        that choice is remembered. Same time budget, retry, breaker and key handling as plan()."""
        return self._plan(system, user, tool_schemas, list(history))

    def _plan(self, system, user, tool_schemas, history):
        self._local.usage = None
        self._local.text = None
        self._local.encoding = None
        key = self._key()
        if not key:
            if not _warned["no_key"]:
                _warned["no_key"] = True
                _logger.warning("PLANNER_BACKEND=sarvam but SARVAM_API_KEY is not set; staff commands use the "
                                "keyword rules only")
            raise SarvamError("no key")
        if not self.breaker.allow():
            raise SarvamError("circuit open")
        try:
            call = self._call(key, system, user, tool_schemas, history)
        except SarvamError as exc:
            self.breaker.record_failure()
            _logger.info("Sarvam planner call failed: %s", exc.reason)
            raise
        self.breaker.record_success()
        return call

    def _call(self, key, system, user, tool_schemas, history=None):
        encoding = None
        if history:
            encoding = "user_message" if getattr(self, "_prefers_user_message", False) else "tool"
        body = self._body(system, user, tool_schemas, history, encoding)
        deadline = self.clock() + (self._timeout or sarvam_timeout())
        retried = False
        while True:
            response = self._send(key, body, deadline - self.clock())
            status = response.status_code
            if status == 200:
                self._local.encoding = encoding
                if encoding == "user_message":
                    self._prefers_user_message = True
                return self._read(response)
            if status in ENCODING_FALLBACK_STATUSES and encoding == "tool":
                encoding = "user_message"
                body = self._body(system, user, tool_schemas, history, encoding)
                _logger.info("Sarvam refused tool-role messages (http %d); sending the tool results as plain text", status)
                continue
            if status in RETRY_STATUSES and not retried and deadline - self.clock() >= self.retry_delay + MIN_ATTEMPT_S:
                retried = True
                self.sleep(self.retry_delay)
                continue
            raise SarvamError("rate limited" if status == 429 else "http {}".format(status))

    def _body(self, system, user, tool_schemas, history, encoding):
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        if history and encoding == "tool":
            for number, (call, result) in enumerate(history, 1):
                call_id = "call_{}".format(number)
                messages.append({"role": "assistant", "content": None, "tool_calls": [{
                    "id": call_id, "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.args, ensure_ascii=False, default=str)}}]})
                messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
        elif history:
            messages[1] = {"role": "user", "content": user + "\n\n" + history_user_text(history)}
        return {"model": self.model, "messages": messages,
                "tools": tool_schemas, "tool_choice": "auto", "temperature": 0, "max_tokens": 400,
                "reasoning_effort": self.reasoning_effort}      # None = off (the API's default, "low", measured no better)

    def _send(self, key, body, remaining):
        """One POST that cannot outlive `remaining` seconds (httpx's own limits are per phase)."""
        if remaining <= 0:
            raise SarvamError("timeout")
        started = self.clock()
        future = _pool.submit(self._post, key, body, remaining)
        try:
            response = future.result(timeout=remaining)
        except (FutureTimeout, httpx.TimeoutException) as exc:
            self._note_network(started, exc)
            raise SarvamError("timeout") from None
        except httpx.HTTPError as exc:
            self._note_network(started, exc)
            raise SarvamError("network error ({})".format(type(exc).__name__)) from None
        except Exception as exc:         # nothing else may carry the request (and its header) into a log
            self._note_network(started, exc)
            raise SarvamError("call failed ({})".format(type(exc).__name__)) from None
        # Any HTTP reply (even a 401, 429 or 5xx) proves the network works; only timeouts and connection errors
        # are network failures for the connection chip (clinic/network_health.py).
        network_health.record("planner", True, self._ms_since(started))
        return response

    def _ms_since(self, started):
        try:
            return max(0.0, (self.clock() - started) * 1000)
        except Exception:
            return None

    def _note_network(self, started, exc):
        kind = network_health.classify(exc)
        if kind:
            network_health.record("planner", False, self._ms_since(started), kind)

    def _post(self, key, body, timeout):
        return self._client.post(self.url, json=body, headers={"api-subscription-key": key}, timeout=timeout)

    def _read(self, response):
        try:
            data = response.json()
        except ValueError:
            raise SarvamError("unreadable reply") from None
        if not isinstance(data, dict):
            raise SarvamError("unreadable reply")
        self._local.usage = parse_usage(data.get("usage"))
        choices = data.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        calls = message.get("tool_calls")
        if not isinstance(calls, list) or not calls or not isinstance(calls[0], dict):
            # finish_reason "stop": it answered in words, no tool call. The words are kept (in memory, for this
            # thread's planner run) so a short plain question can be shown and the rest logged (clinic/nlu/prose.py);
            # a reply cut off by the token limit is never kept.
            content = message.get("content")
            if isinstance(content, str) and content.strip() and choice.get("finish_reason") in (None, "stop"):
                self._local.text = content[:MAX_PROSE_CHARS]
            return None
        if len(calls) > 1:
            _logger.info("Sarvam planner returned %d tool calls; using only the first", len(calls))
        function = calls[0].get("function") if isinstance(calls[0].get("function"), dict) else {}
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                pass                     # left as text: validation rejects it
        return ToolCall(function.get("name"), arguments if arguments is not None else {})


_shared = None
_shared_lock = threading.Lock()


def shared_backend():
    """The one SarvamBackend the app uses, so its circuit breaker is shared by every voice session."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = SarvamBackend()
        return _shared
