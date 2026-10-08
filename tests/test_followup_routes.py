"""The follow-up routes the Patients > Follow-ups tab and Settings call (plan,
apply, undo, list, edit the diagnosis, retry, mark sent by hand, settings and
template approval), and the patient's buttons arriving through the real WhatsApp
webhook. Flask test client, in-process; the sender is a recording fake and the
real WhatsApp client is tripwired."""
import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_wa_agent_routes import CLOCK_NOW, AgentRouteCase, AgentSender, WA, clinic_app  # noqa: E402  (stubs dotenv, sets the safe env)

from clinic import booking_blocks, branches, followup_notify, followups, settings  # noqa: E402

TODAY = "2026-10-05"          # the agent tests' fixed clock: Monday 10:00
WED, THU, SUNDAY = "2026-10-07", "2026-10-08", "2026-10-11"


class TemplateSender(AgentSender):
    """Also takes template messages, like the real sender."""

    def __call__(self, wa_id, text, interactive=None, template=None):
        self.calls.append((wa_id, text, interactive))
        if template is not None:
            self.templates.append((wa_id, template))

    templates = None

    def __init__(self):
        super().__init__()
        self.templates = []


class FollowupRouteCase(AgentRouteCase):
    AUTOMATION = True

    def setUp(self):
        super().setUp()
        self.sunita = self.patient("Sunita Devi", "9876543210")
        self.rakesh = self.patient("Rakesh Verma", "9811100001")       # staff booked by phone: has never written to the clinic
        self.open_window(WA, "hello")

    def open_window(self, wa_id, text="hello", when=None):
        """The patient wrote to the clinic an hour before the (fixed) test clock -- not before the real
        date, so these tests never depend on what day it really is."""
        when = when or (CLOCK_NOW - timedelta(hours=6, minutes=30))
        self.conn.execute("INSERT INTO wa_messages (wa_message_id, wa_id, message_type, raw_text, received_at) "
                          "VALUES (?, ?, 'text', ?, ?)", ("wamid.fu{}{}".format(wa_id, when), wa_id, text, when.strftime("%Y-%m-%d %H:%M:%S")))
        self.conn.commit()

    def patient(self, name, phone):
        pid = self.conn.execute("INSERT INTO patients (name, phone) VALUES (?, ?)", (name, phone)).lastrowid
        self.conn.commit()
        return pid

    def row(self, patient=None, date_=WED, time="11:00", **kw):
        row = {"patient_id": patient or self.sunita, "due_date": date_, "due_time": time, "doctor_id": 1, "branch_id": 1,
               "diagnosis": None}
        row.update(kw)
        return row

    def api(self, method, url, payload=None):
        response = getattr(self.client, method)(url, json=payload) if method == "post" else self.client.get(url)
        return response.status_code, response.get_json()

    def apply(self, *rows):
        status, body = self.api("post", "/followups/apply", {"rows": list(rows)})
        self.assertEqual(status, 200, body)
        return body

    def data(self, query=""):
        status, body = self.api("get", "/followups/data" + query)
        self.assertEqual(status, 200)
        return body

    def fu_calls(self):
        return [c for c in self.sender.calls if c[2] and any(b["id"].startswith("followup:") for b in c[2].get("buttons", []))]


