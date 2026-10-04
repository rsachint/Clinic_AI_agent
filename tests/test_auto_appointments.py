"""Automatic patient-initiated appointment actions, end to end through the
real Flask routes (Flask's test client, in-process; the WhatsApp senders are
recording fakes and the real client functions are tripwired -- nothing can
reach WhatsApp or Google).

The invariant these tests protect (it replaces the old "the agent never writes
anything"): writes happen ONLY through the guarded automatic path, ONLY for
book / cancel / reschedule, and every automatic write has an audit_log row, a
patient_activity row and an [auto:whatsapp-agent] proposal."""
import contextlib
import json
import os
import sys
import threading
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_wa_agent_routes import (  # noqa: E402  (also stubs load_dotenv and sets the safe env)
    AgentRouteCase, AgentSender, CLOCK_NOW, TOMORROW, WA, WA2, clinic_app)
from tests.test_conversation import SAY  # noqa: E402
from tests.fake_gcal import FakeCalendar  # noqa: E402

from clinic import (auto_actions, auto_policy, booking_blocks, conv_templates as ct, core, conversation as cv, db, gcal_client,
                    gcal_config, notify, patient_activity, scheduling, settings)  # noqa: E402

FRIDAY = "2026-10-09"
WA_FOR = {"en": "919800000001", "hi": "919800000002", "hinglish": "919800000003"}


class AutoCase(AgentRouteCase):
    AUTOMATION = True

    # -- fixtures --------------------------------------------------------------
    def patient(self, wa_id=WA, name="Sunita Devi"):
        pid = self.conn.execute("INSERT INTO patients (name, phone) VALUES (?, ?)", (name, wa_id[-10:])).lastrowid
        self.conn.commit()
        return pid

    def appt(self, pid=None, date_=TOMORROW, time="09:00", name=None, phone=None, status="booked"):
        cur = self.conn.execute(
            "INSERT INTO appointments (patient_id, patient_name, patient_phone, appt_date, start_time, status) "
            "VALUES (?, ?, ?, ?, ?, ?)", (pid, name, phone, date_, time, status))
        self.conn.commit()
        return cur.lastrowid

    def block(self, start, end=None, t0=None, t1=None, reason="Doctor on leave"):
        return booking_blocks.add_block(self.conn, start, end, t0, t1, reason)["id"]

    # -- reading state -----------------------------------------------------------
    def rows(self, sql, *args):
        return [dict(r) for r in self.conn.execute(sql, args)]

    def status_of(self, aid):
        return self.conn.execute("SELECT status FROM appointments WHERE id=?", (aid,)).fetchone()[0]

    def activity(self, *events):
        marks = ",".join("?" for _ in events)
        return self.rows("SELECT * FROM patient_activity WHERE event IN ({}) ORDER BY id".format(marks), *events) if events \
            else self.rows("SELECT * FROM patient_activity ORDER BY id")

    def autos(self):
        return self.rows("SELECT * FROM proposals WHERE source_text LIKE '[auto:whatsapp-agent]%' ORDER BY id")

    def assert_one_auto_write(self, intent, event):
        """The new invariant for exactly one automatic write: [auto:] proposal
        -> confirmed -> audit row -> activity row."""
        autos = self.autos()
        self.assertEqual(len(autos), 1, autos)
        self.assertEqual((autos[0]["intent"], autos[0]["status"]), (intent, "confirmed"))
        audits = self.rows("SELECT * FROM audit_log WHERE proposal_id = ?", autos[0]["id"])
        self.assertEqual([a["intent"] for a in audits], [intent])
        acts = self.activity(event)
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0]["proposal_id"], autos[0]["id"])
        self.assertEqual(self.activity("requested")[0]["event"], "requested")
        return autos[0], audits[0], acts[0]

    def not_a_staff_item(self):
        self.assertEqual(self.inbox(), [])

    @contextlib.contextmanager
    def stale_view(self, times):
        """The dialog's list of free times is out of date for ONE call -- the
        patient confirmed a time that looked free a moment ago (the real race
        window between the dialog's own check and the commit)."""
        real = cv.free_times
        state = {"calls": 0}

        def fake(conn, iso, now, for_wa_id=None):
            state["calls"] += 1
            return list(times) if state["calls"] == 1 else real(conn, iso, now, for_wa_id=for_wa_id)

        with mock.patch.object(cv, "free_times", fake):
            yield


class AutoDialogueTests(AutoCase):
    """The full dialogue, per language, ending in an automatic write."""

    def test_booking_writes_the_proposal_audit_and_activity_rows(self):
        self.book_through_conversation()
        proposal, audit, act = self.assert_one_auto_write("book_appointment", "auto_booked")
        self.assertTrue(proposal["source_text"].startswith("[auto:whatsapp-agent]"))
        self.assertEqual(json.loads(audit["payload_json"])["start_time"], "16:00")
        self.assertEqual((act["source"], act["patient_name"], act["wa_id"]), ("whatsapp-agent", "Sunita Devi", WA))
        self.assertEqual(act["appointment_id"], audit["entity_id"])
        self.assertEqual([a["event"] for a in self.activity()], ["requested", "auto_booked"])
        self.assertEqual(self.count("slot_holds"), 0)                     # holds are only for the escalated path
        row = self.last_row()
        self.assertEqual((row["status"], row["agent_handled"], row["proposal_id"]), ("dismissed", 1, proposal["id"]))
        self.not_a_staff_item()
        self.assertEqual(self.events().count("booking_confirmed"), 1)

    def test_a_registered_patient_is_not_asked_for_a_name(self):
        pid = self.patient()
        self.say("I want to book an appointment")
        self.say("tomorrow")
        self.say("4 pm")
        self.tap("confirm:yes", "Confirm request")
        appt = self.rows("SELECT * FROM appointments")[0]
        self.assertEqual((appt["patient_id"], appt["patient_name"], appt["start_time"]), (pid, None, "16:00"))
        self.assertEqual(self.activity("auto_booked")[0]["patient_id"], pid)
        self.assertNotIn(ct.text("ask_name", "en"), self.sender.texts)

    def test_a_second_yes_after_the_booking_changes_nothing(self):
        self.book_through_conversation()
        self.sender.calls.clear()
        self.say("yes")
        self.tap("confirm:yes", "Confirm request")
        self.assertEqual(self.sender.texts, [ct.text("already_done", "en")] * 2)
        self.assertEqual(self.count("appointments"), 1)
        self.assertEqual(len(self.autos()), 1)

    def test_a_cancel_minutes_before_the_slot_is_still_automatic(self):
        pid = self.patient()
        aid = self.appt(pid, "2026-10-05", "10:05")                         # CLOCK is 10:00 on 5 Oct
        self.say("cancel my appointment")
        self.tap("confirm:yes", "Yes")
        self.assertEqual(self.status_of(aid), "cancelled")
        self.assert_one_auto_write("cancel_appointment", "auto_cancelled")
        self.not_a_staff_item()

    def test_an_unknown_number_is_auto_booked_with_its_name_and_phone_and_capped_at_three(self):
        for i, t in enumerate(("9 am", "10 am", "11 am")):
            self.book_through_conversation(wa_id=WA2, name="Raju Kumar", time=t)
            self.assertEqual(self.count("appointments"), i + 1)
        appt = self.rows("SELECT * FROM appointments ORDER BY id")[0]
        self.assertEqual((appt["patient_id"], appt["patient_name"], appt["patient_phone"]), (None, "Raju Kumar", WA2[-10:]))
        self.sender.calls.clear()
        self.say("I want to book an appointment", WA2)
        self.assertEqual(self.sender.texts, [ct.text("cap_reached", "en", n=3)])    # the dialog stops a 4th before it starts
        self.assertEqual(self.count("appointments"), 3)

    def test_the_policy_cap_per_number_also_holds_if_the_dialog_is_bypassed(self):
        for t in ("09:00", "09:15", "09:30"):
            self.appt(None, TOMORROW, t, name="Raju", phone=WA2[-10:])
        decision = auto_policy.evaluate(self.conn, "book_appointment", {
            "patient_id": None, "patient_name": "Raju", "appt_date": TOMORROW, "start_time": "16:00"}, WA2, CLOCK_NOW)
        self.assertEqual(decision.code, "number_cap")

    def test_the_patients_only_confirmation_is_the_normal_notification(self):
        """If that confirmation cannot be queued, a fixed-template fallback is sent instead (never nothing)."""
        with mock.patch.object(notify, "notify_appointment", return_value=None):
            self.book_through_conversation()
        self.assertEqual(self.sender.texts[-1], ct.text("auto_done_book", "en", date=notify.format_date(TOMORROW, "en"),
                                                        time=notify.format_time("16:00", "en")))
        self.assertEqual(self.count("appointments"), 1)

    def test_voice_notes_go_through_the_same_automatic_path(self):
        message = {"from": WA, "id": self.mid(), "type": "audio", "audio": {"id": "media1"}}
        for p in (mock.patch.object(clinic_app.wa, "download_media", return_value=b"ogg"),
                  mock.patch.object(clinic_app, "transcribe", return_value=mock.Mock(text="I want to book an appointment"))):
            p.start()
            self.addCleanup(p.stop)
        self.post(message)
        self.say("Sunita Devi")
        self.say("tomorrow")
        self.say("4 pm")
        self.tap("confirm:yes", "Confirm request")
        self.assertEqual(self.count("appointments"), 1)
        self.assert_one_auto_write("book_appointment", "auto_booked")

    def test_a_symptom_mentioned_in_a_booking_still_reaches_staff_even_though_the_booking_is_automatic(self):
        self.say("I have fever, I want to book an appointment tomorrow 4 pm")
        self.say("Sunita Devi")
        self.tap("confirm:yes", "Confirm request")
        self.assertEqual(self.count("appointments"), 1)
        card = self.inbox()
        self.assertEqual(len(card), 1)
        self.assertEqual(card[0]["slots"]["flag"], "clinical")                      # the call-back request is not lost
        self.assertEqual(card[0]["status"], "needs_human_reply")

    def test_a_clinical_question_in_the_middle_never_writes(self):
        self.say("I want to book an appointment")
        self.say("which tablet should I take for fever")
        self.say("I have chest pain")
        self.assertEqual(self.count("appointments"), 0)
        self.assertEqual(self.autos(), [])


