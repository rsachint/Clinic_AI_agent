"""A phone number is mandatory for every NEW appointment booking, on every path (clinic/booking_phone.py).

The write handler is the authority (so /approve, the WhatsApp inbox approve, the staff form, the follow-up
scheduler and the automatic WhatsApp path all stop at the same place); the review card, the voice assistant and
the follow-up plan flag the problem earlier. Only NEW bookings are checked: an older appointment may have no phone,
and rescheduling, cancelling, the queue and a closure move must keep working for it. Fake numbers only."""
import json
import shutil
import subprocess
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_auto_appointments import AutoCase, CLOCK_NOW, TOMORROW, WA, WA2, clinic_app  # noqa: E402
from tests.test_closures import A, B, ClosureCase, TUE  # noqa: E402
from tests.test_voice_dialog import DialogTestCase  # noqa: E402
from tests.followup_fixtures import FollowupCase, at  # noqa: E402
from tests.planner_support import PlannerCase  # noqa: E402

from clinic import auto_actions, auto_policy, booking_phone, closures, core, db, followups, scheduling  # noqa: E402
from clinic.booking_phone import PhoneRequiredError, PATIENT_NO_PHONE, REQUIRED  # noqa: E402
from clinic.intents import HANDLERS  # noqa: E402
from clinic.pipeline import ParsedResult, PipelineError  # noqa: E402
from clinic.voice_context import AskResult, Note, spoken_phone  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DAY, NINE, TEN = "2026-10-12", "09:00", "10:00"      # a Monday; the handler books without looking at today's date


def book(conn, **slots):
    """Propose and confirm a booking the way /approve does; returns (appointment row, proposal id)."""
    base = {"appt_date": DAY, "start_time": NINE, "duration_minutes": 15}
    pid = core.propose(conn, "book_appointment", dict(base, **slots))
    _, appt_id = core.confirm(conn, pid, HANDLERS)
    return conn.execute("SELECT * FROM appointments WHERE id = ?", (appt_id,)).fetchone(), pid


def count(conn, table):
    return conn.execute("SELECT COUNT(*) FROM {}".format(table)).fetchone()[0]


class TheRule(unittest.TestCase):
    def test_the_messages_are_fixed(self):
        self.assertEqual(REQUIRED, "A phone number is required to book an appointment. Enter the patient's 10-digit phone number.")
        self.assertIn("no valid phone number on file", PATIENT_NO_PHONE)
        self.assertIn("add or correct", PATIENT_NO_PHONE)

    def test_forms_that_normalise_to_ten_digits_are_valid(self):
        for phone in ("9876500301", "+91 98765 00301", "09876500301", "98765 00301", "919876500301", "98765-00301", 9876500301,
                      "1122334455"):                                   # no first-digit rule: real and test numbers like this exist
            self.assertEqual(len(booking_phone.valid_phone(phone)), 10, phone)

    def test_everything_else_is_not(self):
        for phone in (None, "", "   ", "12345", "98765 0030", "abcdefghij", "no phone"):
            self.assertEqual(booking_phone.valid_phone(phone), "", phone)