class PlanAndApply(FollowupRouteCase):
    def test_plan_checks_every_row_and_writes_nothing(self):
        status, body = self.api("post", "/followups/plan", {"rows": [self.row(), self.row(time="14:00")]})
        self.assertEqual(status, 200)
        rows = body["plan"]["rows"]
        self.assertEqual([r["ok"] for r in rows], [True, False])
        self.assertIn("no doctor on duty", rows[1]["errors"][0])
        self.assertEqual(body["plan"]["counts"], {"total": 2, "valid": 1, "invalid": 1})
        self.assertEqual(self.count("appointments"), 0)
        self.assertEqual(self.count("followups"), 0)

    def test_plan_and_apply_reject_bad_requests_with_a_message(self):
        for url in ("/followups/plan", "/followups/apply"):
            for payload in ({}, {"rows": []}, {"rows": "x"}, {"rows": [self.row()] * 51}):
                with self.subTest(url=url, payload=str(payload)[:30]):
                    status, body = self.api("post", url, payload)
                    self.assertEqual(status, 400)
                    self.assertFalse(body["ok"])
                    self.assertTrue(body["error"])
        self.assertEqual(self.count("followups"), 0)
        response = self.client.post("/followups/plan", data="not json", content_type="text/plain")
        self.assertEqual(response.status_code, 400)

    def test_apply_books_the_slot_links_it_and_the_first_reminder_goes_out_at_once(self):
        body = self.apply(self.row(diagnosis="internal only"))
        self.assertEqual((body["counts"], body["message"]), ({"created": 1, "failed": 0}, "1 follow-up booked."))
        result = body["results"][0]
        appt = self.conn.execute("SELECT * FROM appointments WHERE id = ?", (result["appointment_id"],)).fetchone()
        self.assertEqual((appt["appt_date"], appt["start_time"], appt["patient_id"]), (WED, "11:00", self.sunita))
        fu = followups.get_followup(self.conn, result["followup_id"])
        self.assertEqual((fu["appointment_id"], fu["diagnosis"]), (appt["id"], "internal only"))
        # made two days ahead: the early reminder is due now and the window is open, so it is sent right away, with its buttons
        calls = self.fu_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], WA)
        self.assertEqual([b["title"] for b in calls[0][2]["buttons"]], ["Reschedule", "Already visited", "Cancel"])
        self.assertNotIn("internal only", json.dumps(self.sender.calls))
        # and the usual booking confirmation went out through the normal path
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notifications WHERE event = 'booking_confirmed'").fetchone()[0], 1)

    def test_apply_reports_a_failed_row_without_stopping_the_others(self):
        body = self.apply(self.row(time="11:00"), self.row(self.rakesh, time="14:00"), self.row(self.rakesh, time="11:30"))
        self.assertEqual(body["counts"], {"created": 2, "failed": 1})
        self.assertEqual(body["message"], "2 follow-ups booked, 1 could not be booked.")
        self.assertEqual([r["ok"] for r in body["results"]], [True, False, True])
        self.assertIn("no doctor on duty at 14:00", body["results"][1]["error"])

    def test_the_list_shows_each_followup_with_its_two_reminders(self):
        self.apply(self.row(), self.row(self.rakesh, time="11:30"))
        data = self.data()
        self.assertEqual(data["today"], TODAY)
        self.assertEqual([f["patient_name"] for f in data["followups"]], ["Sunita Devi", "Rakesh Verma"])
        sunita, rakesh = data["followups"]
        self.assertEqual([(r["kind"], r["status"]) for r in sunita["reminders"]], [("2d", "sent"), ("4h", "queued")])
        self.assertEqual([(r["kind"], r["status"]) for r in rakesh["reminders"]], [("2d", "blocked"), ("4h", "queued")])
        self.assertEqual((sunita["due_time"], sunita["doctor"], sunita["branch"], sunita["status"]), ("11:00", "Dr. Mehta", "Branch A", "pending"))
        self.assertEqual([m["patient_name"] for m in data["manual"]], ["Rakesh Verma"])

    def test_undo_takes_the_batch_back(self):
        body = self.apply(self.row())
        status, undone = self.api("post", "/followups/batches/{}/undo".format(body["batch_id"]))
        self.assertEqual(status, 200)
        self.assertEqual(undone["message"], "Batch undone: 1 follow-up cancelled.")
        self.assertEqual(self.conn.execute("SELECT status FROM appointments").fetchone()[0], "cancelled")
        status, again = self.api("post", "/followups/batches/{}/undo".format(body["batch_id"]))
        self.assertEqual((status, again["ok"]), (400, False))
        self.assertEqual(self.api("post", "/followups/batches/999/undo")[0], 400)

    def test_the_branch_filter(self):
        branches.ensure_seed(self.conn)
        rao = [d["id"] for d in branches.list_doctors(self.conn) if d["name"] == "Dr. Rao"][0]
        self.apply(self.row(), self.row(self.rakesh, date_="2026-10-12", branch_id=2, doctor_id=rao))
        names = lambda q: [f["patient_name"] for f in self.data(q)["followups"]]  # noqa: E731
        self.assertEqual(names("?branch=all"), ["Sunita Devi", "Rakesh Verma"])
        self.assertEqual(names("?branch=1"), ["Sunita Devi"])
        self.assertEqual(names("?branch=2"), ["Rakesh Verma"])
        self.assertEqual(names(""), ["Sunita Devi"])                          # no branch named: the default branch
        self.assertEqual(names("?branch=banana"), ["Sunita Devi"])
        self.assertEqual(self.data("?branch=all")["branch"], "all")


