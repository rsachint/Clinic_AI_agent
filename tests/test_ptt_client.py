"""Runs the node test for the hold-to-talk client logic (static/live_voice.js)
inside the normal unittest suite. Skipped when `node` is not installed."""

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PttClientNodeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_key_decision_and_hold_state_machine(self):
        out = subprocess.run(["node", str(ROOT / "tests" / "ptt_client.test.js")],
                             capture_output=True, text=True, cwd=str(ROOT), timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("passed", out.stdout)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_every_static_script_parses(self):
        for script in sorted((ROOT / "static").glob("*.js")):
            out = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, "%s: %s" % (script.name, out.stderr))


class PttFrontEndWiringTests(unittest.TestCase):
    """The page keeps the mic closed until a key is held."""

    def setUp(self):
        self.js = (ROOT / "static" / "live_voice.js").read_text()
        self.html = (ROOT / "templates" / "dashboard.html").read_text()

    def test_mic_is_only_requested_inside_the_press_handler_path(self):
        # exactly one getUserMedia call, and it lives in startCapture (called from onStart)
        self.assertEqual(self.js.count("getUserMedia("), 1)
        start = self.js.index("function startCapture")
        self.assertLess(start, self.js.index("getUserMedia("))
        self.assertIn("startCapture(listen)", self.js)
        # not requested on socket connect / page load
        connect_handler = self.js[self.js.index('socket.on("connect"'):self.js.index('socket.on("disconnect"')]
        self.assertNotIn("startCapture", connect_handler)
        self.assertNotIn("getUserMedia", connect_handler)

    def test_release_stops_every_track(self):
        self.assertIn("track.stop()", self.js)
        self.assertIn("ctx.close()", self.js)

    def test_mute_button_is_gone(self):
        self.assertNotIn("call-mic-toggle", self.html)
        self.assertNotIn("Mute mic", self.html)
        self.assertNotIn("Mute mic", self.js)

    def test_protocol_events_used(self):
        for name in ("listen_start", "listen_stop", "listen_cancel", "session_ready", "audio_chunk"):
            self.assertIn(name, self.js)

    def test_assistant_screen_shows_only_the_mic_and_its_one_line(self):
        # No explanatory paragraph, no fn-key note, no empty-state sentence.
        for gone in ("call-hint", "fn / Globe", "No other key", "conversation-empty", "Nothing yet"):
            self.assertNotIn(gone, self.html)
        self.assertIn('id="call-orb"', self.html)
        self.assertIn('id="call-status"', self.html)
        self.assertIn("Hold 'Enter' or 'F1' key to speak and release to end", self.js)

    def test_stat_cards_moved_to_the_queue_tab(self):
        queue = self.html.index('data-tab="queue"')
        assistant = self.html.index('data-tab="assistant"')
        self.assertGreater(self.html.index('id="stat-row"'), queue)
        self.assertLess(assistant, queue)
        assistant_section = self.html[assistant:queue]
        self.assertNotIn("stat-row", assistant_section)
        self.assertNotIn("citation-line", assistant_section)

    def test_mic_panel_does_not_scroll_with_the_conversation(self):
        css = (Path(__file__).resolve().parents[1] / "static" / "style.css").read_text()
        self.assertIn("flex: 0 0 auto", css[css.index(".call-stage {"):css.index(".call-orb {")])
        feed = css[css.index(".conversation-feed {"):css.index(".conversation-empty")]
        self.assertIn("overflow-y: auto", feed)
        self.assertNotIn("max-height", feed)
        self.assertIn('.tab-panel[data-tab="assistant"]:not([hidden])', css)


if __name__ == "__main__":
    unittest.main()
