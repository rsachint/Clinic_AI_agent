"""The planner's tool registry (clinic/nlu/tools.py): 19 typed tools, one
declaration each; validation from that declaration; and the mapping to the
(intent, slots) pair the rest of the app already understands."""
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_conversation import make_db  # noqa: E402
from tests.test_conversation_branches import add_branches  # noqa: E402

from clinic import branches  # noqa: E402
from clinic.nlu import tools  # noqa: E402
from clinic.nlu.intent_llm import KNOWN_INTENTS  # noqa: E402
from clinic.nlu.parser import parse  # noqa: E402

TODAY = date(2026, 10, 6)
EXPECTED_TOOLS = (
    "book_appointment", "reschedule_appointment", "cancel_appointment", "close_branch", "doctor_leave",
    "register_patient", "register_staff", "record_visit", "set_followup", "cancel_followup", "reschedule_followup",
    "log_expense", "log_attendance", "queue_action", "query", "switch_branch", "open_calendar", "clarify",
    "unsupported",
)


class WithBranches(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        add_branches(self.conn)
        self.ctx = tools.ToolContext(self.conn, TODAY, "")

    def ok(self, name, args, text=""):
        return tools.validate(name, args, tools.ToolContext(self.conn, TODAY, text))

    def bad(self, name, args, code=None):
        with self.assertRaises(tools.ToolError) as caught:
            tools.validate(name, args, self.ctx)
        if code:
            self.assertEqual(caught.exception.code, code)


class Registry(unittest.TestCase):
    def test_there_are_exactly_nineteen_tools(self):
        self.assertEqual(tuple(t.name for t in tools.TOOLS), EXPECTED_TOOLS)
        self.assertEqual(len(tools.schemas()), 19)

    def test_each_schema_is_a_function_with_typed_properties_and_required_inside_them(self):
        for schema in tools.schemas():
            fn = schema["function"]
            self.assertEqual(schema["type"], "function")
            self.assertTrue(fn["description"])
            params = fn["parameters"]
            self.assertEqual(params["type"], "object")
            self.assertTrue(set(params["required"]) <= set(params["properties"]), fn["name"])
            for prop in params["properties"].values():
                self.assertIn(prop["type"], ("string", "integer", "array", "boolean"))

    def test_the_schemas_never_offer_an_id_argument(self):
        for schema in tools.schemas():
            for key in schema["function"]["parameters"]["properties"]:
                self.assertFalse(key.endswith("_id") or key == "id", (schema["function"]["name"], key))

    def test_every_existing_intent_label_is_reachable_from_some_tool(self):
        reachable = set()
        for tool in tools.TOOLS:
            reachable |= tool.intents
        self.assertEqual(set(KNOWN_INTENTS) - {"unclear"} - reachable, set())
        # unclear is what "unsupported" means; the pipeline-level intents come from tools too
        self.assertEqual(tools.BY_NAME["unsupported"].intents, {"unclear"})
        self.assertTrue({"patient_count", "close_branch", "set_my_branch", "query", "clarify"} <= reachable)

    def test_write_tools_are_exactly_the_ones_that_end_in_a_card_or_proposal(self):
        self.assertEqual(tools.WRITE_TOOLS, {
            "book_appointment", "reschedule_appointment", "cancel_appointment", "register_patient", "register_staff",
            "record_visit", "set_followup", "cancel_followup", "reschedule_followup", "log_expense", "log_attendance",
            "queue_action"})
        for name in ("close_branch", "doctor_leave", "query", "switch_branch", "open_calendar", "clarify", "unsupported"):
            self.assertNotIn(name, tools.WRITE_TOOLS)


class Validation(WithBranches):
    def test_unknown_tool_is_rejected(self):
        self.bad("drop_all_tables", {}, "unknown_tool")
        self.bad("", {}, "unknown_tool")

    def test_unknown_argument_rejects_the_whole_call_when_strict(self):
        self.bad("cancel_appointment", {"patient_name": "Amit", "patient_id": 7}, "unknown_arg")
        self.bad("book_appointment", {"patient_name": "A", "date": "2026-10-07", "time": "10:00", "sql": "x"}, "unknown_arg")

    def test_unknown_arguments_are_dropped_when_not_strict(self):
        clean = tools.validate("cancel_appointment", {"patient_name": "Amit", "mood": "happy"}, self.ctx, strict=False)
        self.assertEqual(clean, {"patient_name": "Amit"})

    def test_empty_values_mean_not_given(self):
        clean = self.ok("book_appointment", {"patient_name": "Amit", "date": "2026-10-07", "time": "10:00",
                                              "branch": None, "doctor_name": "", "phone": []})
        self.assertEqual(clean, {"patient_name": "Amit", "date": "2026-10-07", "time": "10:00"})

    def test_missing_required_argument(self):
        # book_appointment lacking its time is no longer an error (the app asks for it; Tool.askable, see
        # tests/test_planner_missing_and_prose.py); a fee, a day count or an amount has no question, so these stay strict
        self.bad("record_visit", {"patient_name": "Amit"}, "missing_required")
        self.bad("set_followup", {"patient_name": "Amit"}, "missing_required")
        self.bad("log_expense", {}, "missing_required")

    def test_wrong_types_and_shapes(self):
        self.bad("record_visit", {"patient_name": "Amit", "fee": "a lot"}, "bad_value")
        self.bad("record_visit", {"patient_name": "Amit", "fee": True}, "bad_value")
        self.bad("record_visit", {"patient_name": "Amit", "fee": 12.5}, "bad_value")
        self.bad("record_visit", {"patient_name": 42, "fee": 100}, "bad_value")
        self.bad("record_visit", {"patient_name": ["Amit"], "fee": 100}, "bad_value")
        self.bad("cancel_appointment", "not an object", "bad_value")
        self.bad("cancel_appointment", ["Amit"], "bad_value")
        self.bad("register_patient", {"name": "12345"}, "bad_value")      # a name has letters
        self.bad("register_patient", {"name": "A" * 200}, "bad_value")

    def test_integers_are_coerced_safely(self):
        self.assertEqual(self.ok("record_visit", {"patient_name": "Amit", "fee": "500"})["fee"], 500)
        self.assertEqual(self.ok("record_visit", {"patient_name": "Amit", "fee": 500.0})["fee"], 500)
        self.assertEqual(self.ok("log_expense", {"amount": "1,200"})["amount"], 1200)
        self.bad("set_followup", {"patient_name": "Amit", "days": 0}, "bad_value")          # below the minimum
        self.bad("set_followup", {"patient_name": "Amit", "days": 100000}, "bad_value")
        self.bad("register_patient", {"name": "Amit", "age": 400}, "bad_value")
        self.bad("log_expense", {"amount": float("nan")}, "bad_value")
        self.bad("log_expense", {"amount": float("inf")}, "bad_value")

    def test_enums(self):
        self.assertEqual(self.ok("log_attendance", {"staff_name": "Seema", "status": "Half Day"})["status"], "half_day")
        self.bad("log_attendance", {"staff_name": "Seema", "status": "sleeping"}, "bad_value")
        self.bad("queue_action", {"action": "explode"}, "bad_value")
        self.bad("open_calendar", {"view": "year"}, "bad_value")

    def test_dates_must_be_real_iso_dates(self):
        self.assertEqual(self.ok("cancel_appointment", {"patient_name": "A1", "date": "2026-10-07"})["date"], "2026-10-07")
        for bad in ("tomorrow", "07/10/2026", "2026-13-40", "2026-10-7x", "2026-02-30", 20261007):
            with self.subTest(date=bad):
                self.bad("cancel_appointment", {"patient_name": "Amit", "date": bad}, "bad_value")
        self.bad("cancel_appointment", {"patient_name": "Amit", "date": "2031-01-01"}, "bad_value")   # implausibly far

    def test_times_must_be_hh_mm_and_are_normalised(self):
        self.assertEqual(self.ok("book_appointment", {"patient_name": "A", "date": "2026-10-07", "time": "9:30"})["time"], "09:30")
        self.assertEqual(self.ok("book_appointment", {"patient_name": "A", "date": "2026-10-07", "time": "16:00:00"})["time"], "16:00")
        for bad in ("4 PM", "25:00", "16:60", "noon", 1600):
            with self.subTest(time=bad):
                self.bad("book_appointment", {"patient_name": "A", "date": "2026-10-07", "time": bad}, "bad_value")

    def test_a_branch_letter_or_name_becomes_the_branch_code(self):
        for spoken in ("B", "b", "Branch B", "branch b", "Branch  B"):
            with self.subTest(spoken=spoken):
                self.assertEqual(self.ok("switch_branch", {"branch": spoken})["branch"], "B")
        self.bad("switch_branch", {"branch": "Z"}, "bad_value")
        self.bad("switch_branch", {"branch": "everything"}, "bad_value")

    def test_all_branches_is_only_allowed_for_a_query(self):
        self.assertEqual(self.ok("query", {"entity": "appointments", "branch": "all"})["branch"], "all")
        self.bad("switch_branch", {"branch": "all"}, "bad_value")

    def test_a_closing_branch_cannot_be_its_own_destination_or_end_before_it_starts(self):
        self.bad("close_branch", {"branch": "B", "start_date": "2026-10-07", "preferred_destination": "B"}, "bad_value")
        self.bad("close_branch", {"branch": "B", "start_date": "2026-10-09", "end_date": "2026-10-07"}, "bad_value")

    def test_doctor_leave_needs_one_matching_doctor(self):
        self.ok("doctor_leave", {"doctor_name": "Rao", "start_date": "2026-10-07"})
        self.bad("doctor_leave", {"doctor_name": "Nobody", "start_date": "2026-10-07"}, "bad_value")

    def test_phone_numbers_are_digits(self):
        self.assertEqual(self.ok("register_patient", {"name": "Amit", "phone": "+91 98765 43210"})["phone"], "9876543210")
        self.bad("register_patient", {"name": "Amit", "phone": "call me"}, "bad_value")

    def test_a_json_string_of_arguments_is_accepted_and_garbage_is_not(self):
        self.assertEqual(tools.validate("cancel_appointment", '{"patient_name": "Amit"}', self.ctx), {"patient_name": "Amit"})
        self.bad("cancel_appointment", "{nope", "bad_value")


class TheModelNeverEmitsAnId(WithBranches):
    def test_an_id_argument_is_rejected_for_every_tool(self):
        for tool in tools.TOOLS:
            for key in ("id", "patient_id", "doctor_id", "branch_id", "staff_id", "appointment_id"):
                with self.subTest(tool=tool.name, key=key):
                    self.bad(tool.name, {key: 3}, "unknown_arg")

    def test_slots_carry_ids_only_where_code_looked_them_up(self):
        intent, slots = tools.to_parse_result("book_appointment", self.ok("book_appointment", {
            "patient_name": "Amit", "date": "2026-10-07", "time": "10:00", "branch": "B"}), self.ctx)
        self.assertEqual(slots["branch_id"], branches.get_branch_by_code(self.conn, "B")["id"])
        self.assertNotIn("patient_id", slots)
        intent, slots = tools.to_parse_result("doctor_leave", self.ok("doctor_leave", {"doctor_name": "Rao", "start_date": "2026-10-07"}), self.ctx)
        self.assertEqual(slots["doctor_id"], self.conn.execute("SELECT id FROM doctors WHERE name = 'Dr. Rao'").fetchone()[0])


class Mapping(WithBranches):
    def mapped(self, name, args, text=""):
        ctx = tools.ToolContext(self.conn, TODAY, text)
        return tools.to_parse_result(name, tools.validate(name, args, ctx), ctx)

    def test_every_tool_maps_to_its_documented_intent(self):
        cases = {
            "book_appointment": ({"patient_name": "A", "date": "2026-10-07", "time": "10:00"}, "book_appointment"),
            "reschedule_appointment": ({"patient_name": "A", "new_date": "2026-10-07"}, "reschedule_appointment"),
            "cancel_appointment": ({"patient_name": "A"}, "cancel_appointment"),
            "close_branch": ({"branch": "B", "start_date": "2026-10-07"}, "close_branch"),
            "doctor_leave": ({"doctor_name": "Rao", "start_date": "2026-10-07"}, "close_branch"),
            "register_patient": ({"name": "Amit"}, "register_patient"),
            "register_staff": ({"name": "Seema", "role": "nurse"}, "register_staff"),
            "record_visit": ({"patient_name": "A", "fee": 300}, "record_visit"),
            "set_followup": ({"patient_name": "A", "days": 7}, "set_followup"),
            "cancel_followup": ({"patient_name": "A"}, "cancel_followup"),
            "reschedule_followup": ({"patient_name": "A", "new_date": "2026-10-09"}, "reschedule_followup"),
            "log_expense": ({"amount": 900}, "log_expense"),
            "log_attendance": ({"staff_name": "Seema", "status": "absent"}, "log_attendance"),
            "queue_action": ({"action": "call_next"}, "queue_call_next"),
            "query": ({"entity": "patients", "aggregate": "count"}, "patient_count"),
            "switch_branch": ({"branch": "C"}, "set_my_branch"),
            "open_calendar": ({"view": "week"}, "open_calendar"),
            "clarify": ({"question": "Which patient?"}, "clarify"),
            "unsupported": ({"reason": "weather"}, "unclear"),
        }
        self.assertEqual(set(cases) | {"query"}, set(tools.BY_NAME))
        for name, (args, intent) in cases.items():
            with self.subTest(tool=name):
                self.assertEqual(self.mapped(name, args)[0], intent)

    def test_queue_actions(self):
        for action, intent in (("check_in", "queue_check_in"), ("call_next", "queue_call_next"),
                               ("done", "queue_mark_done"), ("no_show", "queue_mark_no_show"), ("status", "queue_status")):
            self.assertEqual(self.mapped("queue_action", {"action": action})[0], intent)
        # by token if one was said, else by name -- never both
        self.assertEqual(self.mapped("queue_action", {"action": "check_in", "token": 5, "patient_name": "Amit"}),
                         ("queue_check_in", {"token": 5, "patient_name": None}))

    def test_close_branch_carries_the_range_reason_and_destination(self):
        intent, slots = self.mapped("close_branch", {"branch": "B", "start_date": "2026-10-06", "end_date": "2026-10-12",
                                                    "reason": "doctor ill", "preferred_destination": "C"})
        self.assertEqual(slots, {"branch_id": 2, "appt_date": "2026-10-06", "end_date": "2026-10-12", "reason": "doctor ill",
                                 "destination_branch_id": 3})
        # a one-day closure has no end_date, exactly like voice_closure.parse
        _, one_day = self.mapped("close_branch", {"branch": "B", "start_date": "2026-10-06", "end_date": "2026-10-06"})
        self.assertNotIn("end_date", one_day)

    def test_doctor_leave_goes_through_the_doctor_day_closure_slots(self):
        intent, slots = self.mapped("doctor_leave", {"doctor_name": "Dr. Rao", "start_date": "2026-10-07", "end_date": "2026-10-09"})
        self.assertEqual(intent, "close_branch")
        self.assertEqual(set(slots), {"doctor_id", "appt_date", "end_date", "reason"})
        self.assertEqual(slots["reason"], "Dr. Rao is on leave")

    def test_log_expense_uses_the_transcript_when_no_description_was_given(self):
        text = "kharch bijli 900 rupees"
        self.assertEqual(self.mapped("log_expense", {"amount": 900}, text)[1], {"description": text, "amount_rupees": 900.0})
        self.assertEqual(self.mapped("log_expense", {"amount": 900, "description": "electricity"}, text)[1]["description"], "electricity")

    def test_a_phone_the_model_left_out_is_read_from_the_words(self):
        _, slots = self.mapped("book_appointment", {"patient_name": "Amit", "date": "2026-10-07", "time": "10:00"},
                               "book Amit tomorrow 10 am phone 9876543210")
        self.assertEqual(slots["patient_phone"], "9876543210")


# (sentence, the tool call that means the same, the name the extractor would hear)
def _day(offset):
    return (date.today() + timedelta(days=offset)).isoformat()


EQUIVALENT = [
    ("Book an appointment for Sunita tomorrow 11am, phone 9876543210", "book_appointment",
     {"patient_name": "Sunita", "date": _day(1), "time": "11:00", "phone": "9876543210"}, "Sunita"),
    ("book Rakesh tomorrow at 5 pm", "book_appointment", {"patient_name": "Rakesh", "date": _day(1), "time": "17:00"}, "Rakesh"),
    ("reschedule Mohan's appointment to tomorrow at 5 pm", "reschedule_appointment",
     {"patient_name": "Mohan", "new_date": _day(1), "new_time": "17:00"}, "Mohan"),
    ("Cancel Sunita's appointment", "cancel_appointment", {"patient_name": "Sunita"}, "Sunita"),
    ("naya patient Sunita Devi, 34 years, 9876543210", "register_patient",
     {"name": "Sunita Devi", "phone": "9876543210", "age": 34}, "Sunita Devi"),
    ("register staff Seema", "register_staff", {"name": "Seema"}, "Seema"),
    ("Sunita ji ka consultation 300 rupees", "record_visit", {"patient_name": "Sunita ji", "fee": 300}, "Sunita ji"),
    ("Sunita ko 7 din baad bulao", "set_followup", {"patient_name": "Sunita", "days": 7}, "Sunita"),
    ("Cancel Sunita's follow-up please", "cancel_followup", {"patient_name": "Sunita"}, "Sunita"),
    ("Reschedule Sunita's follow-up to kal", "reschedule_followup", {"patient_name": "Sunita", "new_date": _day(1)}, "Sunita"),
    ("kharch bijli 900 rupees", "log_expense", {"amount": 900}, None),
    ("Seema is absent today", "log_attendance", {"staff_name": "Seema", "status": "absent"}, "Seema"),
    ("Seema aaj half day hai", "log_attendance", {"staff_name": "Seema", "status": "half_day"}, "Seema"),
    ("Token 5 aa gaya", "queue_action", {"action": "check_in", "token": 5}, None),
    ("Sunita aa gayi", "queue_action", {"action": "check_in", "patient_name": "Sunita"}, "Sunita"),
    ("call next patient", "queue_action", {"action": "call_next"}, None),
    ("Token 7 consultation done", "queue_action", {"action": "done", "token": 7}, None),
    ("queue mein kaun hai", "queue_action", {"action": "status"}, None),
    ("show month view", "open_calendar", {"view": "month"}, None),
    ("What slots are free tomorrow?", "query", {"entity": "availability", "date": _day(1)}, None),
    ("What's scheduled today?", "query", {"entity": "appointments", "aggregate": "list", "date": _day(0)}, None),
    ("What's Sunita's next appointment?", "query", {"entity": "appointments", "patient_name": "Sunita", "limit": 1}, "Sunita"),
    ("what is Rakesh Verma's phone number", "query", {"entity": "patients", "patient_name": "Rakesh Verma"}, "Rakesh Verma"),
    ("which patients missed their follow-up", "query", {"entity": "followups"}, None),
    ("Aaj ka hisaab batao", "query", {"entity": "cashbook"}, None),
    ("how many patients are registered", "query", {"entity": "patients", "aggregate": "count"}, None),
]


class MapperMatchesTheRulePath(WithBranches):
    """The point of the registry: for the same command, the slots the tool call maps to
    are exactly what parse() yields from the sentence today, so nothing downstream moves."""

    def rule_path(self, text, heard):
        with patch("clinic.nlu.parser.extract_name", return_value=heard), \
                patch("clinic.nlu.parser.pick_intent", return_value=None):
            return parse(text)

    def test_table(self):
        for text, tool, args, heard in EQUIVALENT:
            with self.subTest(text=text):
                rule_intent, rule_slots = self.rule_path(text, heard)
                ctx = tools.ToolContext(self.conn, date.today(), text)
                intent, slots = tools.to_parse_result(tool, tools.validate(tool, args, ctx), ctx)
                self.assertEqual(intent, rule_intent)
                self.assertEqual(slots, rule_slots)


if __name__ == "__main__":
    unittest.main()
