"""clinic/help_requests.py and clinic/help_uploads.py: feedback records for "Need help". A temp database and a temp
upload folder are used everywhere (never clinic.db); the clock is injected, so nothing depends on today's date."""
import csv
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import db, help_requests as hr, help_uploads as hu, settings  # noqa: E402

T0 = datetime(2026, 10, 5, 10, 0, 0)          # Monday 10:00, the fixed clock
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n% test\n"


class FakeUpload:
    """What werkzeug's FileStorage looks like to help_uploads: a filename and a stream."""

    def __init__(self, name, data):
        self.filename = name
        self.stream = io.BytesIO(data)


def office_zip(kind="docx", extra=(), content_types=b"<Types></Types>", skip_types=False):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        if not skip_types:
            z.writestr("[Content_Types].xml", content_types)
        z.writestr("word/document.xml" if kind == "docx" else "xl/workbook.xml", "<x/>")
        for name in extra:
            z.writestr(name, "x")
    return buffer.getvalue()


class HelpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "uploads"
        self.conn = db.connect(str(Path(self.tmp.name) / "scratch.db"))
        self.addCleanup(self.conn.close)

    def make(self, description="The booking screen is confusing and slow to use", category="other", user="Reception (demo)",
             now=T0, files=(), **kw):
        staged = hu.stage_files(list(files), self.root) if files else None
        return hr.create_request(self.conn, user, category, description, staged=staged, now=now, root=self.root, **kw)

    def count(self, table):
        return self.conn.execute("SELECT COUNT(*) FROM {}".format(table)).fetchone()[0]

    def files_on_disk(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file()) if self.root.exists() else []


class CategoriesAndSchema(HelpCase):
    def test_the_four_categories_are_seeded_with_stable_keys_in_order(self):
        self.assertEqual([(c["key"], c["label"]) for c in hr.categories(self.conn)], [
            ("voice_assistant", "Voice assistant"), ("patients_data", "Patients data"),
            ("language_understanding", "Language understanding"), ("other", "Other")])

    def test_seeding_never_overwrites_and_a_new_category_needs_no_code(self):
        self.conn.execute("UPDATE help_categories SET label = 'Voice helper', active = 0 WHERE key = 'voice_assistant'")
        self.conn.execute("INSERT INTO help_categories (key, label, sort_order) VALUES ('billing', 'Billing', 50)")
        self.conn.commit()
        again = db.connect(str(Path(self.tmp.name) / "scratch.db"))            # the seed runs again on every connect
        self.addCleanup(again.close)
        self.assertEqual(again.execute("SELECT COUNT(*) FROM help_categories").fetchone()[0], 5)
        self.assertEqual(again.execute("SELECT label, active FROM help_categories WHERE key = 'voice_assistant'").fetchone()[:], ("Voice helper", 0))
        keys = [c["key"] for c in hr.categories(again)]
        self.assertEqual(keys, ["patients_data", "language_understanding", "billing", "other"])
        self.assertTrue(self.make(category="billing")["category"] == "billing")

    def test_a_retired_category_is_refused_for_new_requests_but_old_ones_stay_readable(self):
        first = self.make(category="voice_assistant")
        self.conn.execute("UPDATE help_categories SET active = 0 WHERE key = 'voice_assistant'")
        self.conn.commit()
        with self.assertRaises(hr.HelpError):
            self.make(category="voice_assistant", now=T0 + timedelta(minutes=1))
        old = hr.get_request(self.conn, first["id"], "Reception (demo)", T0)
        self.assertEqual((old["category"], old["category_label"]), ("voice_assistant", "Voice assistant"))
        self.assertNotIn("voice_assistant", [c["key"] for c in hr.config_view(self.conn)["categories"]])

    def test_a_request_stores_the_category_key(self):
        self.make(category="patients_data")
        self.assertEqual(self.conn.execute("SELECT category_key FROM help_requests").fetchone()[0], "patients_data")


class Creating(HelpCase):
    def test_a_request_gets_a_sequential_ticket_and_the_server_side_username(self):
        a = self.make()
        b = self.make(now=T0 + timedelta(minutes=1))
        self.assertEqual((a["ticket_no"], b["ticket_no"]), ("HELP-0001", "HELP-0002"))
        self.assertEqual(a["username"], "Reception (demo)")
        self.assertEqual((a["status"], a["category_label"], a["created_at"]), ("new", "Other", "2026-10-05 10:00:00"))
        row = self.conn.execute("SELECT * FROM help_requests WHERE id = 1").fetchone()
        self.assertEqual((row["username"], row["severity"], row["source"], row["status"]), ("Reception (demo)", "minor", "typed", "new"))

    def test_current_user_is_the_one_stand_in(self):
        self.assertEqual(hr.current_user(), {"username": "Reception (demo)"})

    def test_ticket_numbers_are_never_reused_even_when_the_sequence_is_ahead_of_the_rows(self):
        self.make()
        self.conn.execute("UPDATE sqlite_sequence SET seq = 9 WHERE name = 'help_requests'")        # rows were removed at some point
        self.conn.commit()
        self.assertEqual(self.make(now=T0 + timedelta(minutes=1))["ticket_no"], "HELP-0010")

    def test_validation(self):
        bad = [dict(category="nope"), dict(category=None), dict(description=""), dict(description="   "), dict(description="short"),
               dict(description="x" * 4001), dict(severity="huge"), dict(source="telepathy"), dict(description=5),
               dict(description="[number hidden]"), dict(description="98765 43210 98765")]
        for kwargs in bad:
            args = dict(category="other", description="The booking screen is confusing and slow to use")
            args.update(kwargs)
            with self.assertRaises(hr.HelpError, msg=repr(kwargs)):
                hr.create_request(self.conn, "Reception (demo)", args.pop("category"), args.pop("description"), now=T0,
                                  root=self.root, **args)
        self.assertEqual(self.count("help_requests"), 0)

    def test_the_length_rules_and_the_attachment_exception(self):
        self.make(description="abcdefghij")                                          # exactly 10 characters
        with self.assertRaises(hr.HelpError):
            self.make(description="abcdefghi", now=T0 + timedelta(minutes=1))
        self.make(description="x" * 4000, now=T0 + timedelta(minutes=2))               # exactly the maximum
        # with a file a few words are enough, but at least one character
        ok = self.make(description="slow", now=T0 + timedelta(minutes=3), files=[FakeUpload("a.png", PNG)])
        self.assertEqual(len(ok["attachments"]), 1)
        with self.assertRaises(hr.HelpError):
            self.make(description="", now=T0 + timedelta(minutes=4), files=[FakeUpload("b.png", PNG)])
        self.assertEqual(self.files_on_disk().__len__(), 1)                            # the refused one left nothing

    def test_severity_and_source_defaults_and_values(self):
        self.make(severity="blocks_work", source="mixed")
        row = self.conn.execute("SELECT severity, source FROM help_requests").fetchone()
        self.assertEqual((row[0], row[1]), ("blocks_work", "mixed"))
        self.make(severity="", source="", now=T0 + timedelta(minutes=1))
        row = self.conn.execute("SELECT severity, source FROM help_requests WHERE id = 2").fetchone()
        self.assertEqual((row[0], row[1]), ("minor", "typed"))

    def test_creating_records_a_created_event(self):
        made = self.make()
        events = hr.get_request(self.conn, made["id"], "Reception (demo)", T0)["events"]
        self.assertEqual([(e["actor"], e["kind"], e["to_status"]) for e in events], [("user", "created", "new")])

    def test_context_keeps_only_plain_known_keys(self):
        made = self.make(context={"tab": "patients", "branch": 2, "language": "en-IN", "screen": "<script>", "name": "Asha Rao",
                                  "phone": "9876543210"}, extra_context={"app_version": "1.2"})
        detail = hr.get_request(self.conn, made["id"], "Reception (demo)", T0)
        self.assertEqual(detail["context"], {"tab": "patients", "branch": "2", "language": "en-IN", "app_version": "1.2"})

    def test_the_confirmation_data(self):
        made = self.make(files=[FakeUpload("shot.png", PNG)], severity="annoying")
        self.assertEqual((made["sla_hours"], made["sla_due_at"], made["sla_state"]), (24, "2026-10-06 10:00:00", "on_track"))
        self.assertEqual([(a["original_name"], a["mime"]) for a in made["attachments"]], [("shot.png", "image/png")])
        self.assertFalse(made["masked"])
        self.assertNotIn("stored_name", made["attachments"][0])


