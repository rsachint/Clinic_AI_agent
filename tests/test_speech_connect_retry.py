"""Opening the speech connection is retried once on a network error (a blip), shows plain wording when it still
fails, and never retries a refused key or a bug. A fake connection factory is used: no network."""

import socket
import ssl
import unittest
from unittest import mock

from tests.test_hold_to_talk import FakeSTT, HoldToTalkBase, wait_for
from tests.test_hold_to_talk import final_msg  # noqa: F401  (kept with the other fakes)

from clinic import realtime_voice
from clinic.realtime_voice import CONNECT_FAILED_MESSAGE, _connect_with_retry, is_network_error


class Flaky:
    """A factory that fails with `error` the first `failures` times, then connects through a FakeSTT."""

    def __init__(self, stt, failures, error):
        self.stt, self.failures, self.error, self.calls = stt, failures, error, 0

    def __call__(self, api_key):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return self.stt(api_key)


class ConnectWithRetryTests(unittest.TestCase):
    def run_connect(self, factory, **kw):
        waits = []
        with _connect_with_retry(factory, "key", wait=waits.append, **kw) as client:
            return client, waits

    def test_a_network_error_is_retried_once_and_then_connects(self):
        stt = FakeSTT()
        flaky = Flaky(stt, 1, socket.timeout("timed out"))
        client, waits = self.run_connect(flaky)
        self.assertIsNotNone(client)
        self.assertEqual((flaky.calls, len(waits)), (2, 1))

    def test_a_tls_handshake_timeout_counts_as_a_network_error(self):
        self.assertTrue(is_network_error(ssl.SSLError("_ssl.c:1112: The handshake operation timed out")))
        self.assertTrue(is_network_error(TimeoutError()))
        self.assertTrue(is_network_error(ConnectionResetError()))

    def test_it_gives_up_after_the_attempts_and_raises_the_last_error(self):
        flaky = Flaky(FakeSTT(), 5, socket.timeout("timed out"))
        with self.assertRaises(socket.timeout):
            self.run_connect(flaky)
        self.assertEqual(flaky.calls, 2)

    def test_other_errors_are_not_retried(self):
        for error in (RuntimeError("bug"), ValueError("bad key")):
            flaky = Flaky(FakeSTT(), 5, error)
            with self.assertRaises(type(error)):
                self.run_connect(flaky)
            self.assertEqual(flaky.calls, 1)

    def test_a_cancelled_listen_is_not_retried(self):
        flaky = Flaky(FakeSTT(), 5, socket.timeout("timed out"))
        with self.assertRaises(socket.timeout):
            self.run_connect(flaky, cancelled=lambda: True)
        self.assertEqual(flaky.calls, 1)

    def test_an_error_while_the_connection_is_in_use_is_not_retried(self):
        stt = FakeSTT()
        flaky = Flaky(stt, 0, None)
        with self.assertRaises(OSError):
            with _connect_with_retry(flaky, "key", wait=lambda s: None):
                raise OSError("lost mid-use")
        self.assertEqual((flaky.calls, stt.opened, stt.closed), (1, 1, 1))     # used once, then closed


class SessionTests(HoldToTalkBase):
    def test_a_blip_on_the_first_connect_is_invisible_to_the_user(self):
        stt = FakeSTT(final_on_end="kal ka calendar")
        session = self.make_session(Flaky(stt, 1, socket.timeout("timed out")))
        with mock.patch.object(realtime_voice, "_CONNECT_PAUSE_S", 0.01):
            session.listen_start(1)
            session.send_audio("x")
            session.listen_stop()
            self.assertTrue(wait_for(lambda: self.events("listen_end")))
        self.assertEqual(self.events("voice_error"), [])
        self.assertEqual(stt.opened, 1)
        self.assertEqual(len(self.events("navigate")), 1)       # the audio spoken meanwhile was not lost
        self.assertNoLeakedThreads()

    def test_a_connection_that_stays_down_shows_plain_wording_not_the_library_error(self):
        error = socket.timeout("_ssl.c:1112: The handshake operation timed out")
        session = self.make_session(Flaky(FakeSTT(), 99, error))
        with mock.patch.object(realtime_voice, "_CONNECT_PAUSE_S", 0.01):
            session.listen_start(1)
            self.assertTrue(wait_for(lambda: self.events("listen_end")))
        messages = [e["message"] for e in self.events("voice_error")]
        self.assertEqual(messages, [CONNECT_FAILED_MESSAGE])
        self.assertNotIn("_ssl", messages[0])
        self.assertEqual(self.events("listen_end")[0]["reason"], "error")
        self.assertNoLeakedThreads()


if __name__ == "__main__":
    unittest.main()
