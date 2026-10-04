"""Randomised (seeded, so reproducible) conversations through the FULL
automatic path -- dialog manager + guard policy + propose/confirm -- against an
in-memory database. This is the successor of the old "the agent never writes"
fuzz invariant (which still holds for the pure dialog manager in
tests/test_conversation_fuzz.py). The new invariant, whatever a patient types
or taps in whatever order:

  * writes happen only through the guarded automatic path, and only for
    book / cancel / reschedule: every audit_log row belongs to an
    [auto:whatsapp-agent] proposal, no proposal is left pending, nothing
    else (patients, visits, follow-ups, expenses, staff) is ever written;
  * every automatic write has exactly one audit row AND one activity row;
  * no double booking, nothing inside a booking block, nothing in the past;
  * a cancel / reschedule only ever touches the SENDER'S OWN appointment;
  * caps hold (3 active per number, the daily automation cap);
  * switch OFF or a human takeover means no automatic write at all.
"""
import random
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import auto_actions, auto_policy, booking_blocks, conv_runtime, conversation as cv, scheduling, settings
from clinic.adapters.registry import build_write_handlers, get_adapters
from clinic.entity_resolution import last10_digits
from tests.test_conversation_fuzz import CHOICES, SENDERS, TEXTS

SCHEMA = (Path(__file__).resolve().parents[1] / "clinic" / "schema.sql").read_text()
NOW = datetime(2026, 10, 5, 10, 0)
HANDLERS = build_write_handlers(*get_adapters())
OTHER_TABLES = ("patients", "visits", "followups", "expenses", "staff", "attendance")


def make_db(rng):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO patients (name, phone) VALUES ('Sunita Devi', '9876543210')")
    conn.execute("INSERT INTO patients (name, phone) VALUES ('Bhavna', '9111122223')")
    rows = [(1, None, None, "2026-10-06", "09:00"), (1, None, None, "2026-10-09", "10:00"),
            (2, None, None, "2026-10-06", "09:15"), (None, "Raju", "9333344445", "2026-10-07", "11:00"),
            (None, "Busy", "9000000000", "2026-10-06", "16:00"), (None, "Busy", "9000000000", "2026-10-06", "16:15"),
            (1, None, None, "2026-10-05", "10:30")]
    for pid, name, phone, d, t in rows:
        conn.execute("INSERT INTO appointments (patient_id, patient_name, patient_phone, appt_date, start_time) "
                     "VALUES (?, ?, ?, ?, ?)", (pid, name, phone, d, t))
    conn.commit()
    for _ in range(rng.choice([0, 0, 1, 2])):
        day = rng.choice(["2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"])
        if rng.random() < 0.5:
            booking_blocks.add_block(conn, day, day, reason="fuzz")
        else:
            start = rng.choice(["09:00", "10:00", "16:00", "17:00"])
            end = "{:02d}:00".format(int(start[:2]) + 2)
            booking_blocks.add_block(conn, day, day, start, end, reason="fuzz")
    return conn


