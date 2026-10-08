"""Every Save button confirms with a bare check mark (static/save_tick.js), never with text."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(*parts):
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


class SaveTickWiringTests(unittest.TestCase):
    def test_the_helper_is_loaded_before_every_script_that_uses_it(self):
        html = read("templates", "dashboard.html")
        order = re.findall(r"filename='([\w.]+\.js)'", html)
        self.assertIn("save_tick.js", order)
        for user in ("automation.js", "followups.js", "settings_branches.js", "settings_followups.js"):
            self.assertLess(order.index("save_tick.js"), order.index(user), user)

    def test_the_tick_is_only_a_check_mark(self):
        js = read("static", "save_tick.js")
        self.assertIn('"\\u2713"', js)
        texts = re.findall(r'textContent = ("[^"]*")', js)
        self.assertEqual(['"\\u2713"'], texts)                         # the only text it ever sets is the check mark
        self.assertIn("aria-label", js)                              # screen readers still hear "Saved"

    def test_each_save_button_uses_it(self):
        expected = {
            "automation.js": "SaveTick.show(capSave)",
            "followups.js": "SaveTick.show(save)",
            "settings_followups.js": "SaveTick.show(tSave)",
            "settings_branches.js": "SaveTick.show(editButton)",
        }
        for name, call in expected.items():
            self.assertIn(call, read("static", name), name)
        self.assertIn("SaveTick.show(save)", read("static", "settings_followups.js"))      # the timing card
        self.assertIn("SaveTick.show(renameButton)", read("static", "settings_branches.js"))

    def test_no_save_still_confirms_with_words(self):
        old_texts = {
            "automation.js": ["Daily cap saved"],
            "followups.js": ['note.textContent = "Saved"'],
            "settings_followups.js": ["Timing saved", "marked approved", "Saved at "],
            "settings_branches.js": ['"Saved."', '"Saved " + value'],
        }
        for name, texts in old_texts.items():
            source = read("static", name)
            for text in texts:
                self.assertNotIn(text, source, "{}: {}".format(name, text))

    def test_failures_still_say_why(self):
        self.assertIn('say(r.error || "Could not save.", false)', read("static", "settings_followups.js"))
        self.assertIn("showTemplateError", read("static", "settings_followups.js"))
        self.assertIn('note.textContent = r.error || "Could not save."', read("static", "followups.js"))
        self.assertIn("showFlash(err.message, false)", read("static", "automation.js"))

    def test_the_style_exists_and_respects_reduced_motion(self):
        css = read("static", "style.css")
        self.assertIn(".save-tick", css)
        self.assertIn("prefers-reduced-motion", css)


if __name__ == "__main__":
    unittest.main()