class SlotsRoute(FollowupRouteCase):
    def test_free_times_for_a_doctor_and_day(self):
        self.apply(self.row(time="10:00"))
        status, body = self.api("get", "/followups/slots?date={}&branch=1&doctor=1".format(WED))
        self.assertEqual(status, 200)
        self.assertNotIn("10:00", body["free"])
        self.assertIn("09:30", body["free"])
        self.assertEqual((body["doctor"], body["problem"]), ("Dr. Mehta", None))

    def test_a_closed_day_says_so_and_offers_the_next_one(self):
        booking_blocks.add_block(self.conn, WED, WED, reason="Holiday")
        status, body = self.api("get", "/followups/slots?date={}".format(WED))
        self.assertEqual((body["free"], body["suggestion"]["date"]), ([], THU))
        self.assertIn("Holiday", body["problem"])

    def test_bad_requests(self):
        for query in ("", "?date=x", "?date={}&branch=99".format(WED), "?date={}&branch=abc".format(WED),
                      "?date={}&doctor=99".format(WED), "?date={}&doctor=abc".format(WED)):
            with self.subTest(query=query):
                status, body = self.api("get", "/followups/slots" + query)
                self.assertEqual(status, 400)
                self.assertFalse(body["ok"])


class DiagnosisRoute(FollowupRouteCase):
    def test_staff_edit_it_and_it_is_audited_and_never_sent(self):
        fid = self.apply(self.row())["results"][0]["followup_id"]
        status, body = self.api("post", "/followups/{}/diagnosis".format(fid), {"diagnosis": "  Hypertension follow-up  "})
        self.assertEqual((status, body), (200, {"ok": True, "diagnosis": "Hypertension follow-up"}))
        self.assertEqual(self.data()["followups"][0]["diagnosis"], "Hypertension follow-up")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM audit_log WHERE intent = 'followup_diagnosis_edited'").fetchone()[0], 1)
        self.assertNotIn("Hypertension", json.dumps(self.sender.calls))
        notes = json.dumps([dict(r) for r in self.conn.execute("SELECT * FROM notifications")])
        self.assertNotIn("Hypertension", notes)
        self.assertEqual(self.api("post", "/followups/{}/diagnosis".format(fid), {"diagnosis": ""})[1], {"ok": True, "diagnosis": ""})

    def test_bad_edits(self):
        fid = self.apply(self.row())["results"][0]["followup_id"]
        for url, payload in (("/followups/{}/diagnosis".format(fid), {"diagnosis": "x" * 501}),
                             ("/followups/{}/diagnosis".format(fid), {"diagnosis": 12}),
                             ("/followups/9999/diagnosis", {"diagnosis": "x"})):
            status, body = self.api("post", url, payload)
            self.assertEqual(status, 400)
            self.assertFalse(body["ok"])


class RetryAndByHand(FollowupRouteCase):
    def blocked(self):
        self.apply(self.row(self.rakesh, time="11:30"))
        return self.data()["manual"][0]

    def test_a_blocked_reminder_can_be_retried_and_marked_sent_by_hand(self):
        manual = self.blocked()
        self.assertEqual((manual["status"], manual["phone"], manual["label"]), ("blocked", "9811100001", "Early reminder"))
        self.assertIn("Rakesh Verma", manual["text"])
        status, body = self.api("post", "/followups/reminders/{}/retry".format(manual["reminder_id"]))
        self.assertEqual((status, body), (200, {"ok": True, "status": "blocked_no_window"}))      # still no window: blocks again
        status, body = self.api("post", "/followups/reminders/{}/manual-sent".format(manual["reminder_id"]))
        self.assertEqual((status, body), (200, {"ok": True}))
        self.assertEqual(self.data()["manual"], [])
        reminder = self.data()["followups"][0]["reminders"][0]
        self.assertEqual((reminder["status"], reminder["detail"]), ("sent", "sent by hand"))
        self.assertEqual(self.api("post", "/followups/reminders/{}/manual-sent".format(manual["reminder_id"]))[0], 400)
        self.assertEqual(self.api("post", "/followups/reminders/{}/retry".format(manual["reminder_id"]))[0], 400)
        self.assertEqual(self.api("post", "/followups/reminders/9999/retry")[0], 400)
        self.assertEqual(self.api("post", "/followups/reminders/9999/manual-sent")[0], 400)

    def test_the_template_goes_out_once_staff_mark_it_approved_and_retry(self):
        sender = TemplateSender()
        clinic_app.NOTIFY_SENDER = sender
        manual = self.blocked()
        self.assertEqual(sender.templates, [])
        status, body = self.api("post", "/settings/followup-templates", {"approved": ["followup_reminder_2d_en"]})
        self.assertEqual(status, 200)
        status, body = self.api("post", "/followups/reminders/{}/retry".format(manual["reminder_id"]))
        self.assertEqual(body["status"], "sent")
        self.assertEqual(len(sender.templates), 1)
        wa_id, template = sender.templates[0]
        self.assertEqual((wa_id, template["name"], len(template["params"]), len(template["buttons"])),
                         ("919811100001", "followup_reminder_2d_en", 5, 3))
        self.assertEqual(self.data()["manual"], [])