def _lang_book(lang):
    def test(self):
        say, wa = SAY[lang], WA_FOR[lang]
        self.say(say["book"], wa)
        self.say(say["name"], wa)
        self.say(say["tomorrow"], wa)
        self.say(say["t4pm"], wa)
        self.assertEqual(self.count("appointments"), 0)                      # nothing is written before the patient confirms
        self.assertEqual(self.autos(), [])
        self.tap("confirm:yes", "x", wa)
        appt = self.rows("SELECT * FROM appointments")[0]
        self.assertEqual((appt["status"], appt["patient_phone"], appt["appt_date"], appt["start_time"]),
                         ("booked", wa[-10:], TOMORROW, "16:00"))
        confirmation = notify.render("booking_confirmed_future", lang, name=say["name"], token=1, date=TOMORROW, time="16:00")
        self.assertEqual(self.sender.texts[-1], confirmation)
        self.assertEqual(self.sender.texts.count(confirmation), 1)           # one confirmation, in the patient's language
        self.assertEqual(len(self.sender.calls), 5)                          # name, day, time, summary -- then the confirmation
        self.assertNotIn(ct.text("handoff_received", lang), self.sender.texts)   # the old interim message is gone
        self.not_a_staff_item()
        self.assertEqual(cv.load_session(self.conn, wa, CLOCK_NOW)["step"], "done")
        self.assert_one_auto_write("book_appointment", "auto_booked")
    return test


def _lang_cancel(lang):
    def test(self):
        say, wa = SAY[lang], WA_FOR[lang]
        pid = self.patient(wa, "Sunita Devi")
        aid = self.appt(pid, TOMORROW, "09:00")
        self.say(say["cancel"], wa)
        self.assertEqual(self.status_of(aid), "booked")                      # asking is not cancelling
        self.tap("confirm:yes", "x", wa)
        self.assertEqual(self.status_of(aid), "cancelled")
        self.assertEqual(self.sender.texts[-1], notify.render(
            "appointment_cancelled", lang, name="Sunita Devi", date=TOMORROW, time="09:00"))
        self.assertEqual(len(self.sender.calls), 2)                          # the yes/no question, then the confirmation
        self.assert_one_auto_write("cancel_appointment", "auto_cancelled")
        self.not_a_staff_item()
    return test


def _lang_reschedule(lang):
    def test(self):
        say, wa = SAY[lang], WA_FOR[lang]
        pid = self.patient(wa, "Sunita Devi")
        aid = self.appt(pid, TOMORROW, "09:00")
        self.say(say["resched"], wa)
        self.say(say["friday"], wa)
        self.say(say["t4pm"], wa)
        row = self.conn.execute("SELECT appt_date FROM appointments WHERE id=?", (aid,)).fetchone()
        self.assertEqual(row["appt_date"], TOMORROW)
        self.tap("confirm:yes", "x", wa)
        row = self.conn.execute("SELECT appt_date, start_time FROM appointments WHERE id=?", (aid,)).fetchone()
        self.assertEqual((row["appt_date"], row["start_time"]), (FRIDAY, "16:00"))
        self.assertEqual(self.sender.texts[-1], notify.render(
            "appointment_rescheduled_future", lang, name="Sunita Devi", token=1, date=FRIDAY, time="16:00"))
        _, _, act = self.assert_one_auto_write("reschedule_appointment", "auto_rescheduled")
        self.assertEqual(json.loads(act["meta_json"])["old_date"], TOMORROW)
        self.not_a_staff_item()
    return test


for _lang in SAY:
    setattr(AutoDialogueTests, "test_booking_" + _lang, _lang_book(_lang))
    setattr(AutoDialogueTests, "test_cancel_" + _lang, _lang_cancel(_lang))
    setattr(AutoDialogueTests, "test_reschedule_" + _lang, _lang_reschedule(_lang))


