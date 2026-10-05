"""The left menu. "Patient messages" is hidden for now: no menu entry, but its panel,
scripts and routes are all still there so it comes back by deleting one flag."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class NavTabs(unittest.TestCase):
    def setUp(self):
        self.nav = (ROOT / "static" / "nav.js").read_text()
        self.html = (ROOT / "templates" / "dashboard.html").read_text()

    def test_patient_messages_is_out_of_the_menu_only(self):
        self.assertRegex(self.nav, r'id: "messages", label: "Patient messages", icon: "[^"]+", hidden: true')
        self.assertIn("if (tab.hidden) return;", self.nav)
        # nothing else is hidden
        entries = re.findall(r'\{ id: "\w+", label: [^}]*\}', self.nav)
        self.assertEqual([e for e in entries if "hidden: true" in e], [e for e in entries if "messages" in e])

    def test_its_panel_and_scripts_are_untouched(self):
        self.assertIn('data-tab="messages"', self.html)
        self.assertIn('id="wa-inbox-list"', self.html)
        self.assertIn('id="wa-threads-list"', self.html)
        for script in ("wa_inbox.js", "wa_threads.js"):
            self.assertIn(script, self.html)

    def test_every_other_tab_still_has_a_menu_entry_and_a_panel(self):
        ids = re.findall(r'\{ id: "(\w+)", label', self.nav)
        for tab in ids:
            self.assertIn('data-tab="%s"' % tab, self.html, tab)
        self.assertGreaterEqual(len(ids), 10)


if __name__ == "__main__":
    unittest.main()
