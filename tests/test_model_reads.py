"""The "Model does all read operations" mode (clinic/architecture.py "model_reads", clinic/nlu/sql_reads.py): the mode
plumbing and the proof that Classic and New (model first) never touch it, the `sql_read` tool and its validation, the
single- and multi-step loop on a fake planner backend (caps, repair, fallback, lookup vs answer), the message-encoding
fallback of the hosted backend against a MOCKED http layer, the answer composed from the rows' shape in English and
Hinglish, the memory of appointment rows, the planner log (notes, cost across steps, no result rows), that no SQL is
ever shown, the prompt budget, and the replay configuration. Fixed dates (today is Wednesday 2026-10-07); nothing here
reaches a model, Sarvam or the network."""
import json
import os
import subprocess
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from unittest.mock import patch

import httpx

os.environ["WHATSAPP_NOTIFY_MODE"] = "dry_run"
os.environ.setdefault("SARVAM_API_KEY", "test-not-real")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.planner_support import PlannerCase  # noqa: E402
from tests.test_conversation_branches import add_branches  # noqa: E402

from clinic import architecture, db, pipeline, read_schema  # noqa: E402
from clinic.nlu import dialogue, planner, sarvam, tools  # noqa: E402
from clinic.pipeline import ParsedResult, PipelineError, ReadResult  # noqa: E402
from clinic.voice_context import AskResult, Note  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXED = date(2026, 10, 7)
FIXED_NOW = datetime(2026, 10, 7, 11, 0)
DAY = "2026-10-08"


class FixedDate(date):
    @classmethod
    def today(cls):
        return FIXED


def sql_call(sql, caption="Appointments", purpose="answer", **more):
    return ("sql_read", dict({"sql": sql, "purpose": purpose, "caption": caption}, **more))


COUNT = "SELECT COUNT(*) AS n FROM v_appointments WHERE appt_date = '{}' AND status IN ('booked', 'confirmed')".format(DAY)
LIST = ("SELECT id, patient_name, appt_date, start_time FROM v_appointments WHERE appt_date = '{}' "
        "AND status IN ('booked', 'confirmed') ORDER BY start_time".format(DAY))
PHONES = "SELECT patient_name, patient_phone FROM v_appointments WHERE appt_date = '{}' ORDER BY start_time".format(DAY)


class UsageFake(planner.FakeBackend):
    """A fake backend that reports token usage per call, as the hosted one does."""

    def __init__(self, script, usages):
        super().__init__(script)
        self.usages = list(usages)
        self.last_usage = None

    def plan(self, system, user, tool_schemas):
        self.last_usage = self.usages.pop(0) if self.usages else None
        return super().plan(system, user, tool_schemas)


class ModelReadsCase(PlannerCase):
    """PlannerCase in "model_reads" mode with the clock frozen and fixed-date appointments on Thursday 2026-10-08."""

    def setUp(self):
        super().setUp()
        for target, value in (("clinic.nlu.planner.date", FixedDate), ("clinic.pipeline._local_now", lambda: FIXED_NOW)):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.conn.execute("DELETE FROM appointments")
        for patient, start, branch in ((1, "10:00", 1), (2, "11:00", 1), (4, "16:00", 2)):          # Rakesh, Mohan Lal, Sunita Devi
            self.conn.execute("INSERT INTO appointments (patient_id, appt_date, start_time, duration_minutes, status, branch_id) "
                              "VALUES (?, ?, ?, 30, 'booked', ?)", (patient, DAY, start, branch))
        self.conn.execute("INSERT INTO appointments (patient_id, appt_date, start_time, duration_minutes, status, branch_id) "
                          "VALUES (3, ?, '12:00', 30, 'cancelled', 1)", (DAY,))
        self.conn.commit()
        architecture.set_mode(self.conn, "model_reads")

    def reads(self, *calls):
        self.backend.script = list(calls)

    def last_system(self):
        return self.backend.calls[-1][0]


# -- the tool -----------------------------------------------------------------------------------------------------------

def declared_text(sql_reads):
    return sql_reads.SQL_READ_SCHEMA["function"]["description"]


class ToolDeclaration(unittest.TestCase):
    def setUp(self):
        from clinic.nlu import sql_reads
        self.sql_reads = sql_reads

    def test_model_reads_offers_the_usual_tools_plus_sql_read_and_a_rewritten_query_description(self):
        offered = self.sql_reads.schemas()
        usual = tools.schemas(dialogue=True)
        self.assertEqual([t["function"]["name"] for t in offered], [t["function"]["name"] for t in usual] + ["sql_read"])
        self.assertEqual(len(offered), 25)
        changed = [(a["function"]["name"]) for a, b in zip(offered, usual) if a != b]
        self.assertEqual(changed, ["query"])                                   # the only usual tool that differs
        query = [t for t in offered if t["function"]["name"] == "query"][0]["function"]
        self.assertIn("FREE SLOTS only", query["description"])
        self.assertIn("availability", query["description"])
        self.assertEqual(query["parameters"], [t for t in usual if t["function"]["name"] == "query"][0]["function"]["parameters"])

    def test_the_registry_the_other_modes_use_is_untouched(self):
        self.sql_reads.schemas()
        names = [t["function"]["name"] for t in tools.schemas()]
        self.assertEqual(len(names), 19)
        self.assertNotIn("sql_read", names)
        self.assertNotIn("sql_read", [t["function"]["name"] for t in tools.schemas(dialogue=True)])
        self.assertIn("READ-ONLY lookup: show, list, count, sum or find records",
                      [t for t in tools.schemas() if t["function"]["name"] == "query"][0]["function"]["description"])
        with self.assertRaises(tools.ToolError):
            tools.validate("sql_read", {"sql": "SELECT 1"})                     # unknown to the classic and model-first validators
        with self.assertRaises(tools.ToolError):
            tools.validate("sql_read", {"sql": "SELECT 1"}, dialogue=True)

    def test_the_declaration_has_the_agreed_arguments(self):
        declared = self.sql_reads.SQL_READ_SCHEMA["function"]
        self.assertEqual(declared["name"], "sql_read")
        self.assertEqual(set(declared["parameters"]["properties"]), {"sql", "purpose", "caption", "show_total"})
        self.assertEqual(declared["parameters"]["required"], ["sql", "purpose"])
        self.assertEqual(declared["parameters"]["properties"]["purpose"]["enum"], ["answer", "lookup"])
        self.assertEqual(declared["parameters"]["properties"]["show_total"]["type"], "boolean")
        self.assertNotIn("id", declared["parameters"]["properties"])

    def test_the_declaration_no_longer_says_one_select_and_the_caption_has_no_digits(self):
        text = json.dumps(self.sql_reads.SQL_READ_SCHEMA)
        self.assertNotIn("ONE SELECT", text)
        self.assertNotIn("One SELECT", text)
        self.assertIn("a SELECT (or WITH ... SELECT)", declared_text(self.sql_reads))
        self.assertIn("A SELECT (or WITH ... SELECT) over the v_ views, one statement", text)
        self.assertIn("NO digits", self.sql_reads.SQL_READ_SCHEMA["function"]["parameters"]["properties"]["caption"]["description"])