class Masking(HelpCase):
    def test_runs_of_eight_or_more_digits_are_hidden(self):
        cases = {
            "call 9876543210 now": "call [number hidden] now",
            "call +91 98765 43210 now": "call [number hidden] now",
            "call 98765-43210 now": "call [number hidden] now",
            "aadhaar 1234 5678 9012 slow": "aadhaar [number hidden] slow",
            "12345678": "[number hidden]",
            "1234567": "1234567",
            "token A-12 at 10:30 on 05/10/2026": "token A-12 at 10:30 on 05/10/2026",
            "seen on 2026-10-05, slow": "seen on 2026-10-05, slow",
            "two numbers 9876543210 and 9811100001": "two numbers [number hidden] and [number hidden]",
        }
        for raw, expected in cases.items():
            self.assertEqual(hr.mask_numbers(raw)[0], expected, raw)

    def test_the_stored_text_is_masked_and_the_person_is_told(self):
        made = self.make(description="When I search for the number 9876543210 nothing comes up")
        self.assertTrue(made["masked"])
        stored = self.conn.execute("SELECT description FROM help_requests").fetchone()[0]
        self.assertEqual(stored, "When I search for the number [number hidden] nothing comes up")
        self.assertNotIn("9876543210", json.dumps(hr.get_request(self.conn, made["id"], None, T0)))

    def test_control_characters_are_dropped_and_newlines_kept(self):
        made = self.make(description="line one is long enough\x00\x07\r\n\r\n\r\n\r\nline two")
        stored = self.conn.execute("SELECT description FROM help_requests WHERE id = ?", (made["id"],)).fetchone()[0]
        self.assertEqual(stored, "line one is long enough\n\nline two")


