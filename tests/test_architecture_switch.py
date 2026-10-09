"""The switch between the ways a command is understood (clinic/architecture.py): Classic, New (model first) and Model
does all read operations: where it is stored and how it is resolved, the Settings routes and card, the guarantee that
with "classic" nothing of the model-first path runs (the rollback), and that flipping the setting mid-conversation takes
effect on the very next sentence. (The model-reads rollback proofs are in tests/test_model_reads.py.)"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["WHATSAPP_NOTIFY_MODE"] = "dry_run"
os.environ.setdefault("SARVAM_API_KEY", "test-not-real")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.planner_support import PlannerCase  # noqa: E402

from clinic import architecture, db, settings  # noqa: E402
from clinic.pipeline import ParsedResult, ReadResult  # noqa: E402
from clinic.voice_context import AskResult  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def fresh():
    return db.connect(":memory:")


class ModeResolution(unittest.TestCase):
    def setUp(self):
        self.conn = fresh()
        self.addCleanup(self.conn.close)
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(architecture.ENV, None)

    def test_the_default_is_model_reads(self):
        self.assertEqual(architecture.DEFAULT, "model_reads")
        self.assertEqual(architecture.mode(self.conn), "model_reads")
        self.assertEqual(architecture.mode(), "model_reads")
        self.assertTrue(architecture.is_model_first(self.conn))
        self.assertTrue(architecture.reads_by_model(self.conn))
        self.assertEqual(architecture.source(self.conn), "default")

    def test_a_blank_setting_is_not_a_choice_so_the_default_applies(self):
        for blank in ("", "   "):
            with self.subTest(value=repr(blank)):
                settings.set_value(self.conn, architecture.KEY, blank)
                self.assertEqual(architecture.mode(self.conn), architecture.DEFAULT)
                self.assertEqual(architecture.source(self.conn), "default")

    def test_a_saved_choice_is_kept_whatever_the_default(self):
        for chosen in ("classic", "model_first", "model_reads"):
            with self.subTest(mode=chosen):
                architecture.set_mode(self.conn, chosen)
                self.assertEqual(architecture.mode(self.conn), chosen)
                self.assertEqual(architecture.source(self.conn), "setting")

    def test_the_environment_is_the_fallback(self):
        os.environ[architecture.ENV] = "model_first"
        self.assertEqual(architecture.mode(self.conn), "model_first")
        self.assertEqual(architecture.mode(), "model_first")
        self.assertEqual(architecture.source(self.conn), "environment")

    def test_the_setting_beats_the_environment(self):
        os.environ[architecture.ENV] = "model_first"
        settings.set_value(self.conn, architecture.KEY, "classic")
        self.assertEqual(architecture.mode(self.conn), "classic")
        self.assertEqual(architecture.source(self.conn), "setting")
        os.environ[architecture.ENV] = "classic"
        settings.set_value(self.conn, architecture.KEY, "model_first")
        self.assertTrue(architecture.is_model_first(self.conn))

    def test_any_invalid_value_reads_as_classic(self):
        for bad in ("newest", "MODEL-FIRST-PLUS", "1", "true", "model first now"):
            with self.subTest(value=bad):
                settings.set_value(self.conn, architecture.KEY, bad)
                self.assertEqual(architecture.mode(self.conn), "classic")
        os.environ[architecture.ENV] = "bogus"
        self.conn.execute("DELETE FROM app_settings WHERE key = ?", (architecture.KEY,))
        self.assertEqual(architecture.mode(self.conn), "classic")

    def test_case_and_dashes_are_forgiven(self):
        for value in ("Model_First", "model-first", " MODEL_FIRST "):
            settings.set_value(self.conn, architecture.KEY, value)
            self.assertEqual(architecture.mode(self.conn), "model_first", value)

    def test_it_is_read_at_every_call(self):
        modes = []
        for value in ("model_first", "classic", "model_first", "classic"):
            settings.set_value(self.conn, architecture.KEY, value)
            modes.append(architecture.mode(self.conn))
        self.assertEqual(modes, ["model_first", "classic", "model_first", "classic"])

    def test_a_connection_without_the_table_reads_as_never_chosen(self):
        import sqlite3
        bare = sqlite3.connect(":memory:")
        self.addCleanup(bare.close)
        self.assertEqual(architecture.mode(bare), architecture.DEFAULT)

    def test_set_mode_validates(self):
        self.assertEqual(architecture.set_mode(self.conn, "model_first"), "model_first")
        self.assertEqual(settings.get(self.conn, architecture.KEY), "model_first")
        for bad in (None, "", "both", "approve"):
            with self.assertRaises(ValueError):
                architecture.set_mode(self.conn, bad)
        self.assertEqual(settings.get(self.conn, architecture.KEY), "model_first")        # a refused value changes nothing

    def test_the_view_has_all_three_choices_and_the_current_label(self):
        settings.set_value(self.conn, architecture.KEY, "model_first")
        view = architecture.view(self.conn)
        self.assertEqual(view["mode"], "model_first")
        self.assertEqual([o["value"] for o in view["options"]], ["classic", "model_first", "model_reads"])
        self.assertEqual([o["label"] for o in view["options"]],
                         ["Classic (rules first)", "New (model first, experimental)", "Model does all read operations"])
        self.assertEqual(view["label"], "New (model first, experimental)")
        for value, label in (("classic", "Classic (rules first)"), ("model_reads", "Model does all read operations")):
            settings.set_value(self.conn, architecture.KEY, value)
            self.assertEqual(architecture.view(self.conn)["label"], label)

    def test_only_the_third_option_carries_a_help_sentence(self):
        helps = {o["value"]: o.get("help") for o in architecture.view(self.conn)["options"]}
        self.assertIsNone(helps["classic"])
        self.assertIsNone(helps["model_first"])
        self.assertIn("read-only queries", helps["model_reads"])
        self.assertIn("never able to change data", helps["model_reads"])
        self.assertIn("Switching back to Classic or New is instant", helps["model_reads"])

    def test_model_reads_is_model_first_understanding_plus_model_written_reads(self):
        expected = {"classic": (False, False), "model_first": (True, False), "model_reads": (True, True)}
        for value, (first, reads) in expected.items():
            settings.set_value(self.conn, architecture.KEY, value)
            self.assertEqual((architecture.is_model_first(self.conn), architecture.reads_by_model(self.conn)), (first, reads), value)

    def test_model_reads_is_read_from_the_setting_the_environment_and_forgiving_spellings(self):
        for value in ("model_reads", "Model-Reads", " MODEL_READS "):
            settings.set_value(self.conn, architecture.KEY, value)
            self.assertEqual(architecture.mode(self.conn), "model_reads", value)
        self.conn.execute("DELETE FROM app_settings WHERE key = ?", (architecture.KEY,))
        os.environ[architecture.ENV] = "model_reads"
        self.assertTrue(architecture.reads_by_model(self.conn))
        self.assertEqual(architecture.set_mode(self.conn, "model_reads"), "model_reads")

    def test_a_near_miss_of_the_third_value_is_classic(self):
        for bad in ("model_read", "reads", "model reads please", "model_reads_plus"):
            settings.set_value(self.conn, architecture.KEY, bad)
            self.assertEqual(architecture.mode(self.conn), "classic", bad)
            self.assertFalse(architecture.reads_by_model(self.conn))


with mock.patch("dotenv.load_dotenv"):
    import app as clinic_app


class SettingsRoutes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(clinic_app, "DB_PATH", str(Path(self.tmp.name) / "test.db"))        # never the real clinic.db
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(architecture.ENV, None)
        self.client = clinic_app.app.test_client()

    def test_get_shows_model_reads_by_default(self):
        reply = self.client.get("/settings/intent-architecture").get_json()
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["data"]["mode"], "model_reads")
        self.assertEqual(reply["data"]["label"], "Model does all read operations")
        self.assertEqual(reply["data"]["source"], "default")
        self.assertEqual(len(reply["data"]["options"]), 3)

    def test_post_saves_and_get_reads_it_back(self):
        reply = self.client.post("/settings/intent-architecture", json={"mode": "model_first"}).get_json()
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["data"]["mode"], "model_first")
        self.assertEqual(self.client.get("/settings/intent-architecture").get_json()["data"]["mode"], "model_first")
        conn = db.connect(clinic_app.DB_PATH)
        self.addCleanup(conn.close)
        self.assertEqual(settings.get(conn, "intent_architecture"), "model_first")
        back = self.client.post("/settings/intent-architecture", json={"mode": "classic"}).get_json()
        self.assertEqual(back["data"]["mode"], "classic")

    def test_the_third_option_saves_and_the_current_line_follows(self):
        reply = self.client.post("/settings/intent-architecture", json={"mode": "model_reads"}).get_json()
        self.assertTrue(reply["ok"])
        self.assertEqual((reply["data"]["mode"], reply["data"]["label"]), ("model_reads", "Model does all read operations"))
        self.assertEqual(self.client.get("/settings/intent-architecture").get_json()["data"]["mode"], "model_reads")
        conn = db.connect(clinic_app.DB_PATH)
        self.addCleanup(conn.close)
        self.assertTrue(architecture.reads_by_model(conn))               # read at the next command, no restart
        self.client.post("/settings/intent-architecture", json={"mode": "model_first"})
        self.assertFalse(architecture.reads_by_model(conn))
        self.assertTrue(architecture.is_model_first(conn))
        self.client.post("/settings/intent-architecture", json={"mode": "classic"})
        self.assertFalse(architecture.is_model_first(conn))

    def test_a_bad_value_is_refused_and_changes_nothing(self):
        self.client.post("/settings/intent-architecture", json={"mode": "model_first"})
        for body in ({"mode": "approve"}, {"mode": ""}, {}, {"mode": None}):
            response = self.client.post("/settings/intent-architecture", json=body)
            self.assertEqual(response.status_code, 400, body)
            self.assertFalse(response.get_json()["ok"])
        self.assertEqual(self.client.get("/settings/intent-architecture").get_json()["data"]["mode"], "model_first")

    def test_the_voice_session_reads_the_saved_mode_for_the_next_command(self):
        conn = db.connect(clinic_app.DB_PATH)
        self.addCleanup(conn.close)
        self.assertTrue(architecture.is_model_first(conn))           # nothing saved yet: the default
        self.client.post("/settings/intent-architecture", json={"mode": "classic"})
        self.assertFalse(architecture.is_model_first(conn))
        self.client.post("/settings/intent-architecture", json={"mode": "model_first"})
        self.assertTrue(architecture.is_model_first(conn))           # the same open connection sees it at once
        self.client.post("/settings/intent-architecture", json={"mode": "classic"})
        self.assertFalse(architecture.is_model_first(conn))


class UiWiring(unittest.TestCase):
    """The Settings card, read from the files (the behaviour is the route tests and tests/settings_architecture.test.js)."""

    def setUp(self):
        self.html = (ROOT / "templates" / "dashboard.html").read_text()
        self.js = (ROOT / "static" / "settings_architecture.js").read_text()

    def test_the_dashboard_hosts_and_loads_the_card(self):
        self.assertIn('<div id="settings-architecture"></div>', self.html)
        self.assertIn("settings_architecture.js", self.html)
        self.assertLess(self.html.index('id="settings-architecture"'), self.html.index('data-tab="profile"'))   # inside Settings

    def test_the_card_has_the_agreed_wording(self):
        for text in ("Command understanding", "Classic (rules first)", "Current mode: "):
            self.assertTrue(text in self.js or text in (ROOT / "clinic" / "architecture.py").read_text(), text)
        self.assertIn("New (model first, experimental)", (ROOT / "clinic" / "architecture.py").read_text())
        self.assertIn("Model does all read operations", (ROOT / "clinic" / "architecture.py").read_text())
        self.assertIn("switching back to Classic is instant", self.js)

    def test_it_uses_the_two_routes_the_save_tick_and_only_text(self):
        self.assertIn('"/settings/intent-architecture"', self.js)
        self.assertIn('method: "POST"', self.js)
        self.assertIn("SaveTick.show(save)", self.js)
        self.assertNotIn("innerHTML", self.js)
        self.assertIn('type = "radio"', self.js)
        self.assertEqual(self.js.count('type = "radio"'), 1)          # one radio per option, from the server's list
        self.assertIn("optionHelp(option)", self.js)                   # the third option's sentence, as text
        self.assertIn('"muted architecture-help"', self.js)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_node_tests_pass(self):
        for name in ("settings_architecture.test.js",):
            out = subprocess.run(["node", str(ROOT / "tests" / name)], capture_output=True, text=True, cwd=str(ROOT), timeout=60)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_script_is_valid_javascript(self):
        out = subprocess.run(["node", "--check", str(ROOT / "static" / "settings_architecture.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)


CLASSIC_PROBE = r"""
import json, sys
sys.path.insert(0, ".")
import tests                              # the usual offline switches
from clinic import db
from clinic.adapters.local_sqlite import LocalSQLiteAdapter
from clinic.voice_context import VoiceContext
from clinic.voice_turns import handle_turn
conn = db.connect(":memory:")
conn.execute("INSERT INTO patients (name, phone, age) VALUES ('Rakesh Verma', '9000000001', 40)")
conn.commit()
adapter = LocalSQLiteAdapter()
ctx = VoiceContext()
out = []
for text in ("how many patients are registered", "book Rakesh Verma tomorrow at 5 pm", "make it 6 pm"):
    out.append(type(handle_turn(ctx, conn, text, adapter, adapter, "en-IN", frozenset({"book_appointment"}))).__name__)
