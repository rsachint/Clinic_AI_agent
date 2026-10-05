"""Listing one person's appointments by name ("अमित नाम के सारे अपॉइंटमेंट निकालो"):
the query, the pipeline answer and caption, the parser slot, and the page wiring.
The name model and the intent model are always patched: no live model call."""

import os
import sqlite3
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import queries
from clinic.adapters.local_sqlite import LocalSQLiteAdapter
from clinic.nlu.parser import parse
from clinic.pipeline import PipelineError, ReadResult, transcript_to_response
from clinic.voice_context import VoiceContext

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "clinic" / "schema.sql").read_text()
LIST_KEYS = ["id", "appt_date", "start_time", "duration_minutes", "status", "notes", "patient_name", "patient_phone", "branch_id", "doctor_id", "branch", "branch_code", "doctor"]   # the last five arrive with multi-branch


def _iso(offset):
    return (date.today() + timedelta(days=offset)).isoformat()


class NamedDataTestCase(unittest.TestCase):
    """Two Amits (one registered, one a walk-in), an Akash, and a spread of
    dates and statuses around today."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.addCleanup(self.conn.close)
        self.adapter = LocalSQLiteAdapter()
        self.conn.execute("INSERT INTO patients (name, phone, age) VALUES ('Amit Dua', '9811111111', 35)")
        self.dua = self.conn.execute("SELECT id FROM patients WHERE name='Amit Dua'").fetchone()["id"]
        self.dua_today = self.book(self.dua, None, 0, "14:00")                     # registered patient
        self.anand_tomorrow = self.book(None, "Amit Anand", 1, "11:00")           # walk-in, name on the row only
        self.akash_later = self.book(None, "Akash", 2, "14:00")
        self.conn.commit()

    def book(self, patient_id, name, offset, start, status="booked"):
        cur = self.conn.execute(
            "INSERT INTO appointments (patient_id, patient_name, patient_phone, appt_date, start_time, duration_minutes, status)"
            " VALUES (?,?,?,?,?,30,?)",
            (patient_id, name, "9822222222" if name else None, _iso(offset), start, status))
        return cur.lastrowid

    def ids(self, rows):
        return [r["id"] for r in rows]


class AppointmentsNamedQueryTests(NamedDataTestCase):
    def test_a_registered_patient_is_found_across_all_dates(self):
        rows = queries.appointments_named(self.conn, "Amit Dua")
        self.assertEqual(self.ids(rows), [self.dua_today])
        self.assertEqual(rows[0]["patient_name"], "Amit Dua")
        self.assertEqual(rows[0]["patient_phone"], "9811111111")

    def test_a_walk_in_is_found_by_the_name_on_the_appointment(self):
        rows = queries.appointments_named(self.conn, "Amit Anand")
        self.assertEqual(self.ids(rows), [self.anand_tomorrow])
        self.assertEqual(rows[0]["patient_phone"], "9822222222")

    def test_rows_have_exactly_the_keys_of_scheduled_appointments(self):
        named = queries.appointments_named(self.conn, "Amit Dua")[0]
        plain = dict(queries.scheduled_appointments(self.conn, _iso(0))[0])
        self.assertEqual(list(named.keys()), LIST_KEYS)
        self.assertEqual(named, plain)

    def test_devanagari_finds_a_roman_name(self):
        self.assertEqual(self.ids(queries.appointments_named(self.conn, "अमित दुआ")), [self.dua_today])
        self.assertEqual(self.ids(queries.appointments_named(self.conn, "अमित आनंद")), [self.anand_tomorrow])
        self.assertEqual(self.ids(queries.appointments_named(self.conn, "आकाश")), [self.akash_later])

    def test_roman_finds_a_devanagari_name(self):
        row = self.book(None, "सुनीता देवी", 3, "09:30")
        self.conn.commit()
        self.assertEqual(self.ids(queries.appointments_named(self.conn, "Sunita Devi")), [row])

    def test_a_first_name_alone_finds_every_person_with_it(self):
        both = [self.dua_today, self.anand_tomorrow]
        self.assertEqual(self.ids(queries.appointments_named(self.conn, "Amit")), both)
        self.assertEqual(self.ids(queries.appointments_named(self.conn, "अमित")), both)
        self.assertEqual(self.ids(queries.appointments_named(self.conn, "amit")), both)

    def test_a_surname_alone_finds_that_person(self):
        self.assertEqual(self.ids(queries.appointments_named(self.conn, "Dua")), [self.dua_today])

    def test_a_similar_but_different_first_name_is_left_out(self):
        self.book(None, "Amita Rao", 4, "10:00")
        self.conn.commit()
        self.assertEqual(self.ids(queries.appointments_named(self.conn, "Amit")), [self.dua_today, self.anand_tomorrow])

    def test_a_full_name_keeps_only_that_person(self):
        self.assertEqual(self.ids(queries.appointments_named(self.conn, "Amit Dua")), [self.dua_today])

    def test_cancelled_and_moved_appointments_are_left_out(self):
        self.book(None, "Amit Anand", 5, "10:00", status="cancelled")
        self.book(None, "Amit Anand", 6, "10:00", status="rescheduled")
        self.conn.commit()
        self.assertEqual(self.ids(queries.appointments_named(self.conn, "Amit Anand")), [self.anand_tomorrow])

    def test_past_completed_and_no_show_appointments_are_included_in_date_order(self):
        past_done = self.book(None, "Amit Anand", -30, "10:00", status="completed")
        past_missed = self.book(None, "Amit Anand", -10, "10:00", status="no_show")
        confirmed = self.book(None, "Amit Anand", 20, "10:00", status="confirmed")
        self.conn.commit()
        rows = queries.appointments_named(self.conn, "Amit Anand")
        self.assertEqual(self.ids(rows), [past_done, past_missed, self.anand_tomorrow, confirmed])
        self.assertEqual([r["status"] for r in rows], ["completed", "no_show", "booked", "confirmed"])

    def test_a_narrowed_search_never_turns_into_a_different_person(self):
        """Amit Anand has nothing on the day asked (or only a cancelled booking):
        that is an empty answer, not Amit Dua, who merely sounds somewhat alike."""
        self.assertEqual(queries.appointments_named(self.conn, "Amit Anand", _iso(0), _iso(0)), [])
        self.assertEqual(queries.appointments_named(self.conn, "अमित आनंद", _iso(0), _iso(0)), [])
        self.book(None, "Rohan Mehra", -2, "10:00", status="cancelled")
        self.conn.commit()
        self.assertEqual(queries.appointments_named(self.conn, "Rohan Mehra"), [])
        self.assertEqual(queries.appointments_named(self.conn, "Rohan Mehta"), [])

    def test_same_day_is_ordered_by_time(self):
        early = self.book(None, "Amit Anand", 1, "09:00")
        self.conn.commit()
        self.assertEqual(self.ids(queries.appointments_named(self.conn, "Amit Anand", _iso(1), _iso(1))),
                         [early, self.anand_tomorrow])

    def test_a_date_range_narrows_the_search(self):
        self.assertEqual(self.ids(queries.appointments_named(self.conn, "Amit", _iso(1), _iso(1))), [self.anand_tomorrow])
        self.assertEqual(self.ids(queries.appointments_named(self.conn, "Amit", _iso(0), _iso(0))), [self.dua_today])
        self.assertEqual(queries.appointments_named(self.conn, "Amit", _iso(2), _iso(9)), [])

    def test_an_unknown_or_empty_name_finds_nothing(self):
        for name in ("Zebra Quux", "", "   ", None):
            with self.subTest(name=name):
                self.assertEqual(queries.appointments_named(self.conn, name), [])

    def test_the_adapter_exposes_it(self):
        rows = self.adapter.appointments_named(self.conn, "Amit", _iso(1), _iso(1))
        self.assertEqual(self.ids(rows), [self.anand_tomorrow])

    def test_the_cancel_card_search_is_unchanged(self):
        """Upcoming, booked/confirmed only, best-fitting person only, with `who` and `score`."""
        past = self.book(None, "Amit Dua", -3, "10:00")
        self.book(None, "Amit Dua", 7, "10:00", status="cancelled")
        self.book(None, "Amit Dua", 8, "10:00", status="completed")
        self.conn.commit()
        rows = queries.upcoming_appointments_named(self.conn, "Amit Dua")
        self.assertEqual(self.ids(rows), [self.dua_today])
        self.assertNotIn(past, self.ids(rows))
        self.assertEqual(list(rows[0].keys()), ["id", "appt_date", "start_time", "duration_minutes", "who", "score", "branch_id", "branch", "branch_code"])
        self.assertEqual(rows[0]["who"], "Amit Dua")
        self.assertEqual(rows[0]["score"], 1.0)
        # A bare first name still keeps only the single best-sounding person there (no first-name rule).
        self.assertEqual(self.ids(queries.upcoming_appointments_named(self.conn, "Amit")), [self.dua_today])
        self.assertEqual(self.ids(self.adapter.upcoming_appointments_named(self.conn, "अमित दुआ")), [self.dua_today])


class ListByNamePipelineTests(NamedDataTestCase):
    def ask(self, text, name, intent="list_appointments", language="en-IN", context=None, now=None, **kwargs):
        with patch("clinic.nlu.parser.pick_intent", return_value=intent), \
                patch("clinic.nlu.parser.extract_name", return_value=name), \
                patch("clinic.pipeline._local_now", return_value=now or datetime.now()):
            return transcript_to_response(self.conn, text, self.adapter, self.adapter, language,
                                          context=context, **kwargs)

    def names(self, result):
        return [r["patient_name"] for r in result.data]

    def test_a_name_alone_lists_every_date_for_that_person(self):
        past = self.book(None, "Amit Anand", -4, "09:00", status="completed")
        self.conn.commit()
        result = self.ask("अमित नाम के सारे अपॉइंटमेंट निकालो।", "अमित")
        self.assertIsInstance(result, ReadResult)
        self.assertEqual(self.ids(result.data), [past, self.dua_today, self.anand_tomorrow])
        self.assertEqual(result.scope_caption, "Appointments for अमित · all dates")
        self.assertIn("3 appointment(s) for अमित.", result.answer_text)
        self.assertNotIn("Akash", self.names(result))

    def test_the_rows_look_like_a_day_list(self):
        result = self.ask("show Amit's appointments", "Amit")
        self.assertEqual(list(result.data[0].keys()), LIST_KEYS)

    def test_a_name_and_a_day_narrow_by_both(self):
        result = self.ask("Amit's appointments tomorrow", "Amit")
        self.assertEqual(self.ids(result.data), [self.anand_tomorrow])
        tomorrow = date.today() + timedelta(days=1)
        label = "{} {} {}".format(tomorrow.strftime("%a"), tomorrow.day, tomorrow.strftime("%b"))
        self.assertEqual(result.scope_caption, "Appointments for Amit · " + label)
        self.assertIn(label, result.answer_text)

    def test_a_name_and_a_devanagari_day(self):
        result = self.ask("अमित के कल के अपॉइंटमेंट", "अमित")
        self.assertEqual(self.ids(result.data), [self.anand_tomorrow])

    def test_a_name_and_today_is_just_today(self):
        result = self.ask("Amit's appointments today", "Amit")
        self.assertEqual(self.ids(result.data), [self.dua_today])
        self.assertTrue(result.scope_caption.startswith("Appointments for Amit · "))

    def test_a_name_and_a_week_narrow_by_both_and_hide_todays_passed_ones(self):
        later_today = self.book(None, "Amit Anand", 0, "20:00")
        far = self.book(None, "Amit Anand", 30, "10:00")
        self.conn.commit()
        now = datetime.combine(date.today(), datetime.strptime("15:00", "%H:%M").time())   # Dua's 14:00 has passed
        result = self.ask("Amit ke is hafte ke appointments", "Amit", now=now)
        self.assertEqual(self.ids(result.data), [later_today, self.anand_tomorrow])
        self.assertNotIn(far, self.ids(result.data))
        self.assertEqual(result.scope_caption, "Appointments for Amit · this week")
        earlier = datetime.combine(date.today(), datetime.strptime("08:00", "%H:%M").time())
        result = self.ask("Amit ke is hafte ke appointments", "Amit", now=earlier)
        self.assertEqual(self.ids(result.data), [self.dua_today, later_today, self.anand_tomorrow])

    def test_nothing_found_says_so_in_english_and_never_shows_today(self):
        result = self.ask("show Zebra's appointments", "Zebra")
        self.assertEqual(result.data, [])
        self.assertTrue(result.answer_text.startswith("No appointments found for Zebra."), result.answer_text)
        self.assertEqual(result.scope_caption, "Appointments for Zebra · all dates")

    def test_nothing_found_says_so_in_hindi(self):
        result = self.ask("ज़ेबरा के सारे अपॉइंटमेंट", "ज़ेबरा", language="hi-IN")
        self.assertEqual(result.data, [])
        self.assertTrue(result.answer_text.startswith("ज़ेबरा ke koi appointments nahi mile."), result.answer_text)

    def test_nothing_found_on_a_day_names_the_day(self):
        result = self.ask("Amit ke aaj ke appointments", "Amit Anand", language="hi-IN")
        self.assertEqual(result.data, [])
        self.assertIn("ko Amit Anand ke koi appointments nahi mile.", result.answer_text)
        result = self.ask("Amit Anand's appointments today", "Amit Anand")
        self.assertIn("No appointments found for Amit Anand on", result.answer_text)

    def test_a_name_that_is_not_there_does_not_fall_back_to_the_day_list(self):
        self.assertEqual(len(self.ask("show appointments today", None).data), 1)   # control: today has Amit Dua
        self.assertEqual(self.ask("show Zebra's appointments", "Zebra").data, [])

    def test_two_different_people_are_both_listed(self):
        result = self.ask("show Amit's appointments", "Amit")
        self.assertEqual(self.names(result), ["Amit Dua", "Amit Anand"])

    def test_no_name_behaves_as_before(self):
        spy = patch.object(LocalSQLiteAdapter, "appointments_named", side_effect=AssertionError("name search used"))
        with spy:
            today = self.ask("show appointments", None)
            self.assertEqual(self.ids(today.data), [self.dua_today])
            self.assertEqual(today.scope_caption, "Today · {} {} {}".format(
                date.today().strftime("%a"), date.today().day, date.today().strftime("%b")))
            self.assertIn("(today)", today.answer_text)
            tomorrow = self.ask("get all appointments for tomorrow", None)
            self.assertEqual(self.ids(tomorrow.data), [self.anand_tomorrow])
            self.assertIsNone(tomorrow.scope_caption)
            self.assertNotIn("for ", tomorrow.answer_text.split("Source")[0])
            morning = datetime.combine(date.today(), datetime.strptime("08:00", "%H:%M").time())
            week = self.ask("this week's appointments", None, now=morning)
            self.assertEqual(self.ids(week.data), [self.dua_today, self.anand_tomorrow, self.akash_later])
            self.assertIsNone(week.scope_caption)

    def test_a_blank_name_is_the_same_as_none(self):
        result = self.ask("show appointments", "  ")
        self.assertEqual(self.ids(result.data), [self.dua_today])

    def test_an_unreadable_date_still_asks_again(self):
        with self.assertRaises(PipelineError) as ctx:
            self.ask("Amit ke tareekh ke appointments batao", "Amit")
        self.assertIn("couldn't read the date", str(ctx.exception))
        with self.assertRaises(PipelineError):
            self.ask("tareekh ke appointments batao", None)

    def test_the_list_is_remembered_for_follow_ups(self):
        ctx = VoiceContext()
        result = self.ask("show Amit's appointments", "Amit", context=ctx)
        self.assertEqual(self.ids(ctx.list_rows), self.ids(result.data))
        self.assertIn("Amit", ctx.list_scope)
        # "cancel the second one" resolves against the rows shown, i.e. Amit Anand's booking.
        follow = self.ask("cancel the second one", None, intent="cancel_appointment", context=ctx,
                          defer_intents=frozenset(("cancel_appointment",)))
        self.assertEqual(follow.slots["appointment_id"], self.anand_tomorrow)
        self.assertEqual(follow.slots["patient_name"], "Amit Anand")

    def test_a_list_with_no_match_forgets_the_previous_list(self):
        ctx = VoiceContext()
        self.ask("show appointments", None, context=ctx)
        self.assertTrue(ctx.list_rows)
        self.ask("show Zebra's appointments", "Zebra", context=ctx)
        self.assertEqual(ctx.list_rows, [])

    def test_a_read_result_built_the_old_way_still_works(self):
        result = ReadResult("patient_lookup", {}, None, "text")
        self.assertIsNone(result.scope_caption)


class ListByNameParserTests(unittest.TestCase):
    def parse(self, text, name, intent="list_appointments"):
        with patch("clinic.nlu.parser.pick_intent", return_value=intent), \
                patch("clinic.nlu.parser.extract_name", return_value=name):
            return parse(text)

    def test_a_heard_name_becomes_a_slot(self):
        intent, slots = self.parse("अमित नाम के सारे अपॉइंटमेंट निकालो।", "अमित")
        self.assertEqual(intent, "list_appointments")
        self.assertEqual(slots, {"range": "today", "date": None, "patient_name": "अमित"})

    def test_a_name_with_a_day(self):
        _, slots = self.parse("Amit's appointments tomorrow", "Amit")
        self.assertEqual(slots["date"], _iso(1))
        self.assertEqual(slots["patient_name"], "Amit")

    def test_a_name_with_a_week(self):
        _, slots = self.parse("Amit ke is hafte ke appointments", "Amit")
        self.assertEqual(slots, {"range": "week", "patient_name": "Amit"})

    def test_no_name_leaves_the_slots_exactly_as_before(self):
        _, today = self.parse("show appointments", None)
        self.assertEqual(today, {"range": "today", "date": None})
        _, week = self.parse("list all appointments for this week", None)
        self.assertEqual(week, {"range": "week"})
        _, tomorrow = self.parse("get all appointments for tomorrow", None)
        self.assertEqual(tomorrow, {"range": "today", "date": _iso(1)})

    def test_an_unreadable_date_is_still_flagged(self):
        _, slots = self.parse("Amit ke tareekh ke appointments batao", "Amit")
        self.assertTrue(slots["date_unreadable"])
        self.assertEqual(slots["patient_name"], "Amit")

    def test_an_unreachable_name_model_does_not_break_the_list(self):
        with patch("clinic.nlu.parser.pick_intent", return_value="list_appointments"), \
                patch("clinic.nlu.parser.extract_name", side_effect=ConnectionError("ollama down")):
            with self.assertLogs("clinic.nlu.parser", level="WARNING"):
                _, slots = parse("show appointments")
        self.assertEqual(slots, {"range": "today", "date": None})

    def test_the_name_is_looked_up_in_the_background_for_a_list_command(self):
        with patch("clinic.nlu.parser.llm_enabled", return_value=True), \
                patch("clinic.nlu.parser.prefetch_name") as prefetch, \
                patch("clinic.nlu.parser.pick_intent", return_value="list_appointments"), \
                patch("clinic.nlu.parser.extract_name", return_value=None):
            parse("get all the appointments for tomorrow")
        prefetch.assert_called_once()

    def test_a_command_that_never_needs_a_name_is_still_not_prefetched(self):
        with patch("clinic.nlu.parser.llm_enabled", return_value=True), \
                patch("clinic.nlu.parser.prefetch_name") as prefetch, \
                patch("clinic.nlu.parser.pick_intent", return_value="check_availability"), \
                patch("clinic.nlu.parser.extract_name", return_value=None):
            parse("free slots tomorrow")
        prefetch.assert_not_called()


class ListCaptionWiringTests(NamedDataTestCase):
    def test_the_read_answer_event_carries_the_caption(self):
        os.environ.setdefault("SARVAM_API_KEY", "test-not-real")
        from clinic.realtime_voice import VoiceSession
        emitted = []
        session = VoiceSession("sid", "key", lambda e, d: emitted.append((e, d)),
                               lambda: self.conn, self.adapter, self.adapter, frozenset())
        with patch("clinic.nlu.parser.pick_intent", return_value="list_appointments"), \
                patch("clinic.nlu.parser.extract_name", return_value="Amit"):
            session._handle_final_transcript("show Amit's appointments")
        events = [d for e, d in emitted if e == "read_answer"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["scope_caption"], "Appointments for Amit · all dates")
        self.assertEqual(len(events[0]["data"]), 2)
        with patch("clinic.nlu.parser.pick_intent", return_value="list_appointments"), \
                patch("clinic.nlu.parser.extract_name", return_value=None):
            session._handle_final_transcript("get all appointments for tomorrow")
        events = [d for e, d in emitted if e == "read_answer"]
        self.assertIsNone(events[1]["scope_caption"])

    def test_the_page_renders_the_caption_above_the_table_and_keeps_hiding_the_sentence(self):
        js = (ROOT / "static" / "live_voice.js").read_text()
        css = (ROOT / "static" / "style.css").read_text()
        body = js[js.index("function showReadTurn"):js.index("// \"Open the calendar\"")]
        self.assertIn("data.scope_caption", body)
        self.assertIn('class: "list-caption"', body)
        self.assertLess(body.index("list-caption"), body.index('el("table")'))
        self.assertIn("list_appointments: true", js)                  # the sentence is still hidden with a table
        self.assertIn(".list-caption", css)


if __name__ == "__main__":
    unittest.main()
