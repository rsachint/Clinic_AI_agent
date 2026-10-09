"""The read views and the guardrails around a model-written SELECT (clinic/schema.sql v_* views, clinic/read_schema.py,
clinic/sql_read.py): the lint, the SQLite authorizer (including the way a view's base tables are authorized), the
read-only connection, the limits, the three registered functions, and that a secret planted in every excluded column is
unreachable however the query is written. Everything runs on in-memory or temporary databases with a fixed "today"
(Friday 2026-10-09): no clock, no network, no model."""
import json
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import sql_read_support as support  # noqa: E402

from clinic import branches, db, query_tool, read_schema, sql_read  # noqa: E402
from clinic.sql_read import ReadError, ReadSession, lint  # noqa: E402

TODAY = support.TODAY


def session_for(conn, **kw):
    return ReadSession(conn, TODAY, support.NOW_HHMM, **kw)


class Clinic(unittest.TestCase):
    """An in-memory clinic with the secrets planted, and one read session on it."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.ids = support.build(self.conn)
        self.session = session_for(self.conn)
        self.addCleanup(self.session.close)

    def rows(self, sql, session=None):
        return (session or self.session).run(sql).rows

    def refused(self, sql, kind=None):
        with self.assertRaises(ReadError) as caught:
            self.session.run(sql)
        if kind:
            self.assertEqual(caught.exception.kind, kind, str(caught.exception))
        return str(caught.exception)


# -- (a) the lint -------------------------------------------------------------------------------------------------

class LintTests(unittest.TestCase):
    def bad(self, sql, words=None):
        with self.assertRaises(ReadError) as caught:
            lint(sql)
        self.assertEqual(caught.exception.kind, "lint")
        if words:
            self.assertIn(words.lower(), str(caught.exception).lower())

    def test_a_plain_select_and_a_with_pass_and_a_trailing_semicolon_is_dropped(self):
        self.assertEqual(lint("SELECT 1"), "SELECT 1")
        self.assertEqual(lint("  select name from v_patients ;  "), "select name from v_patients")
        self.assertEqual(lint("WITH x AS (SELECT 1 AS a) SELECT a FROM x;"), "WITH x AS (SELECT 1 AS a) SELECT a FROM x")

    def test_exactly_one_statement(self):
        self.bad("SELECT 1; SELECT 2", "one statement")
        self.bad("SELECT 1;;", "one statement")
        self.bad("SELECT 1; DROP TABLE patients", "one statement")
        self.bad("SELECT 1 ; ; ", "one statement")

    def test_it_must_begin_with_select_or_with(self):
        for sql in ("INSERT INTO patients (name, phone) VALUES ('x', 'y')", "UPDATE patients SET age = 1", "DELETE FROM patients",
                    "DROP TABLE patients", "PRAGMA table_info(patients)", "ATTACH DATABASE 'x.db' AS x", "EXPLAIN SELECT 1",
                    "VALUES (1)", "REPLACE INTO patients (name, phone) VALUES ('x', 'y')", "(SELECT 1)", "BEGIN", ""):
            with self.subTest(sql=sql):
                self.bad(sql)

    def test_no_comments_of_either_kind(self):
        self.bad("SELECT 1 -- why", "comment")
        self.bad("SELECT 1 /* why */", "comment")
        self.bad("SELECT /* x */ 1", "comment")
        self.bad("SELECT 1 /* never closed", "comment")
        self.bad("SELECT 1\n-- one more line\n", "comment")

    def test_comment_marks_and_semicolons_inside_a_string_are_fine(self):
        self.assertIn("a--b", lint("SELECT * FROM v_patients WHERE name = 'a--b'"))
        self.assertIn("/*", lint("SELECT * FROM v_patients WHERE name = '/* x */'"))
        self.assertIn("a;b", lint("SELECT * FROM v_patients WHERE name = 'a;b'"))
        self.assertIn("it''s", lint("SELECT * FROM v_patients WHERE name = 'it''s'"))

    def test_no_nul_unterminated_quotes_or_gibberish(self):
        self.bad("SELECT 1\x00", "nul")
        self.bad("SELECT 'abc", "quote")
        self.bad('SELECT "abc', "quote")
        self.bad("SELECT 1 # hash")
        self.bad("SELECT {x}")
        self.bad("SELECT 1 \\ 2")

    def test_length_cap(self):
        lint("SELECT 1" + " " * 100)
        self.bad("SELECT '" + "x" * sql_read.MAX_SQL_CHARS + "'", "too long")
        self.bad("SELECT 1 " + "+ 1 " * 400, "too long")

    def test_only_the_named_parameters_today_and_now(self):
        lint("SELECT :today, :now")
        lint("SELECT :TODAY")
        for sql in ("SELECT :other", "SELECT ?", "SELECT ?1", "SELECT @today", "SELECT $today", "SELECT :today2"):
            with self.subTest(sql=sql):
                self.bad(sql, "parameters")

    def test_person_tests_are_tests_not_values(self):
        good = ("SELECT * FROM v_appointments WHERE name_match(patient_name, 'Amit')",
                "SELECT * FROM v_appointments a WHERE name_match(a.patient_name, 'Amit') = 1 AND a.status = 'booked'",
                "SELECT * FROM v_appointments a WHERE name_match(a.patient_name, 'Amit') <> 1",
                "SELECT * FROM v_appointments a WHERE name_match(a.patient_name, 'Amit') = 0",
                "SELECT * FROM v_appointments a WHERE 1 = name_match(a.patient_name, 'Amit')",
                "SELECT * FROM v_appointments a WHERE NOT name_match(a.patient_name, 'Amit')",
                "SELECT * FROM v_appointments WHERE phone10(patient_phone, '9876543210') = 1 AND doctor_name = 'Dr. Mehta' "
                "AND branch_name = 'Branch A' AND status IN ('booked', 'confirmed')",
                "SELECT a.patient_name FROM v_appointments a JOIN v_patients p ON name_match(a.patient_name, p.name) = 1",
                "SELECT * FROM v_attendance WHERE name_match(staff_name, 'Seema') AND status = 'present'",
                "SELECT name_match(NULL, 'a'), phone10('9811122233', NULL)",
                "SELECT patient_name FROM v_appointments WHERE patient_name <> ''",
                "SELECT COUNT(DISTINCT patient_name) FROM v_appointments WHERE status = 'booked'",
                "SELECT * FROM v_branches WHERE name = 'Branch A'", "SELECT * FROM v_doctors WHERE name = 'Dr. Rao'")
        for sql in good:
            with self.subTest(sql=sql):
                lint(sql)

    def test_a_person_test_compared_to_a_value_names_the_right_form(self):
        name_form = "Use name_match(<column>, 'spoken name') = 1 to find a person; it returns 1 or 0, so never compare a column to it."
        for sql in ("SELECT * FROM v_appointments WHERE patient_name = name_match(patient_name, 'Amit')",
                    "SELECT * FROM v_appointments WHERE patient_name != name_match(patient_name, 'Amit')",
                    "SELECT * FROM v_appointments WHERE patient_name <> name_match(patient_name, 'Amit')",
                    "SELECT * FROM v_appointments WHERE patient_name IN name_match(patient_name, 'Amit')",
                    "SELECT * FROM v_appointments WHERE patient_name IN (name_match(patient_name, 'Amit'))",
                    "SELECT * FROM v_appointments WHERE patient_name LIKE name_match(patient_name, 'Amit')",
                    "SELECT * FROM v_appointments WHERE name_match(patient_name, 'Amit') = patient_name",
                    "SELECT * FROM v_appointments WHERE name_match(patient_name, 'Amit') = 'Amit'",
                    "SELECT * FROM v_appointments WHERE name_match(patient_name, 'Amit') IN (1)",
                    "SELECT * FROM v_appointments WHERE name_match(patient_name, 'Amit') LIKE 1"):
            with self.subTest(sql=sql):
                with self.assertRaises(ReadError) as caught:
                    lint(sql)
                self.assertEqual(str(caught.exception), name_form)
                self.assertEqual(caught.exception.kind, "lint")
        self.bad("SELECT * FROM v_appointments WHERE patient_phone = phone10(patient_phone, '9876543210')", "phone10(<column>, 'digits') = 1")

    def test_a_person_test_never_takes_a_bind_parameter_or_the_wrong_number_of_arguments(self):
        self.bad("SELECT * FROM v_appointments WHERE patient_name = :name_match(patient_name, 'A')", "name_match is a function, not a parameter")
        self.bad("SELECT * FROM v_appointments WHERE :phone10(patient_phone, '1') = 1", "phone10 is a function, not a parameter")
        self.bad("SELECT * FROM v_appointments WHERE name_match(patient_name, :today) = 1", "never a bind parameter")
        self.bad("SELECT * FROM v_appointments WHERE phone10(patient_phone, ?) = 1", "never a bind parameter")
        self.bad("SELECT * FROM v_appointments WHERE name_match(patient_name) = 1", "name_match takes exactly two arguments")
        self.bad("SELECT * FROM v_appointments WHERE name_match(patient_name, 'A', 'B') = 1", "name_match takes exactly two arguments")
        self.bad("SELECT * FROM v_appointments WHERE phone10() = 1", "phone10 takes exactly two arguments")
        self.bad("SELECT * FROM v_appointments WHERE name_match(lower(patient_name), 'A', 'B') = 1", "exactly two arguments")
        lint("SELECT * FROM v_appointments WHERE name_match(lower(patient_name), 'A') = 1")          # a nested call's comma is not an argument

    def test_a_person_column_is_never_compared_to_a_text(self):
        for sql, form in (("SELECT * FROM v_appointments WHERE patient_name = 'Neha'", "name_match(patient_name, 'spoken name') = 1"),
                          ("SELECT * FROM v_appointments a WHERE a.patient_name = 'Neha'", "name_match(patient_name"),
                          ("SELECT * FROM v_appointments WHERE lower(patient_name) = 'neha'", "name_match(patient_name"),
                          ("SELECT * FROM v_appointments WHERE 'Neha' = patient_name", "name_match(patient_name"),
                          ("SELECT * FROM v_appointments WHERE patient_name != 'Neha'", "name_match(patient_name"),
                          ("SELECT * FROM v_appointments WHERE patient_name IN ('Neha', 'Amit')", "name_match(patient_name"),
                          ("SELECT * FROM v_appointments WHERE patient_name NOT IN ('Neha')", "name_match(patient_name"),
                          ("SELECT * FROM v_appointments WHERE patient_name LIKE '%Neha%'", "name_match(patient_name"),
                          ("SELECT * FROM v_attendance WHERE staff_name = 'Seema'", "name_match(staff_name, 'spoken name') = 1"),
                          ("SELECT * FROM v_appointments WHERE patient_phone = '9876543210'", "phone10(patient_phone, 'digits') = 1"),
                          ("SELECT * FROM v_followups WHERE patient_phone LIKE '%3210'", "phone10(patient_phone, 'digits') = 1")):
            with self.subTest(sql=sql):
                self.bad(sql, form)

    def test_other_exact_compares_are_not_flagged(self):
        for sql in ("SELECT * FROM v_appointments WHERE doctor_name = 'Dr. Mehta' AND branch_name = 'Branch A' AND status = 'booked'",
                    "SELECT * FROM v_followups WHERE status IN ('pending', 'missed') AND branch_code = 'A'",
                    "SELECT * FROM v_cashbook WHERE kind = 'fee' AND description = 'Clinic rent'",
                    "SELECT * FROM v_appointments WHERE patient_name IS NULL OR patient_name = patient_phone",
                    "SELECT * FROM v_patients WHERE name = 'Amit Dua'"):
            with self.subTest(sql=sql):
                lint(sql)

    def test_no_recursive_ctes(self):
        self.bad("WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t) SELECT n FROM t LIMIT 5", "recursive")
        self.bad("with   recursive t(n) as (select 1 union all select n+1 from t) select n from t limit 5", "recursive")

    def test_write_ddl_and_transaction_words_are_refused_anywhere(self):
        for sql in ("WITH x AS (SELECT 1) INSERT INTO patients (name, phone) SELECT 'a', 'b'", "SELECT 1 FROM v_patients; DELETE FROM patients",
                    "SELECT load_extension('x')", "WITH x AS (SELECT 1) DELETE FROM patients", "SELECT 1 UNION SELECT 2 RETURNING 1",
                    "WITH x AS (SELECT 1) UPDATE patients SET age = 1"):
            with self.subTest(sql=sql):
                self.bad(sql)
        lint("SELECT replace(name, 'a', 'b') FROM v_patients")            # replace() is a function, REPLACE INTO is not
        lint("SELECT 'DROP TABLE x' AS text")                             # words inside a string are just text

    def test_a_cte_may_not_be_named_like_a_view_or_a_table(self):
        for name in ("v_appointments", "V_PATIENTS", "patients", "appointments", "sqlite_master", "v_anything", "roster_series", "_x"):
            with self.subTest(name=name):
                with self.assertRaises(ReadError):
                    lint("WITH {} AS (SELECT 1 AS a) SELECT a FROM {}".format(name, name), reserved=("patients", "appointments"))
        lint("WITH recent AS (SELECT 1 AS a), other(b) AS (SELECT 2) SELECT a, b FROM recent, other")
        with self.assertRaises(ReadError):
            lint('WITH "v_patients" AS (SELECT 1) SELECT * FROM "v_patients"')

    def test_a_cte_hiding_after_a_comma_is_found(self):
        with self.assertRaises(ReadError):
            lint("WITH fine AS (SELECT 1 AS a), v_appointments AS (SELECT 2 AS a) SELECT * FROM v_appointments")

    def test_string_builders_cannot_make_a_huge_string(self):
        lint("SELECT printf('%d rows', 3)")
        lint("SELECT printf('%5.2f', 3.14159)")
        lint("SELECT format('%s-%s', 'a', 'b')")
        lint("SELECT replace(replace(replace(name, 'a', 'b'), 'c', 'd'), 'e', 'f') FROM v_patients")
        for sql in ("SELECT printf('%999999999d', 1)", "SELECT printf('%.999999999f', 1)", "SELECT printf('%*d', 99999999, 1)",
                    "SELECT printf('%' || '99999999' || 'd', 1)", "SELECT printf(name, 1) FROM v_patients", "SELECT format(name) FROM v_patients",
                    "SELECT replace(replace(replace(replace(name, 'a', 'b'), 'c', 'd'), 'e', 'f'), 'g', 'h') FROM v_patients"):
            with self.subTest(sql=sql):
                self.bad(sql)

    def test_complexity_caps(self):
        eight = " ".join("JOIN v_patients p{} ON 1 = 1".format(i) for i in range(7))
        lint("SELECT 1 FROM v_patients p0 {}".format(eight))                  # eight sources
        self.bad("SELECT 1 FROM v_patients p0 {} JOIN v_patients pz ON 1 = 1".format(eight), "joined sources")
        self.bad("SELECT 1 FROM " + ", ".join("v_patients p{}".format(i) for i in range(9)), "joined sources")
        self.bad("SELECT " + " + ".join("(SELECT 1)" for _ in range(11)), "sub-selects")
        lint("SELECT a, b, c FROM v_patients ORDER BY a, b LIMIT 5, 10")      # commas outside a FROM list are not sources


# -- the views and their description -------------------------------------------------------------------------------

EXPECTED_VIEWS = ("v_appointments", "v_patients", "v_doctors", "v_branches", "v_staff", "v_attendance", "v_followups", "v_visits",
                  "v_expenses", "v_cashbook", "v_reminders", "v_closures", "v_blocks", "v_activity", "v_audit", "v_schedules",
                  "v_roster_days")
# Column NAMES that must never be a column of a view (the planted secrets test the data; this tests the schema).
EXCLUDED_NAMES = {"notes", "diagnosis", "body", "wa_id", "maps_url", "payload_json", "paid_to", "meta_json", "message", "closed_message",
                  "error", "detail", "raw_text", "interactive_json", "template_json", "token", "latitude", "longitude", "slots_json",
                  "dedup_key", "last_notified_token", "fee_paise", "amount_paise", "patient_id", "doctor_id", "branch_id"}

# alias -> base table of each query_tool entity's expressions: how the test re-derives what query_tool reads
_ALIASES = {
    "patients": {"p": "patients"},
    "appointments": {"a": "appointments", "p": "patients", "b": "branches", "d": "doctors"},
    "followups": {"f": "followups", "p": "patients", "d": "doctors", "b": "branches"},
    "cashbook": {"v": "visits", "e": "expenses", "p": "patients"},
    "visits": {"v": "visits", "p": "patients"},
    "expenses": {"e": "expenses"},
    "staff": {"s": "staff", "b": "branches"},
    "attendance": {"att": "attendance", "s": "staff", "b": "branches"},
    "branches": {"b0": "branches", "b": "branches", "k": "booking_blocks", "s": "doctor_schedules", "d": "doctors"},
    "doctors": {"d": "doctors"},
    "schedules": {"s": "doctor_schedules", "d": "doctors", "b": "branches"},
    "reminders": {"n": "notifications", "a": "appointments", "p": "patients", "p2": "patients"},
    "closures": {"c": "closures", "b": "branches", "d": "doctors", "m": "closure_moves"},
    "blocks": {"k": "booking_blocks", "b": "branches", "d": "doctors"},
    "audit": {"a": "audit_log"},
    "activity": {"pa": "patient_activity", "p": "patients"},
}


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def query_tool_reads(conn):
    """{base table: the real columns clinic/query_tool.py's own SQL mentions}, re-derived from its whitelist tables."""
    real = {t: {r[1] for r in conn.execute("PRAGMA table_info({})".format(t))} for (t,) in
            conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    found = {}
    for entity, spec in query_tool._TABLES.items():
        aliases = _ALIASES[entity]
        for text in _strings({k: v for k, v in spec.items() if k != "noun"}):
            for alias, column in re.findall(r"\b([a-z][a-z0-9]*)\.([a-z_]+)\b", text):
                table = aliases.get(alias)
                if table and column in real.get(table, ()):
                    found.setdefault(table, set()).add(column)
        # bare columns of the closure_moves sub-select
        if entity == "closures":
            found.setdefault("closure_moves", set()).update({"closure_id", "action", "result"})
    return found


class DescribedViews(Clinic):
    def view_names(self):
        return [r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'view' ORDER BY name")]

    def test_the_views_are_exactly_the_seventeen_entities_minus_availability(self):
        self.assertEqual(set(self.view_names()), set(EXPECTED_VIEWS))
        self.assertEqual(set(read_schema.VIEW_NAMES), set(EXPECTED_VIEWS))
        # one view per query_tool entity except availability (a computed thing: it stays a tool), plus the roster
        self.assertEqual({"v_" + entity for entity in query_tool.ENTITIES if entity != "availability"} | {"v_roster_days"},
                         set(EXPECTED_VIEWS))

    def test_every_view_selects_on_the_read_connection(self):
        for view in EXPECTED_VIEWS:
            with self.subTest(view=view):
                self.session.run("SELECT * FROM {}".format(view))

    def test_the_described_columns_are_the_views_columns_in_order(self):
        for view in EXPECTED_VIEWS:
            with self.subTest(view=view):
                actual = read_schema.view_columns(self.conn, view)
                self.assertEqual(list(read_schema.COLUMNS[view]), actual)
                for column, meaning in read_schema.VIEWS[view][1].items():
                    self.assertTrue(meaning.strip(), (view, column))

    def test_the_prompt_lists_exactly_the_views_columns_and_nothing_else(self):
        text = read_schema.schema_text(self.conn)
        listed = {}
        for line in text.splitlines():
            head, _, body = line.partition("): ")
            view = head.split(" (")[0]
            listed[view] = [part.split(" = ")[0].strip() for part in body.split("; ")]
        self.assertEqual(set(listed), set(EXPECTED_VIEWS))
        for view, columns in listed.items():
            actual = read_schema.view_columns(self.conn, view)
            self.assertEqual(columns, actual, view)
        self.assertEqual(set(re.findall(r"\bv_[a-z_]+", text)), set(EXPECTED_VIEWS))          # no view or table outside the allow-list
        for name in EXCLUDED_NAMES - {"patient_id", "doctor_id", "branch_id"}:
            self.assertNotRegex(text, r"\b{}\b".format(name), name)
        for table in ("proposals", "wa_messages", "audit_log", "notifications", "sqlite_master", "app_settings"):
            self.assertNotIn(table, text)

    def test_a_column_the_file_does_not_describe_is_listed_bare_and_a_missing_one_is_dropped(self):
        spec = read_schema.VIEWS["v_doctors"][1]
        saved = dict(spec)
        try:
            spec.pop("specialty")
            self.assertIn("v_doctors (active doctors): name = doctor; title = Dr.; specialty", read_schema.schema_text(self.conn))
        finally:
            spec.update(saved)

    def test_no_view_has_an_excluded_column_name(self):
        for view in EXPECTED_VIEWS:
            columns = {c.lower() for c in read_schema.COLUMNS[view]}
            self.assertFalse(columns & {n for n in EXCLUDED_NAMES if n not in ("patient_id",)} - {"id"}, view)
        self.assertNotIn("patient_id", {c for v in read_schema.COLUMNS.values() for c in v})     # appointments carry no patient id column

    def test_the_views_only_read_base_columns_query_tool_reads_plus_the_listed_extras(self):
        reads = query_tool_reads(self.conn)
        for table, columns in read_schema.ALLOWED_BASE.items():
            extra = read_schema.EXTRA_BASE.get(table, frozenset())
            self.assertLessEqual(extra, columns, table)
            self.assertLessEqual(set(columns) - set(extra), reads.get(table, set()),
                                 "{} exposes a column query_tool does not read: {}".format(table, set(columns) - set(extra) - reads.get(table, set())))
        # ... and what the views really read is inside that list (the session refuses to open otherwise)
        for view, pairs in self.session._footprint.items():
            for table, column in pairs:
                self.assertIn(column, read_schema.ALLOWED_BASE[table], (view, table, column))

    def test_no_planted_secret_column_is_in_the_allowed_base_columns_but_two_read_for_a_lookup(self):
        allowed = {"{}.{}".format(t, c) for t, cols in read_schema.ALLOWED_BASE.items() for c in cols}
        # app_settings.value is read for ONE row (default_branch_id, a NULL appointment branch) and notifications.wa_id only to
        # find a reminder's patient by phone, as clinic/query_tool.py does; neither is ever an output column (SecretsAreUnreachable)
        lookups = {"app_settings.value", "notifications.wa_id"}
        for planted in support.SECRETS:
            if planted not in lookups:
                self.assertNotIn(planted, allowed)
        self.assertLessEqual(lookups, allowed)

    def test_a_view_that_reads_an_unlisted_column_makes_the_session_refuse_to_open(self):
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("DROP VIEW v_doctors")
        conn.execute("CREATE VIEW v_doctors AS SELECT d.name AS name, d.title AS title, d.specialty AS specialty, "
                     "(SELECT group_concat(notes) FROM appointments) AS x FROM doctors d")
        sql_read._footprints.clear()
        with self.assertRaises(ReadError) as caught:
            session_for(conn)
        self.assertEqual(caught.exception.kind, "unavailable")
        sql_read._footprints.clear()

    def test_money_is_in_rupees_and_nothing_in_paise_is_exposed(self):
        self.assertEqual(self.rows("SELECT fee_rupees FROM v_visits ORDER BY fee_rupees"), [(300.0,), (450.5,), (500.0,), (1000.0,)])
        self.assertEqual(self.rows("SELECT amount_rupees FROM v_expenses ORDER BY amount_rupees"), [(1200.0,), (50000.0,)])
        self.assertEqual(self.rows("SELECT SUM(amount_rupees) FROM v_cashbook WHERE kind = 'expense'"), [(51200.0,)])
        self.assertEqual(self.rows("SELECT SUM(amount_rupees) FROM v_cashbook WHERE kind = 'fee'"), [(2250.5,)])
        for view in ("v_visits", "v_expenses", "v_cashbook"):
            self.assertFalse([c for c in read_schema.COLUMNS[view] if "paise" in c], view)


# -- what the views hold ----------------------------------------------------------------------------------------------

class ViewContent(Clinic):
    def test_appointments_have_the_patient_and_the_walk_in_combined(self):
        rows = {r[0]: r for r in self.rows("SELECT patient_name, patient_phone, status, doctor_name FROM v_appointments")}
        self.assertEqual(rows["Amit Dua"][:2], ("Amit Dua", "9811122233"))
        self.assertEqual(rows["Walk In Wali"][:2], ("Walk In Wali", "9555500000"))          # a walk-in has no patient row
        self.assertEqual(self.rows("SELECT COUNT(*) FROM v_appointments WHERE name_match(patient_name, 'Walk In Wali') = 1"), [(1,)])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM patients WHERE name = 'Walk In Wali'").fetchone()[0], 0)

    def test_a_null_branch_is_shown_as_the_default_branch(self):
        self.assertIsNone(self.conn.execute("SELECT branch_id FROM appointments WHERE id = 3").fetchone()[0])
        self.assertEqual(self.rows("SELECT branch_code, branch_name FROM v_appointments WHERE id = 3"), [("A", "Branch A")])
        branches.set_default_branch(self.conn, self.ids["B"])
        with session_for(self.conn) as session:
            self.assertEqual(session.run("SELECT branch_code, branch_name FROM v_appointments WHERE id = 3").rows, [("B", "Branch B")])
            self.assertEqual(session.run("SELECT branch_code FROM v_appointments WHERE id = 1").rows, [("A",)])    # a set branch stays

    def test_the_end_time_the_length_and_the_queue_state(self):
        self.conn.execute("UPDATE appointments SET queue_state = 'checked_in', duration_minutes = 45 WHERE id = 1")
        self.conn.commit()
        with session_for(self.conn) as session:
            self.assertEqual(session.run("SELECT start_time, end_time, minutes, queue_state FROM v_appointments WHERE id = 1").rows,
                             [("11:00", "11:45", 45, "checked_in")])

    def test_timestamps_are_ist(self):
        self.assertEqual(self.rows("SELECT created_ist FROM v_appointments WHERE id = 1"), [("2026-10-09 10:00",)])      # 04:30 UTC
        self.assertEqual(self.rows("SELECT created_ist FROM v_appointments WHERE id = 3"), [("2026-10-08 23:59",)])      # 18:29 UTC
        self.assertEqual(self.rows("SELECT registered_ist FROM v_patients WHERE name = 'Amit Dua'"), [("2026-10-01 10:00",)])
        self.assertEqual(self.rows("SELECT sent_ist FROM v_reminders"), [("2026-10-08 10:00",)])
        self.assertEqual(self.rows("SELECT logged_ist FROM v_audit"), [("2026-10-08 10:00",)])
        self.assertEqual(self.rows("SELECT activity_time FROM v_activity"), [("2026-10-08 10:00",)])    # already the clinic's wall clock

    def test_the_other_views_hold_what_they_say(self):
        self.assertEqual(self.rows("SELECT name, title, specialty FROM v_doctors ORDER BY name"),
                         [("Dr. Iyer", "Dr.", "General physician"), ("Dr. Mehta", "Dr.", "General physician"), ("Dr. Rao", "Dr.", "Paediatrics")])
        self.assertEqual(self.rows("SELECT name, role, branch_code, branch_name FROM v_staff ORDER BY name"),
                         [("Ravi", "receptionist", None, "Any"), ("Seema", "nurse", "A", "Branch A")])
        self.assertEqual(self.rows("SELECT staff_name, status FROM v_attendance ORDER BY staff_name"), [("Ravi", "leave"), ("Seema", "present")])
        self.assertEqual(self.rows("SELECT patient_name, due_date, due_time, status, doctor_name, branch_code, has_slot FROM v_followups ORDER BY due_date"),
                         [("Amit Anand", "2026-10-05", None, "missed", None, None, "no"), ("Amit Dua", "2026-10-20", "10:00", "pending", "Dr. Mehta", "A", "yes")])
        self.assertEqual(self.rows("SELECT branch_name, doctor_name, start_date, end_date, status, patients_moved, appointments_cancelled FROM v_closures"),
                         [("Branch B", None, "2026-10-12", "2026-10-14", "applied", 1, 1)])
        self.assertEqual(self.rows("SELECT branch_name, doctor_name, start_date, end_date, reason FROM v_blocks"),
                         [("Branch B", "All doctors", "2026-10-12", "2026-10-14", "Doctor on leave")])
        self.assertEqual(self.rows("SELECT kind, status, patient_name FROM v_reminders"), [("booking_confirmed", "sent", "Amit Dua")])      # conv_reply is not shown
        self.assertEqual(self.rows("SELECT action, record_type FROM v_audit"), [("book_appointment", "appointment")])
        self.assertEqual(self.rows("SELECT event, patient_name, source FROM v_activity"), [("auto_booked", "Amit Dua", "whatsapp-agent")])
        self.assertEqual(self.rows("SELECT description, amount_rupees FROM v_cashbook WHERE kind = 'expense' ORDER BY entry_date"),
                         [("Clinic rent", 50000.0), ("electricity", 1200.0)])

    def test_branches_say_whether_they_are_open_today(self):
        self.assertEqual(self.rows("SELECT code, status, open_today, closed_reason FROM v_branches ORDER BY code"),
                         [("A", "open", "yes", None), ("B", "open", "yes", None), ("C", "closed", "no", "Renovation")])
        self.conn.execute("INSERT INTO booking_blocks (start_date, end_date, reason, active, branch_id) VALUES ('2026-10-09', '2026-10-09', 'holiday', 1, NULL)")
        self.conn.commit()
        with session_for(self.conn) as session:
            self.assertEqual(session.run("SELECT code, open_today FROM v_branches ORDER BY code").rows, [("A", "no"), ("B", "no"), ("C", "no")])

    def test_schedules_are_the_weekly_windows_with_their_validity(self):
        rows = self.rows("SELECT doctor_name, weekday, start_time, end_time, valid_from, valid_to FROM v_schedules "
                         "WHERE branch_code = 'B' AND weekday = 'Monday' ORDER BY valid_from")
        self.assertEqual(rows, [("Dr. Rao", "Monday", "10:00", "14:00", None, "2026-10-11"),
                                ("Dr. Iyer", "Monday", "10:00", "14:00", "2026-10-12", "2026-10-18"),
                                ("Dr. Rao", "Monday", "10:00", "14:00", "2026-10-19", None)])

    def test_the_roster_matches_doctor_windows_for_every_branch_and_day_of_the_next_sixty(self):
        view = {(r[0], r[1], r[2]): r[3] for r in self.rows("SELECT roster_date, branch_code, doctor_name, hours FROM v_roster_days")}
        expected = {}
        for offset in range(60):
            day = (TODAY + timedelta(days=offset)).isoformat()
            for branch in branches.list_branches(self.conn):
                windows = branches.doctor_windows(self.conn, branch["id"], day)
                by_doctor = {}
                for start, end, doctor_id in windows:
                    by_doctor.setdefault(doctor_id, []).append("{:02d}:{:02d}-{:02d}:{:02d}".format(start // 60, start % 60, end // 60, end % 60))
                for doctor_id, parts in by_doctor.items():
                    expected[(day, branch["code"], branches.get_doctor(self.conn, doctor_id)["name"])] = ", ".join(parts)
        self.assertEqual(view, expected)
        self.assertGreater(len(expected), 100)

    def test_the_roster_rotates_as_the_live_clinic_does(self):
        def at(day, branch):
            return [r[0] for r in self.rows("SELECT doctor_name FROM v_roster_days WHERE roster_date = '{}' AND branch_code = '{}'".format(day, branch))]
        self.assertEqual(at("2026-10-09", "B"), ["Dr. Rao"])            # Friday, the permanent pattern
        self.assertEqual(at("2026-10-10", "B"), ["Dr. Iyer"])           # Saturday: Dr. Iyer all year
        self.assertEqual(at("2026-10-12", "B"), ["Dr. Iyer"])           # the dated week
        self.assertEqual(at("2026-10-16", "B"), ["Dr. Iyer"])
        self.assertEqual(at("2026-10-19", "B"), ["Dr. Rao"])            # the pattern resumes
        self.assertEqual(at("2026-10-11", "B"), [])                     # Sunday: nobody at B
        self.assertEqual(at("2026-10-11", "C"), [])                     # C is closed, so its Sunday doctor is not on the roster
        self.assertEqual(at("2026-10-16", "A"), ["Dr. Mehta"])

    def test_the_roster_covers_today_and_the_next_fifty_nine_days_from_the_injected_clock(self):
        days = [r[0] for r in self.rows("SELECT DISTINCT roster_date FROM v_roster_days ORDER BY roster_date")]
        self.assertEqual((days[0], days[-1], len(days)), ("2026-10-09", "2026-12-07", 60))
        with ReadSession(self.conn, date(2027, 2, 27), "09:00") as later:
            days = [r[0] for r in later.run("SELECT DISTINCT roster_date FROM v_roster_days ORDER BY roster_date").rows]
            self.assertEqual((days[0], days[-1]), ("2027-02-27", "2027-04-27"))

    def test_an_inactive_doctor_or_branch_leaves_the_roster_and_the_schedule(self):
        self.conn.execute("UPDATE doctors SET active = 0 WHERE name = 'Dr. Rao'")
        self.conn.execute("UPDATE branches SET active = 0 WHERE code = 'B'")
        self.conn.commit()
        with session_for(self.conn) as session:
            self.assertEqual(session.run("SELECT COUNT(*) FROM v_roster_days WHERE doctor_name = 'Dr. Rao' OR branch_code = 'B'").rows, [(0,)])
            self.assertEqual(session.run("SELECT COUNT(*) FROM v_schedules WHERE doctor_name = 'Dr. Rao' OR branch_code = 'B'").rows, [(0,)])
            self.assertEqual(session.run("SELECT COUNT(*) FROM v_branches WHERE code = 'B'").rows, [(0,)])
            self.assertEqual(session.run("SELECT COUNT(*) FROM v_doctors WHERE name = 'Dr. Rao'").rows, [(0,)])

    def test_per_branch_counts_with_a_left_join_show_the_zeros(self):
        rows = self.rows("SELECT b.name, COUNT(a.id) FROM v_branches b LEFT JOIN v_appointments a ON a.branch_code = b.code "
                         "AND a.status = 'cancelled' GROUP BY b.code, b.name ORDER BY b.code")
        self.assertEqual(rows, [("Branch A", 1), ("Branch B", 1), ("Branch C", 0)])

    def test_the_parameters_today_and_now_come_from_the_injected_clock(self):
        self.assertEqual(self.rows("SELECT :today, :now, today_ist()"), [("2026-10-09", "10:00", "2026-10-09")])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM v_appointments WHERE appt_date = :today"), [(1,)])


# -- (b) the authorizer -------------------------------------------------------------------------------------------------

class AuthorizerTests(Clinic):
    def test_a_base_table_named_directly_is_denied_however_it_is_read(self):
        for sql in ("SELECT notes FROM appointments", "SELECT * FROM patients", "SELECT name FROM patients", "SELECT count(*) FROM patients",
                    "SELECT count(*) FROM appointments", "SELECT 1 FROM visits", "SELECT body FROM notifications",
                    "SELECT diagnosis FROM followups", "SELECT paid_to FROM expenses", "SELECT payload_json FROM audit_log",
                    "SELECT value FROM app_settings", "SELECT * FROM patients p JOIN v_patients q ON q.id = p.id",
                    "SELECT p.name FROM patients p, v_patients q", "SELECT (SELECT notes FROM appointments LIMIT 1)",
                    "SELECT * FROM v_patients WHERE name IN (SELECT name FROM patients)",
                    "SELECT * FROM main.patients", "SELECT * FROM v_patients UNION SELECT name, phone, age, id, 1 FROM patients"):
            with self.subTest(sql=sql):
                self.refused(sql, "denied")

    def test_the_engine_tables_and_table_functions_are_denied(self):
        for sql in ("SELECT name FROM sqlite_master", "SELECT * FROM sqlite_schema", "SELECT * FROM sqlite_temp_master",
                    "SELECT * FROM pragma_table_info('patients')", "SELECT * FROM pragma_database_list", "SELECT * FROM sqlite_sequence",
                    "SELECT * FROM json_each('[1]')", "SELECT * FROM generate_series(1, 5)"):
            with self.subTest(sql=sql):
                self.refused(sql)

    def test_denied_functions(self):
        for sql in ("SELECT random()", "SELECT randomblob(4)", "SELECT typeof(1)", "SELECT hex('a')", "SELECT sqlite_version()",
                    "SELECT json_extract('{}', '$.a')", "SELECT load_extension('x')", "SELECT zeroblob(10)", "SELECT quote(1)",
                    "SELECT * FROM v_patients WHERE regexp('a', name)", "SELECT last_insert_rowid()", "SELECT changes()"):
            with self.subTest(sql=sql):
                self.refused(sql)

    def test_the_short_list_of_functions_works(self):
        expected = {"count(*)": 1, "sum(1)": 1, "avg(2)": 2.0, "min(3)": 3, "max(4)": 4, "total(5)": 5.0, "abs(-1)": 1, "round(1.234, 1)": 1.2,
                    "coalesce(NULL, 1)": 1, "ifnull(NULL, 2)": 2, "nullif(1, 2)": 1, "length('ab')": 2, "lower('A')": "a", "upper('a')": "A",
                    "trim(' a ')": "a", "substr('abc', 2)": "bc", "replace('abc', 'b', 'x')": "axc", "instr('abc', 'c')": 3,
                    "date('2026-10-09')": "2026-10-09", "time('10:00')": "10:00:00", "datetime('2026-10-09 10:00')": "2026-10-09 10:00:00",
                    "strftime('%Y', '2026-10-09')": "2026", "julianday('2026-10-09')": 2461322.5, "printf('%d', 3)": "3", "group_concat('a')": "a",
                    "CAST('5' AS INTEGER)": 5, "CASE WHEN 1 THEN 'y' ELSE 'n' END": "y", "name_match('Amit', 'Amit')": 1,
                    "phone10('9811122233', '9811122233')": 1, "'abc' LIKE 'a%'": 1, "'abc' GLOB 'a*'": 1, "today_ist()": "2026-10-09",
                    "iif(1, 'a', 'b')": "a"}
        for expression, value in expected.items():
            with self.subTest(expression=expression):
                self.assertEqual(self.rows("SELECT " + expression), [(value,)])

    def test_writes_ddl_pragma_attach_and_temp_objects_are_denied_by_the_engine_itself(self):
        # straight to the connection, bypassing the lint: the authorizer alone must refuse every one
        for sql in ("INSERT INTO patients (name, phone) VALUES ('x', 'y')", "UPDATE patients SET age = 1", "DELETE FROM patients",
                    "DROP TABLE patients", "DROP VIEW v_patients", "CREATE TABLE t (x)", "CREATE TEMP TABLE t (x)", "CREATE VIEW v_x AS SELECT 1",
                    "CREATE TRIGGER tr AFTER INSERT ON patients BEGIN SELECT 1; END", "ALTER TABLE patients ADD COLUMN z", "ATTACH DATABASE ':memory:' AS x",
                    "PRAGMA table_info(patients)", "PRAGMA query_only = OFF", "PRAGMA writable_schema = ON", "BEGIN", "SAVEPOINT s",
                    "REINDEX", "ANALYZE", "VACUUM", "SELECT load_extension('x')", "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 3) SELECT n FROM t"):
            with self.subTest(sql=sql):
                with self.assertRaises(sqlite3.Error):
                    self.session.conn.execute(sql)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0], 6)

    def test_a_cte_named_like_a_view_cannot_borrow_the_views_right_to_read_base_tables(self):
        # the lint refuses the name; with the lint out of the way the authorizer's per-view footprint still refuses the column
        for sql in ("WITH v_appointments AS (SELECT notes AS patient_name FROM appointments) SELECT * FROM v_appointments",
                    "WITH v_visits AS (SELECT notes AS x FROM visits) SELECT x FROM v_visits",
                    "WITH v_reminders AS (SELECT body AS x FROM notifications) SELECT x FROM v_reminders",
                    "WITH v_branches AS (SELECT maps_url AS x FROM branches) SELECT x FROM v_branches"):
            with self.subTest(sql=sql):
                with self.assertRaises(ReadError):
                    self.session.run(sql)                                  # the lint
                with self.assertRaises(sqlite3.DatabaseError):
                    self.session.conn.execute(sql).fetchall()              # the engine's authorizer, lint bypassed

    def test_a_cte_named_like_a_view_over_a_fully_exposed_table_is_refused_by_the_lint_only(self):
        # patients has no column the view hides, so the footprint has nothing to refuse: the lint is the layer that does
        sql = "WITH v_patients AS (SELECT * FROM patients) SELECT * FROM v_patients"
        with self.assertRaises(ReadError):
            self.session.run(sql)

    def test_a_view_reads_its_base_tables_only_for_the_columns_it_uses(self):
        footprint = self.session._footprint
        self.assertIn(("appointments", "appt_date"), footprint["v_appointments"])
        self.assertNotIn(("appointments", "notes"), footprint["v_appointments"])
        self.assertNotIn(("visits", "notes"), footprint["v_visits"])
        self.assertNotIn(("followups", "diagnosis"), footprint["v_followups"])
        self.assertNotIn(("expenses", "paid_to"), footprint["v_expenses"])
        self.assertNotIn(("notifications", "body"), footprint["v_reminders"])
        self.assertNotIn(("audit_log", "payload_json"), footprint["v_audit"])
        self.assertNotIn(("patient_activity", "meta_json"), footprint["v_activity"])
        self.assertNotIn(("branches", "maps_url"), footprint["v_branches"])
        self.assertNotIn(("closures", "message"), footprint["v_closures"])

    def test_a_missing_column_or_table_is_a_plain_repairable_message(self):
        columns = "Columns: v_appointments ({})".format(", ".join(read_schema.COLUMNS["v_appointments"]))
        self.assertEqual(self.refused("SELECT cancelled FROM v_appointments", "sql"), "no such column: cancelled. " + columns)
        self.assertIn("no such column: notes", self.refused("SELECT notes FROM v_appointments"))
        self.assertIn("no such table: v_nothing", self.refused("SELECT * FROM v_nothing"))
        self.assertIn("table not allowed: appointments", self.refused("SELECT notes FROM appointments"))
        self.assertIn("function not allowed: random", self.refused("SELECT random()"))
        self.assertIn("syntax error", self.refused("SELECT FROM"))

    def test_a_missing_column_lists_the_real_columns_of_every_view_the_query_uses(self):
        message = self.refused("SELECT d.doctor_name, a.id FROM v_doctors d JOIN v_appointments a ON a.doctor_name = d.name", "sql")
        self.assertTrue(message.startswith("no such column: d.doctor_name. Columns: "), message)
        self.assertEqual(message.split("Columns: ", 1)[1],
                         "v_doctors ({}); v_appointments ({})".format(", ".join(read_schema.COLUMNS["v_doctors"]),
                                                                       ", ".join(read_schema.COLUMNS["v_appointments"])))
        self.assertIn("v_doctors (name, title, specialty)", message)
        for column in read_schema.COLUMNS["v_appointments"]:                 # taken from the views' own list, never hand-written
            self.assertIn(column, message)
        self.assertNotIn("v_patients", message)                                # only the views the query uses

    def test_a_missing_table_lists_the_views_when_the_query_names_none(self):
        message = self.refused("SELECT * FROM v_appointment")
        self.assertTrue(message.startswith("no such table: v_appointment"), message)
        self.assertTrue(message.endswith("Views: " + ", ".join(read_schema.VIEW_NAMES)), message)
        with_view = self.refused("SELECT * FROM v_patients p JOIN v_nothing n ON n.x = p.id")
        self.assertIn("Columns: v_patients (id, name, phone, age, registered_ist)", with_view)

    def test_other_errors_do_not_get_the_column_list(self):
        for sql in ("SELECT FROM v_patients", "SELECT random() FROM v_patients", "SELECT notes FROM appointments"):
            with self.subTest(sql=sql):
                self.assertNotIn("Columns:", self.refused(sql))

    def test_the_recursive_view_is_allowed_to_recurse_and_nothing_else_is(self):
        self.assertEqual(self.rows("SELECT COUNT(DISTINCT roster_date) FROM v_roster_days"), [(60,)])
        self.refused("WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 3) SELECT n FROM t", "lint")

    def test_an_error_never_names_a_path_or_internals(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "unlikely-name.db")
            conn = db.connect(path)
            support.build(conn)
            conn.commit()
            with session_for(conn) as session:
                for sql in ("SELECT * FROM nothing", "SELECT notes FROM appointments", "SELECT FROM", "SELECT random()", "SELECT * FROM sqlite_master",
                            "SELECT x FROM v_patients"):
                    with self.assertRaises(ReadError) as caught:
                        session.run(sql)
                    for leak in (tmp, "unlikely-name", ".db", "sqlite3.", "Traceback", "main."):
                        self.assertNotIn(leak, str(caught.exception))
            conn.close()


# -- (c) the read-only connection ------------------------------------------------------------------------------------

class ReadOnlyTests(unittest.TestCase):
    def test_an_in_memory_database_is_copied_into_a_second_read_only_in_memory_connection(self):
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        support.build(conn)
        with session_for(conn) as session:
            self.assertIsNot(session.conn, conn)
            self.assertEqual(session.run("SELECT COUNT(*) FROM v_patients").rows, [(6,)])
            session.conn.set_authorizer(lambda *args: sqlite3.SQLITE_OK)                      # take the authorizer away: query_only alone still refuses writes
            self.assertEqual(session.conn.execute("PRAGMA query_only").fetchone()[0], 1)
            for sql in ("INSERT INTO patients (name, phone) VALUES ('x', 'y')", "UPDATE patients SET age = 1", "DELETE FROM visits",
                        "CREATE TABLE t (x)", "DROP VIEW v_patients"):
                with self.subTest(sql=sql):
                    with self.assertRaises(sqlite3.OperationalError):
                        session.conn.execute(sql)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0], 6)           # the original is untouched

    def test_a_database_file_is_opened_read_only_by_uri_and_the_app_connection_is_never_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(str(Path(tmp) / "clinic-test.db"))
            support.build(conn)
            before = conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
            with session_for(conn) as session:
                self.assertEqual(session.run("SELECT COUNT(*) FROM v_patients").rows, [(before,)])
                session.conn.set_authorizer(lambda *args: sqlite3.SQLITE_OK)
                self.assertEqual(session.conn.execute("PRAGMA database_list").fetchone()[2], str(Path(tmp, "clinic-test.db").resolve()))
                self.assertEqual(session.conn.execute("PRAGMA query_only").fetchone()[0], 1)
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    session.conn.execute("INSERT INTO patients (name, phone) VALUES ('x', 'y')")
                self.assertIn("readonly", str(caught.exception).lower())
                session.conn.execute("PRAGMA query_only = OFF")          # even if that were switched off, the file is opened mode=ro
                with self.assertRaises(sqlite3.OperationalError):
                    session.conn.execute("CREATE TABLE t (x)")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0], before)
            conn.close()

    def test_a_file_that_cannot_be_opened_read_only_is_copied_into_memory_instead(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(str(Path(tmp) / "clinic-test.db"))
            support.build(conn)
            conn.commit()
            real = sqlite3.connect

            def refuse_uri(target, *args, **kwargs):
                if kwargs.get("uri"):
                    raise sqlite3.OperationalError("unable to open database file")
                return real(target, *args, **kwargs)
            with mock.patch("clinic.sql_read.sqlite3.connect", side_effect=refuse_uri):
                with session_for(conn) as session:
                    self.assertEqual(session.run("SELECT COUNT(*) FROM v_patients").rows, [(6,)])
                    session.conn.set_authorizer(lambda *args: sqlite3.SQLITE_OK)
                    self.assertEqual(session.conn.execute("PRAGMA database_list").fetchone()[2], "")             # the in-memory copy
                    self.assertEqual(session.conn.execute("PRAGMA query_only").fetchone()[0], 1)
                with self.assertRaises(ReadError):                                                               # a bare path has no copy to fall back to
                    sql_read.run_query(str(Path(tmp) / "clinic-test.db"), "SELECT 1", TODAY)
            conn.close()

    def test_it_sees_what_the_app_connection_committed_in_wal_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(str(Path(tmp) / "clinic-test.db"))
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            support.build(conn)
            conn.execute("INSERT INTO patients (name, phone, age) VALUES ('Late Arrival', '9000000555', 33)")
            conn.commit()
            with session_for(conn) as session:
                self.assertEqual(session.run("SELECT age FROM v_patients WHERE name = 'Late Arrival'").rows, [(33,)])
            conn.close()

    def test_a_path_works_too_and_a_missing_view_means_reading_is_not_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "clinic-test.db")
            conn = db.connect(path)
            support.build(conn)
            conn.commit()
            self.assertEqual(sql_read.run_query(path, "SELECT COUNT(*) FROM v_patients", TODAY).rows, [(6,)])
            bare = str(Path(tmp) / "bare.db")
            raw = sqlite3.connect(bare)
            raw.execute("CREATE TABLE patients (id INTEGER PRIMARY KEY, name TEXT)")
            raw.commit()
            raw.close()
            with self.assertRaises(ReadError) as caught:
                sql_read.run_query(bare, "SELECT 1", TODAY)
            self.assertEqual(caught.exception.kind, "unavailable")
            conn.close()

    def test_a_closed_session_cannot_run(self):
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        session = session_for(conn)
        session.close()
        with self.assertRaises(ReadError):
            session.run("SELECT 1")


