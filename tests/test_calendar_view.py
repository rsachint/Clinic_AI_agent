import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_branch_routes import BranchRouteCase, next_monday  # noqa: E402
from clinic import branches  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class CalendarDataTests(BranchRouteCase):
    def setUp(self):
        super().setUp()
        monday = next_monday()
        self.iso = monday.isoformat()
        rows = (("Alice A", self.iso, "09:00", 1, "booked"), ("Bob B", self.iso, "10:00", 2, "booked"),
                ("Cara B", (monday + timedelta(days=1)).isoformat(), "10:30", 2, "completed"),
                ("Gone", self.iso, "09:30", 1, "cancelled"), ("Moved", self.iso, "11:00", 1, "rescheduled"),
                ("Far", (monday + timedelta(days=30)).isoformat(), "10:00", 1, "booked"))
        for name, day, time, branch, status in rows:
            self.conn.execute("INSERT INTO appointments (patient_name, patient_phone, appt_date, start_time, status, branch_id) VALUES (?, '9876500001', ?, ?, ?, ?)",
                              (name, day, time, status, branch))
        self.conn.commit()

    def data(self, branch, start=None, end=None):
        end = end or (next_monday() + timedelta(days=6)).isoformat()
        response = self.client.get("/calendar/data?start={}&end={}&branch={}".format(start or self.iso, end, branch))
        return response.status_code, response.get_json()

    def names(self, body):
        return [a["patient_name"] for a in body["appointments"]]

    def test_one_branch_only(self):
        status, body = self.data(1)
        self.assertEqual((status, self.names(body)), (200, ["Alice A"]))
        self.assertEqual(self.names(self.data(2)[1]), ["Bob B", "Cara B"])

    def test_all_branches_in_time_order_with_branch_colour_and_token(self):
        body = self.data("all")[1]
        self.assertEqual(self.names(body), ["Alice A", "Bob B", "Cara B"])
        alice, bob = body["appointments"][0], body["appointments"][1]
        self.assertEqual((alice["branch"], bob["branch"]), ("Branch A", "Branch B"))
        self.assertEqual((alice["token"], bob["token"]), ("A-T01", "B-T01"))
        self.assertEqual(alice["end_time"], "09:30")
        self.assertTrue(alice["branch_color"] and bob["branch_color"] and alice["branch_color"] != bob["branch_color"])

    def test_cancelled_and_moved_are_left_out_and_completed_is_shown(self):
        names = self.names(self.data("all")[1])
        self.assertNotIn("Gone", names)
        self.assertNotIn("Moved", names)
        self.assertIn("Cara B", names)

    def test_the_range_limits_what_comes_back(self):
        far = (next_monday() + timedelta(days=30)).isoformat()
        self.assertEqual(self.names(self.data("all", far, far)[1]), ["Far"])

    def test_no_branch_means_the_default_branch(self):
        response = self.client.get("/calendar/data?start={}&end={}".format(self.iso, self.iso))
        self.assertEqual(self.names(response.get_json()), ["Alice A"])

    def test_closures_are_returned_for_shading(self):
        self.post("/automation/blocks", {"start_date": self.iso, "end_date": self.iso, "reason": "Renovation", "branch_id": 2})
        body = self.data("all")[1]
        self.assertEqual([(b["branch"], b["reason"]) for b in body["blocks"]], [("Branch B", "Renovation")])
        self.assertEqual(self.data(1)[1]["blocks"], [])        # Branch A's own view does not show B's closure

    def test_bad_input_is_refused(self):
        for query in ("start=zzz&end=2026-10-05", "start=2026-10-05&end=2026-10-01", "start=2026-10-05&end=2027-12-31",
                      "start=2026-10-05&end=2026-10-06&branch=99", "start=2026-10-05&end=2026-10-06&branch=x"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get("/calendar/data?" + query).status_code, 400)


class CalendarPageTests(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "templates" / "dashboard.html").read_text()
        self.js = (ROOT / "static" / "calendar_view.js").read_text()

    def test_the_appointments_tab_is_the_in_app_calendar_with_no_google(self):
        for needle in ('id="calendar-panel"', 'id="cv-body"', 'id="cv-branch"', 'data-cal-mode="week"', 'data-cal-mode="month"',
                       'data-cal-mode="agenda"', "calendar_view.js"):
            self.assertIn(needle, self.html)
        for gone in ("cal-frame", "calendar.google.com", "Sync now", "Set up the Google Calendar", "calendar.js\""):
            self.assertNotIn(gone, self.html)
        self.assertFalse((ROOT / "static" / "calendar.js").exists())

    def test_the_calendar_asks_the_server_and_follows_the_branch_switcher(self):
        self.assertIn("/calendar/data?start=", self.js)
        self.assertIn("Branches.view()", self.js)
        self.assertIn('document.addEventListener("branchchange"', self.js)

    def test_voice_open_calendar_still_works(self):
        self.assertIn("window.CalendarTab", self.js)
        self.assertIn("show: function", self.js)
        self.assertIn("CalendarTab.show", (ROOT / "static" / "live_voice.js").read_text())

    def test_the_calendar_inserts_text_never_html(self):
        self.assertNotIn("innerHTML = \"<", self.js)
        self.assertNotIn("insertAdjacentHTML", self.js)


if __name__ == "__main__":
    unittest.main()
