import os
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic.adapters.local_sqlite import LocalSQLiteAdapter

SCHEMA = (Path(__file__).resolve().parents[1] / "clinic" / "schema.sql").read_text()


class ReadAnswerEventTests(unittest.TestCase):
    """The dashboard decides what to show above a read answer's table from the
    event's `intent` -- a patient lookup shows only the table."""

    def setUp(self):
        os.environ.setdefault("SARVAM_API_KEY", "test-not-real")
        from clinic.realtime_voice import VoiceSession
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO patients (name, phone, age) VALUES ('Priya Shah', '9123499999', 28)")
        self.conn.commit()
        self.emitted = []
        adapter = LocalSQLiteAdapter()
        self.session = VoiceSession("sid", "key", lambda e, d: self.emitted.append((e, d)),
                                    lambda: self.conn, adapter, adapter, frozenset())

    @patch("clinic.nlu.parser.pick_intent", return_value="patient_lookup")
    @patch("clinic.nlu.parser.extract_name", return_value="Priya Shah")
    def test_patient_lookup_event_carries_its_intent_and_the_row(self, _name, _pick):
        self.session._handle_final_transcript("Can you pull up the data on the patient Priya Shah?")
        events = [d for e, d in self.emitted if e == "read_answer"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["intent"], "patient_lookup")
        self.assertEqual(events[0]["data"]["name"], "Priya Shah")
        self.assertIn("9123499999", events[0]["answer_text"])   # the sentence still exists; the page hides it for lookups


if __name__ == "__main__":
    unittest.main()