class ValidateArgs(unittest.TestCase):
    def setUp(self):
        from clinic.nlu import sql_reads
        self.v = sql_reads.validate_args

    def bad(self, args, code=None):
        with self.assertRaises(tools.ToolError) as caught:
            self.v(args)
        if code:
            self.assertEqual(caught.exception.code, code)

    def test_a_good_call(self):
        self.assertEqual(self.v({"sql": "SELECT 1", "purpose": "answer", "caption": "Patients", "show_total": True}),
                         {"sql": "SELECT 1", "purpose": "answer", "caption": "Patients", "show_total": True})

    def test_purpose_defaults_to_answer_and_is_forgiving_about_case(self):
        self.assertEqual(self.v({"sql": "SELECT 1"})["purpose"], "answer")
        self.assertEqual(self.v({"sql": "SELECT 1", "purpose": " Lookup "})["purpose"], "lookup")
        self.bad({"sql": "SELECT 1", "purpose": "execute"}, "bad_value")

    def test_unknown_arguments_reject_the_whole_call_and_there_is_never_an_id(self):
        for extra in ({"patient_id": 3}, {"id": 7}, {"limit": 5}, {"table": "patients"}, {"appointment_id": 1}):
            with self.subTest(extra=extra):
                self.bad(dict({"sql": "SELECT 1", "purpose": "answer"}, **extra), "unknown_arg")
        self.v({"sql": "SELECT 1", "purpose": "answer", "patient_id": None, "limit": ""})        # empty extras are ignored

    def test_sql_is_required_and_must_be_text(self):
        self.bad({"purpose": "answer"}, "missing_required")
        self.bad({"sql": "", "purpose": "answer"}, "missing_required")
        self.bad({"sql": 5, "purpose": "answer"}, "bad_value")
        self.bad("not json", "bad_value")
        self.bad([1, 2], "bad_value")
        self.assertEqual(self.v(json.dumps({"sql": "SELECT 1"}))["sql"], "SELECT 1")

    def test_a_caption_has_no_figures_but_may_name_a_date(self):
        for fine in ("Appointments cancelled last week", "Who is at Branch B on 16 Oct", "Booked on 2026-10-08", "Appointments for 9th October 2026",
                     "Pichle hafte cancel hue"):
            with self.subTest(caption=fine):
                self.assertEqual(self.v({"sql": "SELECT 1", "caption": fine})["caption"], fine)
        for figure in ("7 appointments", "Total is 5", "Fees Rs 500", "Top 3 patients"):
            with self.subTest(caption=figure):
                self.bad({"sql": "SELECT 1", "caption": figure}, "bad_value")

    def test_a_rejected_caption_names_the_digits_to_remove(self):
        for caption, named in (("Top 10 patients", "'10'"), ("7 appointments", "'7'"), ("Fees Rs 500", "'500'"),
                               ("10 days from 9 Oct and 3 doctors", "'10', '3'")):
            with self.subTest(caption=caption):
                with self.assertRaises(tools.ToolError) as caught:
                    self.v({"sql": "SELECT 1", "caption": caption})
                self.assertEqual(str(caught.exception), "sql_read.caption: remove the digits {} from the caption "
                                 "(write words, or leave the figure out)".format(named))

    def test_digits_the_person_said_may_be_in_the_caption_and_no_others(self):
        from clinic.nlu import sql_reads
        v = sql_reads.validate_args
        for command, caption in (("Aaj 6 PM ke appointments dikhao", "Appointments at 6 PM"),
                                 ("next 10 days mein kaun aa raha hai", "Patients in the next 10 days"),
                                 ("yeh number 9866542210 kis patient ka hai", "Owner of 9866542210"),
                                 ("yeh number 98665 42210 kis patient ka hai", "Owner of 9866542210"),
                                 ("yeh number 98665-42210 kis patient ka hai", "Owner of 98665 42210"),
                                 ("September 2026 ki fees", "Fees in September 2026")):
            with self.subTest(command=command):
                self.assertEqual(v({"sql": "SELECT 1", "caption": caption}, command)["caption"], caption)
        for command, caption in (("Aaj ke appointments dikhao", "Appointments at 6 PM"),       # not said by the person
                                 ("next 10 days mein kaun aa raha hai", "Patients in the next 7 days"),
                                 ("next 10 days mein kaun aa raha hai", "Patients, 10 of them, in the next 4 days"),
                                 ("yeh number 9866542210 kis patient ka hai", "Owner of 9866542211"),
                                 ("6 baje ke appointments", "Appointments at 16"),            # a digit run is whole: 16 is not 6
                                 ("", "Top 3 patients"), (None, "Top 3 patients")):
            with self.subTest(command=command, caption=caption):
                with self.assertRaises(tools.ToolError) as caught:
                    v({"sql": "SELECT 1", "caption": caption}, command)
                self.assertEqual(caught.exception.code, "bad_value")

    def test_a_spoken_digit_does_not_make_a_caption_sql_or_unbounded(self):
        from clinic.nlu import sql_reads
        for sqlish in ("SELECT 6 FROM v_patients", "6 = 6", "count(*) of 6"):
            with self.assertRaises(tools.ToolError):
                sql_reads.validate_args({"sql": "SELECT 1", "caption": sqlish}, "6 baje")
        with self.assertRaises(tools.ToolError):
            sql_reads.validate_args({"sql": "SELECT 1", "caption": "x" * 81}, "6 baje")

    def test_a_caption_is_a_title_not_sql_and_short(self):
        for sqlish in ("SELECT * FROM v_patients", "name = 'x'", "count(*) of patients", "v_appointments", "a; b"):
            with self.subTest(caption=sqlish):
                self.bad({"sql": "SELECT 1", "caption": sqlish}, "bad_value")
        self.bad({"sql": "SELECT 1", "caption": "x" * 81}, "bad_value")
        self.v({"sql": "SELECT 1", "caption": "x" * 80})

    def test_show_total_is_a_flag(self):
        self.assertIs(self.v({"sql": "SELECT 1", "show_total": "yes"})["show_total"], True)
        self.assertIs(self.v({"sql": "SELECT 1", "show_total": "false"})["show_total"], False)
        self.bad({"sql": "SELECT 1", "show_total": "maybe"}, "bad_value")
        self.assertNotIn("show_total", self.v({"sql": "SELECT 1"}))


# -- the loop -------------------------------------------------------------------------------------------------------------

class SingleStep(ModelReadsCase):
    def test_one_answer_query_is_one_planner_call_and_one_answer(self):
        self.reads(sql_call(COUNT, "Appointments on Thursday"))
        result = self.say("how many appointments are there on thursday")
        self.assertIsInstance(result, ReadResult)
        self.assertEqual(result.intent, "sql_read")
        self.assertEqual(result.answer_text, "Appointments on Thursday: 3. Source: local clinic records, updated just now.")
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.backend.histories, [])

    def test_the_planner_is_offered_sql_read_and_the_schema(self):
        self.reads(sql_call(COUNT))
        self.say("how many appointments are there on thursday")
        system, user, offered = self.backend.calls[0]
        self.assertEqual([t["function"]["name"] for t in offered][-1], "sql_read")
        self.assertEqual(len(offered), 25)
        self.assertIn("READING MODE", system)
        for view in read_schema.VIEW_NAMES:
            self.assertIn(view, system)
        self.assertIn("This week 2026-10-05 to 2026-10-11; last week 2026-09-28 to 2026-10-04.", system)
        self.assertNotIn("v_appointments", user)                  # nothing about the schema in the state card
        self.assertTrue(system.startswith(planner._RULES))
        self.assertIn(dialogue.MODEL_FIRST_RULES, system)

    def test_the_answer_never_shows_the_sql(self):
        self.reads(sql_call(LIST, "Booked on Thursday"))
        result = self.say("who is booked on thursday")
        shown = json.dumps([result.answer_text, result.data, result.scope_caption], ensure_ascii=False)
        for word in ("SELECT", "select", "FROM", "v_appointments", "WHERE", "status IN"):
            self.assertNotIn(word, shown)

    def test_a_write_is_impossible_even_if_the_model_asks(self):
        before = self.table_counts("patients", "appointments", "audit_log", "proposals")
        for sql in ("INSERT INTO patients (name, phone) VALUES ('x', 'y')", "DELETE FROM appointments", "UPDATE patients SET age = 1",
                    "SELECT 1; DROP TABLE patients"):
            self.reads(sql_call(sql), sql_call(sql))
            result = self.say("do something with the data")
            self.assertIsInstance(result, Note)
            self.assertIn("can't answer that yet", result.message)
        self.assertEqual(self.table_counts("patients", "appointments", "audit_log", "proposals"), before)

    def test_every_other_tool_still_works_in_this_mode(self):
        self.call("book_appointment", patient_name="Rakesh Verma", date="2026-10-09", time="17:00")
        self.assertIsInstance(self.say("book Rakesh Verma tomorrow at 5 pm"), ParsedResult)
        self.call("clarify", question="Which day?")
        ask = self.say("book someone")
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.kind, "clarify")

    def test_the_old_query_tool_still_answers_free_slots_and_any_other_entity_runs_the_old_path(self):
        self.call("query", entity="availability", date=DAY)
        slots = self.say("what slots are free on thursday")
        self.assertIsInstance(slots, ReadResult)
        self.assertEqual(slots.intent, "check_availability")
        self.call("query", entity="patients", aggregate="count")
        count = self.say("how many patients are registered")
        self.assertEqual(count.intent, "patient_count")                     # the old read path, as today
        rows = self.log_rows()
        self.assertEqual([r["route_detail"] for r in rows], ["mf:query", "mf:query"])

    def test_a_plain_words_question_is_still_shown_as_a_question(self):
        self.backend.script = ["Which day do you mean?"]
        ask = self.say("how many were there")
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.kind, "clarify")


