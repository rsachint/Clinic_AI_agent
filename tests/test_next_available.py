"""The "next available appointment" read and the booking question that can follow it (clinic/next_available.py).

  * the forward search, with a fixed clock: today's passed times, closed days, closures and blocks, one doctor's own
    schedule, existing bookings, the branch filter, the number of slots and the 30-day cap, and that it only reads;
  * choosing the doctor in code: one, nobody or several (never a guess), and the sentence readers;
  * the whitelist (clinic/query_tool.py) and the tool schema that carry the new options;
  * the wording of the answers (English, Hinglish);
  * the "... and book it for <name>" tail: found conservatively, and what a yes / no / another time does, in BOTH
    architectures, with a scripted planner (no model). A yes only builds the normal booking card: nothing is written
    before a person presses Approve, and a yes never approves a card that is already on screen.
All dates are fixed (the replay clock: Friday 2026-10-09, 10:00); nothing reaches a model or the network."""
import contextlib
import os
import shutil
import subprocess
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clinic import architecture, branches, next_available, query_tool  # noqa: E402
from clinic.adapters.local_sqlite import LocalSQLiteAdapter  # noqa: E402
from clinic.nlu import planner, tools  # noqa: E402
from clinic.nlu.answer import compose_answer  # noqa: E402
from clinic.pipeline import ParsedResult, PipelineError, ReadResult  # noqa: E402
from clinic.voice_context import AskResult, Note, VoiceContext  # noqa: E402
from clinic.voice_turns import handle_pick, handle_turn  # noqa: E402
from scripts.replay import configs, engine  # noqa: E402
from tests.replay_cases.fixtures import BASE_SETUP, BRANCHES, with_setup  # noqa: E402

NOW = datetime(2026, 10, 9, 10, 0)              # Friday 10:00
TODAY, SAT, SUN, MON = "2026-10-09", "2026-10-10", "2026-10-11", "2026-10-12"
SENTENCE = "Find the next available appointment with Doctor Mehta and book it for a patient named Neha Gupta."
SHARMAS = [{"code": "D", "name": "Branch D", "address": "Sector 14", "pin_code": "122001", "doctor": "Dr. Anil Sharma", "hours": ("09:00", "13:00")},
           {"code": "E", "name": "Branch E", "address": "Sector 21", "pin_code": "122002", "doctor": "Dr. Sunil Sharma", "hours": ("14:00", "18:00")}]


def clinic(setup=None):
    """(conn, ids): the replay's fake clinic (Branch A Dr. Mehta 09-13 and 16-20, B Dr. Rao 10-14, C Dr. Iyer 09-12, every
    day; Saturday's list at A: Sunita 10:00, Rakesh 11:00, Manju 12:00)."""
    conn, ids = engine.build_clinic(setup or BASE_SETUP)
    return conn, ids


def doctor_id(conn, name):
    return next(d["id"] for d in branches.list_doctors(conn) if d["name"] == name)


def block(conn, start, end=None, from_time=None, to_time=None, branch_id=None, doctor=None):
    conn.execute("INSERT INTO booking_blocks (start_date, end_date, start_time, end_time, reason, active, branch_id, doctor_id) "
                 "VALUES (?, ?, ?, ?, 'test', 1, ?, ?)", (start, end or start, from_time, to_time, branch_id, doctor))
    conn.commit()


def times(found):
    return [(s["date"], s["time"]) for s in found]


# -- the forward search ------------------------------------------------------------------------------------