print(json.dumps({"results": out, "loaded": sorted(m for m in sys.modules if m in ("clinic.nlu.dialogue", "clinic.state_card"))}))
"""


class ClassicNeverTouchesTheNewPath(PlannerCase):
    """Rollback guarantee: with "classic" none of the model-first code runs, and what classic does is unchanged."""

    def test_a_classic_turn_in_a_fresh_process_never_even_loads_the_model_first_modules(self):
        done = subprocess.run([sys.executable, "-c", CLASSIC_PROBE], capture_output=True, text=True, cwd=str(ROOT), timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        report = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(report["results"], ["ReadResult", "ParsedResult", "CardUpdate"])
        self.assertEqual(report["loaded"], [])

    def test_no_model_first_function_is_called_in_classic(self):
        from clinic import state_card
        from clinic.nlu import dialogue
        boom = AssertionError("a model-first function ran in classic mode")
        with mock.patch.object(dialogue, "route", side_effect=boom), \
                mock.patch.object(dialogue, "ModelFirstRun", side_effect=boom), \
                mock.patch.object(state_card, "build", side_effect=boom):
            self.assertEqual(architecture.mode(self.conn), "classic")
            self.call("query", entity="patients", aggregate="list", fields=["name"])
            self.assertIsInstance(self.say("who all do we have on file"), ReadResult)
            self.call("book_appointment", patient_name="Rakesh Verma", date=self.tomorrow.isoformat(), time="17:00")
            self.assertIsInstance(self.say("book Rakesh Verma tomorrow at 5 pm"), ParsedResult)
            self.say("how many patients are registered")
            self.say("make it 6 pm")
            self.call("book_appointment", patient_name="Mohan", date=self.tomorrow.isoformat(), time="17:00")
            self.assertIsInstance(self.say("book Mohan tomorrow at 5 pm"), AskResult)

    def test_an_explicit_classic_setting_behaves_exactly_like_no_setting(self):
        def run(explicit):
            self.setUp()
            if explicit:
                architecture.set_mode(self.conn, "classic")
            self.call("book_appointment", patient_name="Mohan", date=self.tomorrow.isoformat(), time="17:00")
            first = self.say("book Mohan tomorrow at 5 pm")
            second = self.say("the second one")
            rows = [(r["route_taken"], r["route_detail"], r["planner_tool"], r["state_card"]) for r in self.log_rows()]
            return [type(first).__name__, first.kind, [o["label"] for o in first.options], type(second).__name__], rows

        self.assertEqual(run(False), run(True))

    def test_classic_planner_log_rows_have_no_state_card_and_no_model_first_detail(self):
        self.call("query", entity="patients", aggregate="list", fields=["name"])
        self.say("who all do we have on file")
        rows = self.log_rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["state_card"])
        self.assertFalse((rows[0]["route_detail"] or "").startswith("mf"))

    def test_the_classic_planner_does_not_know_the_conversation_tools(self):
        from clinic.nlu import tools
        self.assertEqual(len(tools.schemas()), 19)
        self.assertEqual(len(tools.schemas(dialogue=True)), 24)
        for name in ("choose_option", "answer_slot", "correct_card", "new_patient", "cancel_task"):
            with self.assertRaises(tools.ToolError):
                tools.validate(name, {"index": 1})               # classic validation: an unknown tool

    def test_a_dialogue_tool_in_classic_is_just_an_invalid_call_and_the_rules_decide(self):
        self.call("choose_option", index=2)
        ask = self.say("book Mohan tomorrow at 5 pm")           # the planner answers with a tool classic does not have
        self.assertIsInstance(ask, AskResult)                    # ...so the keyword rules read the sentence, as before
        self.assertEqual(ask.kind, "choose_patient")
        self.assertEqual(self.log_rows()[0]["route_taken"], "rules")


class InstantRollback(PlannerCase):
    """The setting is read at every command: flip it mid-conversation and the next sentence uses the other path."""

    def test_flipping_the_setting_changes_the_path_on_the_very_next_sentence(self):
        architecture.set_mode(self.conn, "model_first")
        self.backend.script = [("reschedule_appointment", {"patient_name": "Rakesh Verma"}),
                               ("answer_slot", {"slot": "date", "value": self.tomorrow.isoformat()})]
        ask = self.say("move Rakesh Verma's appointment")
        self.assertEqual((ask.kind, len(self.backend.calls)), ("date", 1))                  # model first: the planner read it
        self.assertEqual(self.log_rows()[-1]["route_detail"], "mf:reschedule_appointment")

        architecture.set_mode(self.conn, "classic")                                          # one click in Settings
        asked_time = self.say("tomorrow at five pm")                                          # classic: the rules answer, no planner call
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(asked_time.__class__.__name__, "ParsedResult")
        self.assertEqual(len(self.log_rows()), 1)                                             # and no model-first log row

        architecture.set_mode(self.conn, "model_first")
        self.backend.script = [("query", {"entity": "patients", "aggregate": "count"})]
        self.say("how many patients are registered")
        self.assertEqual(len(self.backend.calls), 2)                                          # model first again: the planner is asked
        self.assertEqual(self.log_rows()[-1]["route_detail"], "mf:query")


if __name__ == "__main__":
    unittest.main()
