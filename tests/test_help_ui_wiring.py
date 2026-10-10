"""The "Need help" tab's wiring, read from the files (there is no live click-through test of these pages), plus the
node test for the pure helpers (tests/help.test.js) and a render of the real page on a temp database."""
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_app_routes import RouteTestCase  # noqa: E402  (stubs load_dotenv, never reads .env)

ROOT = Path(__file__).resolve().parents[1]
NEW_JS = ("help.js", "help_subtabs.js", "help_notice.js", "settings_help.js")


def read(*parts):
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


class Nav(unittest.TestCase):
    def test_need_help_is_right_after_settings_and_before_profile(self):
        ids = re.findall(r'\{ id: "(\w+)", label: "([^"]*)", icon: "([^"]+)"', read("static", "nav.js"))
        order = [i[0] for i in ids]
        self.assertEqual(order[order.index("settings") + 1], "help")
        self.assertEqual(order[order.index("settings") + 2], "profile")
        entry = [i for i in ids if i[0] == "help"][0]
        self.assertEqual(entry[1], "Need help")
        self.assertTrue(entry[2])

    def test_it_has_a_panel_and_the_three_sub_tabs(self):
        html = read("templates", "dashboard.html")
        self.assertEqual(html.count('data-tab="help"'), 1)
        panel = html[html.index('<section class="tab-panel" data-tab="help"'):]
        panel = panel[:panel.index("</section>")]
        for link in ("report", "mine", "team"):
            self.assertIn('data-subtab-link="%s"' % link, panel)
        for label in ("Report an issue", "My requests", "Team view"):
            self.assertIn(label, panel)
        for node in ('id="help-report"', 'id="help-mine"', 'id="help-team"', 'id="help-subtabs"'):
            self.assertIn(node, panel)
        self.assertLess(html.index('data-tab="settings"'), html.index('data-tab="help"'))
        self.assertLess(html.index('data-tab="help"'), html.index('data-tab="profile"'))

    def test_panels_and_menu_entries_still_match(self):
        ids = re.findall(r'\{ id: "(\w+)", label', read("static", "nav.js"))
        html = read("templates", "dashboard.html")
        self.assertEqual(len(re.findall(r'<section class="tab-panel" data-tab="', html)), len(ids))

    def test_the_settings_card_and_the_notice_box_have_a_home(self):
        html = read("templates", "dashboard.html")
        settings = html[html.index('<section class="tab-panel" data-tab="settings"'):]
        settings = settings[:settings.index("</section>")]
        self.assertIn('id="settings-help"', settings)
        self.assertIn('id="help-notices"', html)

    def test_the_scripts_load_in_a_sensible_order(self):
        order = re.findall(r"filename='([\w.]+\.js)'", read("templates", "dashboard.html"))
        for name in NEW_JS:
            self.assertEqual(order.count(name), 1, name)
        self.assertLess(order.index("live_voice.js"), order.index("help.js"))            # help.js uses window.Dictation
        self.assertLess(order.index("patients_subtabs.js"), order.index("help_subtabs.js"))
        self.assertLess(order.index("help_subtabs.js"), order.index("help.js"))
        self.assertLess(order.index("save_tick.js"), order.index("help.js"))
        self.assertLess(order.index("help.js"), order.index("nav.js"))
        self.assertLess(order.index("help_notice.js"), order.index("nav.js"))
        self.assertLess(order.index("settings_help.js"), order.index("settings_collapse.js"))


class Source(unittest.TestCase):
    def test_the_node_helpers_pass(self):
        result = subprocess.run(["node", str(ROOT / "tests" / "help.test.js")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_every_new_script_parses(self):
        for name in NEW_JS + ("live_voice.js", "nav.js"):
            result = subprocess.run(["node", "--check", str(ROOT / "static" / name)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, name + result.stderr)

    def test_the_dom_is_text_only(self):
        for name in NEW_JS:
            source = read("static", name)
            for banned in ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write", "eval("):
                self.assertNotIn(banned, source, "%s uses %s" % (name, banned))

    def test_the_page_only_talks_to_the_help_and_settings_routes(self):
        for name in ("help.js", "help_notice.js", "settings_help.js"):
            for url in re.findall(r'(?:fetch|getJson|postJson)\(\s*"([^"]+)"', read("static", name)):
                self.assertTrue(url.startswith("/help/") or url == "/settings/help-sla", "%s calls %s" % (name, url))
        for name in NEW_JS:
            for forbidden in ("/approve", "/speak", "core.propose", "propose(", "review_card", "ReviewCard"):
                self.assertNotIn(forbidden, read("static", name), name)

    def test_the_draft_storage_is_always_in_try_catch(self):
        source = read("static", "help.js")
        start = source.index("function storage(")
        block = source[start:source.index("var config = null;", start)]
        self.assertIn("try {", block)
        self.assertIn("catch (e)", block)
        self.assertEqual(source.count("window.localStorage"), block.count("window.localStorage"))     # every access is inside that one function
        sub = read("static", "help_subtabs.js")
        self.assertEqual(sub.count("try {"), sub.count("catch (e)"))

    def test_the_form_follows_the_agreed_rules(self):
        source = read("static", "help.js")
        for needle in ('"/help/config"', '"/help/requests"', "FormData", 'body.append("files"', "state.submitting", "submit.disabled = true",
                       "window.Dictation.start", "window.Dictation.stop", "storage(\"remove\")", "Logged in as", '"SLA"', '"Ticket"',
                       '"Reported"', '"Attachments"', "[number hidden]", "no access control", "/help/team/export?unexported=1&mark_exported=1",
                       "/help/team/import", "Import updates", "Overdue only", "drop"):
            self.assertIn(needle, source, needle)
        self.assertNotIn('append("username"', source)                     # the browser never sends who it is
        self.assertNotRegex(source, r"username\s*:")

    def test_the_dictate_button_never_uses_the_talk_key_machinery(self):
        source = read("static", "help.js")
        for forbidden in ("pttShouldStart", "createHoldMachine", "listen_start", "audio_chunk", "keydown\", function (event) {\n        var decision"):
            self.assertNotIn(forbidden, source)

    def test_the_disclaimer_comes_first_and_says_the_agreed_things(self):
        source = read("static", "help.js")
        self.assertLess(source.index("help-disclaimer"), source.index('"Report an issue"'))
        from clinic import help_requests
        for needle in ("how the app feels to use", "You do not need to report errors", "records the technical details automatically",
                       "do not mention patient names, phone numbers or any medical details"):
            self.assertIn(needle, help_requests.DISCLAIMER.replace("\n", " "))

    def test_styles_use_the_design_tokens(self):
        css = read("static", "style.css")
        block = css[css.index("/* \"Need help\" tab"):]
        for token in ("var(--accent", "var(--ok-", "var(--danger-", "var(--warn-", "var(--border", "var(--muted)"):
            self.assertIn(token, block)
        self.assertNotRegex(block, r"#[0-9a-fA-F]{3,6}\b(?<!#fff)")        # no new colours besides white

    def test_uploads_are_git_ignored(self):
        self.assertIn("data/help_uploads/", read(".gitignore").split())


class Page(RouteTestCase):
    def test_the_real_page_renders_with_the_tab_and_the_scripts(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-tab="help"', html)
        for name in NEW_JS:
            self.assertIn(name, html)
        self.assertIn('id="settings-help"', html)


if __name__ == "__main__":
    unittest.main()