class SettingsRoutes(FollowupRouteCase):
    def test_defaults_and_saving_the_timing(self):
        status, body = self.api("get", "/settings/followups/data")
        self.assertEqual(body["data"]["timing"], {"days_before": 2, "send_time": "10:00", "hours_before": 4, "earliest_send": "07:00"})
        self.assertEqual(len(body["data"]["templates"]), 6)
        self.assertEqual([t["approved"] for t in body["data"]["templates"]], [False] * 6)
        status, body = self.api("post", "/settings/followups", {"days_before": "3", "send_time": "09:00", "hours_before": 2, "earliest_send": "08:00"})
        self.assertEqual((status, body["data"]["timing"]), (200, {"days_before": 3, "send_time": "09:00", "hours_before": 2, "earliest_send": "08:00"}))
        self.assertEqual(settings.followup_reminder_settings(self.conn)["days_before"], 3)

    def test_saving_new_timing_moves_reminders_not_yet_sent(self):
        fid = self.apply(self.row(self.rakesh, date_="2026-10-14", time="16:00"))["results"][0]["followup_id"]
        before = {r["kind"]: r["due_at"] for r in self.conn.execute("SELECT kind, due_at FROM followup_reminders WHERE followup_id = ?", (fid,))}
        self.assertEqual(before, {"2d": "2026-10-12 10:00", "4h": "2026-10-14 12:00"})
        self.api("post", "/settings/followups", {"days_before": 1, "send_time": "08:00", "hours_before": 3, "earliest_send": "07:00"})
        after = {r["kind"]: r["due_at"] for r in self.conn.execute("SELECT kind, due_at FROM followup_reminders WHERE followup_id = ?", (fid,))}
        self.assertEqual(after, {"2d": "2026-10-13 08:00", "4h": "2026-10-14 13:00"})

    def test_bad_timing_is_refused_with_the_reason_and_nothing_is_saved(self):
        good = {"days_before": 2, "send_time": "10:00", "hours_before": 4, "earliest_send": "07:00"}
        for change, word in (({"days_before": 0}, "Days before"), ({"send_time": "99:00"}, "Send time"),
                             ({"hours_before": "many"}, "Hours before"), ({"earliest_send": ""}, "Earliest send time"),
                             ({"days_before": None}, "Days before")):
            with self.subTest(change=change):
                payload = dict(good, **change)
                status, body = self.api("post", "/settings/followups", payload)
                self.assertEqual(status, 400)
                self.assertIn(word, body["error"])
        self.assertEqual(settings.followup_reminder_settings(self.conn)["days_before"], 2)

    def test_approving_templates_and_the_validation(self):
        names = followup_notify.all_template_names()
        status, body = self.api("post", "/settings/followup-templates", {"approved": names[:2]})
        self.assertEqual(status, 200)
        self.assertEqual([t["name"] for t in body["data"]["templates"] if t["approved"]], names[:2])
        self.assertEqual(settings.approved_templates(self.conn), set(names[:2]))
        for payload in ({}, {"approved": "followup_reminder_2d_en"}, {"approved": ["made_up"]}, {"approved": [1]}):
            with self.subTest(payload=payload):
                status, body = self.api("post", "/settings/followup-templates", payload)
                self.assertEqual(status, 400)
                self.assertFalse(body["ok"])
        self.assertEqual(settings.approved_templates(self.conn), set(names[:2]))          # a bad request changed nothing
        self.assertEqual(self.api("post", "/settings/followup-templates", {"approved": []})[0], 200)
        self.assertEqual(settings.approved_templates(self.conn), set())


class StaffEditsFlowThroughToTheFollowup(FollowupRouteCase):
    def test_moving_and_cancelling_the_appointment_from_the_queue_updates_the_followup(self):
        body = self.apply(self.row())["results"][0]
        status, moved = self.api("post", "/appointments/{}/move".format(body["appointment_id"]),
                                 {"appt_date": THU, "start_time": "17:00", "branch_id": 1})
        self.assertTrue(moved["ok"], moved)
        fu = self.data()["followups"][0]
        self.assertEqual((fu["due_date"], fu["due_time"], fu["status"]), (THU, "17:00", "pending"))
        status, cancelled = self.api("post", "/appointments/{}/cancel".format(body["appointment_id"]))
        self.assertTrue(cancelled["ok"], cancelled)
        fu = self.data()["followups"][0]
        self.assertEqual(fu["status"], "cancelled")
        self.assertEqual({r["status"] for r in fu["reminders"]} - {"sent"}, {"skipped"})