class SlaStates(HelpCase):
    def test_the_due_soon_rule(self):
        self.assertEqual(hr.due_soon_window_seconds(24), 4.8 * 3600)             # 20% of 24 h
        self.assertEqual(hr.due_soon_window_seconds(5), 2 * 3600)                # 20% is 1 h, so the 2 hour floor wins
        self.assertEqual(hr.due_soon_window_seconds(100), 20 * 3600)

    def test_an_open_request_moves_through_the_states_with_the_clock(self):
        made = self.make()
        view = lambda at: hr.get_request(self.conn, made["id"], None, at)
        due = T0 + timedelta(hours=24)
        self.assertEqual(view(T0)["sla_state"], "on_track")
        self.assertEqual(view(due - timedelta(hours=4, minutes=49))["sla_state"], "on_track")
        self.assertEqual(view(due - timedelta(hours=4, minutes=48))["sla_state"], "due_soon")     # last 20% (4 h 48 min) starts
        self.assertEqual(view(due - timedelta(minutes=1))["sla_state"], "due_soon")
        self.assertEqual(view(due)["sla_state"], "due_soon")                                      # exactly due is not yet late
        self.assertEqual(view(due + timedelta(seconds=1))["sla_state"], "overdue")
        self.assertEqual(view(T0)["seconds_left"], 24 * 3600)
        self.assertEqual(view(due + timedelta(minutes=5))["seconds_left"], -300)

    def test_resolved_is_met_or_breached_by_resolved_at(self):
        a, b = self.make(), self.make(now=T0 + timedelta(minutes=1))
        hr.set_status(self.conn, a["id"], "resolved", None, "team", T0 + timedelta(hours=23, minutes=59))
        hr.set_status(self.conn, b["id"], "resolved", None, "team", T0 + timedelta(hours=26))
        later = T0 + timedelta(days=5)
        self.assertEqual(hr.get_request(self.conn, a["id"], None, later)["sla_state"], "met")
        self.assertEqual(hr.get_request(self.conn, b["id"], None, later)["sla_state"], "breached")
        self.assertIsNone(hr.get_request(self.conn, a["id"], None, later)["seconds_left"])
        # exactly at the due time counts as met
        c = self.make(now=T0 + timedelta(minutes=2))
        hr.set_status(self.conn, c["id"], "resolved", None, "team", T0 + timedelta(minutes=2, hours=24))
        self.assertEqual(hr.get_request(self.conn, c["id"], None, later)["sla_state"], "met")

    def test_the_sla_is_snapshotted_when_the_request_is_sent(self):
        first = self.make()
        settings.set_help_sla_hours(self.conn, 4)
        second = self.make(now=T0 + timedelta(minutes=1))
        settings.set_help_sla_hours(self.conn, 72)
        self.assertEqual((first["sla_hours"], first["sla_due_at"]), (24, "2026-10-06 10:00:00"))
        self.assertEqual((second["sla_hours"], second["sla_due_at"]), (4, "2026-10-05 14:01:00"))
        rows = self.conn.execute("SELECT sla_hours, sla_due_at FROM help_requests ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(24, "2026-10-06 10:00:00"), (4, "2026-10-05 14:01:00")])
        self.assertEqual(hr.config_view(self.conn)["sla_hours"], 72)

    def test_the_setting_is_validated_and_defaults_to_24(self):
        self.assertEqual(settings.help_sla_hours(self.conn), 24)
        for bad in (0, 721, "x", None, True, 1.5, ""):
            with self.assertRaises(ValueError, msg=repr(bad)):
                settings.set_help_sla_hours(self.conn, bad)
        settings.set_help_sla_hours(self.conn, " 48 ")
        self.assertEqual(settings.help_sla_hours(self.conn), 48)
        settings.set_value(self.conn, "help_sla_hours", "garbage")
        self.assertEqual(settings.help_sla_hours(self.conn), 24)


class StatusChanges(HelpCase):
    def test_forward_moves_are_free_and_each_is_an_event(self):
        made = self.make()
        hr.set_status(self.conn, made["id"], "acknowledged", "Seen", "team", T0 + timedelta(hours=1))
        hr.set_status(self.conn, made["id"], "resolved", "Fixed in the update", "team", T0 + timedelta(hours=2))       # skipping a step
        hr.set_status(self.conn, made["id"], "closed", None, "team", T0 + timedelta(hours=3))
        detail = hr.get_request(self.conn, made["id"], None, T0 + timedelta(hours=4))
        self.assertEqual([(e["kind"], e["from_status"], e["to_status"], e["actor"]) for e in detail["events"]][1:],
                         [("status", "new", "acknowledged", "team"), ("status", "acknowledged", "resolved", "team"),
                          ("status", "resolved", "closed", "team")])
        self.assertEqual(detail["events"][1]["note"], "Seen")
        self.assertEqual(detail["resolved_at"], "2026-10-05 12:00:00")             # set when it was resolved, kept when closed

    def test_going_back_is_refused_except_reopening_as_in_progress(self):
        made = self.make()
        hr.set_status(self.conn, made["id"], "in_progress", None, "team", T0)
        for target in ("new", "acknowledged"):
            with self.assertRaises(hr.HelpError):
                hr.set_status(self.conn, made["id"], target, None, "team", T0)
        hr.set_status(self.conn, made["id"], "resolved", None, "team", T0)
        for target in ("new", "acknowledged"):
            with self.assertRaises(hr.HelpError):
                hr.set_status(self.conn, made["id"], target, None, "team", T0)
        reopened = hr.set_status(self.conn, made["id"], "in_progress", "Still broken", "team", T0 + timedelta(hours=1))
        self.assertEqual((reopened["status"], reopened["resolved_at"]), ("in_progress", None))
        hr.set_status(self.conn, made["id"], "closed", None, "team", T0 + timedelta(hours=2))
        with self.assertRaises(hr.HelpError):
            hr.set_status(self.conn, made["id"], "resolved", None, "team", T0)                  # closed cannot be "resolved" again
        self.assertEqual(hr.set_status(self.conn, made["id"], "in_progress", None, "team", T0)["status"], "in_progress")

    def test_same_status_is_an_error_unless_it_adds_a_note(self):
        made = self.make()
        with self.assertRaises(hr.HelpError):
            hr.set_status(self.conn, made["id"], "new", None, "team", T0)
        hr.set_status(self.conn, made["id"], "new", "Asked for a screenshot", "team", T0)
        detail = hr.get_request(self.conn, made["id"], None, T0)
        self.assertEqual([(e["kind"], e["note"]) for e in detail["events"]][-1], ("note", "Asked for a screenshot"))

    def test_bad_inputs(self):
        made = self.make()
        with self.assertRaises(hr.HelpError):
            hr.set_status(self.conn, made["id"], "done", None, "team", T0)
        with self.assertRaises(hr.HelpError):
            hr.set_status(self.conn, 999, "resolved", None, "team", T0)
        with self.assertRaises(hr.HelpError):
            hr.set_status(self.conn, made["id"], "resolved", "x" * 501, "team", T0)
        with self.assertRaises(hr.HelpError):
            hr.set_status(self.conn, made["id"], "resolved", None, "user", T0)
        self.assertEqual(hr.get_request(self.conn, made["id"], None, T0)["status"], "new")

    def test_next_statuses_are_offered_by_the_server(self):
        made = self.make()
        self.assertEqual(hr.get_request(self.conn, made["id"], None, T0)["next_statuses"], ["acknowledged", "in_progress", "resolved", "closed"])
        hr.set_status(self.conn, made["id"], "resolved", None, "team", T0)
        self.assertEqual(hr.get_request(self.conn, made["id"], None, T0)["next_statuses"], ["in_progress", "closed"])


class EventsAreAppendOnly(HelpCase):
    def test_the_database_refuses_to_change_or_remove_an_event(self):
        made = self.make()
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE help_request_events SET note = 'tampered' WHERE request_id = ?", (made["id"],))
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM help_request_events")
        self.assertEqual(self.count("help_request_events"), 1)

    def test_no_code_path_updates_or_deletes_events(self):
        for name in ("help_requests.py", "help_uploads.py"):
            source = (Path(__file__).resolve().parents[1] / "clinic" / name).read_text()
            self.assertNotRegex(source, r"(?i)(UPDATE|DELETE\s+FROM)\s+help_request_events")


class Listing(HelpCase):
    def test_a_person_sees_only_their_own_requests(self):
        mine = self.make()
        theirs = self.make(user="Dr. Rao", now=T0 + timedelta(minutes=1))
        self.assertEqual([r["ticket_no"] for r in hr.list_for_user(self.conn, "Reception (demo)", T0)], [mine["ticket_no"]])
        self.assertIsNone(hr.get_request(self.conn, theirs["id"], "Reception (demo)", T0))
        self.assertIsNotNone(hr.get_request(self.conn, theirs["id"], "Dr. Rao", T0))
        self.assertIsNotNone(hr.get_request(self.conn, theirs["id"], None, T0))               # the team side sees everything

    def test_newest_first_with_attachment_counts(self):
        self.make()
        self.make(now=T0 + timedelta(minutes=1), files=[FakeUpload("a.png", PNG), FakeUpload("b.pdf", PDF)])
        rows = hr.list_for_user(self.conn, "Reception (demo)", T0)
        self.assertEqual([(r["ticket_no"], r["attachment_count"]) for r in rows], [("HELP-0002", 2), ("HELP-0001", 0)])

    def test_team_filters_and_summary(self):
        a = self.make(category="voice_assistant")
        b = self.make(category="other", now=T0 + timedelta(minutes=1))
        c = self.make(category="other", now=T0 + timedelta(minutes=2))
        hr.set_status(self.conn, b["id"], "in_progress", None, "team", T0)
        hr.set_status(self.conn, c["id"], "resolved", None, "team", T0 + timedelta(days=2))
        now = T0 + timedelta(days=3)          # a and b are overdue, c was resolved after its due time
        self.assertEqual([r["ticket_no"] for r in hr.team_list(self.conn, now=now)], ["HELP-0003", "HELP-0002", "HELP-0001"])
        self.assertEqual([r["ticket_no"] for r in hr.team_list(self.conn, status="new", now=now)], ["HELP-0001"])
        self.assertEqual([r["ticket_no"] for r in hr.team_list(self.conn, category="other", now=now)], ["HELP-0003", "HELP-0002"])
        self.assertEqual([r["ticket_no"] for r in hr.team_list(self.conn, overdue=True, now=now)], ["HELP-0002", "HELP-0001"])
        self.assertEqual(hr.team_list(self.conn, overdue=True, now=T0), [])
        summary = hr.team_summary(self.conn, now)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["by_status"], {"new": 1, "acknowledged": 0, "in_progress": 1, "resolved": 1, "closed": 0})
        self.assertEqual(summary["by_sla"], {"on_track": 0, "due_soon": 0, "overdue": 2, "met": 0, "breached": 1})
        self.assertEqual({c["key"]: c["count"] for c in summary["by_category"]},
                         {"voice_assistant": 1, "other": 2, "patients_data": 0, "language_understanding": 0})
        with self.assertRaises(hr.HelpError):
            hr.team_list(self.conn, status="bogus", now=now)
        del a


