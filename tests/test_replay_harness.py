"""The replay harness (scripts/replay): the engine on tiny conversations, the verdict logic, the refusal of live configs
without --live and --yes, the tripwire that keeps every offline run off the network and off a real model, the fixed clock,
the stdlib-only xlsx writer (read back with zipfile + ElementTree), the report, and the seed conversation set."""
import io
import json
import os
import re
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clinic import state_card  # noqa: E402
from clinic.nlu import planner, sarvam, tools  # noqa: E402
from scripts.replay import clock, configs, engine, report, run as replay_run, xlsx_writer  # noqa: E402
from tests.replay_cases import ALL_CASES, MODULES  # noqa: E402
from tests.replay_cases.dsl import NO_CALL, approve, architecture, call, conv, idle, network, pick, say  # noqa: E402
from tests.replay_cases.fixtures import BASE_SETUP, MONDAY, TOMORROW, with_setup  # noqa: E402

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def one(case, config="model_first_scripted", **kw):
    return engine.run_cases([case], configs.get(config), **kw)[0]


# -- the seed set -------------------------------------------------------------------------------------

ALLOWED_EXPECT = {"kind", "intent", "ask_kind", "slots", "options_count", "options_contain", "rows", "patient", "note_contains",
                  "note_lacks", "pending_after", "task_after", "card_after", "remembered_after", "list_after", "route_contains",
                  "card_has", "card_lacks", "no_write", "approved"}
ACTIONS = ("say", "pick", "approve", "idle_minutes", "network", "architecture")
KNOWN_TOOLS = set(tools.BY_NAME) | set(tools.DIALOGUE_BY_NAME) | {"approve_card", "sql_read"}      # sql_read: model-reads mode only
CATEGORIES = {"regression", "dialogue", "entities", "multilingual", "reads", "safety", "architecture", "sql_reads"}
FINAL_DB = {"appointment", "patient", "patients_total", "appointments_total", "no_writes", "no_duplicate_patients"}


class SeedSet(unittest.TestCase):
    def test_it_is_a_real_set(self):
        self.assertGreaterEqual(len(ALL_CASES), 45)
        self.assertGreaterEqual(sum(len(c["turns"]) for c in ALL_CASES), 150)
        self.assertEqual({c["category"] for c in ALL_CASES}, CATEGORIES)
        self.assertEqual(len(ALL_CASES), sum(len(m.CASES) for m in MODULES))

    def test_ids_are_unique_and_every_conversation_is_well_formed(self):
        ids = [c["id"] for c in ALL_CASES]
        self.assertEqual(len(ids), len(set(ids)))
        for case in ALL_CASES:
            with self.subTest(case=case["id"]):
                self.assertRegex(case["id"], r"^[a-z0-9_]+$")
                self.assertTrue(case["title"] and case["turns"])
                self.assertIn(case["language"], ("en", "hinglish", "hi"))
                self.assertTrue(set(case["setup"]) <= {"branches", "patients", "staff", "appointments", "visits", "expenses"})
                keys = {p["key"] for p in case["setup"]["patients"]}
                self.assertTrue(keys, "a conversation without patients")
                for turn in case["turns"]:
                    actions = [a for a in ACTIONS if a in turn]
                    self.assertEqual(len(actions), 1, turn)
                    self.assertLessEqual(set(turn["expect"]), ALLOWED_EXPECT, turn)
                    for name in ("patient", "remembered_after"):
                        value = turn["expect"].get(name)
                        self.assertTrue(value is None or value in keys, (case["id"], name, value))
                    if "say" in turn and "model" in turn and isinstance(turn["model"], tuple):
                        name, args = turn["model"]
                        self.assertIn(name, KNOWN_TOOLS, turn)
                        self.assertIsInstance(args, dict)
                    elif "model" in turn and isinstance(turn["model"], list):          # one golden call per planner call of the turn
                        self.assertTrue(turn["model"])
                        for step in turn["model"]:
                            self.assertIn(step[0], KNOWN_TOOLS, turn)
                            self.assertIsInstance(step[1], dict)
                    elif "model" in turn and isinstance(turn["model"], dict):
                        self.assertEqual(set(turn["model"]), {"prose"}, turn)        # plain words, no tool call
                        self.assertIsInstance(turn["model"]["prose"], str)
                    elif "model" in turn:
                        self.assertIn(turn["model"], (None, "timeout"))
                for check in case["final_db"]:
                    self.assertEqual(len(check), 1)
                    self.assertIn(next(iter(check)), FINAL_DB)
                if case["configs"]:
                    self.assertTrue(set(case["configs"]) <= set(configs.CONFIGS))
                appointment_patients = {a.get("patient") for a in case["setup"]["appointments"]} - {None}
                self.assertTrue(appointment_patients <= keys)

    def test_the_agreed_regressions_are_all_there(self):
        ids = {c["id"] for c in ALL_CASES}
        for needle in ("manju_move", "priya_her_mobile", "dotted_time_11_pm", "phone_in_words_english", "phone_in_words_hinglish",
                       "phone_in_devanagari", "nalin_new_patient", "two_rahuls_choose_by_phone", "two_rahuls_choose_by_position",
                       "two_amits_cancel", "read_interrupts", "card_correction", "branch_change_note", "name_beats_remembered",
                       "book_him", "cancel_the_second_one", "memory_timeout", "network_down", "reschedule_with_no_appointment",
                       "consultation_is_not_record_visit", "shift_karo", "patient_count", "branch_switch", "close_branch",
                       "approve_by_voice", "prompt_injection", "flip_back_and_forth"):
            self.assertTrue(any(needle in i for i in ids), needle)

    def test_the_two_rahuls_priyas_amits_manju_and_nalin_are_in_the_clinic(self):
        by_key = {p["key"]: p for p in BASE_SETUP["patients"]}
        self.assertEqual((by_key["rahul_dev"]["name"], by_key["rahul_dev"]["phone"]), ("राहुल शर्मा", "9876543210"))
        self.assertEqual((by_key["rahul_en"]["name"], by_key["rahul_en"]["phone"]), ("Rahul Sharma", "9876546543"))
        self.assertEqual((by_key["priya_shah"]["name"], by_key["priya_shah"]["phone"]), ("Priya Shah", "9123499999"))
        self.assertEqual((by_key["priya_dev"]["name"], by_key["priya_dev"]["phone"]), ("प्रिया शर्मा", "9123456780"))
        self.assertTrue({"amit_dua", "amit_anand", "manju", "nalin"} <= set(by_key))
        self.assertEqual({b["code"] for b in BASE_SETUP["branches"]}, {"B", "C"})                  # A comes from the schema (Dr. Mehta)

    def test_every_phone_number_is_made_up(self):
        for person in BASE_SETUP["patients"]:
            self.assertRegex(person["phone"], r"^\d{10}$")


