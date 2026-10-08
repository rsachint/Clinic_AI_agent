"""The planner on Sarvam's hosted model (clinic/nlu/sarvam.py, PLANNER_BACKEND=sarvam): the request, the
reply, usage and cost, the 5-second budget and its one retry, the circuit breaker, the key never leaking,
the staff path never touching the local model, the planner log's new columns, the Settings usage route and
the Settings line. Every call goes through a FAKE HTTP transport: nothing here reaches Sarvam or Ollama."""
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.planner_support import PlannerCase  # noqa: E402
from tests.test_app_routes import RouteTestCase, clinic_app  # noqa: E402  (stubs dotenv, never reads .env)
from tests.test_voice_dialog import DEFER  # noqa: E402

from clinic import db, planner_log, settings  # noqa: E402
from clinic.nlu import intent_llm, llm_slots, planner, sarvam, tools  # noqa: E402
from clinic.pipeline import ParsedResult, PipelineError  # noqa: E402
from clinic.voice_context import AskResult, VoiceContext  # noqa: E402
from clinic.voice_turns import handle_turn  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
KEY = "sk_test_SECRET_key_0123456789"
TODAY = datetime(2026, 10, 6).date()
SCHEMAS = tools.schemas()


def reply(name=None, arguments=None, prompt=3700, completion=40, finish=None, **extra):
    """A 200 reply from the chat API: one tool call (or none), with the usage object."""
    message = {"role": "assistant", "content": None}
    if name is not None:
        message["tool_calls"] = [{"id": "call_1", "type": "function", "function": {"name": name, "arguments": arguments}}]
    body = {"choices": [{"index": 0, "message": message, "finish_reason": finish or ("tool_calls" if name else "stop")}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}}
    body.update(extra)
    return httpx.Response(200, json=body)


def book_reply(**kw):
    return reply("book_appointment", json.dumps({"patient_name": "Amit Dua", "date": "2026-10-07", "time": "16:00"}), **kw)


