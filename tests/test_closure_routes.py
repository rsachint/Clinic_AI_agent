"""The closure routes the Automation tab and the voice card call: plan, apply
(with the patient notices going out), undo, and the history list."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_branch_routes import BranchRouteCase  # noqa: E402

from clinic import branches  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class ClosureRoutes(BranchRouteCase):
    PHONES = {"One": "9111100001", "Two": "9111100002"}

    def setUp(self):
        super().setUp()
        self.ids = {}
        for name, time in (("One", "10:00"), ("Two", "11:00")):
            cur = self.conn.execute(
                "INSERT INTO appointments (patient_name, patient_phone, appt_date, start_time, duration_minutes, status, branch_id, doctor_id) "
                "VALUES (?, ?, ?, ?, 30, 'booked', 1, 1)", ("Pt " + name, self.PHONES[name], self.monday, time))
            self.ids[name] = cur.lastrowid
            self.open_window("91" + self.PHONES[name], "hello")
        self.conn.commit()

    def scope(self, **extra):
        body = {"branch_id": 1, "start_date": self.monday, "end_date": self.monday}
        body.update(extra)
        return body

    def plan(self, **extra):
        status, body = self.post("/closures/plan", self.scope(**extra))
        return status, body

    def apply_default(self, **extra):
        _, planned = self.plan()
        moves = [{"appointment_id": m["appointment_id"], "action": m["action"], "to_branch_id": (m["to"] or {}).get("branch_id"),
                  "to_date": (m["to"] or {}).get("date"), "to_time": (m["to"] or {}).get("time")} for m in planned["plan"]["moves"]]
        body = self.scope(reason="Doctor on leave", message="Sorry!", moves=moves)
        body.update(extra)
        return self.post("/closures/apply", body)

    # -- plan -------------------------------------------------------------------------
    def test_plan_lists_the_patients_with_a_proposed_slot_and_writes_nothing(self):
        status, body = self.plan()
        self.assertEqual(status, 200)
        plan = body["plan"]
        self.assertEqual([m["name"] for m in plan["moves"]], ["Pt One", "Pt Two"])
        self.assertEqual([m["action"] for m in plan["moves"]], ["move", "move"])
        self.assertEqual(plan["moves"][0]["to"]["branch"], "Branch B")
        self.assertEqual(plan["counts"], {"total": 2, "movable": 2, "unresolved": 0})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM booking_blocks").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM closures").fetchone()[0], 0)

    def test_plan_rejects_bad_input_with_a_message(self):
        for extra in (dict(branch_id=None), dict(branch_id=99), dict(start_date="soon"), dict(end_date="2000-01-01")):
            with self.subTest(extra=extra):
                status, body = self.plan(**extra)
                self.assertEqual(status, 400)
                self.assertFalse(body["ok"])
                self.assertTrue(body["error"])

    # -- apply ----------------------------------------------------------------------------
    def test_apply_moves_blocks_notifies_and_reports(self):
        status, body = self.apply_default()
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["counts"], {"moved": 2, "cancelled": 0, "failed": 0, "left": 0})
        self.assertEqual(body["message"], "Closure applied: 2 moved, 0 cancelled, 0 left as they were.")
        rows = self.conn.execute("SELECT branch_id FROM appointments ORDER BY id").fetchall()
        self.assertEqual([r["branch_id"] for r in rows], [2, 2])
        # each patient was messaged once, with the buttons, and it went out (the 24-hour window is open)
        # (this sender takes plain text, so the buttons arrive as their titles under the message)
        texts = [text for _, text in self.sender.calls if "cannot see patients at Branch A" in text]
        self.assertEqual(len(texts), 2)
        self.assertTrue(all("- Accept" in t and "- Choose another" in t and "(Doctor on leave)" in t for t in texts), texts)
        self.assertEqual(body["notices"]["sent"], 2)

    def test_notices_outside_the_24_hour_window_are_reported_as_waiting(self):
        self.conn.execute("DELETE FROM wa_messages")
        self.conn.commit()
        _, body = self.apply_default()
        self.assertEqual(body["notices"]["sent"], 0)
        self.assertEqual(body["notices"]["waiting"], 2)

    def test_apply_with_nobody_booked_still_stops_new_bookings(self):
        self.conn.execute("UPDATE appointments SET status = 'cancelled'")
        self.conn.commit()
        status, body = self.post("/closures/apply", self.scope(reason="Renovation", moves=[]))
        self.assertTrue(body["ok"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM booking_blocks WHERE active = 1").fetchone()[0], 1)

    def test_apply_with_bad_scope_changes_nothing(self):
        status, body = self.post("/closures/apply", self.scope(branch_id=None, moves=[]))
        self.assertEqual(status, 400)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM booking_blocks").fetchone()[0], 0)

    # -- undo / list ---------------------------------------------------------------------------
    def test_undo_restores_everything_and_tells_the_patients(self):
        _, applied = self.apply_default()
        status, body = self.post("/closures/{}/undo".format(applied["closure_id"]))
        self.assertEqual(status, 200)
        self.assertEqual(body["restored"], 2)
        self.assertIn("Closure undone: 2 put back", body["message"])
        self.assertEqual([r["branch_id"] for r in self.conn.execute("SELECT branch_id FROM appointments ORDER BY id")], [1, 1])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM booking_blocks WHERE active = 1").fetchone()[0], 0)
        later = [text for _, text in self.sender.calls[2:]]
        self.assertTrue(any("moved" in t.lower() or "reschedul" in t.lower() for t in later), later)
        listed = self.client.get("/closures/data").get_json()["closures"][0]
        self.assertEqual((listed["status"], listed["counts"]["undone"]), ("undone", 2))
        again_status, again = self.post("/closures/{}/undo".format(applied["closure_id"]))
        self.assertEqual(again_status, 400)
        self.assertFalse(again["ok"])

    def test_undo_names_who_was_left_alone_and_why(self):
        _, applied = self.apply_default()
        self.conn.execute("UPDATE appointments SET start_time = '12:00' WHERE id = ?", (self.ids["One"],))
        self.conn.commit()
        status, body = self.post("/closures/{}/undo".format(applied["closure_id"]))
        self.assertEqual(status, 200)
        self.assertIn("Closure undone: 1 put back; left as they are: Pt One (the patient has changed it since)", body["message"])

    def test_the_history_shows_counts_and_what_patients_answered(self):
        _, applied = self.apply_default()
        move_id = applied["results"][0]["move_id"]
        from clinic import closures
        closures.respond(self.conn, move_id, "919111100001", "accepted")
        body = self.client.get("/closures/data").get_json()
        item = body["closures"][0]
        self.assertEqual(item["id"], applied["closure_id"])
        self.assertEqual((item["counts"]["moved"], item["counts"]["accepted"], item["counts"]["waiting"]), (2, 1, 1))
        self.assertEqual(item["notices"]["sent"], 2)
        self.assertEqual(item["branch"], "Branch A")


class ClosureScreenWiring(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "templates" / "dashboard.html").read_text()
        self.js = (ROOT / "static" / "closures.js").read_text()

    def test_the_automation_tab_has_the_closure_form_and_history(self):
        for needle in ('id="closures-card"', 'id="closure-form"', 'id="closure-review"', 'id="closures-list"', "closures.js"):
            self.assertIn(needle, self.html)

    def test_the_card_posts_to_the_closure_routes_and_inserts_data_as_text(self):
        for url in ("/closures/plan", "/closures/apply", "/closures/", "/closures/data"):
            self.assertIn(url, self.js)
        self.assertNotIn("innerHTML = " + "\"<", self.js)
        self.assertNotIn(".innerHTML +=", self.js)

    def test_the_card_asks_before_applying_and_before_undoing(self):
        self.assertGreaterEqual(self.js.count("window.confirm("), 3)


if __name__ == "__main__":
    unittest.main()
