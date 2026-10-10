"""The /help/* routes (and the Settings card's route) through the Flask test client: a temp database and a temp upload
folder, a fixed clock, no WhatsApp/Sarvam/Meta/model call. Uploads are the new attack surface, so most of this file
is about what must be refused and what must be left behind (nothing)."""
import csv
import io
import json
import os
import sys
import unittest
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_app_routes import RouteTestCase, clinic_app  # noqa: E402  (stubs load_dotenv, never reads .env)
from tests.test_help_requests import PDF, PNG, office_zip  # noqa: E402

from clinic import help_requests as hr, help_uploads as hu  # noqa: E402

T0 = datetime(2026, 10, 5, 10, 0, 0)
GOOD = "The booking screen is confusing and slow to use"


class HelpRouteCase(RouteTestCase):
    def setUp(self):
        super().setUp()
        self.uploads = Path(self.tmp.name).resolve() / "help_uploads"
        env = mock.patch.dict(os.environ, {"HELP_UPLOAD_DIR": str(self.uploads)})
        env.start()
        self.addCleanup(env.stop)
        real_get = self.client.get
        self.client.get = lambda *args, **kwargs: real_get(*args, buffered=True, **kwargs)      # closes download files
        self.now = T0
        clinic_app.CLOCK = lambda: self.now
        self.addCleanup(setattr, clinic_app, "CLOCK", None)

    # -- helpers --------------------------------------------------------------------------------
    def send(self, files=(), description=GOOD, category="other", **fields):
        data = {"category": category, "description": description}
        data.update(fields)
        data = {k: v for k, v in data.items() if v is not None}
        if files:
            data["files"] = [(io.BytesIO(content), name) for name, content in files]
        return self.client.post("/help/requests", data=data, content_type="multipart/form-data")

    def sent(self, *args, **kwargs):
        response = self.send(*args, **kwargs)
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()["data"]

    def on_disk(self):
        return sorted(str(p.relative_to(self.uploads)) for p in self.uploads.rglob("*") if p.is_file()) if self.uploads.exists() else []

    def rows(self, table="help_requests"):
        return self.count(table)

    def assert_nothing_kept(self):
        self.assertEqual((self.rows(), self.rows("help_attachments"), self.rows("help_request_events")), (0, 0, 0))
        self.assertEqual(self.on_disk(), [])
        staging = self.uploads / ".staging"
        self.assertEqual([p.name for p in staging.iterdir()] if staging.exists() else [], [])

    def team_post(self, request_id, status, note=None):
        return self.client.post("/help/team/requests/{}/status".format(request_id), json={"status": status, "note": note})


