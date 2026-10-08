"""The Cancel / Reschedule card shows the appointment as text with the calendar hover's details, so each option the
server sends carries them: who, end time, phone, status, branch and doctor (and the token the hover shows).
Dates are relative to today, never fixed."""

import json
import shutil
import sqlite3
import subprocess
import unittest
from datetime import date, timedelta
from pathlib import Path

from clinic import branches
from clinic.adapters.local_sqlite import LocalSQLiteAdapter
from clinic.nlu.parser import QUEUE_WRITE_INTENTS
from clinic.pipeline import respond_to_intent

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "clinic" / "schema.sql").read_text()
DEFER = frozenset({"book_appointment", "cancel_appointment", "reschedule_appointment"}) | QUEUE_WRITE_INTENTS
TODAY = date.today()


def day(offset):
    return (TODAY + timedelta(days=offset)).isoformat()


class Case(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.addCleanup(self.conn.close)
        self.adapter = LocalSQLiteAdapter()
        self.conn.execute("INSERT INTO patients (name, phone, age) VALUES ('Amit Dua', '9876500301', 40)")
        self.conn.execute("INSERT INTO patients (name, phone, age) VALUES ('Ravi Kumar', '9876500503', 40)")

    def book(self, patient_id, name, phone, days, start, minutes=30, status="booked", notes=None, branch_id=None, doctor_id=None):
        cur = self.conn.execute(
            "INSERT INTO appointments (patient_id, patient_name, patient_phone, appt_date, start_time, duration_minutes, status, "
            "notes, branch_id, doctor_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (patient_id, name, phone, day(days), start, minutes, status, notes, branch_id, doctor_id))
        self.conn.commit()
        return cur.lastrowid

    def card(self, intent, slots):
        return respond_to_intent(self.conn, intent, dict(slots), "", self.adapter, self.adapter, "en-IN", DEFER)

    def options(self, intent, slots):
        return self.card(intent, slots).resolved["appointments"]


class OptionDetails(Case):
    def test_a_registered_patients_option_carries_what_the_hover_shows(self):
        appt = self.book(1, None, None, 2, "10:00", minutes=45, notes="secret diagnosis text")
        [opt] = self.options("cancel_appointment", {"patient_name": "Amit Dua"})
        self.assertEqual(opt["id"], appt)
        self.assertEqual((opt["appt_date"], opt["start_time"], opt["duration_minutes"]), (day(2), "10:00", 45))
        self.assertEqual(opt["end_time"], "10:45")
        self.assertEqual(opt["patient_name"], "Amit Dua")
        self.assertEqual(opt["patient_phone"], "9876500301")
        self.assertEqual(opt["status"], "booked")
        self.assertEqual(opt["label"], "{} 10:00 - Amit Dua".format(day(2)))          # the dropdown label is unchanged
        self.assertTrue(opt["branch"])                                               # the default branch, as on the calendar
        self.assertIn("token", opt)
        self.assertNotIn("secret", json.dumps(opt))                                   # notes are never carried

    def test_a_walk_ins_option_uses_the_name_and_phone_written_on_the_appointment(self):
        self.book(None, "Sunita Walk", "9811122233", 1, "15:00")
        [opt] = self.options("cancel_appointment", {"patient_name": "Sunita Walk"})
        self.assertEqual(opt["patient_name"], "Sunita Walk")
        self.assertEqual(opt["patient_phone"], "9811122233")
        self.assertEqual(opt["end_time"], "15:30")
        self.assertEqual(opt["status"], "booked")

    def test_a_walk_in_with_no_phone_has_none(self):
        self.book(None, "Sunita Walk", None, 1, "15:00")
        [opt] = self.options("cancel_appointment", {"patient_name": "Sunita Walk"})
        self.assertFalse(opt["patient_phone"])

    def test_a_confirmed_status_is_carried(self):
        self.book(1, None, None, 1, "09:00", status="confirmed")
        [opt] = self.options("reschedule_appointment", {"patient_name": "Amit Dua"})
        self.assertEqual(opt["status"], "confirmed")

    def test_branch_and_doctor_names_when_the_clinic_has_branches(self):
        b = branches.add_branch(self.conn, "B", "Branch B", "Sector 56")
        rao = branches.add_doctor(self.conn, "Dr. Rao")
        self.book(1, None, None, 3, "11:00", branch_id=b, doctor_id=rao)
        [opt] = self.options("cancel_appointment", {"patient_name": "Amit Dua"})
        self.assertEqual((opt["branch"], opt["doctor"]), ("Branch B", "Dr. Rao"))

    def test_the_picked_appointment_off_the_list_still_carries_details(self):
        # One that is today but earlier is listed; one picked by id from the screen (not in the person's list
        # because the name carried does not match) comes through appointment_option with the same fields.
        appt = self.book(2, None, None, 1, "12:00", minutes=20)
        card = self.card("cancel_appointment", {"appointment_id": appt, "patient_name": "Nobody Matching"})
        [opt] = [o for o in card.resolved["appointments"] if o["id"] == appt]
        self.assertEqual(opt["patient_name"], "Ravi Kumar")
        self.assertEqual(opt["end_time"], "12:20")
        self.assertEqual(opt["patient_phone"], "9876500503")
        self.assertEqual(card.slots["appointment_id"], appt)

    def test_two_people_keep_their_phone_tails_in_the_labels_and_each_option_its_own_details(self):
        self.conn.execute("INSERT INTO patients (name, phone, age) VALUES ('Amit Dua', '9876500402', 40)")
        self.book(1, None, None, 2, "10:00")
        self.book(3, None, None, 3, "10:00")
        opts = self.options("cancel_appointment", {"patient_name": "Amit Dua"})
        self.assertEqual(len(opts), 2)
        self.assertEqual({o["patient_phone"] for o in opts}, {"9876500301", "9876500402"})
        self.assertTrue(all("(…" in o["label"] for o in opts))

    def test_one_settled_appointment_is_still_preselected(self):
        appt = self.book(1, None, None, 2, "10:00")
        card = self.card("cancel_appointment", {"patient_name": "Amit Dua"})
        self.assertEqual(card.slots["appointment_id"], appt)

    def test_no_appointment_gives_no_options(self):
        card = self.card("cancel_appointment", {"patient_name": "Amit Dua"})
        self.assertEqual(card.resolved["appointments"], [])
        self.assertIn("No upcoming appointment", card.resolved["note"])

    def test_the_options_survive_json_for_the_socket(self):
        self.book(1, None, None, 2, "10:00")
        json.dumps(self.options("cancel_appointment", {"patient_name": "Amit Dua"}))


class TheScreens(unittest.TestCase):
    def read(self, *parts):
        return ROOT.joinpath(*parts).read_text(encoding="utf-8")

    def test_only_cancel_and_reschedule_ask_for_the_text_display(self):
        card = self.read("static", "review_card.js")
        for intent in ("cancel_appointment", "reschedule_appointment"):
            block = card[card.index(intent + ": ["):]
            self.assertIn("detail: true", block[:block.index("]")])
        queue = card[card.index("queue_check_in: ["):]
        self.assertNotIn("detail: true", queue[:queue.index("};")])

    def test_the_card_keeps_a_hidden_carrier_of_the_id_and_builds_text_safely(self):
        card = self.read("static", "review_card.js")
        fn = card[card.index("function buildAppointmentDetail"):card.index("function buildFieldInput")]
        self.assertIn('type: "hidden", "data-key": spec.key', fn)
        self.assertNotIn("innerHTML", fn)
        self.assertNotIn('"data-key"', fn[fn.index("var select = el("):fn.index("select.addEventListener")])   # the selector is not the field

    def test_the_shared_helper_loads_before_the_card_and_the_calendar(self):
        html = self.read("templates", "dashboard.html")
        self.assertLess(html.index("appt_details.js"), html.index("review_card.js"))
        self.assertLess(html.index("appt_details.js"), html.index("calendar_view.js"))
        self.assertIn("ApptDetails.lines", self.read("static", "calendar_view.js"))


class NodeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_pure_helper(self):
        out = subprocess.run(["node", str(ROOT / "tests" / "appt_details.test.js")], capture_output=True, text=True,
                             cwd=str(ROOT), timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("passed", out.stdout)


if __name__ == "__main__":
    unittest.main()
