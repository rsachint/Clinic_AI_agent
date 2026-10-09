"""scripts/export_planner_log.py: the planner log as draft labelled cases, read-only."""
import ast
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clinic import db, planner_log  # noqa: E402
from clinic.nlu import planner  # noqa: E402

SPEC = importlib.util.spec_from_file_location("export_planner_log", ROOT / "scripts" / "export_planner_log.py")
export = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(export)


class Export(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "clinic_test.db")
        conn = db.connect(self.path)
        previous = planner.format_previous({"text": "How many patients are registered?",
                                            "call": "query(entity=patients, aggregate=count)", "result": "12 patients; ok"})
        planner_log.record(conn, "voice", "give me the names as well", previous, "query",
                           {"entity": "patients", "aggregate": "list", "fields": ["name"]}, "planner", "query", 4210, [])
        planner_log.record(conn, "voice", "book Amit Sharma kal 4 baje", None, "book_appointment",
                           {"patient_name": "Amit Sharma", "date": "2026-10-07", "time": "16:00"}, "planner",
                           "book_appointment", 3800, ["date 2026-10-06 -> 2026-10-07 (read from 'kal')"])
        planner_log.record(conn, "voice", "kuch bhi karo O'Brien ka", None, None, None, "label_fallback", "list_appointments", 12001,
                           ["planner error: ReadTimeout: timed out"])
        planner_log.set_outcome(conn, 2, "approved")
        planner_log.set_outcome(conn, 1, "rejected")
        conn.commit()
        conn.close()

    def cases(self, *argv):
        conn = export.open_read_only(self.path)
        self.addCleanup(conn.close)
        return [export.to_case(r) for r in export.fetch(conn, *argv)]

    def test_rows_become_cases_in_the_eval_format(self):
        cases = self.cases()
        self.assertEqual(len(cases), 3)
        text, tool, args, history, note = cases[0]
        self.assertEqual((text, tool, args), ("give me the names as well", "query",
                                              {"entity": "patients", "aggregate": "list", "fields": ["name"]}))
        self.assertEqual(history, ("How many patients are registered?", "query(entity=patients, aggregate=count)", "12 patients; ok"))
        self.assertIn("route=planner", note)
        self.assertIn("outcome=rejected", note)
        self.assertEqual(cases[1][3], None)
        self.assertIn("notes=date 2026-10-06 -> 2026-10-07", cases[1][4])
        self.assertEqual(cases[2][1:3], (None, {}))         # a failed planner call has nothing to propose

    def test_the_python_output_is_a_valid_case_file(self):
        source = export.as_python(self.cases())
        self.assertIn("REVIEW EVERY ONE", source)
        self.assertIn("patient names", source)
        namespace = {}
        exec(compile(source, "cases.py", "exec"), namespace)
        self.assertEqual(len(namespace["CASES"]), 3)
        self.assertEqual(len(namespace["CASES"][0]), 4)           # (text, expected_tool, expected_args, history)
        self.assertEqual(namespace["CASES"][2][0], "kuch bhi karo O'Brien ka")
        ast.parse(source)

    def test_json_output(self):
        data = json.loads(export.as_json(self.cases()))
        self.assertEqual([d["text"] for d in data], ["give me the names as well", "book Amit Sharma kal 4 baje", "kuch bhi karo O'Brien ka"])
        self.assertEqual(data[0]["history"][0], "How many patients are registered?")

    def test_filters(self):
        self.assertEqual([c[0] for c in self.cases(None, "label_fallback")], ["kuch bhi karo O'Brien ka"])
        self.assertEqual([c[0] for c in self.cases(None, None, "approved")], ["book Amit Sharma kal 4 baje"])
        problems = [c[0] for c in self.cases(None, None, None, True)]
        self.assertEqual(problems, ["give me the names as well", "book Amit Sharma kal 4 baje", "kuch bhi karo O'Brien ka"])
        self.assertEqual(len(self.cases(None, None, None, False, 1)), 1)
        self.assertEqual(self.cases("2999-01-01"), [])

    def test_the_previous_turn_round_trips_even_with_quotes(self):
        for said in ("it's fine", 'say "hi"', "plain"):
            line = planner.format_previous({"text": said, "call": "c(a=1)", "result": "r"})
            self.assertEqual(export.parse_previous(line), (said, "c(a=1)", "r"))
        self.assertIsNone(export.parse_previous(None))
        self.assertIsNone(export.parse_previous("nonsense"))

    def test_the_command_line_writes_cases_to_stdout_and_never_changes_the_database(self):
        before = Path(self.path).read_bytes()
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "export_planner_log.py"), "--db", self.path],
                             capture_output=True, text=True, cwd=str(ROOT), timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("CASES = [", out.stdout)
        self.assertIn("3 case(s) exported", out.stderr)
        self.assertEqual(Path(self.path).read_bytes()[:100], before[:100])

    def test_a_database_without_the_log_is_reported_not_created(self):
        old = str(Path(self.tmp.name) / "old.db")
        conn = sqlite3.connect(old)
        conn.execute("CREATE TABLE patients (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "export_planner_log.py"), "--db", old],
                             capture_output=True, text=True, cwd=str(ROOT), timeout=60)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("planner log", out.stderr)
        tables = {r[0] for r in sqlite3.connect(old).execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(tables, {"patients"})


class ModelFirstRows(unittest.TestCase):
    """Model-first rows (route_detail "mf:<tool>", a state_card) export like any other, with or without the column."""

    def test_a_model_first_row_exports_and_says_which_path_decided_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "mf_test.db")
            conn = db.connect(path)
            planner_log.record(conn, "voice", "it's a new patient called Nalin", "user said: 'x'; you called c(); result: r",
                               "new_patient", {"name": "Nalin"}, "planner", "book_appointment", 900, [], backend="sarvam",
                               route_detail="mf:new_patient", state_card="STATE CARD (names and last four phone digits only; no ids)")
            planner_log.record(conn, "voice", "book Amit", None, None, None, "rules", "book_appointment", 5001, ["sarvam: timeout"],
                               route_detail="mf_fallback_classic", state_card="STATE CARD ...")
            conn.close()
            ro = export.open_read_only(path)
            self.addCleanup(ro.close)
            cases = [export.to_case(r) for r in export.fetch(ro)]
            self.assertEqual(cases[0][:3], ("it's a new patient called Nalin", "new_patient", {"name": "Nalin"}))
            self.assertIn("route=planner/mf:new_patient", cases[0][4])
            self.assertIn("route=rules/mf_fallback_classic", cases[1][4])
            self.assertEqual(len(export.fetch(ro, problems=True)), 1)          # the fallback is a problem row; the planner row is not

    def test_a_database_from_before_the_column_still_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old_test.db")
            old = sqlite3.connect(path)
            old.executescript("""CREATE TABLE planner_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL DEFAULT (datetime('now')),
                source TEXT NOT NULL DEFAULT 'voice', transcript TEXT NOT NULL, previous_turn TEXT, planner_tool TEXT,
                planner_args_json TEXT, route_taken TEXT NOT NULL, final_intent TEXT, latency_ms INTEGER, override_notes TEXT,
                outcome TEXT);
                INSERT INTO planner_log (transcript, planner_tool, planner_args_json, route_taken, final_intent)
                VALUES ('book Amit kal', 'book_appointment', '{"patient_name": "Amit"}', 'planner', 'book_appointment');""")
            old.commit()
            old.close()
            ro = export.open_read_only(path)
            self.addCleanup(ro.close)
            rows = export.fetch(ro)
            self.assertNotIn("state_card", rows[0])
            self.assertEqual(export.to_case(rows[0])[:3], ("book Amit kal", "book_appointment", {"patient_name": "Amit"}))
            conn = db.connect(path)                               # opening it with the app adds the nullable column in place
            self.addCleanup(conn.close)
            self.assertIn("state_card", {r[1] for r in conn.execute("PRAGMA table_info(planner_log)")})
            self.assertIsNone(conn.execute("SELECT state_card FROM planner_log").fetchone()[0])


class Readme(unittest.TestCase):
    def test_the_log_and_its_privacy_are_documented(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("planner_log", readme)
        self.assertIn("patient names", readme)
        self.assertIn("export_planner_log.py", readme)


if __name__ == "__main__":
    unittest.main()
