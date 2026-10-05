"""Google Calendar integration: configuration and the embed / "open" URLs.

Everything here is read from the environment at CALL time (not import time),
so a test can patch it and so the answer always reflects the process's
current environment (app.py's load_dotenv() runs at startup -- which is why
the app must be restarted after credentials are added to .env).

Nothing in this module reads, parses or prints the service-account key file.
"is it configured?" is only "is the env var set and does that path exist".
"""

import os
from urllib.parse import quote, urlencode

# There is no built-in calendar: set GOOGLE_CALENDAR_ID (in .env) to the
# address the calendar is shared with. Until then the integration is simply
# "not configured".
DEFAULT_CALENDAR_ID = ""
TIMEZONE = "Asia/Kolkata"
SCOPE = "https://www.googleapis.com/auth/calendar.events"

# Stamped into every event we create (extendedProperties.private), and used as
# the list filter, so reconciliation only ever touches events this app made --
# never something a person added to the calendar by hand.
SOURCE_PROPERTY = "source"
SOURCE_VALUE = "clinic-copilot"
APPOINTMENT_PROPERTY = "appointment_id"

# Google's embed supports exactly these three views.
EMBED_MODES = ("WEEK", "MONTH", "AGENDA")
DEFAULT_EMBED_MODE = "WEEK"


def sync_enabled():
    """Google sync is OFF unless GOOGLE_CALENDAR_SYNC is set to 1/true/yes/on: the
    in-app calendar (the Appointments tab) is the calendar now. The integration
    code stays in the repo, dormant, and the events already on that calendar are
    never touched when it is off."""
    return (os.environ.get("GOOGLE_CALENDAR_SYNC") or "").strip().lower() in ("1", "true", "yes", "on")


def calendar_id():
    return (os.environ.get("GOOGLE_CALENDAR_ID") or "").strip() or DEFAULT_CALENDAR_ID


def service_account_file():
    """The configured key-file PATH (never its contents), or None."""
    path = (os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE") or "").strip()
    return os.path.expanduser(path) if path else None


def uses_scratch_database():
    """True when this process runs on a non-default database (CLINIC_DB_PATH is
    set to something other than the real clinic.db) -- a scratch/test copy.
    Such a copy must never touch the real Google calendar: reconcile deletes
    every app-tagged event that isn't in ITS database, so a copy with an empty
    scratch DB wipes the real calendar (this happened once)."""
    path = (os.environ.get("CLINIC_DB_PATH") or "").strip()
    return bool(path) and os.path.basename(path) != "clinic.db"


def is_configured():
    """True iff both pieces exist: a calendar id (GOOGLE_CALENDAR_ID)
    and a service-account key file at the configured path -- and this is not a
    scratch-database copy of the app (see uses_scratch_database). Only the path
    is checked; the file is not opened."""
    if not sync_enabled():
        return False
    if uses_scratch_database() and os.environ.get("GOOGLE_SYNC_ALLOW_SCRATCH_DB") != "1":
        return False
    path = service_account_file()
    return bool(path) and bool(calendar_id()) and os.path.isfile(path)


def embed_url(mode=DEFAULT_EMBED_MODE, cal_id=None):
    """The calendar.google.com embed URL for one of Week / Month / Agenda."""
    mode = (mode or DEFAULT_EMBED_MODE).upper()
    if mode not in EMBED_MODES:
        mode = DEFAULT_EMBED_MODE
    query = urlencode(
        [
            ("src", cal_id or calendar_id()),
            ("ctz", TIMEZONE),
            ("mode", mode),
            # Our own buttons switch the view, so hide the embed's own tabs;
            # keep the previous/next arrows.
            ("showTabs", "0"),
            ("showTitle", "0"),
            ("showPrint", "0"),
            ("showCalendars", "0"),
            ("showTz", "0"),
        ],
        quote_via=quote,
    )
    return "https://calendar.google.com/calendar/embed?" + query


def open_url():
    """Opens the person's own Google Calendar in a new tab (the shared
    calendar shows there for an account that has access to it)."""
    return "https://calendar.google.com/calendar/u/0/r"
