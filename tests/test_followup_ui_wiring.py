"""The Patients tab is now four sub-tabs (Patients, Missed follow-ups, Attendance
today, Follow-ups) -- a layout change only: no new top-level tab, and every id,
route and script the old stacked page relied on is still there. Plus the wiring of
the Follow-ups sub-tab and the Settings card. (Mirrors tests/test_nav_tabs.py and
tests/test_branch_ui_wiring.py: these read the files; the behaviour is covered by
the route tests and tests/ptt_client.test.js.)"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


def read(*parts):
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


class PatientsSubtabs(unittest.TestCase):
    def setUp(self):
        self.html = read("templates", "dashboard.html")
        start = self.html.index('<section class="tab-panel" data-tab="patients"')
        self.section = self.html[start:self.html.index("</section>", start)]
        # the panels, in page order: {subtab id: its html}
        parts = re.split(r'<div class="subtab-panel" data-subtab="(\w+)"', self.section)
        self.panels = {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}

    def test_four_sub_tabs_in_the_agreed_order_with_the_agreed_names(self):
        links = re.findall(r'data-subtab-link="(\w+)" aria-selected="\w+">([^<]+)</button>', self.section)
        self.assertEqual(links, [("patients", "Patients"), ("missed", "Missed follow-ups"),
                                 ("attendance", "Attendance today"), ("followups", "Follow-ups")])
        self.assertEqual(list(self.panels), ["patients", "missed", "attendance", "followups"])

    def test_each_old_card_sits_in_its_own_sub_tab_with_its_id_unchanged(self):
        self.assertIn('id="patients-table-card"', self.panels["patients"])
        self.assertIn('id="patient-activity-card"', self.panels["patients"])      # the timeline opens under the patient list
        for needle in ('class="patient-row"', 'id="patient-activity-list"', 'id="patient-activity-title"'):
            self.assertIn(needle, self.panels["patients"])
        self.assertIn('id="missed-followups-card"', self.panels["missed"])
        self.assertIn('id="attendance-card"', self.panels["attendance"])
        for sub, other_ids in (("missed", ("patients-table-card", "attendance-card")), ("attendance", ("patients-table-card", "missed-followups-card"))):
            for other in other_ids:
                self.assertNotIn('id="{}"'.format(other), self.panels[sub])

    def test_only_the_patients_sub_tab_starts_visible(self):
        for name, html in self.panels.items():
            head = html[:html.index(">")]
            self.assertEqual("hidden" in head, name != "patients", name)

    def test_the_old_content_is_all_still_rendered(self):
        for needle in ("(showing {{ patients|length }} of {{ patients_total }})", "Click a patient to see their appointment activity.",
                       "Missed follow-ups <span", "{% for f in missed %}", "Attendance today</h2>", "{% for a in attendance %}",
                       "{{ clinical_citation.source }}"):
            self.assertIn(needle, self.section, needle)

    def test_there_is_no_new_top_level_tab(self):
        nav = read("static", "nav.js")
        ids = re.findall(r'\{ id: "(\w+)", label', nav)
        self.assertEqual(ids, ["assistant", "queue", "appointments", "automation", "patients", "messages", "connectors",
                               "audit", "settings", "help", "profile", "login"])      # "Need help" sits right after Settings
        self.assertEqual(self.html.count('data-tab="followups"'), 0)
        self.assertEqual(len(re.findall(r'<section class="tab-panel" data-tab="', self.html)), len(ids))

    def test_the_refresh_script_still_finds_every_card_it_updates(self):
        refresh = read("static", "dashboard_refresh.js")
        for card_id in re.findall(r'copyContent\(doc, "([\w-]+)"\)', refresh):
            self.assertIn('id="{}"'.format(card_id), self.html, card_id)
        self.assertIn('copyContent(doc, "fu-patient-list")', refresh)

    def test_other_scripts_that_look_up_patient_cards_are_untouched(self):
        automation = read("static", "automation.js")
        for card_id in ("patients-table-card", "patient-activity-card", "patient-activity-title", "patient-activity-list"):
            self.assertIn('"{}"'.format(card_id), automation)
            self.assertIn('id="{}"'.format(card_id), self.html)

    def test_the_chosen_sub_tab_is_remembered_safely(self):
        js = read("static", "patients_subtabs.js")
        self.assertIn('"clinic.patientsSubtab"', js)
        self.assertRegex(js, r"try \{ return window\.localStorage \? window\.localStorage\.getItem\(KEY\) : null; \} catch \(e\)")
        self.assertRegex(js, r"try \{ if \(window\.localStorage\) window\.localStorage\.setItem\(KEY, value\); \} catch \(e\)")
        self.assertIn('new CustomEvent("subtabchange"', js)
        self.assertNotIn("NAV_TABS", js)                                  # it is not a navigation tab
        self.assertIn("window.pickSubtab", js)

    def test_the_sub_tab_bar_is_accessible_and_styled_with_the_design_tokens(self):
        self.assertIn('role="tablist"', self.section)
        self.assertEqual(self.section.count('role="tab"'), 4)
        css = read("static", "style.css")
        block = css[css.index("/* ---- Patients tab sub-tabs"):]
        self.assertIn(".subtab.active", block)
        for token in ("--accent-soft", "--accent-dark", "--shadow", "--danger-soft", "--ok-soft", "--warn-bg"):
            self.assertIn(token, block)
        self.assertEqual([h for h in re.findall(r"#[0-9a-fA-F]{3,8}\b", block) if h.lower() != "#fff"], [])      # no ad-hoc colours
        self.assertIn("@media (max-width: 720px)", block)


class FollowupsSubtab(unittest.TestCase):
    def setUp(self):
        self.html = read("templates", "dashboard.html")
        self.js = read("static", "followups.js")
        self.settings_js = read("static", "settings_followups.js")
        start = self.html.index('data-subtab="followups"')
        self.panel = self.html[start:self.html.index("</section>", start)]

    def test_the_panel_has_the_batch_card_the_list_and_the_manual_fallback(self):
        for needle in ('id="followup-batch-card"', 'id="fu-rows"', 'id="fu-add-row"', 'id="fu-review-btn"', 'id="fu-review"',
                       'id="fu-error"', 'id="followup-list-card"', 'id="fu-list"', 'id="followup-manual-card"', 'id="fu-manual"',
                       'id="fu-patient-list"', 'id="followups-flash"', "Send manually", "never</strong> sent to the patient"):
            self.assertIn(needle, self.panel)

    def test_the_scripts_load_in_the_right_order(self):
        order = [self.html.index("filename='{}'".format(name)) for name in (
            "branches.js", "patients_subtabs.js", "followups.js", "settings_branches.js", "settings_followups.js", "nav.js")]
        self.assertEqual(order, sorted(order))

    def test_it_calls_every_follow_up_route(self):
        for route in ("/followups/data", "/followups/plan", "/followups/apply", "/followups/slots", "/followups/batches/",
                      "/diagnosis", "/retry", "/manual-sent"):
            self.assertIn(route, self.js)

    def test_it_follows_the_viewed_branch_and_hides_the_branch_field_for_one_branch(self):
        self.assertIn("Branches.viewQuery()", self.js)
        self.assertIn('document.addEventListener("branchchange"', self.js)
        self.assertIn('"branch-field"', self.js)
        self.assertRegex(self.js, r"label\.hidden = !multi\(\)")
        self.assertIn("if (multi())", self.js)                                # the branch is shown in the list / review only when there are several

    def test_it_reloads_when_the_sub_tab_or_tab_is_shown(self):
        self.assertIn('"subtabchange"', self.js)
        self.assertIn('"tabchange"', self.js)

    def test_data_is_inserted_as_text_never_html(self):
        for source in (self.js, self.settings_js):
            assignments = re.findall(r"\.innerHTML\s*=[^;]*;", source)
            self.assertTrue(assignments)
            self.assertEqual({a.replace(" ", "") for a in assignments}, {'.innerHTML="";'})
            self.assertNotIn("insertAdjacentHTML", source)
            self.assertNotIn("document.write", source)
            self.assertIn("textContent", source)

    def test_diagnosis_edits_say_they_are_internal(self):
        self.assertIn("Internal only, never sent to the patient", self.js)
        self.assertIn("Diagnosis (internal)", self.js)

    def test_the_manual_list_has_copy_and_mark_as_sent(self):
        self.assertIn("Copy text", self.js)
        self.assertIn("Mark as sent manually", self.js)
        self.assertIn("navigator.clipboard", self.js)

    def test_the_mobile_layout_wraps(self):
        css = read("static", "style.css")
        self.assertIn(".fu-table", css)
        self.assertIn("closure-table-wrap", self.js)                           # tables scroll sideways instead of overflowing the page
        self.assertIn(".appt-form-grid", css)


class SettingsCard(unittest.TestCase):
    def setUp(self):
        self.html = read("templates", "dashboard.html")
        self.js = read("static", "settings_followups.js")

    def test_the_settings_tab_has_a_place_for_the_card(self):
        start = self.html.index('<section class="tab-panel" data-tab="settings"')
        settings_section = self.html[start:self.html.index("</section>", start)]
        self.assertIn('id="settings-followups"', settings_section)
        self.assertIn('id="settings-flash"', settings_section)

    def test_it_talks_to_the_settings_routes_and_checks_before_saving(self):
        for route in ("/settings/followups/data", "/settings/followups", "/settings/followup-templates"):
            self.assertIn(route, self.js)
        self.assertIn("window.fuTimingProblems", self.js)

    def test_it_explains_the_templates_and_where_the_docs_are(self):
        self.assertIn("docs/meta_templates/", self.js)
        self.assertIn("Send manually", self.js)
        for label in ("Early reminder: days before", "Second reminder: hours before the visit", "Save approved templates"):
            self.assertIn(label, self.js)


if __name__ == "__main__":
    unittest.main()