class Server:
    """The fake transport. Each answer is an httpx.Response, an exception to raise, or a callable(request)
    returning one; the last answer repeats. Every request is kept in `requests`."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []
        self._lock = threading.Lock()

    def __call__(self, request):
        with self._lock:
            self.requests.append(request)
            answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @property
    def calls(self):
        return len(self.requests)


class Clock:
    """A clock the tests move by hand; sleeping just moves it."""

    def __init__(self, now=1000.0):
        self.now = now
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def backend_for(server, clock=None, **kw):
    clock = clock or Clock()
    kw.setdefault("api_key", KEY)
    return sarvam.SarvamBackend(transport=httpx.MockTransport(server), clock=clock, sleep=clock.sleep, **kw), clock


def plan(backend, text="Book Amit Dua tomorrow at 4 PM"):
    return backend.plan("SYSTEM", text, SCHEMAS)


class Request(unittest.TestCase):
    def test_the_request_has_the_agreed_shape(self):
        server = Server(book_reply())
        backend, _ = backend_for(server)
        plan(backend)
        request = server.requests[0]
        self.assertEqual((request.method, str(request.url)), ("POST", "https://api.sarvam.ai/v1/chat/completions"))
        self.assertEqual(request.headers["api-subscription-key"], KEY)
        body = json.loads(request.content)
        self.assertEqual(body["model"], "sarvam-105b-conversations")
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual((body["messages"][0]["content"], body["messages"][1]["content"]), ("SYSTEM", "Book Amit Dua tomorrow at 4 PM"))
        self.assertEqual(body["tools"], SCHEMAS)
        self.assertEqual(len(body["tools"]), 19)
        self.assertEqual(body["tool_choice"], "auto")
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["max_tokens"], 400)
        self.assertIn("reasoning_effort", body)
        self.assertIsNone(body["reasoning_effort"])                         # reasoning off, sent as a real null
        self.assertEqual(set(body), {"model", "messages", "tools", "tool_choice", "temperature", "max_tokens", "reasoning_effort"})
        self.assertNotIn(KEY, request.content.decode())                      # the key goes in the header only

    def test_the_key_is_read_when_the_call_is_made(self):
        server = Server(book_reply())
        backend = sarvam.SarvamBackend(transport=httpx.MockTransport(server))
        with patch.dict(os.environ, {"SARVAM_API_KEY": "  key-from-env  "}):
            plan(backend)
        self.assertEqual(server.requests[0].headers["api-subscription-key"], "key-from-env")

    def test_the_benchmark_script_can_still_ask_for_reasoning(self):
        server = Server(book_reply())
        backend, _ = backend_for(server, reasoning_effort="low")
        plan(backend)
        self.assertEqual(json.loads(server.requests[0].content)["reasoning_effort"], "low")

    def test_the_time_limit_is_five_seconds_unless_the_environment_says_otherwise(self):
        server = Server(book_reply())
        backend, _ = backend_for(server)
        plan(backend)
        self.assertLessEqual(server.requests[0].extensions["timeout"]["read"], 5.0)
        self.assertGreater(server.requests[0].extensions["timeout"]["read"], 4.9)
        with patch.dict(os.environ, {"SARVAM_TIMEOUT_S": ""}):
            self.assertEqual(sarvam.sarvam_timeout(), 5.0)
        with patch.dict(os.environ, {"SARVAM_TIMEOUT_S": "3"}):
            self.assertEqual(sarvam.sarvam_timeout(), 3.0)
            server2 = Server(book_reply())
            backend2, _ = backend_for(server2)
            plan(backend2)
            self.assertLessEqual(server2.requests[0].extensions["timeout"]["read"], 3.0)
        with patch.dict(os.environ, {"SARVAM_TIMEOUT_S": "soon"}):
            self.assertEqual(sarvam.sarvam_timeout(), 5.0)


class Parsing(unittest.TestCase):
    def one(self, response):
        backend, _ = backend_for(Server(response))
        return plan(backend)

    def test_a_good_reply_is_a_tool_call_with_decoded_arguments(self):
        call = self.one(book_reply())
        self.assertEqual(call, planner.ToolCall("book_appointment", {"patient_name": "Amit Dua", "date": "2026-10-07", "time": "16:00"}))

    def test_arguments_may_come_as_an_object_too(self):
        self.assertEqual(self.one(reply("open_calendar", {"mode": "week"})), planner.ToolCall("open_calendar", {"mode": "week"}))

    def test_missing_arguments_are_an_empty_object(self):
        self.assertEqual(self.one(reply("queue_action", None)), planner.ToolCall("queue_action", {}))

    def test_invalid_json_arguments_are_left_as_text_for_validation_to_reject(self):
        call = self.one(reply("book_appointment", "{not json"))
        self.assertEqual(call, planner.ToolCall("book_appointment", "{not json"))
        with self.assertRaises(tools.ToolError):
            tools.validate(call.name, call.args)

    def test_stop_without_a_tool_call_is_none(self):
        self.assertIsNone(self.one(reply(finish="stop")))

    def test_an_empty_or_odd_reply_is_none_not_a_crash(self):
        for body in ({"choices": []}, {"choices": [{}]}, {"choices": [{"message": None}]}, {"choices": "x"}, {},
                     {"choices": [{"message": {"tool_calls": []}}]}, {"choices": [{"message": {"tool_calls": ["x"]}}]}):
            with self.subTest(body=body):
                self.assertIsNone(self.one(httpx.Response(200, json=body)))

    def test_only_the_first_of_several_tool_calls_is_used(self):
        body = json.loads(reply("cancel_appointment", '{"patient_name": "A"}').content)
        body["choices"][0]["message"]["tool_calls"].append({"function": {"name": "open_calendar", "arguments": "{}"}})
        self.assertEqual(self.one(httpx.Response(200, json=body)), planner.ToolCall("cancel_appointment", {"patient_name": "A"}))

    def test_a_reply_that_is_not_json_is_a_failure(self):
        for response in (httpx.Response(200, text="<html>oops</html>"), httpx.Response(200, json=["x"])):
            with self.subTest(response=response.content):
                backend, _ = backend_for(Server(response))
                with self.assertRaises(sarvam.SarvamError) as caught:
                    plan(backend)
                self.assertEqual(caught.exception.reason, "unreadable reply")


class UsageAndCost(unittest.TestCase):
    def test_the_token_counts_of_the_call_are_kept(self):
        backend, _ = backend_for(Server(book_reply(prompt=3712, completion=41)))
        self.assertIsNone(backend.last_usage)
        plan(backend)
        self.assertEqual(backend.last_usage, sarvam.Usage(3712, 41, 0))

    def test_a_reply_with_no_tool_call_still_reports_its_tokens(self):
        backend, _ = backend_for(Server(reply(prompt=3600, completion=12)))
        plan(backend)
        self.assertEqual(backend.last_usage, sarvam.Usage(3600, 12, 0))

    def test_cached_tokens_are_read_wherever_the_api_puts_them(self):
        backend, _ = backend_for(Server(reply("queue_action", "{}", usage=None)))
        plan(backend)
        self.assertIsNone(backend.last_usage)               # no usage object: unknown, not zero
        for usage in ({"prompt_tokens": 100, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 60}},
                      {"prompt_tokens": 100, "completion_tokens": 5, "cached_tokens": 60}):
            backend, _ = backend_for(Server(reply("queue_action", "{}", usage=usage)))
            plan(backend)
            self.assertEqual(backend.last_usage, sarvam.Usage(100, 5, 60))

    def test_garbage_in_the_usage_object_counts_as_zero(self):
        usage = sarvam.parse_usage({"prompt_tokens": "many", "completion_tokens": -3, "cached_tokens": True})
        self.assertEqual(usage, sarvam.Usage(0, 0, 0))
        self.assertIsNone(sarvam.parse_usage(None))

    def test_a_failed_call_has_no_usage_and_a_new_call_forgets_the_last(self):
        server = Server(book_reply(), httpx.Response(500), httpx.Response(500))
        backend, _ = backend_for(server)
        plan(backend)
        self.assertIsNotNone(backend.last_usage)
        with self.assertRaises(sarvam.SarvamError):
            plan(backend)
        self.assertIsNone(backend.last_usage)

    def test_usage_belongs_to_the_thread_that_made_the_call(self):
        backend, _ = backend_for(Server(lambda request: book_reply(prompt=len(json.loads(request.content)["messages"][1]["content"]))))
        seen = {}

        def run(name, text):
            plan(backend, text)
            time.sleep(0.05)
            seen[name] = backend.last_usage

        threads = [threading.Thread(target=run, args=("a", "x")), threading.Thread(target=run, args=("b", "y" * 500))]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(set(seen), {"a", "b"})
        self.assertNotEqual(seen["a"], seen["b"])            # each thread kept its own call's tokens

    def test_the_cost_in_whole_paise_at_the_published_prices(self):
        self.assertEqual(str(sarvam.RATE_INPUT_RS_PER_M), "29.28")
        self.assertEqual(str(sarvam.RATE_CACHED_INPUT_RS_PER_M), "10.98")
        self.assertEqual(str(sarvam.RATE_OUTPUT_RS_PER_M), "73.20")
        cases = (
            (sarvam.Usage(3700, 40, 0), 11),                    # 0.111264 Rs: a typical command is about 11 paise
            (sarvam.Usage(1000000, 0, 0), 2928),                # Rs 29.28
            (sarvam.Usage(0, 1000000, 0), 7320),                # Rs 73.20
            (sarvam.Usage(1000000, 1000000, 400000), 9516),     # 600k at 29.28 + 400k cached at 10.98 + 1M out
            (sarvam.Usage(10, 1, 0), 0),                        # a fraction of a paisa rounds down
            (sarvam.Usage(93750, 0, 0), 275),                   # exactly 274.5 paise: halves round up
            (sarvam.Usage(100, 0, 500), sarvam.cost_paise(sarvam.Usage(100, 0, 100))),   # cached can never exceed the prompt
            (None, 0),
        )
        for usage, paise in cases:
            with self.subTest(usage=usage):
                self.assertEqual(sarvam.cost_paise(usage), paise)

    def test_a_thousand_typical_commands_cost_about_rs_111(self):
        self.assertEqual(sarvam.cost_paise(sarvam.Usage(3700000, 40000, 0)), 11126)


class Budget(unittest.TestCase):
    def test_a_rate_limit_is_retried_once_after_a_short_pause_and_then_succeeds(self):
        server = Server(httpx.Response(429, json={"error": {"code": "rate_limit_exceeded_error"}}), book_reply())
        backend, clock = backend_for(server)
        self.assertEqual(plan(backend).name, "book_appointment")
        self.assertEqual(server.calls, 2)
        self.assertEqual(clock.slept, [0.4])
        self.assertEqual(backend.last_usage, sarvam.Usage(3700, 40, 0))
        self.assertFalse(backend.breaker.is_open)

    def test_a_server_error_is_retried_too(self):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                server = Server(httpx.Response(status), book_reply())
                backend, _ = backend_for(server)
                self.assertEqual(plan(backend).name, "book_appointment")
                self.assertEqual(server.calls, 2)

    def test_two_rate_limits_in_a_row_give_up(self):
        server = Server(httpx.Response(429), httpx.Response(429))
        backend, clock = backend_for(server)
        with self.assertRaises(sarvam.SarvamError) as caught:
            plan(backend)
        self.assertEqual(caught.exception.reason, "rate limited")
        self.assertEqual(caught.exception.planner_note, "sarvam: rate limited")
        self.assertEqual((server.calls, clock.slept), (2, [0.4]))                  # one retry, never more
        self.assertIsNone(backend.last_usage)                                      # an unbilled call has no tokens

    def test_a_server_error_that_stays_gives_up_with_its_status(self):
        server = Server(httpx.Response(500))
        backend, _ = backend_for(server)
        with self.assertRaises(sarvam.SarvamError) as caught:
            plan(backend)
        self.assertEqual((caught.exception.reason, server.calls), ("http 500", 2))

    def test_anything_else_is_not_retried(self):
        for status in (400, 401, 403, 404, 422):
            with self.subTest(status=status):
                server = Server(httpx.Response(status, json={"error": "no"}))
                backend, clock = backend_for(server)
                with self.assertRaises(sarvam.SarvamError) as caught:
                    plan(backend)
                self.assertEqual((caught.exception.reason, server.calls, clock.slept), ("http {}".format(status), 1, []))

    def test_no_retry_when_the_budget_will_not_allow_one(self):
        clock = Clock()

        def slow_rate_limit(request):
            clock.now += 4.2              # the first answer took 4.2 s of the 5: 0.8 s left, a retry needs 1.4
            return httpx.Response(429)

        server = Server(slow_rate_limit, book_reply())
        backend, _ = backend_for(server, clock=clock)
        with self.assertRaises(sarvam.SarvamError) as caught:
            plan(backend)
        self.assertEqual((caught.exception.reason, server.calls, clock.slept), ("rate limited", 1, []))

    def test_a_retry_that_fits_is_given_only_what_is_left_of_the_budget(self):
        clock = Clock()

        def slow_rate_limit(request):
            clock.now += 3.0
            return httpx.Response(503)

        server = Server(slow_rate_limit, book_reply())
        backend, _ = backend_for(server, clock=clock)
        self.assertEqual(plan(backend).name, "book_appointment")
        self.assertEqual(server.calls, 2)
        self.assertAlmostEqual(server.requests[1].extensions["timeout"]["read"], 5.0 - 3.0 - 0.4, places=3)

    def test_a_timeout_is_a_failure_that_is_not_retried(self):
        server = Server(httpx.ReadTimeout("slow"), book_reply())
        backend, clock = backend_for(server)
        with self.assertRaises(sarvam.SarvamError) as caught:
            plan(backend)
        self.assertEqual((caught.exception.reason, server.calls, clock.slept), ("timeout", 1, []))
        self.assertEqual(caught.exception.planner_note, "sarvam: timeout")

    def test_a_network_error_is_a_failure_that_is_not_retried(self):
        server = Server(httpx.ConnectError("refused"), book_reply())
        backend, _ = backend_for(server)
        with self.assertRaises(sarvam.SarvamError) as caught:
            plan(backend)
        self.assertEqual((caught.exception.reason, server.calls), ("network error (ConnectError)", 1))

    def test_an_answer_that_never_comes_cannot_outlive_the_budget(self):
        server = Server(lambda request: (time.sleep(1.5), book_reply())[1])
        backend = sarvam.SarvamBackend(api_key=KEY, transport=httpx.MockTransport(server), timeout=0.25)
        started = time.monotonic()
        with self.assertRaises(sarvam.SarvamError) as caught:
            plan(backend)
        self.assertEqual(caught.exception.reason, "timeout")
        self.assertLess(time.monotonic() - started, 1.0)


class KeyIsNeverLeaked(unittest.TestCase):
    def failures(self):
        """Every way a call can fail, each against a server that echoes the key back in its body."""
        echo = {"error": "bad key " + KEY, "key": KEY}
        return (
            Server(httpx.Response(401, json=echo)),
            Server(httpx.Response(429, json=echo)),
            Server(httpx.Response(500, text=KEY)),
            Server(httpx.Response(200, text="not json " + KEY)),
            Server(httpx.ConnectError("could not connect, header api-subscription-key: " + KEY)),
            Server(httpx.ReadTimeout("slow " + KEY)),
            Server(RuntimeError("boom " + KEY)),
        )

    def test_neither_the_errors_nor_the_logs_carry_the_key_or_any_body(self):
        for server in self.failures():
            backend, _ = backend_for(server)
            with self.assertLogs(level=logging.DEBUG) as logs:
                logging.getLogger("clinic.test").debug("marker")        # assertLogs needs one line to exist
                with self.assertRaises(sarvam.SarvamError) as caught:
                    plan(backend, "Book Amit Dua tomorrow at 4 PM")
            seen = " ".join([str(caught.exception), caught.exception.reason, caught.exception.planner_note, repr(caught.exception)]
                            + logs.output + [r.getMessage() for r in logs.records])
            self.assertNotIn(KEY, seen)
            self.assertNotIn("SECRET", seen)
            self.assertNotIn("Amit Dua", seen)                           # the sentence is not logged by the backend
            self.assertIsNone(caught.exception.__cause__)
            self.assertTrue(caught.exception.__suppress_context__ or caught.exception.__context__ is None)

    def test_a_planner_run_logs_only_safe_words(self):
        conn = db.connect(":memory:")
        backend, _ = backend_for(Server(httpx.Response(401, json={"error": KEY})))
        run = planner.PlannerRun(conn, "Book Amit Dua tomorrow at 4 PM", backend=backend, today=TODAY)
        self.assertIsNone(run.ask())
        run.finish("unclear")
        row = dict(conn.execute("SELECT * FROM planner_log").fetchone())
        self.assertNotIn(KEY, json.dumps(row, default=str))
        self.assertEqual(row["override_notes"], "sarvam: http 401")

    def test_a_missing_key_is_one_warning_and_no_call(self):
        sarvam._warned["no_key"] = False
        self.addCleanup(sarvam._warned.update, no_key=False)
        server = Server(book_reply())
        backend = sarvam.SarvamBackend(transport=httpx.MockTransport(server))
        with patch.dict(os.environ, {"SARVAM_API_KEY": ""}), self.assertLogs("clinic.nlu.sarvam", level="WARNING") as logs:
            for _ in range(3):
                with self.assertRaises(sarvam.SarvamError) as caught:
                    plan(backend)
        self.assertEqual(caught.exception.planner_note, "sarvam: no key")
        self.assertEqual(server.calls, 0)
        self.assertEqual(len(logs.records), 1)                          # once, not once per command
        self.assertIn("SARVAM_API_KEY", logs.output[0])
        self.assertFalse(backend.breaker.is_open)                       # a missing key is not an outage


class Breaker(unittest.TestCase):
    def test_three_failures_in_a_row_open_it_for_thirty_seconds(self):
        server = Server(httpx.ConnectError("down"))
        backend, clock = backend_for(server)
        for _ in range(3):
            with self.assertRaises(sarvam.SarvamError):
                plan(backend)
        self.assertEqual(server.calls, 3)
        self.assertTrue(backend.breaker.is_open)
        for _ in range(2):
            with self.assertRaises(sarvam.SarvamError) as caught:
                plan(backend)
            self.assertEqual(caught.exception.planner_note, "sarvam: circuit open")
        self.assertEqual(server.calls, 3)                               # no call, no waiting
        clock.now += 29.9
        with self.assertRaises(sarvam.SarvamError):
            plan(backend)
        self.assertEqual(server.calls, 3)

    def test_after_the_pause_one_call_is_let_through_and_a_success_closes_it(self):
        server = Server(httpx.ConnectError("down"), httpx.ConnectError("down"), httpx.ConnectError("down"), book_reply())
        backend, clock = backend_for(server)
        for _ in range(3):
            with self.assertRaises(sarvam.SarvamError):
                plan(backend)
        clock.now += 30.1
        self.assertEqual(plan(backend).name, "book_appointment")        # the half-open probe
        self.assertFalse(backend.breaker.is_open)
        self.assertEqual(plan(backend).name, "book_appointment")        # and it is closed for good: counts start again
        self.assertEqual(server.calls, 5)

    def test_a_failed_probe_opens_it_again_for_another_pause(self):
        server = Server(httpx.ConnectError("down"))
        backend, clock = backend_for(server)
        for _ in range(3):
            with self.assertRaises(sarvam.SarvamError):
                plan(backend)
        clock.now += 30.1
        with self.assertRaises(sarvam.SarvamError) as caught:
            plan(backend)
        self.assertEqual(caught.exception.reason, "network error (ConnectError)")     # the probe really went out
        self.assertEqual(server.calls, 4)
        with self.assertRaises(sarvam.SarvamError) as caught:
            plan(backend)
        self.assertEqual(caught.exception.reason, "circuit open")
        self.assertEqual(server.calls, 4)
        clock.now += 30.1
        with self.assertRaises(sarvam.SarvamError):
            plan(backend)
        self.assertEqual(server.calls, 5)

    def test_while_one_probe_is_out_no_second_call_is_let_through(self):
        clock = Clock()
        breaker = sarvam.CircuitBreaker(clock=clock)
        for _ in range(3):
            breaker.record_failure()
        clock.now += 31
        self.assertTrue(breaker.allow())
        self.assertFalse(breaker.allow())
        breaker.record_success()
        self.assertTrue(breaker.allow())

    def test_a_success_resets_the_count(self):
        server = Server(httpx.ConnectError("x"), httpx.ConnectError("x"), book_reply(), httpx.ConnectError("x"), httpx.ConnectError("x"))
        backend, _ = backend_for(server)
        for outcome in ("fail", "fail", "ok", "fail", "fail"):
            if outcome == "ok":
                plan(backend)
            else:
                with self.assertRaises(sarvam.SarvamError):
                    plan(backend)
        self.assertFalse(backend.breaker.is_open)                       # four failures, but never three in a row

    def test_a_reply_the_planner_rejects_is_not_a_failure(self):
        backend, _ = backend_for(Server(reply("book_appointment", "{not json")))
        for _ in range(5):
            plan(backend)
        self.assertFalse(backend.breaker.is_open)

    def test_the_apps_one_backend_is_shared(self):
        self.assertIs(sarvam.shared_backend(), sarvam.shared_backend())


class Selection(unittest.TestCase):
    def test_local_is_the_default_and_sarvam_is_opt_in(self):
        with patch.dict(os.environ, {"PLANNER_BACKEND": ""}):
            self.assertEqual(planner.backend_name(), "local")
            self.assertIsInstance(planner.get_backend(), planner.OllamaBackend)
        with patch.dict(os.environ):
            os.environ.pop("PLANNER_BACKEND", None)
            self.assertEqual(planner.backend_name(), "local")
        for value in ("local", "ollama", "gemma", "Sarvam2"):
            with patch.dict(os.environ, {"PLANNER_BACKEND": value}):
                self.assertEqual(planner.backend_name(), "local", value)
        for value in ("sarvam", " Sarvam ", "SARVAM"):
            with patch.dict(os.environ, {"PLANNER_BACKEND": value}):
                self.assertEqual(planner.backend_name(), "sarvam", value)
                self.assertIs(planner.get_backend(), sarvam.shared_backend())

    def test_a_backend_set_by_a_test_still_wins(self):
        fake = planner.FakeBackend()
        with patch.dict(os.environ, {"PLANNER_BACKEND": "sarvam"}), planner.use_backend(fake):
            self.assertIs(planner.get_backend(), fake)

    def test_the_test_suite_never_selects_it_by_accident(self):
        self.assertEqual(os.environ.get("PLANNER_BACKEND"), "local")

    def test_there_is_no_warm_up_for_a_hosted_model(self):
        planner._warm.update(at=None, running=False)
        self.addCleanup(planner._warm.update, at=None, running=False)
        fake = planner.FakeBackend()
        with patch.dict(os.environ, {"INTENT_LLM_ENABLED": "1", "INTENT_PLANNER_ENABLED": "1", "PLANNER_BACKEND": "sarvam"}), \
                patch.object(planner.OllamaBackend, "plan", side_effect=AssertionError("Ollama was warmed up")) as ollama:
            self.assertIsNone(planner.warm_up_async(lambda: None, backend=fake))
            ollama.assert_not_called()
        self.assertEqual(fake.calls, [])
        with patch.dict(os.environ, {"INTENT_LLM_ENABLED": "1", "INTENT_PLANNER_ENABLED": "1", "PLANNER_BACKEND": "local"}):
            thread = planner.warm_up_async(lambda: db.connect(":memory:"), backend=fake)
            self.assertIsNotNone(thread)                                  # the local planner is warmed as before
            thread.join(5)
        self.assertEqual(len(fake.calls), 1)


class StaffPath(PlannerCase):
    """The staff voice flow with PLANNER_BACKEND=sarvam: the planner on the fake transport, and a tripwire on
    every way the local model could be reached (the Ollama HTTP call, the label picker, the name model, the
    planner's own Ollama backend, the warm-up)."""

    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {"PLANNER_BACKEND": "sarvam", "SARVAM_API_KEY": KEY})
        env.start()
        self.addCleanup(env.stop)
        self.server = Server(book_reply())
        self.sarvam, self.net_clock = backend_for(self.server)
        sarvam._warned["no_key"] = False
        self.addCleanup(sarvam._warned.update, no_key=False)
        planner.set_backend(self.sarvam)
        # The REAL local-model entry points, so their own guards are exercised; each is wrapped so a call is visible.
        self.real_pick = Mock(wraps=intent_llm.pick_intent)
        self.real_prefetch = Mock(wraps=llm_slots.prefetch_name)
        self.real_name = Mock(wraps=llm_slots.extract_name)
        for target, wrapper in (("clinic.nlu.parser.pick_intent", self.real_pick), ("clinic.nlu.parser.prefetch_name", self.real_prefetch),
                                ("clinic.nlu.parser.extract_name", self.real_name), ("clinic.voice_turns.extract_name", self.real_name)):
            patcher = patch(target, wrapper)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(planner.OllamaBackend, "plan", side_effect=AssertionError("the local planner was used"))
        self.ollama_planner = patcher.start()
        self.addCleanup(patcher.stop)

    def command(self, text):
        """One command in a fresh conversation (so nothing carries over from the last one)."""
        self.ctx = VoiceContext(clock=self.clock)
        self.ctx.set_client_branch(1, 1)
        return handle_turn(self.ctx, self.conn, text, self.adapter, self.adapter, "en-IN", DEFER)

    def assertLocalModelUntouched(self):
        self.live.assert_not_called()                     # no Ollama HTTP call of any kind
        self.ollama_planner.assert_not_called()
        self.real_pick.assert_not_called()
        self.real_prefetch.assert_not_called()

    def break_sarvam(self, *answers):
        self.server.answers[:] = list(answers or [httpx.ConnectError("offline")])

    # -- the planner answers --------------------------------------------------------------------

    def test_a_sarvam_answer_becomes_the_review_card_and_costs_what_the_log_says(self):
        self.server.answers[:] = [reply("book_appointment", json.dumps({"patient_name": "Rakesh Verma", "date": self.tomorrow_iso,
                                                                         "time": "16:00"}), prompt=3712, completion=41)]
        card = self.command("Book Rakesh Verma tomorrow at 4 PM")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual((card.intent, card.slots["patient_name"], card.slots["start_time"]), ("book_appointment", "Rakesh Verma", "16:00"))
        self.assertEqual(self.server.calls, 1)
        [row] = self.log_rows()
        self.assertEqual((row["route_taken"], row["backend"], row["tokens_in"], row["tokens_out"], row["cost_paise"]),
                         ("planner", "sarvam", 3712, 41, 11))
        self.assertIsNone(row["override_notes"])
        self.assertLocalModelUntouched()

    def test_the_prompt_sent_is_the_planners_prompt_and_the_sentence_no_patient_data(self):
        self.command("Book Rakesh Verma tomorrow at 4 PM")
        body = json.loads(self.server.requests[0].content)
        text = json.dumps(body)
        for patient_data in ("9000000001", "9000000002", "Mohan Lal", "Sunita Devi"):
            self.assertNotIn(patient_data, text)                          # registered patients are not sent
        self.assertIn("Book Rakesh Verma tomorrow at 4 PM", body["messages"][1]["content"])
        self.assertIn("Dr. Rao", body["messages"][0]["content"])
        self.assertNotIn(KEY, text)

    # -- the planner cannot help: straight to the rules, nothing local ------------------------------

    def test_a_failed_call_goes_straight_to_the_keyword_rules_for_the_same_card(self):
        for text in ("book Rakesh Verma tomorrow at 5 pm",                       # English
                     "kal 5 baje Sunita Devi ka appointment book karo",          # Hinglish
                     "cancel Mohan Das appointment",
                     "Sunita Devi ka follow up 5 din baad",
                     "show tomorrow's appointments"):
            with self.subTest(text=text):
                after_failure = self.run_in_mode("sarvam", text)
                self.assertEqual(after_failure, self.run_in_mode("local", text), text)   # what the failing local planner gave today

    def run_in_mode(self, mode, text):
        """(result type, intent, slots) of one command, with the planner failing, in either mode."""
        with patch.dict(os.environ, {"PLANNER_BACKEND": mode}):
            if mode == "sarvam":
                self.server.answers[:] = [httpx.ConnectError("offline")]
                planner.set_backend(self.sarvam)
                self.sarvam.breaker.record_success()
            else:
                planner.set_backend(planner.FakeBackend(httpx.ConnectError("offline")))
            result = self.command(text)
        return type(result).__name__, getattr(result, "intent", None), getattr(result, "slots", None)

    def test_after_a_failure_the_phrases_are_placed_by_rules_and_nothing_local_is_asked(self):
        self.break_sarvam(httpx.ReadTimeout("slow"))
        cases = (
            ("book Rakesh Verma tomorrow at 5 pm", "book_appointment", "17:00"),
            ("kal 5 baje Sunita Devi ka appointment book karo", "book_appointment", "05:00"),
            ("Sunita Devi ka follow up 5 din baad", "set_followup", None),
            ("cancel Mohan Das appointment", "cancel_appointment", None),
            ("आज की अपॉइंटमेंट दिखाओ", "list_appointments", None),
        )
        for text, intent, start in cases:
            with self.subTest(text=text):
                self.sarvam.breaker.record_success()
                result = self.command(text)
                self.assertEqual(getattr(result, "intent", None), intent)
                if start:
                    self.assertEqual(result.slots["start_time"], start)
        self.assertLocalModelUntouched()
        rows = self.log_rows()
        self.assertEqual({r["route_taken"] for r in rows}, {"rules"})
        self.assertEqual({r["override_notes"] for r in rows}, {"sarvam: timeout"})
        self.assertEqual({(r["backend"], r["tokens_in"], r["cost_paise"]) for r in rows}, {("sarvam", None, 0)})

    def test_each_reason_is_written_to_the_log_without_secrets(self):
        reasons = (
            (httpx.ReadTimeout("slow"), "sarvam: timeout"),
            (httpx.Response(429), "sarvam: rate limited"),
            (httpx.Response(503), "sarvam: http 503"),
            (httpx.ConnectError("offline"), "sarvam: network error (ConnectError)"),
        )
        for answer, note in reasons:
            with self.subTest(note=note):
                self.break_sarvam(answer)
                self.sarvam.breaker.record_success()
                self.command("book Rakesh Verma tomorrow at 5 pm")
        self.assertEqual([r["override_notes"] for r in self.log_rows()], [n for _, n in reasons])
        self.assertLocalModelUntouched()

    def test_an_open_circuit_is_instant_and_logged(self):
        self.break_sarvam()
        for _ in range(3):
            self.command("book Rakesh Verma tomorrow at 5 pm")
        calls = self.server.calls
        card = self.command("book Rakesh Verma tomorrow at 5 pm")
        self.assertEqual(self.server.calls, calls)                       # no wait, no call
        self.assertEqual((card.intent, card.slots["start_time"]), ("book_appointment", "17:00"))
        self.assertEqual(self.log_rows()[-1]["override_notes"], "sarvam: circuit open")
        self.assertEqual(self.log_rows()[-1]["route_taken"], "rules")
        self.assertLocalModelUntouched()

    def test_a_missing_key_is_the_same_fallback(self):
        with patch.dict(os.environ, {"SARVAM_API_KEY": ""}):
            self.sarvam._api_key = None
            card = self.command("book Rakesh Verma tomorrow at 5 pm")
        self.assertEqual(card.intent, "book_appointment")
        self.assertEqual(self.server.calls, 0)
        self.assertEqual(self.log_rows()[0]["override_notes"], "sarvam: no key")
        self.assertLocalModelUntouched()

    def test_a_reply_the_planner_rejects_also_falls_to_the_rules_not_the_picker(self):
        self.server.answers[:] = [reply("book_appointment", json.dumps({"patient_name": "Rakesh Verma", "invented": "x"}))]
        card = self.command("book Rakesh Verma tomorrow at 5 pm")
        self.assertEqual((card.intent, card.slots["start_time"]), ("book_appointment", "17:00"))
        [row] = self.log_rows()
        self.assertEqual(row["route_taken"], "rules")
        self.assertIn("rejected", row["override_notes"])
        self.assertEqual((row["tokens_in"], row["cost_paise"]), (3700, 11))      # the call was answered, so it was billed
        self.assertLocalModelUntouched()

    def test_no_tool_call_falls_to_the_rules(self):
        self.server.answers[:] = [reply(finish="stop")]
        card = self.command("cancel Mohan Das appointment")
        self.assertEqual(card.intent, "cancel_appointment")
        self.assertLocalModelUntouched()

    def test_nothing_places_it_so_the_user_is_asked_to_rephrase(self):
        self.break_sarvam()
        with self.assertRaises(PipelineError) as caught:
            self.command("play some music please")
        self.assertIn("Could not classify", str(caught.exception))
        self.assertEqual(self.log_rows()[0]["route_taken"], "rephrase")
        self.assertLocalModelUntouched()

    def test_an_unknown_name_is_not_looked_up_by_a_model_so_which_patient_is_asked(self):
        self.break_sarvam()
        ask = self.command("book Amit Dua tomorrow at 4 PM")
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.kind, "patient")
        self.assertIsNone(ask.slots["patient_name"])
        self.assertEqual((ask.slots["appt_date"], ask.slots["start_time"]), (self.tomorrow_iso, "16:00"))     # the rest is still read
        self.assertLocalModelUntouched()

    def test_a_new_patient_card_gets_its_phone_and_age_but_a_blank_name(self):
        self.break_sarvam()
        card = self.command("register a new patient Neha Jain phone 9812345678 age 30")
        self.assertEqual(card.intent, "register_patient")
        self.assertEqual((card.slots["name"], card.slots["phone"], card.slots["age"]), (None, "9812345678", 30))
        self.assertLocalModelUntouched()

    def test_answering_which_patient_with_a_sentence_does_not_ask_a_model_either(self):
        self.break_sarvam()
        ask = self.command("book tomorrow at 5 pm")
        self.assertIsInstance(ask, AskResult)
        asked = self.real_name.call_count
        followup = handle_turn(self.ctx, self.conn, "I think the patient is Amit Dua from Sector 5 please", self.adapter, self.adapter,
                               "en-IN", DEFER)
        self.assertIsNotNone(followup)
        self.assertGreater(self.real_name.call_count, asked)              # the name extractor really was reached ...
        self.assertLocalModelUntouched()                                  # ... and it asked no model
        self.assertTrue(llm_slots.local_model_allowed())                  # the switch is only on while a command is parsed

    def test_the_guards_hold_on_their_own_even_with_the_planner_off(self):
        with patch.dict(os.environ, {"INTENT_PLANNER_ENABLED": "0"}):
            card = self.command("book Rakesh Verma tomorrow at 5 pm")
        self.assertEqual(card.intent, "book_appointment")
        self.assertEqual(self.server.calls, 0)
        self.assertEqual(self.log_rows(), [])
        self.assertLocalModelUntouched()
        with llm_slots.staff_command():
            self.assertFalse(llm_slots.local_model_allowed())
            self.assertIsNone(intent_llm.pick_intent("book Amit tomorrow"))
            self.assertIsNone(llm_slots.extract_name("book Amit Dua tomorrow"))
            llm_slots.prefetch_name("book Amit Dua tomorrow")
            self.assertEqual(llm_slots.extract_name("book Mohan Lal", model=llm_slots.MODEL), None)
        self.live.assert_not_called()
        self.assertTrue(llm_slots.local_model_allowed())

    def test_the_scope_setting_is_unchanged_with_the_rules_standing_when_they_matched(self):
        with patch.dict(os.environ, {"INTENT_PLANNER_SCOPE": "unmatched"}):
            card = self.command("book Rakesh Verma tomorrow at 5 pm")
            self.assertEqual(card.intent, "book_appointment")
            self.assertEqual(self.server.calls, 0)                         # the rules placed it: the planner is not asked
            self.server.answers[:] = [reply("queue_action", '{"action": "status"}')]
            self.command("play some music please")
            self.assertEqual(self.server.calls, 1)                         # the rules found nothing: it is
        self.assertLocalModelUntouched()

    def test_a_planner_closing_command_that_fails_is_read_by_the_rules(self):
        self.break_sarvam()
        result = self.command("close branch B tomorrow for the next one week")
        self.assertIsNotNone(result)
        self.assertLocalModelUntouched()