class MultiStep(ModelReadsCase):
    def test_a_lookup_runs_and_the_next_call_sees_a_compact_result(self):
        self.reads(sql_call("SELECT patient_name FROM v_appointments WHERE appt_date = '{}' AND branch_code = 'B'".format(DAY),
                            "Who is at B", purpose="lookup"),
                   sql_call("SELECT patient_name, start_time FROM v_appointments WHERE name_match(patient_name, 'Sunita') ORDER BY start_time",
                            "Sunita's appointment"))
        result = self.say("when is the patient at branch B booked")
        self.assertEqual(len(self.backend.calls), 2)
        self.assertEqual(len(self.backend.histories), 1)
        call, text = self.backend.histories[0][0]
        self.assertEqual(call.name, "sql_read")
        self.assertIn("Sunita Devi", text)
        self.assertTrue(text.startswith("1 row(s):"))
        self.assertEqual(result.intent, "sql_read")
        self.assertEqual(self.log_rows()[-1]["route_detail"], "sql:2")

    def test_the_rows_given_back_to_the_model_have_no_ids_and_only_the_last_four_phone_digits(self):
        self.conn.execute("UPDATE patients SET phone = '9876543210' WHERE id = 1")
        self.conn.commit()
        self.reads(sql_call("SELECT id, patient_name, patient_phone, appt_date FROM v_appointments WHERE appt_date = '{}' ORDER BY start_time".format(DAY),
                            "Look", purpose="lookup"), sql_call(COUNT, "Count"))
        self.say("how many are booked for the first one's day")
        text = self.backend.histories[0][0][1]
        self.assertNotIn("9876543210", text)
        self.assertIn("...3210", text)
        header = text.splitlines()[1]
        self.assertEqual(header, "patient_name | patient_phone | appt_date")          # the id column is not shown
        self.assertNotRegex(text, r"\b\d{7,}\b")

    def test_a_long_result_is_cut_to_thirty_rows_for_the_model(self):
        from clinic.nlu import sql_reads
        self.conn.executemany("INSERT INTO appointments (patient_id, appt_date, start_time, duration_minutes, status, branch_id) "
                              "VALUES (1, '2026-11-01', ?, 30, 'booked', 1)", [("{:02d}:{:02d}".format(9 + n // 60, n % 60),) for n in range(120)])
        self.conn.commit()
        self.reads(sql_call("SELECT patient_name, start_time FROM v_appointments WHERE appt_date = '2026-11-01' ORDER BY start_time", "Many",
                            purpose="lookup"), sql_call(COUNT, "Count"))
        self.say("how many on the first of november then")
        text = self.backend.histories[0][0][1]
        self.assertTrue(text.startswith("120 row(s):"))
        self.assertEqual(len(text.splitlines()), 1 + 1 + sql_reads.MAX_FEEDBACK_ROWS + 1)       # count line, header, 30 rows, "showing"
        self.assertIn("(showing the first 30)", text)

    def test_three_queries_is_the_most_and_the_last_result_answers(self):
        from clinic.nlu import sql_reads
        with patch.object(sql_reads.sql_read, "run_query", wraps=sql_reads.sql_read.run_query) as spy:
            self.reads(*[sql_call(COUNT, "Step {}".format(word), purpose="lookup") for word in ("one", "two", "three", "four")])
            result = self.say("keep looking")
        self.assertEqual(spy.call_count, 3)
        self.assertEqual(len(self.backend.calls), 3)
        self.assertEqual(result.intent, "sql_read")
        self.assertTrue(result.answer_text.startswith("Step three: 3."))
        self.assertEqual(self.log_rows()[-1]["route_detail"], "sql:3")
        self.assertIn("step limit", self.log_rows()[-1]["override_notes"])

    def test_the_model_can_end_a_lookup_with_a_question_or_another_tool(self):
        self.reads(sql_call(COUNT, "Count", purpose="lookup"), ("clarify", {"question": "Which branch do you mean?"}))
        ask = self.say("how many at the other branch")
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.kind, "clarify")
        self.assertEqual(len(self.backend.calls), 2)
        self.reads(sql_call(COUNT, "Count", purpose="lookup"), ("book_appointment", {"patient_name": "Rakesh Verma", "date": "2026-10-09", "time": "17:00"}))
        self.assertIsInstance(self.say("book Rakesh Verma tomorrow at 5 pm if anyone is free"), ParsedResult)

    def test_plain_words_after_a_lookup_are_not_shown_as_an_answer(self):
        self.reads(sql_call(COUNT, "Count", purpose="lookup"), "There are three of them.")
        result = self.say("how many appointments on thursday, tell me in words")
        self.assertNotEqual(getattr(result, "intent", None), "sql_read")      # words are never an answer: the classic rules took the turn
        self.assertEqual(self.log_rows()[-1]["route_detail"], "mf_fallback_classic")

    def test_the_total_time_budget_ends_the_loop_with_the_last_result_or_a_fallback(self):
        from clinic.nlu import sql_reads
        with patch.object(sql_reads, "TOTAL_BUDGET_S", 0.5):              # less than the minimum for another call
            self.reads(sql_call(COUNT, "Counted", purpose="lookup"), sql_call(COUNT, "Never asked"))
            result = self.say("count them")
            self.assertEqual(len(self.backend.calls), 1)
            self.assertTrue(result.answer_text.startswith("Counted: 3."))
            self.assertIn("out of time", self.log_rows()[-1]["override_notes"])
        with patch.object(sql_reads, "TOTAL_BUDGET_S", 0.5):
            self.reads(sql_call("SELECT bogus FROM v_appointments", "Broken", purpose="lookup"), sql_call(COUNT, "Never asked"))
            before = len(self.backend.calls)
            with self.assertRaises(PipelineError):                           # nothing ran: the classic rules take it, and cannot read it
                self.say("count them again")
            self.assertEqual(len(self.backend.calls) - before, 1)

    def test_the_model_never_gets_more_than_six_calls(self):
        from clinic.nlu import sql_reads
        self.reads(*[sql_call("SELECT bogus FROM v_appointments", "Bad") for _ in range(10)])
        self.say("this will not work")
        self.assertLessEqual(len(self.backend.calls), sql_reads.MAX_MODEL_CALLS)
        self.assertEqual(len(self.backend.calls), 2)                         # a second failure of the same query stops it


class RepairAndFallback(ModelReadsCase):
    def test_a_refused_query_goes_back_to_the_model_once_with_the_plain_reason_and_its_second_try_answers(self):
        self.reads(sql_call("SELECT COUNT(*) FROM v_appointments WHERE cancelled = 1", "Cancelled"),
                   sql_call("SELECT COUNT(*) AS n FROM v_appointments WHERE status = 'cancelled'", "Cancelled"))
        result = self.say("how many were cancelled")
        self.assertEqual(result.answer_text.split(" Source")[0], "Cancelled: 1.")
        text = self.backend.histories[0][0][1]
        self.assertEqual(text, "Error: no such column: cancelled. Columns: v_appointments ({}). Fix only what the error says, keep "
                               "every part of the question, and call sql_read again.".format(", ".join(read_schema.COLUMNS["v_appointments"])))
        self.assertEqual(len(self.backend.calls), 2)
        self.assertIn("repaired", self.log_rows()[-1]["override_notes"])
        self.assertEqual(self.log_rows()[-1]["route_detail"], "sql:1")

    def test_a_second_failure_is_saved_for_a_person_and_nothing_else_is_asked(self):
        self.reads(sql_call("SELECT notes FROM appointments", "Notes"), sql_call("SELECT notes FROM v_appointments", "Notes"), sql_call(COUNT))
        result = self.say("show me the notes on the appointments")
        self.assertIsInstance(result, Note)
        self.assertIn("can't answer that yet", result.message)
        self.assertEqual(len(self.backend.calls), 2)
        saved = self.conn.execute("SELECT example_transcript, rejected_spec_json FROM unanswered_questions").fetchall()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0][0], "show me the notes on the appointments")
        self.assertIn("v_appointments", saved[0][1])                         # the model's SQL, as a hint for the developer
        self.assertEqual(self.log_rows()[-1]["route_detail"], "sql:0")

    def test_the_repair_message_for_a_base_table_names_the_views_and_leaks_nothing(self):
        self.reads(sql_call("SELECT notes FROM appointments", "Notes"), ("unsupported", {"reason": "not available", "wanted": "appointment notes"}))
        self.say("show me the notes")
        text = self.backend.histories[0][0][1]
        self.assertIn("table not allowed: appointments (read only the v_ views)", text)
        for leak in (str(ROOT), ".db", "sqlite3", "Traceback"):
            self.assertNotIn(leak, text)

    def test_each_query_of_a_multi_step_lookup_gets_its_own_repair(self):
        self.reads(sql_call("SELECT bogus FROM v_appointments", "First", purpose="lookup"), sql_call(COUNT, "First", purpose="lookup"),
                   sql_call("SELECT bogus2 FROM v_appointments", "Second"), sql_call(COUNT, "Second"))
        result = self.say("a two step question")
        self.assertTrue(result.answer_text.startswith("Second: 3."))
        self.assertEqual(len(self.backend.calls), 4)

    def test_a_malformed_call_is_repaired_the_same_way(self):
        self.reads(("sql_read", {"sql": COUNT, "purpose": "answer", "caption": "7 appointments"}), sql_call(COUNT, "Appointments"))
        result = self.say("how many")
        self.assertTrue(result.answer_text.startswith("Appointments: 3."))
        self.assertEqual(self.backend.histories[0][0][1], "Error: sql_read.caption: remove the digits '7' from the caption (write words, "
                         "or leave the figure out). Fix only what the error says, keep every part of the question, and call sql_read again.")
        self.reads(("sql_read", {"sql": COUNT, "purpose": "answer", "patient_id": 4}), ("sql_read", {"sql": COUNT, "purpose": "answer", "patient_id": 4}))
        self.assertIsInstance(self.say("how many for patient four"), Note)

    def test_a_caption_with_a_digit_the_person_said_is_accepted_without_a_repair(self):
        self.reads(sql_call(COUNT, "Appointments at 6 PM"))
        result = self.say("how many appointments at 6 PM on thursday")
        self.assertTrue(result.answer_text.startswith("Appointments at 6 PM: 3."), result.answer_text)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.backend.histories, [])

    def test_a_caption_with_a_digit_the_person_did_not_say_is_repaired(self):
        self.reads(sql_call(COUNT, "Appointments at 6 PM"), sql_call(COUNT, "Appointments"))
        result = self.say("how many appointments on thursday")
        self.assertTrue(result.answer_text.startswith("Appointments: 3."), result.answer_text)
        self.assertIn("remove the digits '6' from the caption", self.backend.histories[0][0][1])

    def test_a_person_test_misuse_is_repaired_with_the_right_form_and_the_second_try_answers(self):
        wrong = "SELECT COUNT(*) AS n FROM v_appointments WHERE patient_name = name_match(patient_name, 'Rakesh')"
        right = "SELECT COUNT(*) AS n FROM v_appointments WHERE name_match(patient_name, 'Rakesh') = 1"
        self.reads(sql_call(wrong, "Rakesh"), sql_call(right, "Rakesh"))
        result = self.say("how many appointments does Rakesh have")
        self.assertEqual(result.answer_text.split(" Source")[0], "Rakesh: 1.")
        self.assertEqual(self.backend.histories[0][0][1].split(". Fix only")[0],
                         "Error: Use name_match(<column>, 'spoken name') = 1 to find a person; it returns 1 or 0, so never compare "
                         "a column to it")

    def test_a_call_to_an_unknown_tool_in_this_mode_is_handled_as_in_model_first(self):
        self.reads(("approve_card", {"card": "open"}))
        result = self.say("go on then, make it official")
        self.assertIsInstance(result, Note)
        self.assertIn("Approve", result.message)

    def test_a_planner_failure_falls_back_to_the_classic_rules(self):
        self.reads(sarvam.SarvamError("timeout"))
        result = self.say("how many patients are registered")
        self.assertEqual(result.intent, "patient_count")
        self.assertEqual(self.log_rows()[-1]["route_detail"], "mf_fallback_classic")

    def test_a_lookup_that_then_times_out_falls_back_too(self):
        self.reads(sql_call(COUNT, "Count", purpose="lookup"), sarvam.SarvamError("timeout"))
        result = self.say("how many patients are registered")
        self.assertEqual(result.intent, "patient_count")
        self.assertEqual(self.log_rows()[-1]["route_detail"], "mf_fallback_classic")