# -- (d) the limits ---------------------------------------------------------------------------------------------------

class LimitTests(Clinic):
    def test_the_row_cap_is_200_and_the_answer_says_how_many_there_were(self):
        self.conn.executemany("INSERT INTO patients (name, phone, age) VALUES (?, '9000000000', 30)", [("Bulk {}".format(i),) for i in range(250)])
        self.conn.commit()
        with session_for(self.conn) as session:
            result = session.run("SELECT name FROM v_patients ORDER BY name")
            self.assertEqual((len(result.rows), result.truncated, result.total), (200, True, 256))
            small = session.run("SELECT name FROM v_patients WHERE name LIKE 'Amit%'")
            self.assertEqual((len(small.rows), small.truncated, small.total), (2, False, None))
            capped = session.run("SELECT name FROM v_patients LIMIT 5")
            self.assertEqual((len(capped.rows), capped.truncated), (5, False))

    def test_a_pathological_cross_join_is_stopped_by_the_time_limit(self):
        sql = "SELECT COUNT(*) FROM v_roster_days a, v_roster_days b, v_roster_days c, v_roster_days d, v_roster_days e"
        with session_for(self.conn, time_limit=0.2) as session:
            with self.assertRaises(ReadError) as caught:
                session.run(sql)
            self.assertEqual(caught.exception.kind, "limit")
            self.assertIn("too long", str(caught.exception))
            self.assertEqual(session.run("SELECT COUNT(*) FROM v_patients").rows, [(6,)])           # the session is still usable

    def test_the_callers_own_deadline_also_stops_a_query(self):
        clock = {"now": 100.0}
        ticks = iter(range(1, 10 ** 6))

        def fake_clock():
            clock["now"] += 0.5 if next(ticks) > 3 else 0.0           # time passes quickly once the query is running
            return clock["now"]
        with session_for(self.conn, clock=fake_clock, time_limit=1000) as session:
            with self.assertRaises(ReadError) as caught:
                session.run("SELECT COUNT(*) FROM v_roster_days a, v_roster_days b, v_roster_days c, v_roster_days d, v_roster_days e",
                            deadline=101.0)
            self.assertEqual(caught.exception.kind, "limit")

    def test_long_strings_are_cut_and_the_size_of_a_result_is_capped(self):
        self.conn.execute("INSERT INTO patients (name, phone, age) VALUES (?, '9000000001', 33)", ("Z" * 500,))
        self.conn.commit()
        with session_for(self.conn) as session:
            cell = session.run("SELECT name FROM v_patients WHERE name LIKE 'ZZZ%'").rows[0][0]
            self.assertEqual(len(cell), sql_read.MAX_CELL_CHARS)
            self.assertTrue(cell.endswith("…"))
            wide = ", ".join("{} AS c{}".format(i, i) for i in range(17))
            with self.assertRaises(ReadError) as caught:
                session.run("SELECT {}".format(wide))
            self.assertIn("too many columns", str(caught.exception))
            session.run("SELECT {}".format(", ".join("{} AS c{}".format(i, i) for i in range(16))))
            self.conn.executemany("INSERT INTO patients (name, phone, age) VALUES (?, '9000000000', 30)", [("W{}".format(i),) for i in range(250)])
            self.conn.commit()
        with session_for(self.conn) as session:
            many = "SELECT {} FROM v_patients LIMIT 200".format(", ".join("name AS n{}".format(i) for i in range(16)))
            result = session.run(many)
            self.assertLessEqual(len(result.rows) * len(result.columns), sql_read.MAX_CELLS)
            self.assertTrue(result.truncated)

    def test_duplicate_column_names_get_distinct_names(self):
        result = self.rows("SELECT a.id, b.id FROM v_patients a JOIN v_patients b ON a.id = b.id LIMIT 1")
        self.assertEqual(len(result[0]), 2)
        names = self.session.run("SELECT a.id, b.id FROM v_patients a JOIN v_patients b ON a.id = b.id LIMIT 1").columns
        self.assertEqual(len({n.lower() for n in names}), 2)

    def test_blobs_and_floats_are_made_plain(self):
        self.assertEqual(self.rows("SELECT x'4142'"), [("[binary]",)])
        self.assertEqual(self.rows("SELECT 1.23456, 2.0"), [(1.23, 2.0)])


