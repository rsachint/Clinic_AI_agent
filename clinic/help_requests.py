""""Need help": what the people who use the app say about how it feels to use it.

A person describes a USER-EXPERIENCE problem (a confusing screen, a slow step, something they could not find,
voice or language not understood), by typing or dictating, optionally with a screenshot or document, and then
sees the status of what they raised. Technical bugs are NOT reported here: the app records its own technical
signals (Audit log -> Connection, the planner log), and the panel says so.

These are FEEDBACK RECORDS, not clinic data. Nothing in this module proposes, commits, or reads a clinic table;
the planner and the voice pipeline never see a help request; no model decides anything about one. They live in
four additive tables (clinic/schema.sql): help_categories, help_requests, help_attachments and the append-only
help_request_events.

How a request moves (there is no server link to the developer, on purpose):

  * the team changes the status in the app (`set_status`), or
  * the developer sends back a small file of updates (CSV or JSON rows: ticket_no, status, note) and the team
    imports it (`import_updates`). Importing the same file twice changes nothing the second time.
  * the team exports the new requests (`export_bundle`, a ZIP) and sends them on by whatever means they like.

When a request becomes "resolved" the person who sent it is told once, the next time the app is open
(`pending_notices` / `mark_notified`, the same pattern as clinic/unanswered.py).

SLA: each request copies the target hours (setting `help_sla_hours`, default 24) and the due time (wall clock,
sent time + hours) when it is sent, so changing the setting later never moves an old request. The state of an
open request is  overdue  (now is past the due time),  due_soon  (not yet due, but no more than
max(2 hours, 20% of the target hours) are left) or  on_track  (anything more). A short target therefore starts
in due_soon: with a 1 hour target the whole window is "soon". A resolved or closed request is  met  when
resolved_at <= the due time, else  breached.

Privacy: any run of 8 or more digits (spaces and dashes allowed inside, so phone numbers) in a description is
replaced by "[number hidden]" before it is stored, and the person is told. No dictated audio is ever stored:
dictation only turns speech into text in the description box (see clinic/realtime_voice.py).

Times are the clinic's local wall clock (IST) as 'YYYY-MM-DD HH:MM:SS'. Every function that needs the time takes
`now` (a naive local datetime) so tests inject a clock.
"""

import csv
import io
import json
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timedelta

from clinic import help_uploads, settings

STATUSES = ("new", "acknowledged", "in_progress", "resolved", "closed")
OPEN_STATUSES = ("new", "acknowledged", "in_progress")
DONE_STATUSES = ("resolved", "closed")
STATUS_LABELS = {"new": "New", "acknowledged": "Acknowledged", "in_progress": "In progress", "resolved": "Resolved",
                 "closed": "Closed"}
SEVERITIES = ("minor", "annoying", "blocks_work")
SEVERITY_LABELS = {"minor": "Minor", "annoying": "Annoying", "blocks_work": "Blocks my work"}
DEFAULT_SEVERITY = "minor"
SOURCES = ("typed", "voice", "mixed")
SLA_STATES = ("on_track", "due_soon", "overdue", "met", "breached")

MIN_DESCRIPTION = 10          # characters, unless a file is attached
MAX_DESCRIPTION = 4000
MAX_NOTE = 500
RATE_LIMIT = 10               # new requests per username ...
RATE_WINDOW = timedelta(hours=1)   # ... per rolling hour
DUE_SOON_MIN_HOURS = 2
DUE_SOON_SHARE = 5               # "the last fifth" (20%) of the target hours
TEAM_LIST_LIMIT = 500
MAX_IMPORT_ROWS = 5000
MAX_IMPORT_BYTES = 1024 * 1024
MASK = "[number hidden]"

DISCLAIMER = (
    "This is for how the app feels to use: confusing screens, steps that take too long, things you could not "
    "find or the app not understanding you. You do not need to report errors or things that are broken: the "
    "app records the technical details automatically. Please do not mention patient names, phone numbers or "
    "any medical details."
)