class Search(unittest.TestCase):
    def setUp(self):
        self.conn, self.ids = clinic()
        self.addCleanup(self.conn.close)
        self.mehta = doctor_id(self.conn, "Dr. Mehta")

    def first(self, now=NOW, **kw):
        kw.setdefault("branch_ids", [1])
        found = next_available.search(self.conn, "2026-10-09", now, **kw)
        return times(found)

    def test_todays_times_that_have_passed_are_skipped(self):
        self.assertEqual(self.first(), [(TODAY, "10:30")])
        self.assertEqual(self.first(datetime(2026, 10, 9, 9, 0)), [(TODAY, "09:30")])
        self.assertEqual(self.first(datetime(2026, 10, 9, 10, 30)), [(TODAY, "11:00")])        # the slot starting now is gone too

    def test_after_the_last_slot_of_the_day_it_is_tomorrow(self):
        self.assertEqual(self.first(datetime(2026, 10, 9, 19, 30)), [(SAT, "09:00")])
        self.assertEqual(self.first(datetime(2026, 10, 9, 21, 0)), [(SAT, "09:00")])

    def test_between_the_two_shifts_the_afternoon_shift_is_next(self):
        self.assertEqual(self.first(datetime(2026, 10, 9, 14, 0)), [(TODAY, "16:00")])

    def test_a_start_in_the_past_means_today(self):
        found = next_available.search(self.conn, "2026-10-01", NOW, branch_ids=[1])
        self.assertEqual(times(found), [(TODAY, "10:30")])

    def test_a_later_start_day_is_honoured(self):
        found = next_available.search(self.conn, MON, NOW, branch_ids=[1])
        self.assertEqual(times(found), [(MON, "09:00")])

    def test_existing_bookings_are_not_free(self):
        self.conn.execute("INSERT INTO appointments (patient_name, patient_phone, appt_date, start_time, duration_minutes, status, branch_id) "
                          "VALUES ('Walk In', '9000000099', ?, '10:30', 30, 'booked', 1)", (TODAY,))
        self.conn.commit()
        self.assertEqual(self.first(), [(TODAY, "11:00")])
        # a cancelled booking frees its slot again
        self.conn.execute("UPDATE appointments SET status = 'cancelled' WHERE patient_name = 'Walk In'")
        self.conn.commit()
        self.assertEqual(self.first(), [(TODAY, "10:30")])

    def test_saturdays_three_bookings_are_skipped_in_a_longer_list(self):
        found = next_available.search(self.conn, SAT, NOW, limit=6, branch_ids=[1])
        self.assertEqual([t for _, t in times(found)], ["09:00", "09:30", "10:30", "11:30", "12:30", "16:00"])

    def test_a_whole_day_closure_for_the_branch_skips_that_day(self):
        block(self.conn, TODAY, TODAY, branch_id=1)
        self.assertEqual(self.first(), [(SAT, "09:00")])
        block(self.conn, SAT, SUN, branch_id=1)
        self.assertEqual(self.first(), [(MON, "09:00")])

    def test_a_block_on_part_of_the_day_skips_only_those_hours(self):
        block(self.conn, TODAY, TODAY, from_time="10:00", to_time="12:00", branch_id=1)
        self.assertEqual(self.first(), [(TODAY, "12:00")])

    def test_a_block_for_another_branch_does_not_matter(self):
        block(self.conn, TODAY, TODAY, branch_id=2)
        self.assertEqual(self.first(), [(TODAY, "10:30")])

    def test_a_brand_wide_block_stops_every_branch(self):
        block(self.conn, TODAY, TODAY)
        self.assertEqual(self.first(branch_ids=[2]), [(SAT, "10:00")])

    def test_a_doctors_leave_removes_only_that_doctors_slots(self):
        block(self.conn, TODAY, SAT, doctor=self.mehta)
        self.assertEqual(self.first(doctor_id=self.mehta), [(SUN, "09:00")])

    def test_a_closed_branch_has_no_free_slot(self):
        branches.set_status(self.conn, 2, "closed", reason="test")
        self.assertEqual(self.first(branch_ids=[2], days=14), [])

    def test_only_the_asked_doctors_windows_count(self):
        late = branches.add_doctor(self.conn, "Dr. Late")
        for weekday in range(7):
            branches.add_schedule(self.conn, late, 1, weekday, "13:00", "16:00")
        self.assertEqual(self.first(), [(TODAY, "10:30")])                                     # nobody named: the first slot of any doctor
        self.assertEqual(self.first(doctor_id=late), [(TODAY, "13:00")])
        everything = next_available.search(self.conn, TODAY, NOW, limit=20, branch_ids=[1], doctor_id=self.mehta)
        self.assertTrue(all(not "13:00" <= s["time"] < "16:00" for s in everything))        # never Dr. Late's hours
        self.assertEqual({s["doctor_id"] for s in everything}, {self.mehta})

    def test_a_doctor_who_works_some_weekdays_only_is_found_on_those_days(self):
        rao = doctor_id(self.conn, "Dr. Rao")
        self.conn.execute("DELETE FROM doctor_schedules WHERE doctor_id = ? AND weekday NOT IN (0, 2)", (rao,))
        self.conn.commit()
        self.assertEqual(self.first(branch_ids=[2], doctor_id=rao), [(MON, "10:00")])
        wed = next_available.search(self.conn, "2026-10-13", NOW, branch_ids=[2], doctor_id=rao)
        self.assertEqual(times(wed), [("2026-10-14", "10:00")])

    def test_a_schedule_that_has_not_started_yet_is_not_free(self):
        iyer = doctor_id(self.conn, "Dr. Iyer")
        self.conn.execute("UPDATE doctor_schedules SET valid_from = '2026-10-25' WHERE doctor_id = ?", (iyer,))
        self.conn.commit()
        self.assertEqual(self.first(branch_ids=[3], doctor_id=iyer, days=14), [])
        self.assertEqual(self.first(branch_ids=[3], doctor_id=iyer, days=30), [("2026-10-25", "09:00")])

    def test_the_branch_filter(self):
        self.assertEqual(self.first(branch_ids=[2]), [(TODAY, "10:30")])
        found = next_available.search(self.conn, TODAY, NOW, limit=3, branch_ids=[1, 2, 3])
        self.assertEqual([(s["time"], s["branch_id"]) for s in found], [("10:30", 1), ("10:30", 2), ("10:30", 3)])
        self.assertEqual([s["branch_id"] for s in next_available.search(self.conn, TODAY, NOW, branch_ids=[3])], [3])

    def test_the_slot_carries_the_doctor_on_duty(self):
        (slot,) = next_available.search(self.conn, TODAY, NOW, branch_ids=[3])
        self.assertEqual(branches.doctor_label(self.conn, slot["doctor_id"]), "Dr. Iyer")

    def test_limit_and_the_slot_cap(self):
        self.assertEqual(len(next_available.search(self.conn, TODAY, NOW, limit=3, branch_ids=[1])), 3)
        self.assertEqual(len(next_available.search(self.conn, TODAY, NOW, limit=500, branch_ids=[1])), next_available.MAX_SLOTS)
        self.assertEqual(len(next_available.search(self.conn, TODAY, NOW, limit=0, branch_ids=[1])), 1)
        self.assertEqual(next_available.MAX_SLOTS, 20)

    def test_the_window_is_capped_at_thirty_days(self):
        iyer = doctor_id(self.conn, "Dr. Iyer")
        # Dr. Iyer starts on day 30 from today (2026-11-08) and on day 29 for the second clinic below
        self.conn.execute("UPDATE doctor_schedules SET valid_from = '2026-11-08' WHERE doctor_id = ?", (iyer,))
        self.conn.commit()
        self.assertEqual(self.first(branch_ids=[3], doctor_id=iyer, days=1000), [])             # day 31 is never looked at
        self.conn.execute("UPDATE doctor_schedules SET valid_from = '2026-11-07' WHERE doctor_id = ?", (iyer,))
        self.conn.commit()
        self.assertEqual(self.first(branch_ids=[3], doctor_id=iyer, days=1000), [("2026-11-07", "09:00")])
        self.assertEqual(next_available.MAX_DAYS, 30)
        self.assertEqual(next_available.DEFAULT_DAYS, 14)

    def test_the_window_of_an_end_date(self):
        self.assertEqual(next_available.search_window(TODAY), 14)
        self.assertEqual(next_available.search_window(TODAY, "2026-10-12"), 4)
        self.assertEqual(next_available.search_window(TODAY, "2027-03-01"), 30)
        self.assertEqual(next_available.search_window(TODAY, "2026-10-01"), 1)

    def test_after_a_slot_means_strictly_later(self):
        found = next_available.search(self.conn, TODAY, NOW, limit=2, branch_ids=[1], after=(TODAY, "10:30"))
        self.assertEqual(times(found), [(TODAY, "11:00"), (TODAY, "11:30")])
        found = next_available.search(self.conn, TODAY, NOW, branch_ids=[1], after=(TODAY, "19:30"))
        self.assertEqual(times(found), [(SAT, "09:00")])

    def test_it_only_reads(self):
        before = engine.fingerprint(self.conn)
        next_available.search(self.conn, TODAY, NOW, limit=20, branch_ids=[1, 2, 3])
        self.assertEqual(engine.fingerprint(self.conn), before)
        self.assertEqual(self.conn.execute("PRAGMA query_only").fetchone()[0], 0)               # the connection is left as it was

    def test_it_runs_on_a_read_only_connection(self):
        self.conn.execute("PRAGMA query_only = ON")
        self.assertEqual(self.first(), [(TODAY, "10:30")])
        self.assertEqual(self.conn.execute("PRAGMA query_only").fetchone()[0], 1)

    def test_one_branch_clinics_search_the_only_branch(self):
        conn, _ = clinic()
        self.addCleanup(conn.close)
        conn.execute("UPDATE branches SET active = 0 WHERE id IN (2, 3)")
        conn.commit()
        self.assertEqual(next_available.branch_ids_for(conn), [None])
        self.assertEqual(times(next_available.search(conn, TODAY, NOW, branch_ids=[None])), [(TODAY, "10:30")])

    def test_which_branches_are_searched(self):
        self.assertEqual(next_available.branch_ids_for(self.conn, branch_id=2), [2])
        self.assertEqual(next_available.branch_ids_for(self.conn, all_branches=True), [1, 2, 3])
        self.assertEqual(next_available.branch_ids_for(self.conn, doctor_id=doctor_id(self.conn, "Dr. Iyer")), [3])
        self.assertEqual(next_available.branch_ids_for(self.conn), [None])                  # nothing named: the default branch
        self.assertTrue(next_available.works_at(self.conn, doctor_id(self.conn, "Dr. Iyer"), 3))
        self.assertFalse(next_available.works_at(self.conn, doctor_id(self.conn, "Dr. Iyer"), 2))

    def test_the_rows_group_times_by_day_and_branch(self):
        found = next_available.search(self.conn, SAT, NOW, limit=5, branch_ids=[1])
        rows = next_available.day_rows(self.conn, found)
        self.assertEqual(rows, [{"date": "Sat 10 Oct", "slots": ["09:00", "09:30", "10:30", "11:30", "12:30"], "branch": "Branch A",
                                 "doctor": "Dr. Mehta"}])
        two = next_available.day_rows(self.conn, next_available.search(self.conn, TODAY, NOW, limit=3, branch_ids=[1, 2, 3]))
        self.assertEqual([r["branch"] for r in two], ["Branch A", "Branch B", "Branch C"])


# -- choosing the doctor ----------------------------------------------------------------------------------------------