class ConfigAndCreate(HelpRouteCase):
    def test_config_has_everything_the_form_needs(self):
        data = self.client.get("/help/config").get_json()["data"]
        self.assertEqual([c["key"] for c in data["categories"]], ["voice_assistant", "patients_data", "language_understanding", "other"])
        self.assertEqual([s["key"] for s in data["severities"]], ["minor", "annoying", "blocks_work"])
        self.assertEqual(data["default_severity"], "minor")
        self.assertEqual((data["sla_hours"], data["username"]), (24, "Reception (demo)"))
        self.assertIn("You do not need to report errors", data["disclaimer"])
        self.assertIn("Please do not mention patient names", data["disclaimer"])
        limits = data["limits"]
        self.assertEqual((limits["max_files"], limits["max_file_bytes"], limits["max_total_bytes"]), (5, 10 * 1024 * 1024, 25 * 1024 * 1024))
        self.assertEqual(limits["image_extensions"], ["png", "jpg", "jpeg", "webp", "gif", "heic"])
        self.assertEqual(limits["document_extensions"], ["pdf", "txt", "csv", "docx", "xlsx"])
        self.assertEqual((limits["min_description"], limits["max_description"]), (10, 4000))

    def test_a_request_with_an_image_and_a_pdf(self):
        data = self.sent(files=[("shot.png", PNG), ("notes.pdf", PDF)], severity="annoying", source="mixed",
                         context=json.dumps({"tab": "help", "screen": "queue", "language": "en-IN", "branch": "all"}))
        self.assertEqual(data["ticket_no"], "HELP-0001")
        self.assertEqual((data["category"], data["category_label"], data["status"], data["username"]), ("other", "Other", "new", "Reception (demo)"))
        self.assertEqual((data["created_at"], data["sla_hours"], data["sla_due_at"], data["sla_state"]),
                         ("2026-10-05 10:00:00", 24, "2026-10-06 10:00:00", "on_track"))
        self.assertEqual([(a["original_name"], a["mime"]) for a in data["attachments"]], [("shot.png", "image/png"), ("notes.pdf", "application/pdf")])
        self.assertFalse(data["masked"])
        self.assertEqual(len(self.on_disk()), 2)
        detail = self.client.get("/help/requests/{}".format(data["id"])).get_json()["request"]
        self.assertEqual(detail["context"], {"tab": "help", "screen": "queue", "language": "en-IN", "branch": "all"})
        self.assertEqual((detail["severity"], detail["source"]), ("annoying", "mixed"))

    def test_the_username_always_comes_from_the_server(self):
        data = self.sent(username="Dr. Hacker", user="admin")
        self.assertEqual(data["username"], "Reception (demo)")
        self.assertEqual(self.conn.execute("SELECT username FROM help_requests").fetchone()[0], "Reception (demo)")
        with mock.patch.object(hr, "current_user", return_value={"username": "Dr. Rao"}):
            self.assertEqual(self.sent()["username"], "Dr. Rao")
            self.assertEqual(self.client.get("/help/requests").get_json()["requests"][0]["username"], "Dr. Rao")

    def test_numbers_are_hidden_and_the_response_says_so(self):
        data = self.sent(description="When I type 9876543210 into the search nothing is found")
        self.assertTrue(data["masked"])
        self.assertEqual(self.conn.execute("SELECT description FROM help_requests").fetchone()[0],
                         "When I type [number hidden] into the search nothing is found")

    def test_validation_errors_are_400_json_and_store_nothing(self):
        for kwargs in (dict(category="nope"), dict(category=None), dict(description=""), dict(description="short"), dict(description="x" * 4001),
                       dict(severity="huge"), dict(source="telepathy")):
            response = self.send(**kwargs)
            self.assertEqual(response.status_code, 400, kwargs)
            body = response.get_json()
            self.assertFalse(body["ok"])
            self.assertTrue(body["error"])
        self.assert_nothing_kept()

    def test_a_realistic_image_over_64_kb_uploads(self):
        # a real screenshot is incompressible and well over the parser's 64 KB read size; once refused as "too large"
        photo = PNG + os.urandom(700 * 1024)
        mac_name = "Screenshot 2026-10-09 at 2.54.56\u202fPM.png"
        data = self.sent(files=[(mac_name, photo)])
        self.assertEqual(data["ticket_no"], "HELP-0001")
        big = PNG + os.urandom(9 * 1024 * 1024)
        self.assertEqual(self.send(files=[("big.png", big), ("small.png", photo)]).status_code, 201)

    def test_an_attachment_makes_a_short_description_enough(self):
        self.assertEqual(self.send(files=[("a.png", PNG)], description="slow").status_code, 201)
        self.assertEqual(self.send(files=[("a.png", PNG)], description="", ).status_code, 400)

    def test_a_malformed_context_is_ignored(self):
        data = self.sent(context="{not json")
        self.assertEqual(self.client.get("/help/requests/{}".format(data["id"])).get_json()["request"]["context"], {})

    def test_the_rate_limit_is_a_clear_429(self):
        for n in range(10):
            self.now = T0 + timedelta(minutes=n)
            self.sent()
        self.now = T0 + timedelta(minutes=20)
        response = self.send(files=[("a.png", PNG)])
        self.assertEqual(response.status_code, 429)
        body = response.get_json()
        self.assertEqual(body["code"], "rate_limited")
        self.assertIn("10 requests in the last hour", body["error"])
        self.assertEqual(self.rows(), 10)
        self.assertEqual(self.on_disk(), [])
        self.now = T0 + timedelta(hours=1, minutes=1)
        self.assertEqual(self.send().status_code, 201)


