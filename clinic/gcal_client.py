"""The Google Calendar client: a deliberately tiny interface plus the real
implementation (Calendar REST API over httpx, authenticated with a service
account through google-auth).

The sync engine (clinic/gcal_sync.py) only ever talks to the four methods of
`CalendarClient` below, so tests use an in-memory fake and never reach Google.

  list_events(calendar_id, time_min, time_max) -> [event dict]   (ours only)
  insert_event(calendar_id, body)              -> event dict (has "id")
  patch_event(calendar_id, event_id, body)     -> event dict
  delete_event(calendar_id, event_id)          -> None

Any failure is a GcalApiError. `status` is the HTTP status (404/410 mean "that
event is gone") or None for a network / authentication problem.

The real client is built lazily and only when configured; google-auth is
imported only when a request is actually made, so importing this module (and
running the app without Google set up) needs neither the package nor the key.
The key file is read by google-auth on the first request, nowhere else.
"""

import logging
import threading
from urllib.parse import quote

import httpx

from clinic import gcal_config

_logger = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/calendar/v3"
REQUEST_TIMEOUT_SECONDS = 10.0


class GcalApiError(Exception):
    """A Google Calendar call failed. `status` is the HTTP status, or None for
    a network / authentication failure."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status

    @property
    def is_gone(self):
        """404 / 410: the event (or its deletion) is already settled."""
        return self.status in (404, 410)


class _HttpxRequest:
    """google-auth's transport interface (a callable returning an object with
    .status / .headers / .data), on top of httpx -- so the token exchange needs
    no `requests`/`urllib3` dependency."""

    class _Response:
        def __init__(self, response):
            self.status = response.status_code
            self.headers = response.headers
            self.data = response.content

    def __init__(self, http):
        self._http = http

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        try:
            response = self._http.request(method, url, content=body, headers=headers,
                                          timeout=timeout or REQUEST_TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:
            from google.auth import exceptions as auth_exceptions
            raise auth_exceptions.TransportError(exc)
        return self._Response(response)


def _error_message(response):
    try:
        message = response.json().get("error", {}).get("message")
    except Exception:
        message = None
    return "Google Calendar API returned {}: {}".format(response.status_code, message or response.reason_phrase)


class GoogleCalendarClient:
    """Service-account authenticated Calendar REST calls.

    `credentials_factory()` (a test seam) returns a google-auth credentials
    object; by default it loads the key file with
    `service_account.Credentials.from_service_account_file`. `http` is an
    httpx.Client (a test passes one with a MockTransport).
    """

    def __init__(self, key_file=None, http=None, credentials_factory=None):
        self._key_file = key_file
        self._http = http or httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)
        self._credentials_factory = credentials_factory
        self._credentials = None
        self._lock = threading.Lock()

    # -- authentication ----------------------------------------------------

    def _load_credentials(self):
        if self._credentials_factory is not None:
            return self._credentials_factory()
        from google.oauth2 import service_account
        return service_account.Credentials.from_service_account_file(
            self._key_file, scopes=[gcal_config.SCOPE])

    def _access_token(self, force_refresh=False):
        with self._lock:
            try:
                if self._credentials is None:
                    self._credentials = self._load_credentials()
                if force_refresh or not self._credentials.valid:
                    self._credentials.refresh(_HttpxRequest(self._http))
            except GcalApiError:
                raise
            except Exception as exc:
                # Never echo the key file's contents: name the failure class
                # and (for a refresh error) Google's own short message only.
                self._credentials = None
                raise GcalApiError(
                    None, "Could not authenticate with Google ({}: {})".format(type(exc).__name__, _safe_text(exc))
                )
            return self._credentials.token

    # -- transport -----------------------------------------------------------

    def _request(self, method, path, params=None, json=None):
        url = "{}/calendars/{}".format(API_BASE, path)
        for attempt in (1, 2):
            token = self._access_token(force_refresh=(attempt == 2))
            try:
                response = self._http.request(
                    method, url, params=params, json=json,
                    headers={"Authorization": "Bearer {}".format(token)},
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except httpx.HTTPError as exc:
                raise GcalApiError(None, "Could not reach Google Calendar ({})".format(type(exc).__name__))
            if response.status_code == 401 and attempt == 1:
                continue  # expired / revoked token: refresh once and retry
            if response.status_code >= 400:
                raise GcalApiError(response.status_code, _error_message(response))
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()
        raise GcalApiError(401, "Google Calendar rejected the credentials")

    @staticmethod
    def _cal(calendar_id):
        return quote(calendar_id, safe="")

    # -- the interface the sync engine uses ----------------------------------

    def list_events(self, calendar_id, time_min, time_max):
        events, page_token = [], None
        while True:
            params = {
                "timeMin": time_min,
                "timeMax": time_max,
                "singleEvents": "true",
                "showDeleted": "false",
                "maxResults": "2500",
                "timeZone": gcal_config.TIMEZONE,
                # Only events this app created -- see gcal_config.SOURCE_*.
                "privateExtendedProperty": "{}={}".format(gcal_config.SOURCE_PROPERTY, gcal_config.SOURCE_VALUE),
            }
            if page_token:
                params["pageToken"] = page_token
            data = self._request("GET", "{}/events".format(self._cal(calendar_id)), params=params)
            events.extend(data.get("items", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                return events

    def insert_event(self, calendar_id, body):
        return self._request("POST", "{}/events".format(self._cal(calendar_id)), json=body)

    def patch_event(self, calendar_id, event_id, body):
        return self._request("PATCH", "{}/events/{}".format(self._cal(calendar_id), quote(event_id, safe="")), json=body)

    def delete_event(self, calendar_id, event_id):
        self._request("DELETE", "{}/events/{}".format(self._cal(calendar_id), quote(event_id, safe="")))


def _safe_text(exc):
    text = " ".join(str(exc).split())
    return text[:200]


# ---------------------------------------------------------------------------
# The shared real client
# ---------------------------------------------------------------------------

_client_lock = threading.Lock()
_client = None
_client_key = None


def get_client():
    """The real client when Google sync is configured, else None. Built lazily
    and reused (it caches its access token). Never touches the key file's
    contents -- only google-auth does, on the first request."""
    global _client, _client_key
    if not gcal_config.is_configured():
        return None
    key_file = gcal_config.service_account_file()
    with _client_lock:
        if _client is None or _client_key != key_file:
            _client = GoogleCalendarClient(key_file)
            _client_key = key_file
        return _client


def reset_client_for_tests():
    global _client, _client_key
    with _client_lock:
        _client, _client_key = None, None