class BlockedSlotDialogueTests(AutoCase):
    def offered_times(self, call=-1):
        interactive = self.sender.calls[call][2] or {}
        return [o["title"] for o in (interactive.get("rows") or interactive.get("buttons") or [])]

    def test_blocked_slots_are_never_offered(self):
        self.block(TOMORROW, t0="09:00", t1="13:00")
        self.say("I want to book an appointment")
        self.say("Sunita Devi")
        self.say("tomorrow")
        offered = self.offered_times()
        self.assertTrue(offered)
        for title in offered:
            self.assertIn("PM", title)                       # the whole morning is blocked
        for row in self.sender.calls[-1][2]["rows"]:
            self.assertGreaterEqual(row["id"].split("T")[1], "16:00")

    def test_a_typed_time_inside_a_block_is_rejected_with_the_fixed_template_and_alternatives(self):
        self.block(TOMORROW, t0="16:00", t1="17:00")
        self.say("book tomorrow 4 pm")
        self.say("Sunita Devi")
        text = self.sender.texts[-1]
        self.assertEqual(text.split("\n")[0], ct.text("time_blocked", "en", time=notify.format_time("16:00", "en"),
                                                      date=notify.format_date(TOMORROW, "en")))
        self.assertIn("isn't taking appointments", text)
        self.assertTrue(self.sender.calls[-1][2])            # alternatives are attached
        self.assertEqual(self.count("appointments"), 0)
        blocked = self.activity("blocked")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["source"], "whatsapp-agent")
        self.assertIn("16:00", blocked[0]["detail"])

    def test_a_whole_blocked_day_offers_the_next_day(self):
        self.block(TOMORROW, reason="Renovation")
        self.say("book tomorrow")
        self.say("Sunita Devi")
        self.assertEqual(self.sender.texts[-1].split("\n")[0], ct.text(
            "day_blocked", "en", date=notify.format_date(TOMORROW, "en"), next_date=notify.format_date("2026-10-07", "en")).split("\n")[0])
        self.assertEqual(len(self.activity("blocked")), 1)

    def test_blocked_slots_also_leave_the_reschedule_offers(self):
        pid = self.patient()
        self.appt(pid, TOMORROW, "09:00")
        self.block(FRIDAY, t0="09:00", t1="12:00")
        self.say("reschedule my appointment")
        self.say("friday")
        for row in self.sender.calls[-1][2]["rows"]:
            self.assertGreaterEqual(row["id"].split("T")[1], "12:00")

    def test_a_block_added_after_the_patient_picked_a_time_is_caught_at_commit_and_other_times_are_offered(self):
        self.say("book tomorrow 4 pm")
        self.say("Sunita Devi")
        self.assertIn("Please confirm", self.sender.texts[-1])
        self.block(TOMORROW, t0="16:00", t1="16:30")             # staff blocks it while the patient is deciding
        self.tap("confirm:yes", "Confirm request")
        self.assertEqual(self.count("appointments"), 0)
        self.assertEqual(self.sender.texts[-1].split("\n")[0], ct.text(
            "slot_blocked", "en", time=notify.format_time("16:00", "en"), date=notify.format_date(TOMORROW, "en")))
        self.assertEqual(self.autos(), [])                        # policy refused before any proposal
        self.not_a_staff_item()
        self.assertEqual([a["event"] for a in self.activity("blocked")], ["blocked"])
        self.tap("slot:{}T16:30".format(TOMORROW), "4:30 PM", kind="list_reply")
        self.tap("confirm:yes", "Confirm request")
        self.assertEqual(self.rows("SELECT start_time FROM appointments")[0]["start_time"], "16:30")

    def test_the_write_handler_itself_refuses_a_blocked_slot_even_if_the_policy_is_bypassed(self):
        self.say("book tomorrow 4 pm")
        self.say("Sunita Devi")
        self.block(TOMORROW, t0="16:00", t1="16:30")
        with self.stale_view(["16:00", "16:15", "16:30"]), \
                mock.patch.object(auto_policy, "evaluate", return_value=auto_policy.Decision(True, None, None)):
            self.tap("confirm:yes", "Confirm request")
        self.assertEqual(self.count("appointments"), 0)
        self.assertEqual(self.count("audit_log"), 0)
        self.assertEqual([p["status"] for p in self.autos()], ["rejected"])      # the attempt is on record, nothing was written
        self.assertEqual(self.sender.texts[-1].split("\n")[0], ct.text(
            "slot_blocked", "en", time=notify.format_time("16:00", "en"), date=notify.format_date(TOMORROW, "en")))


class EscalationTests(AutoCase):
    def needs_staff_card(self):
        inbox = self.inbox()
        self.assertEqual(len(inbox), 1)
        return inbox[0]

    def test_daily_cap_overflow_goes_to_staff_with_the_reason_on_the_card(self):
        settings.set_auto_daily_cap(self.conn, 1)
        self.book_through_conversation(wa_id=WA, name="Sunita Devi", time="4 pm")
        self.assertEqual(self.count("appointments"), 1)
        self.sender.calls.clear()
        row = self.book_through_conversation(wa_id=WA2, name="Raju", time="5 pm")
        self.assertEqual((row["intent"], row["status"], row["agent_handled"]), ("book_appointment", "classified", 0))
        self.assertEqual(self.count("appointments"), 1)                          # not booked
        card = self.needs_staff_card()
        self.assertIn("daily automation cap reached", card["slots"]["needs_staff_reason"])
        self.assertEqual(self.sender.texts[-1], ct.text("handoff_received", "en"))     # the patient is told the clinic will confirm
        self.assertEqual(self.count("slot_holds"), 1)                              # holds still work on the escalated path
        esc = self.activity("escalated")
        self.assertEqual(len(esc), 1)
        self.assertIn("daily automation cap", esc[0]["detail"])
        self.assertEqual(len(self.autos()), 1)                                     # only the first was automatic

    def test_the_card_shows_the_reason_and_staff_can_still_approve_it(self):
        settings.set_auto_daily_cap(self.conn, 0)
        row = self.book_through_conversation()
        slots = json.loads(row["slots_json"])
        result = self.client.post("/wa/{}/approve".format(row["id"]), json={"slots": {
            "patient_id": None, "patient_name": slots["patient_name"], "patient_phone": slots["patient_phone"],
            "appt_date": slots["appt_date"], "start_time": slots["start_time"], "duration_minutes": None,
            "notes": slots["notes"]}}).get_json()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.count("appointments"), 1)
        self.assertEqual(self.autos(), [])                                          # the human's approval is NOT an [auto:] write
        self.assertEqual(self.count("slot_holds"), 0)

    def test_switch_off_restores_the_old_behaviour(self):
        settings.set_auto_enabled(self.conn, False)
        row = self.book_through_conversation()
        self.assertEqual((row["intent"], row["status"]), ("book_appointment", "classified"))
        self.assertNotIn("needs_staff_reason", json.loads(row["slots_json"]))
        self.assertEqual(self.sender.texts[-1], ct.text("handoff_received", "en"))
        self.assertEqual((self.count("appointments"), self.count("proposals"), self.count("audit_log")), (0, 0, 0))

    def test_switch_via_the_endpoint_takes_effect_immediately(self):
        self.assertTrue(self.client.post("/automation/settings", json={"enabled": False}).get_json()["ok"])
        row = self.book_through_conversation()
        self.assertEqual(row["status"], "classified")
        self.client.post("/automation/settings", json={"enabled": True})
        row = self.book_through_conversation(wa_id=WA2, name="Raju", time="5 pm")
        self.assertEqual(row["status"], "dismissed")

    def test_human_takeover_means_nothing_is_committed(self):
        cv.set_mode(self.conn, WA, "human", now=CLOCK_NOW)
        self.say("book tomorrow 4 pm")
        self.assertEqual(self.sender.calls, [])
        self.assertEqual(self.count("appointments"), 0)
        self.assertEqual(json.loads(self.last_row()["slots_json"])["flag"], "human_mode")
        # the policy refuses on its own too, even if something drove the finished request
        self.assertEqual(auto_policy.evaluate(self.conn, "book_appointment", {
            "patient_id": None, "patient_name": "x", "appt_date": TOMORROW, "start_time": "16:00"}, WA, CLOCK_NOW).code, "human_mode")

    def test_taking_over_mid_conversation_stops_automation(self):
        self.say("book tomorrow 4 pm")
        self.say("Sunita Devi")
        cv.set_mode(self.conn, WA, "human", now=CLOCK_NOW)
        self.tap("confirm:yes", "Confirm request")
        self.assertEqual(self.count("appointments"), 0)

    def test_cancelling_an_appointment_that_is_already_checked_in_goes_to_staff(self):
        pid = self.patient()
        aid = self.appt(pid, "2026-10-05", "10:30")
        self.conn.execute("UPDATE appointments SET queue_state='checked_in' WHERE id=?", (aid,))
        self.conn.commit()
        self.say("cancel my appointment")
        self.tap("confirm:yes", "Yes")
        self.assertEqual(self.status_of(aid), "booked")
        card = self.needs_staff_card()
        self.assertIn("checked in", card["slots"]["needs_staff_reason"])
        self.assertEqual(self.sender.texts[-1], ct.text("handoff_received", "en"))
        self.assertEqual(self.count("slot_holds"), 0)                              # a cancel holds nothing

    def test_the_agent_switch_still_works_and_never_auto_commits(self):
        with mock.patch.dict(os.environ, {"WHATSAPP_AGENT_ENABLED": "0"}):
            self.say("mujhe appointment chahiye")
        self.assertEqual(self.count("appointments"), 0)
        self.assertEqual(self.autos(), [])
        self.assertEqual(self.last_row()["status"], "classified")
        events = [a["event"] for a in self.activity()]
        self.assertEqual(events, ["requested", "escalated"])                        # still logged

    def test_a_stale_backlog_message_is_never_auto_committed(self):
        self.say("book tomorrow 4 pm", timestamp="1000000000")
        self.assertEqual(self.count("appointments"), 0)
        self.assertEqual(self.autos(), [])

    def test_a_request_the_policy_cannot_evaluate_goes_to_staff(self):
        self.say("book tomorrow 4 pm")
        self.say("Sunita Devi")
        with mock.patch.object(auto_policy, "evaluate", side_effect=RuntimeError("boom")):
            self.tap("confirm:yes", "Confirm request")
        self.assertEqual(self.count("appointments"), 0)
        self.assertEqual(self.needs_staff_card()["intent"], "book_appointment")

    def test_a_write_that_fails_at_commit_goes_to_staff_not_to_a_crash(self):
        self.say("book tomorrow 4 pm")
        self.say("Sunita Devi")
        broken = dict(clinic_app.HANDLERS, book_appointment=mock.Mock(side_effect=RuntimeError("disk full")))
        with mock.patch.object(clinic_app, "HANDLERS", broken):
            self.tap("confirm:yes", "Confirm request")
        self.assertEqual(self.count("appointments"), 0)
        card = self.needs_staff_card()
        self.assertIn("failed", card["slots"]["needs_staff_reason"])
        self.assertEqual(self.sender.texts[-1], ct.text("handoff_received", "en"))
        self.assertEqual([p["status"] for p in self.autos()], ["rejected"])