# -- the answer ------------------------------------------------------------------------------------------------------------

class Composing(unittest.TestCase):
    def setUp(self):
        from clinic.nlu import sql_reads
        self.sql_reads = sql_reads

    def result(self, columns, rows, truncated=False, total=None):
        from clinic.sql_read import SqlResult
        return SqlResult(list(columns), [tuple(r) for r in rows], truncated, total, 1)

    def compose(self, columns, rows, caption="Caption", show_total=False, language="en-IN", **kw):
        return self.sql_reads.compose(self.result(columns, rows, **kw), caption, show_total, language)

    def test_one_value_is_caption_colon_value(self):
        got = self.compose(["n"], [(3,)], "Patients")
        self.assertEqual((got.sentence, got.data, got.shape), ("Patients: 3.", None, "scalar"))

    def test_money_dates_and_times_read_as_the_app_shows_them(self):
        self.assertEqual(self.compose(["total_rupees"], [(1250.5,)], "Fees").sentence, "Fees: Rs 1,250.50.")
        self.assertEqual(self.compose(["fee_rupees"], [(300.0,)], "Fee").sentence, "Fee: Rs 300.")
        self.assertEqual(self.compose(["amount_rupees"], [(124000,)], "Rent").sentence, "Rent: Rs 1,24,000.")
        self.assertEqual(self.compose(["appt_date"], [("2026-10-09",)], "Next").sentence, "Next: Fri 9 Oct.")
        self.assertEqual(self.compose(["created_ist"], [("2026-10-09 10:00",)], "Booked").sentence, "Booked: Fri 9 Oct 10:00.")
        self.assertEqual(self.compose(["start_time"], [("16:00",)], "Starts").sentence, "Starts: 16:00.")
        self.assertEqual(self.compose(["avg"], [(3.456,)], "Average").sentence, "Average: 3.46.")
        self.assertEqual(self.compose(["x"], [(None,)], "Value").sentence, "Value: -.")

    def test_label_and_number_rows_read_as_a_list(self):
        got = self.compose(["branch", "cancelled"], [("Branch A", 3), ("Branch B", 1), ("Branch C", 0)], "Cancelled last week")
        self.assertEqual((got.sentence, got.data, got.shape), ("Cancelled last week: Branch A 3, Branch B 1, Branch C 0.", None, "pairs"))
        one = self.compose(["branch", "n"], [("Branch A", 2)], "Cancelled")
        self.assertEqual(one.sentence, "Cancelled: Branch A 2.")

    def test_total_only_when_asked_and_only_from_the_last_numeric_column(self):
        rows = [("Rent", 5000.0), ("Electricity", 1200.0)]
        self.assertEqual(self.compose(["description", "amount_rupees"], rows, "Expenses").sentence, "Expenses: Rent Rs 5,000, Electricity Rs 1,200.")
        self.assertEqual(self.compose(["description", "amount_rupees"], rows, "Expenses", show_total=True).sentence,
                         "Expenses: Rent Rs 5,000, Electricity Rs 1,200. Total Rs 6,200.")
        self.assertEqual(self.compose(["b", "n"], [("A", 2), ("B", 1)], "Count", show_total=True).sentence, "Count: A 2, B 1. Total 3.")
        table = self.compose(["patient_name", "appt_date", "n"], [("A", "2026-10-09", 2), ("B", "2026-10-10", 5)], "Rows", show_total=True)
        self.assertEqual(table.sentence, "Rows: 2 result(s). Total 7.")
        text_last = self.compose(["a", "b"], [("x", "y")], "Words", show_total=True)
        self.assertNotIn("Total", text_last.sentence)                       # a text column cannot be added up
        scalar = self.compose(["n"], [(3,)], "Count", show_total=True)
        self.assertEqual(scalar.sentence, "Count: 3.")                      # one value needs no total

    def test_several_columns_or_rows_become_a_table_with_readable_headings_and_no_id(self):
        got = self.compose(["id", "patient_name", "appt_date", "start_time", "fee_rupees"],
                           [(1, "Amit", "2026-10-09", "10:00", 500), (2, "Sunita", "2026-10-09", "11:00", 300)], "Booked")
        self.assertEqual(got.shape, "table")
        self.assertEqual(got.sentence, "Booked: 2 result(s).")
        self.assertEqual(got.data[0], {"Patient": "Amit", "Date": "2026-10-09", "Start": "10:00", "fee_rupees": 500})
        self.assertEqual(list(got.data[0]), ["Patient", "Date", "Start", "fee_rupees"])         # money keeps its name: the page formats it
        self.assertNotIn("id", json.dumps(got.data))

    def test_a_table_with_two_rows_of_one_column_is_a_table_not_a_value(self):
        got = self.compose(["patient_name"], [("A",), ("B",)], "Names")
        self.assertEqual((got.shape, got.data), ("table", [{"Patient": "A"}, {"Patient": "B"}]))

    def test_headings_never_collide(self):
        got = self.compose(["branch_code", "branch_name", "n_x"], [("A", "Branch A", 1), ("B", "Branch B", 2)], "Branches")
        self.assertEqual(len(set(k.lower() for k in got.data[0])), 3)

    def test_nothing_found_names_the_caption(self):
        got = self.compose(["patient_name"], [], "No shows last week")
        self.assertEqual((got.sentence, got.data, got.shape), ("No shows last week: Nothing found for that.", None, "none"))

    def test_a_truncated_result_says_how_many_there_were(self):
        got = self.compose(["description", "amount_rupees"], [("x", 1)] * 200, "Expenses", truncated=True, total=340)
        self.assertEqual((got.sentence, got.scope_caption), ("Expenses: 340 result(s).", "Showing the first 200 of 340"))
        self.assertEqual(len(got.data), 200)
        unknown = self.compose(["a", "b"], [("x", "y")] * 200, "Rows", truncated=True, total=None)
        self.assertEqual(unknown.scope_caption, "Showing the first 200")
        withtotal = self.compose(["d", "n"], [("x", 1)] * 200, "Rows", show_total=True, truncated=True, total=340)
        self.assertNotIn("Total", withtotal.sentence)                       # a total of a cut list would be wrong

    def test_hinglish_wording(self):
        h = "hi-IN"
        self.assertEqual(self.compose(["x"], [], "Pichle hafte", language=h).sentence, "Pichle hafte: Is ke liye kuch nahi mila.")
        self.assertEqual(self.compose(["a", "b", "c"], [(1, 2, 3), (4, 5, 6)], "Nateeje", language=h).sentence, "Nateeje: 2 nateeje.")
        self.assertEqual(self.compose(["b", "n"], [("A", 2), ("B", 1)], "Cancel", language=h, show_total=True).sentence, "Cancel: A 2, B 1. Kul 3.")
        self.assertEqual(self.compose(["n"], [(7,)], "Kul patient", language=h).sentence, "Kul patient: 7.")
        cut = self.compose(["a", "b"], [("x", "y")] * 200, "Rows", language=h, truncated=True, total=250)
        self.assertEqual(cut.scope_caption, "Pehle 200 dikha raha hoon, kul 250")

    def test_a_missing_caption_gets_a_plain_default(self):
        self.assertEqual(self.sql_reads.compose(self.result(["n"], [(2,)]), None, False, "en-IN").sentence, "Result: 2.")
        self.assertEqual(self.sql_reads.compose(self.result(["n"], [(2,)]), None, False, "hi-IN").sentence, "Nateeja: 2.")