class HandlerRefusesAndAccepts(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def patient(self, name="Sunita Devi", phone="9876543210"):
        cur = self.conn.execute("INSERT INTO patients (name, phone) VALUES (?, ?)", (name, phone))
        self.conn.commit()
        return cur.lastrowid

    def test_no_phone_short_phone_and_blank_phone_are_refused_for_a_new_person(self):
        for kw in ({"patient_name": "Walk In"}, {"patient_name": "Walk In", "patient_phone": ""},
                   {"patient_name": "Walk In", "patient_phone": "   "}, {"patient_name": "Walk In", "patient_phone": "98765"},
                   {"patient_name": "Walk In", "patient_phone": "98765 0030"}, {"patient_phone": None}, {}):
            with self.assertRaises(PhoneRequiredError, msg=kw) as raised:
                book(self.conn, **kw)
            self.assertEqual(str(raised.exception), REQUIRED)
        self.assertEqual((count(self.conn, "appointments"), count(self.conn, "patients"), count(self.conn, "audit_log")), (0, 0, 0))

    def test_the_refusal_is_a_plain_failure_not_a_slot_error(self):
        # core.confirm / /approve / auto_actions treat it as an ordinary refusal; the automatic path must NOT
        # read it as "that time was just taken" and offer the patient other times.
        self.assertTrue(issubclass(PhoneRequiredError, ValueError))
        self.assertFalse(issubclass(PhoneRequiredError, scheduling.SlotConflictError))

    def test_a_refused_booking_writes_nothing_and_registers_nobody(self):
        pid = core.propose(self.conn, "book_appointment", {"patient_name": "Kavita", "appt_date": DAY, "start_time": NINE})
        with self.assertRaises(PhoneRequiredError):
            core.confirm(self.conn, pid, HANDLERS)
        self.assertEqual((count(self.conn, "appointments"), count(self.conn, "patients"), count(self.conn, "audit_log")), (0, 0, 0))
        self.assertEqual(self.conn.execute("SELECT status FROM proposals WHERE id = ?", (pid,)).fetchone()[0], "pending")

    def test_the_forms_of_a_phone_all_book(self):
        for i, phone in enumerate(("+91 98765 00301", "09876500301", "98765 00301", "919876500301")):
            row, _ = book(self.conn, patient_name="Walk In {}".format(i), patient_phone=phone, start_time="{:02d}:00".format(9 + i))
            self.assertEqual(row["status"], "booked", phone)

    def test_a_phone_with_no_name_is_a_walk_in_that_books(self):
        row, _ = book(self.conn, patient_phone="9876500301")
        self.assertEqual((row["patient_id"], row["patient_name"], row["patient_phone"]), (None, None, "9876500301"))

    def test_a_registered_patient_with_a_valid_phone_books_without_one_on_the_booking(self):
        pid = self.patient()
        row, _ = book(self.conn, patient_id=pid)
        self.assertEqual((row["patient_id"], row["patient_phone"]), (pid, None))

    def test_a_registered_patient_with_a_valid_phone_ignores_a_bad_phone_typed_on_the_booking(self):
        pid = self.patient()
        row, _ = book(self.conn, patient_id=pid, patient_phone="12")
        self.assertEqual((row["patient_id"], row["patient_phone"]), (pid, None))

    def test_a_registered_patient_with_an_unusable_phone_is_refused_and_the_row_is_untouched(self):
        for i, phone in enumerate(("12345", "")):
            pid = self.patient("Anita {}".format(i), phone)
            with self.assertRaises(PhoneRequiredError) as raised:
                book(self.conn, patient_id=pid)
            self.assertEqual(str(raised.exception), PATIENT_NO_PHONE)
            self.assertEqual(self.conn.execute("SELECT phone FROM patients WHERE id = ?", (pid,)).fetchone()[0], phone)
        self.assertEqual(count(self.conn, "appointments"), 0)

    def test_a_phone_given_on_the_booking_serves_such_a_patient_and_never_rewrites_the_record(self):
        pid = self.patient("Anita Rao", "12345")
        row, proposal = book(self.conn, patient_id=pid, patient_phone="+91 98765 00302")
        self.assertEqual(self.conn.execute("SELECT phone FROM patients WHERE id = ?", (pid,)).fetchone()[0], "12345")
        self.assertEqual((row["patient_id"], row["patient_phone"]), (pid, "9876500302"))     # the booking's own contact
        audit = json.loads(self.conn.execute("SELECT payload_json FROM audit_log WHERE proposal_id = ?", (proposal,)).fetchone()[0])
        self.assertEqual(audit["patient_phone"], "9876500302")

    def test_a_patient_id_that_does_not_exist_needs_a_phone_like_anyone(self):
        with self.assertRaises(PhoneRequiredError) as raised:
            book(self.conn, patient_id=999)
        self.assertEqual(str(raised.exception), REQUIRED)

    def test_name_and_phone_still_register_the_patient_exactly_as_before(self):
        row, proposal = book(self.conn, patient_name="Kavita", patient_phone="+91 98765 00301")
        patient = self.conn.execute("SELECT * FROM patients WHERE name = 'Kavita'").fetchone()
        self.assertEqual((row["patient_id"], row["patient_name"], row["patient_phone"]), (patient["id"], None, None))
        self.assertEqual(patient["phone"], "+91 98765 00301")                    # stored as given, as it always was
        audit = json.loads(self.conn.execute("SELECT payload_json FROM audit_log WHERE proposal_id = ?", (proposal,)).fetchone()[0])
        self.assertEqual(audit["registered_patient_id"], patient["id"])

    def test_an_unattended_booking_needs_the_phone_too_and_still_registers_nobody(self):
        row, _ = book(self.conn, patient_name="Auto Caller", patient_phone="9111122223", unattended=True)
        self.assertEqual((row["patient_id"], row["patient_phone"], count(self.conn, "patients")), (None, "9111122223", 0))
        with self.assertRaises(PhoneRequiredError):
            book(self.conn, patient_name="Auto Caller", unattended=True, start_time=TEN)


class OlderAppointmentsAreUntouched(unittest.TestCase):
    """An appointment from before the rule may have no phone at all: nothing but a NEW booking is checked."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.today = date.today().isoformat()
        cur = self.conn.execute("INSERT INTO appointments (patient_name, appt_date, start_time, duration_minutes) "
                                "VALUES ('Old Row', ?, '09:00', 15)", (DAY,))
        self.old = cur.lastrowid
        cur = self.conn.execute("INSERT INTO appointments (patient_name, appt_date, start_time, duration_minutes) "
                                "VALUES ('Old Today', ?, '09:00', 15)", (self.today,))
        self.old_today = cur.lastrowid
        self.conn.commit()

    def run_intent(self, intent, **slots):
        pid = core.propose(self.conn, intent, slots)
        return core.confirm(self.conn, pid, HANDLERS)

    def test_reschedule_cancel_and_the_queue_work_for_an_appointment_with_no_phone(self):
        self.run_intent("reschedule_appointment", appointment_id=self.old, appt_date="2026-10-13", start_time="10:00")
        self.assertEqual(self.conn.execute("SELECT appt_date FROM appointments WHERE id = ?", (self.old,)).fetchone()[0], "2026-10-13")
        self.run_intent("cancel_appointment", appointment_id=self.old)
        self.assertEqual(self.conn.execute("SELECT status FROM appointments WHERE id = ?", (self.old,)).fetchone()[0], "cancelled")
        self.run_intent("restore_appointment", appointment_id=self.old, status="booked")
        self.run_intent("queue_check_in", appointment_id=self.old_today)
        self.assertEqual(self.conn.execute("SELECT queue_state FROM appointments WHERE id = ?", (self.old_today,)).fetchone()[0], "checked_in")
        self.run_intent("queue_call_next", appointment_id=self.old_today)
        self.run_intent("queue_mark_done", appointment_id=self.old_today)

    def test_the_move_route_and_a_closure_move_work_too(self):
        class Closing(ClosureCase):
            def runTest(self):
                pass
        case = Closing()
        case.setUp()
        try:
            a = case.appt("No Phone", A, TUE, "10:00", phone=None)
            plan = case.plan()
            self.assertEqual(plan["moves"][0]["action"], "move")
            result = case.apply(plan)
            self.assertTrue(result["ok"], result)
            self.assertEqual(case.row(a)["branch_id"], B)
        finally:
            case.conn.close()


class RoutesRefuseAWalkInWithoutAPhone(AutoCase):
    def approve(self, slots, intent="book_appointment"):
        return self.client.post("/approve", json={"intent": intent, "slots": slots, "language": "en-IN"}).get_json()

    def wa_row(self, slots, patient_id=None):
        cur = self.conn.execute(
            "INSERT INTO wa_messages (wa_message_id, wa_id, patient_id, message_type, raw_text, intent, slots_json, status) "
            "VALUES ('wamid.bp1', ?, ?, 'text', 'book me', 'book_appointment', ?, 'classified')", (WA, patient_id, json.dumps(slots)))
        self.conn.commit()
        return cur.lastrowid

    def slots(self, **kw):
        return dict({"appt_date": TOMORROW, "start_time": "10:00", "duration_minutes": None}, **kw)

    def test_approve_refuses_a_name_only_booking_with_the_fixed_message(self):
        result = self.approve(self.slots(patient_name="Walk In"))
        self.assertEqual((result["ok"], result["error"]), (False, REQUIRED))
        self.assertEqual(self.count("appointments"), 0)
        self.assertEqual(self.count("patients"), 0)
        self.assertEqual(self.count("audit_log"), 0)

    def test_approve_books_once_a_phone_is_given(self):
        self.assertTrue(self.approve(self.slots(patient_name="Walk In", patient_phone="+91 90000 00001"))["ok"])
        self.assertEqual(self.count("appointments"), 1)

    def test_approve_refuses_a_registered_patient_with_no_valid_phone_and_leaves_the_record(self):
        pid = self.conn.execute("INSERT INTO patients (name, phone) VALUES ('Anita Rao', '12345')").lastrowid
        self.conn.commit()
        result = self.approve(self.slots(patient_id=pid))
        self.assertEqual((result["ok"], result["error"]), (False, PATIENT_NO_PHONE))
        self.assertEqual(self.conn.execute("SELECT phone FROM patients WHERE id = ?", (pid,)).fetchone()[0], "12345")
        self.assertTrue(self.approve(self.slots(patient_id=pid, patient_phone="9000000002"))["ok"])
        self.assertEqual(self.conn.execute("SELECT phone FROM patients WHERE id = ?", (pid,)).fetchone()[0], "12345")

    def test_approve_books_a_registered_patient_with_a_valid_phone_and_none_on_the_booking(self):
        pid = self.patient()
        self.assertTrue(self.approve(self.slots(patient_id=pid))["ok"])

    def test_approve_still_cancels_and_reschedules_an_older_appointment_with_no_phone(self):
        aid = self.appt(None, TOMORROW, "09:00", name="Old Row", phone=None)
        self.assertTrue(self.approve({"appointment_id": aid, "appt_date": TOMORROW, "start_time": "11:00"}, "reschedule_appointment")["ok"])
        self.assertTrue(self.approve({"appointment_id": aid}, "cancel_appointment")["ok"])

    def test_the_whatsapp_inbox_approve_refuses_a_walk_in_without_a_phone_and_keeps_the_item(self):
        msg = self.wa_row(self.slots(patient_name="Walk In"))
        result = self.client.post("/wa/{}/approve".format(msg), json={"slots": self.slots(patient_name="Walk In")}).get_json()
        self.assertEqual((result["ok"], result["error"]), (False, REQUIRED))
        self.assertEqual(self.count("appointments"), 0)
        self.assertEqual(self.conn.execute("SELECT status FROM wa_messages WHERE id = ?", (msg,)).fetchone()[0], "classified")
        again = self.client.post("/wa/{}/approve".format(msg), json={"slots": self.slots(patient_name="Walk In", patient_phone="9000000003")}).get_json()
        self.assertTrue(again["ok"], again)

    def test_the_inbox_card_data_carries_the_flag_before_anyone_presses_approve(self):
        self.wa_row(self.slots(patient_name="Walk In"))
        html = self.client.get("/").get_data(as_text=True)
        start = html.index('id="wa-inbox-data"')
        data = json.loads(html[html.index(">", start) + 1:html.index("</script>", start)])
        self.assertEqual(data[0]["phone_problem"], REQUIRED)

    # -- /appointments/new ---------------------------------------------------------------------
    def new(self, **body):
        base = {"appt_date": TOMORROW, "start_time": "10:00"}
        base.update(body)
        return self.client.post("/appointments/new", json=base).get_json()

    def test_the_staff_form_says_the_same_thing(self):
        for body in (dict(patient_name="Walk In"), dict(patient_name="Walk In", patient_phone="12345"),
                     dict(patient_name="Walk In", patient_phone="")):
            result = self.new(**body)
            self.assertEqual((result["ok"], result["error"]), (False, REQUIRED), body)
        self.assertEqual(self.count("appointments"), 0)

    def test_the_staff_form_books_with_any_normalising_form_of_the_phone(self):
        for i, phone in enumerate(("9000000001", "+91 90000 00002", "090000 00003")):
            self.assertTrue(self.new(patient_name="Walk In {}".format(i), patient_phone=phone, start_time="1{}:00".format(i))["ok"], phone)

    def test_the_staff_form_for_a_registered_patient(self):
        ok = self.patient()
        self.assertTrue(self.new(patient_id=ok)["ok"])                                  # their own number is enough
        bad = self.conn.execute("INSERT INTO patients (name, phone) VALUES ('Anita Rao', '12345')").lastrowid
        self.conn.commit()
        refused = self.new(patient_id=bad, start_time="11:00")
        self.assertEqual((refused["ok"], refused["error"]), (False, PATIENT_NO_PHONE))
        self.assertEqual(self.count("appointments"), 1)
        fixed = self.new(patient_id=bad, patient_phone="9000000004", start_time="11:00")
        self.assertTrue(fixed["ok"], fixed)
        self.assertEqual(self.conn.execute("SELECT phone FROM patients WHERE id = ?", (bad,)).fetchone()[0], "12345")

    def test_the_staff_form_still_moves_and_cancels_an_older_appointment_with_no_phone(self):
        aid = self.appt(None, TOMORROW, "09:00", name="Old Row", phone=None)
        moved = self.client.post("/appointments/{}/move".format(aid), json={"appt_date": TOMORROW, "start_time": "11:00"}).get_json()
        self.assertTrue(moved["ok"], moved)
        cancelled = self.client.post("/appointments/{}/cancel".format(aid), json={}).get_json()
        self.assertTrue(cancelled["ok"], cancelled)


class TheAutomaticWhatsAppPathStillBooks(AutoCase):
    """The sender's own number is always known there, so it is passed as the phone."""

    def test_an_unregistered_sender_books_with_their_number(self):
        self.book_through_conversation(wa_id=WA2, name="Raju Kumar")
        appt = self.rows("SELECT * FROM appointments")[0]
        self.assertEqual((appt["patient_id"], appt["patient_name"], appt["patient_phone"]), (None, "Raju Kumar", WA2[-10:]))
        self.assertEqual(len(self.activity("auto_booked")), 1)
        self.assertEqual(self.inbox(), [])

    def test_a_registered_sender_books_with_their_number_on_file(self):
        pid = self.patient()
        self.say("I want to book an appointment")
        self.say("tomorrow")
        self.say("4 pm")
        self.tap("confirm:yes", "Confirm request")
        appt = self.rows("SELECT * FROM appointments")[0]
        self.assertEqual((appt["patient_id"], appt["start_time"]), (pid, "16:00"))
        self.assertEqual(len(self.activity("auto_booked")), 1)

    def test_the_commit_adds_the_senders_number_when_the_slots_carry_none(self):
        # an older caller of the automatic path (the `unattended` slot) that never passed a phone
        slots = {"patient_id": None, "patient_name": "Raju Kumar", "patient_phone": None, "appt_date": TOMORROW,
                 "start_time": "16:00", "duration_minutes": None, "via": "conversation"}
        outcome = auto_actions.handle_request(
            self.conn, intent="book_appointment", slots=slots, wa_id=WA2, patient_id=None, patient_name="Raju Kumar",
            msg_id=1, now=CLOCK_NOW, handlers=clinic_app.HANDLERS)
        self.assertEqual(outcome.kind, "committed", outcome)
        self.assertEqual(self.rows("SELECT patient_phone FROM appointments")[0]["patient_phone"], WA2[-10:])

    def test_a_registered_patient_whose_number_is_unusable_goes_to_staff_with_the_reason(self):
        # a sender whose number is short enough to match a patient stored with the same short number
        pid = self.conn.execute("INSERT INTO patients (name, phone) VALUES ('Anita Rao', '12345')").lastrowid
        self.conn.commit()
        slots = {"patient_id": pid, "patient_name": None, "patient_phone": None, "appt_date": TOMORROW, "start_time": "16:00",
                 "duration_minutes": None}
        decision = auto_policy.evaluate(self.conn, "book_appointment", slots, "12345", CLOCK_NOW)
        self.assertEqual((decision.auto, decision.code), (False, "no_phone"))
        outcome = auto_actions.handle_request(
            self.conn, intent="book_appointment", slots=slots, wa_id="12345", patient_id=pid, patient_name="Anita Rao",
            msg_id=2, now=CLOCK_NOW, handlers=clinic_app.HANDLERS)
        self.assertEqual((outcome.kind, outcome.code), ("escalate", "no_phone"))
        self.assertEqual(self.count("appointments"), 0)

    def test_the_commit_level_refusal_is_an_escalation_not_a_slot_taken_retry(self):
        # even if the policy let it through, the handler's refusal must not be read as "that time was taken"
        pid = self.conn.execute("INSERT INTO patients (name, phone) VALUES ('Anita Rao', '12345')").lastrowid
        self.conn.commit()
        slots = {"patient_id": pid, "appt_date": TOMORROW, "start_time": "16:00", "duration_minutes": None}
        with patch.object(auto_policy, "evaluate", return_value=auto_policy.Decision(True, None, None)):
            outcome = auto_actions.handle_request(
                self.conn, intent="book_appointment", slots=slots, wa_id="12345", patient_id=pid, patient_name="Anita Rao",
                msg_id=3, now=CLOCK_NOW, handlers=clinic_app.HANDLERS)
        self.assertEqual((outcome.kind, outcome.code), ("escalate", "no_phone"))
        self.assertEqual(self.count("proposals") and self.conn.execute("SELECT status FROM proposals").fetchone()[0], "rejected")

    def test_the_older_inbox_proposal_for_an_unregistered_sender_already_carries_the_number(self):
        from clinic.whatsapp_pipeline import classify_text_message
        from clinic.adapters.local_sqlite import LocalSQLiteAdapter
        with patch("clinic.whatsapp_pipeline.extract_name", return_value=None):      # never the local model
            result = classify_text_message(self.conn, WA2, "I want to book an appointment tomorrow at 4 pm", LocalSQLiteAdapter(),
                                           now=CLOCK_NOW)
        self.assertEqual(result["intent"], "book_appointment")
        self.assertEqual(result["slots"]["patient_phone"], WA2[-10:])


class FollowUpsRefuseTheRowNotTheBatch(FollowupCase):
    def test_the_plan_flags_the_row_before_apply(self):
        no_phone = self.add_patient("No Phone", "12345")
        planned = followups.plan(self.conn, [self.row(no_phone), self.row(self.staff_patient, due_time="11:00")], now=at(5, 9))
        self.assertEqual([r["ok"] for r in planned["rows"]], [False, True])
        self.assertEqual(planned["rows"][0]["errors"], [PATIENT_NO_PHONE])
        self.assertEqual(planned["counts"], {"total": 2, "valid": 1, "invalid": 1})

    def test_apply_reports_the_refused_row_and_books_the_others(self):
        no_phone = self.add_patient("No Phone", "")
        result = self.schedule(self.row(no_phone), self.row(self.staff_patient, due_time="11:00"), expect=False)
        self.assertEqual(result["counts"], {"created": 1, "failed": 1})
        refused = result["results"][0]
        self.assertEqual((refused["ok"], refused["error"], refused["patient"]), (False, PATIENT_NO_PHONE, "No Phone"))
        self.assertTrue(result["results"][1]["ok"])
        self.assertEqual(count(self.conn, "appointments"), 1)
        self.assertEqual(count(self.conn, "followups"), 1)

    def test_the_handler_is_the_second_line_of_defence(self):
        # a number that goes bad between the plan and the booking is still refused by the booking handler
        no_phone = self.add_patient("Late Phone", "9876543299")
        real = followups._validate

        def stale(conn, row, index, now, taken):
            checked = real(conn, row, index, now, taken)
            conn.execute("UPDATE patients SET phone = '12345' WHERE id = ?", (no_phone,))
            conn.commit()
            return checked

        with patch.object(followups, "_validate", stale):
            result = self.schedule(self.row(no_phone), expect=False)
        self.assertEqual((result["results"][0]["ok"], result["results"][0]["error"]), (False, PATIENT_NO_PHONE))
        self.assertEqual(count(self.conn, "appointments"), 0)


class VoiceAsksForThePhone(DialogTestCase):
    def setUp(self):
        super().setUp()
        names = ["रवि कुमार", "Ravi Kumar", "Anita Rao", "Sunita Devi"]

        def fake(text):
            found = [n for n in names if n.lower() in text.lower()]
            return max(found, key=len) if found else None

        patcher = patch("clinic.nlu.parser.extract_name", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ask_for_ravi(self, language="en-IN", text="book Ravi Kumar tomorrow at 5"):
        ask = self.say(text, language)
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.kind, "phone")
        return ask

    def test_an_unregistered_name_is_asked_for_a_phone_in_each_language(self):
        self.assertEqual(self.ask_for_ravi().question, "What is the patient's phone number?")
        self.ctx.clear()
        self.assertEqual(self.ask_for_ravi("hi-IN").question, "Patient ka phone number kya hai?")
        self.ctx.clear()
        self.assertEqual(self.ask_for_ravi("hi-IN", "रवि कुमार को कल शाम 5 बजे बुक करो").question, "मरीज़ का फ़ोन नंबर क्या है?")

    def test_a_spoken_number_lands_in_the_cards_slots(self):
        self.ask_for_ravi()
        card = self.say("9876500301")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual((card.intent, card.slots["patient_phone"], card.slots["patient_name"]), ("book_appointment", "9876500301", "Ravi Kumar"))
        self.assertNotIn("phone_problem", card.resolved)

    def test_the_ways_a_number_is_said(self):
        for said in ("my phone number is 98765 00301", "+91 9876500301", "९८७६५००३०१", "mera number 98765-00301 hai",
                     "nine eight seven six five zero zero three zero one", "नौ आठ सात छह पांच शून्य शून्य तीन शून्य एक"):
            self.ctx.clear()
            self.ask_for_ravi()
            card = self.say(said)
            self.assertIsInstance(card, ParsedResult, said)
            self.assertEqual(card.slots["patient_phone"], "9876500301", said)

    def test_a_bad_answer_is_asked_again_once_then_let_go(self):
        self.ask_for_ravi()
        again = self.say("98765")                                   # a digit dropped: not a phone number
        self.assertIsInstance(again, AskResult)
        self.assertEqual(again.kind, "phone")
        with self.assertRaises(PipelineError) as raised:
            self.say("I don't know")
        self.assertIn("stopped asking", str(raised.exception))
        self.assertIsNone(self.ctx.pending)

    def test_never_mind_drops_the_question(self):
        self.ask_for_ravi()
        note = self.say("never mind")
        self.assertIsInstance(note, Note)
        self.assertIsNone(self.ctx.pending)

    def test_a_number_on_the_command_itself_is_not_asked_for_again(self):
        card = self.say("book Ravi Kumar tomorrow at 5 phone 9876500301")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.slots["patient_phone"], "9876500301")

    def test_a_short_number_on_the_command_is_asked_again(self):
        ask = self.say("book Ravi Kumar tomorrow at 5 phone 98765432")
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.kind, "phone")

    def test_a_registered_patient_with_a_valid_phone_is_not_asked(self):
        card = self.say("book Sunita Devi tomorrow at 5")
        self.assertIsInstance(card, ParsedResult)
        self.assertNotIn("phone_problem", card.resolved)

    def test_a_registered_patient_whose_number_on_file_is_unusable_is_asked(self):
        self.conn.execute("INSERT INTO patients (name, phone) VALUES ('Anita Rao', '12345')")
        self.conn.commit()
        ask = self.say("book Anita Rao tomorrow at 5")
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.kind, "phone")
        card = self.say("9876500302")
        self.assertEqual(card.slots["patient_phone"], "9876500302")

    def test_asking_for_a_phone_is_not_skippable(self):
        self.ask_for_ravi()
        again = self.say("skip")
        self.assertIsInstance(again, AskResult)
        self.assertEqual(again.kind, "phone")

    def test_a_card_that_still_has_no_phone_is_flagged_from_the_start(self):
        # no voice context (a typed one-shot): no question can be asked, the card says so itself
        from clinic.pipeline import transcript_to_response
        card = transcript_to_response(self.conn, "book Ravi Kumar tomorrow at 5", self.adapter, self.adapter, "en-IN",
                                      frozenset({"book_appointment"}))
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.resolved["phone_problem"], REQUIRED)
        registered = transcript_to_response(self.conn, "book Sunita Devi tomorrow at 5", self.adapter, self.adapter, "en-IN",
                                            frozenset({"book_appointment"}))
        self.assertNotIn("phone_problem", registered.resolved)

    def test_the_spoken_phone_reader(self):
        self.assertEqual(spoken_phone("9876500301"), "9876500301")
        self.assertEqual(spoken_phone("zero nine eight seven six five zero zero three zero one"), "9876500301")
        for said in ("", "hello", "98765", "98765003011", "987650030"):
            self.assertIsNone(spoken_phone(said), said)


