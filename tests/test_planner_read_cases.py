"""The 62 labelled read questions (tests/planner_eval_cases.py READ_CASES) are well-formed, and the call each one
EXPECTS is accepted by the planner's validator, survives the date safety net unchanged and runs on the read tool.
This does not test the model: scripts/eval_planner.py --cases reads does, live. It guards the case file itself."""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.planner_eval_cases import CASES, READ_CASES  # noqa: E402
from tests.test_conversation import make_db  # noqa: E402
from tests.test_conversation_branches import add_branches  # noqa: E402

from clinic import query_tool  # noqa: E402
from clinic.nlu import planner, tools  # noqa: E402

TODAY = date(2026, 10, 6)


def concrete(args):
    """An expected-args dict as a call: "a|b" -> a, "*" -> some words."""
    return {k: ("some words" if v == "*" else (v.split("|")[0] if isinstance(v, str) else v)) for k, v in args.items()}


class CaseFile(unittest.TestCase):
    def test_there_are_at_least_fifty_and_the_original_set_is_untouched(self):
        self.assertGreaterEqual(len(READ_CASES), 50)
        self.assertEqual(len(CASES), 65)
        self.assertEqual(len({c[0] for c in READ_CASES}), len(READ_CASES))
        self.assertFalse({c[0] for c in READ_CASES} & {c[0] for c in CASES})

    def test_the_shape(self):
        for text, tool, args, history in READ_CASES:
            with self.subTest(text=text):
                self.assertIn(tool, tools.BY_NAME)
                self.assertIsInstance(args, dict)
                self.assertTrue(history is None or len(history) == 3)

    def test_every_new_entity_and_feature_is_covered_in_three_languages(self):
        entities = {args.get("entity") for _, tool, args, _ in READ_CASES if tool == "query"}
        self.assertTrue(set(query_tool.ENTITIES) - {"availability"} <= entities | {"cashbook", "patients", "appointments", "followups"},
                        set(query_tool.ENTITIES) - entities)
        for entity in ("staff", "attendance", "branches", "doctors", "schedules", "visits", "expenses", "reminders", "closures",
                       "blocks", "audit", "activity", "followups", "patients", "appointments", "cashbook"):
            self.assertIn(entity, entities)
        keys = set().union(*(set(args) for _, _, args, _ in READ_CASES))
        for key in ("aggregate", "measure", "group_by", "order", "limit", "text", "doctor", "time", "date_to", "branch", "status"):
            self.assertIn(key, keys)
        self.assertTrue({"sum", "average"} <= {args.get("aggregate") for _, _, args, _ in READ_CASES})
        texts = [c[0] for c in READ_CASES]
        self.assertTrue(any(any("ऀ" <= ch <= "ॿ" for ch in t) for t in texts))                  # Devanagari
        self.assertTrue(any(w in t.lower() for t in texts for w in ("kitna", "kaun", "kab", "mahine")))     # Hinglish
        self.assertGreaterEqual(sum(1 for _, tool, _, _ in READ_CASES if tool == "unsupported"), 5)       # deliberately unanswerable
        self.assertGreaterEqual(sum(1 for c in READ_CASES if c[3]), 2)                                      # follow-up turns


class TheExpectedCallsWork(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        add_branches(self.conn)

    def test_each_expected_call_is_valid_unchanged_by_the_date_safety_net_and_runs(self):
        for text, tool, args, history in READ_CASES:
            with self.subTest(text=text):
                call = concrete(args)
                run = planner.PlannerRun(self.conn, text, today=TODAY, backend=planner.FakeBackend((tool, call)))
                planned = run.ask()
                self.assertIsNotNone(planned, run.error)
                self.assertEqual(planned.tool, tool)
                # the net may FILL a day the call left out ("date None -> ..."); it must not CHANGE one the case expects
                changed = [n for n in run.notes if "->" in n and " None ->" not in n]
                self.assertEqual(changed, [], "the safety net changed an expected date: {}".format(run.notes))
                if tool == "query" and planned.intent == "query":
                    spec = {k: v for k, v in planned.slots.items() if k not in ("branch_id", "all_branches")}
                    query_tool.run(self.conn, spec, today=TODAY.isoformat(), now="11:00")
                elif tool == "unsupported":
                    self.assertEqual(planned.intent, "unclear")
                    self.assertTrue(planned.args.get("wanted") or "wanted" not in args)
                else:
                    self.assertIn(planned.intent, tools.BY_NAME["query"].intents)       # routed to an existing read intent


if __name__ == "__main__":
    unittest.main()
