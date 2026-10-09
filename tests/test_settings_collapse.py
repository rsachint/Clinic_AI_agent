"""Settings: every configuration card collapses under its heading (static/settings_collapse.js). The behaviour is
the node test; this reads the wiring from the files."""

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SettingsCollapseTests(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "templates" / "dashboard.html").read_text()
        self.css = (ROOT / "static" / "style.css").read_text()
        self.js = (ROOT / "static" / "settings_collapse.js").read_text()

    def test_the_node_helpers_pass(self):
        result = subprocess.run(["node", str(ROOT / "tests" / "settings_collapse.test.js")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_script_is_loaded_after_the_settings_scripts(self):
        self.assertIn("settings_collapse.js", self.html)
        for earlier in ("settings_branches.js", "settings_followups.js", "settings_sarvam.js", "settings_architecture.js"):
            self.assertLess(self.html.index(earlier), self.html.index("settings_collapse.js"))

    def test_it_only_touches_the_settings_tab(self):
        self.assertIn('.tab-panel[data-tab="settings"]', self.js)

    def test_it_never_inserts_html(self):
        self.assertNotIn("innerHTML", self.js)

    def test_the_styles_exist(self):
        for selector in (".card.collapsible", ".card-chevron", ".settings-collapse-bar", ".card.collapsible.collapsed"):
            self.assertIn(selector, self.css)

    def test_cards_the_other_scripts_draw_later_are_picked_up(self):
        self.assertIn("MutationObserver", self.js)
        self.assertIn("aria-expanded", self.js)


if __name__ == "__main__":
    unittest.main()