class AnswerEndToEnd(ModelReadsCase):
    def test_the_answer_carries_the_fixed_source_line_and_the_table_data(self):
        self.reads(sql_call(LIST, "Booked on Thursday"))
        result = self.say("who is booked on thursday")
        self.assertEqual(result.answer_text, "Booked on Thursday: 3 result(s). Source: local clinic records, updated just now.")
        self.assertEqual([row["Patient"] for row in result.data], ["Rakesh Verma", "Mohan Lal", "Sunita Devi"])
        self.assertEqual(list(result.data[0]), ["Patient", "Date", "Start"])
        self.assertEqual(result.citation.source, "local clinic records")

    def test_a_hindi_session_gets_hinglish_wording(self):
        self.reads(sql_call("SELECT patient_name FROM v_appointments WHERE status = 'no_show'", "Pichle hafte no show"))
        result = self.say("kisi ka no show hua", language="hi-IN")
        self.assertTrue(result.answer_text.startswith("Pichle hafte no show: Is ke liye kuch nahi mila. Source:"))

    def test_a_per_branch_count_with_zeros(self):
        self.reads(sql_call("SELECT b.name AS branch, COUNT(a.id) AS booked FROM v_branches b LEFT JOIN v_appointments a "
                            "ON a.branch_code = b.code AND a.status = 'booked' GROUP BY b.code, b.name ORDER BY b.code", "Booked per branch"))
        result = self.say("how many booked per branch")
        self.assertEqual(result.answer_text.split(" Source")[0], "Booked per branch: Branch A 2, Branch B 1, Branch C 0.")

    def test_an_open_question_is_asked_again_after_the_read(self):
        self.call("book_appointment", patient_name="Rakesh Verma", date="2026-10-09")
        ask = self.say("book Rakesh Verma tomorrow")
        self.assertIsInstance(ask, AskResult)
        self.reads(sql_call(COUNT, "Appointments on Thursday"))
        result = self.say("how many appointments on thursday")
        self.assertIsInstance(result, ReadResult)
        self.assertIn("Still waiting for your answer", result.answer_text)
        self.assertTrue(result.answer_text.endswith("Source: local clinic records, updated just now."))
        self.assertIsNotNone(self.ctx.pending)                                  # the question is still open


# -- memory -----------------------------------------------------------------------------------------------------------------

class Memory(ModelReadsCase):
    def test_appointment_shaped_rows_are_remembered_as_the_list_on_screen(self):
        self.reads(sql_call(LIST, "Booked on Thursday"))
        self.say("who is booked on thursday")
        self.assertEqual([r["patient_name"] for r in self.ctx.list_rows], ["Rakesh Verma", "Mohan Lal", "Sunita Devi"])
        ids = [r[0] for r in self.conn.execute("SELECT id FROM appointments WHERE status = 'booked' ORDER BY start_time")]
        self.assertEqual([r["id"] for r in self.ctx.list_rows], ids)
        self.assertEqual(self.ctx.list_scope, "Booked on Thursday")

    def test_cancel_the_second_one_finds_the_second_listed_appointment(self):
        self.reads(sql_call(LIST, "Booked on Thursday"))
        self.say("who is booked on thursday")
        self.call("cancel_appointment", patient_name="Mohan Lal")
        card = self.say("cancel the second one")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.intent, "cancel_appointment")
        second = self.conn.execute("SELECT id FROM appointments WHERE status = 'booked' ORDER BY start_time").fetchall()[1][0]
        self.assertEqual(card.slots["appointment_id"], second)
        self.assertEqual(self.table_counts("audit_log")[0], 0)                  # only a card: nothing is cancelled until Approve

    def test_rows_without_the_three_columns_do_not_replace_the_list(self):
        self.reads(sql_call(LIST, "Booked on Thursday"))
        self.say("who is booked on thursday")
        self.reads(sql_call(COUNT, "Count"))
        self.say("how many are there")
        self.assertEqual(len(self.ctx.list_rows), 3)
        self.reads(sql_call("SELECT patient_name, appt_date FROM v_appointments", "Without a time"))
        self.say("names and days")
        self.assertEqual(len(self.ctx.list_rows), 3)

    def test_the_next_turn_is_told_what_this_one_was_without_the_sql(self):
        self.reads(sql_call(COUNT, "Appointments on Thursday"))
        self.say("how many appointments are there on thursday")
        self.assertEqual(self.ctx.turns[-1]["call"], "sql_read(purpose=answer, caption=Appointments on Thursday)")
        self.assertEqual(self.ctx.turns[-1]["result"], "Appointments on Thursday: 3.")
        self.assertNotIn("SELECT", json.dumps(self.ctx.turns[-1]))

    def test_the_state_card_has_nothing_about_the_schema(self):
        self.reads(sql_call(LIST, "Booked on Thursday"))
        self.say("who is booked on thursday")
        self.reads(sql_call(COUNT, "Count"))
        self.say("and how many")
        user = self.backend.calls[-1][1]
        self.assertIn("STATE CARD", user)
        self.assertIn("List on screen", user)
        self.assertNotIn("v_appointments", user)
        self.assertNotRegex(user, r"\bid\b\s*=")


# -- the planner log -----------------------------------------------------------------------------------------------------------

class LogRow(ModelReadsCase):
    def use_usage(self, script, *usages):
        self.backend = UsageFake(script, usages)
        planner.set_backend(self.backend)

    def test_the_row_has_the_sql_in_the_args_a_route_detail_and_a_note_and_no_rows(self):
        self.reads(sql_call(PHONES, "Phones", purpose="lookup"), sql_call(COUNT, "Count"))
        self.say("how many are booked on thursday")
        row = self.log_rows()[-1]
        self.assertEqual((row["route_taken"], row["route_detail"], row["planner_tool"], row["final_intent"]), ("planner", "sql:2", "sql_read", "sql_read"))
        self.assertEqual(json.loads(row["planner_args_json"])["sql"], COUNT)               # the final call's SQL is in the args
        self.assertRegex(row["override_notes"], r"sql: 2 step\(s\), 1 rows, \d+ ms")
        everything = json.dumps(row, ensure_ascii=False)
        for value in ("Rakesh Verma", "Mohan Lal", "Sunita Devi", "9000000001", "9000000002"):
            self.assertNotIn(value, everything)                                              # no result rows, ever
        self.assertIsNotNone(row["state_card"])

    def test_the_note_says_repaired_and_the_encoding_used(self):
        self.reads(sql_call("SELECT nope FROM v_appointments", "Bad"), sql_call(COUNT, "Count"))
        self.say("how many are booked on thursday")
        self.assertRegex(self.log_rows()[-1]["override_notes"], r"sql: 1 step\(s\), 1 rows, \d+ ms, repaired, tool")

    def test_tokens_and_cost_add_up_across_the_steps_of_one_command(self):
        usages = [sarvam.Usage(3700, 60, 0), sarvam.Usage(3900, 70, 0), sarvam.Usage(4100, 40, 1000)]
        self.use_usage([sql_call(COUNT, "One", purpose="lookup"), sql_call(COUNT, "Two", purpose="lookup"), sql_call(COUNT, "Three")], *usages)
        self.say("a three step question")
        row = self.log_rows()[-1]
        self.assertEqual((row["tokens_in"], row["tokens_out"]), (3700 + 3900 + 4100, 60 + 70 + 40))
        self.assertEqual(row["cost_paise"], sum(sarvam.cost_paise(u) for u in usages))
        self.assertGreater(row["cost_paise"], max(sarvam.cost_paise(u) for u in usages))
        self.assertEqual(row["route_detail"], "sql:3")

    def test_a_failed_command_still_costs_what_its_calls_cost(self):
        usages = [sarvam.Usage(3700, 60, 0), sarvam.Usage(3900, 70, 0)]
        self.use_usage([sql_call("SELECT nope FROM v_appointments", "Bad"), sql_call("SELECT nope2 FROM v_appointments", "Bad")], *usages)
        self.say("this fails")
        row = self.log_rows()[-1]
        self.assertEqual(row["cost_paise"], sum(sarvam.cost_paise(u) for u in usages))

    def test_a_backend_without_usage_logs_no_tokens(self):
        self.reads(sql_call(COUNT, "Count"))
        self.say("how many")
        row = self.log_rows()[-1]
        self.assertEqual((row["tokens_in"], row["tokens_out"], row["cost_paise"]), (None, None, 0))

    def test_other_tools_keep_the_model_first_row_shape(self):
        self.call("book_appointment", patient_name="Rakesh Verma", date="2026-10-09", time="17:00")
        self.say("book Rakesh Verma tomorrow at 5 pm")
        row = self.log_rows()[-1]
        self.assertEqual((row["route_detail"], row["planner_tool"]), ("mf:book_appointment", "book_appointment"))
        self.assertNotIn("sql", row["override_notes"] or "")


