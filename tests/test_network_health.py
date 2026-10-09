"""The connection chip's engine (clinic/network_health.py): what counts as a network failure, the Good / Slow /
Down state machine with hysteresis (fake clock), the per-service rows, the call-site hooks (speech connect, the
Sarvam planner, WhatsApp sends), the idle check and the scheduler tick, the incident table and its pruning, the
two routes, the broadcast-only-on-change rule and the template wiring. Everything is offline: fake clocks, fake
transports, fake sockets. Nothing reaches Sarvam, Meta or a real database."""
import json
import os
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import httpx
from websockets.exceptions import ConnectionClosed, InvalidHandshake

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_app_routes import RouteTestCase, clinic_app  # noqa: E402  (stubs dotenv, never reads .env)
from tests.test_hold_to_talk import FakeSocket, FakeSTT, HoldToTalkBase, wait_for  # noqa: E402
from tests.test_planner_sarvam import KEY, SCHEMAS, Server, backend_for, book_reply, plan, reply  # noqa: E402
from tests.test_scheduler import make_db  # noqa: E402
from tests.test_speech_connect_retry import Flaky  # noqa: E402

from clinic import db, network_health, realtime_voice, scheduler, whatsapp  # noqa: E402
from clinic.network_health import DOWN, GOOD, SLOW, Monitor  # noqa: E402
from clinic.nlu import sarvam  # noqa: E402
from clinic.timefmt import IST  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NOON = datetime(2026, 10, 8, 12, 0, tzinfo=IST).timestamp()      # a fixed instant; no test reads the real date


class FakeClock:
    def __init__(self, now=NOON):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def fresh(clock=None, **wiring):
    clock = clock or FakeClock()
    return Monitor(clock=clock, **wiring), clock


def fail(mon, service="planner", kind="timeout", **kw):
    mon.record(service, False, 1000, kind, **kw)


def ok(mon, service="planner", **kw):
    mon.record(service, True, 200, **kw)


def events_of(mon):
    return [(e.service, e.ok, e.kind) for e in mon._events]


class SharedMonitorCase(unittest.TestCase):
    """Tests of the call-site hooks use the app-wide monitor: start each from a clean one, with no wiring."""

    def setUp(self):
        self.clock = FakeClock()
        network_health.reset(clock=self.clock)
        self.addCleanup(network_health.reset)

    def events(self):
        return events_of(network_health.monitor())


# ---- what counts as a network failure ------------------------------------------------------------------------------

class ClassifyTests(unittest.TestCase):
    def test_the_kinds(self):
        c = network_health.classify
        self.assertEqual(c(httpx.ReadTimeout("x")), "timeout")
        self.assertEqual(c(TimeoutError("timed out")), "timeout")
        self.assertEqual(c(socket.timeout("timed out")), "timeout")
        self.assertEqual(c(httpx.ConnectTimeout("x")), "connect")
        self.assertEqual(c(httpx.ConnectError("x")), "connect")
        self.assertEqual(c(ConnectionRefusedError()), "connect")
        self.assertEqual(c(OSError("Network is unreachable")), "connect")
        self.assertEqual(c(httpx.ReadError("x")), "dropped")
        self.assertEqual(c(httpx.RemoteProtocolError("x")), "dropped")
        self.assertEqual(c(ConnectionResetError()), "dropped")
        self.assertEqual(c(BrokenPipeError()), "dropped")

    def test_a_tls_handshake_that_timed_out(self):
        self.assertEqual(network_health.classify(ssl.SSLError("_ssl.c:1112: The handshake operation timed out")), "handshake")
        self.assertEqual(network_health.classify(socket.timeout("_ssl.c:1112: The handshake operation timed out"),
                                                 "connect"), "handshake")
        self.assertEqual(network_health.classify(TimeoutError("timed out during opening handshake"), "connect"), "handshake")
        self.assertEqual(network_health.classify(httpx.ConnectTimeout("The handshake operation timed out")), "handshake")

    def test_while_connecting_a_timeout_or_a_reset_is_could_not_connect(self):
        self.assertEqual(network_health.classify(socket.timeout("timed out"), "connect"), "connect")
        self.assertEqual(network_health.classify(ConnectionResetError(), "connect"), "connect")

    def test_a_websocket_closing_mid_use_is_a_drop(self):
        closed = ConnectionClosed(None, None)
        self.assertEqual(network_health.classify(closed), "dropped")

    def test_what_is_not_network_evidence(self):
        c = network_health.classify
        self.assertIsNone(c(RuntimeError("bug")))
        self.assertIsNone(c(ValueError("bad key")))
        self.assertIsNone(c(InvalidHandshake("server rejected the key")))      # a refused key (HTTP 401/403 on the upgrade)
        self.assertIsNone(c(ssl.SSLCertVerificationError("certificate verify failed")))
        self.assertIsNone(c(httpx.TooManyRedirects("x")))
        self.assertIsNone(c(httpx.DecodingError("x")))
        self.assertIsNone(c(KeyError("x")))

    def test_it_never_raises(self):
        class Odd(Exception):
            def __str__(self):
                raise RuntimeError("no text")
        self.assertIsNone(network_health.classify(Odd()))


# ---- the state machine ------------------------------------------------------------------------------------------------

