"""The planner's widened read tool, end to end: how a `query` call is validated and routed (the simple shapes
still reach their dedicated read intents with identical answers; the new shapes reach the generic one), the
month ranges ("this month", "pichle mahine") read by code, the planner prompt's size, and the answers.

Everything runs on the fake planner backend and an in-memory database with FIXED dates (Wednesday 2026-10-07
is "today"): the real calendar never matters."""
import json
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.planner_support import PlannerCase  # noqa: E402
from tests.test_conversation import make_db  # noqa: E402
from tests.test_conversation_branches import add_branches  # noqa: E402

from clinic import branches, pipeline, query_tool  # noqa: E402
from clinic.nlu import date_guard, planner, tools  # noqa: E402
from clinic.pipeline import PipelineError, ReadResult  # noqa: E402

FIXED = date(2026, 10, 7)               # a Wednesday
FIXED_NOW = datetime(2026, 10, 7, 11, 0)


class FixedDate(date):
    @classmethod
    def today(cls):
        return FIXED


class ReadCase(PlannerCase):
    """PlannerCase with the clock frozen on 2026-10-07 11:00 and a little fixed-date data to read."""

    def setUp(self):
        super().setUp()
        for target, value in (("clinic.nlu.planner.date", FixedDate), ("clinic.pipeline._local_now", lambda: FIXED_NOW)):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        c = self.conn
        for name, role, branch in (("Sunita Devi", "Nurse", 2), ("Ravi Kumar", "Receptionist", None), ("Meena Joshi", "Nurse", 1)):
            c.execute("INSERT INTO staff (name, role, branch_id) VALUES (?, ?, ?)", (name, role, branch))
        for staff, day, status in ((1, "2026-10-07", "present"), (2, "2026-10-07", "absent"), (3, "2026-10-07", "half_day")):
            c.execute("INSERT INTO attendance (staff_id, attendance_date, status) VALUES (?, ?, ?)", (staff, day, status))
        for patient, day, paise in ((1, "2026-10-01", 50000), (1, "2026-10-05", 30000), (2, "2026-10-05", 45050), (3, "2026-09-20", 100000)):
            c.execute("INSERT INTO visits (patient_id, visit_date, fee_paise) VALUES (?, ?, ?)", (patient, day, paise))
        for day, text, paise in (("2026-10-01", "Clinic rent", 5000000), ("2026-10-02", "electricity", 120000), ("2026-09-15", "stationery", 25050)):
            c.execute("INSERT INTO expenses (expense_date, description, amount_paise) VALUES (?, ?, ?)", (day, text, paise))
        c.execute("INSERT INTO closures (branch_id, start_date, end_date, reason, status) VALUES (2, '2026-10-12', '2026-10-14', 'Doctor on leave', 'applied')")
        c.execute("INSERT INTO closure_moves (closure_id, appointment_id, action, from_date, from_time, result) VALUES (1, 1, 'move', '2026-10-12', '10:00', 'done')")
        c.commit()

    def ask(self, said, tool="query", **args):
        self.call(tool, **args)
        return self.say(said)

    def last_args(self):
        return json.loads(self.log_rows()[-1]["planner_args_json"])