class LocalBackendUnchanged(PlannerCase):
    """PLANNER_BACKEND unset or 'local': everything is exactly as before this feature."""

    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {"PLANNER_BACKEND": "local"})
        env.start()
        self.addCleanup(env.stop)

    def test_a_failed_planner_still_asks_the_label_picker_and_the_name_model(self):
        self.backend.script = httpx.ConnectError("offline")
        self.pick.return_value = "book_appointment"
        card = self.say("please do the needful for Rakesh Verma tomorrow at 5 pm")
        self.assertEqual(card.intent, "book_appointment")
        self.pick.assert_called_once()
        [row] = self.log_rows()
        self.assertEqual(row["route_taken"], "label_fallback")
        self.assertEqual((row["backend"], row["tokens_in"], row["tokens_out"], row["cost_paise"]), ("fake", None, None, 0))
        self.assertTrue(row["override_notes"].startswith("planner error: ConnectError"))

    def test_the_name_model_is_still_asked_for_a_name_it_does_not_know(self):
        answer = Mock()
        answer.json.return_value = {"message": {"content": "Amit Dua"}}
        self.live.side_effect, self.live.return_value = None, answer
        with llm_slots.staff_command():
            self.assertEqual(llm_slots.extract_name("book Amit Dua tomorrow"), "Amit Dua")
        self.live.assert_called_once()
        self.assertTrue(llm_slots.local_model_allowed())

    def test_the_picker_is_still_consulted_by_the_real_function(self):
        answer = Mock()
        answer.json.return_value = {"message": {"content": "book_appointment"}}
        self.live.side_effect, self.live.return_value = None, answer
        with llm_slots.staff_command():
            self.assertEqual(intent_llm.pick_intent("do the needful for Amit"), "book_appointment")
        self.live.assert_called_once()

    def test_the_real_ollama_backend_is_the_one_used_and_logged_as_local(self):
        planner.set_backend(None)
        answer = Mock()
        answer.json.return_value = {"message": {"tool_calls": [{"function": {"name": "queue_action", "arguments": {"action": "status"}}}]}}
        self.live.side_effect, self.live.return_value = None, answer
        result = self.say("who is next in the queue")
        self.assertIsNotNone(result)
        self.assertEqual(self.live.call_args.args[0], llm_slots.OLLAMA_URL)
        [row] = self.log_rows()
        self.assertEqual((row["backend"], row["route_taken"], row["tokens_in"], row["cost_paise"]), ("local", "planner", None, 0))