class StateMachineTests(unittest.TestCase):
    def test_a_fresh_monitor_is_good_with_the_three_fixed_services(self):
        mon, _ = fresh()
        status = mon.status()
        self.assertEqual((status["state"], status["label"]), (GOOD, "Connection good"))
        self.assertEqual([r["name"] for r in status["services"]],
                         ["Voice (microphone)", "Understanding commands", "WhatsApp messages"])
        self.assertEqual({r["label"] for r in status["services"]}, {"OK"})
        self.assertIsNone(status["last_checked"])

    def test_one_failure_never_flips_the_chip(self):
        mon, _ = fresh()
        fail(mon)
        self.assertEqual(mon.status()["state"], GOOD)

    def test_two_failures_in_the_window_make_it_slow(self):
        mon, clock = fresh()
        fail(mon, "voice", "connect")
        clock.advance(40)
        fail(mon, "planner")
        self.assertEqual(mon.status()["state"], SLOW)

    def test_three_failures_in_a_row_across_services_make_it_down(self):
        mon, _ = fresh()
        fail(mon, "voice", "connect")
        fail(mon, "planner")
        self.assertEqual(mon.status()["state"], SLOW)
        fail(mon, "whatsapp", "dropped")
        status = mon.status()
        self.assertEqual((status["state"], status["label"]), (DOWN, "No connection"))

    def test_a_success_between_failures_keeps_it_from_going_down(self):
        mon, _ = fresh()
        fail(mon)
        fail(mon)
        ok(mon, "whatsapp")
        fail(mon)
        self.assertEqual(mon.status()["state"], SLOW)      # three failures, but not in a row

    def test_failures_age_out_of_the_window(self):
        mon, clock = fresh()
        fail(mon)
        fail(mon)
        self.assertEqual(mon.status()["state"], SLOW)
        clock.advance(network_health.WINDOW_S + 1)
        self.assertEqual(mon.status()["state"], GOOD)

    def test_down_recovers_only_after_three_successes_in_a_row(self):
        mon, clock = fresh()
        for _ in range(3):
            fail(mon)
        self.assertEqual(mon.status()["state"], DOWN)
        ok(mon)
        clock.advance(1)
        self.assertEqual(mon.status()["state"], DOWN)      # one success is not enough
        ok(mon)
        self.assertEqual(mon.status()["state"], DOWN)
        ok(mon)
        self.assertEqual(mon.status()["state"], GOOD)

    def test_a_failure_during_recovery_restarts_the_count(self):
        mon, _ = fresh()
        for _ in range(3):
            fail(mon)
        ok(mon)
        ok(mon)
        fail(mon)
        ok(mon)
        ok(mon)
        self.assertEqual(mon.status()["state"], DOWN)
        ok(mon)
        self.assertEqual(mon.status()["state"], GOOD)

    def test_after_recovery_the_old_failures_cannot_push_it_back_to_slow(self):
        mon, _ = fresh()
        fail(mon)
        fail(mon)
        self.assertEqual(mon.status()["state"], SLOW)
        for _ in range(3):
            ok(mon)
        self.assertEqual(mon.status()["state"], GOOD)
        fail(mon)                                          # the 3rd failure in the window, but the first since recovery
        self.assertEqual(mon.status()["state"], GOOD)
        self.assertEqual({r["label"] for r in mon.status()["services"]}, {"OK", "Slow"})   # the service shows its own failure

    def test_two_quiet_minutes_with_a_success_also_recover(self):
        mon, clock = fresh()
        fail(mon)
        fail(mon)
        clock.advance(10)
        ok(mon)
        self.assertEqual(mon.status()["state"], SLOW)
        clock.advance(network_health.RECOVER_QUIET_S - 20)
        self.assertEqual(mon.status()["state"], SLOW)
        clock.advance(20)
        self.assertEqual(mon.status()["state"], GOOD)

    def test_a_slow_idle_check_makes_it_slow_and_fast_checks_bring_it_back(self):
        mon, clock = fresh()
        mon.record("planner", True, 2600, probe="sarvam", services=("voice", "planner"))
        self.assertEqual(mon.status()["state"], SLOW)
        mon.record("planner", True, 90, probe="sarvam", services=("voice", "planner"))
        mon.record("planner", True, 90, probe="sarvam", services=("voice", "planner"))
        self.assertEqual(mon.status()["state"], SLOW)      # a slow check does not count as a good call
        mon.record("planner", True, 90, probe="sarvam", services=("voice", "planner"))
        self.assertEqual(mon.status()["state"], GOOD)

    def test_a_probe_exactly_at_the_limit_is_not_slow(self):
        mon, _ = fresh()
        mon.record("planner", True, network_health.PROBE_SLOW_MS, probe="sarvam")
        self.assertEqual(mon.status()["state"], GOOD)

    def test_every_idle_check_host_failing_twice_in_a_row_is_down(self):
        mon, _ = fresh()
        for _ in range(2):
            fail(mon, "planner", "connect", probe="sarvam", services=("voice", "planner"))
            ok(mon, "whatsapp", probe="meta")
        self.assertEqual(mon.status()["state"], SLOW)      # Meta still answers, so not down
        mon2, _ = fresh()
        fail(mon2, "planner", "connect", probe="sarvam", services=("voice", "planner"))
        fail(mon2, "whatsapp", "connect", probe="meta")
        ok(mon2, "voice")                                  # a good call in between: never 3 failures in a row
        fail(mon2, "planner", "connect", probe="sarvam", services=("voice", "planner"))
        self.assertEqual(mon2.status()["state"], SLOW)     # Meta has failed only once so far
        fail(mon2, "whatsapp", "connect", probe="meta")
        self.assertEqual(mon2.status()["state"], DOWN)     # both hosts failed their last two checks

    def test_the_idle_check_failing_once_per_host_is_not_down(self):
        mon, _ = fresh()
        fail(mon, "planner", "connect", probe="sarvam")
        self.assertEqual(mon.status()["state"], GOOD)

    def test_per_service_rows(self):
        mon, _ = fresh()
        ok(mon, "planner")
        fail(mon, "planner")
        ok(mon, "planner")
        fail(mon, "planner")
        rows = {r["key"]: r for r in mon.status()["services"]}
        self.assertEqual((rows["planner"]["label"], rows["planner"]["detail"]), ("Slow", "2 of last 4 failed"))
        self.assertEqual((rows["voice"]["label"], rows["voice"]["detail"]), ("OK", None))
        fail(mon, "voice", "connect")
        fail(mon, "voice", "connect")
        rows = {r["key"]: r for r in mon.status()["services"]}
        self.assertEqual((rows["voice"]["label"], rows["voice"]["detail"]), ("Not responding", "2 of last 2 failed"))

    def test_a_service_with_only_old_failures_looks_fine_again(self):
        mon, _ = fresh()
        fail(mon, "whatsapp", "connect")
        for _ in range(4):
            ok(mon, "whatsapp")
        row = {r["key"]: r for r in mon.status()["services"]}["whatsapp"]
        self.assertEqual((row["label"], row["detail"]), ("OK", None))      # the failure left its last 4 calls

    def test_the_idle_check_of_the_speech_host_counts_for_voice_and_commands(self):
        mon, _ = fresh()
        fail(mon, "planner", "connect", probe="sarvam", services=("voice", "planner"))
        rows = {r["key"]: r for r in mon.status()["services"]}
        self.assertEqual((rows["voice"]["label"], rows["planner"]["label"], rows["whatsapp"]["label"]),
                         ("Slow", "Slow", "OK"))

    def test_last_checked_is_the_ist_clock_time_of_the_latest_outcome(self):
        mon, clock = fresh()
        ok(mon)
        clock.advance(65)
        ok(mon)
        status = mon.status()
        self.assertEqual(status["last_checked"], "12:01")
        self.assertEqual(status["last_checked_ts"], NOON + 65)

    def test_bad_input_is_ignored_and_recording_never_raises(self):
        mon, _ = fresh()
        mon.record("telephone", False, 10, "timeout")
        mon.record(None, True)
        mon.record("planner", False, "not a number", "timeout")
        mon.record("planner", False, 10, "something odd")
        self.assertEqual(events_of(mon), [("planner", False, None)])       # an unknown kind is kept as unknown
        broken = Monitor(clock=mock.Mock(side_effect=RuntimeError("clock")))
        broken.record("planner", False, 10, "timeout")                     # no exception
        loud = Monitor(clock=FakeClock(), sink=mock.Mock(side_effect=RuntimeError("db")),
                       emit=mock.Mock(side_effect=RuntimeError("socket")))
        fail(loud)
        fail(loud)
        self.assertEqual(loud.status()["state"], SLOW)

    def test_concurrent_recording_is_safe(self):
        mon, _ = fresh()

        def hammer():
            for i in range(200):
                mon.record("planner", i % 2 == 0, 5, None if i % 2 == 0 else "timeout")
                mon.status()
        threads = [threading.Thread(target=hammer) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertIn(mon.status()["state"], (GOOD, SLOW, DOWN))
        self.assertLessEqual(len(mon._events), network_health.KEEP_EVENTS)


class BroadcastTests(unittest.TestCase):
    def test_it_is_emitted_only_when_something_changes(self):
        sent = []
        mon, clock = fresh(emit=sent.append)
        for _ in range(3):
            ok(mon)
        mon.status()
        mon.refresh()
        self.assertEqual(sent, [])                                         # nothing changed
        fail(mon, "planner")
        self.assertEqual(len(sent), 1)                                     # the planner row changed
        self.assertEqual(sent[0]["services"][1]["label"], "Slow")
        mon.status()
        mon.refresh()
        self.assertEqual(len(sent), 1)                                     # reading changes nothing
        fail(mon, "planner")
        self.assertEqual((len(sent), sent[-1]["state"]), (2, SLOW))        # now the state changed too
        fail(mon, "voice", "connect")
        self.assertEqual(sent[-1]["state"], DOWN)
        before = len(sent)
        ok(mon, "whatsapp")
        self.assertEqual(len(sent), before)                                # a lone success changes nothing visible

    def test_time_passing_alone_is_announced_by_the_next_refresh(self):
        sent = []
        mon, clock = fresh(emit=sent.append)
        fail(mon)
        fail(mon)
        sent.clear()
        clock.advance(network_health.WINDOW_S + 5)
        mon.refresh()
        self.assertEqual([m["state"] for m in sent], [GOOD])
        mon.refresh()
        self.assertEqual(len(sent), 1)

    def test_the_payload_carries_no_hosts_urls_or_text(self):
        sent = []
        mon, _ = fresh(emit=sent.append)
        fail(mon, "planner", "connect")
        text = json.dumps(sent)
        for forbidden in ("sarvam", "facebook", "http", "traceback", "token"):
            self.assertNotIn(forbidden, text.lower())

    def test_only_failures_reach_the_sink(self):
        stored = []
        mon, _ = fresh(sink=stored.append)
        ok(mon)
        fail(mon, "whatsapp", "dropped")
        self.assertEqual(len(stored), 1)
        self.assertEqual((stored[0]["service"], stored[0]["kind"], stored[0]["ms"]), ("whatsapp", "dropped", 1000))


# ---- the hooks at the real call sites ------------------------------------------------------------------------------

class SpeechConnectHookTests(SharedMonitorCase):
    def connect(self, factory, **kw):
        with realtime_voice._connect_with_retry(factory, "key", wait=lambda s: None, **kw) as client:
            return client

    def test_each_failed_attempt_and_the_success_are_recorded(self):
        self.connect(Flaky(FakeSTT(), 1, socket.timeout("timed out")))
        self.assertEqual(self.events(), [("voice", False, "connect"), ("voice", True, None)])

    def test_a_handshake_timeout_is_recorded_as_such(self):
        with self.assertRaises(socket.timeout):
            self.connect(Flaky(FakeSTT(), 9, socket.timeout("_ssl.c:1112: The handshake operation timed out")))
        self.assertEqual(self.events(), [("voice", False, "handshake")] * 2)
        self.assertEqual(network_health.status()["state"], SLOW)

    def test_a_refused_key_or_a_bug_is_not_a_network_failure(self):
        for error in (RuntimeError("bug"), ValueError("bad key"), InvalidHandshake("server rejected the key")):
            with self.assertRaises(type(error)):
                self.connect(Flaky(FakeSTT(), 9, error))
        self.assertEqual([e for e in self.events() if not e[1]], [])
        self.assertEqual(network_health.status()["state"], GOOD)


class VoiceSessionHookTests(HoldToTalkBase):
    def setUp(self):
        super().setUp()
        network_health.reset(clock=FakeClock())
        self.addCleanup(network_health.reset)

    def net(self):
        return events_of(network_health.monitor())

    def test_a_session_that_cannot_connect_records_voice_failures(self):
        session = self.make_session(Flaky(FakeSTT(), 99, socket.timeout("timed out")))
        with mock.patch.object(realtime_voice, "_CONNECT_PAUSE_S", 0.01):
            session.listen_start(1)
            self.assertTrue(wait_for(lambda: self.events("listen_end")))
        self.assertEqual(self.net(), [("voice", False, "connect")] * 2)
        row = {r["key"]: r for r in network_health.status()["services"]}["voice"]
        self.assertEqual(row["label"], "Not responding")
        self.assertNoLeakedThreads()

    def test_a_connection_that_opens_records_ok(self):
        stt = FakeSTT(final_on_end="kal ka calendar")
        session = self.make_session(stt)
        session.listen_start(1)
        session.send_audio("x")
        session.listen_stop()
        self.assertTrue(wait_for(lambda: self.events("listen_end")))
        self.assertEqual(self.net(), [("voice", True, None)])
        self.assertNoLeakedThreads()

    def test_a_connection_lost_mid_use_is_recorded_as_dropped(self):
        session = self.make_session(FakeSTT())
        with mock.patch.object(FakeSocket, "send_realtime_audio_input", side_effect=ConnectionResetError()):
            session.listen_start(1)
            session.send_audio("a")
            self.assertTrue(wait_for(lambda: self.events("listen_end")))
        self.assertIn(("voice", False, "dropped"), self.net())
        self.assertNoLeakedThreads()

    def test_a_bug_while_sending_is_not_a_network_failure(self):
        session = self.make_session(FakeSTT(fail_sends=True))               # raises RuntimeError("socket gone")
        session.listen_start(1)
        session.send_audio("a")
        self.assertTrue(wait_for(lambda: self.events("listen_end")))
        self.assertEqual([e for e in self.net() if not e[1]], [])
        self.assertNoLeakedThreads()


class PlannerHookTests(SharedMonitorCase):
    def failures(self):
        return [e for e in self.events() if not e[1]]

    def run_plan(self, *answers, **kw):
        server = Server(*answers)
        backend, clock = backend_for(server, **kw)
        try:
            plan(backend)
        except sarvam.SarvamError:
            pass
        return server

    def test_timeouts_and_connection_errors_are_network_failures(self):
        cases = ((httpx.ReadTimeout("slow"), "timeout"), (httpx.ConnectError("no route"), "connect"),
                 (httpx.ConnectTimeout("tcp"), "connect"), (httpx.ReadError("reset"), "dropped"),
                 (httpx.ConnectTimeout("_ssl.c:1112: The handshake operation timed out"), "handshake"))
        for error, kind in cases:
            network_health.reset(clock=self.clock)
            self.run_plan(error)
            self.assertEqual(self.failures(), [("planner", False, kind)], kind)

    def test_a_call_that_outlives_its_time_limit_is_a_timeout(self):
        def slow(request):
            time.sleep(0.6)
            return book_reply()
        server = Server(slow)
        backend = sarvam.SarvamBackend(api_key=KEY, transport=httpx.MockTransport(server), timeout=0.25)
        with self.assertRaises(sarvam.SarvamError):
            plan(backend)
        self.assertEqual(self.failures(), [("planner", False, "timeout")])

    def test_an_answer_is_ok_and_a_failure_is_not_recorded_for_it(self):
        self.run_plan(book_reply())
        self.assertEqual(self.events(), [("planner", True, None)])

    def test_http_errors_are_not_network_failures(self):
        for status in (401, 403, 404, 429, 500, 503):
            network_health.reset(clock=self.clock)
            self.run_plan(httpx.Response(status, json={"error": "x"}))
            self.assertEqual(self.failures(), [], status)
            self.assertEqual(network_health.status()["state"], GOOD, status)

    def test_an_answer_without_a_tool_call_or_a_bad_reply_is_not_a_network_failure(self):
        for answer in (reply(None), httpx.Response(200, text="not json"), httpx.Response(200, json=["x"])):
            network_health.reset(clock=self.clock)
            self.run_plan(answer)
            self.assertEqual(self.failures(), [])

    def test_an_open_circuit_breaker_sends_nothing_and_records_nothing(self):
        breaker = sarvam.CircuitBreaker(failures=1, pause=30, clock=self.clock)
        breaker.record_failure()
        server = self.run_plan(book_reply(), breaker=breaker)
        self.assertEqual((server.calls, self.events()), (0, []))

    def test_a_missing_key_records_nothing(self):
        server = Server(book_reply())
        backend = sarvam.SarvamBackend(api_key="", transport=httpx.MockTransport(server))
        with mock.patch.dict(os.environ, {"SARVAM_API_KEY": ""}):
            with self.assertRaises(sarvam.SarvamError):
                plan(backend)
        self.assertEqual(self.events(), [])

    def test_repeated_timeouts_make_the_state_slow_then_down(self):
        for _ in range(2):
            self.run_plan(httpx.ReadTimeout("slow"))
        self.assertEqual(network_health.status()["state"], SLOW)
        self.run_plan(httpx.ReadTimeout("slow"))
        self.assertEqual(network_health.status()["state"], DOWN)


class WhatsappHookTests(SharedMonitorCase):
    ENV = {"WHATSAPP_ACCESS_TOKEN": "test-token", "WHATSAPP_PHONE_NUMBER_ID": "123"}

    def send(self, **patch):
        with mock.patch.dict(os.environ, self.ENV), mock.patch("clinic.whatsapp.httpx.post", **patch):
            return whatsapp.send_message("919876543210", "hello")

    def response(self, status, body=None):
        return httpx.Response(status, json=body or {}, request=httpx.Request("POST", "https://example.invalid/m"))

    def test_a_connection_error_or_timeout_is_recorded(self):
        for error, kind in ((httpx.ConnectError("x"), "connect"), (httpx.ReadTimeout("x"), "timeout"),
                            (httpx.ConnectTimeout("x"), "connect"), (httpx.RemoteProtocolError("x"), "dropped")):
            network_health.reset(clock=self.clock)
            with self.assertRaises(type(error)):
                self.send(side_effect=error)
            self.assertEqual(self.events(), [("whatsapp", False, kind)])

    def test_a_good_send_records_ok(self):
        self.assertEqual(self.send(return_value=self.response(200, {"messages": []})), {"messages": []})
        self.assertEqual(self.events(), [("whatsapp", True, None)])

    def test_an_http_error_is_raised_but_is_not_a_network_failure(self):
        for status in (400, 401, 429, 500):
            network_health.reset(clock=self.clock)
            with self.assertRaises(httpx.HTTPStatusError):
                self.send(return_value=self.response(status))
            self.assertEqual([e for e in self.events() if not e[1]], [], status)

    def test_a_bug_is_not_a_network_failure(self):
        with self.assertRaises(RuntimeError):
            self.send(side_effect=RuntimeError("bug"))
        self.assertEqual(self.events(), [])


# ---- the incident table ---------------------------------------------------------------------------------------------

class IncidentTableTests(SharedMonitorCase):
    def setUp(self):
        super().setUp()
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def add(self, when=NOON, service="planner", kind="timeout", ms=5000):
        network_health.write_incident(self.conn, {"t": when, "service": service, "kind": kind, "ms": ms})

    def test_a_failure_is_stored_with_plain_wording_and_its_length(self):
        self.add(NOON, "voice", "handshake", 10040)
        row = self.conn.execute("SELECT * FROM network_events").fetchone()
        self.assertEqual((row["service"], row["kind"], row["detail"], row["duration_ms"]),
                         ("voice", "handshake", "Secure connection timed out", 10040))
        self.assertEqual(row["ts"], "2026-10-08 06:30:00")                  # UTC text, like every other table

    def test_the_four_kinds_have_their_fixed_wording(self):
        self.assertEqual(network_health.WHAT, {"timeout": "Did not answer in time", "handshake": "Secure connection timed out",
                                               "connect": "Could not connect", "dropped": "Connection dropped"})

    def test_an_unknown_kind_is_stored_as_unknown_not_as_text(self):
        self.add(kind="java.net.SocketTimeoutException: https://api.sarvam.ai")
        row = self.conn.execute("SELECT kind, detail FROM network_events").fetchone()
        self.assertEqual((row["kind"], row["detail"]), (None, None))
        shown = network_health.incidents(self.conn, now=NOON)["rows"][0]
        self.assertEqual(shown["what"], "Connection problem")

    def test_only_the_newest_500_rows_are_kept(self):
        for i in range(505):
            self.add(NOON + i)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM network_events").fetchone()[0], 500)
        oldest = self.conn.execute("SELECT MIN(ts) FROM network_events").fetchone()[0]
        self.assertEqual(oldest, network_health._utc_text(NOON + 5))

    def test_nothing_older_than_30_days_is_kept(self):
        self.add(NOON - 31 * 86400)
        self.add(NOON - 29 * 86400)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM network_events").fetchone()[0], 2)
        self.add(NOON)                                                      # the write that prunes
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM network_events").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM network_events WHERE ts < ?",
                                           (network_health._utc_text(NOON - 30 * 86400),)).fetchone()[0], 0)

    def test_the_sink_writes_through_its_own_connection(self):
        with mock.patch("clinic.network_health.write_incident") as write:
            sink = network_health.make_db_sink(lambda: self.conn)
            sink({"t": NOON, "service": "voice", "kind": "connect", "ms": 1})
        self.assertEqual(write.call_count, 1)

    def test_a_broken_database_never_reaches_the_caller(self):
        def refuse():
            raise sqlite3.OperationalError("database is locked")
        network_health.make_db_sink(refuse)({"t": NOON, "service": "voice", "kind": "connect", "ms": 1})
        network_health.make_db_sink(lambda: self.conn)({"service": "voice"})        # a malformed event

    def test_incidents_lists_newest_first_in_ist_with_a_count_for_today(self):
        yesterday = datetime(2026, 10, 7, 23, 30, tzinfo=IST).timestamp()
        today_early = datetime(2026, 10, 8, 0, 30, tzinfo=IST).timestamp()
        self.add(yesterday, "whatsapp", "connect", 15000)
        self.add(today_early, "voice", "handshake", 10000)
        self.add(NOON, "planner", "timeout", 5012)
        data = network_health.incidents(self.conn, now=NOON)
        self.assertEqual((data["today_count"], data["state"]), (2, GOOD))
        self.assertEqual([(r["service"], r["what"], r["length"]) for r in data["rows"]], [
            ("Understanding commands", "Did not answer in time", "5.0 s"),
            ("Voice (microphone)", "Secure connection timed out", "10.0 s"),
            ("WhatsApp messages", "Could not connect", "15.0 s")])
        self.assertEqual(data["rows"][0]["when"], "2026-10-08 12:00:00")
        self.assertEqual(data["rows"][2]["when"], "2026-10-07 23:30:00")

    def test_only_the_last_50_are_shown(self):
        for i in range(60):
            self.add(NOON + i)
        data = network_health.incidents(self.conn, now=NOON + 100)
        self.assertEqual(len(data["rows"]), 50)
        self.assertEqual(data["today_count"], 60)

    def test_a_length_that_is_unknown_shows_a_dash(self):
        self.add(ms=None)
        self.assertEqual(network_health.incidents(self.conn, now=NOON)["rows"][0]["length"], "-")

    def test_nothing_in_a_row_names_a_host_a_url_or_an_error_text(self):
        for error in (httpx.ConnectError("https://api.sarvam.ai/v1/chat key=sk_SECRET"),
                      socket.timeout("graph.facebook.com:443 timed out"), httpx.ReadTimeout("api-subscription-key")):
            kind = network_health.classify(error)
            self.add(kind=kind)
        text = json.dumps(network_health.incidents(self.conn, now=NOON)) + json.dumps(
            [list(r) for r in self.conn.execute("SELECT * FROM network_events")])
        for forbidden in ("sarvam", "facebook", "http", "sk_", "subscription", "timed out", ":443"):
            self.assertNotIn(forbidden, text.lower())

    def test_a_missing_table_gives_an_empty_answer(self):
        bare = sqlite3.connect(":memory:")
        bare.row_factory = sqlite3.Row
        self.assertEqual(network_health.incidents(bare, now=NOON)["rows"], [])