class RateLimit(HelpCase):
    def test_ten_an_hour_per_username_with_a_clear_error(self):
        for n in range(10):
            self.make(now=T0 + timedelta(minutes=n))
        with self.assertRaises(hr.HelpError) as caught:
            self.make(now=T0 + timedelta(minutes=30))
        error = caught.exception
        self.assertEqual((error.status, error.code), (429, "rate_limited"))
        self.assertIn("10 requests in the last hour", str(error))
        self.assertIn("about 11:00", str(error))
        self.assertEqual(error.extra["retry_after"], "2026-10-05 11:00:00")           # when the oldest one leaves the hour
        self.assertEqual(self.count("help_requests"), 10)
        self.make(user="Dr. Rao", now=T0 + timedelta(minutes=30))                   # someone else is not limited
        self.make(now=T0 + timedelta(hours=1, seconds=1))                            # an hour after the first one: allowed again

    def test_a_refused_request_leaves_no_file(self):
        for n in range(10):
            self.make(now=T0 + timedelta(minutes=n))
        with self.assertRaises(hr.HelpError):
            self.make(now=T0 + timedelta(minutes=30), files=[FakeUpload("a.png", PNG)])
        self.assertEqual(self.files_on_disk(), [])
        self.assertEqual([p.name for p in (self.root / ".staging").iterdir()], [])