class Doctors(unittest.TestCase):
    def setUp(self):
        self.conn, _ = clinic(with_setup(branches=BRANCHES + SHARMAS))
        self.addCleanup(self.conn.close)
        branches.add_doctor(self.conn, "Dr. Anil Mehta")                     # shares a surname with Dr. Mehta, no schedule

    def resolve(self, spoken):
        status, found = next_available.resolve_doctor(self.conn, spoken)
        return status, [d["name"] for d in found]

    def test_one_doctor(self):
        for spoken in ("Dr. Rao", "Rao", "doctor rao", "Doctor Rao", "dr rao"):
            with self.subTest(spoken=spoken):
                self.assertEqual(self.resolve(spoken), ("one", ["Dr. Rao"]))
        self.assertEqual(self.resolve("Dr. Anil Sharma"), ("one", ["Dr. Anil Sharma"]))
        self.assertEqual(self.resolve("Sunil Sharma"), ("one", ["Dr. Sunil Sharma"]))

    def test_nobody(self):
        for spoken in ("Dr. Gupta", "Gupta", "", "doctor", "Dr.", "Dr. Rao Gupta"):
            with self.subTest(spoken=spoken):
                self.assertEqual(self.resolve(spoken), ("none", []))

    def test_several_is_never_picked_between(self):
        self.assertEqual(self.resolve("Dr. Sharma"), ("several", ["Dr. Anil Sharma", "Dr. Sunil Sharma"]))
        self.assertEqual(self.resolve("Sharma"), ("several", ["Dr. Anil Sharma", "Dr. Sunil Sharma"]))

    def test_a_full_name_outranks_a_longer_name_that_contains_it(self):
        self.assertEqual(self.resolve("Dr. Mehta"), ("one", ["Dr. Mehta"]))
        self.assertEqual(self.resolve("Dr. Anil Mehta"), ("one", ["Dr. Anil Mehta"]))

    def test_a_devanagari_spelling_of_a_roman_name(self):
        self.assertEqual(self.resolve("डॉक्टर मेहता"), ("one", ["Dr. Mehta"]))
        self.assertEqual(self.resolve("डॉ. अय्यर")[0], "none")             # a skeleton too short to trust (i-y-e-r): the app asks instead
        self.assertEqual(self.resolve("डॉक्टर गुप्ता")[0], "none")

    def test_an_inactive_doctor_is_not_there(self):
        branches.update_doctor(self.conn, doctor_id(self.conn, "Dr. Rao"), active=False)
        self.assertEqual(self.resolve("Dr. Rao"), ("none", []))

    def test_the_doctor_in_a_sentence(self):
        read = lambda text: next_available.doctor_in_sentence(self.conn, text)
        self.assertEqual(read("next available with Doctor Rao"), "Dr. Rao")
        self.assertEqual(read("next available appointment Dr. Mehta ke saath"), "Dr. Mehta")
        self.assertEqual(read("next available with Rao"), "Dr. Rao")                         # a bare name after "with"
        self.assertEqual(read("Rao ke saath agla available slot"), "Dr. Rao")
        self.assertEqual(read("next available with Dr. Anil Sharma please"), "Dr. Anil Sharma")
        self.assertEqual(read("next available with Dr. Gupta"), "Dr. Gupta")                  # a title and a name nobody has: asked later
        self.assertEqual(read("डॉक्टर मेहता के साथ अगला उपलब्ध"), "Dr. मेहता")

    def test_a_surname_alone_in_a_sentence_is_not_a_doctor(self):
        read = lambda text: next_available.doctor_in_sentence(self.conn, text)
        self.assertIsNone(read("next available appointment for Seema Rao"))                    # a patient called Rao
        self.assertIsNone(read("what is the next available slot"))
        self.assertIsNone(read("show free slots, the doctor on duty is fine"))
        self.assertIsNone(read("next available with the doctor"))
        self.assertIsNone(read("next available with Neha"))                                    # "with <someone>" that is not a doctor's name

    def test_the_sentence_fills_what_the_planner_left_out_and_never_overrides(self):
        sentence = "Find the next available appointment with Dr. Rao and book it for Neha Gupta."
        slots = next_available.read_sentence(self.conn, sentence, {"appt_date": None})
        self.assertEqual((slots["doctor"], slots["next_available"], slots["then_book_for"]), ("Dr. Rao", True, "Neha Gupta"))
        given = next_available.read_sentence(self.conn, sentence, {"doctor": "Dr. Sunil Sharma", "next_available": True, "limit": 2,
                                                                  "then_book_for": "Someone Else"})
        self.assertEqual((given["doctor"], given["limit"], given["then_book_for"]), ("Dr. Sunil Sharma", 2, "Someone Else"))
        # a model's doctor that matches nobody gives way to the title and name actually said
        fixed = next_available.read_sentence(self.conn, sentence, {"doctor": "the doctor"})
        self.assertEqual(fixed["doctor"], "Dr. Rao")


# -- the words ------------------------------------------------------------------------------------------------------------

class Words(unittest.TestCase):
    def test_the_next_cue(self):
        for text in ("next available appointment", "Next free slot with Dr. Rao", "earliest slot", "soonest available", "first available",
                     "when is the next open slot", "agla available appointment", "pehla khaali slot", "agla khaali", "अगला उपलब्ध अपॉइंटमेंट",
                     "next few available slots", "next three available slots"):
            with self.subTest(text=text):
                self.assertTrue(next_available.asks_next(text), text)
        for text in ("what is free tomorrow", "free slots on Monday", "next appointment of Amit", "the first patient", "available doctors"):
            with self.subTest(text=text):
                self.assertFalse(next_available.asks_next(text), text)

    def test_how_many(self):
        self.assertEqual(next_available.how_many("next three available slots"), 3)
        self.assertEqual(next_available.how_many("next 5 free slots"), 5)
        self.assertEqual(next_available.how_many("next few available slots with Dr. Rao"), 3)
        self.assertEqual(next_available.how_many("next available slots"), 3)
        self.assertEqual(next_available.how_many("next 50 free slots"), 20)
        self.assertEqual(next_available.how_many("agle teen khaali slot"), 3)
        self.assertIsNone(next_available.how_many("next available appointment"))
        self.assertIsNone(next_available.how_many("what is free tomorrow"))

    def test_the_booking_tail_is_found(self):
        for text, name, rest in (
            (SENTENCE, "Neha Gupta", "Find the next available appointment with Doctor Mehta"),
            ("next available with Dr. Rao and book it for Neha Gupta", "Neha Gupta", "next available with Dr. Rao"),
            ("next available with Dr. Rao, then book it for Neha", "Neha", "next available with Dr. Rao"),
            ("Next available slot. Book it for Neha Gupta.", "Neha Gupta", "Next available slot"),
            ("find the first free slot and book the first one for Rakesh Verma tomorrow", "Rakesh Verma", "find the first free slot"),
            ("earliest slot with Dr. Iyer and also book that for a new patient called Asha Rao", "Asha Rao", "earliest slot with Dr. Iyer"),
            ("next available and book a patient named Neha Gupta", "Neha Gupta", "next available"),
            ("agla available appointment Dr. Mehta ke saath aur Neha Gupta ke liye book karo", "Neha Gupta", "agla available appointment Dr. Mehta ke saath"),
            ("agla khaali slot phir Neha ko usme book kar do", "Neha", "agla khaali slot"),
            ("डॉक्टर मेहता के साथ अगला उपलब्ध अपॉइंटमेंट और नेहा गुप्ता के लिए बुक करो", "नेहा गुप्ता", "डॉक्टर मेहता के साथ अगला उपलब्ध अपॉइंटमेंट"),
        ):
            with self.subTest(text=text):
                found, find = next_available.book_tail(text)
                self.assertEqual((found, find), (name, rest))

    def test_the_tail_reader_is_conservative(self):
        for text in (
            "next available appointment with Dr. Mehta, Neha Gupta called to book one",          # "book" and a name, but no booking clause
            "show free slots, we can book for Neha later",                                         # "book for" without it / and / then
            "what is the next available slot to book for Neha",
            "next available slot and I will book it for Neha",                                     # not a command to the assistant
            "next available and book it for me",
            "next available and book it for tomorrow",
            "next available and book it for tomorrow at 5",
            "next available and book it for the doctor",
            "next available and book it for Monday",
            "show free slots tomorrow. Book Neha Gupta.",                                          # no "for" and no "patient named"
            "Neha Gupta wants the next available slot",
            "who booked it for Neha Gupta",
            "book it for Neha Gupta",                                                              # nothing was found first
            "agla available slot, usko book karo",
        ):
            with self.subTest(text=text):
                self.assertEqual(next_available.book_tail(text), (None, text))

    def test_the_reply_to_the_offer(self):
        for text in ("yes", "Yes please", "yeah", "ok", "okay", "ok book it", "book it", "sure", "haan", "haan kar do", "ha", "हाँ", "ठीक है",
                     "yes, go ahead", "theek hai", "bilkul"):
            with self.subTest(text=text):
                self.assertEqual(next_available.read_answer(text), "yes", text)
        for text in ("no, the next one", "another time", "next one", "the next one please", "no, another", "something else", "agla wala",
                     "koi aur time", "doosra time", "दूसरा समय", "later slot", "not that, another one"):
            with self.subTest(text=text):
                self.assertEqual(next_available.read_answer(text), "next", text)
        for text in ("no", "no thanks", "nahi", "nope", "नहीं", "don't book it", "cancel"):
            with self.subTest(text=text):
                self.assertEqual(next_available.read_answer(text), "no", text)
        for text in ("", "how many patients are registered", "make it 5 pm", "yes but at 5 pm", "show the appointments for tomorrow",
                     "book Rakesh Verma tomorrow at 5 pm", "yes yes yes yes yes yes yes yes yes", "Dr. Rao"):
            with self.subTest(text=text):
                self.assertIsNone(next_available.read_answer(text), text)


