"""Patients table: the first 10 rows plus a down arrow that loads the next 10."""

import re
import unittest

from tests.test_app_routes import RouteTestCase, clinic_app


class PatientsPagingTests(RouteTestCase):
    def add_patients(self, count):
        for n in range(1, count + 1):
            self.conn.execute(
                "INSERT INTO patients (name, phone, age, registered_at) VALUES (?, ?, ?, ?)",
                ("Patient {:02d}".format(n), "90000{:05d}".format(n), 20 + n if n % 2 else None, "2026-10-03 06:34:16"),
            )
        self.conn.commit()

    def rows(self, html):
        return re.findall(r'data-patient-id="(\d+)"', html)

    def test_first_page_is_ten_newest_with_a_next_arrow(self):
        self.add_patients(27)
        html = self.client.get("/").get_data(as_text=True)
        ids = self.rows(html)
        self.assertEqual(10, len(ids))
        self.assertEqual(sorted(ids, key=int, reverse=True), ids)          # newest first
        self.assertIn("(showing 10 of 27)", html)
        self.assertIn('id="patients-more"', html)
        self.assertIn("Show the next 10 patients", html)

    def test_no_arrow_when_everyone_already_shows(self):
        self.add_patients(10)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("(showing 10 of 10)", html)
        self.assertNotIn('id="patients-more"', html)

    def test_pages_walk_back_to_the_oldest_with_no_repeats(self):
        self.add_patients(27)
        html = self.client.get("/").get_data(as_text=True)
        seen = self.rows(html)
        last, rounds = seen[-1], 0
        while True:
            data = self.client.get("/patients/page?before_id={}&limit=10".format(last)).get_json()
            self.assertTrue(data["ok"])
            seen += [str(p["id"]) for p in data["patients"]]
            rounds += 1
            if not data["has_more"]:
                break
            last = str(data["patients"][-1]["id"])
        self.assertEqual(2, rounds)                    # 10 + 10 + 7
        self.assertEqual(27, len(seen))
        self.assertEqual(27, len(set(seen)))
        self.assertEqual(sorted(seen, key=int, reverse=True), seen)

    def test_last_page_says_nothing_more(self):
        self.add_patients(12)
        last_first_page = self.rows(self.client.get("/").get_data(as_text=True))[-1]
        data = self.client.get("/patients/page?before_id={}".format(last_first_page)).get_json()
        self.assertEqual(2, len(data["patients"]))
        self.assertFalse(data["has_more"])

    def test_row_fields_match_the_table(self):
        self.add_patients(12)
        first = self.rows(self.client.get("/").get_data(as_text=True))[-1]
        row = self.client.get("/patients/page?before_id={}".format(first)).get_json()["patients"][0]
        self.assertEqual({"id", "name", "phone", "age", "registered_at"}, set(row))
        self.assertEqual("2026-10-03 12:04:16", row["registered_at"])        # IST, like the main table

    def test_a_patient_registered_meanwhile_cannot_repeat_a_row(self):
        self.add_patients(15)
        shown = self.rows(self.client.get("/").get_data(as_text=True))
        self.add_patients(3)                                                  # new patients arrive while it is open
        data = self.client.get("/patients/page?before_id={}".format(shown[-1])).get_json()
        self.assertFalse(set(shown) & {str(p["id"]) for p in data["patients"]})

    def test_limit_is_capped_and_bad_input_is_refused(self):
        self.add_patients(5)
        big = self.client.get("/patients/page?before_id=9999&limit=100000").get_json()
        self.assertTrue(big["ok"])
        self.assertLessEqual(len(big["patients"]), clinic_app.PATIENTS_PAGE_MAX)
        for query in ("", "before_id=abc", "before_id=5&limit=x"):
            response = self.client.get("/patients/page?" + query)
            self.assertEqual(400, response.status_code, query)
            self.assertFalse(response.get_json()["ok"])

    def test_nothing_older_than_the_first_patient(self):
        self.add_patients(3)
        oldest = self.rows(self.client.get("/").get_data(as_text=True))[-1]
        data = self.client.get("/patients/page?before_id={}".format(oldest)).get_json()
        self.assertEqual([], data["patients"])
        self.assertFalse(data["has_more"])

    def test_the_script_is_loaded_and_wired_into_the_refresh(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("patients_list.js", html)
        refresh = open("static/dashboard_refresh.js").read()
        self.assertIn("PatientsList.restore", refresh)


if __name__ == "__main__":
    unittest.main()
