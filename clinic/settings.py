"""Tiny key/value settings store (the additive `app_settings` table).

The automatic-appointment controls, the follow-up reminder settings and the planner log
switch live here:

  auto_appointments_enabled  '1' (default) / '0'  -- the kill switch. When off,
                             every patient request goes to the staff inbox as a
                             proposal, exactly as before automation existed.
  auto_daily_cap             integer, default 40  -- how many automated
                             BOOKINGS may be created per calendar day (a flood
                             guard, not a per-appointment-date capacity).
  planner_log_enabled        '1' (default) / '0'  -- whether the tool-calling
                             planner records each command it handled in the
                             local `planner_log` table (clinic/planner_log.py).

  fu_days_before / fu_send_time / fu_hours_before / fu_earliest_send
                             when the two follow-up reminders go out (clinic/followups.py):
                             N days before at HH:MM, and N hours before the slot but never
                             earlier than HH:MM that day. fu_approved_templates is a JSON
                             list of the Meta message templates marked approved (default
                             none), so nothing tries to send one Meta has not approved.

Reads never raise: a missing table or row simply yields the default (so an
old connection or a half-migrated database behaves like "defaults"). Writes
validate and raise ValueError on nonsense.
"""

import json
import re
import sqlite3
from datetime import datetime

AUTO_ENABLED = "auto_appointments_enabled"
AUTO_DAILY_CAP = "auto_daily_cap"
PLANNER_LOG = "planner_log_enabled"

FU_DAYS_BEFORE = "fu_days_before"
FU_SEND_TIME = "fu_send_time"
FU_HOURS_BEFORE = "fu_hours_before"
FU_EARLIEST_SEND = "fu_earliest_send"
FU_APPROVED_TEMPLATES = "fu_approved_templates"

DEFAULTS = {
    AUTO_ENABLED: "1",
    AUTO_DAILY_CAP: "40",
    FU_DAYS_BEFORE: "2",
    FU_SEND_TIME: "10:00",
    FU_HOURS_BEFORE: "4",
    FU_EARLIEST_SEND: "07:00",
    FU_APPROVED_TEMPLATES: "[]",
    PLANNER_LOG: "1",
}

MAX_DAILY_CAP = 1000


def get(conn, key):
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is None or row[0] is None:
        return DEFAULTS.get(key)
    return row[0]


def set_value(conn, key, value):
    conn.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, str(value), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()


def auto_enabled(conn):
    return str(get(conn, AUTO_ENABLED)).strip() != "0"


def set_auto_enabled(conn, enabled):
    set_value(conn, AUTO_ENABLED, "1" if enabled else "0")


def auto_daily_cap(conn):
    try:
        return max(0, int(get(conn, AUTO_DAILY_CAP)))
    except (TypeError, ValueError):
        return int(DEFAULTS[AUTO_DAILY_CAP])


def set_auto_daily_cap(conn, cap):
    try:
        number = int(cap)
    except (TypeError, ValueError):
        raise ValueError("The daily cap must be a whole number.")
    if isinstance(cap, bool) or not 0 <= number <= MAX_DAILY_CAP:
        raise ValueError("The daily cap must be between 0 and {}.".format(MAX_DAILY_CAP))
    set_value(conn, AUTO_DAILY_CAP, number)


# ---------------------------------------------------------------------------
# Meta-approved WhatsApp message templates. Outside WhatsApp's 24-hour window
# only an approved template may be sent; staff mark a template approved here
# once Meta has approved it. The default (none) means nothing is ever sent as
# a template, and an out-of-window reminder just waits as "blocked".
# ---------------------------------------------------------------------------

def approved_templates(conn):
    """The set of template names staff marked approved. A bad stored value
    reads as 'none approved' (the safe direction)."""
    try:
        names = json.loads(get(conn, FU_APPROVED_TEMPLATES) or "[]")
    except ValueError:
        return set()
    return {n for n in names if isinstance(n, str)} if isinstance(names, list) else set()


def template_approved(conn, name):
    return name in approved_templates(conn)


def set_approved_templates(conn, names, known):
    """Replace the approved list. `known` is every template name that exists;
    anything else is refused (a typo must not look like an approval)."""
    if not isinstance(names, (list, tuple, set)) or not all(isinstance(n, str) for n in names):
        raise ValueError("Send the approved template names as a list.")
    unknown = sorted(set(names) - set(known))
    if unknown:
        raise ValueError("Unknown template: {}.".format(", ".join(unknown)))
    set_value(conn, FU_APPROVED_TEMPLATES, json.dumps(sorted(set(names))))


# ---------------------------------------------------------------------------
# Follow-up reminder timing
# ---------------------------------------------------------------------------

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MIN_SEND_HOUR, MAX_SEND_HOUR = 5, 22          # a reminder never goes out in the small hours


def _valid_clock(text):
    if not isinstance(text, str) or not _HHMM.match(text):
        return False
    return MIN_SEND_HOUR <= int(text[:2]) <= MAX_SEND_HOUR


def followup_reminder_settings(conn):
    """{'days_before', 'send_time', 'hours_before', 'earliest_send'}; a bad
    stored value falls back to its default."""
    def number(key, low, high):
        try:
            value = int(get(conn, key))
        except (TypeError, ValueError):
            value = int(DEFAULTS[key])
        return value if low <= value <= high else int(DEFAULTS[key])

    def clock(key):
        value = str(get(conn, key) or "")
        return value if _valid_clock(value) else DEFAULTS[key]

    return {
        "days_before": number(FU_DAYS_BEFORE, 1, 14), "send_time": clock(FU_SEND_TIME),
        "hours_before": number(FU_HOURS_BEFORE, 1, 12), "earliest_send": clock(FU_EARLIEST_SEND),
    }


def set_followup_reminder_settings(conn, days_before, send_time, hours_before, earliest_send):
    """Validate and save all four values together (all-or-nothing). Raises
    ValueError with a message meant for staff."""
    def whole(value, label, low, high):
        if isinstance(value, bool):
            raise ValueError("{} must be a whole number.".format(label))
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError("{} must be a whole number.".format(label))
        if not low <= number <= high:
            raise ValueError("{} must be between {} and {}.".format(label, low, high))
        return number

    def clock(value, label):
        value = str(value or "").strip()
        if not _valid_clock(value):
            raise ValueError("{} must be a time like 10:00, between {:02d}:00 and {:02d}:59.".format(
                label, MIN_SEND_HOUR, MAX_SEND_HOUR))
        return value

    values = {
        FU_DAYS_BEFORE: whole(days_before, "Days before", 1, 14),
        FU_SEND_TIME: clock(send_time, "Send time"),
        FU_HOURS_BEFORE: whole(hours_before, "Hours before", 1, 12),
        FU_EARLIEST_SEND: clock(earliest_send, "Earliest send time"),
    }
    for key, value in values.items():
        set_value(conn, key, value)


def planner_log_enabled(conn):
    return str(get(conn, PLANNER_LOG)).strip() != "0"


def set_planner_log_enabled(conn, enabled):
    set_value(conn, PLANNER_LOG, "1" if enabled else "0")