# -- (e) the registered functions ---------------------------------------------------------------------------------------

class FunctionTests(Clinic):
    def names(self, spoken, column="name", view="v_patients"):
        return [r[0] for r in self.rows("SELECT {c} FROM {v} WHERE name_match({c}, '{s}') ORDER BY {c}".format(c=column, v=view, s=spoken))]

    def test_name_match_is_the_entity_resolvers_exact_rule(self):
        from clinic import entity_resolution
        for stored in ("Amit Dua", "Amit Anand", "राहुल शर्मा", "Rahul Sharma", "Priya Shah", "Sunita Devi"):
            for spoken in ("Amit", "amit dua", "Amit Dua", "Ami", "Amitt", "Dua", "Rahul", "राहुल", "राहुल शर्मा", "Rahul Sharma", "Rahu",
                           "Mr. Amit", "Dr Priya", "प्रिया", "Sunita Devi", "sunita"):
                with self.subTest(stored=stored, spoken=spoken):
                    expected = 1 if entity_resolution.name_match(spoken, stored) == 1.0 else 0
                    got = self.rows("SELECT name_match(?, ?)".replace("?", "'{}'")
                                    .format(stored.replace("'", "''"), spoken.replace("'", "''")))[0][0]
                    self.assertEqual(got, expected)

    def test_a_first_name_finds_every_person_with_it_and_never_a_part_of_a_word(self):
        self.assertEqual(self.names("Amit"), ["Amit Anand", "Amit Dua"])                 # two patients share the first name
        self.assertEqual(self.names("Amit Dua"), ["Amit Dua"])
        self.assertEqual(self.names("Ami"), [])
        self.assertEqual(self.names("Dua"), [])                                           # a surname alone is not a match
        self.assertEqual(self.names("AMIT dua"), ["Amit Dua"])
        self.assertEqual(self.names("Sunita"), ["Sunita Devi"])

    def test_devanagari_and_roman_names_match_by_their_roman_spelling(self):
        self.assertEqual(self.names("Rahul"), ["Rahul Sharma", "राहुल शर्मा"])
        self.assertEqual(self.names("राहुल"), ["Rahul Sharma", "राहुल शर्मा"])
        self.assertEqual(self.names("राहुल शर्मा"), ["Rahul Sharma", "राहुल शर्मा"])
        self.assertEqual(self.names("राहु"), [])

    def test_name_match_on_an_appointments_combined_name_includes_walk_ins(self):
        self.assertEqual(self.names("Walk", "patient_name", "v_appointments"), ["Walk In Wali"])
        self.assertEqual(self.names("Walk In Wali", "patient_name", "v_appointments"), ["Walk In Wali"])

    def test_name_match_and_phone10_are_null_safe(self):
        self.assertEqual(self.rows("SELECT name_match(NULL, 'a'), name_match('a', NULL), phone10(NULL, '9811122233'), phone10('9811122233', NULL)"),
                         [(0, 0, 0, 0)])

    def phones(self, text):
        return [r[0] for r in self.rows("SELECT name FROM v_patients WHERE phone10(phone, '{}') ORDER BY name".format(text))]

    def test_phone10_compares_the_last_ten_digits_exactly(self):
        for written in ("9811122233", "+91 98111 22233", "09811122233", "91-9811122233", "98111 22233", "(+91) 9811122233"):
            with self.subTest(written=written):
                self.assertEqual(self.phones(written), ["Amit Dua"])
        for written in ("811122233", "98111222", "9811122234", "98111222334", "", "abc", "98111 2223", "9811122233123"):
            with self.subTest(written=written):
                self.assertEqual(self.phones(written), [])

    def test_phone10_matches_a_stored_number_with_the_country_code(self):
        self.assertEqual(self.phones("9000000004"), ["Sunita Devi"])             # stored as 919000000004

    def test_phone10_on_an_appointments_phone(self):
        self.assertEqual(self.rows("SELECT patient_name FROM v_appointments WHERE phone10(patient_phone, '9555500000')"), [("Walk In Wali",)])

    def test_today_ist_is_the_injected_date_and_no_other_function_is_registered(self):
        with ReadSession(self.conn, date(2027, 1, 31), "08:15") as session:
            self.assertEqual(session.run("SELECT today_ist(), :today, :now").rows, [("2027-01-31", "2027-01-31", "08:15")])
        self.assertEqual(set(read_schema.REGISTERED_FUNCTIONS), {"name_match", "phone10", "today_ist"})
        self.assertLessEqual(set(read_schema.REGISTERED_FUNCTIONS), read_schema.ALLOWED_FUNCTIONS)


