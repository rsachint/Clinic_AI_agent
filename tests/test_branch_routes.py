import json
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_app_routes import RouteTestCase, clinic_app  # noqa: E402  (also stubs dotenv and the DB path)
from clinic import branches  # noqa: E402


def next_monday():
    day = date.today() + timedelta(days=1)
    return day + timedelta(days=(7 - day.weekday()) % 7)


class BranchRouteCase(RouteTestCase):
    def setUp(self):
        super().setUp()
        branches.ensure_seed(self.conn)          # Branch A + example B and C
        self.monday = next_monday().isoformat()

    def post(self, url, payload=None):
        response = self.client.post(url, json=payload or {})
        return response.status_code, response.get_json()

    def appt(self, appointment_id):
        return dict(self.conn.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,)).fetchone())


class DashboardTests(BranchRouteCase):
    def test_the_page_carries_the_branch_data_and_switcher(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="branch-switcher"', html)
        self.assertIn('id="my-branch"', html)
        self.assertIn('id="settings-branches"', html)
        data = json.loads(html.split('id="branches-data" type="application/json">')[1].split("</script>")[0])
        self.assertEqual([b["code"] for b in data["branches"]], ["A", "B", "C"])
        self.assertTrue(data["multi_branch"])
        self.assertEqual(data["default_branch_id"], 1)

    def test_importing_the_app_never_seeds_the_example_branches(self):
        # RouteTestCase's own database was seeded by this test class; a fresh one is not.
        import tempfile
        from clinic import db
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(str(Path(tmp) / "x.db"))
            self.assertEqual([b["code"] for b in branches.list_branches(conn)], ["A"])
            conn.close()


class SlotRouteTests(BranchRouteCase):
    def slots(self, branch, day=None):
        response = self.client.get("/appointments/slots?date={}&branch={}".format(day or self.monday, branch))
        return response.status_code, response.get_json()

    def test_each_branch_offers_its_own_doctors_hours(self):
        status, a = self.slots(1)
        self.assertEqual(status, 200)
        self.assertEqual(a["free"][0], "09:00")
        status, b = self.slots(2)
        self.assertEqual(b["branch_id"], 2)
        self.assertEqual(b["doctor"], "Dr. Rao")          # the seed puts Dr. Rao at Branch B on Mondays
        status, c = self.slots(3)
        self.assertEqual(c["doctor"], "Dr. Iyer")

    def test_no_branch_means_the_default_branch(self):
        response = self.client.get("/appointments/slots?date={}".format(self.monday))
        self.assertEqual(response.get_json()["branch_id"], 1)

    def test_a_bad_branch_is_refused(self):
        status, body = self.slots(99)
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        status, body = self.slots("abc")
        self.assertEqual(status, 400)

    def test_a_closed_branch_has_no_slots(self):
        self.post("/settings/branches/2/status", {"status": "closed", "reason": "Renovation", "message": "Closed"})
        status, body = self.slots(2)
        self.assertEqual(body["free"], [])


class BookingRouteTests(BranchRouteCase):
    def book(self, branch, time="10:00", **extra):
        payload = {"patient_name": "Route Pt", "patient_phone": "9876500123", "appt_date": self.monday, "start_time": time, "branch_id": branch}
        payload.update(extra)
        return self.post("/appointments/new", payload)

    def test_booking_records_the_branch_and_the_doctor_and_says_where(self):
        status, body = self.book(2)
        self.assertTrue(body["ok"], body)
        self.assertIn("Branch B", body["message"])
        row = self.appt(body["appointment_id"])
        self.assertEqual(row["branch_id"], 2)
        self.assertEqual(branches.doctor_label(self.conn, row["doctor_id"]), "Dr. Rao")

    def test_a_time_outside_that_branchs_hours_is_refused(self):
        status, body = self.book(2, time="08:00")
        self.assertFalse(body["ok"])
        self.assertIn("slots", body["error"])

    def test_the_same_time_can_be_booked_at_two_branches_but_not_twice_at_one(self):
        self.assertTrue(self.book(1)[1]["ok"])
        self.assertTrue(self.book(2)[1]["ok"])
        again = self.book(2)[1]
        self.assertFalse(again["ok"])

    def test_no_branch_in_the_request_means_the_default_branch(self):
        body = self.post("/appointments/new", {"patient_name": "Pt", "patient_phone": "9876500124", "appt_date": self.monday, "start_time": "09:00"})[1]
        self.assertTrue(body["ok"], body)
        self.assertEqual(self.appt(body["appointment_id"])["branch_id"], 1)

    def test_a_bad_branch_is_refused(self):
        self.assertFalse(self.book(99)[1]["ok"])

    def test_moving_an_appointment_to_another_branch(self):
        appt_id = self.book(1, "09:00")[1]["appointment_id"]
        status, body = self.post("/appointments/{}/move".format(appt_id), {"appt_date": self.monday, "start_time": "10:00", "branch_id": 2})
        self.assertTrue(body["ok"], body)
        self.assertIn("Branch B", body["message"])
        row = self.appt(appt_id)
        self.assertEqual((row["branch_id"], row["start_time"]), (2, "10:00"))

    def test_moving_without_a_branch_keeps_the_appointment_where_it_is(self):
        appt_id = self.book(2, "10:00")[1]["appointment_id"]
        body = self.post("/appointments/{}/move".format(appt_id), {"appt_date": self.monday, "start_time": "10:30"})[1]
        self.assertTrue(body["ok"], body)
        self.assertEqual(self.appt(appt_id)["branch_id"], 2)

    def test_moving_to_a_branch_with_no_doctor_at_that_time_is_refused(self):
        appt_id = self.book(1, "09:00")[1]["appointment_id"]
        body = self.post("/appointments/{}/move".format(appt_id), {"appt_date": self.monday, "start_time": "08:30", "branch_id": 2})[1]
        self.assertFalse(body["ok"])
        self.assertEqual(self.appt(appt_id)["branch_id"], 1)


class QueueRouteTests(BranchRouteCase):
    def setUp(self):
        super().setUp()
        for name, time, branch in (("Alice A", "09:00", 1), ("Bob B", "10:00", 2), ("Carol B", "10:30", 2)):
            self.conn.execute("INSERT INTO appointments (patient_name, patient_phone, appt_date, start_time, status, branch_id) VALUES (?, '9876500001', ?, ?, 'booked', ?)",
                              (name, self.monday, time, branch))
        self.conn.commit()

    def partial(self, branch=None):
        suffix = "" if branch is None else "&branch={}".format(branch)
        return self.client.get("/queue/partial?date={}{}".format(self.monday, suffix)).get_data(as_text=True)

    def test_default_is_the_default_branchs_queue(self):
        html = self.partial()
        self.assertIn("Alice A", html)
        self.assertNotIn("Bob B", html)

    def test_one_branchs_queue_with_its_own_token_numbers(self):
        html = self.partial(2)
        self.assertIn("Bob B", html)
        self.assertNotIn("Alice A", html)
        self.assertIn("B-T01", html)
        self.assertIn("B-T02", html)

    def test_all_branches_show_one_card_each(self):
        html = self.partial("all")
        for needle in ("Alice A", "Bob B", "A-T01", "B-T01", "Branch A", "Branch B", "Branch C"):
            self.assertIn(needle, html)

    def test_a_bad_branch_falls_back_to_the_default(self):
        self.assertIn("Alice A", self.partial("zzz"))


class SettingsRouteTests(BranchRouteCase):
    def test_add_edit_close_reopen_and_default(self):
        status, body = self.post("/settings/branches", {"code": "d", "name": "Branch D", "pin_code": "122020", "address": "Somewhere"})
        self.assertTrue(body["ok"], body)
        new_id = body["id"]
        self.assertEqual([b["code"] for b in body["data"]["branches"]], ["A", "B", "C", "D"])
        self.assertTrue(self.post("/settings/branches/{}".format(new_id), {"name": "Sector 9", "phone": "0124-1"})[1]["ok"])
        self.assertEqual(branches.get_branch(self.conn, new_id)["name"], "Sector 9")
        self.assertTrue(self.post("/settings/branches/{}/status".format(new_id), {"status": "closed", "reason": "Renovation", "message": "Closed"})[1]["ok"])
        self.assertEqual(branches.get_branch(self.conn, new_id)["status"], "closed")
        self.assertTrue(self.post("/settings/branches/{}/status".format(new_id), {"status": "open"})[1]["ok"])
        body = self.post("/settings/default-branch", {"branch_id": new_id})[1]
        self.assertEqual(body["data"]["default_branch_id"], new_id)

    def test_bad_input_comes_back_as_a_readable_error(self):
        status, body = self.post("/settings/branches", {"code": "A", "name": "Duplicate"})
        self.assertEqual(status, 400)
        self.assertIn("already", body["error"])
        self.assertEqual(self.post("/settings/branches", {"code": "Z", "name": "x", "pin_code": "12"})[0], 400)
        self.assertEqual(self.post("/settings/branches/99", {"name": "x"})[0], 400)
        self.assertEqual(self.post("/settings/default-branch", {"branch_id": 99})[0], 400)

    def test_unknown_fields_cannot_be_set(self):
        self.post("/settings/branches/2", {"name": "Renamed", "status": "closed", "active": 0, "id": 77, "conn": "x"})
        branch = branches.get_branch(self.conn, 2)
        self.assertEqual((branch["name"], branch["status"], branch["active"], branch["id"]), ("Renamed", "open", 1, 2))

    def test_a_branch_with_upcoming_appointments_cannot_be_removed(self):
        self.conn.execute("INSERT INTO appointments (patient_name, appt_date, start_time, status, branch_id) VALUES ('X', ?, '10:00', 'booked', 2)", (self.monday,))
        self.conn.commit()
        status, body = self.post("/settings/branches/2/deactivate")
        self.assertEqual(status, 400)
        self.assertIn("upcoming", body["error"])
        self.assertEqual(self.post("/settings/branches/3/deactivate")[0], 200)     # C has none

    def test_doctors_and_schedules(self):
        doctor = self.post("/settings/doctors", {"name": "Dr. New", "specialty": "ENT"})[1]["id"]
        status, body = self.post("/settings/schedules", {"doctor_id": doctor, "branch_id": 1, "weekdays": [0, 1], "start_time": "09:00", "end_time": "13:00"})
        self.assertEqual(status, 400)                              # Branch A already has Dr. Mehta then
        self.assertIn("only one doctor per branch", body["error"])
        status, body = self.post("/settings/schedules", {"doctor_id": doctor, "branch_id": 2, "weekday": 6, "start_time": "09:00", "end_time": "13:00"})
        self.assertEqual(status, 200, body)                         # Sunday at B is free
        added = [s for s in body["data"]["schedules"] if s["doctor_id"] == doctor]
        self.assertEqual(len(added), 1)
        self.assertTrue(self.post("/settings/schedules/{}/remove".format(added[0]["id"]))[1]["ok"])
        self.assertEqual(self.post("/settings/doctors/{}".format(doctor), {"name": "Dr. Newer"})[1]["ok"], True)

    def test_a_doctor_cannot_be_put_at_two_branches_at_once(self):
        rao_at_b = [s for s in branches.list_schedule(self.conn, branch_id=2) if s["doctor_name"] == "Dr. Rao"][0]
        status, body = self.post("/settings/schedules", {"doctor_id": rao_at_b["doctor_id"], "branch_id": 3, "weekday": rao_at_b["weekday"],
                                                          "start_time": rao_at_b["start_time"], "end_time": rao_at_b["end_time"]})
        self.assertEqual(status, 400)

    def test_a_branch_scoped_block_closes_only_that_branch(self):
        status, body = self.post("/automation/blocks", {"start_date": self.monday, "end_date": self.monday, "reason": "Renovation", "branch_id": 2})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["block"]["branch_id"], 2)
        free = lambda b: self.client.get("/appointments/slots?date={}&branch={}".format(self.monday, b)).get_json()
        self.assertEqual(free(2)["free"], [])
        self.assertTrue(free(1)["free"])
        data = self.client.get("/automation/data").get_json()
        self.assertEqual(data["blocks"][0]["branch"], "Branch B")

    def test_a_bad_block_scope_is_refused(self):
        status, body = self.post("/automation/blocks", {"start_date": self.monday, "branch_id": 99})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
