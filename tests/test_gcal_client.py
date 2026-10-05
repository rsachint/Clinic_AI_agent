"""The real Google Calendar client (clinic/gcal_client.py), exercised against
httpx.MockTransport only -- no request ever leaves the process, and the only
key material involved is a throwaway RSA key generated inside the test."""
import json
import os
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from clinic import gcal_client, gcal_config
from clinic.gcal_client import GcalApiError, GoogleCalendarClient

CAL = "clinic-demo@example.com"
EVENTS_PATH = "/calendar/v3/calendars/clinic-demo@example.com/events"   # httpx reports the decoded path


class FakeCredentials:
    """google-auth's credentials interface, minus the crypto."""

    def __init__(self):
        self.valid = False
        self.token = None
        self.refreshes = 0

    def refresh(self, request):
        self.refreshes += 1
        self.token = "token-{}".format(self.refreshes)
        self.valid = True


def make_client(handler, credentials=None):
    credentials = credentials or FakeCredentials()
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return GoogleCalendarClient(key_file=None, http=http, credentials_factory=lambda: credentials), credentials


class RequestShapeTests(unittest.TestCase):
    def test_list_events_pages_and_filters_to_our_events(self):
        seen = []

        def handler(request):
            seen.append(request)
            if "pageToken" not in request.url.params:
                return httpx.Response(200, json={"items": [{"id": "a"}], "nextPageToken": "p2"})
            return httpx.Response(200, json={"items": [{"id": "b"}]})

        client, _ = make_client(handler)
        events = client.list_events(CAL, "2026-10-03T00:00:00+05:30", "2026-10-04T00:00:00+05:30")
        self.assertEqual([e["id"] for e in events], ["a", "b"])
        first = seen[0]
        self.assertEqual((first.method, first.url.path), ("GET", EVENTS_PATH))
        self.assertEqual(first.headers["authorization"], "Bearer token-1")
        params = first.url.params
        self.assertEqual(params["timeMin"], "2026-10-03T00:00:00+05:30")
        self.assertEqual(params["timeMax"], "2026-10-04T00:00:00+05:30")
        self.assertEqual(params["privateExtendedProperty"], "source=clinic-copilot")
        self.assertEqual(params["singleEvents"], "true")
        self.assertEqual(params["timeZone"], "Asia/Kolkata")
        self.assertEqual(seen[1].url.params["pageToken"], "p2")

    def test_insert_patch_delete(self):
        seen = []

        def handler(request):
            seen.append(request)
            if request.method == "DELETE":
                return httpx.Response(204)
            return httpx.Response(200, json={"id": "evt9"})

        client, creds = make_client(handler)
        self.assertEqual(client.insert_event(CAL, {"summary": "T-01 · Asha V."})["id"], "evt9")
        client.patch_event(CAL, "evt9", {"summary": "new", "colorId": None})
        self.assertIsNone(client.delete_event(CAL, "evt9"))
        insert, patch_, delete = seen
        self.assertEqual((insert.method, insert.url.path), ("POST", EVENTS_PATH))
        self.assertEqual(json.loads(insert.content), {"summary": "T-01 · Asha V."})
        self.assertEqual((patch_.method, patch_.url.path), ("PATCH", EVENTS_PATH + "/evt9"))
        self.assertEqual(json.loads(patch_.content), {"summary": "new", "colorId": None})
        self.assertEqual((delete.method, delete.url.path), ("DELETE", EVENTS_PATH + "/evt9"))
        self.assertEqual(creds.refreshes, 1)           # one token, reused for all three calls

    def test_http_errors_become_gcal_api_errors_with_googles_message(self):
        def handler(request):
            return httpx.Response(404, json={"error": {"code": 404, "message": "Not Found"}})

        client, _ = make_client(handler)
        with self.assertRaises(GcalApiError) as ctx:
            client.list_events(CAL, "a", "b")
        self.assertEqual(ctx.exception.status, 404)
        self.assertTrue(ctx.exception.is_gone)
        self.assertIn("404", str(ctx.exception))
        self.assertIn("Not Found", str(ctx.exception))

    def test_a_non_json_error_body_is_still_reported(self):
        client, _ = make_client(lambda request: httpx.Response(502, text="<html>bad gateway</html>"))
        with self.assertRaises(GcalApiError) as ctx:
            client.insert_event(CAL, {})
        self.assertEqual(ctx.exception.status, 502)
        self.assertFalse(ctx.exception.is_gone)

    def test_an_expired_token_is_refreshed_once_and_the_call_retried(self):
        calls = []

        def handler(request):
            calls.append(request.headers["authorization"])
            if len(calls) == 1:
                return httpx.Response(401, json={"error": {"message": "Invalid Credentials"}})
            return httpx.Response(200, json={"items": []})

        client, creds = make_client(handler)
        self.assertEqual(client.list_events(CAL, "a", "b"), [])
        self.assertEqual(calls, ["Bearer token-1", "Bearer token-2"])

    def test_a_persistent_401_is_an_error_not_a_loop(self):
        client, creds = make_client(lambda request: httpx.Response(401, json={"error": {"message": "nope"}}))
        with self.assertRaises(GcalApiError) as ctx:
            client.list_events(CAL, "a", "b")
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(creds.refreshes, 2)

    def test_network_failure_is_a_gcal_api_error_without_a_status(self):
        def handler(request):
            raise httpx.ConnectError("dns failure", request=request)

        client, _ = make_client(handler)
        with self.assertRaises(GcalApiError) as ctx:
            client.delete_event(CAL, "evt1")
        self.assertIsNone(ctx.exception.status)
        self.assertFalse(ctx.exception.is_gone)

    def test_authentication_failure_is_reported_without_a_status(self):
        class BrokenCredentials(FakeCredentials):
            def refresh(self, request):
                raise RuntimeError("invalid_grant: bad signature")

        client, _ = make_client(lambda request: httpx.Response(200, json={}), BrokenCredentials())
        with self.assertRaises(GcalApiError) as ctx:
            client.list_events(CAL, "a", "b")
        self.assertIsNone(ctx.exception.status)
        self.assertIn("authenticate", str(ctx.exception))