# -- the whitelist and the tool ---------------------------------------------------------------------------------------------

class Whitelist(unittest.TestCase):
    def v(self, **spec):
        return query_tool.validate_spec(dict({"entity": "availability"}, **spec))

    def test_one_day_is_unchanged(self):
        self.assertEqual(self.v(date=TODAY), {"entity": "availability", "aggregate": "list", "date": TODAY})
        self.assertEqual(self.v(date=TODAY, branch="B"), {"entity": "availability", "aggregate": "list", "date": TODAY, "branch": "B"})

    def test_the_doctor_is_allowed_and_stays_spoken_text(self):
        self.assertEqual(self.v(doctor=" Dr. Mehta ")["doctor"], "Dr. Mehta")
        with self.assertRaises(query_tool.QueryError):
            self.v(doctor="x" * 81)

    def test_the_forward_search_options(self):
        self.assertTrue(self.v(next=True)["next"])
        self.assertTrue(self.v(next="true")["next"])
        self.assertNotIn("next", self.v(next=False))
        self.assertNotIn("next", self.v(next="no"))
        self.assertEqual(self.v(limit=3), {"entity": "availability", "aggregate": "list", "limit": 3, "next": True})      # a number of slots is a search
        self.assertEqual(self.v(limit=200)["limit"], query_tool.MAX_SLOTS)                                            # capped
        with self.assertRaises(query_tool.QueryError):
            self.v(limit=500)                                                                                         # beyond the table cap: refused
        self.assertTrue(self.v(date=TODAY, date_to="2026-10-20")["next"])                                              # an end date is a search
        self.assertTrue(self.v(order="earliest")["next"])
        self.assertTrue(self.v(order="oldest")["next"])
        self.assertEqual(self.v(next=True, doctor="Dr. Mehta", branch="A", limit=1, date=TODAY),
                         {"entity": "availability", "aggregate": "list", "next": True, "doctor": "Dr. Mehta", "branch": "A", "limit": 1, "date": TODAY})

    def test_what_is_still_refused(self):
        for spec in ({"order": "newest"}, {"order": "highest"}, {"group_by": "doctor"}, {"fields": ["name"]},
                     {"status": "booked"}, {"patient_name": "Amit"}, {"age_min": 3}, {"time": "10:00"},
                     {"date": TODAY, "date_to": "2026-10-01"}, {"limit": 0}):
            with self.subTest(spec=spec):
                with self.assertRaises(query_tool.QueryError):
                    self.v(**spec)

    def test_only_availability_can_search_ahead(self):
        for entity in ("appointments", "patients", "followups", "cashbook"):
            with self.subTest(entity=entity):
                with self.assertRaises(query_tool.QueryError):
                    query_tool.validate_spec({"entity": entity, "next": True})
                query_tool.validate_spec({"entity": entity, "next": False})                 # a false flag is just absent

    def test_the_whitelist_and_its_caps(self):
        self.assertEqual(query_tool.ENTITY_FILTERS["availability"], ("date", "date_to", "branch", "doctor"))
        self.assertEqual((query_tool.DEFAULT_SEARCH_DAYS, query_tool.MAX_SEARCH_DAYS, query_tool.MAX_SLOTS), (14, 30, 20))
        self.assertEqual(query_tool.FIELDS["availability"], ())                              # no column of a table is exposed
        with self.assertRaises(query_tool.QueryError):
            query_tool.run(self.conn_for_run(), {"entity": "availability", "date": TODAY})         # still answered by the free-slots read

    def conn_for_run(self):
        conn, _ = clinic()
        self.addCleanup(conn.close)
        return conn

    def test_a_secret_cannot_ride_in_on_the_new_arguments(self):
        for spec in ({"sql": "SELECT 1"}, {"where": "1=1"}, {"next": True, "sql": "x"}, {"entity_id": 3}):
            with self.subTest(spec=spec):
                with self.assertRaises(query_tool.QueryError):
                    self.v(**spec)


class ToolSchemaAndMapping(unittest.TestCase):
    def setUp(self):
        self.conn, _ = clinic()
        self.addCleanup(self.conn.close)
        self.ctx = tools.ToolContext(self.conn, date(2026, 10, 9), "")

    def parse(self, **args):
        clean = tools.validate("query", dict({"entity": "availability"}, **args), self.ctx)
        return tools.to_parse_result("query", clean, self.ctx)

    def test_the_query_tool_declares_next_as_a_boolean_and_still_no_id(self):
        props = tools.BY_NAME["query"].schema()["function"]["parameters"]["properties"]
        self.assertEqual(props["next"]["type"], "boolean")
        self.assertIn("next available", props["next"]["description"])
        self.assertEqual(len(tools.TOOLS), 19)
        self.assertFalse([k for k in props if k.endswith("_id") or k == "id"])

    def test_the_failing_call_of_the_live_log_is_now_valid(self):
        intent, slots = self.parse(doctor="Dr. Mehta", branch="A", limit=1)
        self.assertEqual(intent, "check_availability")
        self.assertEqual((slots["doctor"], slots["next_available"], slots["limit"], slots["branch_id"]), ("Dr. Mehta", True, 1, 1))

    def test_one_day_maps_as_before(self):
        self.assertEqual(self.parse(date=SAT), ("check_availability", {"appt_date": SAT}))
        self.assertEqual(self.parse(), ("check_availability", {"appt_date": None}))

    def test_a_doctor_for_one_day_and_a_search_with_an_end_date(self):
        self.assertEqual(self.parse(date=SAT, doctor="Dr. Rao")[1], {"appt_date": SAT, "doctor": "Dr. Rao"})
        slots = self.parse(next=True, date=MON, date_to="2026-10-16", limit=3)[1]
        self.assertEqual((slots["appt_date"], slots["end_date"], slots["limit"], slots["next_available"]), (MON, "2026-10-16", 3, True))

    def test_next_as_a_word_is_accepted_and_nonsense_is_not(self):
        self.assertTrue(self.parse(next="true")[1]["next_available"])
        with self.assertRaises(tools.ToolError):
            tools.validate("query", {"entity": "availability", "next": "maybe"}, self.ctx)

    def test_the_call_is_described_for_the_next_turn(self):
        text = tools.describe_intent("check_availability", {"appt_date": None, "doctor": "Dr. Mehta", "next_available": True, "limit": 1}, "2026-10-09")
        self.assertIn("doctor=Dr. Mehta", text)
        self.assertIn("next=True", text)

    def test_a_month_phrase_does_not_turn_availability_into_a_range(self):
        from clinic.nlu import date_guard
        args, notes = date_guard.crosscheck("query", {"entity": "availability"}, "free slots this month", date(2026, 10, 9))
        self.assertNotIn("date_to", args)
        self.assertEqual(notes, [])


MON = "2026-10-12"


# -- how it is worded ------------------------------------------------------------------------------------------------------------