class PatientTapsThroughTheWebhook(FollowupRouteCase):
    def setUp(self):
        super().setUp()
        self.result = self.apply(self.row())["results"][0]
        self.fid, self.aid = self.result["followup_id"], self.result["appointment_id"]
        del self.sender.calls[:]

    def test_already_visited_on_a_free_form_reminder_button(self):
        self.tap("followup:visited:{}".format(self.fid), "Already visited")
        fu = followups.get_followup(self.conn, self.fid)
        self.assertEqual((fu["status"], fu["appointment_status"]), ("done", "cancelled"))
        self.assertEqual(len(self.sender.calls), 1)
        self.assertIn("will not send more reminders", self.sender.calls[0][1])
        event = self.conn.execute("SELECT event, source, appointment_id FROM patient_activity WHERE event = 'followup_visited'").fetchone()
        self.assertEqual(tuple(event), ("followup_visited", "whatsapp-agent", self.aid))
        self.assertEqual([r[0] for r in self.conn.execute("SELECT status FROM wa_messages ORDER BY id DESC LIMIT 1")], ["dismissed"])

    def test_a_tap_on_a_template_quick_reply_arrives_the_same_way(self):
        self.post({"from": WA, "id": self.mid(), "type": "button",
                   "button": {"payload": "followup:cancel:{}".format(self.fid), "text": "Cancel"}})
        self.assertIn("Cancel your follow-up visit", self.sender.calls[0][1])
        self.assertEqual([b["title"] for b in self.sender.calls[0][2]["buttons"]], ["Yes, cancel", "No, keep it"])
        self.tap("followup:cancel_confirm:{}".format(self.fid), "Yes, cancel")
        self.assertEqual(followups.get_followup(self.conn, self.fid)["status"], "cancelled")
        self.assertEqual(len(self.sender.calls), 2)
        self.assertIn("has been cancelled", self.sender.calls[1][1])

    def test_a_tap_from_anyone_else_changes_nothing(self):
        self.tap("followup:visited:{}".format(self.fid), "Already visited", wa_id="919111122223")
        self.assertEqual(followups.get_followup(self.conn, self.fid)["status"], "pending")
        self.assertIn("no longer active", self.sender.calls[0][1])

    def test_reschedule_from_the_reminder_and_the_visit_moves(self):
        self.tap("followup:reschedule:{}".format(self.fid), "Reschedule")
        self.assertIn("Which day would you like to move it to?", self.sender.calls[0][1])
        self.tap("day:{}".format(THU), "Thu")
        self.say("5 pm")
        self.tap("confirm:yes", "Confirm request")
        fu = followups.get_followup(self.conn, self.fid)
        self.assertEqual((fu["due_date"], fu["due_time"]), (THU, "17:00"))
        self.assertEqual(self.conn.execute("SELECT start_time FROM appointments WHERE id = ?", (self.aid,)).fetchone()[0], "17:00")

    def test_stop_through_the_webhook(self):
        self.say("STOP")
        self.assertTrue(followups.is_opted_out(self.conn, WA))
        self.assertIn("no longer receive follow-up reminders", self.sender.calls[0][1])


class DashboardPage(FollowupRouteCase):
    def test_the_page_has_the_patient_list_for_the_batch_form_escaped(self):
        self.patient('<script>alert(1)</script>', "9000000001")
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="fu-patient-list"', html)
        self.assertIn("Sunita Devi &middot; 9876543210", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_the_first_scheduler_tick_after_downtime_sends_what_became_due(self):
        # The app was down when the early reminder was due: the first tick after it starts sends it.
        from clinic import scheduler
        from tests.followup_fixtures import at
        self.apply(self.row(date_="2026-10-09", time="16:00"))
        del self.sender.calls[:]
        self.assertEqual(len(self.fu_calls()), 0)
        self.open_window(WA, "hello again", when=at(7, 12).utc)               # they wrote this morning
        self.assertEqual(scheduler.tick(self.conn, self.sender, now=at(7, 9, 0))["followup_reminders"], 0)     # not due yet
        summary = scheduler.tick(self.conn, self.sender, now=at(7, 13, 0))            # the app came back three hours late
        self.assertEqual((summary["followup_reminders"], summary["errors"]), (1, 0))
        self.assertEqual(len(self.fu_calls()), 1)


if __name__ == "__main__":
    unittest.main()