class ConflictTests(AutoCase):
    def take_4pm(self, day=TOMORROW):
        self.appt(None, day, "16:00", name="Walk-in", phone="9000000001")      # staff books it meanwhile

    def assert_friendly_alternatives(self, day=TOMORROW):
        self.assertEqual(self.sender.texts[-1].split("\n")[0], ct.text(
            "slot_just_taken", "en", time=notify.format_time("16:00", "en"), date=notify.format_date(day, "en")))
        interactive = self.sender.calls[-1][2]
        options = [o["id"].split("T")[1] for o in (interactive.get("rows") or interactive.get("buttons") or [])]
        self.assertTrue(options)
        self.assertNotIn("16:00", options)
        self.assertEqual(cv.load_session(self.conn, WA, CLOCK_NOW)["step"], "time")
        self.assertEqual(self.inbox(), [])                                         # not an escalation
        self.assertEqual(self.autos(), self.autos())

    def test_a_slot_taken_between_the_summary_and_the_confirm_gets_alternatives_not_an_escalation(self):
        self.say("book tomorrow 4 pm")
        self.say("Sunita Devi")
        self.take_4pm()
        self.tap("confirm:yes", "Confirm request")
        self.assertEqual(self.count("appointments"), 1)
        self.assert_friendly_alternatives()
        self.assertEqual(len(self.activity("conflict")), 1)
        self.assertEqual(self.autos(), [])
        # ...and the patient simply picks another time
        self.tap("slot:{}T16:30".format(TOMORROW), "4:30 PM", kind="list_reply")
        self.tap("confirm:yes", "Confirm request")
        self.assertEqual(self.count("appointments"), 2)
        self.assertEqual(len(self.autos()), 1)

    def test_a_conflict_in_the_instant_before_the_commit_is_caught_by_the_policy(self):
        self.say("book tomorrow 4 pm")
        self.say("Sunita Devi")
        self.take_4pm()
        with self.stale_view(["16:00", "16:15", "16:30"]):
            self.tap("confirm:yes", "Confirm request")
        self.assertEqual(self.count("appointments"), 1)
        self.assert_friendly_alternatives()
        self.assertEqual(self.autos(), [])                                          # refused before any proposal
        self.assertEqual(len(self.activity("conflict")), 1)

    def test_a_conflict_that_only_the_write_handler_sees_is_handled_the_same_way(self):
        self.say("book tomorrow 4 pm")
        self.say("Sunita Devi")
        self.take_4pm()
        with self.stale_view(["16:00", "16:15", "16:30"]), \
                mock.patch.object(auto_policy, "evaluate", return_value=auto_policy.Decision(True, None, None)):
            self.tap("confirm:yes", "Confirm request")
        self.assertEqual(self.count("appointments"), 1)
        self.assert_friendly_alternatives()
        self.assertEqual([p["status"] for p in self.autos()], ["rejected"])
        self.assertEqual(self.count("audit_log"), 0)
        self.assertEqual(self.rows("SELECT status FROM proposals WHERE status='pending'"), [])
        self.assertEqual(len(self.activity("conflict")), 1)

    def test_a_reschedule_conflict_keeps_the_old_appointment_and_offers_other_times(self):
        pid = self.patient()
        aid = self.appt(pid, TOMORROW, "09:00")
        self.say("reschedule my appointment")
        self.say("friday")
        self.say("4 pm")
        self.take_4pm(FRIDAY)
        self.tap("confirm:yes", "Confirm request")
        row = self.conn.execute("SELECT appt_date, start_time FROM appointments WHERE id=?", (aid,)).fetchone()
        self.assertEqual((row["appt_date"], row["start_time"]), (TOMORROW, "09:00"))
        self.assert_friendly_alternatives(FRIDAY)
        self.assertEqual(self.autos(), [])