class ImportingUpdates(HelpCase):
    def setUp(self):
        super().setUp()
        self.a = self.make()
        self.b = self.make(now=T0 + timedelta(minutes=1))

    def test_csv_and_json_are_read(self):
        csv_rows = hr.parse_updates("ticket_no,status,note\nHELP-0001,in progress,On it\nHELP-1,Resolved,\n")
        self.assertEqual(csv_rows[0], {"ticket_no": "HELP-0001", "status": "in progress", "note": "On it"})
        json_rows = hr.parse_updates(b'\xef\xbb\xbf[{"ticket_no": "HELP-0001", "status": "resolved"}]')
        self.assertEqual(json_rows, [{"ticket_no": "HELP-0001", "status": "resolved"}])
        self.assertEqual(hr.parse_updates('{"updates": [{"ticket_no": 2, "status": "closed"}]}'), [{"ticket_no": 2, "status": "closed"}])
        for broken in ("", "   ", "[1, 2", "just text", "ticket,state\n1,2", '{"nope": 1}', b"\xff\xfe\x00"):
            with self.assertRaises(hr.HelpError, msg=repr(broken)):
                hr.parse_updates(broken)

    def test_import_changes_statuses_and_records_events_as_import(self):
        rows = [{"ticket_no": "HELP-0001", "status": "in progress", "note": "Looking"}, {"ticket_no": "help-2", "status": "RESOLVED", "note": "Fixed"}]
        result = hr.import_updates(self.conn, rows, T0 + timedelta(hours=1))
        self.assertEqual((result["rows"], result["updated"], result["unchanged"], result["stale"]), (2, 2, 0, 0))
        a = hr.get_request(self.conn, self.a["id"], None, T0)
        self.assertEqual(a["status"], "in_progress")
        self.assertEqual([(e["actor"], e["to_status"], e["note"]) for e in a["events"]][-1], ("import", "in_progress", "Looking"))
        self.assertEqual(hr.get_request(self.conn, self.b["id"], None, T0)["status"], "resolved")

    def test_importing_the_same_file_twice_changes_nothing_the_second_time(self):
        rows = [{"ticket_no": "HELP-0001", "status": "acknowledged", "note": "Seen"}, {"ticket_no": "HELP-0002", "status": "resolved", "note": "Fixed"}]
        first = hr.import_updates(self.conn, rows, T0 + timedelta(hours=1))
        events_before = self.count("help_request_events")
        snapshot = [tuple(r) for r in self.conn.execute("SELECT status, resolved_at, notified_at FROM help_requests ORDER BY id")]
        second = hr.import_updates(self.conn, rows, T0 + timedelta(hours=5))
        self.assertEqual((first["updated"], second["updated"], second["unchanged"]), (2, 0, 2))
        self.assertEqual(self.count("help_request_events"), events_before)
        self.assertEqual([tuple(r) for r in self.conn.execute("SELECT status, resolved_at, notified_at FROM help_requests ORDER BY id")], snapshot)

    def test_a_replayed_older_file_is_skipped_not_applied(self):
        hr.import_updates(self.conn, [{"ticket_no": "HELP-0001", "status": "acknowledged"}], T0)
        hr.import_updates(self.conn, [{"ticket_no": "HELP-0001", "status": "resolved", "note": "Done"}], T0)
        replay = hr.import_updates(self.conn, [{"ticket_no": "HELP-0001", "status": "acknowledged"}], T0 + timedelta(hours=9))
        self.assertEqual((replay["updated"], replay["stale"]), (0, 1))
        self.assertEqual(hr.get_request(self.conn, self.a["id"], None, T0)["status"], "resolved")

    def test_a_note_on_the_same_status_is_added_once(self):
        rows = [{"ticket_no": "HELP-0001", "status": "new", "note": "Please send a screenshot"}]
        self.assertEqual(hr.import_updates(self.conn, rows, T0)["notes_added"], 1)
        self.assertEqual(hr.import_updates(self.conn, rows, T0)["notes_added"], 0)
        self.assertEqual(hr.import_updates(self.conn, [{"ticket_no": "HELP-0001", "status": "new", "note": "Second note"}], T0)["notes_added"], 1)

    def test_unknown_tickets_and_bad_rows_are_reported_never_fatal(self):
        rows = [{"ticket_no": "HELP-0099", "status": "resolved"}, {"ticket_no": "HELP-0099", "status": "closed"},
                {"ticket_no": "banana", "status": "resolved"}, {"ticket_no": "HELP-0001", "status": "finished"},
                {"ticket_no": "HELP-0001", "status": "resolved", "note": "x" * 600}, "not an object",
                {"ticket_no": "HELP-0002", "status": "closed", "note": "ok"}]
        result = hr.import_updates(self.conn, rows, T0)
        self.assertEqual(result["unknown_tickets"], ["HELP-0099"])
        self.assertEqual([i["row"] for i in result["invalid"]], [3, 4, 5, 6])
        self.assertEqual((result["rows"], result["updated"]), (7, 1))
        self.assertEqual(hr.get_request(self.conn, self.b["id"], None, T0)["status"], "closed")
        self.assertEqual(hr.get_request(self.conn, self.a["id"], None, T0)["status"], "new")

    def test_ticket_and_status_spellings(self):
        for raw, expected in (("HELP-0007", "HELP-0007"), ("help-7", "HELP-0007"), (" 7 ", "HELP-0007"), ("HELP 7", "HELP-0007"),
                              (7, "HELP-0007"), ("HELP-", None), ("x", None), (None, None), ("", None)):
            self.assertEqual(hr.normalise_ticket(raw), expected, raw)
        for raw, expected in (("in_progress", "in_progress"), ("In-Progress", "in_progress"), ("in progress", "in_progress"),
                              ("Resolved", "resolved"), ("done", None), (None, None)):
            self.assertEqual(hr.normalise_status(raw), expected, raw)

    def test_resolved_through_import_starts_a_notice_and_reopening_clears_it(self):
        hr.import_updates(self.conn, [{"ticket_no": "HELP-0001", "status": "resolved", "note": "Fixed in the 12 Oct update"}], T0)
        notices = hr.pending_notices(self.conn, "Reception (demo)")
        self.assertEqual([(n["ticket_no"], n["note"]) for n in notices], [("HELP-0001", "Fixed in the 12 Oct update")])
        self.assertEqual(hr.notice_text(notices[0]), "Your request HELP-0001 has been resolved. Fixed in the 12 Oct update.")
        self.assertEqual(hr.pending_notices(self.conn, "Dr. Rao"), [])


class Notices(HelpCase):
    def test_a_resolved_request_is_announced_once_to_its_owner(self):
        made = self.make()
        self.assertEqual(hr.pending_notices(self.conn, "Reception (demo)"), [])
        hr.set_status(self.conn, made["id"], "resolved", "Done", "team", T0 + timedelta(hours=1))
        self.assertEqual(len(hr.pending_notices(self.conn, "Reception (demo)")), 1)
        self.assertFalse(hr.mark_notified(self.conn, made["id"], "Dr. Rao", T0))             # someone else cannot tell it
        self.assertTrue(hr.mark_notified(self.conn, made["id"], "Reception (demo)", T0 + timedelta(hours=2)))
        self.assertFalse(hr.mark_notified(self.conn, made["id"], "Reception (demo)", T0))
        self.assertEqual(hr.pending_notices(self.conn, "Reception (demo)"), [])
        hr.set_status(self.conn, made["id"], "closed", None, "team", T0 + timedelta(hours=3))      # closing is not a new notice
        self.assertEqual(hr.pending_notices(self.conn, "Reception (demo)"), [])

    def test_reopened_then_resolved_again_is_announced_again(self):
        made = self.make()
        hr.set_status(self.conn, made["id"], "resolved", None, "team", T0)
        hr.mark_notified(self.conn, made["id"], "Reception (demo)", T0)
        hr.set_status(self.conn, made["id"], "in_progress", None, "team", T0)
        hr.set_status(self.conn, made["id"], "resolved", "Really fixed", "team", T0)
        self.assertEqual([n["note"] for n in hr.pending_notices(self.conn, "Reception (demo)")], ["Really fixed"])

    def test_closing_without_resolving_is_not_announced(self):
        made = self.make()
        hr.set_status(self.conn, made["id"], "closed", "Not something we can change", "team", T0)
        self.assertEqual(hr.pending_notices(self.conn, "Reception (demo)"), [])