# -- the prompt ------------------------------------------------------------------------------------------------------------------

class PromptBudget(unittest.TestCase):
    """The model-reads prompt is bigger on purpose (the schema and the reading rules, about 1.5k tokens); Classic and New
    keep their own budgets (tests/test_planner_reads.py)."""

    def setUp(self):
        from clinic.nlu import sql_reads
        self.sql_reads = sql_reads
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        add_branches(self.conn)

    def test_the_extra_text_stays_inside_its_budget(self):
        extra = self.sql_reads.prompt_extra(self.conn, FIXED)
        self.assertLessEqual(len(extra), 8200)                                          # about 2k tokens (rules, two examples, views)
        self.assertLessEqual(len(json.dumps(self.sql_reads.SQL_READ_SCHEMA)), 1200)
        total = (len(planner.build_system_prompt(self.conn, FIXED)) + len(dialogue.MODEL_FIRST_RULES) + len(extra)
                 + len(json.dumps(self.sql_reads.schemas(), ensure_ascii=False)))
        self.assertLessEqual(total, 27500, "the model-reads prompt is {} characters".format(total))

    def test_classic_and_model_first_prompts_are_not_touched(self):
        base = planner.build_system_prompt(self.conn, FIXED)
        self.assertNotIn("READING MODE", base)
        self.assertNotIn("v_appointments", base)
        self.assertLessEqual(len(base) + len(json.dumps(tools.schemas(), ensure_ascii=False)), 1417 + 9894 + 1800)

    def test_the_schema_block_lists_every_view_and_the_rules_name_the_helpers(self):
        extra = self.sql_reads.prompt_extra(self.conn, FIXED)
        for view in read_schema.VIEW_NAMES:
            self.assertIn(view, extra)
        for word in ("name_match", "phone10", ":today", "LEFT JOIN", "query(entity=availability)", "purpose='lookup'", "rupees"):
            self.assertIn(word, extra)
        self.assertIn("2026-10-05 to 2026-10-11", extra)

    def test_the_reading_rules_say_what_the_live_comparison_showed_was_missing(self):
        rules = read_schema.DOMAIN_RULES
        self.assertNotIn("ONE SELECT", rules)
        for phrase in ("TESTS that return 1 or 0", "name_match(x.patient_name, 'Some Name') = 1", "never pass a bind parameter",
                       "Devanagari stays Devanagari", "test the phone with phone10 and do not test the name",
                       "one SELECT (or WITH ... SELECT) per call", "purpose='answer' is final", "purpose='lookup'", "at most 3 queries",
                       "the WHERE must filter the date column", "Count only booked or confirmed", "in the ON clause (not WHERE)",
                       "NO digits", "Appointments per doctor", "fix only what the error says and keep every part of the question",
                       "query(entity=availability)", "ORDER BY appt_date, start_time", "Money is in rupees", "walk-in", "'query:' hints"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, rules)

    def test_the_examples_are_invented_dated_from_today_and_in_the_prompt(self):
        extra = self.sql_reads.prompt_extra(self.conn, FIXED)
        examples = self.sql_reads.example_lines(FIXED)
        self.assertIn(examples, extra)
        self.assertIn("BETWEEN '2026-10-05' AND '2026-10-11'", examples)                     # Monday to Sunday of FIXED's week
        later = self.sql_reads.example_lines(date(2027, 3, 3))
        self.assertIn("BETWEEN '2027-03-01' AND '2027-03-07'", later)
        self.assertNotIn("2026", later)
        self.assertIn("name_match(a.patient_name, 'Kavita Joshi') = 1", examples)
        self.assertIn("LEFT JOIN v_appointments a ON a.doctor_name = d.name AND a.status", examples)

    def test_the_fixed_prefix_is_first_and_the_extra_does_not_depend_on_the_database_rows(self):
        a = self.sql_reads.prompt_extra(self.conn, FIXED)
        self.conn.execute("INSERT INTO patients (name, phone) VALUES ('Zed', '9000000009')")
        b = self.sql_reads.prompt_extra(self.conn, FIXED)
        self.assertEqual(a, b)                                                            # no patient or row data in the prompt


# -- Classic and New never touch any of it --------------------------------------------------------------------------------

PROBE = r"""
import json, os, sys
sys.path.insert(0, ".")
import tests                              # the usual offline switches
os.environ["INTENT_LLM_ENABLED"] = "1"    # ... then the planner ON, with a fake backend: no model is ever reached
os.environ["INTENT_PLANNER_ENABLED"] = "1"
os.environ["PLANNER_BACKEND"] = "local"
from unittest import mock
for target, side in (("httpx.post", AssertionError("the probe reached a model")), ("httpx.get", AssertionError("the probe reached the network")),
                     ("socket.create_connection", AssertionError("the probe reached the network"))):
    mock.patch(target, side_effect=side).start()              # nothing here may reach Ollama, Sarvam or any network
from clinic import architecture, db
from clinic.adapters.local_sqlite import LocalSQLiteAdapter
from clinic.nlu import planner
from clinic.voice_context import VoiceContext
from clinic.voice_turns import handle_turn
mock.patch("clinic.nlu.parser.pick_intent", return_value=None).start()       # the one-word label picker and the name model: off
mock.patch("clinic.nlu.parser.prefetch_name", mock.Mock()).start()
mode = sys.argv[1]
conn = db.connect(":memory:")
conn.execute("INSERT INTO patients (name, phone, age) VALUES ('Rakesh Verma', '9000000001', 40)")
conn.commit()
architecture.set_mode(conn, mode)
planner.set_backend(planner.FakeBackend([("query", {"entity": "patients", "aggregate": "count"}),
                                         ("sql_read", {"sql": "SELECT COUNT(*) FROM v_patients", "purpose": "answer", "caption": "Patients"}),
                                         ("book_appointment", {"patient_name": "Rakesh Verma", "date": "2026-10-09", "time": "17:00"})]))
adapter = LocalSQLiteAdapter()
ctx = VoiceContext()
out = []
for text in ("how many patients are registered", "how many patients are there", "book Rakesh Verma tomorrow at 5 pm", "make it 6 pm"):
    try:
        out.append(type(handle_turn(ctx, conn, text, adapter, adapter, "en-IN", frozenset({"book_appointment"}))).__name__)
    except Exception as exc:
        out.append(type(exc).__name__)
watched = ("clinic.sql_read", "clinic.nlu.sql_reads", "clinic.read_schema")
print(json.dumps({"results": out, "loaded": sorted(m for m in sys.modules if m in watched)}))
"""


def run_probe(mode):
    done = subprocess.run([sys.executable, "-c", PROBE, mode], capture_output=True, text=True, cwd=str(ROOT), timeout=180)
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout.strip().splitlines()[-1])
    assert "AssertionError" not in report["results"], report            # the probe never reached a model or the network
    return report


class RollbackNeverImportsTheNewModules(unittest.TestCase):
    def test_a_fresh_process_in_classic_never_loads_them(self):
        report = run_probe("classic")
        self.assertEqual(report["loaded"], [])

    def test_a_fresh_process_in_model_first_never_loads_them_even_when_the_model_calls_sql_read(self):
        report = run_probe("model_first")
        self.assertEqual(report["loaded"], [])
        self.assertEqual(len(report["results"]), 4)

    def test_a_fresh_process_in_model_reads_does_load_them(self):
        report = run_probe("model_reads")
        self.assertEqual(report["loaded"], ["clinic.nlu.sql_reads", "clinic.read_schema", "clinic.sql_read"])