# -- the secrets ---------------------------------------------------------------------------------------------------------

def _dump(rows):
    return json.dumps(rows, ensure_ascii=False, default=str)


class SecretsAreUnreachable(Clinic):
    markers = set(support.SECRETS.values()) | {"SECRET"}

    def assert_clean(self, text, where):
        for marker in self.markers:
            self.assertNotIn(marker, text, (where, marker))

    def test_the_fixture_really_holds_every_secret_in_the_database(self):
        for planted, marker in support.SECRETS.items():
            table, column = planted.split(".")
            count = self.conn.execute("SELECT COUNT(*) FROM {} WHERE {} LIKE ?".format(table, column), ("%" + marker + "%",)).fetchone()[0]
            self.assertGreater(count, 0, planted)

    def test_every_view_in_every_query_shape_is_free_of_every_secret(self):
        for view in EXPECTED_VIEWS:
            columns = ", ".join(read_schema.COLUMNS[view])
            shapes = [
                "SELECT * FROM {v}",
                "SELECT * FROM (SELECT * FROM {v})",
                "WITH x AS (SELECT * FROM {v}) SELECT * FROM x",
                "SELECT * FROM {v} UNION ALL SELECT * FROM {v}",
                "SELECT a.* FROM {v} a LEFT JOIN {v} b ON 1 = 1 LIMIT 100",
                "SELECT group_concat(c) FROM (SELECT {first} AS c FROM {v})".format(v="{v}", first=read_schema.COLUMNS[view][0]),
                "SELECT {cols} FROM {v} ORDER BY 1",
                "SELECT * FROM {v} WHERE 1 = 1 GROUP BY {cols}",
            ]
            for shape in shapes:
                sql = shape.format(v=view, cols=columns)
                with self.subTest(sql=sql):
                    result = self.session.run(sql)
                    self.assert_clean(_dump(result.rows), sql)

    def test_a_view_of_a_view_through_a_join_across_all_of_them_is_clean(self):
        for left, right in ((a, b) for a in ("v_appointments", "v_patients", "v_followups", "v_reminders", "v_branches", "v_closures")
                            for b in ("v_audit", "v_activity", "v_visits", "v_cashbook", "v_blocks", "v_staff")):
            sql = "SELECT * FROM {} l LEFT JOIN {} r ON 1 = 1 LIMIT 20".format(left, right)
            with self.subTest(sql=sql):
                try:
                    result = self.session.run(sql)
                except ReadError as exc:                  # too many columns is a refusal too, and also clean
                    self.assertEqual(exc.kind, "limit")
                    continue
                self.assert_clean(_dump(result.rows), sql)

    def test_every_column_of_every_base_table_is_unreachable_directly_or_by_any_trick(self):
        tables = [r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        reached = 0
        for table in tables:
            for column in [r[1] for r in self.conn.execute("PRAGMA table_info({})".format(table))]:
                tries = [
                    "SELECT {c} FROM {t}",
                    "SELECT {c} FROM {t} LIMIT 1",
                    "SELECT * FROM (SELECT {c} FROM {t})",
                    "WITH x AS (SELECT {c} FROM {t}) SELECT * FROM x",
                    "SELECT (SELECT {c} FROM {t} LIMIT 1)",
                    "SELECT v.name FROM v_patients v WHERE EXISTS (SELECT 1 FROM {t} WHERE {c} IS NOT NULL)",
                    "SELECT * FROM v_patients WHERE name IN (SELECT {c} FROM {t})",
                    "SELECT {c} FROM v_patients, {t}",
                    "SELECT count({c}) FROM {t}",
                ]
                for pattern in tries:
                    sql = pattern.format(c=column, t=table)
                    with self.subTest(sql=sql):
                        with self.assertRaises(ReadError):
                            self.session.run(sql)
                        reached += 1
        self.assertGreater(reached, 1000)

    def test_a_search_for_a_secret_inside_the_views_finds_nothing(self):
        for view in EXPECTED_VIEWS:
            for column in read_schema.COLUMNS[view]:
                sql = "SELECT COUNT(*) FROM {} WHERE CAST({} AS TEXT) LIKE '%SECRET%'".format(view, column)
                with self.subTest(sql=sql):
                    self.assertEqual(self.rows(sql), [(0,)])

    def test_the_wa_id_used_to_name_a_reminders_patient_never_comes_out(self):
        self.assert_clean(_dump(self.rows("SELECT * FROM v_reminders")), "v_reminders")
        self.assertNotIn("919000000099", _dump(self.rows("SELECT * FROM v_reminders")))
        self.assertNotIn("919000000088", _dump(self.rows("SELECT * FROM v_activity")))

    def test_the_settings_token_is_not_reachable_through_the_default_branch_lookup(self):
        for sql in ("SELECT * FROM v_appointments", "SELECT branch_name FROM v_appointments WHERE branch_name LIKE '%SECRET%'"):
            self.assert_clean(_dump(self.rows(sql)), sql)
        self.refused("SELECT value FROM app_settings", "denied")
        self.refused("WITH v_appointments AS (SELECT value AS x FROM app_settings) SELECT x FROM v_appointments", "lint")
        with self.assertRaises(sqlite3.DatabaseError):
            self.session.conn.execute("WITH v_branches AS (SELECT key, value FROM app_settings) SELECT value FROM v_branches").fetchall()


if __name__ == "__main__":
    unittest.main()