# ---- the idle check -------------------------------------------------------------------------------------------------

class FakeSock:
    def __init__(self):
        self.closed = 0
        self.sent = []

    def close(self):
        self.closed += 1

    def sendall(self, data):          # the probe must never send anything
        self.sent.append(data)


class ProbeTests(unittest.TestCase):
    TARGETS = [network_health.ProbeTarget("sarvam", "sarvam.test", 443, "planner", ("voice", "planner")),
               network_health.ProbeTarget("meta", "meta.test", 443, "whatsapp", ("whatsapp",))]

    def run_probe(self, outcomes):
        """`outcomes`: per target, seconds the connect takes, or an exception to raise."""
        clock = FakeClock(100.0)
        socks, hosts = [], []
        outcomes = list(outcomes)

        def connect(address, timeout=None):
            hosts.append((address, timeout))
            outcome = outcomes.pop(0)
            clock.advance(outcome if isinstance(outcome, (int, float)) else 3.0)
            if isinstance(outcome, BaseException):
                raise outcome
            socks.append(FakeSock())
            return socks[-1]
        mon, _ = fresh()
        results = network_health.probe_once(self.TARGETS, connect=connect, clock=clock, target_monitor=mon)
        return mon, results, socks, hosts

    def test_a_good_probe_records_the_connect_time_and_closes_at_once(self):
        mon, results, socks, hosts = self.run_probe([0.25, 0.5])
        self.assertEqual(results, [("sarvam", True, 250), ("meta", True, 500)])
        self.assertEqual([(e.service, e.ok, e.ms, e.probe) for e in mon._events],
                         [("planner", True, 250, "sarvam"), ("whatsapp", True, 500, "meta")])
        self.assertEqual([s.closed for s in socks], [1, 1])
        self.assertEqual([s.sent for s in socks], [[], []])               # nothing is ever sent
        self.assertEqual([h[1] for h in hosts], [network_health.PROBE_TIMEOUT_S] * 2)    # a 3 s limit
        self.assertEqual(mon.status()["state"], GOOD)

    def test_a_failing_probe_is_recorded_with_a_plain_kind(self):
        mon, results, socks, _ = self.run_probe([socket.timeout("timed out"), ConnectionRefusedError()])
        self.assertEqual([r[1] for r in results], [False, False])
        self.assertEqual([(e.service, e.ok, e.kind) for e in mon._events],
                         [("planner", False, "connect"), ("whatsapp", False, "connect")])
        self.assertEqual(socks, [])

    def test_a_slow_connect_is_recorded_as_slow(self):
        mon, results, _, _ = self.run_probe([2.5, 0.125])
        self.assertTrue(list(mon._events)[0].slow)
        self.assertEqual(mon.status()["state"], SLOW)

    def test_the_probe_hosts_follow_the_configuration(self):
        env = {"SARVAM_API_KEY": "", "WHATSAPP_NOTIFY_MODE": "dry_run", "WHATSAPP_ACCESS_TOKEN": "", "CLINIC_DB_PATH": ""}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(network_health.probe_targets(), [])           # dry run and no key: nothing to check
        with mock.patch.dict(os.environ, dict(env, SARVAM_API_KEY="k")):
            self.assertEqual([t.name for t in network_health.probe_targets()], ["sarvam"])
        with mock.patch.dict(os.environ, dict(env, WHATSAPP_NOTIFY_MODE="dry_run", WHATSAPP_ACCESS_TOKEN="t")):
            self.assertEqual(network_health.probe_targets(), [])           # dry_run: the Meta probe is skipped
        live = dict(env, SARVAM_API_KEY="k", WHATSAPP_NOTIFY_MODE="live", WHATSAPP_ACCESS_TOKEN="t")
        with mock.patch.dict(os.environ, live):
            targets = network_health.probe_targets()
        self.assertEqual([(t.host, t.port) for t in targets], [("api.sarvam.ai", 443), ("graph.facebook.com", 443)])

    def test_the_switch_is_off_in_tests(self):
        self.assertEqual(os.environ["NETWORK_PROBE_ENABLED"], "0")
        self.assertFalse(network_health.probe_enabled())
        with mock.patch("clinic.network_health.socket.create_connection",
                        side_effect=AssertionError("a real connection was opened")):
            self.assertIsNone(network_health.start_probe())

    def test_an_injected_probe_runs_in_its_own_thread_one_at_a_time(self):
        started, release, calls = threading.Event(), threading.Event(), []

        def probe():
            calls.append(threading.current_thread().name)
            started.set()
            release.wait(5)
        thread = network_health.start_probe(probe)
        self.assertTrue(started.wait(2))
        self.assertIsNot(thread, threading.current_thread())
        self.assertIsNone(network_health.start_probe(probe))               # one is still running
        release.set()
        thread.join(2)
        self.assertEqual(calls, ["network-probe"])
        again = network_health.start_probe(lambda: None)                  # free again
        again.join(2)

    def test_a_probe_that_raises_is_swallowed_and_frees_the_slot(self):
        thread = network_health.start_probe(mock.Mock(side_effect=RuntimeError("boom")))
        thread.join(2)
        again = network_health.start_probe(lambda: None)
        self.assertIsNotNone(again)
        again.join(2)


class TickTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def test_the_tick_does_not_wait_for_a_slow_probe(self):
        started, release = threading.Event(), threading.Event()

        def slow_probe():
            started.set()
            release.wait(10)
        began = time.monotonic()
        summary = scheduler.tick(self.conn, dry_run=True, network_probe=slow_probe)
        took = time.monotonic() - began
        self.assertTrue(started.wait(2))
        self.assertLess(took, 2.0)                                         # the probe is still blocked; the tick is done
        self.assertEqual(summary["errors"], 0)
        release.set()
        self.assertTrue(wait_for(lambda: not [t for t in threading.enumerate() if t.name == "network-probe"]))

    def test_a_failing_probe_does_not_break_the_tick(self):
        summary = scheduler.tick(self.conn, dry_run=True, network_probe=mock.Mock(side_effect=RuntimeError("boom")))
        self.assertEqual(summary["errors"], 0)
        self.assertTrue(wait_for(lambda: not [t for t in threading.enumerate() if t.name == "network-probe"]))

    def test_with_the_switch_off_the_tick_opens_no_connection(self):
        with mock.patch("clinic.network_health.socket.create_connection",
                        side_effect=AssertionError("a real connection was opened")):
            summary = scheduler.tick(self.conn, dry_run=True)
        self.assertEqual(summary["errors"], 0)
        self.assertEqual([t for t in threading.enumerate() if t.name == "network-probe"], [])

    def test_the_tick_re_evaluates_the_state(self):
        with mock.patch.object(network_health, "refresh") as refresh:
            scheduler.tick(self.conn, dry_run=True)
        refresh.assert_called_once_with()


