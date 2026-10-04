import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SeedDemoTests(unittest.TestCase):
    def run_seed(self, db_path):
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "seed_demo.py"), "--db", str(db_path)],
                              capture_output=True, text=True, env=env, cwd=str(ROOT))

    def test_creates_a_populated_fake_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.db"
            result = self.run_seed(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            conn = sqlite3.connect(path)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0], 10)
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0], 10)
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM staff").fetchone()[0], 0)
            # every phone number is in the reserved fake range
            phones = [r[0] for r in conn.execute("SELECT phone FROM patients")]
            self.assertTrue(all(p.startswith("98765") for p in phones), phones)
            conn.close()

    def test_refuses_to_touch_a_database_that_already_has_patients(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.db"
            self.assertEqual(self.run_seed(path).returncode, 0)
            before = path.read_bytes()
            second = self.run_seed(path)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("refusing", second.stderr + second.stdout)
            self.assertEqual(path.read_bytes(), before)

    def test_the_demo_has_content_for_every_screen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.db"
            self.run_seed(path)
            conn = sqlite3.connect(path)
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM appointments WHERE appt_date = date('now','localtime')").fetchone()[0], 0)
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM followups WHERE due_date < date('now','localtime')").fetchone()[0], 0)
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM visits").fetchone()[0], 0)
            conn.close()


if __name__ == "__main__":
    unittest.main()