class UploadAttacks(HelpRouteCase):
    def test_types_that_are_not_allowed_are_refused_with_nothing_left_behind(self):
        for name, content in (("clip.mp4", b"\x00\x00\x00\x18ftypmp42"), ("clip.mov", b"\x00\x00\x00\x14ftypqt  "), ("logo.svg", b"<svg/>"),
                              ("page.html", b"<html></html>"), ("run.js", b"alert(1)"), ("tool.exe", b"MZ\x90\x00"), ("pack.zip", b"PK\x03\x04"),
                              ("macro.docm", office_zip("docx")), ("macro.xlsm", office_zip("xlsx")), ("noext", PNG), ("x.png.exe", PNG)):
            response = self.send(files=[(name, content)])
            self.assertEqual(response.status_code, 400, name)
            self.assertIn("cannot be attached", response.get_json()["error"], name)
        self.assert_nothing_kept()

    def test_disguised_files_are_refused_by_their_content(self):
        for name, content in (("photo.png", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"),
                              ("photo.jpg", PNG), ("doc.pdf", b"<html><script>alert(1)</script></html>"), ("sheet.xlsx", office_zip("docx")),
                              ("doc.docx", office_zip("docx", extra=["word/vbaProject.bin"])), ("t.txt", b"a\x00b"), ("p.heic", PNG)):
            response = self.send(files=[(name, content)])
            self.assertEqual(response.status_code, 400, name)
        self.assert_nothing_kept()

    def test_limits(self):
        self.assertEqual(self.send(files=[("f{}.png".format(n), PNG) for n in range(6)]).status_code, 400)
        big = PNG + b"\x00" * (10 * 1024 * 1024)
        response = self.send(files=[("big.png", big)])
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.get_json()["code"], "too_large")
        nine = PNG + b"\x00" * (9 * 1024 * 1024)
        response = self.send(files=[("f{}.png".format(n), nine) for n in range(3)])
        self.assertEqual(response.status_code, 413)
        self.assertIn("25 MB", response.get_json()["error"])
        self.assertEqual(self.send(files=[("empty.png", b"")]).status_code, 400)
        self.assert_nothing_kept()
        ok = PNG + b"\x00" * (10 * 1024 * 1024 - len(PNG))
        self.assertEqual(self.send(files=[("exact.png", ok)]).status_code, 201)

    def test_a_body_over_the_cap_is_refused_before_it_is_read_and_json(self):
        with mock.patch.object(hu, "MAX_BODY_BYTES", 4000):
            response = self.send(files=[("a.png", PNG + b"\x00" * 6000)])
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.get_json()["code"], "too_large")
        self.assert_nothing_kept()

    def test_a_body_without_a_length_is_still_capped_while_it_streams(self):
        boundary = "XBOUNDARY"
        body = (("--{b}\r\nContent-Disposition: form-data; name=\"category\"\r\n\r\nother\r\n"
                 "--{b}\r\nContent-Disposition: form-data; name=\"description\"\r\n\r\n{d}\r\n"
                 "--{b}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"a.png\"\r\nContent-Type: image/png\r\n\r\n").format(
                     b=boundary, d=GOOD).encode() + PNG + b"\x00" * 8000 + "\r\n--{}--\r\n".format(boundary).encode())
        with mock.patch.object(hu, "MAX_BODY_BYTES", 4000):
            response = self.client.post("/help/requests", input_stream=io.BytesIO(body),
                                        content_type="multipart/form-data; boundary=" + boundary)
        self.assertEqual(response.status_code, 413)
        self.assert_nothing_kept()

    def test_the_size_limit_is_scoped_to_the_help_routes(self):
        self.assertIsNone(clinic_app.app.config.get("MAX_CONTENT_LENGTH"))
        self.sent(files=[("a.png", PNG)])
        self.assertIsNone(clinic_app.app.config.get("MAX_CONTENT_LENGTH"))
        with clinic_app.app.test_request_context("/webhook/whatsapp", method="POST"):
            self.assertIsNone(clinic_app.request.max_content_length)             # another route: no help limit applies
        big_json = {"entry": [], "pad": "x" * (2 * 1024 * 1024)}
        self.assertEqual(self.client.post("/webhook/whatsapp", json=big_json).status_code, 200)

    def test_filenames_with_traversal_are_only_ever_text(self):
        for name in ("../../../etc/passwd.png", "..\\..\\windows\\evil.png", "/etc/shadow.png", "a/b/../../c.png", "%2e%2e%2fx.png", "x\x00.png"):
            response = self.send(files=[(name, PNG)])
            self.assertEqual(response.status_code, 201, name)
        self.assertEqual(self.on_disk().__len__(), 6)
        for relative in self.on_disk():
            self.assertRegex(relative, r"^HELP-\d{4}/[0-9a-f]{32}$")
        outside = [p for p in Path(self.tmp.name).rglob("*") if p.is_file() and self.uploads not in p.parents and p.suffix in ("", ".png")
                   and p.name in ("passwd", "passwd.png", "evil.png", "shadow.png", "c.png")]
        self.assertEqual(outside, [])
        names = [a["original_name"] for a in self.client.get("/help/requests/1").get_json()["request"]["attachments"]]
        self.assertEqual(names, ["passwd.png"])

    def test_one_bad_file_among_good_ones_keeps_nothing(self):
        response = self.send(files=[("good.png", PNG), ("good.pdf", PDF), ("bad.exe", b"MZ")])
        self.assertEqual(response.status_code, 400)
        self.assert_nothing_kept()

    def test_a_failure_in_the_middle_of_saving_keeps_nothing(self):
        real = hr._event

        def explode(conn, request_id, *a, **k):
            real(conn, request_id, *a, **k)
            raise RuntimeError("boom")

        with mock.patch.object(hr, "_event", side_effect=explode):
            response = self.send(files=[("a.png", PNG), ("b.pdf", PDF)])
        self.assertEqual(response.status_code, 500)
        self.assert_nothing_kept()
        self.assertEqual(self.sent(files=[("a.png", PNG)])["ticket_no"], "HELP-0001")