class ThePlannerEndsInTheQuestion(PlannerCase):
    def test_a_planner_booking_with_no_phone_for_an_unregistered_name_asks_for_it(self):
        from clinic.realtime_voice import VoiceSession
        emitted = []
        session = VoiceSession("sid", "key", lambda event, data: emitted.append((event, data)), lambda: self.conn, self.adapter,
                               self.adapter, frozenset({"book_appointment", "cancel_appointment", "reschedule_appointment"}))
        session.context = self.ctx
        self.call("book_appointment", patient_name="Ravi Kumar", date=self.tomorrow.isoformat(), time="17:00")
        session._handle_final_transcript("get Ravi Kumar a seat tomorrow evening")
        events = dict(emitted)
        self.assertNotIn("review_card", events)
        self.assertEqual((events["assistant_question"]["kind"], events["assistant_question"]["question"]),
                         ("phone", "Patient ka phone number kya hai?"))       # the voice page's default language is Hindi (Hinglish)
        self.call("book_appointment", patient_name="Ravi Kumar", date=self.tomorrow.isoformat(), time="17:00", phone="9876500301")
        self.ctx.clear()
        emitted.clear()
        session._handle_final_transcript("get Ravi Kumar a seat tomorrow evening, 9876500301")
        self.assertEqual(dict(emitted)["review_card"]["slots"]["patient_phone"], "9876500301")


