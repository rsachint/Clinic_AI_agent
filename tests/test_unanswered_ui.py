"""The Audit-tab card ("Questions I couldn't answer"), the one-time Assistant notice and the read-table
formatting: wiring checks that read the files (mirrors tests/test_followup_ui_wiring.py and test_save_tick.py),
plus the node test for the pure helpers. There is no live click-through test of these pages."""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(*parts):
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


class Wiring(unittest.TestCase):
    def setUp(self):
        self.html = read("templates", "dashboard.html")
        self.card = read("static", "unanswered.js")
        self.notice = read("static", "unanswered_notice.js")

    def test_the_card_lives_in_the_audit_tab_and_the_notice_box_in_the_assistant_tab(self):
        audit = self.html[self.html.index('<section class="tab-panel" data-tab="audit"'):]
        audit = audit[:audit.index("</section>")]
        self.assertIn('id="unanswered-card"', audit)
        self.assertIn('id="audit-table-card"', audit)                       # the old audit table is still there
        assistant = self.html[self.html.index('<section class="tab-panel" data-tab="assistant"'):]
        assistant = assistant[:assistant.index("</section>")]
        self.assertIn('id="unanswered-notices"', assistant)
        self.assertIn('id="conversation-feed"', assistant)
        self.assertEqual(self.html.count('id="unanswered-card"'), 1)

    def test_no_new_top_level_tab(self):
        nav = read("static", "nav.js")
        ids = re.findall(r'\{ id: "(\w+)", label', nav)
        self.assertEqual(len(re.findall(r'<section class="tab-panel" data-tab="', self.html)), len(ids))

    def test_the_scripts_load_in_a_sensible_order(self):
        order = re.findall(r"filename='([\w.]+\.js)'", self.html)
        for name in ("unanswered.js", "unanswered_notice.js", "read_format.js"):
            self.assertIn(name, order)
        self.assertLess(order.index("save_tick.js"), order.index("unanswered.js"))
        self.assertLess(order.index("read_format.js"), order.index("live_voice.js"))
        self.assertLess(order.index("unanswered_notice.js"), order.index("nav.js"))

    def test_live_voice_only_gained_the_formatting_hook(self):
        js = read("static", "live_voice.js")
        self.assertIn("window.ReadFormat", js)
        self.assertEqual(js.count("socket.on("), len(re.findall(r"socket\.on\(", js)))
        self.assertNotIn("unanswered", js)                       # the notice hooks the page from its own file

    def test_the_card_talks_to_the_four_routes_and_has_the_four_buttons(self):
        for needle in ('"/unanswered/data"', '"/unanswered/" + item.id + "/status"', '"Mark as added"', '"Mark as building"',
                       '"Dismiss"', '"Reopen"', "SaveTick.show(again)", 'event.detail.id === "audit"'):
            self.assertIn(needle, self.card)
        self.assertIn("/unanswered/notices", self.notice)
        self.assertIn('"/notified"', self.notice)
        self.assertIn('event.detail.id === "assistant"', self.notice)
        self.assertIn("MutationObserver", self.notice)             # the next turn on an open page

    def test_mark_as_added_asks_for_a_one_line_note(self):
        self.assertIn("What can they ask now", self.card)
        self.assertIn("maxLength = 200", self.card)
        self.assertIn("Write one line about what can be asked now.", self.card)

    def test_the_dom_is_text_only(self):
        for name in ("unanswered.js", "unanswered_notice.js", "read_format.js"):
            source = read("static", name)
            self.assertNotIn("innerHTML", source, name)
            self.assertNotIn("insertAdjacentHTML", source, name)
            self.assertNotIn("outerHTML", source, name)
            self.assertNotIn("document.write", source, name)

    def test_the_notice_is_shown_once_per_item_on_the_page_and_tells_the_server(self):
        self.assertIn("shown[item.id]", self.notice)
        self.assertIn('method: "POST"', self.notice)

    def test_the_saved_tick_helper_is_used_for_the_note_save(self):
        self.assertIn("window.SaveTick", self.card)

    def test_the_styles_exist(self):
        css = read("static", "style.css")
        for selector in (".unanswered-notices", ".unanswered-notice", "#unanswered-card", "th.cell-num"):
            self.assertIn(selector, css)


class NodeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_pure_helpers(self):
        out = subprocess.run(["node", str(ROOT / "tests" / "unanswered_ui.test.js")], capture_output=True, text=True,
                             cwd=str(ROOT), timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("passed", out.stdout)


if __name__ == "__main__":
    unittest.main()