class AutoFuzzTests(unittest.TestCase):
    def upcoming_for(self, conn, wa_id, clock):
        """Booked/confirmed appointments of this number that have not started yet."""
        target = last10_digits(wa_id)
        key = (clock.date().isoformat(), clock.strftime("%H:%M"))
        n = 0
        for r in conn.execute("SELECT a.appt_date, a.start_time, COALESCE(a.patient_phone, p.phone) AS phone FROM appointments a "
                              "LEFT JOIN patients p ON p.id = a.patient_id WHERE a.status IN ('booked','confirmed')"):
            if last10_digits(r["phone"] or "") == target and (r["appt_date"], r["start_time"]) >= key:
                n += 1
        return n

    def check_invariants(self, conn, cap, enabled, human_senders, convo):
        for t in OTHER_TABLES:
            expected = 2 if t == "patients" else 0
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0], expected, t)
        # every proposal is an automatic one, none left pending
        for p in conn.execute("SELECT * FROM proposals"):
            self.assertTrue(p["source_text"].startswith(auto_actions.AUTO_PREFIX), p["source_text"])
            self.assertIn(p["status"], ("confirmed", "rejected"))
            self.assertIn(p["intent"], auto_policy.AUTO_INTENTS)
        audits = conn.execute("SELECT * FROM audit_log").fetchall()
        if not enabled:
            self.assertEqual(audits, [], "automation is OFF but something was written")
        for a in audits:
            self.assertIn(a["intent"], auto_policy.AUTO_INTENTS)
            proposal = conn.execute("SELECT * FROM proposals WHERE id = ?", (a["proposal_id"],)).fetchone()
            self.assertEqual(proposal["status"], "confirmed")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM audit_log WHERE proposal_id = ?", (proposal["id"],)).fetchone()[0], 1)
            event = {"book_appointment": "auto_booked", "cancel_appointment": "auto_cancelled",
                     "reschedule_appointment": "auto_rescheduled"}[a["intent"]]
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM patient_activity WHERE proposal_id = ? AND event = ?",
                                          (proposal["id"], event)).fetchone()[0], 1, "an automatic write without its activity row")
            # whose message was it?
            msg_id = int(proposal["source_text"].split("#")[1].split()[0])
            wa_id = conn.execute("SELECT wa_id FROM wa_messages WHERE id = ?", (msg_id,)).fetchone()["wa_id"]
            self.assertNotIn(wa_id, human_senders, "an automatic write for a chat staff had taken over")
            if a["intent"] != "book_appointment":
                appt = conn.execute("SELECT a.patient_id, COALESCE(a.patient_phone, p.phone) AS phone FROM appointments a "
                                    "LEFT JOIN patients p ON p.id = a.patient_id WHERE a.id = ?", (a["entity_id"],)).fetchone()
                self.assertEqual(last10_digits(appt["phone"] or ""), last10_digits(wa_id), "touched someone else's appointment")
        # activity rows only reference real proposals; every auto_* row has its audit row
        for r in conn.execute("SELECT * FROM patient_activity WHERE event IN ('auto_booked','auto_cancelled','auto_rescheduled')"):
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM audit_log WHERE proposal_id = ?", (r["proposal_id"],)).fetchone()[0], 1)
        # the schedule is sane
        live = conn.execute("SELECT * FROM appointments WHERE status IN ('booked','confirmed')").fetchall()
        seen = set()
        for a in live:
            key = (a["appt_date"], a["start_time"])
            if (a["id"] > 7):   # appointments the fixtures did not create
                self.assertNotIn(key, seen, "double booking at {}".format(key))
                self.assertIn(a["start_time"], scheduling.slot_grid())
                self.assertGreaterEqual((a["appt_date"], a["start_time"]), (NOW.date().isoformat(), "00:00"))
            seen.add(key)
        booked = conn.execute("SELECT COUNT(*) FROM patient_activity WHERE event = 'auto_booked'").fetchone()[0]
        self.assertLessEqual(booked, cap, "the daily automation cap was exceeded")

    def next_input(self, rng, conn, wa_id, clock):
        roll = rng.random()
        if roll < 0.72:
            s = cv.load_session(conn, wa_id, clock)
            step = s["step"]
            if s["goal"] is None:
                return rng.choice(["book", "mujhe appointment chahiye", "cancel my appointment",
                                   "reschedule my appointment", "book tomorrow 4 pm"]), None
            if step == "name":
                return rng.choice(["Sunita Devi", "Raju", "Neeta Sharma"]), None
            if step == "which":
                return "", rng.choice(["appt:1", "appt:2", "appt:3", "appt:4", "appt:7"])
            if step == "day":
                return "", rng.choice(["day:2026-10-06", "day:2026-10-07", "day:2026-10-05", "day:2026-10-08"])
            if step == "time":
                return "", "slot:{}T{}".format(rng.choice(["2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]),
                                              rng.choice(cv._slot_starts()))
            if step == "confirm":
                return "", rng.choice(["confirm:yes"] * 5 + ["confirm:change", "confirm:no"])
        if roll < 0.85:
            return "", rng.choice(CHOICES)
        return rng.choice(TEXTS), None

    @staticmethod
    def stale_once(real):
        calls = {"n": 0}

        def fake(conn, iso, now, for_wa_id=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return [t for t in cv._slot_starts() if iso > now.date().isoformat() or t > now.strftime("%H:%M")]
            return real(conn, iso, now, for_wa_id=for_wa_id)
        return fake

    def test_random_conversations_hold_the_automatic_path_invariants(self):
        rng = random.Random(20261006)
        auto_writes = 0
        for convo in range(500):
            conn = make_db(rng)
            enabled = rng.random() > 0.15
            cap = rng.choice([1, 3, 40, 40])
            settings.set_auto_enabled(conn, enabled)
            settings.set_auto_daily_cap(conn, cap)
            human = set()
            if rng.random() < 0.15:
                human.add(rng.choice(SENDERS))
                cv.set_mode(conn, next(iter(human)), "human", now=NOW)
            runner = lambda c, **kw: auto_actions.handle_request(c, handlers=HANDLERS, after_commit=None, **kw)
            clock = NOW
            picker_label = rng.choice(["unclear", "book", "cancel", "reschedule", "status", "greeting", "human"])
            for turn in range(16):
                wa_id = rng.choice(SENDERS)
                clock += timedelta(minutes=rng.choice([0, 0, 1, 2, 5, 40]))
                text, choice = self.next_input(rng, conn, wa_id, clock)
                if rng.random() < 0.1:      # staff add a block behind the patients' backs
                    day = rng.choice(["2026-10-06", "2026-10-07"])
                    booking_blocks.add_block(conn, day, day, "16:00", "18:00", "fuzz")
                if rng.random() < 0.12:      # ...or book a slot the patient is about to confirm
                    day, when = rng.choice(["2026-10-06", "2026-10-07", "2026-10-08"]), rng.choice(cv._slot_starts())
                    if scheduling.is_slot_free(conn, day, when, 15):
                        conn.execute("INSERT INTO appointments (patient_name, patient_phone, appt_date, start_time) "
                                     "VALUES ('Staff', '9000000001', ?, ?)", (day, when))
                        conn.commit()
                stale = rng.random() < 0.3  # the dialog's view of free times is out of date for one look
                cur = conn.execute("INSERT INTO wa_messages (wa_message_id, wa_id, message_type, raw_text) VALUES (?, ?, 'text', ?)",
                                   ("fuzz{}-{}".format(convo, turn), wa_id, text))
                conn.commit()
                with self.subTest(convo=convo, turn=turn, text=text[:30], choice=choice), \
                        (mock.patch.object(cv, "free_times", self.stale_once(cv.free_times)) if stale else mock.patch.object(cv, "NOOP", 0, create=True)):
                    result = conv_runtime.process_inbound(
                        conn, cur.lastrowid, wa_id, text, choice, now=clock, picker=lambda t: picker_label, auto=runner)
                    if result.auto is not None and result.auto.kind == "committed":
                        auto_writes += 1
                        self.assertTrue(enabled)
                        self.assertIsNone(result.handoff)
                        if result.auto.intent == "book_appointment":
                            self.assertLessEqual(self.upcoming_for(conn, wa_id, clock), auto_policy.MAX_ACTIVE_PER_NUMBER,
                                                 "a number went over its active-bookings cap")
                        if result.auto.intent != "cancel_appointment":
                            # right now (a block staff add LATER may legitimately cover it): not blocked, not past
                            appt = conn.execute("SELECT appt_date, start_time FROM appointments WHERE id = ?",
                                                (result.auto.appointment_id,)).fetchone()
                            self.assertIsNone(scheduling.block_reason(conn, appt["appt_date"], appt["start_time"]),
                                              "committed inside a booking block")
                            self.assertGreater((appt["appt_date"], appt["start_time"]),
                                               (clock.date().isoformat(), clock.strftime("%H:%M")), "committed in the past")
                    for reply in result.replies:
                        self.assertLessEqual(len(reply.text), 1024)
                        if reply.buttons:
                            self.assertLessEqual(len(reply.buttons), 3)
                        if reply.rows:
                            self.assertLessEqual(len(reply.rows), 10)
                    if result.handoff is not None:
                        # escalated requests are plain inbox proposals: nothing written for them
                        self.assertEqual(result.handoff.slots["via"], "conversation")
            self.check_invariants(conn, cap, enabled, human, convo)
            conn.close()
        self.assertGreater(auto_writes, 60, "the fuzz script should reach automatic writes or it proves little")


if __name__ == "__main__":
    unittest.main()