class RollbackSpies(PlannerCase):
    """With classic or model_first none of the model-reads functions is called, and what the planner is sent is exactly
    what it always was."""

    def setUp(self):
        super().setUp()
        from clinic import sql_read
        from clinic.nlu import sql_reads
        boom = AssertionError("a model-reads function ran outside model_reads mode")
        for target in (mock.patch.object(sql_reads, "ModelReadsRun", side_effect=boom), mock.patch.object(sql_reads, "schemas", side_effect=boom),
                       mock.patch.object(sql_reads, "prompt_extra", side_effect=boom), mock.patch.object(sql_read, "run_query", side_effect=boom),
                       mock.patch.object(sql_read, "ReadSession", side_effect=boom)):
            target.start()
            self.addCleanup(target.stop)

    def test_classic_sends_the_original_prompt_and_tools(self):
        architecture.set_mode(self.conn, "classic")
        self.call("query", entity="patients", aggregate="count")
        self.say("how many patients are registered")
        self.call("query", entity="patients", aggregate="list", fields=["name"])
        self.say("who all do we have on file")
        system, user, offered = self.backend.calls[-1]
        self.assertEqual(system, planner.build_system_prompt(self.conn, planner.date.today()))
        self.assertEqual(offered, tools.schemas())
        self.assertEqual(len(offered), 19)

    def test_model_first_sends_the_original_prompt_and_tools(self):
        architecture.set_mode(self.conn, "model_first")
        self.call("query", entity="patients", aggregate="list", fields=["name"])
        self.say("who all do we have on file")
        system, user, offered = self.backend.calls[-1]
        self.assertEqual(system, planner.build_system_prompt(self.conn, planner.date.today()) + dialogue.MODEL_FIRST_RULES)
        self.assertEqual(offered, tools.schemas(dialogue=True))
        self.assertEqual(len(offered), 24)

    def test_a_sql_read_call_in_model_first_is_just_an_unknown_tool_and_the_rules_decide(self):
        architecture.set_mode(self.conn, "model_first")
        self.call("sql_read", sql="SELECT COUNT(*) FROM v_patients", purpose="answer", caption="Patients")
        result = self.say("how many patients are registered")
        self.assertEqual(result.intent, "patient_count")                       # the classic rules answered
        self.assertEqual(self.log_rows()[-1]["route_detail"], "mf_fallback_classic")

    def test_a_sql_read_call_in_classic_is_just_an_unknown_tool_too(self):
        architecture.set_mode(self.conn, "classic")
        self.call("sql_read", sql="SELECT COUNT(*) FROM v_patients", purpose="answer", caption="Patients")
        result = self.say("how many patients are registered")
        self.assertEqual(result.intent, "patient_count")
        self.assertEqual(self.log_rows()[-1]["route_taken"], "rules")



class FlippingTheSetting(PlannerCase):
    """The setting is read at every command: flip it mid-conversation and the next sentence uses the other path."""

    def test_flipping_between_the_three_modes_changes_the_path_on_the_very_next_sentence(self):
        architecture.set_mode(self.conn, "model_first")
        self.call("query", entity="patients", aggregate="count")
        first = self.say("how many patients are registered")
        self.assertEqual((first.intent, self.log_rows()[-1]["route_detail"]), ("patient_count", "mf:query"))
        architecture.set_mode(self.conn, "model_reads")
        self.call("sql_read", sql="SELECT COUNT(*) AS n FROM v_patients", purpose="answer", caption="Patients registered")
        second = self.say("how many patients are registered")
        self.assertEqual((second.intent, self.log_rows()[-1]["route_detail"]), ("sql_read", "sql:1"))
        self.assertTrue(second.answer_text.startswith("Patients registered: 4."))
        architecture.set_mode(self.conn, "model_first")
        third = self.say("how many patients are registered")                  # the same sql_read call, now an unknown tool
        self.assertEqual((third.intent, self.log_rows()[-1]["route_detail"]), ("patient_count", "mf_fallback_classic"))
        architecture.set_mode(self.conn, "classic")
        self.call("query", entity="patients", aggregate="count")
        fourth = self.say("how many patients are registered")
        self.assertEqual(fourth.intent, "patient_count")
        self.assertEqual(self.log_rows()[-1]["route_taken"], "rules")
        architecture.set_mode(self.conn, "model_reads")
        self.call("sql_read", sql="SELECT COUNT(*) AS n FROM v_patients", purpose="answer", caption="Patients registered")
        self.assertEqual(self.say("how many patients are registered").intent, "sql_read")


# -- the hosted backend: how earlier tool results are sent -----------------------------------------------------------------------

KEY = "sk_test_SECRET_key_0123456789"
SCHEMAS = tools.schemas()


def reply(name=None, arguments=None, prompt=3700, completion=40):
    message = {"role": "assistant", "content": None}
    if name is not None:
        message["tool_calls"] = [{"id": "call_9", "type": "function", "function": {"name": name, "arguments": arguments}}]
    return httpx.Response(200, json={"choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
                                     "usage": {"prompt_tokens": prompt, "completion_tokens": completion}})


class Server:
    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []

    def __call__(self, request):
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def body(self, index):
        return json.loads(self.requests[index].content)


def has_tool_role(request):
    return any(m["role"] == "tool" for m in json.loads(request.content)["messages"])


REFUSES_TOOL_ROLE = lambda status: (lambda request: httpx.Response(status, json={"error": "bad request"}) if has_tool_role(request)    # noqa: E731
                                    else reply("sql_read", json.dumps({"sql": "SELECT 1", "purpose": "answer", "caption": "Done"})))
HISTORY = [(planner.ToolCall("sql_read", {"sql": "SELECT patient_name FROM v_appointments", "purpose": "lookup", "caption": "Names"}),
            "2 row(s):\npatient_name\nAmit Dua\nSunita Devi")]


def backend_with(server):
    return sarvam.SarvamBackend(api_key=KEY, transport=httpx.MockTransport(server), sleep=lambda s: None)