class Routing(unittest.TestCase):
    """_map_query: the shapes an existing read intent answers keep going there."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        add_branches(self.conn)

    def mapped(self, **args):
        ctx = tools.ToolContext(self.conn, FIXED, "")
        return tools.to_parse_result("query", tools.validate("query", args, ctx), ctx)

    def test_the_old_simple_shapes_still_reach_their_dedicated_intents(self):
        for args, intent in (
            ({"entity": "patients", "aggregate": "count"}, "patient_count"),
            ({"entity": "patients", "patient_name": "Amit"}, "patient_lookup"),
            ({"entity": "appointments", "date": "2026-10-07"}, "list_appointments"),
            ({"entity": "appointments", "date": "2026-10-07", "patient_name": "Amit"}, "list_appointments"),
            ({"entity": "appointments", "patient_name": "Amit", "limit": 1}, "next_appointment"),
            ({"entity": "appointments", "date": "2026-10-07", "date_to": "2026-10-13"}, "list_appointments"),
            ({"entity": "availability", "date": "2026-10-07"}, "check_availability"),
            ({"entity": "followups"}, "missed_followups"),
            ({"entity": "followups", "status": "pending"}, "missed_followups"),
            ({"entity": "cashbook"}, "day_end_cashbook"),
            ({"entity": "cashbook", "date": "2026-10-07"}, "day_end_cashbook"),
        ):
            with self.subTest(args=args):
                self.assertEqual(self.mapped(**args)[0], intent)

    def test_the_new_shapes_reach_the_generic_query(self):
        for args in (
            {"entity": "patients", "order": "newest", "limit": 5}, {"entity": "patients", "aggregate": "average", "measure": "age"},
            {"entity": "appointments", "group_by": "doctor"}, {"entity": "appointments", "doctor": "Rao", "date": "2026-10-07"},
            {"entity": "appointments", "date": "2026-10-07", "order": "newest"},
            {"entity": "followups", "branch": "B"}, {"entity": "followups", "doctor": "Rao"}, {"entity": "followups", "order": "oldest", "limit": 1},
            {"entity": "followups", "status": "done"}, {"entity": "cashbook", "aggregate": "sum", "measure": "amount"},
            {"entity": "cashbook", "date": "2026-10-01"},
            {"entity": "staff"}, {"entity": "attendance", "status": "absent"}, {"entity": "branches"}, {"entity": "doctors"},
            {"entity": "schedules", "time": "now"}, {"entity": "visits", "aggregate": "sum", "measure": "fee"},
            {"entity": "expenses", "text": "electricity"}, {"entity": "reminders", "status": "failed"}, {"entity": "closures"},
            {"entity": "blocks"}, {"entity": "audit"}, {"entity": "activity"},
        ):
            with self.subTest(args=args):
                intent, slots = self.mapped(**args)
                self.assertEqual(intent, "query")
                self.assertEqual(slots["entity"], args["entity"])

    def test_a_branch_rides_along_as_a_scope_not_a_filter_and_all_means_every_branch(self):
        intent, slots = self.mapped(entity="staff", branch="B")
        self.assertEqual((intent, slots["branch_id"]), ("query", 2))
        self.assertNotIn("branch", slots)
        self.assertTrue(self.mapped(entity="schedules", branch="all")[1]["all_branches"])

    def test_questions_about_a_branch_or_a_doctors_leave_are_not_a_request_to_close(self):
        from clinic import voice_branch, voice_closure
        for text in ("why is Branch B closed", "Why is branch A closed?", "who is on leave today", "is Dr. Rao on leave tomorrow",
                     "is branch C closed on Monday", "which branches are closed next week", "kyun band hai branch B", "Branch B kyon band hai",
                     "ब्रांच बी क्यों बंद है"):
            with self.subTest(text=text):
                self.assertFalse(voice_closure.is_close_command(self.conn, text, voice_branch.find(self.conn, text)))
        for text in ("close Branch B tomorrow", "Branch A band rahegi kal", "Dr. Rao is on leave tomorrow"):
            with self.subTest(text=text):
                self.assertTrue(voice_closure.is_close_command(self.conn, text, voice_branch.find(self.conn, text)))

    def test_the_tool_schema_lists_every_entity_aggregate_and_new_argument(self):
        properties = tools.BY_NAME["query"].schema()["function"]["parameters"]["properties"]
        self.assertEqual(properties["entity"]["enum"], list(query_tool.ENTITIES))
        self.assertEqual(properties["aggregate"]["enum"], list(query_tool.AGGREGATES))
        for key in ("text", "doctor", "time", "measure", "group_by", "order", "limit", "fields", "status"):
            self.assertIn(key, properties)
        # `kind` and `weekday` are supported by the read tool itself (clinic/query_tool.py) but are not advertised to the
        # model: that keeps the prompt inside its budget. A Monday reaches schedules as a date (turned into its weekday by code).
        for key in ("kind", "weekday"):
            self.assertNotIn(key, properties)
            self.assertIn(key, query_tool.FILTERS)
        self.assertIn("wanted", tools.BY_NAME["unsupported"].schema()["function"]["parameters"]["properties"])
        self.assertEqual(len(tools.schemas()), 19)

    def test_how_a_rejected_query_is_told_apart(self):
        ctx = tools.ToolContext(self.conn, FIXED, "")

        def code(args):
            with self.assertRaises(tools.ToolError) as caught:
                tools.validate("query", args, ctx)
            return caught.exception.code, caught.exception.key

        self.assertEqual(code({"entity": "salaries"}), ("not_listed", "entity"))
        self.assertEqual(code({"entity": "visits", "fields": ["tax"]})[0], "not_listed")
        self.assertEqual(code({"entity": "visits", "group_by": "mood", "aggregate": "count"})[0], "not_listed")
        self.assertEqual(code({"entity": "visits", "aggregate": "sum", "measure": "tips"})[0], "not_listed")
        self.assertEqual(code({"entity": "doctors", "branch": "A"})[0], "bad_value")             # a slip, not an unknown thing
        self.assertEqual(code({"entity": "staff", "date": "2026-10-07"})[0], "bad_value")
        self.assertEqual(code({"entity": "visits", "salary": "high"}), ("unknown_arg", "salary"))
        self.assertEqual(code({"entity": "visits", "date": "yesterday"})[0], "bad_value")
        self.assertEqual(code({"entity": "patients", "aggregate": "sum"})[0], "bad_value")      # no measure: malformed, not unlisted


class Answers(ReadCase):
    """A scripted `query` call, turned into the answer on screen."""

    def read(self, said, **args):
        result = self.ask(said, **args)
        self.assertIsInstance(result, ReadResult)
        return result

    def test_people(self):
        nurses = self.read("list the nurses", entity="staff", text="nurse")
        self.assertEqual((nurses.intent, [r["name"] for r in nurses.data]), ("query", ["Meena Joshi", "Sunita Devi"]))
        self.assertTrue(nurses.answer_text.startswith("2 staff members matching nurse."))
        absent = self.read("who is absent today", entity="attendance", status="absent", date="2026-10-07")
        self.assertEqual([r["staff"] for r in absent.data], ["Ravi Kumar"])
        doctors = self.read("which doctors do we have", entity="doctors")
        self.assertEqual([r["name"] for r in doctors.data], ["Dr. Mehta", "Dr. Rao", "Dr. Iyer"])

    def test_places(self):
        today = self.read("which branches are open today", entity="branches", status="open", date="2026-10-07")
        self.assertEqual([r["code"] for r in today.data], ["A", "B", "C"])
        self.assertEqual(today.answer_text.split(" Source")[0], "3 branches (open) on Wed 7 Oct.")
        why = self.read("why is branch B closed", entity="branches", branch="B", status="closed")
        self.assertEqual(why.data, [])
        on_duty = self.read("who is on duty now at branch A", entity="schedules", branch="A", date="2026-10-07", time="now")
        self.assertEqual([(r["doctor"], r["start_time"]) for r in on_duty.data], [("Dr. Mehta", "09:00")])        # 11:00 on the frozen clock
        self.assertEqual(on_duty.scope_caption, "Branch A")
        closed = self.read("which branches are closed next week", entity="closures", date="2026-10-12", date_to="2026-10-18")
        self.assertEqual([(r["branch"], r["patients_moved"]) for r in closed.data], [("Branch B", 1)])

    def test_money_and_visits(self):
        total = self.read("how much did we collect this month", entity="visits", aggregate="sum", measure="fee",
                          date="2026-10-01", date_to="2026-10-31")
        self.assertIsNone(total.data)
        self.assertEqual(total.answer_text.split(" Source")[0], "Total fees collected this month: Rs 1,250.50 (3 visits).")
        by_month = self.read("fees by month", entity="visits", aggregate="sum", measure="fee", group_by="month")
        self.assertEqual(by_month.data, [{"month": "Sep 2026", "total_fee_rupees": 1000.0}, {"month": "Oct 2026", "total_fee_rupees": 1250.5}])
        last = self.read("when did Rakesh last visit", entity="visits", patient_name="Rakesh", order="newest", limit=1)
        self.assertEqual([r["date"] for r in last.data], ["2026-10-05"])
        biggest = self.read("what was the biggest expense", entity="expenses", order="highest", limit=1)
        self.assertEqual(biggest.data[0]["description"], "Clinic rent")
        by_category = self.read("expenses by category", entity="expenses", aggregate="sum", measure="amount", group_by="description")
        self.assertEqual(by_category.data[0], {"description": "clinic rent", "total_amount_rupees": 50000.0})

    def test_logs(self):
        self.conn.execute("INSERT INTO audit_log (logged_at, intent, entity_type, entity_id, payload_json) "
                          "VALUES ('2026-10-07 12:00:00', 'record_visit', 'visit', 1, '{\"visit_date\": \"2026-10-07\", \"fee_paise\": 50000, \"notes\": \"SECRET\"}')")
        self.conn.commit()
        approved = self.read("what did I approve today", entity="audit", date="2026-10-07")
        self.assertEqual([(r["action"], r["summary"]) for r in approved.data], [("recorded a visit", "2026-10-07 · fee Rs 500")])
        self.assertNotIn("SECRET", json.dumps(approved.data))

    def test_my_branch_still_defaults_for_appointments_only(self):
        self.book_at(2, "10:00", patient_id=1, day="2026-10-07")
        self.book_at(1, "11:00", patient_id=2, day="2026-10-07")
        appointments = self.read("appointments by doctor", entity="appointments", group_by="doctor", date="2026-10-07")
        self.assertEqual(appointments.scope_caption, "Branch A")                 # My branch is A
        self.assertEqual(sum(r["count"] for r in appointments.data), 1)
        staff = self.read("list the staff", entity="staff")
        self.assertEqual(len(staff.data), 3)                                       # staff are clinic-wide unless a branch is named
        self.assertIsNone(staff.scope_caption)
        at_b = self.read("staff at branch B", entity="staff", branch="B")
        self.assertEqual([r["name"] for r in at_b.data], ["Ravi Kumar", "Sunita Devi"])
        self.assertEqual(at_b.scope_caption, "Branch B")
        everywhere = self.read("appointments by doctor in all branches", entity="appointments", group_by="doctor", branch="all", date="2026-10-07")
        self.assertEqual(sum(r["count"] for r in everywhere.data), 2)

    def test_a_branch_named_in_the_words_scopes_the_new_entities_too(self):
        result = self.read("attendance at branch B today", entity="attendance", date="2026-10-07")
        self.assertEqual({r["staff"] for r in result.data}, {"Sunita Devi", "Ravi Kumar"})

    def test_followups_with_a_branch_are_not_swallowed_by_the_plain_list(self):
        self.conn.execute("INSERT INTO followups (patient_id, due_date, due_time, doctor_id, branch_id, status) VALUES (1, '2026-10-09', '10:00', 1, 1, 'pending')")
        self.conn.execute("INSERT INTO followups (patient_id, due_date, due_time, doctor_id, branch_id, status) VALUES (2, '2026-10-10', '10:00', 2, 2, 'pending')")
        self.conn.commit()
        plain = self.read("which patients missed their follow-up", entity="followups")
        self.assertEqual(plain.intent, "missed_followups")
        at_b = self.read("which follow-ups are at branch B", entity="followups", branch="B")
        self.assertEqual((at_b.intent, [r["patient"] for r in at_b.data]), ("query", ["Mohan Lal"]))

    def test_the_truncation_caption_for_a_long_list_and_for_groups(self):
        self.conn.executemany("INSERT INTO expenses (expense_date, description, amount_paise) VALUES ('2026-10-01', ?, 100)",
                              [("Bulk %03d" % i,) for i in range(210)])
        self.conn.commit()
        long = self.read("list expenses", entity="expenses")
        self.assertEqual(long.scope_caption, "Showing the first 200 of 213")
        groups = self.read("count by description", entity="expenses", aggregate="count", group_by="description")
        self.assertEqual(groups.scope_caption, "Showing the first 200 groups")

    def test_a_value_the_validator_refuses_is_not_run(self):
        self.call("query", entity="patients", aggregate="sum")          # no measure, and none is obvious
        with self.assertRaises(PipelineError):
            self.say("how much money")
        self.assertEqual(self.log_rows()[-1]["route_taken"], "rephrase")

    def test_describe_intent_notation_carries_the_new_arguments_to_the_next_turn(self):
        self.ask("fees by month", entity="visits", aggregate="sum", measure="fee", group_by="month")
        self.assertEqual(self.ctx.last_turn["call"], "query(entity=visits, aggregate=sum, measure=fee, group_by=month)")
        self.assertIn("Total fees collected by month", self.ctx.last_turn["result"])


class MonthRanges(ReadCase):
    def planned(self, text, args, tool="query"):
        run = planner.PlannerRun(self.conn, text, today=FIXED, backend=planner.FakeBackend((tool, args)))
        return run, run.ask()

    def test_this_month_and_last_month_become_a_whole_month_range_whatever_the_model_said(self):
        for text, first, last in (
            ("how much did we collect this month", "2026-10-01", "2026-10-31"),
            ("is mahine ki fees kitni hai", "2026-10-01", "2026-10-31"),
            ("इस महीने कितनी कमाई हुई", "2026-10-01", "2026-10-31"),
            ("what did we spend last month", "2026-09-01", "2026-09-30"),
            ("pichle mahine ka kharch batao", "2026-09-01", "2026-09-30"),
            ("पिछले महीने की फीस", "2026-09-01", "2026-09-30"),
            ("visits next month", "2026-11-01", "2026-11-30"),
        ):
            for given in ({}, {"date": "2026-10-07"}, {"date": "2026-10-01", "date_to": "2026-10-07"}, {"date": "2026-09-30", "date_to": "2026-10-30"}):
                with self.subTest(text=text, given=given):
                    run, planned = self.planned(text, dict({"entity": "visits", "aggregate": "sum", "measure": "fee"}, **given))
                    self.assertEqual((planned.args["date"], planned.args["date_to"]), (first, last))
                    self.assertEqual(planned.slots["date"], first)

    def test_a_correct_model_is_not_second_guessed(self):
        run, planned = self.planned("how much did we collect this month",
                                    {"entity": "visits", "aggregate": "sum", "measure": "fee", "date": "2026-10-01", "date_to": "2026-10-31"})
        self.assertEqual(run.notes, [])

    def test_the_override_is_noted_for_the_log(self):
        run, _ = self.planned("fees this month", {"entity": "visits", "aggregate": "sum", "measure": "fee", "date": "2026-10-07"})
        self.assertTrue(any("month phrase" in note for note in run.notes), run.notes)

    def test_an_entity_with_no_date_filter_is_left_alone(self):
        for entity in ("staff", "doctors"):
            run, planned = self.planned("show the {} this month".format(entity), {"entity": entity})
            self.assertNotIn("date", planned.args)
            self.assertEqual(run.notes, [])
        run, planned = self.planned("branches open this month", {"entity": "branches"})        # a branch is open on one day, not a range
        self.assertNotIn("date_to", planned.args)

    def test_a_sentence_with_its_own_day_in_it_is_not_overridden(self):
        run, planned = self.planned("fees this month from Monday", {"entity": "visits", "aggregate": "sum", "measure": "fee",
                                                                  "date": "2026-10-05", "date_to": "2026-10-31"})
        self.assertEqual(planned.args["date_to"], "2026-10-31")

    def test_two_different_month_phrases_are_ambiguous_and_left_to_the_model(self):
        self.assertIsNone(date_guard.month_phrase("fees this month and last month"))

    def test_month_arithmetic(self):
        for offset, today, expected in (
            (0, date(2026, 10, 7), ("2026-10-01", "2026-10-31")), (-1, date(2026, 1, 15), ("2025-12-01", "2025-12-31")),
            (1, date(2026, 12, 3), ("2027-01-01", "2027-01-31")), (0, date(2028, 2, 29), ("2028-02-01", "2028-02-29")),
            (0, date(2027, 2, 1), ("2027-02-01", "2027-02-28")), (-1, date(2026, 3, 31), ("2026-02-01", "2026-02-28")),
            (1, date(2026, 1, 31), ("2026-02-01", "2026-02-28")), (0, date(2026, 11, 30), ("2026-11-01", "2026-11-30")),
        ):
            with self.subTest(offset=offset, today=today):
                self.assertEqual(date_guard.month_range(offset, today), expected)

    def test_the_phrases(self):
        for text, offset in (
            ("how much this month", 0), ("This Month's total", 0), ("is mahine", 0), ("iss mahine ka hisaab", 0), ("is maheene", 0),
            ("इस महीने", 0), ("current month", 0), ("last month", -1), ("Last month's visits", -1), ("previous month", -1),
            ("pichle mahine", -1), ("pichhle mahine", -1), ("pichla mahina", -1), ("पिछले महीने", -1), ("next month", 1),
            ("agle mahine", 1), ("अगले महीने", 1), ("aane wale mahine", 1),
        ):
            with self.subTest(text=text):
                self.assertEqual(date_guard.month_phrase(text), offset)
        for text in ("this week", "how many patients", "monthly report", "the month of October", "kal", "", None):
            with self.subTest(text=text):
                self.assertIsNone(date_guard.month_phrase(text))

    def test_other_tools_are_untouched(self):
        args, notes = date_guard.crosscheck("close_branch", {"branch": "A", "start_date": "2026-10-08"}, "close branch A this month", FIXED)
        self.assertEqual((args["start_date"], notes), ("2026-10-08", []))

    def test_the_prompt_tells_the_model_the_month_ranges_next_to_the_calendar_line(self):
        system = planner.build_system_prompt(self.conn, date(2026, 10, 6))
        self.assertIn("This month 2026-10-01 to 2026-10-31; last month 2026-09-01 to 2026-09-30.", system)
        self.assertEqual(system.index("Calendar:") < system.index("This month ") < system.index("Branches:"), True)
        january = planner.build_system_prompt(self.conn, date(2026, 1, 15))
        self.assertIn("This month 2026-01-01 to 2026-01-31; last month 2025-12-01 to 2025-12-31.", january)

    def test_end_to_end_a_wrong_month_from_the_model_is_corrected_before_anything_runs(self):
        result = self.ask("how much did we collect this month", entity="visits", aggregate="sum", measure="fee", date="2026-09-01", date_to="2026-09-30")
        self.assertEqual(result.answer_text.split(" Source")[0], "Total fees collected this month: Rs 1,250.50 (3 visits).")
        self.assertEqual(self.last_args()["date"], "2026-10-01")
        result = self.ask("pichle mahine ka kharch", entity="expenses", aggregate="sum", measure="amount")
        self.assertEqual(result.answer_text.split(" Source")[0], "Total expenses last month: Rs 250.50 (1 expense).")


class PromptSize(unittest.TestCase):
    """The planner prompt (the system text plus the tool declarations) is read by a small local model on every
    command, so it must stay small. Baseline before the widened read tool: 1,417 + 9,894 characters."""

    BASELINE_CHARS = 1417 + 9894
    BUDGET_GROWTH_CHARS = 1800        # about 450 tokens

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        add_branches(self.conn)

    def test_the_prompt_has_not_grown_past_its_budget(self):
        system = planner.build_system_prompt(self.conn, date(2026, 10, 6))
        declared = json.dumps(tools.schemas(), ensure_ascii=False)
        self.assertLessEqual(len(system) + len(declared), self.BASELINE_CHARS + self.BUDGET_GROWTH_CHARS,
                             "the planner prompt grew by {} characters".format(len(system) + len(declared) - self.BASELINE_CHARS))

    def test_the_fixed_prefix_is_still_first_and_stable(self):
        a = planner.build_system_prompt(self.conn, date(2026, 10, 6))
        b = planner.build_system_prompt(self.conn, date(2026, 11, 20))
        self.assertTrue(a.startswith(planner._RULES) and b.startswith(planner._RULES))
        self.assertEqual(a.split("Today is")[0], b.split("Today is")[0])
        self.assertNotRegex(planner._RULES, r"\d{4}-\d{2}-\d{2}")

    def test_the_entities_are_described_compactly_one_short_phrase_each(self):
        entity = tools.BY_NAME["query"].params["entity"]
        for name in query_tool.ENTITIES:
            self.assertIn(name, entity.description)
        self.assertLess(len(entity.description), 520)
        self.assertTrue(all(len(p.description) < 150 for key, p in tools.BY_NAME["query"].params.items() if key not in ("entity", "status", "limit")))


if __name__ == "__main__":
    unittest.main()