class Wording(unittest.TestCase):
    class Cite:
        source, as_of = "local clinic records", "just now"

    def say(self, data, lang="en-IN", branch=None):
        text = compose_answer("next_available", data, self.Cite, lang, branch=branch)
        return text.split(" Source:")[0]

    def slot(self, day, time, branch=None):
        return {"day": day, "time": time, "branch": branch}

    def test_one_slot(self):
        data = {"slots": [self.slot("Fri 9 Oct", "16:00", "Branch A")], "doctor": "Dr. Mehta", "days": 14}
        self.assertEqual(self.say(data, branch="Branch A"), "Next available with Dr. Mehta: Fri 9 Oct, 16:00 (Branch A).")
        self.assertEqual(self.say(dict(data, doctor=None), branch="Branch A"), "Next available: Fri 9 Oct, 16:00 (Branch A).")
        self.assertEqual(self.say(dict(data, doctor=None, slots=[self.slot("Fri 9 Oct", "16:00")])), "Next available: Fri 9 Oct, 16:00.")

    def test_several_slots_are_grouped_by_day(self):
        data = {"slots": [self.slot("Fri 9 Oct", "16:00"), self.slot("Fri 9 Oct", "16:30"), self.slot("Sat 10 Oct", "09:00")],
                "doctor": None, "days": 14}
        self.assertEqual(self.say(data), "Next available: Fri 9 Oct 16:00, 16:30; Sat 10 Oct 09:00.")

    def test_slots_at_two_branches_say_which(self):
        data = {"slots": [self.slot("Fri 9 Oct", "10:30", "Branch A"), self.slot("Fri 9 Oct", "10:30", "Branch B")], "doctor": None, "days": 14}
        self.assertEqual(self.say(data), "Next available: Fri 9 Oct 10:30 at Branch A; Fri 9 Oct 10:30 at Branch B.")

    def test_nothing_free(self):
        self.assertEqual(self.say({"slots": [], "doctor": "Dr. Mehta", "days": 14}), "No free slot with Dr. Mehta in the next 14 days.")
        self.assertEqual(self.say({"slots": [], "doctor": None, "days": 14}, branch="Branch A"), "No free slot in the next 14 days (Branch A).")
        self.assertEqual(self.say({"slots": [], "doctor": None, "days": 5}), "No free slot in the next 5 days.")

    def test_a_doctor_who_is_not_at_that_branch(self):
        self.assertEqual(self.say({"slots": [], "doctor": "Dr. Iyer", "days": 0, "not_at_branch": "Branch B"}), "Dr. Iyer does not work at Branch B.")

    def test_hinglish(self):
        data = {"slots": [self.slot("Fri 9 Oct", "16:00", "Branch A")], "doctor": "Dr. Mehta", "days": 14}
        self.assertEqual(self.say(data, "hi-IN", "Branch A"), "Agla khaali slot Dr. Mehta ke saath: Fri 9 Oct, 16:00 (Branch A).")
        many = dict(data, slots=[self.slot("Fri 9 Oct", "16:00"), self.slot("Sat 10 Oct", "09:00")])
        self.assertEqual(self.say(many, "hi-IN"), "Agle khaali slot Dr. Mehta ke saath: Fri 9 Oct 16:00; Sat 10 Oct 09:00.")
        self.assertEqual(self.say({"slots": [], "doctor": "Dr. Mehta", "days": 14}, "hi-IN"), "Agle 14 din mein Dr. Mehta ke saath koi slot khaali nahi hai.")
        self.assertEqual(self.say({"slots": [], "doctor": "Dr. Iyer", "days": 0, "not_at_branch": "Branch B"}, "hi-IN"), "Dr. Iyer Branch B par nahi baithte hain.")

    def test_a_days_slots_for_one_doctor_use_the_existing_wording_with_the_doctor(self):
        data = {"date": SAT, "slots": ["09:00", "09:30"], "doctor": "Dr. Mehta"}
        text = compose_answer("check_availability", data, self.Cite, "en-IN").split(" Source:")[0]
        self.assertEqual(text, "2 free slot(s) on 2026-10-10 with Dr. Mehta: 09:00, 09:30.")
        self.assertEqual(compose_answer("check_availability", {"date": SAT, "slots": []}, self.Cite, "en-IN").split(" Source:")[0],
                         "No slots are free on 2026-10-10.")                                                           # no doctor: exactly as before

    def test_the_booking_question_in_three_languages(self):
        slot = {"date": TODAY, "time": "16:00", "branch_id": 1}
        self.assertEqual(next_available.offer_question("en", "Neha Gupta", slot, "Dr. Mehta", None), "Book Neha Gupta on Fri 9 Oct at 16:00 with Dr. Mehta?")
        self.assertEqual(next_available.offer_question("en", "Neha Gupta", slot, None, "Branch B"), "Book Neha Gupta on Fri 9 Oct at 16:00 (Branch B)?")
        self.assertEqual(next_available.offer_question("hinglish", "Neha Gupta", slot, "Dr. Mehta", None),
                         "Neha Gupta ko Fri 9 Oct 16:00 baje Dr. Mehta ke saath book karun?")
        self.assertEqual(next_available.offer_question("hi", "नेहा गुप्ता", slot, "Dr. Mehta", None),
                         "नेहा गुप्ता को Fri 9 Oct 16:00 बजे Dr. Mehta के साथ बुक करूँ?")
        self.assertEqual(next_available.no_other_text("en", "Dr. Mehta", 14), "No other free slot with Dr. Mehta in the next 14 days. Nothing was booked.")
        self.assertEqual(next_available.language_key("हाँ", "hi-IN"), "hi")
        self.assertEqual(next_available.language_key("haan", "hi-IN"), "hinglish")
        self.assertEqual(next_available.language_key("yes", "en-IN"), "en")


# -- the conversation, in both architectures ------------------------------------------------------------------------------------------