class PlannerLogColumns(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def rows(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM planner_log ORDER BY id")]

    def test_a_fresh_database_has_the_columns(self):
        names = {r[1] for r in self.conn.execute("PRAGMA table_info(planner_log)")}
        self.assertTrue({"backend", "tokens_in", "tokens_out", "cost_paise"} <= names)

    def test_record_writes_them_and_defaults_to_nothing_billed(self):
        planner_log.record(self.conn, "voice", "x", None, "t", {}, "planner", "i", 5, [], backend="sarvam", tokens_in=3700,
                           tokens_out=40, cost_paise=11)
        planner_log.record(self.conn, "voice", "y", None, None, None, "rules", "i", 5, [])
        first, second = self.rows()
        self.assertEqual((first["backend"], first["tokens_in"], first["tokens_out"], first["cost_paise"]), ("sarvam", 3700, 40, 11))
        self.assertEqual((second["backend"], second["tokens_in"], second["tokens_out"], second["cost_paise"]), (None, None, None, 0))

    def test_the_run_prices_a_call_from_its_tokens(self):
        backend, _ = backend_for(Server(book_reply(prompt=3712, completion=41)))
        run = planner.PlannerRun(self.conn, "Book Amit Dua tomorrow at 4 PM", backend=backend, today=TODAY)
        self.assertIsNotNone(run.ask())
        run.finish("book_appointment")
        [row] = self.rows()
        self.assertEqual((row["backend"], row["route_taken"], row["tokens_in"], row["tokens_out"], row["cost_paise"]),
                         ("sarvam", "planner", 3712, 41, 11))

    def test_a_failed_call_is_logged_with_no_tokens_and_no_cost(self):
        for answer in (httpx.Response(429), httpx.ReadTimeout("slow")):
            backend, _ = backend_for(Server(answer))
            run = planner.PlannerRun(self.conn, "Book Amit Dua tomorrow at 4 PM", backend=backend, today=TODAY)
            self.assertIsNone(run.ask())
            run.finish("unclear")
        for row in self.rows():
            self.assertEqual((row["backend"], row["tokens_in"], row["tokens_out"], row["cost_paise"]), ("sarvam", None, None, 0))

    def test_the_planner_log_switch_still_turns_it_all_off(self):
        settings.set_planner_log_enabled(self.conn, False)
        backend, _ = backend_for(Server(book_reply()))
        run = planner.PlannerRun(self.conn, "Book Amit Dua tomorrow at 4 PM", backend=backend, today=TODAY)
        run.ask()
        run.finish("book_appointment")
        self.assertEqual(self.rows(), [])

    def test_the_export_script_reads_a_log_that_has_the_new_columns(self):
        from scripts import export_planner_log
        planner_log.record(self.conn, "voice", "Book Amit Dua tomorrow at 4 PM", None, "book_appointment", {"patient_name": "Amit Dua"},
                           "planner", "book_appointment", 300, [], backend="sarvam", tokens_in=3700, tokens_out=40, cost_paise=11)
        cases = [export_planner_log.to_case(r) for r in export_planner_log.fetch(self.conn)]
        self.assertEqual(cases[0][:3], ("Book Amit Dua tomorrow at 4 PM", "book_appointment", {"patient_name": "Amit Dua"}))
        self.assertIn("Amit Dua", export_planner_log.as_python(cases))


class UsageQuery(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def add(self, ts, backend="sarvam", tokens_in=3700, tokens_out=40, cost=11):
        self.conn.execute(
            "INSERT INTO planner_log (ts, transcript, route_taken, backend, tokens_in, tokens_out, cost_paise) VALUES (?, 'x', 'planner', ?, ?, ?, ?)",
            (ts, backend, tokens_in, tokens_out, cost))
        self.conn.commit()

    def usage(self, now=datetime(2026, 10, 15, 12, 0)):
        return planner_log.sarvam_usage(self.conn, now)

    def test_the_month_runs_from_midnight_ist_to_midnight_ist(self):
        self.add("2026-09-30 18:29:59")       # 23:59:59 IST on 30 September: last month
        self.add("2026-09-30 18:30:00")       # 00:00:00 IST on 1 October: this month
        self.add("2026-10-15 06:00:00")
        self.add("2026-10-31 18:29:59")       # 23:59:59 IST on 31 October: this month
        self.add("2026-10-31 18:30:00")       # 00:00:00 IST on 1 November: next month
        usage = self.usage()
        self.assertEqual((usage["month"], usage["commands"], usage["spend_paise"]), ("2026-10", 3, 33))
        self.assertEqual(usage["spend_rupees"], 0.33)
        self.assertEqual((usage["tokens_in"], usage["tokens_out"]), (11100, 120))

    def test_december_rolls_into_the_next_year(self):
        self.add("2026-11-30 18:30:00")
        self.add("2026-12-31 18:29:59")
        self.add("2026-12-31 18:30:00")
        self.add("2026-11-30 18:29:59")
        usage = self.usage(datetime(2026, 12, 25, 9, 0))
        self.assertEqual((usage["month"], usage["commands"]), ("2026-12", 2))

    def test_the_first_and_last_instant_of_the_clock_month(self):
        self.add("2026-10-01 05:00:00")
        self.assertEqual(self.usage(datetime(2026, 10, 1, 0, 0))["commands"], 1)
        self.assertEqual(self.usage(datetime(2026, 10, 31, 23, 59, 59))["commands"], 1)
        self.assertEqual(self.usage(datetime(2026, 11, 1, 0, 0))["commands"], 0)

    def test_only_sarvam_commands_that_were_answered_count(self):
        self.add("2026-10-10 05:00:00")
        self.add("2026-10-10 05:00:00", backend="local", tokens_in=None, tokens_out=None, cost=0)
        self.add("2026-10-10 05:00:00", backend="fake", tokens_in=None, tokens_out=None, cost=0)
        self.add("2026-10-10 05:00:00", backend=None, tokens_in=None, tokens_out=None, cost=None)
        self.add("2026-10-10 05:00:00", tokens_in=None, tokens_out=None, cost=0)           # a failed Sarvam call: not billed
        usage = self.usage()
        self.assertEqual((usage["commands"], usage["spend_paise"], usage["tokens_in"]), (1, 11, 3700))

    def test_nothing_this_month_is_zero_not_an_error(self):
        self.assertEqual(self.usage(), {"month": "2026-10", "commands": 0, "tokens_in": 0, "tokens_out": 0, "spend_paise": 0,
                                        "spend_rupees": 0.0})

    def test_rupees_have_two_decimals(self):
        self.add("2026-10-10 05:00:00", cost=1240)
        self.assertEqual(self.usage()["spend_rupees"], 12.4)
        self.assertEqual("%.2f" % self.usage()["spend_rupees"], "12.40")

    def test_a_database_without_the_table_gives_zeros(self):
        usage = planner_log.sarvam_usage(sqlite3.connect(":memory:"), datetime(2026, 10, 15))
        self.assertEqual((usage["commands"], usage["spend_paise"]), (0, 0))


class UsageRoute(RouteTestCase):
    NOW = datetime(2026, 10, 15, 12, 0)

    def setUp(self):
        super().setUp()
        clinic_app.CLOCK = lambda: self.NOW
        self.addCleanup(setattr, clinic_app, "CLOCK", None)
        env = patch.dict(os.environ, {"PLANNER_BACKEND": "sarvam", "SARVAM_API_KEY": KEY})
        env.start()
        self.addCleanup(env.stop)

    def add(self, ts, backend="sarvam", tokens_in=3700, tokens_out=40, cost=11):
        self.conn.execute(
            "INSERT INTO planner_log (ts, transcript, route_taken, backend, tokens_in, tokens_out, cost_paise) VALUES (?, 'x', 'planner', ?, ?, ?, ?)",
            (ts, backend, tokens_in, tokens_out, cost))
        self.conn.commit()

    def fetch(self):
        response = self.client.get("/settings/sarvam-usage")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["ok"])
        return body["data"]

    def test_the_route_reports_this_months_estimate(self):
        self.add("2026-09-30 18:29:59", cost=500)           # last month (IST)
        self.add("2026-10-02 05:00:00", cost=1000, tokens_in=1, tokens_out=2)
        self.add("2026-10-03 05:00:00", cost=240, tokens_in=10, tokens_out=20)
        self.add("2026-10-04 05:00:00", backend="local", tokens_in=None, tokens_out=None, cost=0)
        self.add("2026-10-05 05:00:00", tokens_in=None, tokens_out=None, cost=0)            # failed: not counted
        data = self.fetch()
        self.assertEqual(data, {"month": "2026-10", "commands": 2, "tokens_in": 11, "tokens_out": 22, "spend_paise": 1240,
                                "spend_rupees": 12.4, "active": True, "log_enabled": True})

    def test_it_uses_the_clinics_clock_so_the_month_can_change_under_it(self):
        self.add("2026-10-20 05:00:00")
        self.add("2026-11-20 05:00:00")
        self.assertEqual(self.fetch()["commands"], 1)
        clinic_app.CLOCK = lambda: datetime(2026, 11, 2, 9, 0)
        data = self.fetch()
        self.assertEqual((data["month"], data["commands"]), ("2026-11", 1))

    def test_active_needs_the_switch_and_a_key(self):
        self.assertTrue(self.fetch()["active"])
        with patch.dict(os.environ, {"SARVAM_API_KEY": "  "}):
            self.assertFalse(self.fetch()["active"])
        with patch.dict(os.environ, {"PLANNER_BACKEND": "local"}):
            self.assertFalse(self.fetch()["active"])
        with patch.dict(os.environ, {"PLANNER_BACKEND": ""}):
            self.assertFalse(self.fetch()["active"])

    def test_inactive_with_no_usage_is_all_zeros(self):
        with patch.dict(os.environ, {"PLANNER_BACKEND": "local"}):
            data = self.fetch()
        self.assertEqual((data["active"], data["commands"], data["spend_rupees"]), (False, 0, 0.0))

    def test_it_says_when_the_log_that_counts_is_off(self):
        settings.set_planner_log_enabled(self.conn, False)
        self.assertFalse(self.fetch()["log_enabled"])

    def test_the_route_is_read_only_and_never_shows_the_key(self):
        before = self.count("planner_log")
        response = self.client.get("/settings/sarvam-usage")
        self.assertNotIn(KEY, response.get_data(as_text=True))
        self.assertEqual(self.client.post("/settings/sarvam-usage").status_code, 405)
        self.assertEqual(self.count("planner_log"), before)


class SettingsLine(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
        self.js = (ROOT / "static" / "settings_sarvam.js").read_text(encoding="utf-8")

    def test_the_settings_tab_has_a_place_for_it_after_the_follow_up_cards(self):
        start = self.html.index('<section class="tab-panel" data-tab="settings"')
        section = self.html[start:self.html.index("</section>", start)]
        self.assertEqual(section.count('id="settings-sarvam"'), 1)
        self.assertLess(section.index('id="settings-followups"'), section.index('id="settings-sarvam"'))

    def test_the_script_loads_after_the_settings_scripts_and_before_the_nav(self):
        order = re.findall(r"filename='([\w.]+\.js)'", self.html)
        self.assertEqual(order.count("settings_sarvam.js"), 1)
        self.assertLess(order.index("settings_followups.js"), order.index("settings_sarvam.js"))
        self.assertLess(order.index("settings_sarvam.js"), order.index("nav.js"))
        self.assertTrue((ROOT / "static" / "settings_sarvam.js").exists())

    def test_it_reads_the_route_and_refreshes_when_settings_opens(self):
        self.assertIn('"/settings/sarvam-usage"', self.js)
        self.assertIn('"tabchange"', self.js)
        self.assertIn('event.detail.id === "settings"', self.js)
        self.assertIn('document.getElementById("settings-sarvam")', self.js)

    def test_there_is_nothing_to_save(self):
        for needle in ('method: "POST"', "SaveTick", "button", "<form", "localStorage"):
            self.assertNotIn(needle, self.js)

    def test_the_dom_is_text_only(self):
        for needle in ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write", "eval("):
            self.assertNotIn(needle, self.js)
        self.assertIn("textContent", self.js)

    def test_it_uses_the_existing_card_style_and_no_colours_of_its_own(self):
        self.assertIn('"card"', self.js)
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b", self.js), [])

    def test_the_wording_says_it_is_an_estimate_at_published_prices(self):
        self.assertIn("Estimated from token counts at Sarvam's published prices.", self.js)
        self.assertIn("Sarvam planner is off.", self.js)


class NodeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_pure_helper(self):
        out = subprocess.run(["node", str(ROOT / "tests" / "sarvam_usage.test.js")], capture_output=True, text=True, cwd=str(ROOT),
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("passed", out.stdout)


class Housekeeping(unittest.TestCase):
    def test_the_benchmark_script_reuses_the_apps_backend(self):
        source = (ROOT / "scripts" / "eval_sarvam.py").read_text(encoding="utf-8")
        self.assertIn("from clinic.nlu import sarvam", source)
        self.assertIn("class SarvamBackend(sarvam.SarvamBackend)", source)
        self.assertNotIn("httpx", source)                              # no second implementation of the call
        self.assertNotIn("api.sarvam.ai", source)

    def test_the_placeholders_in_env_example_are_commented_and_hold_no_value(self):
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("# PLANNER_BACKEND=sarvam", text)
        self.assertIn("# SARVAM_TIMEOUT_S=5", text)
        self.assertNotRegex(text, r"(?m)^PLANNER_BACKEND=")

    def test_the_default_in_code_is_local(self):
        source = (ROOT / "clinic" / "nlu" / "llm_slots.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("PLANNER_BACKEND", "")', source)


if __name__ == "__main__":
    unittest.main()
