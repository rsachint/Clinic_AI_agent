"""scripts/export_unanswered.py: the developer queue, read-only."""
import io
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import export_unanswered  # noqa: E402

from clinic import db, unanswered  # noqa: E402


class ExportTests(unittest.TestCase):
    def run_script(self, path):
        out = io.StringIO()
        with redirect_stdout(out):
            code = export_unanswered.main(["--db", path])
        return code, out.getvalue()

    def test_prints_open_questions_most_asked_first_with_the_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "t.db")
            conn = db.connect(path)
            unanswered.capture(conn, "who is on duty now", "voice", wanted="doctor on duty")
            for _ in range(2):
                unanswered.capture(conn, "profit this month", "typed", rejected_spec={"entity": "profit"})
            done = unanswered.capture(conn, "old question", "voice")
            unanswered.set_status(conn, done.id, "dismissed")
            conn.close()
            code, text = self.run_script(path)
            self.assertEqual(code, 0)
            self.assertLess(text.index("profit this month"), text.index("who is on duty now"))
            self.assertIn("asked 2x", text)
            self.assertIn("wanted:   doctor on duty", text)
            self.assertIn('{"entity": "profit"}', text)
            self.assertNotIn("old question", text)

    def test_it_never_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "t.db")
            conn = db.connect(path)
            unanswered.capture(conn, "who is on duty now", "voice")
            conn.close()
            conn = export_unanswered.open_read_only(path)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE unanswered_questions SET status = 'resolved'")
            conn.close()

    def test_an_old_database_and_an_empty_queue_are_explained(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old.db"
            sqlite3.connect(str(old)).close()
            self.assertIn("No unanswered_questions table", self.run_script(str(old))[1])
            fresh = str(Path(tmp) / "fresh.db")
            db.connect(fresh).close()
            self.assertIn("No open questions.", self.run_script(fresh)[1])


if __name__ == "__main__":
    unittest.main()
