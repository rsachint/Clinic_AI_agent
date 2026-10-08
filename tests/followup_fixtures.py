"""Shared fixtures for the follow-up reminder tests: a real (in-memory) database
built the way the app builds one, a fake clock, recording senders, and a few
helpers that book a follow-up the way the Follow-ups tab does. Nothing here can
reach WhatsApp: senders are recording fakes. The "today" is a fixed Monday, never
the real date."""
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

os.environ["WHATSAPP_NOTIFY_MODE"] = "dry_run"   # belt and braces: nothing here may ever reach the real API

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import core, db, followups, notify  # noqa: E402
from clinic.intents import HANDLERS  # noqa: E402

TODAY = "2026-10-05"      # a Monday
D1, D2, D3, D4, D5 = "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10"   # Tue .. Sat
SUNDAY = "2026-10-11"
WA = "919876543210"       # a patient who has written to the clinic
STAFF_PHONE = "98765 43211"   # a patient staff booked by phone number only
STAFF_WA = "919876543211"


def at(day, hour=0, minute=0, month=10):
    """Both clocks (local IST and UTC = IST - 5:30) for 2026-<month>-<day> hour:minute."""
    local = datetime(2026, month, day, hour, minute)
    return notify.Now(local, local - timedelta(hours=5, minutes=30))


def make_db():
    return db.connect(":memory:")


class Sender:
    """A sender that takes everything the real one takes (interactive buttons AND
    template messages) and records it."""

    def __init__(self, fail_with=None):
        self.calls = []
        self.fail_with = fail_with

    def __call__(self, wa_id, text, interactive=None, template=None):
        self.calls.append({"wa_id": wa_id, "text": text, "interactive": interactive, "template": template})
        if self.fail_with:
            raise self.fail_with

    def followup_calls(self):
        return [c for c in self.calls if (c["interactive"] and any(
            b["id"].startswith("followup:") for b in c["interactive"].get("buttons", []))) or c["template"]]


class PlainSender:
    """The old two-argument sender some tests use: it cannot send templates."""

    def __init__(self):
        self.calls = []

    def __call__(self, wa_id, text):
        self.calls.append((wa_id, text))


def hooks(with_notify=False):
    """The post-commit hook of app.py, minus the parts a test does not need: it keeps
    follow-ups in step with their appointments, and (optionally) queues the usual
    patient notifications."""
    def after_commit(conn, intent, slots, entity_id, wa_id=None, language=None):
        if with_notify:
            notify.after_write(conn, intent, slots, entity_id, now=at(5, 9))
        followups.after_write(conn, intent, slots, entity_id, now=at(5, 9))
    return after_commit


class FollowupCase(unittest.TestCase):
    """A database with Dr. Mehta at Branch A (09:00-13:00 and 16:00-20:00, every day),
    one patient who has written to the clinic (WhatsApp) and one staff booked by phone."""

    def setUp(self):
        self.conn = make_db()
        self.wa_patient = self.add_patient("Sunita Devi", "9876543210")
        self.staff_patient = self.add_patient("Rakesh Verma", STAFF_PHONE)
        self.inbound(WA, at(5, 9))

    def add_patient(self, name, phone):
        cur = self.conn.execute("INSERT INTO patients (name, phone) VALUES (?, ?)", (name, phone))
        self.conn.commit()
        return cur.lastrowid

    def inbound(self, wa_id, when, text="hello", n=[0]):
        n[0] += 1
        self.conn.execute(
            "INSERT INTO wa_messages (wa_message_id, wa_id, message_type, raw_text, received_at) VALUES (?, ?, 'text', ?, ?)",
            ("wamid.fu{}".format(n[0]), wa_id, text, when.utc.strftime("%Y-%m-%d %H:%M:%S")))
        self.conn.commit()

    def row(self, patient_id=None, due_date=D2, due_time="10:00", doctor_id=1, branch_id=1, diagnosis=None):
        return {"patient_id": patient_id or self.wa_patient, "due_date": due_date, "due_time": due_time,
                "doctor_id": doctor_id, "branch_id": branch_id, "diagnosis": diagnosis}

    def schedule(self, *rows, now=None, with_notify=False, expect=True):
        """Apply a batch the way the Follow-ups tab does."""
        result = followups.apply(self.conn, list(rows), handlers=HANDLERS, after_commit=hooks(with_notify), now=now or at(5, 9))
        if expect:
            self.assertEqual(result["counts"]["failed"], 0, result)
        return result

    def make(self, now=None, **kw):
        """One follow-up; returns its id."""
        return self.schedule(self.row(**kw), now=now)["results"][0]["followup_id"]

    def followup(self, fid):
        return followups.get_followup(self.conn, fid)

    def reminder_rows(self, fid=None):
        sql = "SELECT * FROM followup_reminders" + (" WHERE followup_id = {}".format(fid) if fid else "") + " ORDER BY id"
        return [dict(r) for r in self.conn.execute(sql)]

    def states(self, fid):
        return {r["kind"]: r["state"] for r in self.reminder_rows(fid) if (r["slot_date"], r["slot_time"]) == (
            self.followup(fid)["due_date"], self.followup(fid)["due_time"])}

    def notes(self, event=None):
        sql = "SELECT * FROM notifications" + (" WHERE event = '{}'".format(event) if event else "") + " ORDER BY id"
        return [dict(r) for r in self.conn.execute(sql)]

    def appointment(self, appointment_id):
        return dict(self.conn.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,)).fetchone())

    def change_appointment(self, intent, slots):
        proposal = core.propose(self.conn, intent, slots)
        _, entity_id = core.confirm(self.conn, proposal, HANDLERS)
        hooks()(self.conn, intent, slots, entity_id)
        return entity_id

    def audit(self, intent=None):
        sql = "SELECT * FROM audit_log" + (" WHERE intent = '{}'".format(intent) if intent else "") + " ORDER BY id"
        return [dict(r) for r in self.conn.execute(sql)]