# ---- the routes and the wiring in app.py ---------------------------------------------------------------------------------

class NetworkRouteTests(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        network_health.reset(clock=self.clock)
        self.addCleanup(network_health.reset)

    def test_status_when_all_is_well(self):
        data = self.client.get("/network/status").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual((data["state"], data["label"], data["last_checked"]), (GOOD, "Connection good", None))
        self.assertEqual([(r["name"], r["label"]) for r in data["services"]],
                         [("Voice (microphone)", "OK"), ("Understanding commands", "OK"), ("WhatsApp messages", "OK")])

    def test_status_follows_what_was_recorded(self):
        network_health.record("voice", False, 10000, "handshake")
        network_health.record("planner", False, 5000, "timeout")
        network_health.record("whatsapp", False, 15000, "connect")
        data = self.client.get("/network/status").get_json()
        self.assertEqual((data["state"], data["label"]), (DOWN, "No connection"))
        self.assertEqual(data["last_checked"], "12:00")
        self.assertEqual([r["label"] for r in data["services"]], ["Slow", "Slow", "Slow"])

    def test_status_carries_no_secrets_or_hosts(self):
        network_health.record("planner", False, 5000, "timeout")
        text = self.client.get("/network/status").get_data(as_text=True).lower()
        for forbidden in ("sarvam.ai", "facebook", "http", "token", "api_key", "test-not-real"):
            self.assertNotIn(forbidden, text)

    def test_incidents_route(self):
        empty = self.client.get("/network/incidents").get_json()
        self.assertEqual((empty["ok"], empty["today_count"], empty["rows"], empty["state"]), (True, 0, [], GOOD))
        network_health.write_incident(self.conn, {"t": NOON - 60, "service": "planner", "kind": "timeout", "ms": 5003})
        network_health.write_incident(self.conn, {"t": NOON, "service": "voice", "kind": "connect", "ms": 10001})
        data = self.client.get("/network/incidents").get_json()
        self.assertEqual(data["today_count"], 2)
        self.assertEqual([(r["when"], r["service"], r["what"], r["length"]) for r in data["rows"]], [
            ("2026-10-08 12:00:00", "Voice (microphone)", "Could not connect", "10.0 s"),
            ("2026-10-08 11:59:00", "Understanding commands", "Did not answer in time", "5.0 s")])

    def test_importing_the_app_wires_nothing(self):
        self.assertIsNone(network_health.monitor().sink)
        self.assertIsNone(network_health.monitor().emit)

    def test_wiring_stores_failures_and_broadcasts_changes(self):
        with mock.patch.object(clinic_app.socketio, "emit") as emit:
            clinic_app.wire_network_health()
            network_health.record("planner", False, 5000, "timeout")        # one failure: the planner row changes
            network_health.record("planner", False, 5000, "timeout")        # two: the state changes
            self.assertTrue(wait_for(lambda: self.count("network_events") == 2))
        self.assertEqual([call.args[0] for call in emit.call_args_list], ["network_status"] * 2)
        self.assertEqual(emit.call_args_list[-1].args[1]["state"], SLOW)
        row = self.conn.execute("SELECT service, kind, detail FROM network_events").fetchone()
        self.assertEqual(tuple(row), ("planner", "timeout", "Did not answer in time"))


# ---- the page --------------------------------------------------------------------------------------------------------

def read(*parts):
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


class TemplateWiringTests(unittest.TestCase):
    def setUp(self):
        self.html = read("templates", "dashboard.html")

    def test_one_chip_sits_right_under_the_date_in_the_sidebar_header(self):
        start = self.html.index('<div class="side-nav-header">')
        header = self.html[start:self.html.index("</nav>", start)]
        self.assertLess(header.index('<div class="date">'), header.index('id="net-chip"'))
        self.assertEqual(self.html.count('id="net-chip"'), 1)
        self.assertIn('id="net-chip-label">Connection good<', self.html)
        for needle in ('id="net-popover"', 'id="net-pop-list"', 'id="net-pop-checked"', 'id="net-pop-history"', "View history"):
            self.assertIn(needle, header)
        self.assertRegex(self.html, r'<button type="button" class="net-chip net-good" id="net-chip" aria-haspopup="true" aria-expanded="false"')

    def test_no_banner_and_nothing_added_to_answers(self):
        self.assertNotRegex(self.html.lower(), r'(class|id)="[^"]*banner')
        for name in ("review_card.js", "read_format.js"):
            text = read("static", name).lower()
            for word in ("connection", "network", "basic rules"):
                self.assertNotIn(word, text, name)
        voice = read("static", "live_voice.js").lower()
        self.assertEqual(voice.count("network"), 1)               # only the comment naming network_chip.js
        self.assertNotIn("basic rules", voice)

    def test_the_scripts_are_included_after_the_script_they_depend_on(self):
        order = re.findall(r"filename='([\w.]+\.js)'", self.html)
        self.assertLess(order.index("patients_subtabs.js"), order.index("audit_subtabs.js"))
        self.assertLess(order.index("live_voice.js"), order.index("network_chip.js"))         # shares its socket
        self.assertLess(order.index("network_chip.js"), order.index("network_history.js"))
        self.assertLess(order.index("network_history.js"), order.index("nav.js"))

    def test_live_voice_shares_its_one_socket(self):
        self.assertEqual(read("static", "live_voice.js").count("io()"), 1)
        self.assertIn("window.clinicSocket = socket;", read("static", "live_voice.js"))
        self.assertNotIn("io(", read("static", "network_chip.js").replace("clinicSocket", ""))   # no second connection

    def audit_section(self):
        start = self.html.index('<section class="tab-panel" data-tab="audit"')
        return self.html[start:self.html.index("</section>", start)]

    def test_the_audit_tab_has_three_sub_tabs_in_order(self):
        section = self.audit_section()
        links = re.findall(r'data-subtab-link="(\w+)" aria-selected="\w+">([^<]+)</button>', section)
        self.assertEqual(links, [("log", "Audit log"), ("unanswered", "Unanswered questions"), ("connection", "Connection")])
        self.assertIn('class="subtabs" id="audit-subtabs"', section)
        parts = re.split(r'<div class="subtab-panel" data-subtab="(\w+)"', section)
        panels = {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}
        self.assertEqual(list(panels), ["log", "unanswered", "connection"])

    def test_the_two_old_cards_keep_their_ids_in_their_own_sub_tab(self):
        section = self.audit_section()
        parts = re.split(r'<div class="subtab-panel" data-subtab="(\w+)"', section)
        panels = {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}
        self.assertIn('id="audit-table-card"', panels["log"])
        self.assertIn("<th>When (IST)</th><th>Intent</th><th>Entity</th><th>Details</th>", panels["log"])    # unchanged
        self.assertIn('id="unanswered-card"', panels["unanswered"])
        self.assertIn('id="network-card"', panels["connection"])
        self.assertIn("Connection problems", panels["connection"])
        self.assertIn("hidden", panels["unanswered"].split(">")[0])
        self.assertEqual(self.html.count('id="audit-table-card"'), 1)
        self.assertEqual(self.html.count('id="unanswered-card"'), 1)

    def test_the_scripts_that_use_the_old_cards_still_find_them_by_id(self):
        self.assertIn('copyContent(doc, "audit-table-card")', read("static", "dashboard_refresh.js"))
        self.assertIn('getElementById("unanswered-card")', read("static", "unanswered.js"))
        self.assertIn('id: "audit"', read("static", "nav.js"))

    def test_the_sub_tab_script_follows_the_patients_pattern(self):
        script = read("static", "audit_subtabs.js")
        for needle in ('"audit-subtabs"', "clinic.auditSubtab", "pickSubtab", "subtabNeighbour", "auditsubtabchange",
                       "window.AuditSubtabs", "ArrowRight"):
            self.assertIn(needle, script)

    def test_the_chip_uses_text_only_and_the_design_tokens(self):
        script = read("static", "network_chip.js") + read("static", "network_history.js")
        self.assertNotIn("innerHTML", script)
        css = read("static", "style.css")
        block = css[css.index("/* connection chip"):]
        for token in ("--ok-soft", "--ok-line", "--ok-text", "--warn-bg", "--warn-border", "--warn-text",
                      "--danger-soft", "--danger-line", "--danger-dark"):
            self.assertIn(token, block)
        self.assertNotRegex(self.html[self.html.index('id="net-chip-wrap"'):self.html.index('id="branch-switcher-box"')],
                            r"[\U0001F300-\U0001FAFF]")                                                         # no emoji in the chip

    def test_node_tests_exist_and_the_env_switch_is_documented(self):
        self.assertTrue((ROOT / "tests" / "network_chip.test.js").exists())
        self.assertIn("NETWORK_PROBE_ENABLED", read(".env.example"))
        self.assertIn('os.environ["NETWORK_PROBE_ENABLED"] = "0"', read("tests", "__init__.py"))


class NodeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_pure_helpers(self):
        out = subprocess.run(["node", str(ROOT / "tests" / "network_chip.test.js")], capture_output=True, text=True,
                             cwd=str(ROOT), timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("passed", out.stdout)


if __name__ == "__main__":
    unittest.main()