class HelpError(ValueError):
    """A request that cannot be accepted. The message is shown to the person; `status` is the HTTP status."""

    def __init__(self, message, status=400, code="invalid", **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.extra = extra


def current_user():
    """Who is using the app. There is no login yet (the Profile and Login tabs say "Coming soon"), so this is a
    stand-in: every request is from "Reception (demo)". When real login exists, change ONLY this function. The
    username is always taken from here on the server and stored on the request; a username sent by the browser
    is never read."""
    return {"username": "Reception (demo)"}


# -- time -------------------------------------------------------------------------------------------

_FORMAT = "%Y-%m-%d %H:%M:%S"


def _now(now):
    return now or datetime.now()


def _stamp(moment):
    return moment.strftime(_FORMAT)


def _parse(text):
    try:
        return datetime.strptime(str(text), _FORMAT)
    except (TypeError, ValueError):
        return None


# -- SLA --------------------------------------------------------------------------------------------

def due_soon_window_seconds(sla_hours):
    """How close to the due time "due soon" starts: the larger of 2 hours and 20% of the target hours."""
    return max(DUE_SOON_MIN_HOURS * 3600.0, float(sla_hours) * 3600.0 / DUE_SOON_SHARE)


def sla_state(status, sla_hours, sla_due_at, resolved_at, now):
    """on_track / due_soon / overdue for an open request, met / breached for a resolved or closed one."""
    due = _parse(sla_due_at)
    if due is None:
        return "on_track"
    if status in DONE_STATUSES:
        done = _parse(resolved_at) or _now(now)
        return "met" if done <= due else "breached"
    remaining = (due - _now(now)).total_seconds()
    if remaining < 0:
        return "overdue"
    return "due_soon" if remaining <= due_soon_window_seconds(sla_hours) else "on_track"


def seconds_left(status, sla_due_at, now):
    """Seconds until the due time (negative when past) for an open request, None once it is resolved or closed."""
    due = _parse(sla_due_at)
    if due is None or status in DONE_STATUSES:
        return None
    return int((due - _now(now)).total_seconds())


# -- text rules -------------------------------------------------------------------------------------

# A run of digits with single spaces or dashes between groups, optionally led by "+": a phone number, an Aadhaar
# number, a card number. An ISO date (2026-10-05) is left alone.
_NUMBER_OR_DATE = re.compile(r"(?P<date>(?<!\d)\d{4}-\d{2}-\d{2}(?!\d))|(?P<num>\+?\d+(?:[ \-]\d+)*)")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def mask_numbers(text):
    """(text with every run of 8+ digits replaced by "[number hidden]", whether anything was replaced)."""
    hidden = []

    def swap(match):
        if match.group("date"):
            return match.group(0)
        if sum(ch.isdigit() for ch in match.group("num")) >= 8:
            hidden.append(1)
            return MASK
        return match.group(0)

    return _NUMBER_OR_DATE.sub(swap, text), bool(hidden)


def clean_description(raw):
    """(stored text, masked?) for what the person typed or dictated. Newlines are kept, other control characters
    dropped, runs of blank lines and trailing spaces trimmed."""
    if not isinstance(raw, str):
        raise HelpError("Describe the problem in a few words.")
    if len(raw) > MAX_DESCRIPTION * 2:
        raise HelpError("Keep the description under {} characters.".format(MAX_DESCRIPTION))
    text = _CONTROL.sub("", raw.replace("\r\n", "\n").replace("\r", "\n"))
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > MAX_DESCRIPTION:
        raise HelpError("Keep the description under {} characters (it is {}).".format(MAX_DESCRIPTION, len(text)))
    return mask_numbers(text)


_SAFE_CONTEXT = re.compile(r"^[A-Za-z0-9 _\-.:]{1,40}$")
_CONTEXT_KEYS = ("tab", "branch", "language", "screen")


def clean_context(raw, extra=None):
    """A small {key: short text} from what the browser says about where the person was (the tab, the branch,
    the browser language). Only known keys, only plain short values: nothing personal can ride along."""
    out = {}
    if isinstance(raw, dict):
        for key in _CONTEXT_KEYS:
            value = raw.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                value = str(value)
            if isinstance(value, str) and _SAFE_CONTEXT.match(value.strip()):
                out[key] = value.strip()
    for key, value in (extra or {}).items():
        if value:
            out[key] = str(value)[:40]
    return out


def _clip_note(note):
    text = " ".join(str(note).split()) if note not in (None, "") else ""
    if len(text) > MAX_NOTE:
        raise HelpError("Keep the note under {} characters.".format(MAX_NOTE))
    return text or None


def _clean_username(username):
    text = " ".join(str(username or "").split())[:80]
    if not text:
        raise HelpError("Nobody is logged in.", 401, "no_user")
    return text


# -- categories and config ----------------------------------------------------------------------------

def categories(conn, include_inactive=False):
    """[{key, label, active}] in sort order. A retired category (active 0) is only listed on request."""
    rows = conn.execute("SELECT key, label, active FROM help_categories {} ORDER BY sort_order, label".format(
        "" if include_inactive else "WHERE active = 1")).fetchall()
    return [{"key": r["key"], "label": r["label"], "active": bool(r["active"])} for r in rows]


def _labels(conn):
    return {r["key"]: r["label"] for r in conn.execute("SELECT key, label FROM help_categories")}


def config_view(conn, user=None):
    """Everything the page needs to draw the form (GET /help/config)."""
    user = user or current_user()
    limits = help_uploads.limits()
    limits.update({"min_description": MIN_DESCRIPTION, "max_description": MAX_DESCRIPTION,
                   "requests_per_hour": RATE_LIMIT})
    return {
        "categories": [{"key": c["key"], "label": c["label"]} for c in categories(conn)],
        "severities": [{"key": k, "label": SEVERITY_LABELS[k]} for k in SEVERITIES],
        "default_severity": DEFAULT_SEVERITY,
        "disclaimer": DISCLAIMER,
        "limits": limits,
        "sla_hours": settings.help_sla_hours(conn),
        "username": user["username"],
    }


# -- reading ----------------------------------------------------------------------------------------

_COLUMNS = ("id, ticket_no, created_at, username, category_key, severity, description, source, status, sla_hours, "
            "sla_due_at, resolved_at, context_json, exported_at, notified_at")


def _next_statuses(status):
    """The statuses a request in `status` may move to: any later one, and (from resolved or closed) back to in_progress."""
    later = [s for s in STATUSES if STATUSES.index(s) > STATUSES.index(status)]
    if status in DONE_STATUSES:
        later = ["in_progress"] + [s for s in later]
    return later


def _view(row, labels, now):
    state = sla_state(row["status"], row["sla_hours"], row["sla_due_at"], row["resolved_at"], now)
    return {
        "id": row["id"], "ticket_no": row["ticket_no"], "created_at": row["created_at"], "username": row["username"],
        "category": row["category_key"], "category_label": labels.get(row["category_key"], row["category_key"]),
        "severity": row["severity"], "severity_label": SEVERITY_LABELS.get(row["severity"], row["severity"]),
        "description": row["description"], "source": row["source"],
        "status": row["status"], "status_label": STATUS_LABELS.get(row["status"], row["status"]),
        "next_statuses": _next_statuses(row["status"]),
        "sla_hours": row["sla_hours"], "sla_due_at": row["sla_due_at"], "sla_state": state,
        "seconds_left": seconds_left(row["status"], row["sla_due_at"], now),
        "resolved_at": row["resolved_at"], "exported_at": row["exported_at"],
    }


def _attachments(conn, request_id):
    rows = conn.execute("SELECT id, original_name, mime, bytes, created_at FROM help_attachments WHERE request_id = ? ORDER BY id",
                        (request_id,)).fetchall()
    return [dict(r) for r in rows]


def _events(conn, request_id):
    rows = conn.execute("SELECT at, actor, kind, from_status, to_status, note FROM help_request_events "
                        "WHERE request_id = ? ORDER BY id", (request_id,)).fetchall()
    return [dict(r) for r in rows]


def _detail(conn, row, labels, now):
    view = _view(row, labels, now)
    try:
        view["context"] = json.loads(row["context_json"]) if row["context_json"] else {}
    except ValueError:
        view["context"] = {}
    view["attachments"] = _attachments(conn, row["id"])
    view["events"] = _events(conn, row["id"])
    return view


def get_request(conn, request_id, username=None, now=None):
    """One request with its attachments and event history. With `username`, only that person's own request
    (anything else reads as missing); without it, any request (the team side). None when there is none."""
    row = conn.execute("SELECT {} FROM help_requests WHERE id = ?".format(_COLUMNS), (request_id,)).fetchone()
    if row is None or (username is not None and row["username"] != username):
        return None
    return _detail(conn, row, _labels(conn), now)


def list_for_user(conn, username, now=None):
    """The person's own requests, newest first, each with its attachment count."""
    labels = _labels(conn)
    rows = conn.execute("SELECT {} FROM help_requests WHERE username = ? ORDER BY id DESC".format(_COLUMNS), (username,)).fetchall()
    counts = {r["request_id"]: r["n"] for r in conn.execute(
        "SELECT request_id, COUNT(*) AS n FROM help_attachments GROUP BY request_id")}
    out = []
    for row in rows:
        view = _view(row, labels, now)
        view["attachment_count"] = counts.get(row["id"], 0)
        out.append(view)
    return out


def team_list(conn, status=None, category=None, overdue=False, now=None):
    """Every person's requests for the team view, newest first (at most TEAM_LIST_LIMIT), optionally filtered by
    status, category key and "overdue" (open and past the due time)."""
    if status and status not in STATUSES:
        raise HelpError("Unknown status.")
    where, params = [], []
    if status:
        where.append("status = ?")
        params.append(status)
    if category:
        where.append("category_key = ?")
        params.append(category)
    labels = _labels(conn)
    rows = conn.execute("SELECT {} FROM help_requests {} ORDER BY id DESC LIMIT ?".format(
        _COLUMNS, "WHERE " + " AND ".join(where) if where else ""), tuple(params) + (TEAM_LIST_LIMIT * (4 if overdue else 1),)).fetchall()
    counts = {r["request_id"]: r["n"] for r in conn.execute(
        "SELECT request_id, COUNT(*) AS n FROM help_attachments GROUP BY request_id")}
    out = []
    for row in rows:
        view = _view(row, labels, now)
        if overdue and view["sla_state"] != "overdue":
            continue
        view["attachment_count"] = counts.get(row["id"], 0)
        out.append(view)
        if len(out) >= TEAM_LIST_LIMIT:
            break
    return out


def team_summary(conn, now=None):
    """Counts over every request: {total, by_status, by_category, by_sla}. Zero counts are included for the
    statuses and SLA states, and for every category that has a row (retired ones too)."""
    labels = _labels(conn)
    by_status = {s: 0 for s in STATUSES}
    by_sla = {s: 0 for s in SLA_STATES}
    by_category = {}
    total = 0
    for row in conn.execute("SELECT status, category_key, sla_hours, sla_due_at, resolved_at FROM help_requests"):
        total += 1
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
        state = sla_state(row["status"], row["sla_hours"], row["sla_due_at"], row["resolved_at"], now)
        by_sla[state] += 1
        by_category[row["category_key"]] = by_category.get(row["category_key"], 0) + 1
    for category in categories(conn):
        by_category.setdefault(category["key"], 0)
    return {"total": total, "by_status": by_status, "by_sla": by_sla,
            "by_category": [{"key": k, "label": labels.get(k, k), "count": n} for k, n in sorted(by_category.items())]}


# -- creating ---------------------------------------------------------------------------------------

def _begin(conn):
    """Start a write transaction that holds the write lock from the first statement, so two people sending at
    once cannot get the same ticket number or both slip past the rate limit."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")


def _event(conn, request_id, now, actor, kind, from_status=None, to_status=None, note=None):
    conn.execute("INSERT INTO help_request_events (request_id, at, actor, kind, from_status, to_status, note) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)", (request_id, _stamp(_now(now)), actor, kind, from_status, to_status, note))


def _check_rate(conn, username, now):
    window_start = _stamp(_now(now) - RATE_WINDOW)
    rows = conn.execute("SELECT created_at FROM help_requests WHERE username = ? AND created_at > ? ORDER BY created_at",
                        (username, window_start)).fetchall()
    if len(rows) >= RATE_LIMIT:
        free_at = _parse(rows[len(rows) - RATE_LIMIT]["created_at"]) + RATE_WINDOW
        raise HelpError(
            "You have already sent {} requests in the last hour. Please wait until about {} and send it again.".format(
                RATE_LIMIT, free_at.strftime("%H:%M")), 429, "rate_limited", retry_after=_stamp(free_at))


def validate_fields(conn, category_key, description, severity, source, has_files):
    """The checks that need no file and no write: (category key, severity, source, stored text, masked?).
    Raises HelpError. Also used before a file is touched, so a bad form costs nothing."""
    if not isinstance(category_key, str) or conn.execute(
            "SELECT 1 FROM help_categories WHERE key = ? AND active = 1", (category_key,)).fetchone() is None:
        raise HelpError("Choose what this is about from the list.")
    severity = DEFAULT_SEVERITY if severity in (None, "") else severity
    if severity not in SEVERITIES:
        raise HelpError("Choose how much it slows you down from the list.")
    source = "typed" if source in (None, "") else source
    if source not in SOURCES:
        raise HelpError("Unknown source.")
    text, masked = clean_description(description)
    words = text.replace(MASK, "").strip()
    if not words:
        raise HelpError("Describe the problem in a few words.")
    if not has_files and len(words) < MIN_DESCRIPTION:
        raise HelpError("Please write at least {} characters, or attach a screenshot.".format(MIN_DESCRIPTION))
    return category_key, severity, source, text, masked


def create_request(conn, username, category_key, description, severity=None, source=None, staged=None, context=None,
                   now=None, root=None, extra_context=None):
    """Store one request (and its staged files) all-or-nothing and return the confirmation data
    {ticket_no, category, category_label, created_at, sla_hours, sla_due_at, sla_state, username, status,
    severity, attachments, masked}. Raises HelpError (and help_uploads.UploadError from the caller's staging).
    On any failure nothing is stored and the staged files are removed."""
    root = root or help_uploads.upload_root()
    try:
        username = _clean_username(username)
        files = list(staged.files) if staged is not None else []
        category_key, severity, source, text, masked = validate_fields(conn, category_key, description, severity, source,
                                                                       bool(files))
        moment = _now(now)
        sla_hours = settings.help_sla_hours(conn)
        due = moment + timedelta(hours=sla_hours)
        context_json = json.dumps(clean_context(context, extra_context), sort_keys=True)
        published = None
        _begin(conn)
        try:
            _check_rate(conn, username, moment)
            top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM help_requests").fetchone()[0]
            try:
                seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'help_requests'").fetchone()
            except sqlite3.OperationalError:
                seq = None
            request_id = max(top, seq[0] if seq else 0) + 1
            ticket = "HELP-{:04d}".format(request_id)
            conn.execute(
                "INSERT INTO help_requests (id, ticket_no, created_at, username, category_key, severity, description, source, "
                "status, sla_hours, sla_due_at, context_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?)",
                (request_id, ticket, _stamp(moment), username, category_key, severity, text, source, sla_hours, _stamp(due),
                 context_json))
            for item in files:
                conn.execute(
                    "INSERT INTO help_attachments (request_id, original_name, stored_name, mime, bytes, sha256, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (request_id, item["original_name"], item["stored_name"], item["mime"], item["bytes"], item["sha256"],
                     _stamp(moment)))
            _event(conn, request_id, moment, "user", "created", None, "new")
            if files:
                published = help_uploads.publish_folder(staged, ticket, root)
            conn.commit()
        except BaseException:
            conn.rollback()
            if published is not None:
                shutil.rmtree(str(published), ignore_errors=True)
            raise
    except BaseException:
        if staged is not None:
            staged.discard()
        raise
    detail = get_request(conn, request_id, username, moment)
    return {
        "id": request_id, "ticket_no": ticket, "category": category_key, "category_label": detail["category_label"],
        "severity": severity, "severity_label": detail["severity_label"], "created_at": detail["created_at"],
        "sla_hours": sla_hours, "sla_due_at": detail["sla_due_at"], "sla_state": detail["sla_state"],
        "username": username, "status": "new", "status_label": STATUS_LABELS["new"],
        "attachments": detail["attachments"], "masked": masked,
    }


# -- changing the status -----------------------------------------------------------------------------

def transition_allowed(current, target):
    """Forward (to any later status) is allowed; the only way back is to reopen a resolved or closed request
    as in_progress."""
    if current not in STATUSES or target not in STATUSES or current == target:
        return False
    return target in _next_statuses(current)


def _apply(conn, row, target, note, actor, now, dedupe_note=False):
    """Change one request. Returns "updated", "noted" (same status, a note was added), "unchanged" or "stale"
    (the move is not allowed from where the request is now). The caller owns the transaction."""
    current = row["status"]
    moment = _now(now)
    if target == current:
        if not note:
            return "unchanged"
        if dedupe_note and conn.execute(
                "SELECT 1 FROM help_request_events WHERE request_id = ? AND actor = ? AND kind IN ('note', 'status') AND note = ?",
                (row["id"], actor, note)).fetchone():
            return "unchanged"
        _event(conn, row["id"], moment, actor, "note", current, current, note)
        return "noted"
    if not transition_allowed(current, target):
        return "stale"
    if target in DONE_STATUSES:
        resolved_at = row["resolved_at"] or _stamp(moment)
    else:
        resolved_at = None            # reopened
    reset_notice = target == "resolved" or target not in DONE_STATUSES
    conn.execute("UPDATE help_requests SET status = ?, resolved_at = ?, notified_at = CASE WHEN ? THEN NULL ELSE notified_at END "
                 "WHERE id = ?", (target, resolved_at, 1 if reset_notice else 0, row["id"]))
    _event(conn, row["id"], moment, actor, "status", current, target, note)
    return "updated"


def set_status(conn, request_id, status, note=None, actor="team", now=None):
    """The team moves a request to `status`, with an optional note (shown in its history). With the status it
    already has, a note is just added to the history. Returns the updated detail."""
    if status not in STATUSES:
        raise HelpError("Unknown status.")
    if actor not in ("team", "import"):
        raise HelpError("Unknown actor.")
    clean = _clip_note(note)
    _begin(conn)
    try:
        row = conn.execute("SELECT {} FROM help_requests WHERE id = ?".format(_COLUMNS), (request_id,)).fetchone()
        if row is None:
            raise HelpError("That request does not exist.", 404, "not_found")
        outcome = _apply(conn, row, status, clean, actor, now)
        if outcome == "unchanged":
            raise HelpError("It is already {}.".format(STATUS_LABELS[status].lower()))
        if outcome == "stale":
            raise HelpError("A {} request can only be moved on, or reopened as In progress.".format(
                STATUS_LABELS[row["status"]].lower()))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return get_request(conn, request_id, None, now)


# -- telling the person it is resolved ----------------------------------------------------------------

def pending_notices(conn, username):
    """Resolved requests this person has not been told about yet: [{id, ticket_no, summary, note}]."""
    rows = conn.execute("SELECT id, ticket_no, description FROM help_requests WHERE username = ? AND status = 'resolved' "
                        "AND notified_at IS NULL ORDER BY resolved_at, id", (username,)).fetchall()
    out = []
    for r in rows:
        note = conn.execute("SELECT note FROM help_request_events WHERE request_id = ? AND kind = 'status' AND to_status = 'resolved' "
                            "ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
        summary = " ".join(r["description"].split())
        out.append({"id": r["id"], "ticket_no": r["ticket_no"], "summary": summary[:80] + ("..." if len(summary) > 80 else ""),
                    "note": note["note"] if note else None})
    return out


def mark_notified(conn, request_id, username, now=None):
    """This person has been told their request is resolved: never show it again. Only their own, only while it is
    resolved. True when a row changed."""
    cur = conn.execute("UPDATE help_requests SET notified_at = ? WHERE id = ? AND username = ? AND status = 'resolved' "
                       "AND notified_at IS NULL", (_stamp(_now(now)), request_id, username))
    conn.commit()
    return cur.rowcount > 0


def notice_text(item):
    """The one-line notice shown to the person (fixed wording)."""
    text = "Your request {} has been resolved.".format(item["ticket_no"])
    if item.get("note"):
        text += " " + str(item["note"]).rstrip(". ") + "."
    return text


# -- the developer's updates file ----------------------------------------------------------------------

def normalise_ticket(value):
    """'HELP-0007' for 'help-7', 'HELP 7', '7' and the like; None when it is not a ticket number."""
    match = re.fullmatch(r"(?i)\s*(?:HELP[-_ ]?)?(\d{1,9})\s*", str(value if value is not None else ""))
    return "HELP-{:04d}".format(int(match.group(1))) if match else None


def normalise_status(value):
    key = re.sub(r"[\s\-]+", "_", str(value or "").strip().lower())
    return key if key in STATUSES else None


def parse_updates(content):
    """Rows [{ticket_no, status, note}] from the text of an updates file: CSV with a header row
    (ticket_no,status,note) or JSON (a list of such objects, or {"updates": [...]}). Raises HelpError when the
    file as a whole cannot be read. A row problem is not found here: import_updates reports those."""
    if isinstance(content, bytes):
        if len(content) > MAX_IMPORT_BYTES:
            raise HelpError("The updates file is larger than {} KB.".format(MAX_IMPORT_BYTES // 1024), 413, "too_large")
        try:
            content = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise HelpError("The updates file must be UTF-8 text (CSV or JSON).")
    text = str(content or "").strip()
    if not text:
        raise HelpError("The updates file is empty.")
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except ValueError:
            raise HelpError("The updates file looks like JSON but could not be read.")
        if isinstance(data, dict):
            data = data.get("updates")
        if not isinstance(data, list):
            raise HelpError('JSON updates must be a list of {"ticket_no", "status", "note"} objects.')
        rows = [item for item in data]
    else:
        reader = csv.DictReader(io.StringIO(text))
        fields = [(f or "").strip().lower() for f in (reader.fieldnames or [])]
        if "ticket_no" not in fields or "status" not in fields:
            raise HelpError("The CSV needs a header row with ticket_no and status (note is optional).")
        rows = []
        for item in reader:
            rows.append({(k or "").strip().lower(): v for k, v in item.items() if k is not None})
    if len(rows) > MAX_IMPORT_ROWS:
        raise HelpError("At most {} rows can be imported at once.".format(MAX_IMPORT_ROWS))
    return rows


def import_updates(conn, rows, now=None):
    """Apply updates rows. Idempotent: a row that says what is already true changes nothing and adds no event,
    so the same file can be imported twice. An unknown ticket, a bad status or a move that is no longer allowed
    (an old file replayed after the request moved on) is reported, never fatal. Every change is an event with
    actor 'import'. Returns {rows, updated, notes_added, unchanged, stale, unknown_tickets, invalid}."""
    summary = {"rows": 0, "updated": 0, "notes_added": 0, "unchanged": 0, "stale": 0, "unknown_tickets": [], "invalid": []}
    if not isinstance(rows, list):
        raise HelpError("Nothing to import.")
    _begin(conn)
    try:
        for number, item in enumerate(rows, start=1):
            summary["rows"] += 1
            if not isinstance(item, dict):
                summary["invalid"].append({"row": number, "reason": "not an object"})
                continue
            ticket = normalise_ticket(item.get("ticket_no"))
            status = normalise_status(item.get("status"))
            if ticket is None:
                summary["invalid"].append({"row": number, "reason": "no valid ticket_no"})
                continue
            if status is None:
                summary["invalid"].append({"row": number, "ticket_no": ticket, "reason": "unknown status"})
                continue
            try:
                note = _clip_note(item.get("note"))
            except HelpError as exc:
                summary["invalid"].append({"row": number, "ticket_no": ticket, "reason": str(exc)})
                continue
            row = conn.execute("SELECT {} FROM help_requests WHERE ticket_no = ?".format(_COLUMNS), (ticket,)).fetchone()
            if row is None:
                if ticket not in summary["unknown_tickets"]:
                    summary["unknown_tickets"].append(ticket)
                continue
            outcome = _apply(conn, row, status, note, "import", now, dedupe_note=True)
            if outcome == "updated":
                summary["updated"] += 1
            elif outcome == "noted":
                summary["notes_added"] += 1
            else:
                summary[outcome] += 1
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return summary


# -- the export bundle ----------------------------------------------------------------------------------

def _csv_cell(value):
    """A cell safe to open in a spreadsheet: text that starts like a formula is prefixed with an apostrophe."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


README = """Clinic Copilot: "Need help" export
==================================

Exported {exported} (clinic local time). {count} request(s).

What is in this ZIP
-------------------
requests.csv      one row per request (open it in a spreadsheet)
requests.json     the same requests with their attachments list and full event history
attachments/      the files people attached, one folder per ticket (HELP-0001/...)
README.txt        this file

These are user-experience reports (confusing screens, slow steps, things people could not find, voice or
language not understood). They are not bug reports. Descriptions have any run of 8 or more digits hidden as
"[number hidden]" before they were stored, and people were asked not to mention patients, so treat the
content as internal anyway and do not forward it further than you need to.

Sending status updates back
---------------------------
Send back a file of updates and the team imports it in the app (Need help -> Team view -> Import updates).
Importing is safe to repeat: a row that says what is already true changes nothing.

CSV: a header row, then one row per ticket.

    ticket_no,status,note
    HELP-0001,in_progress,Looking at the booking screen this week
    HELP-0002,resolved,Fixed in the update on 12 Oct: the Save button is now at the top

JSON: a list of objects with the same three fields (note is optional).

    [{{"ticket_no": "HELP-0001", "status": "in_progress", "note": "Looking at it"}}]

ticket_no  as in requests.csv (HELP-0001). HELP-1 and 1 also work.
status     one of: new, acknowledged, in_progress, resolved, closed (in progress and In-Progress also work).
note       optional, up to {max_note} characters. It is shown to the person in the request history, and a note
           on a request that becomes "resolved" is included in the message they see.

A request moves forward (new -> acknowledged -> in_progress -> resolved -> closed, skipping steps is fine). The
only way back is reopening a resolved or closed request as in_progress. A row that would move a request
backwards (for example an old file imported again) is skipped and reported, not applied.
Tickets that are not in this app are reported, not treated as errors.
"""


def _since_stamp(since):
    text = str(since or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return _stamp(datetime.strptime(text, fmt))
        except ValueError:
            continue
    raise HelpError("since must be a date like 2026-10-01 (or 2026-10-01 14:30).")


def export_bundle(conn, root=None, status=None, since=None, only_unexported=False, mark_exported=False, now=None):
    """Build the export ZIP for the requests matching the filters. Returns (file object positioned at the start,
    number of requests). Nothing is stamped unless `mark_exported`, so an export can be repeated; with it, each
    included request that has not been exported before gets exported_at and an 'exported' event."""
    root = root or help_uploads.upload_root()
    moment = _now(now)
    if status and status not in STATUSES:
        raise HelpError("Unknown status.")
    where, params = [], []
    if status:
        where.append("status = ?")
        params.append(status)
    if since:
        where.append("created_at >= ?")
        params.append(_since_stamp(since))
    if only_unexported:
        where.append("exported_at IS NULL")
    rows = conn.execute("SELECT {} FROM help_requests {} ORDER BY id".format(
        _COLUMNS, "WHERE " + " AND ".join(where) if where else ""), tuple(params)).fetchall()
    labels = _labels(conn)
    out = tempfile.TemporaryFile()
    csv_buffer = io.StringIO(newline="")
    writer = csv.writer(csv_buffer)
    columns = ["ticket_no", "created_at", "username", "category_key", "category", "severity", "status", "source", "sla_hours",
               "sla_due_at", "sla_state", "resolved_at", "previously_exported_at", "description", "attachments", "context"]
    writer.writerow(columns)
    records = []
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
            for row in rows:
                detail = _detail(conn, row, labels, moment)
                names = []
                for number, item in enumerate(detail["attachments"], start=1):
                    arcname = "attachments/{}/{:02d}_{}".format(row["ticket_no"], number, help_uploads.display_name(item["original_name"]))
                    path = help_uploads.stored_path(row["ticket_no"], _stored_name(conn, item["id"]), root)
                    if path is None:
                        item["file_missing"] = True
                        continue
                    archive.write(str(path), arcname)
                    item["file"] = arcname
                    names.append(arcname)
                detail.pop("next_statuses", None)
                records.append(detail)
                writer.writerow([_csv_cell(v) for v in (
                    row["ticket_no"], row["created_at"], row["username"], row["category_key"], detail["category_label"],
                    row["severity"], row["status"], row["source"], row["sla_hours"], row["sla_due_at"], detail["sla_state"],
                    row["resolved_at"], row["exported_at"], row["description"], "; ".join(names),
                    json.dumps(detail["context"], sort_keys=True))])
            archive.writestr("requests.csv", "﻿" + csv_buffer.getvalue())
            archive.writestr("requests.json", json.dumps({"exported_at": _stamp(moment), "requests": records}, indent=2,
                                                          ensure_ascii=False))
            archive.writestr("README.txt", README.format(exported=_stamp(moment), count=len(rows), max_note=MAX_NOTE))
    except BaseException:
        out.close()
        raise
    if mark_exported and rows:
        _begin(conn)
        try:
            for row in rows:
                changed = conn.execute("UPDATE help_requests SET exported_at = ? WHERE id = ? AND exported_at IS NULL",
                                       (_stamp(moment), row["id"])).rowcount
                if changed:
                    _event(conn, row["id"], moment, "team", "exported")
            conn.commit()
        except BaseException:
            conn.rollback()
            out.close()
            raise
    out.seek(0)
    return out, len(rows)


def _stored_name(conn, attachment_id):
    row = conn.execute("SELECT stored_name FROM help_attachments WHERE id = ?", (attachment_id,)).fetchone()
    return row["stored_name"] if row else None


# -- one attachment ---------------------------------------------------------------------------------------

def attachment_for_download(conn, attachment_id, username=None, root=None):
    """(path, original_name, mime) of an attachment, or None when it does not exist, is not this person's (when
    `username` is given), or its file is gone."""
    row = conn.execute(
        "SELECT a.original_name, a.stored_name, a.mime, r.ticket_no, r.username FROM help_attachments a "
        "JOIN help_requests r ON r.id = a.request_id WHERE a.id = ?", (attachment_id,)).fetchone()
    if row is None or (username is not None and row["username"] != username):
        return None
    path = help_uploads.stored_path(row["ticket_no"], row["stored_name"], root)
    if path is None:
        return None
    return path, help_uploads.display_name(row["original_name"]), row["mime"]