class OwnRequestsAndDownloads(HelpRouteCase):
    def setUp(self):
        super().setUp()
        self.mine = self.sent(files=[("my shot.png", PNG), ("report.txt", b"hello")])
        self.conn.execute("INSERT INTO help_requests (ticket_no, created_at, username, category_key, description, sla_hours, sla_due_at) "
                          "VALUES ('HELP-0002', '2026-10-05 11:00:00', 'Dr. Rao', 'other', 'someone else''s request text', 24, '2026-10-06 11:00:00')")
        self.conn.execute("INSERT INTO help_attachments (request_id, original_name, stored_name, mime, bytes, sha256, created_at) "
                          "VALUES (2, 'theirs.png', ?, 'image/png', 8, 'x', '2026-10-05 11:00:00')", ("a" * 32,))
        self.conn.commit()
        (self.uploads / "HELP-0002").mkdir()
        (self.uploads / "HELP-0002" / ("a" * 32)).write_bytes(PNG)

    def test_the_list_and_detail_are_the_users_own_only(self):
        listed = self.client.get("/help/requests").get_json()["requests"]
        self.assertEqual([r["ticket_no"] for r in listed], ["HELP-0001"])
        self.assertEqual(listed[0]["attachment_count"], 2)
        self.assertEqual(self.client.get("/help/requests/2").status_code, 404)
        self.assertEqual(self.client.get("/help/requests/999").status_code, 404)
        detail = self.client.get("/help/requests/1").get_json()["request"]
        self.assertEqual([e["kind"] for e in detail["events"]], ["created"])
        self.assertEqual(len(detail["attachments"]), 2)
        self.assertNotIn("stored_name", json.dumps(detail))
        self.assertNotIn("sha256", json.dumps(detail))

    def test_download_headers_and_content(self):
        attachment_id = self.client.get("/help/requests/1").get_json()["request"]["attachments"][0]["id"]
        response = self.client.get("/help/attachments/{}".format(attachment_id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, PNG)
        self.assertTrue(response.headers["Content-Disposition"].startswith("attachment"))
        self.assertIn("my shot.png", response.headers["Content-Disposition"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["Content-Type"], "image/png")
        self.assertIn("sandbox", response.headers["Content-Security-Policy"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        text = self.client.get("/help/attachments/{}".format(attachment_id + 1))
        self.assertEqual(text.headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertTrue(text.headers["Content-Disposition"].startswith("attachment"))

    def test_someone_elses_file_and_unknown_ids_are_404(self):
        self.assertEqual(self.client.get("/help/attachments/3").status_code, 404)
        self.assertEqual(self.client.get("/help/attachments/999").status_code, 404)
        self.assertEqual(self.client.get("/help/attachments/abc").status_code, 404)
        self.assertEqual(self.client.get("/help/attachments/1/../3").status_code, 404)
        self.assertEqual(self.client.get("/help/attachments/..%2F..%2Fetc%2Fpasswd").status_code, 404)

    def test_a_tampered_database_row_cannot_point_outside_the_folder(self):
        secret = Path(self.tmp.name) / "secret.txt"
        secret.write_text("secret")
        for evil in ("../../secret.txt", "..", "/etc/passwd", str(secret), "../HELP-0002/" + "a" * 32, "a" * 31, ("a" * 32) + "/x"):
            self.conn.execute("UPDATE help_attachments SET stored_name = ? WHERE id = 1", (evil,))
            self.conn.commit()
            self.assertEqual(self.client.get("/help/attachments/1").status_code, 404, evil)
            self.assertEqual(self.client.get("/help/team/attachments/1").status_code, 404, evil)

    def test_a_missing_file_is_404_not_a_crash(self):
        for path in (self.uploads / "HELP-0001").iterdir():
            path.unlink()
        self.assertEqual(self.client.get("/help/attachments/1").status_code, 404)

    def test_the_team_side_can_download_anyones_file(self):
        response = self.client.get("/help/team/attachments/3")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, PNG)
        self.assertTrue(response.headers["Content-Disposition"].startswith("attachment"))
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")


class TeamSide(HelpRouteCase):
    def setUp(self):
        super().setUp()
        self.a = self.sent(category="voice_assistant")
        self.now = T0 + timedelta(minutes=5)
        self.b = self.sent(category="other")
        self.now = T0 + timedelta(minutes=10)

    def test_list_filters_and_summary(self):
        data = self.client.get("/help/team/requests").get_json()
        self.assertEqual([r["ticket_no"] for r in data["requests"]], ["HELP-0002", "HELP-0001"])
        self.assertEqual(data["summary"]["total"], 2)
        self.assertEqual(data["summary"]["by_status"]["new"], 2)
        self.assertEqual(data["summary"]["by_sla"]["on_track"], 2)
        self.assertEqual([r["ticket_no"] for r in self.client.get("/help/team/requests?category=voice_assistant").get_json()["requests"]], ["HELP-0001"])
        self.assertEqual(self.client.get("/help/team/requests?status=resolved").get_json()["requests"], [])
        self.assertEqual(self.client.get("/help/team/requests?status=bogus").status_code, 400)
        self.now = T0 + timedelta(days=2)
        overdue = self.client.get("/help/team/requests?overdue=1").get_json()
        self.assertEqual(len(overdue["requests"]), 2)
        self.assertEqual(overdue["summary"]["by_sla"]["overdue"], 2)

    def test_changing_the_status_with_a_note(self):
        response = self.team_post(self.a["id"], "in_progress", "Looking at the voice flow")
        self.assertEqual(response.status_code, 200)
        item = response.get_json()["request"]
        self.assertEqual((item["status"], item["next_statuses"]), ("in_progress", ["resolved", "closed"]))
        self.assertEqual(item["events"][-1]["actor"], "team")
        self.assertEqual(item["events"][-1]["note"], "Looking at the voice flow")
        mine = self.client.get("/help/requests/{}".format(self.a["id"])).get_json()["request"]          # the user sees it
        self.assertEqual(mine["status_label"], "In progress")
        self.assertEqual(mine["events"][-1]["note"], "Looking at the voice flow")

    def test_bad_status_changes_are_400_and_404(self):
        self.assertEqual(self.team_post(self.a["id"], "done").status_code, 400)
        self.assertEqual(self.team_post(self.a["id"], "new").status_code, 400)           # already new, no note
        self.assertEqual(self.team_post(999, "resolved").status_code, 404)
        self.assertEqual(self.team_post(self.a["id"], "resolved", "x" * 501).status_code, 400)
        self.team_post(self.a["id"], "resolved")
        self.assertEqual(self.team_post(self.a["id"], "acknowledged").status_code, 400)  # no going back
        self.assertEqual(self.team_post(self.a["id"], "in_progress").status_code, 200)   # reopening is allowed
        self.assertEqual(self.client.post("/help/team/requests/1/status", data="nope").status_code, 400)

    def test_import_a_csv_file(self):
        csv_text = "ticket_no,status,note\nHELP-0001,in_progress,Looking\nHELP-0002,resolved,Fixed it\nHELP-0099,resolved,\n"
        response = self.client.post("/help/team/import", data={"file": (io.BytesIO(csv_text.encode()), "updates.csv")},
                                    content_type="multipart/form-data")
        self.assertEqual(response.status_code, 200)
        result = response.get_json()["result"]
        self.assertEqual((result["rows"], result["updated"], result["unknown_tickets"]), (3, 2, ["HELP-0099"]))
        self.assertEqual(self.conn.execute("SELECT status FROM help_requests ORDER BY id").fetchall()[0][0], "in_progress")
        again = self.client.post("/help/team/import", data={"file": (io.BytesIO(csv_text.encode()), "updates.csv")},
                                 content_type="multipart/form-data").get_json()["result"]
        self.assertEqual((again["updated"], again["unchanged"]), (0, 2))                 # idempotent
        self.assertEqual(self.rows("help_request_events"), 2 + 2)                         # 2 created + 2 status changes, none added twice

    def test_import_as_json_body_raw_csv_and_json_file(self):
        rows = [{"ticket_no": "HELP-0001", "status": "acknowledged", "note": "Seen"}]
        self.assertEqual(self.client.post("/help/team/import", json=rows).get_json()["result"]["updated"], 1)
        self.assertEqual(self.client.post("/help/team/import", json={"updates": [{"ticket_no": "HELP-0002", "status": "closed"}]}).get_json()["result"]["updated"], 1)
        raw = self.client.post("/help/team/import", data="ticket_no,status,note\nHELP-0001,resolved,ok\n", content_type="text/csv")
        self.assertEqual(raw.get_json()["result"]["updated"], 1)
        as_file = self.client.post("/help/team/import", data={"file": (io.BytesIO(b'[{"ticket_no": "HELP-0001", "status": "closed"}]'), "u.json")},
                                   content_type="multipart/form-data")
        self.assertEqual(as_file.get_json()["result"]["updated"], 1)

    def test_import_errors(self):
        for kwargs in (dict(data="", content_type="text/csv"), dict(data="a,b\n1,2\n", content_type="text/csv"), dict(json={"nope": 1}),
                       dict(json="text"), dict(data="[1,", content_type="application/json")):
            response = self.client.post("/help/team/import", **kwargs)
            self.assertEqual(response.status_code, 400, kwargs)
            self.assertFalse(response.get_json()["ok"])
        big = "ticket_no,status,note\n" + "HELP-0001,new,\n" * 100000
        response = self.client.post("/help/team/import", data={"file": (io.BytesIO(big.encode()), "big.csv")}, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM help_requests WHERE status <> 'new'").fetchone()[0], 0)

    def test_export_is_a_zip_and_can_be_repeated(self):
        self.sent(files=[("shot.png", PNG)])
        response = self.client.get("/help/team/export")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "application/zip")
        self.assertRegex(response.headers["Content-Disposition"], r'attachment; filename=help-requests-\d{8}-\d{4}\.zip')
        self.assertEqual(response.headers["X-Help-Request-Count"], "3")
        archive = zipfile.ZipFile(io.BytesIO(response.data))
        self.assertTrue({"requests.csv", "requests.json", "README.txt", "attachments/HELP-0003/01_shot.png"} <= set(archive.namelist()))
        rows = list(csv.DictReader(io.StringIO(archive.read("requests.csv").decode("utf-8-sig"))))
        self.assertEqual([r["ticket_no"] for r in rows], ["HELP-0001", "HELP-0002", "HELP-0003"])
        self.assertEqual(self.client.get("/help/team/export").headers["X-Help-Request-Count"], "3")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM help_requests WHERE exported_at IS NOT NULL").fetchone()[0], 0)

    def test_export_filters_and_stamping_only_when_asked(self):
        self.team_post(self.a["id"], "in_progress")
        count = lambda query: self.client.get("/help/team/export" + query).headers["X-Help-Request-Count"]
        self.assertEqual(count("?status=new"), "1")
        self.assertEqual(count("?status=in_progress"), "1")
        self.assertEqual(count("?since=2026-10-05 10:04"), "1")
        self.assertEqual(count("?since=2026-10-06"), "0")
        self.assertEqual(count("?unexported=1&mark_exported=1"), "2")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM help_requests WHERE exported_at IS NOT NULL").fetchone()[0], 2)
        self.assertEqual(count("?unexported=1"), "0")
        self.assertEqual(count(""), "2")
        self.assertEqual(self.client.get("/help/team/export?status=bogus").status_code, 400)
        self.assertEqual(self.client.get("/help/team/export?since=yesterday").status_code, 400)

    def test_the_resolved_notice_flow_end_to_end(self):
        self.assertEqual(self.client.get("/help/notices").get_json()["notices"], [])
        self.client.post("/help/team/import", json=[{"ticket_no": "HELP-0001", "status": "resolved", "note": "Fixed in the 12 Oct update"}])
        notices = self.client.get("/help/notices").get_json()["notices"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["ticket_no"], "HELP-0001")
        self.assertEqual(notices[0]["text"], "Your request HELP-0001 has been resolved. Fixed in the 12 Oct update.")
        self.assertTrue(self.client.post("/help/requests/{}/notified".format(notices[0]["id"])).get_json()["changed"])
        self.assertEqual(self.client.get("/help/notices").get_json()["notices"], [])
        self.assertFalse(self.client.post("/help/requests/{}/notified".format(notices[0]["id"])).get_json()["changed"])

    def test_the_notice_is_only_for_its_owner(self):
        self.team_post(self.a["id"], "resolved", "Done")
        with mock.patch.object(hr, "current_user", return_value={"username": "Dr. Rao"}):
            self.assertEqual(self.client.get("/help/notices").get_json()["notices"], [])
            self.assertFalse(self.client.post("/help/requests/{}/notified".format(self.a["id"])).get_json()["changed"])
        self.assertEqual(len(self.client.get("/help/notices").get_json()["notices"]), 1)


class SlaSettingRoute(HelpRouteCase):
    def test_read_and_save(self):
        self.assertEqual(self.client.get("/settings/help-sla").get_json()["data"], {"hours": 24, "min": 1, "max": 720})
        response = self.client.post("/settings/help-sla", json={"hours": 48})
        self.assertEqual(response.get_json()["data"]["hours"], 48)
        self.assertEqual(self.sent()["sla_hours"], 48)
        self.assertEqual(self.client.get("/help/config").get_json()["data"]["sla_hours"], 48)

    def test_bad_values_are_400_and_change_nothing(self):
        for body in ({"hours": 0}, {"hours": 721}, {"hours": "abc"}, {"hours": None}, {}, {"hours": True}):
            response = self.client.post("/settings/help-sla", json=body)
            self.assertEqual(response.status_code, 400, body)
            self.assertFalse(response.get_json()["ok"])
        self.assertEqual(self.client.get("/settings/help-sla").get_json()["data"]["hours"], 24)

    def test_old_requests_keep_their_snapshot(self):
        old = self.sent()
        self.client.post("/settings/help-sla", json={"hours": 2})
        detail = self.client.get("/help/requests/{}".format(old["id"])).get_json()["request"]
        self.assertEqual((detail["sla_hours"], detail["sla_due_at"]), (24, "2026-10-06 10:00:00"))


class NoClinicWrites(HelpRouteCase):
    def test_a_help_request_changes_no_clinic_table_and_proposes_nothing(self):
        clinic_tables = ("patients", "appointments", "proposals", "audit_log", "staff", "followups", "notifications", "wa_messages",
                         "planner_log", "unanswered_questions")
        before = {t: self.count(t) for t in clinic_tables}
        self.sent(files=[("a.png", PNG)])
        self.team_post(1, "resolved", "ok")
        self.client.post("/help/team/import", json=[{"ticket_no": "HELP-0001", "status": "closed"}])
        self.client.get("/help/team/export?mark_exported=1")
        self.assertEqual({t: self.count(t) for t in clinic_tables}, before)
        self.assertEqual(self.sender.calls, [])                                          # and nothing was sent to anyone


if __name__ == "__main__":
    unittest.main()