class TheScreens(unittest.TestCase):
    """Wiring tests (these read the files; the pure rule is tests/booking_phone.test.js)."""

    def read(self, *parts):
        return ROOT.joinpath(*parts).read_text(encoding="utf-8")

    def test_the_review_card_marks_the_phone_required_and_blocks_an_invalid_submit(self):
        card = self.read("static", "review_card.js")
        self.assertIn("Phone (required)", card)
        self.assertIn("BookingPhone", card)
        self.assertIn("function wirePhone", card)
        self.assertIn("_checkPhone(true)", card)
        self.assertIn("field-error", card)
        approve = card[card.index('approveBtn.addEventListener("click"'):]
        self.assertLess(approve.index("_checkPhone(true)"), approve.index("send(body)"))      # checked before anything is sent
        self.assertNotIn("innerHTML", card)

    def test_the_helper_is_loaded_before_the_card(self):
        html = self.read("templates", "dashboard.html")
        self.assertLess(html.index("booking_phone.js"), html.index("review_card.js"))

    def test_the_inbox_and_the_voice_card_hand_the_server_flag_to_the_card(self):
        self.assertIn("phone_problem", self.read("static", "wa_inbox.js"))
        self.assertIn("data.resolved", self.read("static", "live_voice.js"))               # the whole `resolved` object reaches build()

    def test_the_staff_form_shows_the_phone_when_the_booking_needs_it(self):
        html, js = self.read("templates", "dashboard.html"), self.read("static", "queue_edit.js")
        self.assertIn('id="new-appt-phone-label"', html)
        self.assertIn('data-phone="{{ p.phone }}"', html)
        self.assertIn("BookingPhone", js)
        self.assertIn("patientPhoneOnFile", js)

    def test_the_helper_and_the_server_use_the_same_words(self):
        helper = self.read("static", "booking_phone.js")
        self.assertIn(REQUIRED, helper)
        self.assertIn(PATIENT_NO_PHONE, helper)


class NodeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_pure_helper(self):
        out = subprocess.run(["node", str(ROOT / "tests" / "booking_phone.test.js")], capture_output=True, text=True,
                             cwd=str(ROOT), timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("passed", out.stdout)


if __name__ == "__main__":
    unittest.main()