class Flow:
    """The voice assistant on the replay's fake clinic (frozen clock, offline). Subclasses choose the architecture."""

    config = "classic_rules_only"
    mode = "classic"

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(engine.sandbox(configs.get(self.config)))
        self.setup_data = self.clinic_setup()
        self.conn, self.ids = engine.build_clinic(self.setup_data)
        stack.callback(self.conn.close)
        architecture.set_mode(self.conn, self.mode)
        self.adapter = LocalSQLiteAdapter()
        self.clockwork = engine.FakeMonotonic()
        self.ctx = VoiceContext(clock=self.clockwork)
        self.ctx.set_client_branch(1, 1)
        self.backend = planner.FakeBackend(script=[])
        if self.config != "classic_rules_only":
            planner.set_backend(self.backend)
            stack.callback(planner.set_backend, None)
        self.start = engine.fingerprint(self.conn)

    def clinic_setup(self):
        return BASE_SETUP

    def rebuild(self, setup):
        """A different fake clinic for this test (same architecture, nothing written yet)."""
        self.conn.close()
        self.setup_data = setup
        self.conn, self.ids = engine.build_clinic(setup)
        self.addCleanup(self.conn.close)
        architecture.set_mode(self.conn, self.mode)
        self.start = engine.fingerprint(self.conn)

    def say(self, text, *planner_calls, language="en-IN"):
        """One spoken turn; `planner_calls` is what a model would answer for it (used only when a model is asked)."""
        self.backend.script = list(planner_calls)
        return handle_turn(self.ctx, self.conn, text, self.adapter, self.adapter, language, engine.DEFERRED_INTENTS)

    def tap(self, index):
        return handle_pick(self.ctx, self.conn, index, self.adapter, self.adapter, "en-IN", engine.DEFERRED_INTENTS)

    def unchanged(self):
        return engine.fingerprint(self.conn) == self.start

    FIND = ("query", {"entity": "availability", "doctor": "Dr. Mehta", "branch": "A", "limit": 1})
    YES = ("choose_option", {"index": 1})
    NEXT = ("choose_option", {"index": 2})

    # -- the tests ---------------------------------------------------------------------------------------------------------------

    def test_the_sentence_answers_and_asks_one_question(self):
        result = self.say(SENTENCE, self.FIND)
        self.assertIsInstance(result, ReadResult)
        self.assertEqual(result.intent, "check_availability")
        head, _, tail = result.answer_text.partition(" Source:")
        self.assertEqual(head, "Next available with Dr. Mehta: Fri 9 Oct, 10:30 (Branch A). Book Neha Gupta on Fri 9 Oct at 10:30 with Dr. Mehta?")
        self.assertIn("local clinic records", tail)                                       # the usual citation line
        self.assertEqual(result.data, [{"date": "Fri 9 Oct", "slots": ["10:30"], "branch": "Branch A", "doctor": "Dr. Mehta"}])
        self.assertEqual([o["label"] for o in result.options], ["Yes, book it", "Another time"])
        self.assertEqual((self.ctx.pending["kind"], self.ctx.pending["intent"]), ("book_slot", "book_appointment"))
        self.assertTrue(self.unchanged())

    def test_yes_builds_the_booking_card_and_asks_a_new_patients_phone(self):
        self.say(SENTENCE, self.FIND)
        ask = self.say("yes", self.YES)
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.kind, "phone")
        self.assertEqual((ask.slots["patient_name"], ask.slots["appt_date"], ask.slots["start_time"]), ("Neha Gupta", TODAY, "10:30"))
        self.assertEqual(ask.slots["branch_id"], 1)
        self.assertNotIn("_offer", ask.slots)                                             # the offer's bookkeeping never reaches the card
        self.assertNotIn("_question", ask.slots)
        card = self.say("9988776655", ("answer_slot", {"slot": "phone", "value": "9988776655"}))
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.intent, "book_appointment")
        self.assertEqual((card.slots["patient_name"], card.slots["patient_phone"], card.slots["appt_date"], card.slots["start_time"]),
                         ("Neha Gupta", "9988776655", TODAY, "10:30"))
        self.assertEqual(card.slots["branch_id"], 1)
        self.assertEqual(card.slots["notes"], "Requested: Dr. Mehta")
        self.assertIn("Branch A · Dr. Mehta.", card.resolved["notes"])
        self.assertTrue(self.unchanged(), "nothing is written until a person presses Approve")

    def test_a_registered_patient_goes_straight_to_the_card(self):
        self.say("Find the next available appointment with Dr. Mehta and book it for Rakesh Verma.", self.FIND)
        card = self.say("ok book it", self.YES)
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.resolved["patient_label"].split(" (")[0], "Rakesh Verma")
        self.assertEqual((card.slots["appt_date"], card.slots["start_time"]), (TODAY, "10:30"))
        self.assertTrue(self.unchanged())

    def test_the_chip_is_the_yes(self):
        self.say(SENTENCE, self.FIND)
        picked = self.tap(0)
        self.assertEqual(picked[0], "Yes, book it")
        self.assertIsInstance(picked[1], AskResult)
        self.assertEqual(picked[1].kind, "phone")

    def test_the_other_chip_is_another_time(self):
        self.say(SENTENCE, self.FIND)
        label, result = self.tap(1)
        self.assertEqual(label, "Another time")
        self.assertIn("Book Neha Gupta on Fri 9 Oct at 11:00 with Dr. Mehta?", result.answer_text)
        self.assertEqual(self.ctx.pending["kind"], "book_slot")

    def test_no_the_next_one_moves_to_the_next_free_slot_and_asks_again(self):
        self.say(SENTENCE, self.FIND)
        again = self.say("no, the next one", self.NEXT)
        self.assertIsInstance(again, ReadResult)
        self.assertIn("Book Neha Gupta on Fri 9 Oct at 11:00 with Dr. Mehta?", again.answer_text)
        self.assertEqual(self.ctx.pending["slots"]["start_time"], "11:00")
        ask = self.say("yes", self.YES)
        self.assertEqual(ask.slots["start_time"], "11:00")
        self.assertTrue(self.unchanged())

    def test_another_time_never_repeats_a_slot_and_runs_out(self):
        conn_setup = with_setup(appointments=[{"patient": "nalin", "date": d, "time": t, "branch": "B"}
                                              for d in ["2026-10-{:02d}".format(n) for n in range(9, 23)]
                                              for t in ("10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "13:00", "13:30")
                                              if (d, t) not in (("2026-10-12", "10:00"), ("2026-10-13", "10:00"))])
        self.rebuild(conn_setup)
        first = self.say("Find the next available appointment with Dr. Rao and book it for Neha Gupta.",
                         ("query", {"entity": "availability", "doctor": "Dr. Rao", "limit": 1}))
        self.assertIn("Mon 12 Oct at 10:00", first.answer_text)
        second = self.say("another time", self.NEXT)
        self.assertIn("Tue 13 Oct at 10:00", second.answer_text)
        last = self.say("next one", self.NEXT)
        self.assertIsInstance(last, Note)
        self.assertEqual(last.message, "No other free slot with Dr. Rao in the next 14 days. Nothing was booked.")
        self.assertIsNone(self.ctx.pending)
        self.assertTrue(self.unchanged())

    def test_a_plain_no_drops_the_offer(self):
        self.say(SENTENCE, self.FIND)
        note = self.say("no thanks")
        self.assertIsInstance(note, Note)
        self.assertEqual(note.message, "Okay, I won't book it.")
        self.assertIsNone(self.ctx.pending)
        self.assertTrue(self.unchanged())

    def test_never_mind_drops_it_too(self):
        self.say(SENTENCE, self.FIND)
        self.assertIsInstance(self.say("never mind", ("cancel_task", {})), Note)
        self.assertIsNone(self.ctx.pending)

    def test_a_real_new_command_is_not_swallowed_as_an_answer(self):
        self.say(SENTENCE, self.FIND)
        result = self.say("how many patients are registered", ("query", {"entity": "patients", "aggregate": "count"}))
        self.assertIsInstance(result, ReadResult)
        self.assertEqual(result.intent, "patient_count")
        self.assertTrue(self.unchanged())
        if self.mode == "classic":
            self.assertIsNone(self.ctx.pending)

    def test_no_booking_tail_means_no_question(self):
        result = self.say("What is the next available appointment with Dr. Mehta?", ("query", {"entity": "availability", "doctor": "Dr. Mehta", "next": True}))
        self.assertEqual(result.answer_text.split(" Source:")[0], "Next available with Dr. Mehta: Fri 9 Oct, 10:30 (Branch A).")
        self.assertIsNone(self.ctx.pending)
        self.assertIsNone(result.options)

    def test_the_word_book_elsewhere_in_the_sentence_is_no_booking_request(self):
        result = self.say("What is the next available appointment with Dr. Mehta, Neha Gupta called to book one",
                          ("query", {"entity": "availability", "doctor": "Dr. Mehta", "next": True}))
        self.assertIsNone(self.ctx.pending)
        self.assertNotIn("Neha", result.answer_text)

    def test_no_free_slot_means_a_plain_sentence_and_no_question(self):
        self.rebuild(with_setup(appointments=[{"patient": "nalin", "date": "2026-10-{:02d}".format(n), "time": t, "branch": "B"}
                                              for n in range(9, 23)
                                              for t in ("10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "13:00", "13:30")]))
        result = self.say("Find the next available appointment with Dr. Rao and book it for Neha Gupta.",
                          ("query", {"entity": "availability", "doctor": "Dr. Rao", "branch": "B", "limit": 1}))
        self.assertEqual(result.answer_text.split(" Source:")[0], "No free slot with Dr. Rao in the next 14 days (Branch B).")
        self.assertIsNone(self.ctx.pending)
        self.assertIsNone(result.data)

    def test_a_yes_with_nothing_open_books_nothing(self):
        for text in ("yes", "haan", "ok"):
            with self.subTest(text=text):
                try:
                    self.say(text)
                except PipelineError:
                    pass
        self.assertTrue(self.unchanged())

    def test_the_offer_expires_with_the_conversation_memory(self):
        self.say(SENTENCE, self.FIND)
        self.clockwork.now += 11 * 60
        with self.assertRaises(PipelineError):
            self.say("yes")
        self.assertIsNone(self.ctx.pending)
        self.assertTrue(self.unchanged())

    def test_a_yes_while_a_card_is_open_answers_the_question_and_never_approves_the_card(self):
        card = self.say("book Rakesh Verma tomorrow at 5 pm", ("book_appointment", {"patient_name": "Rakesh Verma", "date": SAT, "time": "17:00"}))
        self.assertIsInstance(card, ParsedResult)
        self.say(SENTENCE, self.FIND)
        ask = self.say("yes", self.YES)
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.slots["patient_name"], "Neha Gupta")
        self.assertTrue(self.unchanged())

    def test_a_yes_after_the_card_is_built_does_not_approve_it(self):
        self.say("Find the next available appointment with Dr. Mehta and book it for Rakesh Verma.", self.FIND)
        self.assertIsInstance(self.say("ok book it", self.YES), ParsedResult)
        for text in ("yes", "ok approve it", "haan kar do"):
            with self.subTest(text=text):
                try:
                    result = self.say(text)
                except PipelineError:
                    result = None
                self.assertNotIsInstance(result, ParsedResult)
                self.assertTrue(self.unchanged())
        self.assertIsNotNone(self.ctx.open_card)                                           # the card is still waiting for a person

    def test_the_doctor_who_matches_nobody_is_asked_not_guessed(self):
        ask = self.say("Find the next available appointment with Dr. Gupta.", ("query", {"entity": "availability", "doctor": "Dr. Gupta", "next": True}))
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.kind, "doctor")
        self.assertEqual([o["label"] for o in ask.options], ["Dr. Mehta", "Dr. Rao", "Dr. Iyer"])
        self.assertIn("Gupta", ask.question)
        answered = self.tap(1)[1]
        self.assertIsInstance(answered, ReadResult)
        self.assertIn("Next available with Dr. Rao: Fri 9 Oct, 10:30 (Branch B)", answered.answer_text)

    def test_the_booking_tail_survives_the_doctor_question(self):
        ask = self.say("Find the next available appointment with Dr. Gupta and book it for Rakesh Verma.",
                       ("query", {"entity": "availability", "doctor": "Dr. Gupta", "next": True}))
        self.assertEqual(ask.kind, "doctor")
        offer = self.tap(1)[1]
        self.assertIn("Book Rakesh Verma on Fri 9 Oct at 10:30 with Dr. Rao?", offer.answer_text)
        card = self.say("yes", self.YES)
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.slots["branch_id"], 2)
        self.assertTrue(self.unchanged())

    def test_the_doctor_who_matches_two_is_asked_which(self):
        self.rebuild(with_setup(branches=BRANCHES + SHARMAS))
        ask = self.say("next available appointment with Dr. Sharma", ("query", {"entity": "availability", "doctor": "Dr. Sharma", "next": True}))
        self.assertEqual((ask.kind, [o["label"] for o in ask.options]), ("doctor", ["Dr. Anil Sharma", "Dr. Sunil Sharma"]))
        answered = self.say("the second one", ("choose_option", {"index": 2}))
        self.assertIn("Next available with Dr. Sunil Sharma: Fri 9 Oct, 14:00 (Branch E)", answered.answer_text)
        self.assertIsNone(self.ctx.pending)

    def test_a_doctor_who_is_not_at_the_named_branch(self):
        result = self.say("next available with Dr. Iyer at Branch B", ("query", {"entity": "availability", "doctor": "Dr. Iyer", "branch": "B", "next": True}))
        self.assertEqual(result.answer_text.split(" Source:")[0], "Dr. Iyer does not work at Branch B.")

    def test_the_booking_goes_to_the_branch_the_slot_is_at(self):
        self.ctx.set_client_branch(1, 1)                                                  # this computer works for Branch A ...
        result = self.say("Find the next available appointment with Dr. Iyer and book it for Rakesh Verma.",
                          ("query", {"entity": "availability", "doctor": "Dr. Iyer", "next": True}))
        self.assertIn("Fri 9 Oct, 10:30 (Branch C)", result.answer_text)                  # ... but Dr. Iyer works at C
        card = self.say("yes", self.YES)
        self.assertEqual(card.slots["branch_id"], 3)

    def test_one_day_for_one_doctor(self):
        result = self.say("what is free tomorrow with Dr. Mehta", ("query", {"entity": "availability", "doctor": "Dr. Mehta", "date": SAT}))
        self.assertEqual(result.data["doctor"], "Dr. Mehta")
        self.assertEqual(result.data["date"], SAT)
        self.assertNotIn("10:00", result.data["slots"])                                    # Saturday 10:00 is booked
        self.assertIn("with Dr. Mehta", result.answer_text)
        self.assertIsNone(self.ctx.pending)

    def test_two_patients_with_that_name_are_asked_which_one(self):
        self.say("Find the next available appointment with Dr. Mehta and book it for Rahul.", self.FIND)
        ask = self.say("yes", self.YES)
        self.assertIsInstance(ask, AskResult)
        self.assertEqual((ask.kind, len(ask.options)), ("choose_patient", 2))
        self.assertTrue(self.unchanged())

    def test_a_phone_in_the_sentence_is_kept_for_the_card(self):
        self.say("Find the next available appointment with Dr. Mehta and book it for Neha Gupta, phone 9988776655.", self.FIND)
        card = self.say("yes", self.YES)
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.slots["patient_phone"], "9988776655")


class FlowClassic(Flow, unittest.TestCase):
    """Classic: the keyword rules and the deterministic readers, no planner at all."""
    config = "classic_rules_only"
    mode = "classic"


class FlowClassicWithAPlanner(Flow, unittest.TestCase):
    """Classic with a planner: its query is read, then the same rules run."""
    config = "classic_scripted"
    mode = "classic"


    def test_the_rules_read_it_without_asking_the_planner(self):
        self.say(SENTENCE, self.FIND)
        self.assertEqual(self.backend.calls, [])                                           # "next available": plain rules, no model call
        row = self.conn.execute("SELECT route_taken, route_detail FROM planner_log ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((row["route_taken"], row["route_detail"]), ("rules", "rule:next"))


class FlowModelFirst(Flow, unittest.TestCase):
    """Model first: every turn goes to the (scripted) model, which answers a yes with choose_option."""
    config = "model_first_scripted"
    mode = "model_first"

    def test_the_state_card_shows_the_offer_and_its_options(self):
        self.say(SENTENCE, self.FIND)
        self.say("yes", self.YES)
        offer_call = [c for c in self.backend.calls if "book_slot" in c[1]][-1][1]               # the card sent with the "yes"
        self.assertIn("Task in progress: book_appointment. Heard: patient=Neha Gupta, date=2026-10-09, time=10:30, branch=A.", offer_call)
        self.assertIn('Waiting for the user\'s answer to: "Book Neha Gupta on Fri 9 Oct at 10:30 with Dr. Mehta?" (book_slot).', offer_call)
        self.assertIn("  1. Yes, book it", offer_call)
        self.assertIn("  2. Another time", offer_call)
        self.assertIn("a person still presses Approve", offer_call)
        for hidden in ("9988776655", "_offer", "_question"):
            self.assertNotIn(hidden, offer_call)

    def test_the_prompt_tells_the_model_about_the_offer(self):
        self.say(SENTENCE, self.FIND)
        system = self.backend.calls[0][0]
        self.assertIn("next=true", system)
        self.assertIn("choose_option(1)", system)
        self.assertIn("nothing is booked until the user presses Approve", system)

    def test_a_yes_to_the_offer_is_not_refused_as_an_approval_even_with_a_card_open(self):
        self.say("book Rakesh Verma tomorrow at 5 pm", ("book_appointment", {"patient_name": "Rakesh Verma", "date": SAT, "time": "17:00"}))
        self.say(SENTENCE, self.FIND)
        self.assertIsInstance(self.say("yes", self.YES), AskResult)

    def test_the_model_answering_with_a_slot_instead_of_an_option_is_read_from_the_words(self):
        self.say(SENTENCE, self.FIND)
        ask = self.say("yes", ("answer_slot", {"slot": "patient_name", "value": "yes"}))
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.kind, "phone")

    def test_an_unreadable_reply_to_the_offer_asks_again_with_the_whole_question(self):
        self.say(SENTENCE, self.FIND)
        again = self.say("hmm", ("answer_slot", {"slot": "patient_name", "value": "hmm"}))
        self.assertIsInstance(again, AskResult)
        self.assertEqual(again.kind, "book_slot")
        self.assertIn("Book Neha Gupta on Fri 9 Oct at 10:30 with Dr. Mehta?", again.question)

    def test_a_new_patient_tool_is_not_an_answer_to_the_offer(self):
        self.say(SENTENCE, self.FIND)
        note = self.say("it is a new patient called Asha", ("new_patient", {"name": "Asha"}))
        self.assertIsInstance(note, Note)
        self.assertEqual(self.ctx.pending["kind"], "book_slot")

    def test_a_read_in_between_keeps_the_offer_open(self):
        self.say(SENTENCE, self.FIND)
        result = self.say("how many patients are registered", ("query", {"entity": "patients", "aggregate": "count"}))
        self.assertIn("Still waiting for your answer: Book Neha Gupta on Fri 9 Oct at 10:30 with Dr. Mehta?", result.answer_text)
        self.assertEqual(self.ctx.pending["kind"], "book_slot")
        self.assertIsInstance(self.say("yes", self.YES), AskResult)


class Hinglish(unittest.TestCase):
    """Hinglish and Devanagari: the sentence, the question and the phone question are in the speaker's language."""

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(engine.sandbox(configs.get("classic_rules_only")))
        self.conn, _ = engine.build_clinic(BASE_SETUP)
        stack.callback(self.conn.close)
        self.adapter = LocalSQLiteAdapter()
        self.ctx = VoiceContext(clock=engine.FakeMonotonic())
        self.ctx.set_client_branch(1, 1)

    def say(self, text, language):
        return handle_turn(self.ctx, self.conn, text, self.adapter, self.adapter, language, engine.DEFERRED_INTENTS)

    def test_hinglish(self):
        result = self.say("agla available appointment Dr. Mehta ke saath aur Neha Gupta ke liye book karo", "hi-IN")
        self.assertEqual(result.answer_text.split(" Source:")[0],
                         "Agla khaali slot Dr. Mehta ke saath: Fri 9 Oct, 10:30 (Branch A). Neha Gupta ko Fri 9 Oct 10:30 baje Dr. Mehta ke saath book karun?")
        self.assertEqual([o["label"] for o in result.options], ["Haan, book karo", "Doosra time"])
        ask = self.say("haan", "hi-IN")
        self.assertEqual((ask.kind, ask.question), ("phone", "Patient ka phone number kya hai?"))

    def test_devanagari(self):
        result = self.say("डॉक्टर मेहता के साथ अगला उपलब्ध अपॉइंटमेंट और नेहा गुप्ता के लिए बुक करो", "hi-IN")
        self.assertIn("नेहा गुप्ता को Fri 9 Oct 10:30 बजे Dr. Mehta के साथ बुक करूँ?", result.answer_text)
        ask = self.say("हाँ", "hi-IN")
        self.assertEqual(ask.kind, "phone")
        self.assertEqual(ask.question, "मरीज़ का फ़ोन नंबर क्या है?")

    def test_the_keyword_rules_know_the_next_free_slot_without_an_appointment_word(self):
        from clinic.nlu.classify import classify
        for text in ("next available with Dr. Mehta", "earliest slot with Dr. Rao", "first free", "next free slot", "agla khaali",
                     "अगला उपलब्ध डॉक्टर मेहता", "what is the next available appointment"):
            with self.subTest(text=text):
                self.assertEqual(classify(text), "check_availability")
        self.assertEqual(classify("Find the earliest appointment with Dr. Iyer and book it for Rakesh"), "check_availability")
        self.assertEqual(classify("cancel the earliest appointment"), "cancel_appointment")        # a cancel stays a cancel
        self.assertEqual(classify("when is Amit's next appointment"), "next_appointment")          # not claimed
        self.assertEqual(classify("book Amit tomorrow at 5 pm"), "book_appointment")


# -- the page ---------------------------------------------------------------------------------------------------------------------------

class TheEventAndThePage(unittest.TestCase):
    def test_the_read_event_carries_the_table_and_the_chips(self):
        os.environ.setdefault("SARVAM_API_KEY", "test-not-real")
        from clinic.realtime_voice import VoiceSession
        with contextlib.ExitStack() as stack:
            stack.enter_context(engine.sandbox(configs.get("classic_rules_only")))
            conn, _ = engine.build_clinic(BASE_SETUP)
            stack.callback(conn.close)
            emitted = []
            adapter = LocalSQLiteAdapter()
            session = VoiceSession("sid", "key", lambda e, d: emitted.append((e, d)), lambda: conn, adapter, adapter, engine.DEFERRED_INTENTS)
            session.context.set_client_branch(1, 1)
            session._language_hint = "en-IN"
            session._handle_final_transcript(SENTENCE)
            event = [d for e, d in emitted if e == "read_answer"][0]
            self.assertEqual(event["intent"], "check_availability")
            self.assertEqual(event["data"], [{"date": "Fri 9 Oct", "slots": ["10:30"], "branch": "Branch A", "doctor": "Dr. Mehta"}])
            self.assertEqual(event["options"], [{"label": "Yes, book it"}, {"label": "Another time"}])
            self.assertIn("Book Neha Gupta on Fri 9 Oct at 10:30 with Dr. Mehta?", event["answer_text"])
            session.pick_option(0)
            question = [d for e, d in emitted if e == "assistant_question"][-1]
            self.assertEqual(question["kind"], "phone")
            self.assertFalse([e for e, d in emitted if e == "review_card"])                   # nothing to approve yet

    def test_a_plain_read_has_no_options(self):
        os.environ.setdefault("SARVAM_API_KEY", "test-not-real")
        from clinic.realtime_voice import VoiceSession
        with contextlib.ExitStack() as stack:
            stack.enter_context(engine.sandbox(configs.get("classic_rules_only")))
            conn, _ = engine.build_clinic(BASE_SETUP)
            stack.callback(conn.close)
            emitted = []
            adapter = LocalSQLiteAdapter()
            session = VoiceSession("sid", "key", lambda e, d: emitted.append((e, d)), lambda: conn, adapter, adapter, engine.DEFERRED_INTENTS)
            session._handle_final_transcript("what slots are free tomorrow")
            event = [d for e, d in emitted if e == "read_answer"][0]
            self.assertEqual(event["options"], [])
            self.assertEqual(event["data"]["date"], SAT)
            self.assertIsInstance(event["data"]["slots"], list)

    def test_the_page_wraps_the_slots_as_chips(self):
        js = (ROOT / "static" / "live_voice.js").read_text()
        css = (ROOT / "static" / "style.css").read_text()
        fmt = (ROOT / "static" / "read_format.js").read_text()
        self.assertIn("slot-chip", js)
        self.assertIn("shown.chips", js)
        self.assertIn("textContent", js)
        self.assertIn("appendOptions(bubble, data.options)", js)                                   # the offer's buttons
        self.assertIn("td.cell-chips { white-space: normal;", css)                                 # wraps inside the card, no clipping
        self.assertIn(".slot-chip", css)
        self.assertNotIn("innerHTML", fmt)
        self.assertNotIn("innerHTML", js.split("function showReadTurn")[1].split("function showNavigateTurn")[0])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_format_helper_node_test(self):
        out = subprocess.run(["node", str(ROOT / "tests" / "read_format.test.js")], capture_output=True, text=True, cwd=str(ROOT), timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("passed", out.stdout)


if __name__ == "__main__":
    unittest.main()
