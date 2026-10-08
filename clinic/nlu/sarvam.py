"""The planner on Sarvam's hosted chat model (PLANNER_BACKEND=sarvam).

The same one tool call per command as the local model (clinic/nlu/planner.py): this is only
another Backend. What leaves the machine is the planner prompt (rules, tool schemas, calendar,
branch and doctor names) and the spoken sentence -- no patient record, phone number or diagnosis.

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

from clinic.nlu.planner import Backend, ToolCall

_logger = logging.getLogger(__name__)

URL = "https://api.sarvam.ai/v1/chat/completions"
MODEL = "sarvam-105b-conversations"
DEFAULT_TIMEOUT_S = 5.0
RETRY_DELAY_S = 0.4
MIN_ATTEMPT_S = 1.0             # a retry needs this much of the budget left after the pause (p95 of a call is 1-2 s)
RETRY_STATUSES = (429, 500, 502, 503, 504)
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

    def _key(self):
        return (self._api_key or api_key()).strip()

    def plan(self, system, user, tool_schemas):
        self._local.usage = None
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
            call = self._call(key, system, user, tool_schemas)
        except SarvamError as exc:
            self.breaker.record_failure()
            _logger.info("Sarvam planner call failed: %s", exc.reason)
            raise
        self.breaker.record_success()
        return call

    def _call(self, key, system, user, tool_schemas):
        body = {"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "tools": tool_schemas, "tool_choice": "auto", "temperature": 0, "max_tokens": 400,
                "reasoning_effort": self.reasoning_effort}      # None = off (the API's default, "low", measured no better)
        deadline = self.clock() + (self._timeout or sarvam_timeout())
        retried = False
        while True:
            response = self._send(key, body, deadline - self.clock())
            status = response.status_code
            if status == 200:
                return self._read(response)
            if status in RETRY_STATUSES and not retried and deadline - self.clock() >= self.retry_delay + MIN_ATTEMPT_S:
                retried = True
                self.sleep(self.retry_delay)
                continue
            raise SarvamError("rate limited" if status == 429 else "http {}".format(status))

    def _send(self, key, body, remaining):
        """One POST that cannot outlive `remaining` seconds (httpx's own limits are per phase)."""
        if remaining <= 0:
            raise SarvamError("timeout")
        future = _pool.submit(self._post, key, body, remaining)
        try:
            return future.result(timeout=remaining)
        except (FutureTimeout, httpx.TimeoutException):
            raise SarvamError("timeout") from None
        except httpx.HTTPError as exc:
            raise SarvamError("network error ({})".format(type(exc).__name__)) from None
        except Exception as exc:         # nothing else may carry the request (and its header) into a log
            raise SarvamError("call failed ({})".format(type(exc).__name__)) from None

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
            return None                  # finish_reason "stop": it answered in words, no tool call
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