class ThreadedRaceTests(unittest.TestCase):
    """Two patients asking for the same slot at the same instant (a threaded
    server): exactly one gets it. Real SQLite file, one connection per thread."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "race.db")
        db.connect(self.path).close()
        self.handlers = clinic_app.HANDLERS

    def request(self, conn, wa_id, name, when):
        return auto_actions.handle_request(
            conn, intent="book_appointment", slots={
                "patient_id": None, "patient_name": name, "patient_phone": wa_id[-10:], "appt_date": TOMORROW,
                "start_time": when, "duration_minutes": None, "notes": "race"},
            wa_id=wa_id, patient_id=None, patient_name=name, msg_id=1, now=CLOCK_NOW, handlers=self.handlers,
            after_commit=None)

    def test_automatic_bookings_for_one_slot_never_double_book(self):
        for round_no in range(15):
            slot = scheduling.slot_grid()[round_no]
            barrier = threading.Barrier(2)
            results, errors = [], []

            def worker(wa_id, name):
                conn = db.connect(self.path)
                try:
                    barrier.wait(timeout=5)
                    results.append(self.request(conn, wa_id, name, slot).kind)
                except Exception as exc:     # pragma: no cover - would fail the assertion below
                    errors.append(exc)
                finally:
                    conn.close()

            threads = [threading.Thread(target=worker, args=("9198000{:05d}".format(round_no * 2), "Sunita")),
                       threading.Thread(target=worker, args=("9198000{:05d}".format(round_no * 2 + 1), "Raju"))]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)
            self.assertEqual(errors, [])
            self.assertEqual(sorted(results), ["committed", "retry"], "round {}".format(round_no))
        conn = db.connect(self.path)
        total = conn.execute("SELECT COUNT(*) FROM appointments WHERE status='booked'").fetchone()[0]
        distinct = conn.execute("SELECT COUNT(DISTINCT start_time) FROM appointments WHERE status='booked'").fetchone()[0]
        self.assertEqual((total, distinct), (15, 15))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], 15)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM proposals WHERE status='pending'").fetchone()[0], 0)
        conn.close()

    def test_even_without_the_process_lock_the_confirm_transaction_is_atomic(self):
        """The in-transaction is_slot_free re-check + BEGIN IMMEDIATE: bypass
        the process-level lock entirely (as a second process would) and the
        slot still cannot be double booked."""
        from clinic import core
        for round_no in range(15):
            slot = scheduling.slot_grid()[round_no]
            barrier = threading.Barrier(2)
            outcomes = []

            def worker(name):
                conn = db.connect(self.path)
                try:
                    pid = core.propose(conn, "book_appointment", {
                        "patient_name": name, "patient_phone": "9000000000", "appt_date": "2026-10-20", "start_time": slot}, "race")
                    barrier.wait(timeout=5)
                    try:
                        core.confirm(conn, pid, self.handlers)
                        outcomes.append("ok")
                    except scheduling.SlotConflictError:
                        outcomes.append("conflict")
                finally:
                    conn.close()

            threads = [threading.Thread(target=worker, args=(n,)) for n in ("A", "B")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)
            self.assertEqual(sorted(outcomes), ["conflict", "ok"], "round {}".format(round_no))
        conn = db.connect(self.path)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT start_time) FROM appointments WHERE appt_date='2026-10-20'").fetchone()[:], (15, 15))
        conn.close()


class UndoTests(AutoCase):
    def feed(self, q=""):
        return self.client.get("/automation/data", query_string={"q": q}).get_json()["feed"]

    def undo(self, activity_id):
        return self.client.post("/automation/undo/{}".format(activity_id)).get_json()

    def entry(self, event):
        return self.activity(event)[-1]

    def test_undo_an_auto_booking_cancels_it_and_tells_the_patient_the_clinic_had_to_cancel(self):
        self.book_through_conversation()
        act = self.entry("auto_booked")
        self.assertTrue([f for f in self.feed() if f["id"] == act["id"]][0]["undo_available"])
        self.sender.calls.clear()
        result = self.undo(act["id"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.status_of(act["appointment_id"]), "cancelled")
        self.assertEqual(self.sender.texts, [notify.render(
            "appointment_cancelled_by_clinic", "en", name="Sunita Devi", date=TOMORROW, time="16:00")])
        self.assertIn("had to cancel", self.sender.texts[0])
        # audited: a [staff:undo] proposal + a cancel audit row, and the original is marked
        undo_prop = self.rows("SELECT * FROM proposals WHERE source_text LIKE '[staff:undo]%'")
        self.assertEqual([(p["intent"], p["status"]) for p in undo_prop], [("cancel_appointment", "confirmed")])
        self.assertEqual(self.rows("SELECT intent FROM audit_log ORDER BY id"), [{"intent": "book_appointment"}, {"intent": "cancel_appointment"}])
        self.assertEqual(self.rows("SELECT undone FROM patient_activity WHERE id=?", act["id"])[0]["undone"], 1)
        undone = self.activity("undone")
        self.assertEqual((len(undone), undone[0]["source"]), (1, "staff"))
        self.assertFalse([f for f in self.feed() if f["id"] == act["id"]][0]["undo_available"])

    def test_a_second_undo_is_refused(self):
        self.book_through_conversation()
        act = self.entry("auto_booked")
        self.assertTrue(self.undo(act["id"])["ok"])
        again = self.undo(act["id"])
        self.assertFalse(again["ok"])
        self.assertIn("already undone", again["error"])
        self.assertEqual(self.count("audit_log"), 2)                                 # nothing more was written
        self.assertEqual(len(self.activity("undone")), 1)

    def test_undo_an_auto_cancel_reinstates_the_same_slot(self):
        pid = self.patient()
        aid = self.appt(pid, TOMORROW, "09:00")
        self.say("cancel my appointment")
        self.tap("confirm:yes", "Yes")
        self.assertEqual(self.status_of(aid), "cancelled")
        self.sender.calls.clear()
        result = self.undo(self.entry("auto_cancelled")["id"])
        self.assertTrue(result["ok"], result)
        row = self.conn.execute("SELECT status, appt_date, start_time FROM appointments WHERE id=?", (aid,)).fetchone()
        self.assertEqual((row["status"], row["appt_date"], row["start_time"]), ("booked", TOMORROW, "09:00"))
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(self.events()[-1], "appointment_reinstated")
        self.assertIn("reinstated", self.sender.texts[0])
        self.assertEqual(self.rows("SELECT intent FROM audit_log ORDER BY id")[-1]["intent"], "restore_appointment")

    def test_undo_an_auto_cancel_reports_when_the_slot_is_gone(self):
        pid = self.patient()
        aid = self.appt(pid, TOMORROW, "09:00")
        self.say("cancel my appointment")
        self.tap("confirm:yes", "Yes")
        self.appt(None, TOMORROW, "09:00", name="Took it", phone="9000000001")
        self.sender.calls.clear()
        act = self.entry("auto_cancelled")
        result = self.undo(act["id"])
        self.assertFalse(result["ok"])
        self.assertIn("no longer free", result["error"])
        self.assertEqual(self.status_of(aid), "cancelled")
        self.assertEqual(self.sender.calls, [])
        self.assertEqual(self.rows("SELECT undone FROM patient_activity WHERE id=?", act["id"])[0]["undone"], 0)   # can be retried later
        self.assertEqual(self.rows("SELECT status FROM proposals WHERE source_text LIKE '[staff:undo]%'"), [{"status": "rejected"}])

    def test_undo_an_auto_cancel_reports_when_the_slot_is_now_blocked(self):
        pid = self.patient()
        self.appt(pid, TOMORROW, "09:00")
        self.say("cancel my appointment")
        self.tap("confirm:yes", "Yes")
        self.block(TOMORROW, t0="09:00", t1="10:00")
        result = self.undo(self.entry("auto_cancelled")["id"])
        self.assertFalse(result["ok"])
        self.assertIn("booking block", result["error"])

    def test_undo_an_auto_reschedule_moves_it_back(self):
        pid = self.patient()
        aid = self.appt(pid, TOMORROW, "09:00")
        self.say("reschedule my appointment")
        self.say("friday")
        self.say("4 pm")
        self.tap("confirm:yes", "Confirm request")
        self.sender.calls.clear()
        result = self.undo(self.entry("auto_rescheduled")["id"])
        self.assertTrue(result["ok"], result)
        row = self.conn.execute("SELECT appt_date, start_time FROM appointments WHERE id=?", (aid,)).fetchone()
        self.assertEqual((row["appt_date"], row["start_time"]), (TOMORROW, "09:00"))
        self.assertIn("moved", self.sender.texts[0])
        self.assertEqual(self.rows("SELECT intent FROM audit_log ORDER BY id")[-1]["intent"], "reschedule_appointment")

    def test_undo_an_auto_reschedule_reports_when_the_old_slot_is_taken(self):
        pid = self.patient()
        aid = self.appt(pid, TOMORROW, "09:00")
        self.say("reschedule my appointment")
        self.say("friday")
        self.say("4 pm")
        self.tap("confirm:yes", "Confirm request")
        self.appt(None, TOMORROW, "09:00", name="Took it", phone="9000000001")
        result = self.undo(self.entry("auto_rescheduled")["id"])
        self.assertFalse(result["ok"])
        self.assertIn("no longer free", result["error"])
        row = self.conn.execute("SELECT appt_date FROM appointments WHERE id=?", (aid,)).fetchone()
        self.assertEqual(row["appt_date"], FRIDAY)

    def test_undo_is_only_available_while_the_action_is_the_latest_change(self):
        self.book_through_conversation()
        act = self.entry("auto_booked")
        # staff move the appointment afterwards (any later audited change blocks the undo)
        moved = self.client.post("/appointments/{}/move".format(act["appointment_id"]),
                                 json={"appt_date": FRIDAY, "start_time": "17:00"}).get_json()
        self.assertTrue(moved["ok"], moved)
        item = [f for f in self.feed() if f["id"] == act["id"]][0]
        self.assertFalse(item["undo_available"])
        self.assertIn("changed since", item["undo_blocked_reason"])
        result = self.undo(act["id"])
        self.assertFalse(result["ok"])
        self.assertIn("changed since", result["error"])
        self.assertEqual(self.status_of(act["appointment_id"]), "booked")

    def test_only_automatic_actions_can_be_undone(self):
        self.book_through_conversation()
        req = self.activity("requested")[0]
        self.assertFalse(self.undo(req["id"])["ok"])
        self.assertFalse(self.undo(99999)["ok"])
        self.assertEqual(self.count("audit_log"), 1)

    def test_the_feed_can_be_filtered_by_patient_name(self):
        self.book_through_conversation(wa_id=WA, name="Sunita Devi", time="4 pm")
        self.book_through_conversation(wa_id=WA2, name="Raju Kumar", time="5 pm")
        self.assertEqual({f["patient_name"] for f in self.feed()}, {"Sunita Devi", "Raju Kumar"})
        self.assertEqual({f["patient_name"] for f in self.feed("raju")}, {"Raju Kumar"})
        self.assertEqual(self.feed("nobody"), [])

    def test_the_feed_is_newest_first_and_carries_what_the_ui_needs(self):
        self.book_through_conversation(wa_id=WA, name="Sunita Devi", time="4 pm")
        settings.set_auto_daily_cap(self.conn, 0)
        self.book_through_conversation(wa_id=WA2, name="Raju Kumar", time="5 pm")
        feed = self.feed()
        self.assertEqual([f["event"] for f in feed], ["escalated", "auto_booked"])
        self.assertIn("daily automation cap", feed[0]["meta"]["reason"])
        self.assertEqual(feed[1]["meta"]["appt_date"], TOMORROW)
        self.assertFalse(feed[0]["undo_available"])


class StaffDirectEditTests(AutoCase):
    def new(self, **body):
        base = {"patient_name": "Walk In", "patient_phone": "9000000001", "appt_date": TOMORROW, "start_time": "10:00"}
        base.update(body)
        return self.client.post("/appointments/new", json=base).get_json()

    def test_slot_list_excludes_booked_and_blocked_and_reports_the_blocked_ones_separately(self):
        self.appt(None, TOMORROW, "09:00", name="X", phone="9000000001")
        self.block(TOMORROW, t0="09:30", t1="10:30")
        data = self.client.get("/appointments/slots", query_string={"date": TOMORROW}).get_json()
        self.assertTrue(data["ok"])
        self.assertNotIn("09:00", data["free"] + data["blocked"])
        self.assertEqual(data["blocked"], ["09:30", "10:00"])
        self.assertNotIn("09:30", data["free"])
        self.assertIn("10:30", data["free"])
        self.assertEqual(self.client.get("/appointments/slots", query_string={"date": "nope"}).status_code, 400)

    def test_todays_slot_list_hides_times_that_have_passed(self):
        data = self.client.get("/appointments/slots", query_string={"date": "2026-10-05"}).get_json()
        self.assertEqual(data["free"][0], "10:00")

    def test_staff_book_a_registered_patient_immediately_with_no_review_card(self):
        pid = self.patient()
        self.open_window(WA)
        result = self.client.post("/appointments/new", json={"patient_id": pid, "appt_date": TOMORROW, "start_time": "10:00"}).get_json()
        self.assertTrue(result["ok"], result)
        appt = self.rows("SELECT * FROM appointments")[0]
        self.assertEqual((appt["patient_id"], appt["start_time"], appt["status"]), (pid, "10:00", "booked"))
        proposal = self.rows("SELECT * FROM proposals")[0]
        self.assertEqual((proposal["status"], proposal["source_text"].split(" ")[0]), ("confirmed", "[staff:dashboard]"))
        self.assertEqual(self.count("audit_log"), 1)
        act = self.activity("staff_booked")
        self.assertEqual((len(act), act[0]["source"], act[0]["patient_id"]), (1, "staff", pid))
        self.assertEqual(self.inbox(), [])
        self.assertIn("appointment is booked", self.sender.texts[0])                # patient notified by the existing template
        self.assertEqual(self.events(), ["booking_confirmed"])

    def test_staff_book_an_unregistered_patient_by_name_and_phone(self):
        self.open_window("919000000001")
        result = self.new()
        self.assertTrue(result["ok"], result)
        appt = self.rows("SELECT * FROM appointments")[0]
        self.assertEqual((appt["patient_id"], appt["patient_name"], appt["patient_phone"]), (None, "Walk In", "9000000001"))
        self.assertEqual(self.sender.calls[0][0], "919000000001")

    def test_staff_booking_validation(self):
        bad = [dict(patient_name=""), dict(patient_phone="123"), dict(appt_date="2026-10-04"), dict(appt_date="x"),
               dict(start_time="10:07"), dict(start_time="13:00"), dict(patient_name="x" * 61)]
        for kw in bad:
            result = self.new(**kw)
            self.assertFalse(result["ok"], kw)
        self.assertFalse(self.client.post("/appointments/new", json={"patient_id": 999, "appt_date": TOMORROW, "start_time": "10:00"}).get_json()["ok"])
        self.assertEqual(self.count("appointments"), 0)
        self.assertEqual(self.count("proposals"), 0)

    def test_staff_cannot_double_book(self):
        self.assertTrue(self.new()["ok"])
        again = self.new(patient_name="Other", patient_phone="9000000002")
        self.assertFalse(again["ok"])
        self.assertIn("no longer free", again["error"])
        self.assertEqual(self.count("appointments"), 1)
        self.assertEqual(self.rows("SELECT status FROM proposals ORDER BY id"), [{"status": "confirmed"}, {"status": "rejected"}])

    def test_booking_into_a_block_needs_the_explicit_override_and_is_audited(self):
        self.block(TOMORROW, t0="10:00", t1="11:00", reason="Surgery")
        refused = self.new()
        self.assertFalse(refused["ok"])
        self.assertTrue(refused["blocked"])
        self.assertIn("booking block", refused["error"])
        self.assertIn("Surgery", refused["error"])
        self.assertEqual(self.count("appointments"), 0)
        forced = self.new(override_block=True)
        self.assertTrue(forced["ok"], forced)
        self.assertTrue(json.loads(self.rows("SELECT payload_json FROM audit_log")[0]["payload_json"])["override_block"])
        self.assertIn("staff override", self.activity("staff_booked")[0]["detail"])

    def test_move_notifies_the_patient_and_logs_it(self):
        pid = self.patient()
        aid = self.appt(pid, TOMORROW, "09:00")
        self.open_window(WA)
        result = self.client.post("/appointments/{}/move".format(aid), json={"appt_date": FRIDAY, "start_time": "11:00"}).get_json()
        self.assertTrue(result["ok"], result)
        row = self.conn.execute("SELECT appt_date, start_time FROM appointments WHERE id=?", (aid,)).fetchone()
        self.assertEqual((row["appt_date"], row["start_time"]), (FRIDAY, "11:00"))
        self.assertEqual(self.events(), ["appointment_rescheduled"])
        self.assertEqual(len(self.activity("staff_rescheduled")), 1)
        self.assertEqual(self.rows("SELECT source_text FROM proposals")[0]["source_text"].split(" ")[0], "[staff:dashboard]")
        self.assertEqual(self.count("audit_log"), 1)

    def test_move_into_a_taken_or_blocked_slot_is_refused(self):
        pid = self.patient()
        aid = self.appt(pid, TOMORROW, "09:00")
        self.appt(None, FRIDAY, "11:00", name="X", phone="9000000001")
        self.block(FRIDAY, t0="12:00", t1="13:00")
        taken = self.client.post("/appointments/{}/move".format(aid), json={"appt_date": FRIDAY, "start_time": "11:00"}).get_json()
        self.assertFalse(taken["ok"])
        blocked = self.client.post("/appointments/{}/move".format(aid), json={"appt_date": FRIDAY, "start_time": "12:00"}).get_json()
        self.assertTrue(blocked["blocked"])
        forced = self.client.post("/appointments/{}/move".format(aid), json={"appt_date": FRIDAY, "start_time": "12:00", "override_block": True}).get_json()
        self.assertTrue(forced["ok"])

    def test_cancel_notifies_the_patient(self):
        pid = self.patient()
        aid = self.appt(pid, TOMORROW, "09:00")
        self.open_window(WA)
        result = self.client.post("/appointments/{}/cancel".format(aid)).get_json()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.status_of(aid), "cancelled")
        self.assertEqual(self.events(), ["appointment_cancelled"])
        self.assertEqual(len(self.activity("staff_cancelled")), 1)
        again = self.client.post("/appointments/{}/cancel".format(aid)).get_json()
        self.assertFalse(again["ok"])
        self.assertIn("already cancelled", again["error"])
        self.assertEqual(self.count("audit_log"), 1)

    def test_edits_are_refused_for_missing_finished_or_in_consultation_appointments(self):
        self.assertFalse(self.client.post("/appointments/99/cancel").get_json()["ok"])
        done = self.appt(None, TOMORROW, "09:00", name="X", phone="9000000001", status="completed")
        self.assertFalse(self.client.post("/appointments/{}/move".format(done), json={"appt_date": FRIDAY, "start_time": "11:00"}).get_json()["ok"])
        live = self.appt(None, TOMORROW, "09:15", name="Y", phone="9000000002")
        self.conn.execute("UPDATE appointments SET queue_state='in_consultation' WHERE id=?", (live,))
        self.conn.commit()
        self.assertIn("with the doctor", self.client.post("/appointments/{}/cancel".format(live)).get_json()["error"])

    def test_the_queue_tab_shows_any_date_with_move_and_cancel_buttons(self):
        self.appt(None, FRIDAY, "09:00", name="Friday Patient", phone="9000000001")
        html = self.client.get("/queue/partial", query_string={"date": FRIDAY}).get_data(as_text=True)
        self.assertIn("Friday Patient", html)
        self.assertIn('data-queue-edit="move"', html)
        self.assertIn('data-queue-edit="cancel"', html)
        self.assertNotIn("data-queue-action", html)                                   # check-in/call/done are today-only
        self.assertIn("Queue for {}".format(FRIDAY), html)
        junk = self.client.get("/queue/partial", query_string={"date": "garbage"}).get_data(as_text=True)
        self.assertIn("Today&#39;s queue", junk.replace("'", "&#39;"))
        page = self.client.get("/").get_data(as_text=True)
        for needle in ('id="queue-date"', 'id="new-appt-form"', 'id="move-appt"', 'data-tab="automation"', 'id="auto-switch"',
                       'id="block-form"', 'id="feed-search"', 'id="patient-activity-card"', 'queue_edit.js', 'automation.js'):
            self.assertIn(needle, page)

    def test_staff_actions_use_the_shared_post_commit_hooks(self):
        calls = []
        with mock.patch.object(clinic_app, "post_write_hooks", side_effect=lambda *a, **k: calls.append(a[1])):
            pid = self.patient()
            aid = self.appt(pid, TOMORROW, "09:00")
            self.client.post("/appointments/new", json={"patient_id": pid, "appt_date": TOMORROW, "start_time": "10:00"})
            self.client.post("/appointments/{}/move".format(aid), json={"appt_date": FRIDAY, "start_time": "11:00"})
            self.client.post("/appointments/{}/cancel".format(aid))
        self.assertEqual(calls, ["book_appointment", "reschedule_appointment", "cancel_appointment"])


class HooksAndIsolationTests(AutoCase):
    def test_automatic_writes_run_the_same_hooks_as_the_approval_route(self):
        calls = []
        with mock.patch.object(clinic_app, "post_write_hooks", side_effect=lambda *a, **k: calls.append((a[1], k.get("wa_id") or (a[4] if len(a) > 4 else None)))):
            self.book_through_conversation()
        self.assertEqual([c[0] for c in calls], ["book_appointment"])
        self.assertEqual(calls[0][1], WA)

    def test_notification_token_fanout_and_calendar_sync_all_happen_for_an_automatic_booking(self):
        fake = FakeCalendar()
        patches = [mock.patch.object(gcal_config, "is_configured", return_value=True),
                   mock.patch.object(gcal_client, "get_client", side_effect=AssertionError("real Google client requested"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        clinic_app.GCAL_CLIENT = fake
        clinic_app.GCAL_BACKGROUND = lambda fn: fn()
        self.addCleanup(setattr, clinic_app, "GCAL_CLIENT", None)
        self.addCleanup(setattr, clinic_app, "GCAL_BACKGROUND", None)
        self.book_through_conversation()
        self.assertEqual(len(fake.events), 1)                                         # calendar enqueue + drain ran
        self.assertEqual(self.events().count("booking_confirmed"), 1)

    def test_token_changes_for_other_patients_are_announced_after_an_automatic_booking(self):
        today = date.today().isoformat()
        self.patient(WA2, "Bhavna")
        pid2 = self.conn.execute("SELECT id FROM patients WHERE phone=?", (WA2[-10:],)).fetchone()[0]
        later = self.appt(pid2, today, "19:00")
        self.open_window(WA2)
        notify.notify_appointment(self.conn, "booking_confirmed", later, today)           # Bhavna was told: token T-01
        notify.flush(self.conn, self.sender)
        self.sender.calls.clear()
        clinic_app.CLOCK = lambda: datetime.combine(date.today(), datetime.min.time()).replace(hour=6)
        self.say("book today 6 pm")
        self.say("Sunita Devi")
        self.tap("confirm:yes", "Confirm request")
        self.assertEqual(self.count("appointments"), 2)
        self.assertTrue(any("token for today is now T-02" in t for t in self.sender.texts), self.sender.texts)

    def test_a_raising_sender_does_not_change_the_automatic_write(self):
        clinic_app.NOTIFY_SENDER = AgentSender(fail_with=RuntimeError("whatsapp is down"))
        self.say("book tomorrow 4 pm")
        self.say("Sunita Devi")
        self.tap("confirm:yes", "Confirm request")
        self.assertEqual(self.count("appointments"), 1)
        self.assert_one_auto_write("book_appointment", "auto_booked")
        self.not_a_staff_item()

    def test_a_raising_notification_hook_does_not_change_the_write_and_the_patient_still_hears(self):
        with mock.patch.object(notify, "post_commit", side_effect=RuntimeError("notify exploded")):
            self.book_through_conversation()
        self.assertEqual(self.count("appointments"), 1)
        self.assertEqual(self.sender.texts[-1], ct.text("auto_done_book", "en", date=notify.format_date(TOMORROW, "en"),
                                                        time=notify.format_time("16:00", "en")))

    def test_a_raising_calendar_hook_does_not_change_the_write(self):
        with mock.patch.object(clinic_app.gcal_sync, "post_commit", side_effect=RuntimeError("google exploded")):
            self.book_through_conversation()
        self.assertEqual(self.count("appointments"), 1)
        self.assert_one_auto_write("book_appointment", "auto_booked")
        self.assertIn("appointment is booked", self.sender.texts[-1])

    def test_a_raising_activity_writer_does_not_change_the_write(self):
        with mock.patch.object(patient_activity, "_stamp", side_effect=RuntimeError("activity exploded")):
            with self.assertLogs("clinic.patient_activity", level="ERROR"):
                row = self.book_through_conversation()
        self.assertEqual(self.count("appointments"), 1)
        self.assertEqual(self.count("audit_log"), 1)
        self.assertEqual(self.count("patient_activity"), 0)
        self.assertEqual(row["status"], "dismissed")
        self.assertIn("appointment is booked", self.sender.texts[-1])

    def test_a_failure_after_the_commit_never_also_hands_the_request_to_staff(self):
        self.say("book tomorrow 4 pm")
        self.say("Sunita Devi")
        with mock.patch.object(cv._Turn, "finish_auto", side_effect=RuntimeError("after the commit")):
            self.tap("confirm:yes", "Confirm request")
        self.assertEqual(self.count("appointments"), 1)
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.last_row()["status"], "dismissed")
        self.assertNotIn(ct.text("handoff_received", "en"), self.sender.texts)
        self.assertEqual(len(self.autos()), 1)


class ExtendedInvariantTests(AutoCase):
    def test_nothing_else_is_ever_automatic(self):
        """A registration, a visit, a follow-up and staff voice commands keep their approval gate."""
        for text in ("I want to register as a new patient", "my name is Raju, add me", "record visit fee 500", "set follow-up in 7 days"):
            self.say(text)
        self.assertEqual(self.count("patients"), 0)
        self.assertEqual(self.count("visits"), 0)
        self.assertEqual(self.count("followups"), 0)
        self.assertEqual(self.autos(), [])
        self.assertEqual(self.count("audit_log"), 0)

    def test_the_policy_names_exactly_three_automatic_intents(self):
        self.assertEqual(set(auto_policy.AUTO_INTENTS), {"book_appointment", "cancel_appointment", "reschedule_appointment"})
        self.assertEqual(set(clinic_app.DEFERRED_INTENTS) - {"register_patient", "register_staff", "record_visit", "set_followup", "log_attendance",
                                                              "log_expense", "cancel_followup", "reschedule_followup", "queue_check_in",
                                                              "queue_call_next", "queue_mark_done", "queue_mark_no_show"},
                         {"book_appointment", "cancel_appointment", "reschedule_appointment"})

    def test_the_voice_approve_route_still_commits_only_on_a_human_tap(self):
        result = self.client.post("/approve", json={"intent": "book_appointment", "slots": {
            "patient_name": "Voice Patient", "patient_phone": "9000000001", "appt_date": TOMORROW, "start_time": "10:00"}}).get_json()
        self.assertTrue(result["ok"])
        self.assertEqual(self.autos(), [])
        self.assertEqual(self.rows("SELECT source_text FROM proposals")[0]["source_text"] in (None, ""), True)


class ApprovalBlockOverrideTests(AutoCase):
    """The review-card routes (voice /approve and the inbox) refuse a blocked
    slot too, and accept it only on an explicit, flagged second submit."""

    def booking(self):
        return {"patient_name": "Voice Patient", "patient_phone": "9000000001", "appt_date": TOMORROW, "start_time": "10:00"}

    def test_voice_card_approval_into_a_block_is_refused_then_allowed_with_the_override(self):
        self.block(TOMORROW, t0="10:00", t1="11:00", reason="Surgery")
        refused = self.client.post("/approve", json={"intent": "book_appointment", "slots": self.booking()}).get_json()
        self.assertFalse(refused["ok"])
        self.assertTrue(refused["blocked"])
        self.assertIn("Surgery", refused["error"])
        self.assertEqual((self.count("appointments"), self.count("audit_log")), (0, 0))
        forced = self.client.post("/approve", json={"intent": "book_appointment", "slots": self.booking(),
                                                    "override_block": True}).get_json()
        self.assertTrue(forced["ok"], forced)
        self.assertTrue(json.loads(self.rows("SELECT payload_json FROM audit_log")[0]["payload_json"])["override_block"])

    def test_override_is_ignored_for_every_other_intent(self):
        slots = {"name": "Raju", "phone": "9000000001"}
        self.assertTrue(self.client.post("/approve", json={"intent": "register_patient", "slots": slots, "override_block": True}).get_json()["ok"])
        self.assertNotIn("override_block", self.rows("SELECT slots_json FROM proposals")[0]["slots_json"])

    def test_the_inbox_card_gets_the_same_treatment(self):
        settings.set_auto_enabled(self.conn, False)
        row = self.book_through_conversation()
        self.block(TOMORROW, t0="16:00", t1="17:00")
        slots = json.loads(row["slots_json"])
        body = {"slots": {"patient_id": None, "patient_name": slots["patient_name"], "patient_phone": slots["patient_phone"],
                          "appt_date": slots["appt_date"], "start_time": slots["start_time"], "duration_minutes": None, "notes": None}}
        refused = self.client.post("/wa/{}/approve".format(row["id"]), json=body).get_json()
        self.assertTrue(refused["blocked"])
        self.assertEqual(self.conn.execute("SELECT status FROM wa_messages WHERE id=?", (row["id"],)).fetchone()[0], "classified")
        body["override_block"] = True
        self.assertTrue(self.client.post("/wa/{}/approve".format(row["id"]), json=body).get_json()["ok"])


class NotificationTemplateTests(AutoCase):
    def test_the_new_templates_render_in_every_language(self):
        for lang in ("en", "hi", "hinglish"):
            clinic = notify.render("appointment_cancelled_by_clinic", lang, name="Sunita", date=TOMORROW, time="16:00")
            self.assertIn(notify.format_date(TOMORROW, lang), clinic)
            self.assertNotIn("{", clinic)
            for key in ("appointment_reinstated_future", "appointment_reinstated_today"):
                text = notify.render(key, lang, name="Sunita", token=2, date=TOMORROW, time="16:00", ahead=1)
                self.assertIn("T-02", text)
                self.assertNotIn("{", text)
        self.assertIn("had to cancel", notify.render("appointment_cancelled_by_clinic", "en", date=TOMORROW, time="16:00"))

    def test_a_second_cancellation_after_a_reinstatement_still_tells_the_patient(self):
        pid = self.patient()
        aid = self.appt(pid, TOMORROW, "09:00")
        self.open_window(WA)
        for _ in range(2):
            self.assertTrue(self.client.post("/appointments/{}/cancel".format(aid)).get_json()["ok"])
            self.assertEqual(self.status_of(aid), "cancelled")
            proposal_id = core.propose(self.conn, "restore_appointment", {"appointment_id": aid}, "test")
            core.confirm(self.conn, proposal_id, clinic_app.HANDLERS)           # reinstated (the notification hook is not run here)
        self.assertTrue(self.client.post("/appointments/{}/cancel".format(aid)).get_json()["ok"])
        # one notification per cancellation: the dedup key must not swallow the later ones
        self.assertEqual(self.events().count("appointment_cancelled"), 3)


class BlockEndpointTests(AutoCase):
    def api(self, path, body):
        return self.client.post(path, json=body)

    def test_add_list_preview_and_remove(self):
        pid = self.patient()
        aid = self.appt(pid, TOMORROW, "09:00")
        preview = self.api("/automation/blocks/preview", {"start_date": TOMORROW, "end_date": TOMORROW}).get_json()
        self.assertEqual(preview["count"], 1)
        self.assertEqual(self.count("booking_blocks"), 0)                              # a preview creates nothing
        added = self.api("/automation/blocks", {"start_date": TOMORROW, "end_date": FRIDAY, "reason": "Away"}).get_json()
        self.assertTrue(added["ok"])
        self.assertEqual(added["count"], 1)
        self.assertEqual(added["block"]["affected"][0]["id"], aid)
        self.assertEqual(self.status_of(aid), "booked")                                # existing appointments untouched
        data = self.client.get("/automation/data").get_json()
        self.assertEqual(len(data["blocks"]), 1)
        self.assertEqual(data["blocks"][0]["reason"], "Away")
        self.assertEqual(len(data["blocks"][0]["affected"]), 1)
        self.assertTrue(self.api("/automation/blocks/{}/remove".format(added["block"]["id"]), {}).get_json()["ok"])
        self.assertEqual(self.api("/automation/blocks/{}/remove".format(added["block"]["id"]), {}).status_code, 404)
        self.assertEqual(self.client.get("/automation/data").get_json()["blocks"], [])

    def test_bad_input_is_a_400_and_creates_nothing(self):
        for body in ({}, {"start_date": "x"}, {"start_date": TOMORROW, "start_time": "09:00"},
                     {"start_date": FRIDAY, "end_date": TOMORROW}):
            self.assertEqual(self.api("/automation/blocks", body).status_code, 400, body)
            self.assertEqual(self.api("/automation/blocks/preview", body).status_code, 400, body)
        self.assertEqual(self.count("booking_blocks"), 0)

    def test_settings_endpoint_validates_and_reports_usage(self):
        self.book_through_conversation()
        data = self.client.get("/automation/data").get_json()
        self.assertEqual((data["settings"]["enabled"], data["settings"]["daily_cap"], data["settings"]["used_today"]), (True, 40, 1))
        ok = self.api("/automation/settings", {"daily_cap": 7}).get_json()
        self.assertEqual(ok["settings"]["daily_cap"], 7)
        for body in ({"daily_cap": -1}, {"daily_cap": "lots"}, {"daily_cap": 5000}, {"enabled": "yes"}):
            self.assertEqual(self.api("/automation/settings", body).status_code, 400, body)
        self.assertEqual(settings.auto_daily_cap(self.conn), 7)

    def test_patient_timeline_endpoint(self):
        pid = self.patient()
        self.say("book tomorrow 4 pm")
        self.tap("confirm:yes", "Confirm request")
        data = self.client.get("/patients/{}/activity".format(pid)).get_json()
        self.assertEqual([a["event"] for a in data["activity"]], ["auto_booked", "requested"])
        self.assertEqual(data["patient"]["name"], "Sunita Devi")
        self.assertEqual(self.client.get("/patients/999/activity").status_code, 404)

    def test_timeline_falls_back_to_the_phone_for_activity_before_registration(self):
        self.book_through_conversation(wa_id=WA, name="Sunita Devi")        # unregistered at the time
        pid = self.patient(WA, "Sunita Devi")                               # registered afterwards
        data = self.client.get("/patients/{}/activity".format(pid)).get_json()
        self.assertEqual([a["event"] for a in data["activity"]], ["auto_booked", "requested"])


if __name__ == "__main__":
    unittest.main()