class ServiceAccountFlowTests(unittest.TestCase):
    """The real google-auth service-account flow with a throwaway key: the JWT
    is signed, exchanged at the (mocked) token endpoint, and the resulting
    token is what the Calendar call carries."""

    def setUp(self):
        warnings.simplefilter("ignore")
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            import google.oauth2.service_account  # noqa: F401
        except ImportError:
            self.skipTest("google-auth / cryptography not installed")
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        self.info = {
            "type": "service_account", "project_id": "test-project", "private_key_id": "k1", "private_key": pem,
            "client_email": "sync@test-project.iam.gserviceaccount.com", "client_id": "1",
            "token_uri": "https://oauth2.googleapis.com/token",
        }

    def test_token_exchange_then_calendar_call(self):
        from google.oauth2 import service_account
        requests_seen = []

        def handler(request):
            requests_seen.append(request)
            if request.url.host == "oauth2.googleapis.com":
                return httpx.Response(200, json={"access_token": "ya29.test", "expires_in": 3600, "token_type": "Bearer"})
            return httpx.Response(200, json={"items": []})

        def credentials_factory():
            return service_account.Credentials.from_service_account_info(self.info, scopes=[gcal_config.SCOPE])

        http = httpx.Client(transport=httpx.MockTransport(handler))
        client = GoogleCalendarClient(key_file=None, http=http, credentials_factory=credentials_factory)
        self.assertEqual(client.list_events(CAL, "2026-10-03T00:00:00+05:30", "2026-10-04T00:00:00+05:30"), [])
        token_request, api_request = requests_seen
        form = token_request.content.decode()
        self.assertIn("grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer", form)
        assertion = form.split("assertion=")[1].split("&")[0]
        self.assertEqual(len(assertion.split(".")), 3)       # a signed JWT
        self.assertEqual(api_request.headers["authorization"], "Bearer ya29.test")
        self.assertEqual(gcal_config.SCOPE, "https://www.googleapis.com/auth/calendar.events")

    def test_an_unreadable_key_file_never_leaks_its_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key.json"
            key.write_text("TOP-SECRET-SENTINEL this is not json")
            http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
            client = GoogleCalendarClient(key_file=str(key), http=http)
            with self.assertRaises(GcalApiError) as ctx:
                client.list_events(CAL, "a", "b")
        self.assertIsNone(ctx.exception.status)
        self.assertNotIn("TOP-SECRET-SENTINEL", str(ctx.exception))


class LazyConstructionTests(unittest.TestCase):
    def setUp(self):
        gcal_client.reset_client_for_tests()
        self.addCleanup(gcal_client.reset_client_for_tests)

    def test_no_client_unless_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(gcal_client.get_client())
        with patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_FILE": "/nonexistent/key.json"}, clear=True):
            self.assertIsNone(gcal_client.get_client())

    def test_client_is_built_without_reading_the_key_and_is_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "key.json"
            key.write_text("{}")
            key.chmod(0)       # unreadable: building the client must not need to read it
            try:
                with patch.dict(os.environ, {"GOOGLE_SERVICE_ACCOUNT_FILE": str(key), "GOOGLE_CALENDAR_ID": CAL, "GOOGLE_CALENDAR_SYNC": "1"}, clear=True):
                    first = gcal_client.get_client()
                    self.assertIsInstance(first, GoogleCalendarClient)
                    self.assertIs(gcal_client.get_client(), first)
            finally:
                key.chmod(0o600)

    def test_importing_the_client_module_does_not_import_google_auth(self):
        import subprocess
        code = ("import sys; import clinic.gcal_client, clinic.gcal_sync, clinic.scheduler; "
                "print('google.auth' in sys.modules)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             cwd=str(Path(__file__).resolve().parents[1]))
        self.assertEqual(out.stdout.strip(), "False", out.stderr)


if __name__ == "__main__":
    unittest.main()