# -- the engine ------------------------------------------------------------------------------------

class Engine(unittest.TestCase):
    def test_a_passing_conversation(self):
        result = one(conv("t_pass", "tiny", "regression", "en", [
            say("book Rakesh Verma tomorrow at 5 pm", call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW, time="17:00"),
                kind="card", intent="book_appointment", patient="rakesh", slots={"start_time": "17:00", "appt_date": TOMORROW}),
            approve(approved=True),
        ], final_db=[{"appointment": {"patient": "rakesh", "date": TOMORROW, "time": "17:00"}}]))
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual([t["verdict"] for t in result["turns"]], ["PASS", "PASS"])
        first = result["turns"][0]
        self.assertEqual((first["tool"], first["route"]), ("book_appointment", "planner"))
        self.assertTrue(first["state_card"].startswith("STATE CARD"))
        self.assertEqual(first["result"]["kind"], "card")
        self.assertEqual(first["checks"]["slots_ok"]["ok"], True)
        self.assertIsNone(first["checks"]["state_ok"]["ok"])                    # no state expectation made
        self.assertEqual(result["final_db"], [{"check": result["final_db"][0]["check"], "ok": True, "why": ""}])

    def test_each_check_fails_separately_and_says_why_in_words(self):
        result = one(conv("t_fail", "tiny", "regression", "en", [
            say("book Rakesh Verma tomorrow at 5 pm", call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW, time="17:00"),
                kind="read", intent="query", ask_kind="date", slots={"start_time": "18:00"}, patient="sunita",
                note_contains="nonsense", pending_after="time", task_after="x", card_after=None, remembered_after="rakesh"),
        ]))
        turn = result["turns"][0]
        self.assertEqual(turn["verdict"], "FAIL")
        failed = {name for name, c in turn["checks"].items() if c["ok"] is False}
        self.assertEqual(failed, {"kind_ok", "intent_ok", "ask_kind_ok", "slots_ok", "patient_ok", "note_ok", "state_ok"})
        self.assertIn("start_time is '17:00', expected '18:00'", turn["checks"]["slots_ok"]["why"])
        self.assertIn("expected a read but the app gave a card", turn["checks"]["kind_ok"]["why"])
        self.assertIn("kind_ok:", turn["why"])
        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(result["first_fail"], 1)

    def test_a_known_gap_is_reported_as_one_and_a_pass_stays_a_pass(self):
        failing = [say("book Rakesh Verma tomorrow at 5 pm", call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW, time="17:00"),
                       kind="note")]
        gap = one(conv("t_gap", "tiny", "regression", "en", failing, known_gap="not built yet"))
        self.assertEqual((gap["verdict"], gap["turns"][0]["verdict"]), ("KNOWN GAP", "KNOWN GAP"))
        self.assertEqual(gap["known_gap"], "not built yet")
        passing = [say("book Rakesh Verma tomorrow at 5 pm", call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW, time="17:00"),
                       kind="card")]
        fixed = one(conv("t_fixed", "tiny", "regression", "en", passing, known_gap="not built yet"))
        self.assertEqual(fixed["verdict"], "PASS")

    def test_a_failed_final_database_check_fails_the_conversation(self):
        turns = [say("book Rakesh Verma tomorrow at 5 pm", call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW, time="17:00"),
                     kind="card")]
        checks = [{"appointment": {"patient": "rakesh", "date": TOMORROW, "time": "17:00"}}]            # never approved: not booked
        bad = one(conv("t_db", "tiny", "regression", "en", turns, final_db=checks))
        self.assertEqual((bad["turns"][0]["verdict"], bad["verdict"]), ("PASS", "FAIL"))
        self.assertFalse(bad["final_db"][0]["ok"])
        self.assertIn("0 matching appointment(s), expected 1", bad["final_db"][0]["why"])
        gap = one(conv("t_db_gap", "tiny", "regression", "en", turns, final_db=checks, known_gap="x"))
        self.assertEqual(gap["verdict"], "KNOWN GAP")

    def test_a_conversation_only_meaningful_elsewhere_is_skipped(self):
        case = conv("t_skip", "tiny", "safety", "en", [say("book Rakesh Verma tomorrow at 5 pm", kind="card")],
                    configs=["model_first_scripted"])
        result = one(case, "classic_scripted")
        self.assertEqual(result["verdict"], "SKIPPED")
        self.assertEqual(result["turns"][0]["verdict"], "SKIPPED")
        self.assertIn("only meaningful under model_first_scripted", result["skip_reason"])
        self.assertNotEqual(one(case)["verdict"], "SKIPPED")                   # it runs where it applies

    def test_nothing_is_written_before_approve_and_a_write_is_caught(self):
        result = one(conv("t_nowrite", "tiny", "safety", "en", [
            say("book Rakesh Verma tomorrow at 5 pm", call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW, time="17:00"), kind="card"),
        ], final_db=[{"no_writes": True}, {"appointments_total": 5}, {"patients_total": 12}]))
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["turns"][0]["checks"]["write_ok"], {"ok": True, "why": ""})
        before = {name: (None, "") for name in engine.CHECKS}
        wrote = engine.evaluate({}, engine.summarize(Note("x"), None, {}), {"pending": None, "task": None, "card": None, "remembered": None,
                                                                        "list_rows": 0}, True, False, False, None, {})
        self.assertEqual(wrote["write_ok"][0], False)
        self.assertIn("before anyone pressed Approve", wrote["write_ok"][1])
        allowed = engine.evaluate({"no_write": False}, engine.summarize(Note("x"), None, {}), {"pending": None, "task": None, "card": None,
                                                                                            "remembered": None, "list_rows": 0},
                                  True, False, False, None, {})
        self.assertIsNone(allowed["write_ok"][0])
        self.assertEqual(set(before), set(engine.CHECKS))

    def test_approve_goes_through_propose_and_confirm_and_records_the_outcome(self):
        result = one(conv("t_app", "tiny", "dialogue", "en", [
            say("cancel Sunita Devi's appointment", call("cancel_appointment", patient_name="Sunita Devi"), kind="card", patient="sunita"),
            approve(approved=True),
            approve(approved=True),                                              # a second Approve: there is no card any more
        ], final_db=[{"appointment": {"patient": "sunita", "status": "cancelled"}}]))
        self.assertEqual([t["result"]["kind"] for t in result["turns"]], ["card", "approved", "approve_failed"])
        self.assertEqual([t["verdict"] for t in result["turns"]], ["PASS", "PASS", "FAIL"])
        self.assertIn("no card on screen", result["turns"][2]["result"]["note"])

    def test_idle_expires_the_memory_and_the_network_switch_makes_the_planner_fail(self):
        result = one(conv("t_idle", "tiny", "dialogue", "en", [
            say("show me the details of patient Rakesh Verma", call("query", entity="patients", aggregate="list", patient_name="Rakesh Verma"),
                remembered_after="rakesh"),
            idle(11),
            say("who is booked tomorrow", call("query", entity="appointments", aggregate="list", date=TOMORROW), remembered_after=None),
            network("down"),
            say("how many patients are registered", call("query", entity="patients", aggregate="count"), kind="read",
                route_contains="fallback"),
        ]))
        self.assertEqual(result["verdict"], "PASS", [t["why"] for t in result["turns"]])
        self.assertEqual(result["turns"][4]["route"], "fallback to classic (planner gave nothing usable)")

    def test_a_tap_and_the_settings_switch_are_turns(self):
        result = one(conv("t_tap", "tiny", "dialogue", "en", [
            say("book Mohan tomorrow at 5 pm", call("book_appointment", patient_name="Mohan", date=TOMORROW, time="17:00"),
                kind="ask", ask_kind="choose_patient"),
            pick(1, kind="card", patient="mohan_das"),
            architecture("classic"),
            say("how many patients are registered", NO_CALL, kind="read", route_contains="rules"),
        ]))
        self.assertEqual(result["verdict"], "PASS", [t["why"] for t in result["turns"]])
        self.assertEqual(result["turns"][1]["action"], "tap")
        self.assertEqual(result["turns"][2]["action"], "switch")

    def test_the_planner_log_supplies_tool_route_and_state_card_and_classic_has_no_card(self):
        case = conv("t_log", "tiny", "dialogue", "en", [
            say("move Manju's appointment", call("reschedule_appointment", patient_name="Manju"), kind="ask")])
        first = one(case)["turns"][0]
        self.assertEqual((first["tool"], first["route"], first["route_detail"]), ("reschedule_appointment", "planner", "mf:reschedule_appointment"))
        classic = one(case, "classic_scripted")["turns"][0]
        self.assertEqual((classic["route_detail"], classic["state_card"]), ("rule:move", None))
        self.assertEqual(classic["route"], "rules (rule:move)")
        rules_only = one(case, "classic_rules_only")["turns"][0]
        self.assertEqual(rules_only["route"], "rules (planner off)")

    def test_golden_dialogue_tools_are_not_offered_to_the_classic_planner(self):
        turn = say("the second one", call("choose_option", index=2))
        self.assertIsNone(engine._golden_for(configs.get("classic_scripted"), turn))
        self.assertEqual(engine._golden_for(configs.get("model_first_scripted"), turn), ("choose_option", {"index": 2}))
        turn = say("x", call("choose_option", index=2), classic_model=call("clarify", question="Which?"))
        self.assertEqual(engine._golden_for(configs.get("classic_scripted"), turn), ("clarify", {"question": "Which?"}))

    def test_the_state_card_is_checked_for_ids_and_numbers(self):
        card = "STATE CARD\nWaiting: Rahul 9876543210 and patient_id 7"
        out = engine.evaluate({}, engine.summarize(Note("x"), None, {}), {"pending": None, "task": None, "card": None, "remembered": None,
                                                                        "list_rows": 0}, False, True, True, card, {})
        self.assertEqual(out["card_ok"][0], False)
        self.assertIn("9876543210", out["card_ok"][1])
        fine = engine.evaluate({"card_has": ["Waiting"], "card_lacks": ["Priya"]}, engine.summarize(Note("x"), None, {}),
                               {"pending": None, "task": None, "card": None, "remembered": None, "list_rows": 0}, False, True, True,
                               "STATE CARD\nWaiting for the user", {})
        self.assertEqual(fine["card_ok"][0], True)

    def test_the_state_card_flags_are_honoured_by_the_scripted_run(self):
        case = conv("t_ablate", "tiny", "dialogue", "en", [
            say("move Manju's appointment", call("reschedule_appointment", patient_name="Manju"), kind="ask", ask_kind="date"),
            say("Monday", call("answer_slot", slot="date", value=MONDAY), kind="ask", ask_kind="time"),
        ])
        full = one(case)["turns"][1]["state_card"]
        self.assertIn("Waiting for the user's answer", full)
        self.assertIn("Task in progress: reschedule_appointment", full)
        dropped = one(case, drop=frozenset({"pending"}))["turns"][1]
        self.assertNotIn("Waiting for the user's answer", dropped["state_card"])
        self.assertIn("Task in progress", dropped["state_card"])
        both = one(case, drop=frozenset({"pending", "task", "last_turns"}))["turns"][1]["state_card"]
        self.assertNotIn("Task in progress", both)
        self.assertNotIn("Recent turns", both)
        self.assertIsNone(state_card.dropped.get() or None)                       # the switch is put back after the run

    def test_the_full_seed_set_runs_offline_and_model_first_scripted_has_no_failure(self):
        results = {}
        for name in configs.OFFLINE:
            results[name] = engine.run_cases(ALL_CASES, configs.get(name))
            self.assertEqual(len(results[name]), len(ALL_CASES))
        verdicts = {name: [r["verdict"] for r in runs] for name, runs in results.items()}
        self.assertEqual([r["id"] for r in results["model_first_scripted"] if r["verdict"] == "FAIL"], [])
        self.assertTrue(any(v == "KNOWN GAP" for v in verdicts["model_first_scripted"]))
        self.assertGreater(verdicts["classic_rules_only"].count("FAIL"), verdicts["classic_scripted"].count("FAIL"))
        self.assertGreater(verdicts["classic_scripted"].count("FAIL"), 0)
        self.assertEqual(verdicts["classic_scripted"].count("SKIPPED"), verdicts["classic_rules_only"].count("SKIPPED"))
        for runs in results.values():
            for conv_result in runs:
                for turn in conv_result["turns"]:
                    self.assertIn(turn["verdict"], ("PASS", "FAIL", "KNOWN GAP", "SKIPPED"))
                    self.assertNotRegex(turn["state_card"] or "", r"\d{5,}")

    def test_the_nalin_and_the_rahul_conversations_pass_model_first_and_fail_classic(self):
        wanted = [c for c in ALL_CASES if c["id"] in ("nalin_new_patient_after_which_one", "two_rahuls_choose_by_phone_suffix",
                                                       "read_interrupts_open_booking")]
        self.assertEqual(len(wanted), 3)
        for case in wanted:
            self.assertEqual(one(case)["verdict"], "PASS", case["id"])
            self.assertEqual(one(case, "classic_scripted")["verdict"], "FAIL", case["id"])