class Exporting(HelpCase):
    def setUp(self):
        super().setUp()
        self.a = self.make(description="=HYPERLINK(\"http://x\") the screen is slow", files=[FakeUpload("shot one.png", PNG)])
        self.b = self.make(description="The calendar is hard to read in the sun", now=T0 + timedelta(days=1, minutes=1),
                           category="patients_data")
        hr.set_status(self.conn, self.b["id"], "in_progress", "Looking", "team", T0 + timedelta(days=2))

    def open_zip(self, **kw):
        bundle, count = hr.export_bundle(self.conn, self.root, now=T0 + timedelta(days=3), **kw)
        self.addCleanup(bundle.close)
        return zipfile.ZipFile(bundle), count

    def test_the_bundle_has_csv_json_readme_and_the_files(self):
        archive, count = self.open_zip()
        self.assertEqual(count, 2)
        names = archive.namelist()
        self.assertEqual(sorted(n for n in names if "/" not in n), ["README.txt", "requests.csv", "requests.json"])
        self.assertIn("attachments/HELP-0001/01_shot one.png", names)
        self.assertEqual(archive.read("attachments/HELP-0001/01_shot one.png"), PNG)
        rows = list(csv.DictReader(io.StringIO(archive.read("requests.csv").decode("utf-8-sig"))))
        self.assertEqual([r["ticket_no"] for r in rows], ["HELP-0001", "HELP-0002"])
        self.assertEqual(rows[1]["category"], "Patients data")
        self.assertEqual(rows[1]["status"], "in_progress")
        data = json.loads(archive.read("requests.json"))
        self.assertEqual([r["ticket_no"] for r in data["requests"]], ["HELP-0001", "HELP-0002"])
        self.assertEqual([e["kind"] for e in data["requests"][1]["events"]], ["created", "status"])
        readme = archive.read("README.txt").decode()
        for needle in ("ticket_no,status,note", "in_progress", "Importing is safe to repeat", "HELP-0001"):
            self.assertIn(needle, readme)

    def test_a_cell_that_looks_like_a_formula_is_defused_in_the_csv_only(self):
        archive, _ = self.open_zip()
        rows = list(csv.DictReader(io.StringIO(archive.read("requests.csv").decode("utf-8-sig"))))
        self.assertTrue(rows[0]["description"].startswith("'=HYPERLINK"))
        self.assertTrue(json.loads(archive.read("requests.json"))["requests"][0]["description"].startswith("=HYPERLINK"))

    def test_filters(self):
        self.assertEqual(self.open_zip(status="new")[1], 1)
        self.assertEqual(self.open_zip(status="in_progress")[1], 1)
        self.assertEqual(self.open_zip(since="2026-10-06")[1], 1)
        self.assertEqual(self.open_zip(since="2026-10-05 10:00")[1], 2)
        self.assertEqual(self.open_zip(since="2026-10-07")[1], 0)
        for bad in ({"status": "bogus"}, {"since": "yesterday"}):
            with self.assertRaises(hr.HelpError):
                self.open_zip(**bad)

    def test_nothing_is_stamped_unless_asked_so_an_export_can_be_repeated(self):
        self.open_zip()
        self.open_zip(only_unexported=True)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM help_requests WHERE exported_at IS NOT NULL").fetchone()[0], 0)
        self.assertEqual(self.open_zip(only_unexported=True)[1], 2)
        self.assertEqual(self.open_zip(only_unexported=True, mark_exported=True)[1], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM help_requests WHERE exported_at IS NOT NULL").fetchone()[0], 2)
        self.assertEqual(self.open_zip(only_unexported=True)[1], 0)                      # "new since the last export"
        self.assertEqual(self.open_zip()[1], 2)                                          # everything is still exportable
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM help_request_events WHERE request_id = 1 ORDER BY id")]
        self.assertEqual(kinds, ["created", "exported"])
        self.open_zip(mark_exported=True)                                                # a second stamp does not repeat the event
        self.assertEqual(self.count("help_request_events"), 5)

    def test_a_missing_file_is_noted_not_fatal(self):
        for path in self.root.rglob("*"):
            if path.is_file():
                path.unlink()
        archive, count = self.open_zip()
        self.assertEqual(count, 2)
        data = json.loads(archive.read("requests.json"))
        self.assertTrue(data["requests"][0]["attachments"][0]["file_missing"])


class Uploads(HelpCase):
    def stage(self, *uploads):
        return hu.stage_files(list(uploads), self.root)

    def refused(self, upload, fragment=None):
        with self.assertRaises(hu.UploadError) as caught:
            self.stage(upload)
        if fragment:
            self.assertIn(fragment, str(caught.exception))
        self.assertEqual(self.files_on_disk(), [])
        self.assertEqual([p.name for p in (self.root / ".staging").iterdir()], [])         # the staging folder is gone too

    def test_every_allowed_type_is_accepted_by_its_content(self):
        samples = {
            "a.png": (PNG, "image/png"), "a.JPG": (b"\xff\xd8\xff\xe0" + b"0" * 20, "image/jpeg"),
            "a.jpeg": (b"\xff\xd8\xff\xdb" + b"0" * 20, "image/jpeg"), "a.gif": (b"GIF89a" + b"0" * 20, "image/gif"),
            "a.webp": (b"RIFF\x10\x00\x00\x00WEBPVP8 ", "image/webp"), "a.heic": (b"\x00\x00\x00\x18ftypheic" + b"0" * 20, "image/heic"),
            "a.pdf": (PDF, "application/pdf"), "a.txt": ("naive text, Hindi: नमस्ते\n".encode(), "text/plain"),
            "a.csv": (b"a,b\n1,2\n", "text/csv"),
            "a.docx": (office_zip("docx"), hu._MIME["docx"]), "a.xlsx": (office_zip("xlsx"), hu._MIME["xlsx"]),
        }
        for name, (data, mime) in samples.items():
            staged = self.stage(FakeUpload(name, data))
            self.assertEqual([f["mime"] for f in staged.files], [mime], name)
            staged.discard()

    def test_the_stored_file_has_a_random_name_and_the_hash_and_size_of_its_content(self):
        import hashlib
        made = self.make(files=[FakeUpload("../../etc/passwd.png", PNG)])
        attachment = self.conn.execute("SELECT * FROM help_attachments").fetchone()
        self.assertRegex(attachment["stored_name"], r"^[0-9a-f]{32}$")
        self.assertEqual(attachment["original_name"], "passwd.png")                       # the folder part is dropped
        self.assertEqual((attachment["bytes"], attachment["sha256"]), (len(PNG), hashlib.sha256(PNG).hexdigest()))
        stored = self.root / made["ticket_no"] / attachment["stored_name"]
        self.assertEqual(stored.read_bytes(), PNG)
        self.assertEqual(self.files_on_disk(), ["{}/{}".format(made["ticket_no"], attachment["stored_name"])])

    def test_names_are_cleaned_for_display_and_never_used_on_disk(self):
        cases = {"C:\\Users\\x\\shot.PNG": "shot.png", "a/b/c.pdf": "c.pdf", "we\x00ird\nname.txt": "we_ird_name.txt",
                 "q?u*o\"t<e>.csv": "q_u_o_t_e_.csv", ".png": "png", "x" * 300 + ".png": "x" * 96 + ".png"}
        for raw, expected in cases.items():
            self.assertEqual(hu.display_name(raw), expected, raw)

    def test_extensions_that_are_not_allowed_are_refused(self):
        for name in ("movie.mp4", "movie.mov", "x.svg", "x.html", "x.htm", "x.js", "x.exe", "x.zip", "x.docm", "x.xlsm", "x.sh", "x.php",
                     "x.pdf.exe", "noextension", "x.", "x.png.svg", "x.heif", "x.tiff", "x.rar", "x.7z", "x.ppt", "x.pptx", "x.doc", "x.xls"):
            self.refused(FakeUpload(name, PNG), "cannot be attached")

    def test_content_that_does_not_match_the_extension_is_refused(self):
        wrong = {"a.png": b"GIF89a....", "a.jpg": PNG, "a.pdf": PNG, "a.gif": PNG, "a.webp": b"RIFF\x00\x00\x00\x00WAVEfmt ",
                 "a.heic": PNG, "a.png.png": b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
                 "a.pdf ": b"<html><script>alert(1)</script></html>", "b.jpeg": b"MZ\x90\x00" + b"0" * 30, "c.png": b"#!/bin/sh\nrm -rf /"}
        for name, data in wrong.items():
            self.refused(FakeUpload(name, data), "real .")

    def test_text_files_must_be_clean_utf8(self):
        self.refused(FakeUpload("a.txt", b"hello\x00world"), "real .txt")
        self.refused(FakeUpload("a.csv", b"\xff\xfe\x00a\x00"), "real .csv")
        self.refused(FakeUpload("a.txt", b"caf\xe9 in latin-1"), "real .txt")
        self.refused(FakeUpload("a.txt", b"escape \x1b[31m codes"), "real .txt")
        self.refused(FakeUpload("a.csv", bytes(range(256))), "real .csv")
        ok = self.stage(FakeUpload("a.txt", "tab\tand\nnewline\r\nand form feed \x0c ok".encode()))
        ok.discard()

    def test_office_files_must_be_plain_zips_of_the_right_kind(self):
        self.refused(FakeUpload("a.docx", office_zip("docx", extra=["word/vbaProject.bin"])), "macros")
        self.refused(FakeUpload("a.docx", office_zip("docx", extra=["WORD/VBAPROJECT.BIN"])), "macros")
        self.refused(FakeUpload("a.xlsx", office_zip("xlsx", extra=["xl/vbaProject.bin"])), "macros")
        self.refused(FakeUpload("a.xlsx", office_zip("xlsx", content_types=b"<Override ContentType='application/vnd.ms-excel.sheet.macroEnabled.main+xml'/>")), "macros")
        self.refused(FakeUpload("a.docx", office_zip("docx", skip_types=True)), "not a docx")
        self.refused(FakeUpload("a.docx", office_zip("xlsx")), "not a docx")            # an xlsx renamed to docx
        self.refused(FakeUpload("a.xlsx", office_zip("docx")), "not a xlsx")
        self.refused(FakeUpload("a.docx", b"PK\x03\x04 not really a zip"), "not a docx")
        self.refused(FakeUpload("a.docx", office_zip("docx", extra=["../evil.txt"])), "unsafe")
        plain_zip = io.BytesIO()
        with zipfile.ZipFile(plain_zip, "w") as z:
            z.writestr("run.exe", "MZ")
        self.refused(FakeUpload("a.docx", plain_zip.getvalue()), "not a docx")

    def test_empty_files_are_refused(self):
        self.refused(FakeUpload("a.png", b""), "empty")

    def test_limits_are_enforced_while_streaming(self):
        class Endless:
            filename = "huge.png"

            def __init__(self):
                self.read_bytes = 0
                self.stream = self

            def read(self, n=-1):
                self.read_bytes += n
                return PNG[:8] + b"\x00" * (n - 8) if self.read_bytes <= n else b"\x00" * n

        endless = Endless()
        with self.assertRaises(hu.UploadTooLarge):
            self.stage(endless)
        self.assertLess(endless.read_bytes, hu.MAX_FILE_BYTES + 3 * 65536)               # it stopped as soon as the limit was crossed
        self.assertEqual(self.files_on_disk(), [])
        # exactly the limit is fine, one byte more is not
        at_limit = PNG + b"\x00" * (hu.MAX_FILE_BYTES - len(PNG))
        self.stage(FakeUpload("a.png", at_limit)).discard()
        self.refused(FakeUpload("a.png", at_limit + b"\x00"), "10 MB")

    def test_count_and_total_limits(self):
        six = [FakeUpload("f{}.png".format(n), PNG) for n in range(6)]
        with self.assertRaises(hu.UploadError) as caught:
            self.stage(*six)
        self.assertIn("at most 5", str(caught.exception))
        self.assertEqual(self.files_on_disk(), [])
        self.stage(*six[:5]).discard()
        big = PNG + b"\x00" * (9 * 1024 * 1024)
        with self.assertRaises(hu.UploadTooLarge) as caught:
            self.stage(*[FakeUpload("f{}.png".format(n), big) for n in range(3)])        # 3 x 9 MB > 25 MB
        self.assertIn("25 MB", str(caught.exception))
        self.assertEqual(self.files_on_disk(), [])
        self.assertEqual([p.name for p in (self.root / ".staging").iterdir()], [])

    def test_one_bad_file_discards_the_good_ones_too(self):
        with self.assertRaises(hu.UploadError):
            self.stage(FakeUpload("good.png", PNG), FakeUpload("bad.exe", b"MZ"))
        self.assertEqual(self.files_on_disk(), [])
        with self.assertRaises(hu.UploadError):
            self.stage(FakeUpload("good.png", PNG), FakeUpload("liar.pdf", PNG))
        self.assertEqual(self.files_on_disk(), [])

    def test_unnamed_parts_are_ignored(self):
        staged = self.stage(FakeUpload("", b""), FakeUpload("a.png", PNG))
        self.assertEqual(len(staged.files), 1)
        staged.discard()
        self.assertEqual(self.stage(FakeUpload("", b"")).files, [])

    def test_stored_path_refuses_anything_that_could_leave_the_folder(self):
        made = self.make(files=[FakeUpload("a.png", PNG)])
        stored = self.conn.execute("SELECT stored_name FROM help_attachments").fetchone()[0]
        self.assertIsNotNone(hu.stored_path(made["ticket_no"], stored, self.root))
        for ticket, name in (("../HELP-0001", stored), ("HELP-0001/..", stored), ("HELP-0001", "../" + stored), ("HELP-0001", stored + "/.."),
                             ("HELP-0001", "/etc/passwd"), ("HELP-0001", "..%2f.."), ("..", ".."), ("", ""), (None, None),
                             ("HELP-0001", stored.upper()), ("help-0001", stored), ("HELP-0001\x00", stored), ("HELP-0002", stored)):
            self.assertIsNone(hu.stored_path(ticket, name, self.root), (ticket, name))

    def test_a_symlink_pointing_out_of_the_folder_is_not_served(self):
        made = self.make(files=[FakeUpload("a.png", PNG)])
        stored = self.conn.execute("SELECT stored_name FROM help_attachments").fetchone()[0]
        outside = Path(self.tmp.name) / "secret.txt"
        outside.write_text("secret")
        target = self.root / made["ticket_no"] / stored
        target.unlink()
        target.symlink_to(outside)
        self.assertIsNone(hu.stored_path(made["ticket_no"], stored, self.root))

    def test_the_upload_folder_comes_from_the_environment(self):
        old = os.environ.get("HELP_UPLOAD_DIR")
        os.environ["HELP_UPLOAD_DIR"] = str(self.root)
        try:
            self.assertEqual(hu.upload_root(), self.root)
            os.environ.pop("HELP_UPLOAD_DIR")
            self.assertEqual(hu.upload_root(), Path(hu.__file__).resolve().parent.parent / "data" / "help_uploads")
        finally:
            if old is not None:
                os.environ["HELP_UPLOAD_DIR"] = old
            else:
                os.environ.pop("HELP_UPLOAD_DIR", None)

    def test_an_orphan_folder_from_a_crash_does_not_block_the_next_ticket(self):
        (self.root / "HELP-0001").mkdir(parents=True)
        (self.root / "HELP-0001" / ("0" * 32)).write_bytes(b"left over")
        made = self.make(files=[FakeUpload("a.png", PNG)])
        self.assertEqual(made["ticket_no"], "HELP-0001")
        self.assertEqual(len(self.files_on_disk()), 1)
        self.assertNotIn("0" * 32, "".join(self.files_on_disk()))

    def test_old_staging_folders_are_swept(self):
        stale = self.root / ".staging" / "abandoned"
        stale.mkdir(parents=True)
        (stale / "x").write_bytes(b"x")
        old = datetime.now().timestamp() - 3 * 86400
        os.utime(str(stale), (old, old))
        self.stage(FakeUpload("a.png", PNG)).discard()
        self.assertFalse(stale.exists())


class AllOrNothing(HelpCase):
    def test_a_database_failure_after_the_files_were_moved_removes_them_and_the_row(self):
        from unittest import mock
        real = hr._event

        def explode(conn, request_id, *a, **k):
            real(conn, request_id, *a, **k)
            raise sqlite3.OperationalError("disk I/O error")

        with mock.patch.object(hr, "_event", side_effect=explode):
            with self.assertRaises(sqlite3.OperationalError):
                self.make(files=[FakeUpload("a.png", PNG), FakeUpload("b.pdf", PDF)])
        self.assertEqual((self.count("help_requests"), self.count("help_attachments"), self.count("help_request_events")), (0, 0, 0))
        self.assertEqual(self.files_on_disk(), [])
        self.assertEqual([p.name for p in (self.root / ".staging").iterdir()], [])
        self.assertEqual(self.make()["ticket_no"], "HELP-0001")                           # and the number was not burned

    def test_a_failure_while_publishing_the_folder_leaves_nothing(self):
        from unittest import mock
        with mock.patch.object(hu, "publish_folder", side_effect=OSError("rename failed")):
            with self.assertRaises(OSError):
                self.make(files=[FakeUpload("a.png", PNG)])
        self.assertEqual(self.count("help_requests"), 0)
        self.assertEqual(self.files_on_disk(), [])
        self.assertEqual([p.name for p in (self.root / ".staging").iterdir()], [])

    def test_a_commit_failure_after_publishing_removes_the_moved_files(self):
        from unittest import mock

        class FailingCommit:
            """Delegates to the real connection but fails the commit."""

            def __init__(self, inner):
                self.inner = inner

            def commit(self):
                raise sqlite3.OperationalError("commit failed")

            def __getattr__(self, name):
                return getattr(self.inner, name)

        staged = hu.stage_files([FakeUpload("a.png", PNG)], self.root)
        with self.assertRaises(sqlite3.OperationalError):
            hr.create_request(FailingCommit(self.conn), "Reception (demo)", "other", "The booking screen is confusing", staged=staged,
                              now=T0, root=self.root)
        self.assertEqual(self.files_on_disk(), [])
        self.assertEqual(self.count("help_requests"), 0)


if __name__ == "__main__":
    unittest.main()