class MessageEncoding(unittest.TestCase):
    def go(self, backend, history=HISTORY):
        return backend.plan_with_history("SYSTEM", "how many", SCHEMAS, history)

    def test_the_tool_results_go_as_openai_style_tool_messages_first(self):
        server = Server(reply("sql_read", json.dumps({"sql": "SELECT 1", "purpose": "answer", "caption": "Done"})))
        backend = backend_with(server)
        call = self.go(backend)
        self.assertEqual(call.name, "sql_read")
        self.assertEqual(len(server.requests), 1)
        messages = server.body(0)["messages"]
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "tool"])
        self.assertEqual(messages[1]["content"], "how many")
        assistant, tool = messages[2], messages[3]
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "sql_read")
        self.assertEqual(json.loads(assistant["tool_calls"][0]["function"]["arguments"])["purpose"], "lookup")
        self.assertEqual(tool["tool_call_id"], assistant["tool_calls"][0]["id"])
        self.assertIn("Amit Dua", tool["content"])
        self.assertEqual(backend.last_encoding, "tool")
        self.assertNotIn(KEY, server.requests[0].content.decode())

    def test_a_400_makes_the_same_call_again_with_the_result_as_a_plain_user_message(self):
        for status in (400, 422):
            with self.subTest(status=status):
                server = Server(REFUSES_TOOL_ROLE(status))
                backend = backend_with(server)
                call = self.go(backend)
                self.assertEqual(call.name, "sql_read")
                self.assertEqual(len(server.requests), 2)
                self.assertTrue(has_tool_role(server.requests[0]))
                second = server.body(1)["messages"]
                self.assertEqual([m["role"] for m in second], ["system", "user"])
                self.assertTrue(second[1]["content"].startswith("how many"))
                self.assertIn("Query result: 2 row(s)", second[1]["content"])
                self.assertIn("Amit Dua", second[1]["content"])
                self.assertEqual(server.body(1)["tools"], server.body(0)["tools"])
                self.assertEqual(backend.last_encoding, "user_message")

    def test_the_encoding_that_worked_is_remembered_for_the_next_step(self):
        server = Server(REFUSES_TOOL_ROLE(400))
        backend = backend_with(server)
        self.go(backend)
        self.go(backend)
        self.assertEqual(len(server.requests), 3)                              # 2 for the first step, 1 for the second
        self.assertFalse(has_tool_role(server.requests[2]))
        self.assertEqual(backend.last_encoding, "user_message")

    def test_a_400_on_both_encodings_is_an_error_and_never_remembered(self):
        server = Server(httpx.Response(400, json={"error": "no"}))
        backend = backend_with(server)
        with self.assertRaises(sarvam.SarvamError) as caught:
            self.go(backend)
        self.assertEqual(caught.exception.reason, "http 400")
        self.assertEqual(len(server.requests), 2)
        self.assertFalse(getattr(backend, "_prefers_user_message", False))

    def test_only_400_and_422_trigger_the_other_encoding(self):
        for status in (401, 403, 404):
            with self.subTest(status=status):
                server = Server(httpx.Response(status, json={}))
                with self.assertRaises(sarvam.SarvamError):
                    self.go(backend_with(server))
                self.assertEqual(len(server.requests), 1)

    def test_a_plain_plan_is_byte_identical_and_a_5xx_still_retries_once(self):
        server = Server(reply("sql_read", json.dumps({"sql": "SELECT 1"})))
        backend = backend_with(server)
        backend.plan("SYSTEM", "how many", SCHEMAS)
        body = server.body(0)
        self.assertEqual(set(body), {"model", "messages", "tools", "tool_choice", "temperature", "max_tokens", "reasoning_effort"})
        self.assertEqual(body["messages"], [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "how many"}])
        self.assertIsNone(backend.last_encoding)
        server = Server(httpx.Response(503), reply("sql_read", json.dumps({"sql": "SELECT 1"})))
        retrying = backend_with(server)
        self.go(retrying)
        self.assertEqual(len(server.requests), 2)
        self.assertTrue(has_tool_role(server.requests[1]))                      # the retry is the same encoding, not the fallback

    def test_the_breaker_usage_and_key_handling_apply_to_every_step(self):
        server = Server(reply("sql_read", json.dumps({"sql": "SELECT 1"}), prompt=1234, completion=21))
        backend = backend_with(server)
        self.go(backend)
        self.assertEqual((backend.last_usage.prompt_tokens, backend.last_usage.completion_tokens), (1234, 21))
        failing = Server(httpx.ConnectError("boom"))
        down = backend_with(failing)
        for _ in range(3):
            with self.assertRaises(sarvam.SarvamError) as caught:
                self.go(down)
            self.assertNotIn(KEY, str(caught.exception))
        with self.assertRaises(sarvam.SarvamError) as caught:
            self.go(down)
        self.assertEqual(caught.exception.reason, "circuit open")

    def test_without_a_key_nothing_is_sent(self):
        server = Server(reply("sql_read", "{}"))
        backend = sarvam.SarvamBackend(api_key="", transport=httpx.MockTransport(server))
        with patch.dict(os.environ, {"SARVAM_API_KEY": ""}):
            with self.assertRaises(sarvam.SarvamError):
                self.go(backend)
        self.assertEqual(server.requests, [])

    def test_the_base_backend_and_the_fake_encode_the_history_as_plain_text(self):
        class Plain(planner.Backend):
            def plan(self, system, user, tool_schemas):
                self.seen = user
                return None
        plain = Plain()
        plain.plan_with_history("S", "how many", [], HISTORY)
        self.assertTrue(plain.seen.startswith("how many\n\n"))
        self.assertIn("Query result: 2 row(s)", plain.seen)
        self.assertEqual(plain.last_encoding, "user_message")
        fake = planner.FakeBackend([("clarify", {"question": "Which?"})])
        self.assertEqual(fake.plan_with_history("S", "how many", [], HISTORY).name, "clarify")
        self.assertEqual(fake.histories, [HISTORY])

    def test_ollama_gets_tool_calls_and_tool_messages_and_its_plain_plan_is_unchanged(self):
        body = {}

        def post(url, json=None, timeout=None):
            body.update(json)
            return httpx.Response(200, json={"message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "sql_read", "arguments": {"sql": "SELECT 1", "purpose": "answer"}}}]}},
                request=httpx.Request("POST", url))
        local = planner.OllamaBackend(model="m", url="http://localhost:1/api/chat", timeout=5)
        with patch("clinic.nlu.planner.httpx.post", side_effect=post):
            local.plan("SYSTEM", "how many", SCHEMAS)
            self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
            self.assertEqual(set(body), {"model", "messages", "tools", "stream", "think", "keep_alive", "options"})
            call = local.plan_with_history("SYSTEM", "how many", SCHEMAS, HISTORY)
        self.assertEqual(call.name, "sql_read")
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user", "assistant", "tool"])
        self.assertEqual(body["messages"][2]["tool_calls"][0]["function"]["name"], "sql_read")
        self.assertIn("Amit Dua", body["messages"][3]["content"])
        self.assertEqual(local.last_encoding, "tool")


# -- the replay -------------------------------------------------------------------------------------------------------------------

class ReplayConfiguration(unittest.TestCase):
    def setUp(self):
        from scripts.replay import configs, engine
        from tests.replay_cases import ALL_CASES
        self.configs, self.engine, self.cases = configs, engine, ALL_CASES
        self.sql_cases = [c for c in ALL_CASES if c["category"] == "sql_reads"]

    def test_the_two_configs_exist_and_only_the_scripted_one_is_offline(self):
        scripted, live = self.configs.get("model_reads_scripted"), self.configs.get("model_reads_live")
        self.assertEqual((scripted.architecture, scripted.planner, scripted.live), ("model_reads", "scripted", False))
        self.assertEqual((live.architecture, live.planner, live.live), ("model_reads", "live", True))
        self.assertIn("model_reads_scripted", self.configs.OFFLINE)
        self.assertNotIn("model_reads_live", self.configs.OFFLINE)

    def test_the_live_config_needs_the_flags_and_a_key_like_the_other_live_ones(self):
        with self.assertRaises(self.configs.ConfigError):
            self.configs.check_live(["model_reads_live"], live=False, yes=False, env={})
        with self.assertRaises(self.configs.ConfigError):
            self.configs.check_live(["model_reads_live"], live=True, yes=True, env={})
        self.configs.check_live(["model_reads_live"], live=True, yes=True, env={"SARVAM_API_KEY": "k"})
        with self.assertRaises(self.engine.LiveNotAllowed):
            self.engine.run_cases(self.sql_cases[:1], self.configs.get("model_reads_live"))

    def test_a_multi_step_turn_is_counted_twice_in_the_live_estimate(self):
        spoken = sum(1 for c in self.cases for t in c["turns"] if "say" in t)
        self.assertEqual(self.configs.planner_calls_estimate(self.cases, ["model_reads_live"]), 2 * spoken)
        self.assertEqual(self.configs.planner_calls_estimate(self.cases, ["model_first_live"]), spoken)

    def test_the_dsl_accepts_the_third_mode_and_a_list_of_calls_for_one_turn(self):
        from tests.replay_cases.dsl import architecture as flip, call, say
        self.assertEqual(flip("model_reads"), {"architecture": "model_reads", "expect": {}})
        turn = say("x", [call("sql_read", sql="SELECT 1"), call("sql_read", sql="SELECT 2")])
        self.assertEqual(len(turn["model"]), 2)

    def test_the_scripted_backend_hands_out_a_list_one_call_at_a_time_and_then_nothing(self):
        backend = self.engine.ScriptedBackend()
        backend.golden = [("a", {"x": 1}), ("b", {})]
        self.assertEqual(backend.plan("s", "u", [{"function": {"name": "a"}}]), planner.ToolCall("a", {"x": 1}))
        self.assertEqual(backend.plan_with_history("s", "u", [{"function": {"name": "a"}}], []), planner.ToolCall("b", {}))
        self.assertIsNone(backend.plan("s", "u", [{"function": {"name": "a"}}]))
        self.assertEqual(len(backend.histories), 1)

    def test_a_list_golden_is_copied_for_each_run_so_the_conversation_is_not_consumed(self):
        turn = [t for c in self.sql_cases for t in c["turns"] if isinstance(t.get("model"), list)][0]
        before = list(turn["model"])
        got = self.engine._golden_for(self.configs.get("model_reads_scripted"), turn)
        got.pop()
        self.assertEqual(turn["model"], before)

    def test_the_sql_conversations_are_the_agreed_set(self):
        ids = {c["id"] for c in self.sql_cases}
        for needle in ("cancelled_last_week_per_branch", "sum", "count", "roster", "first_name", "phone", "multi_step", "repaired", "notes",
                       "injection", "hindi", "hinglish", "empty", "truncated", "free_slots", "cancelled_by_position", "total"):
            self.assertTrue(any(needle in i for i in ids), needle)
        for case in self.sql_cases:
            self.assertTrue(set(case["configs"]) <= {"model_reads_scripted", "model_reads_live"}, case["id"])

    def test_the_sql_conversations_pass_under_model_reads_scripted_and_are_skipped_elsewhere(self):
        results = self.engine.run_cases(self.cases, self.configs.get("model_reads_scripted"))
        self.assertEqual([r["id"] for r in results if r["verdict"] == "FAIL"], [])
        by_id = {r["id"]: r["verdict"] for r in results}
        for case in self.sql_cases:
            self.assertEqual(by_id[case["id"]], "PASS", case["id"])
        for name in ("classic_scripted", "model_first_scripted"):
            others = self.engine.run_cases(self.sql_cases, self.configs.get(name))
            self.assertEqual({r["verdict"] for r in others}, {"SKIPPED"}, name)

    def test_the_old_model_first_only_conversations_also_run_under_model_reads(self):
        flip = [c for c in self.cases if c["id"].startswith("flip_back_and_forth")][0]
        self.assertEqual(flip["configs"], ["model_first_scripted"])
        result = self.engine.run_cases([flip], self.configs.get("model_reads_scripted"))[0]
        self.assertEqual(result["verdict"], "PASS")

    def test_the_replay_never_reaches_the_network_or_a_key(self):
        with self.engine.sandbox(self.configs.get("model_reads_scripted")):
            self.assertEqual(os.environ.get("SARVAM_API_KEY"), "")
            with self.assertRaises(AssertionError):
                sarvam.SarvamBackend(api_key="x").plan_with_history("s", "u", [], [])


if __name__ == "__main__":
    unittest.main()