from clinic.voice_context import Note  # noqa: E402  (used above)


# -- live configs are refused, and nothing offline can reach the outside ----------------------------------------

class LiveIsRefused(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"SARVAM_API_KEY": "sk-test-key-not-real"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_check_live_needs_the_flag_the_confirmation_and_a_key(self):
        for names in (["classic_live"], ["model_first_live"], ["classic_scripted", "model_first_live"]):
            with self.assertRaises(configs.ConfigError):
                configs.check_live(names)
            with self.assertRaises(configs.ConfigError):
                configs.check_live(names, live=True)
            with self.assertRaises(configs.ConfigError):
                configs.check_live(names, yes=True)
        with self.assertRaises(configs.ConfigError) as raised:
            configs.check_live(["classic_live"], live=True, yes=True, env={})
        self.assertIn("SARVAM_API_KEY", str(raised.exception))
        configs.check_live(["classic_live"], live=True, yes=True)                         # everything given: allowed
        configs.check_live(configs.OFFLINE)                                               # offline configs never need any of it
        with self.assertRaises(configs.ConfigError):
            configs.get("nonsense")

    def test_the_engine_will_not_run_a_live_config_unless_told(self):
        with self.assertRaises(engine.LiveNotAllowed):
            engine.run_cases(ALL_CASES[:1], configs.get("model_first_live"))
        with self.assertRaises(engine.LiveNotAllowed):
            engine.run_conversation(ALL_CASES[0], configs.get("classic_live"))

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("scripts.replay.engine.run_cases", side_effect=AssertionError("a run started")):
            code = replay_run.run(list(argv), out=out)
        return code, out.getvalue()

    def test_the_command_line_refuses_without_the_flags_and_never_starts_a_run(self):
        for argv in (["--config", "classic_live"], ["--config", "model_first_live", "--live"], ["--config", "model_first_live", "--yes"]):
            code, text = self.run_cli(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("Refused", text)
            self.assertNotIn("sk-test-key-not-real", text)

    def test_with_both_flags_but_no_key_it_still_refuses(self):
        with mock.patch.dict(os.environ, {"SARVAM_API_KEY": ""}), mock.patch.object(replay_run, "load_key_from_dotenv"):
            code, text = self.run_cli("--config", "classic_live", "--live", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("SARVAM_API_KEY is not set", text)
        self.assertIn("up to", text)                                                       # the estimate was printed first
        self.assertIn("Rs", text)

    def test_the_estimate_counts_spoken_turns_and_ablated_runs(self):
        cases = ALL_CASES
        spoken = sum(1 for c in cases for t in c["turns"] if "say" in t)
        self.assertEqual(configs.planner_calls_estimate(cases, ["classic_live"]), spoken)
        self.assertEqual(configs.planner_calls_estimate(cases, ["classic_live", "model_first_live"]), 2 * spoken)
        self.assertEqual(configs.planner_calls_estimate(cases, ["model_first_live"], ["pending", "card"]), 3 * spoken)
        self.assertEqual(configs.planner_calls_estimate(cases, list(configs.OFFLINE)), 0)
        self.assertEqual(configs.cost_estimate_rupees(100), 11.0)
        self.assertLessEqual(1.0 / configs.pace_seconds(), 3.0)                            # under three calls a second

    def test_the_key_is_scrubbed_from_anything_printed(self):
        env = {"SARVAM_API_KEY": "sk-secret-123"}
        self.assertNotIn("sk-secret-123", configs.scrub("failed with sk-secret-123 in it", env))
        self.assertNotIn("abc", configs.scrub("api-subscription-key: abc", env))

    def test_unknown_ablation_sections_are_refused(self):
        code, text = self.run_cli("--config", "model_first_scripted", "--ablate", "pending,mystery")
        self.assertEqual(code, 2)
        self.assertIn("mystery", text)


class Tripwire(unittest.TestCase):
    def test_offline_runs_cannot_reach_the_network_or_a_real_backend(self):
        import httpx
        import socket
        with engine.sandbox(configs.get("classic_scripted")):
            for attempt in (lambda: httpx.post("http://localhost:11434/api/chat", json={}),
                            lambda: httpx.get("http://example.invalid/"),
                            lambda: socket.create_connection(("example.invalid", 80)),
                            lambda: planner.OllamaBackend().plan("s", "u", []),
                            lambda: sarvam.SarvamBackend(api_key="k").plan("s", "u", [])):
                with self.assertRaises(AssertionError) as raised:
                    attempt()
                self.assertIn("network", str(raised.exception))
            self.assertEqual(os.environ["SARVAM_API_KEY"], "")                                # no key in an offline run
            self.assertEqual(os.environ["PLANNER_BACKEND"], "sarvam")                         # and the local model is off
            self.assertEqual(os.environ["INTENT_PLANNER_ENABLED"], "1")
        with engine.sandbox(configs.get("classic_rules_only")):
            self.assertEqual(os.environ["INTENT_PLANNER_ENABLED"], "0")
        self.assertNotEqual(os.environ.get("PLANNER_BACKEND"), "sarvam")                      # restored (tests set 'local')

    def test_a_live_sandbox_leaves_the_network_to_the_caller_but_is_never_entered_by_tests(self):
        with mock.patch.dict(os.environ, {"SARVAM_API_KEY": "kept"}):
            with engine.sandbox(configs.get("model_first_live")):
                self.assertEqual(os.environ["SARVAM_API_KEY"], "kept")
                import httpx
                self.assertFalse(isinstance(httpx.post, mock.Mock))

    def test_the_scripted_backend_is_the_only_planner_an_offline_run_has(self):
        backend = engine.ScriptedBackend()
        backend.golden = ("book_appointment", {"patient_name": "Amit"})
        call_ = backend.plan("s", "u", tools.schemas())
        self.assertEqual((call_.name, call_.args), ("book_appointment", {"patient_name": "Amit"}))
        backend.golden = "timeout"
        with self.assertRaises(sarvam.SarvamError):
            backend.plan("s", "u", tools.schemas())
        backend.golden = None
        self.assertIsNone(backend.plan("s", "u", tools.schemas()))
        backend.down = True
        with self.assertRaises(sarvam.SarvamError):
            backend.plan("s", "u", tools.schemas())

    def test_every_offline_run_ends_with_no_backend_left_installed(self):
        engine.run_cases(ALL_CASES[:2], configs.get("model_first_scripted"))
        self.assertIsNone(planner._backend)


class FixedClock(unittest.TestCase):
    def test_today_is_friday_the_ninth_inside_and_real_outside(self):
        import clinic.pipeline as pipeline
        import clinic.nlu.datetime_extract as extract
        other = clock.REAL_DATE(2031, 3, 4)
        import datetime as dtmod
        with clock.frozen(other, clock.REAL_DATETIME(2031, 3, 4, 9, 0)):
            self.assertEqual(dtmod.date.today(), other)
            self.assertEqual(pipeline.date.today(), other)
            self.assertEqual(pipeline.datetime.now(), clock.REAL_DATETIME(2031, 3, 4, 9, 0))
            self.assertEqual(extract.extract_appt_date("kal"), "2031-03-05")
            from datetime import date as late_import
            self.assertEqual(late_import.today(), other)                             # a function-level import sees it too
            self.assertTrue(isinstance(clock.REAL_DATE(2020, 1, 1), dtmod.date))
            self.assertEqual(dtmod.date.fromisoformat("2026-10-10").isoformat(), "2026-10-10")
        self.assertNotEqual(dtmod.date.today(), other)
        self.assertIs(pipeline.date, clock.REAL_DATE)

    def test_the_replay_day_is_the_fixed_friday_whatever_the_machine_says(self):
        self.assertEqual(clock.FIXED_DAY, date(2026, 10, 9))
        self.assertEqual(clock.FIXED_DAY.strftime("%A"), "Friday")
        with clock.frozen(clock.REAL_DATE(2031, 3, 4), clock.REAL_DATETIME(2031, 3, 4, 9, 0)):
            pass
        case = conv("t_clock", "tiny", "dialogue", "en", [
            say("book Rakesh Verma tomorrow at 5 pm", call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW, time="17:00"),
                kind="card", slots={"appt_date": "2026-10-10"}, card_has=["Friday 2026-10-09"])])
        self.assertEqual(one(case)["verdict"], "PASS")

    def test_no_seed_conversation_depends_on_the_machine_date(self):
        text = "".join((ROOT / "tests" / "replay_cases" / f"{name}.py").read_text() for name in
                       ("regressions", "dialogue", "entities", "multilingual", "reads", "safety", "architecture_switch", "sql_reads", "fixtures"))
        self.assertNotIn("date.today", text)
        self.assertNotIn("datetime.now", text)


# -- the xlsx writer ----------------------------------------------------------------------------------

def open_xlsx(path):
    z = zipfile.ZipFile(path)
    parts = {name: ET.fromstring(z.read(name)) for name in z.namelist()}        # every part must be well-formed XML
    return z, parts


def cell_texts(sheet_root):
    out = {}
    for c in sheet_root.iter("{%s}c" % NS["m"]):
        inline = c.find("m:is/m:t", NS)
        value = c.find("m:v", NS)
        out[c.get("r")] = (inline.text if inline is not None else (value.text if value is not None else None), int(c.get("s", "0")))
    return out


class XlsxWriter(unittest.TestCase):
    def build(self, rows_extra=()):
        book = xlsx_writer.Workbook()
        first = book.add_sheet("Turns", widths=[10, 40, 12], freeze=(1, 1), autofilter=True)
        first.append(["Conversation", "Said", "Verdict"], header=True)
        first.append(["c1", xlsx_writer.Cell("a <b> & \"c\" 'd' > e", style=xlsx_writer.WRAP), xlsx_writer.Cell("PASS", style=xlsx_writer.PASS)])
        first.append(["c2", "नमस्ते डॉक्टर, कल सुबह", xlsx_writer.Cell("FAIL", style=xlsx_writer.FAIL)])
        first.append(["c3", 3.5, xlsx_writer.Cell("KNOWN GAP", style=xlsx_writer.GAP)])
        first.append(["c4", True, xlsx_writer.Cell("SKIPPED", style=xlsx_writer.SKIP)])
        for row in rows_extra:
            first.append(row)
        second = book.add_sheet("Conversations")
        second.append(["Open", xlsx_writer.Cell("c2", style=xlsx_writer.LINK, link=("Turns", 3))])
        return book

    def test_the_package_has_every_required_part_and_all_of_it_is_well_formed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.build().save(str(Path(tmp) / "t.xlsx"))
            z, parts = open_xlsx(path)
            self.assertEqual(set(z.namelist()), {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/_rels/workbook.xml.rels",
                                                 "xl/styles.xml", "xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"})
            self.assertIsNone(z.testzip())
            types = parts["[Content_Types].xml"]
            overrides = {o.get("PartName") for o in types}
            self.assertIn("/xl/workbook.xml", overrides)
            self.assertIn("/xl/worksheets/sheet2.xml", overrides)
            names = [s.get("name") for s in parts["xl/workbook.xml"].iter("{%s}sheet" % NS["m"])]
            self.assertEqual(names, ["Turns", "Conversations"])
            rels = {r.get("Id"): r.get("Target") for r in parts["xl/_rels/workbook.xml.rels"]}
            self.assertEqual(rels["rId1"], "worksheets/sheet1.xml")
            self.assertIn("styles.xml", rels.values())

    def test_header_cells_numbers_booleans_and_text_read_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            z, parts = open_xlsx(self.build().save(str(Path(tmp) / "t.xlsx")))
            cells = cell_texts(parts["xl/worksheets/sheet1.xml"])
            self.assertEqual([cells[c][0] for c in ("A1", "B1", "C1")], ["Conversation", "Said", "Verdict"])
            self.assertEqual(cells["B4"][0], "3.5")
            self.assertEqual(cells["B5"][0], "1")                                    # True stored as a boolean 1
            booleans = [c.get("t") for c in parts["xl/worksheets/sheet1.xml"].iter("{%s}c" % NS["m"]) if c.get("r") == "B5"]
            self.assertEqual(booleans, ["b"])

    def test_text_is_escaped_and_devanagari_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.build().save(str(Path(tmp) / "t.xlsx"))
            raw = zipfile.ZipFile(path).read("xl/worksheets/sheet1.xml").decode("utf-8")
            self.assertIn("a &lt;b&gt; &amp; \"c\" 'd' &gt; e", raw)
            self.assertNotIn("a <b>", raw)
            z, parts = open_xlsx(path)
            cells = cell_texts(parts["xl/worksheets/sheet1.xml"])
            self.assertEqual(cells["B2"][0], "a <b> & \"c\" 'd' > e")
            self.assertEqual(cells["B3"][0], "नमस्ते डॉक्टर, कल सुबह")

    def test_characters_xml_cannot_hold_are_dropped_and_long_text_is_cut(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.build([["x", "bell\x07 and nul\x00 and tab\tok", "y"], ["z", "q" * 40000, "w"]]).save(str(Path(tmp) / "t.xlsx"))
            z, parts = open_xlsx(path)                                                # parsing at all proves the XML is legal
            cells = cell_texts(parts["xl/worksheets/sheet1.xml"])
            self.assertEqual(cells["B6"][0], "bell and nul and tab\tok")
            self.assertLessEqual(len(cells["B7"][0]), xlsx_writer.MAX_CELL_CHARS)
            self.assertTrue(cells["B7"][0].endswith("…"))

    def test_fills_make_pass_green_and_fail_red_and_header_bold_with_a_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            z, parts = open_xlsx(self.build().save(str(Path(tmp) / "t.xlsx")))
            styles = parts["xl/styles.xml"]
            fills = [f.find("m:patternFill/m:fgColor", NS).get("rgb") if f.find("m:patternFill/m:fgColor", NS) is not None else None
                     for f in styles.find("m:fills", NS)]
            fonts = styles.find("m:fonts", NS)
            xfs = list(styles.find("m:cellXfs", NS))
            cells = cell_texts(parts["xl/worksheets/sheet1.xml"])

            def fill_of(ref):
                return fills[int(xfs[cells[ref][1]].get("fillId"))]

            self.assertEqual(fill_of("C2"), "FFC6EFCE")        # PASS: green
            self.assertEqual(fill_of("C3"), "FFFFC7CE")        # FAIL: red
            self.assertEqual(fill_of("C4"), "FFFFEB9C")        # KNOWN GAP: amber
            self.assertEqual(fill_of("C5"), "FFD9D9D9")        # SKIPPED: grey
            self.assertEqual(fill_of("A1"), "FF1F3A5F")        # the header row has a fill...
            header_font = fonts[int(xfs[cells["A1"][1]].get("fontId"))]
            self.assertIsNotNone(header_font.find("m:b", NS))   # ...and is bold
            wrapped = xfs[cells["B2"][1]].find("m:alignment", NS)
            self.assertEqual(wrapped.get("wrapText"), "1")
            self.assertEqual(int(styles.find("m:cellXfs", NS).get("count")), len(xfs))

    def test_freeze_panes_autofilter_and_widths(self):
        with tempfile.TemporaryDirectory() as tmp:
            z, parts = open_xlsx(self.build().save(str(Path(tmp) / "t.xlsx")))
            sheet = parts["xl/worksheets/sheet1.xml"]
            pane = sheet.find("m:sheetViews/m:sheetView/m:pane", NS)
            self.assertEqual((pane.get("xSplit"), pane.get("ySplit"), pane.get("topLeftCell"), pane.get("state")), ("1", "1", "B2", "frozen"))
            self.assertEqual(sheet.find("m:autoFilter", NS).get("ref"), "A1:C5")
            widths = [(c.get("min"), c.get("width")) for c in sheet.find("m:cols", NS)]
            self.assertEqual(widths, [("1", "10"), ("2", "40"), ("3", "12")])
            names = parts["xl/workbook.xml"].findall(".//m:definedName", NS)
            self.assertEqual(names[0].get("name"), "_xlnm._FilterDatabase")
            self.assertEqual(names[0].text, "'Turns'!$A$1:$C$5")
            self.assertIsNone(parts["xl/worksheets/sheet2.xml"].find("m:autoFilter", NS))

    def test_internal_hyperlinks_point_at_a_real_sheet_and_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            z, parts = open_xlsx(self.build().save(str(Path(tmp) / "t.xlsx")))
            links = parts["xl/worksheets/sheet2.xml"].findall(".//m:hyperlink", NS)
            self.assertEqual(len(links), 1)
            self.assertEqual((links[0].get("ref"), links[0].get("location")), ("B1", "'Turns'!A3"))
            sheet, row = re.match(r"'(.+)'!A(\d+)", links[0].get("location")).groups()
            self.assertEqual(sheet, "Turns")
            self.assertLessEqual(int(row), len(parts["xl/worksheets/sheet1.xml"].findall(".//m:row", NS)))
            self.assertNotIn("r:id", ET.tostring(links[0], encoding="unicode"))             # internal: no relationship needed

    def test_helpers_and_refusals(self):
        self.assertEqual([xlsx_writer.column_letter(i) for i in (0, 1, 25, 26, 27, 51, 52, 701, 702)],
                         ["A", "B", "Z", "AA", "AB", "AZ", "BA", "ZZ", "AAA"])
        book = xlsx_writer.Workbook()
        book.add_sheet("One")
        with self.assertRaises(ValueError):
            book.add_sheet("One")
        for bad in ("", "x" * 32, "a/b", "a[b]", "what?"):
            with self.assertRaises(ValueError):
                xlsx_writer.Sheet(bad)
        with self.assertRaises(ValueError):
            xlsx_writer.Workbook().save("never.xlsx")


# -- the report ------------------------------------------------------------------------------------

class Report(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cases = [c for c in ALL_CASES if c["id"] in ("manju_move_one_word_name", "nalin_new_patient_after_which_one",
                                                       "reschedule_with_no_appointment", "approve_refusal_wording")]
        cls.runs = []
        for name in ("classic_rules_only", "classic_scripted", "model_first_scripted"):
            cls.runs.append({"config": name, "label": name, "ablate": None, "live": False,
                             "results": engine.run_cases(cases, configs.get(name))})

    def test_sheets_in_order_and_comparison_only_with_more_than_one_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            xlsx, raw = report.write_report(self.runs, tmp)
            z, parts = open_xlsx(xlsx)
            names = [s.get("name") for s in parts["xl/workbook.xml"].iter("{%s}sheet" % NS["m"])]
            self.assertEqual(names, ["Read me", "Summary", "Conversations", "Turns", "Failures", "Comparison"])
            single = report.build_workbook(self.runs[:1])
            self.assertEqual([s.name for s in single.sheets], ["Read me", "Summary", "Conversations", "Turns", "Failures"])
            self.assertTrue(re.match(r"replay_\d{8}T\d{6}Z\.xlsx$", xlsx.name))
            self.assertEqual(raw.with_suffix("").name, xlsx.with_suffix("").name)
            data = json.loads(raw.read_text(encoding="utf-8"))
            self.assertEqual([r["config"] for r in data["runs"]], ["classic_rules_only", "classic_scripted", "model_first_scripted"])

    def test_read_me_says_what_scripted_runs_do_not_measure(self):
        book = report.build_workbook(self.runs)
        text = " ".join(str(c.value) for cells, _ in book.sheets[0].rows for c in cells)
        self.assertIn("DO NOT MEASURE THE MODEL", text)
        for word in ("intent_ok", "write_ok", "state_ok", "KNOWN GAP", "Privacy", "Rollback"):
            self.assertIn(word, text)
        summary = " ".join(str(c.value) for cells, _ in book.sheets[1].rows for c in cells)
        self.assertIn("DO NOT MEASURE THE MODEL", summary)

    def test_turns_sheet_has_a_row_per_turn_and_a_column_per_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            xlsx, _ = report.write_report(self.runs, tmp)
            z, parts = open_xlsx(xlsx)
            turns = parts["xl/worksheets/sheet4.xml"]
            rows = turns.findall(".//m:row", NS)
            total = sum(len(c["turns"]) for run in self.runs for c in run["results"])
            self.assertEqual(len(rows), total + 1)
            header = [c for c in rows[0]]
            texts = [h.find("m:is/m:t", NS).text for h in header]
            for name in ("Run", "Conversation", "Turn", "Said", "State card sent (model-first)", "Planner tool + args", "Route",
                         "Slots", "Patient resolved", "Expected", "Verdict", "Why it failed", "Planner ms", "Tokens in", "Cost paise",
                         *engine.CHECKS):
                self.assertIn(name, texts)

    def test_each_conversations_link_lands_on_its_own_first_turn_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            xlsx, _ = report.write_report(self.runs, tmp)
            z, parts = open_xlsx(xlsx)
            turns = cell_texts(parts["xl/worksheets/sheet4.xml"])
            conversations = parts["xl/worksheets/sheet3.xml"]
            links = conversations.findall(".//m:hyperlink", NS)
            self.assertEqual(len(links), sum(len(r["results"]) for r in self.runs))
            conv_cells = cell_texts(conversations)
            for link in links:
                row = re.match(r"'Turns'!A(\d+)", link.get("location")).group(1)
                source_row = re.match(r"[A-Z]+(\d+)", link.get("ref")).group(1)
                self.assertEqual(turns["B" + row][0], conv_cells["B" + source_row][0])         # the same conversation id
                self.assertEqual(turns["A" + row][0], conv_cells["A" + source_row][0])         # the same run
                self.assertEqual(turns["C" + row][0], "1")                                      # its first turn

    def test_failures_sheet_lists_only_failures_and_known_gaps_with_the_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            xlsx, _ = report.write_report(self.runs, tmp)
            z, parts = open_xlsx(xlsx)
            cells = cell_texts(parts["xl/worksheets/sheet5.xml"])
            verdicts = [v for (ref, (v, _)) in cells.items() if ref.startswith("D") and ref != "D1"]
            self.assertTrue(verdicts)
            self.assertTrue(set(verdicts) <= {"FAIL", "KNOWN GAP"})
            self.assertTrue(any(v == "KNOWN GAP" for v in verdicts))
            whys = [t for (ref, (t, _)) in cells.items() if ref.startswith("F") and ref != "F1"]
            self.assertTrue(all(whys))

    def test_comparison_marks_where_the_runs_differ(self):
        with tempfile.TemporaryDirectory() as tmp:
            xlsx, _ = report.write_report(self.runs, tmp)
            z, parts = open_xlsx(xlsx)
            cells = cell_texts(parts["xl/worksheets/sheet6.xml"])
            self.assertEqual([cells[c][0] for c in ("D1", "E1", "F1", "G1")], ["classic_rules_only", "classic_scripted", "model_first_scripted", "Differs?"])
            flags = [t for ref, (t, _) in cells.items() if ref.startswith("G") and ref != "G1" and t]
            self.assertIn("YES", flags)

    def test_summary_numbers_add_up(self):
        for run in self.runs:
            stats = report.run_stats(run)
            self.assertEqual(sum(stats["conv"].values()), stats["conversations"])
            self.assertEqual(sum(stats["turn"].values()), stats["turns"])
        text = report.text_summary(self.runs, "x.xlsx")
        for name in ("classic_rules_only", "classic_scripted", "model_first_scripted", "DO NOT MEASURE THE MODEL"):
            self.assertIn(name, text)

    def test_the_ablation_sheet_lists_turns_that_stop_passing(self):
        base = {"config": "model_first_live", "label": "model_first_live", "ablate": None, "live": True,
                "results": [{"id": "c", "title": "t", "category": "regression", "language": "en", "known_gap": None, "config": "model_first_live",
                             "verdict": "PASS", "first_fail": None, "final_db": [], "turns": [
                                 {"conv": "c", "turn": 1, "action": "say", "said": "hello", "verdict": "PASS", "why": "", "checks": {}, "result": {},
                                  "state_card": None, "tool": None, "route": None, "expected": "", "planner_ms": 900, "turn_ms": 1000,
                                  "tokens_in": 3000, "tokens_out": 20, "cost_paise": 11, "memory": {}}]}]}
        worse = json.loads(json.dumps(base))
        worse.update(label="model_first_live -pending", ablate="pending")
        worse["results"][0]["turns"][0].update(verdict="FAIL", why="kind_ok: expected ask")
        worse["results"][0]["verdict"] = "FAIL"
        book = report.build_workbook([base, worse])
        self.assertEqual([s.name for s in book.sheets][-1], "Ablation")
        rows = [[str(c.value) for c in cells] for cells, _ in book.sheets[-1].rows]
        self.assertEqual(rows[1][:3], ["pending", "c", "1"])
        stats = report.run_stats(base)
        self.assertEqual((stats["p50"], stats["p95"], stats["cost_rupees"]), (900, 900, 0.11))


class CommandLine(unittest.TestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        code = replay_run.run(list(argv), out=out)
        return code, out.getvalue()

    def test_list(self):
        code, text = self.run_cli("--list")
        self.assertEqual(code, 0)
        self.assertIn("nalin_new_patient_after_which_one", text)
        self.assertRegex(text, r"\d+ conversation\(s\), \d+ turns")

    def test_filters(self):
        code, text = self.run_cli("--list", "--cases", "nalin", "--category", "regression")
        self.assertEqual(code, 0)
        self.assertEqual(len([l for l in text.splitlines() if "regression" in l]), 2)
        code, text = self.run_cli("--list", "--category", "nonexistent")
        self.assertEqual(code, 2)

    def test_an_offline_run_writes_the_workbook_and_prints_a_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, text = self.run_cli("--config", "model_first_scripted", "classic_scripted", "--cases", "manju_move", "--out", tmp)
            self.assertEqual(code, 0, text)
            files = sorted(p.name for p in Path(tmp).iterdir())
            self.assertEqual([f.rsplit(".", 1)[1] for f in files], ["json", "xlsx"])
            self.assertIn("model_first_scripted", text)
            self.assertIn("Report:", text)
            self.assertIn("DO NOT MEASURE THE MODEL", text)

    def test_the_results_folder_is_git_ignored(self):
        self.assertIn("replay_results/", (ROOT / ".gitignore").read_text().splitlines())


class SameAsTheApp(unittest.TestCase):
    def test_the_deferred_intents_are_the_apps(self):
        with mock.patch("dotenv.load_dotenv"):
            import app as clinic_app
        self.assertEqual(engine.DEFERRED_INTENTS, clinic_app.DEFERRED_INTENTS)


if __name__ == "__main__":
    unittest.main()
