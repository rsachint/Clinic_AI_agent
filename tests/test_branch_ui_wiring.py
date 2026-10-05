import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


class BranchUiWiringTests(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "templates" / "dashboard.html").read_text()
        self.queue = (ROOT / "templates" / "_queue_panel.html").read_text()
        self.edit = (ROOT / "static" / "queue_edit.js").read_text()
        self.refresh = (ROOT / "static" / "dashboard_refresh.js").read_text()
        self.settings = (ROOT / "static" / "settings_branches.js").read_text()
        self.automation = (ROOT / "static" / "automation.js").read_text()

    def test_branches_script_loads_before_the_scripts_that_use_it(self):
        order = [self.html.index(name) for name in ("branches.js", "dashboard_refresh.js", "queue_edit.js", "automation.js", "settings_branches.js")]
        self.assertEqual(order, sorted(order))

    def test_the_page_has_the_switcher_the_branch_pickers_and_the_data(self):
        for needle in ('id="branch-switcher"', 'id="new-appt-branch"', 'id="move-appt-branch"', 'id="block-branch"',
                       'id="block-doctor"', 'id="my-branch"', 'id="settings-branches"', 'id="branches-data"'):
            self.assertIn(needle, self.html)

    def test_the_queue_follows_the_viewed_branch(self):
        self.assertIn("Branches.viewQuery()", self.refresh)
        self.assertIn('document.addEventListener("branchchange"', self.edit)

    def test_booking_and_moving_send_the_branch(self):
        self.assertIn("branch_id:", self.edit)
        self.assertEqual(self.edit.count("&branch="), 1)           # the slots request
        self.assertIn('data-branch="{{ e.branch_id }}"', self.queue)

    def test_the_queue_shows_one_card_per_branch_with_its_name(self):
        self.assertIn("{% for group in queue_groups_or_default %}", self.queue)
        self.assertIn("queue-branch-name", self.queue)

    def test_settings_calls_every_branch_route(self):
        for route in ("/settings/branches", "/settings/default-branch", "/settings/doctors", "/settings/schedules", "/status", "/deactivate", "/remove"):
            self.assertIn(route, self.settings)

    def test_settings_inserts_text_never_html(self):
        self.assertNotIn("innerHTML =", self.settings.replace('root.innerHTML = "";', "").replace('mineSelect.innerHTML = "";', ""))

    def test_blocks_can_be_scoped(self):
        self.assertIn("branch_id:", self.automation)
        self.assertIn("doctor_id:", self.automation)


if __name__ == "__main__":
    unittest.main()
