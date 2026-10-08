"""The planner's generic read (clinic/query_tool.py): a whitelisted, parameterised,
read-only SELECT. No model-written SQL ever reaches the database."""
import sqlite3
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_conversation import make_db  # noqa: E402
from tests.test_conversation_branches import add_branches  # noqa: E402

from clinic import query_tool  # noqa: E402
from clinic.query_tool import QueryError  # noqa: E402

TODAY = date.today()
TOMORROW = (TODAY + timedelta(days=1)).isoformat()


class QueryCase(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        add_branches(self.conn)
        for name, phone, age, registered in (
                ("Amit Sharma", "9000000001", 72, "2026-09-01 10:00:00"), ("Priya Shah", "9000000002", 28, "2026-09-02 10:00:00"),
                ("Ramesh Gupta", "9000000003", 64, "2026-10-01 10:00:00"), ("Seema Rao", "9000000004", 40, TODAY.isoformat() + " 09:00:00")):
            self.conn.execute("INSERT INTO patients (name, phone, age, registered_at) VALUES (?, ?, ?, ?)", (name, phone, age, registered))
        for pid, branch, start, status in ((1, 1, "10:00", "booked"), (2, 2, "11:00", "booked"), (3, 1, "12:00", "cancelled"),
                                           (1, 2, "15:00", "confirmed")):
            self.conn.execute("INSERT INTO appointments (patient_id, appt_date, start_time, duration_minutes, status, branch_id) "
                              "VALUES (?, ?, ?, 30, ?, ?)", (pid, TOMORROW, start, status, branch))
        self.conn.execute("INSERT INTO appointments (patient_name, patient_phone, appt_date, start_time, duration_minutes, status, branch_id) "
                          "VALUES ('Walk In', '9111111111', ?, '16:00', 30, 'booked', 1)", (TOMORROW,))
        self.conn.execute("INSERT INTO followups (patient_id, due_date, status) VALUES (1, ?, 'pending'), (2, '2026-09-01', 'done')", (TOMORROW,))
        self.conn.execute("INSERT INTO visits (patient_id, visit_date, fee_paise) VALUES (1, ?, 50000)", (TODAY.isoformat(),))
        self.conn.execute("INSERT INTO expenses (expense_date, description, amount_paise) VALUES (?, 'electricity', 120000)", (TODAY.isoformat(),))
        self.conn.commit()

    def rows(self, **spec):
        return query_tool.run(self.conn, spec).rows


class Whitelist(QueryCase):
    def test_unknown_entity_field_filter_and_key_are_rejected(self):
        for spec in (
            {"entity": "sqlite_master"}, {"entity": "patients; DROP TABLE patients"}, {"entity": "users"}, {"entity": None},
            {"entity": "patients", "fields": ["name", "password"]}, {"entity": "patients", "fields": "name"},
            {"entity": "patients", "fields": ["name; --"]},
            {"entity": "patients", "status": "booked"},                       # patients have no status filter
            {"entity": "patients", "branch": "B"},
            {"entity": "appointments", "age_min": 5},
            {"entity": "cashbook", "status": "pending"},
            {"entity": "patients", "where": "1=1"}, {"entity": "patients", "sql": "SELECT 1"}, {"entity": "patients", "order_by": "age"},
            {"entity": "patients", "aggregate": "sum"},
            {"entity": "appointments", "status": "deleted"},
            {"entity": "followups", "status": "upcoming"},
            {"entity": "patients", "age_min": "old"}, {"entity": "patients", "age_min": 99999},
            {"entity": "patients", "date": "yesterday"}, {"entity": "patients", "date": "2026-02-30"},
            {"entity": "patients", "date": "2026-10-09", "date_to": "2026-10-01"},
            {"entity": "patients", "limit": 0}, {"entity": "patients", "limit": 100000},
            "SELECT * FROM patients", None, ["patients"],
        ):
            with self.subTest(spec=spec):
                with self.assertRaises(QueryError):
                    query_tool.validate_spec(spec)

    def test_availability_is_not_a_table_to_query(self):
        query_tool.validate_spec({"entity": "availability", "date": TOMORROW})
        with self.assertRaises(QueryError):
            query_tool.run(self.conn, {"entity": "availability", "date": TOMORROW})

    def test_every_listed_field_runs_for_every_entity(self):
        for entity in ("patients", "appointments", "followups", "cashbook"):
            fields = list(query_tool.FIELDS[entity])
            result = query_tool.run(self.conn, {"entity": entity, "fields": fields, "aggregate": "list"})
            self.assertEqual(result.columns, fields)
            for row in result.rows:
                self.assertEqual(list(row), fields)


class Entities(QueryCase):
    def test_patients_list_and_count(self):
        self.assertEqual([r["name"] for r in self.rows(entity="patients")], ["Amit Sharma", "Priya Shah", "Ramesh Gupta", "Seema Rao"])
        self.assertEqual(list(self.rows(entity="patients")[0]), ["name", "phone", "age"])
        self.assertEqual(query_tool.run(self.conn, {"entity": "patients", "aggregate": "count"}).total, 4)
        only_names = self.rows(entity="patients", fields=["name"])
        self.assertEqual(only_names[0], {"name": "Amit Sharma"})

    def test_patients_aged_over_sixty(self):
        rows = self.rows(entity="patients", age_min=60, fields=["name", "age"])
        self.assertEqual([(r["name"], r["age"]) for r in rows], [("Amit Sharma", 72), ("Ramesh Gupta", 64)])
        self.assertEqual(len(self.rows(entity="patients", age_min=30, age_max=50)), 1)

    def test_patients_by_name_and_registration_date(self):
        self.assertEqual([r["name"] for r in self.rows(entity="patients", patient_name="amit")], ["Amit Sharma"])
        self.assertEqual([r["name"] for r in self.rows(entity="patients", date=TODAY.isoformat())], ["Seema Rao"])
        self.assertEqual(len(self.rows(entity="patients", date="2026-09-01", date_to="2026-09-30")), 2)

    def test_appointments_default_to_what_happened_or_is_coming_not_cancelled(self):
        rows = self.rows(entity="appointments", date=TOMORROW)
        self.assertEqual(len(rows), 4)                      # the cancelled one is left out
        self.assertEqual(list(rows[0]), ["patient", "date", "time", "doctor", "branch", "status"])
        self.assertIn("Walk In", [r["patient"] for r in rows])      # a walk-in has no patient record
        cancelled = self.rows(entity="appointments", status="cancelled")
        self.assertEqual([r["patient"] for r in cancelled], ["Ramesh Gupta"])

    def test_appointments_for_a_person_and_a_branch(self):
        self.assertEqual([r["time"] for r in self.rows(entity="appointments", patient_name="Amit")], ["10:00", "15:00"])
        at_b = query_tool.run(self.conn, {"entity": "appointments", "date": TOMORROW}, branch_id=2).rows
        self.assertEqual({r["branch"] for r in at_b}, {"Branch B"})
        self.assertEqual(len(at_b), 2)
        everywhere = query_tool.run(self.conn, {"entity": "appointments", "date": TOMORROW}, branch_id=None).rows
        self.assertEqual(len(everywhere), 4)

    def test_upcoming_status_leaves_out_the_past(self):
        self.conn.execute("INSERT INTO appointments (patient_id, appt_date, start_time, duration_minutes, status, branch_id) "
                          "VALUES (1, '2026-01-01', '10:00', 30, 'booked', 1)")
        self.assertEqual(len(query_tool.run(self.conn, {"entity": "appointments", "status": "upcoming"}).rows), 4)

    def test_followups_default_to_pending(self):
        rows = self.rows(entity="followups")
        self.assertEqual([(r["patient"], r["status"]) for r in rows], [("Amit Sharma", "pending")])
        self.assertEqual(len(self.rows(entity="followups", status="done")), 1)

    def test_cashbook_lists_fees_and_expenses(self):
        rows = self.rows(entity="cashbook", date=TODAY.isoformat())
        self.assertEqual({(r["type"], r["amount_rupees"]) for r in rows}, {("fee", 500.0), ("expense", 1200.0)})
        self.assertEqual(query_tool.run(self.conn, {"entity": "cashbook", "aggregate": "count"}).total, 2)


class Caps(QueryCase):
    def test_at_most_two_hundred_rows_and_it_says_so(self):
        self.conn.executemany("INSERT INTO patients (name, phone, age) VALUES (?, ?, 30)",
                              [("Bulk %03d" % i, "9%09d" % i) for i in range(250)])
        self.conn.commit()
        result = query_tool.run(self.conn, {"entity": "patients"})
        self.assertEqual((len(result.rows), result.total, result.truncated), (200, 254, True))
        sentence = query_tool.describe({"entity": "patients", "aggregate": "list"}, result)
        self.assertIn("254 patients", sentence)
        self.assertIn("Showing the first 200", sentence)

    def test_a_smaller_limit_is_honoured_and_a_count_ignores_the_cap(self):
        self.assertEqual(len(self.rows(entity="patients", limit=2)), 2)
        self.conn.executemany("INSERT INTO patients (name, phone, age) VALUES (?, ?, 30)",
                              [("Bulk %03d" % i, "9%09d" % i) for i in range(250)])
        self.assertEqual(query_tool.run(self.conn, {"entity": "patients", "aggregate": "count"}).total, 254)


class NoArbitrarySql(QueryCase):
    ATTACKS = ("'; DROP TABLE patients; --", "x' OR '1'='1", "%", "_", "\\", "' UNION SELECT name, sql, 1 FROM sqlite_master --",
               "Robert'); DELETE FROM appointments;--", "\x00", "a" * 79)

    def test_sql_injection_style_values_are_just_text_to_look_for(self):
        before = self.conn.execute("SELECT (SELECT COUNT(*) FROM patients), (SELECT COUNT(*) FROM appointments)").fetchone()
        for attack in self.ATTACKS:
            for entity in ("patients", "appointments", "followups", "cashbook"):
                with self.subTest(attack=attack, entity=entity):
                    result = query_tool.run(self.conn, {"entity": entity, "patient_name": attack})
                    self.assertEqual(result.rows, [])             # nothing is named like that, and nothing else leaks
                    self.assertEqual(result.total, 0)
        after = self.conn.execute("SELECT (SELECT COUNT(*) FROM patients), (SELECT COUNT(*) FROM appointments)").fetchone()
        self.assertEqual(tuple(before), tuple(after))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'patients'").fetchone()[0], 1)

    def test_like_wildcards_in_a_name_do_not_match_everything(self):
        self.assertEqual(self.rows(entity="patients", patient_name="%"), [])
        self.assertEqual(self.rows(entity="patients", patient_name="_"), [])

    def test_the_connection_is_read_only_while_a_query_runs_and_restored_after(self):
        seen = []
        original = query_tool._where

        def spy(*args, **kwargs):
            seen.append(self.conn.execute("PRAGMA query_only").fetchone()[0])
            return original(*args, **kwargs)

        query_tool._where = spy
        self.addCleanup(setattr, query_tool, "_where", original)
        self.rows(entity="patients")
        self.assertEqual(self.conn.execute("PRAGMA query_only").fetchone()[0], 0)
        # and a write attempted on the guarded connection is refused by SQLite itself
        self.conn.execute("PRAGMA query_only = ON")
        with self.assertRaises(sqlite3.OperationalError):
            self.conn.execute("INSERT INTO patients (name, phone) VALUES ('x', '1')")
        self.conn.execute("PRAGMA query_only = OFF")

    def test_it_restores_query_only_even_when_it_was_already_on(self):
        self.conn.execute("PRAGMA query_only = ON")
        self.rows(entity="patients")
        self.assertEqual(self.conn.execute("PRAGMA query_only").fetchone()[0], 1)

    def test_no_user_value_is_ever_formatted_into_the_sql(self):
        class Recording:
            """Wraps the connection and keeps every (sql, params) it is given."""
            def __init__(self, conn):
                self.conn, self.seen = conn, []

            def execute(self, sql, params=()):
                self.seen.append((sql, list(params)))
                return self.conn.execute(sql, params)

        for attack in (a for a in self.ATTACKS if len(a) > 3):     # a lone "_" is also in column names
            for entity in ("patients", "appointments", "followups", "cashbook"):
                recorder = Recording(self.conn)
                query_tool.run(recorder, {"entity": entity, "patient_name": attack})
                statements = [sql for sql, _ in recorder.seen]
                self.assertFalse(any(attack.casefold() in sql.casefold() for sql in statements), (entity, attack))
                self.assertTrue(any(attack.casefold() in [str(p).casefold() for p in params] for _, params in recorder.seen),
                                "the value must travel as a bound parameter")


class Wording(QueryCase):
    def test_sentences(self):
        result = query_tool.run(self.conn, {"entity": "patients", "age_min": 60})
        self.assertEqual(query_tool.describe({"entity": "patients", "aggregate": "list", "age_min": 60}, result),
                         "2 patients aged 60 or more.")
        none = query_tool.run(self.conn, {"entity": "patients", "patient_name": "zzz"})
        self.assertEqual(query_tool.describe({"entity": "patients", "aggregate": "list", "patient_name": "zzz"}, none),
                         "No patients found matching zzz.")
        count = query_tool.run(self.conn, {"entity": "appointments", "aggregate": "count", "date": TOMORROW})
        self.assertIn("4 appointments on", query_tool.describe({"entity": "appointments", "aggregate": "count", "date": TOMORROW}, count))


if __name__ == "__main__":
    unittest.main()
