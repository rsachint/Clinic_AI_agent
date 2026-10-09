"""The planner's generic read tool: a small, structured, READ-ONLY query.

The planner (clinic/nlu/planner.py) never writes SQL. For a question that has no
dedicated read intent ("list the patients", "who is absent today", "how much did
we collect this month", "appointments per doctor") it fills in a spec -- an
entity, an aggregate (list / count / sum / average / min / max), a few filters,
an optional order and group-by -- and THIS module turns that into one
parameterised SELECT:

  * the entity, every column, filter, measure, group-by and sort is looked up in
    the fixed tables below; a name that is not in them is rejected (NotListed, a
    QueryError), so no model-written text can ever reach the SQL string;
  * every value (a name, a date, an age ...) is a bound parameter;
  * the statement runs with PRAGMA query_only on, so even a bug here could not
    write, and at most MAX_ROWS rows come back (the caller says "showing the
    first N").

What is deliberately NOT here (so it can never be read, however a question is
phrased): visit notes, diagnoses (including the follow-up `diagnosis` column),
appointment notes, raw WhatsApp message text and message bodies, WhatsApp ids and
tokens, API keys, `paid_to`, the raw audit payload and activity meta. Where a
free-text column is useful (a notification's error, an audit summary, an activity
detail) it goes through a small code-side filter that keeps only whitelisted keys
or redacts numbers and tokens (see _safe_text / audit_summary).

Questions an existing read intent already answers (a patient count, a day's
appointments, free slots, missed follow-ups, today's cash book, one patient's
phone number or next appointment) are routed to that intent by
clinic/nlu/tools.py, so their answers stay exactly as they were; only the rest
reaches run().
"""

import json
import re
from collections import namedtuple
from datetime import date, datetime, timedelta

from clinic import branches

MAX_ROWS = 200
# "Next available": how far ahead the free-slot read looks and how many slots it returns (clinic/next_available.py).
DEFAULT_SEARCH_DAYS = 14
MAX_SEARCH_DAYS = 30
MAX_SLOTS = 20

ENTITIES = ("patients", "appointments", "availability", "followups", "cashbook",
            "staff", "attendance", "branches", "doctors", "schedules", "visits", "expenses",
            "reminders", "closures", "blocks", "audit", "activity")
AGGREGATES = ("list", "count", "sum", "average", "min", "max")
MEASURE_AGGREGATES = ("sum", "average", "min", "max")
APPOINTMENT_STATUSES = ("booked", "confirmed", "cancelled", "completed", "no_show", "upcoming")
FOLLOWUP_STATUSES = ("pending", "done", "missed", "cancelled")
ATTENDANCE_STATUSES = ("present", "half_day", "absent", "leave")
BRANCH_STATUSES = ("open", "closed")
CLOSURE_STATUSES = ("applied", "undone")
# What an appointment search lists when no status is asked for: everything that
# happened or is still going to, not what was cancelled or moved away.
_DEFAULT_APPOINTMENT_STATUSES = ("booked", "confirmed", "completed", "no_show")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
ORDER_WORDS = ("newest", "oldest", "highest", "lowest", "name")
_ORDER_SYNONYMS = {
    "latest": "newest", "recent": "newest", "last": "newest", "new": "newest", "desc": "newest",
    "earliest": "oldest", "old": "oldest", "first": "oldest", "asc": "oldest",
    "biggest": "highest", "largest": "highest", "most": "highest", "top": "highest", "max": "highest", "greatest": "highest",
    "smallest": "lowest", "least": "lowest", "min": "lowest", "cheapest": "lowest",
    "alphabetical": "name", "a-z": "name", "a_z": "name", "alphabetically": "name",
}

# Every filter the spec can carry; each entity then allows a subset.
FILTERS = ("patient_name", "text", "date", "date_to", "branch", "doctor", "status", "kind", "weekday", "time",
           "age_min", "age_max")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_ORDER_FIELD = re.compile(r"^([a-z_]+)[ :]+(asc|desc)$")


class QueryError(ValueError):
    """A spec that names something outside the whitelist or is malformed. The
    message is for logs and tests; the caller treats it as "not understood"."""


class NotListed(QueryError):
    """The spec names an entity, field, filter, measure, group-by or sort that is
    NOT in the whitelist (as opposed to a bad value for one that is). The planner
    treats this as "the app cannot answer that yet" (clinic/unanswered.py)."""


QueryResult = namedtuple("QueryResult", ["rows", "total", "truncated", "columns", "value", "value_key"],
                         defaults=(None, None))


# -- small code-side filters for the few free-text columns that are exposed -----

_NUMBERS = re.compile(r"\d{7,}")
_LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-]{28,}")
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_SECRET = re.compile(r"(?i)\b(token|key|secret|password|authorization)\b\s*[=:]\s*\S+")
_URL = re.compile(r"https?://\S+")


def _safe_text(value, cap=80):
    """A short, single-line version of free text with phone numbers, tokens, keys
    and links removed. Returns None for empty input."""
    if value in (None, ""):
        return None
    text = " ".join(str(value).split())
    text = _SECRET.sub(lambda m: m.group(1) + "=[removed]", text)
    text = _BEARER.sub("[removed]", text)
    text = _URL.sub("[link]", text)
    text = _LONG_TOKEN.sub("[removed]", text)
    text = _NUMBERS.sub("[number]", text)
    return text if len(text) <= cap else text[:cap - 1].rstrip() + "…"


# -- readable labels -------------------------------------------------------------

REMINDER_LABELS = {
    "booking_confirmed": "booking confirmation", "appointment_rescheduled": "reschedule notice",
    "appointment_cancelled": "cancellation notice", "token_changed": "token update",
    "reminder_day_before": "appointment reminder (day before)", "reminder_morning": "appointment reminder (morning)",
    "queue_two_ahead": "queue update (two ahead)", "your_turn": "your-turn call", "status_reply": "status reply",
    "registered": "registration welcome", "staff_message": "message from staff", "request_declined": "request declined notice",
    "appointment_cancelled_by_clinic": "cancellation by the clinic", "appointment_reinstated": "reinstated notice",
    "closure_moved": "closure: appointment moved", "closure_cancelled": "closure: appointment cancelled",
    "followup_reminder_2d": "follow-up reminder (2 days before)", "followup_reminder_4h": "follow-up reminder (4 hours before)",
}
REMINDER_STATUS_LABELS = {
    "pending": "queued", "sent": "sent", "dry_run": "not sent (test mode)", "failed": "failed",
    "blocked_no_window": "blocked: outside the 24-hour window", "skipped_no_phone": "skipped: no phone number",
}
# what a person can say -> the stored status
_REMINDER_STATUS_WORDS = {"queued": "pending", "pending": "pending", "sent": "sent", "failed": "failed", "blocked": "blocked_no_window",
                          "blocked_no_window": "blocked_no_window", "skipped": "skipped_no_phone", "skipped_no_phone": "skipped_no_phone",
                          "test": "dry_run", "dry_run": "dry_run"}
# what a person can say about the kind -> the stored events
_REMINDER_KINDS = {
    "appointment reminder": ("reminder_day_before", "reminder_morning"),
    "follow-up reminder": ("followup_reminder_2d", "followup_reminder_4h"),
    "booking confirmation": ("booking_confirmed",),
    "reschedule notice": ("appointment_rescheduled", "closure_moved"),
    "cancellation notice": ("appointment_cancelled", "appointment_cancelled_by_clinic", "closure_cancelled"),
    "queue update": ("token_changed", "queue_two_ahead", "your_turn", "status_reply"),
}

AUDIT_LABELS = {
    "register_patient": "registered a patient", "register_staff": "registered a staff member",
    "record_visit": "recorded a visit", "set_followup": "set a follow-up", "cancel_followup": "cancelled a follow-up",
    "reschedule_followup": "moved a follow-up", "book_appointment": "booked an appointment",
    "cancel_appointment": "cancelled an appointment", "reschedule_appointment": "moved an appointment",
    "restore_appointment": "restored an appointment", "queue_check_in": "checked a patient in",
    "queue_call_next": "called a patient in", "queue_mark_done": "finished a consultation",
    "queue_mark_no_show": "marked a no-show", "log_attendance": "logged attendance", "log_expense": "logged an expense",
    "schedule_followup": "scheduled a follow-up visit", "followup_synced": "follow-up updated with its appointment",
    "followup_batch_undone": "undid a follow-up batch", "followup_visited": "follow-up marked as visited",
    "followup_cancelled_by_patient": "follow-up cancelled by the patient",
    "followup_diagnosis_edited": "edited a follow-up (details not shown)",
    "followup_reminder_sent_by_hand": "sent a follow-up reminder by hand",
}
AUDIT_RECORD_LABELS = {"patient": "patient", "staff": "staff member", "visit": "visit", "followup": "follow-up",
                       "appointment": "appointment", "attendance": "attendance", "expense": "expense"}
_AUDIT_KINDS = {
    "patient": ("register_patient",), "staff": ("register_staff",), "visit": ("record_visit",),
    "follow-up": ("set_followup", "cancel_followup", "reschedule_followup", "schedule_followup", "followup_synced",
                  "followup_batch_undone", "followup_visited", "followup_cancelled_by_patient", "followup_diagnosis_edited",
                  "followup_reminder_sent_by_hand"),
    "appointment": ("book_appointment", "cancel_appointment", "reschedule_appointment", "restore_appointment"),
    "queue": ("queue_check_in", "queue_call_next", "queue_mark_done", "queue_mark_no_show"),
    "attendance": ("log_attendance",), "expense": ("log_expense",),
}

# The one place audit payloads are turned into text: ONLY these keys are ever read, so a
# diagnosis, notes, phone number or message text in a payload can never reach an answer.
_AUDIT_SAFE_CHANGE_KEYS = ("status", "due_date", "due_time")


def _rupees_text(paise):
    try:
        return rupees(int(paise) / 100.0)
    except (TypeError, ValueError):
        return None


def audit_summary(intent, payload_json):
    """A short summary of an audit payload built from a fixed list of safe keys."""
    try:
        payload = json.loads(payload_json) if isinstance(payload_json, str) else (payload_json or {})
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    parts = []

    def scalar(key):
        value = payload.get(key)
        return value if isinstance(value, (str, int, float)) and not isinstance(value, bool) else None

    who = scalar("patient_name") or scalar("name")
    if who:
        parts.append(_safe_text(who, 40))
    if scalar("role"):
        parts.append(_safe_text(scalar("role"), 30))
    day = scalar("appt_date") or scalar("visit_date") or scalar("due_date") or scalar("attendance_date") or scalar("expense_date")
    when_time = scalar("start_time") or scalar("due_time")
    if day:
        parts.append(_safe_text("{} {}".format(day, when_time) if when_time else day, 20))
    for key in ("status", "action"):
        if scalar(key):
            parts.append(_safe_text(str(scalar(key)).replace("_", " "), 20))
    changes = payload.get("changes")
    if isinstance(changes, dict):
        bits = ["{} {}".format(k.replace("_", " "), _safe_text(changes[k], 20)) for k in _AUDIT_SAFE_CHANGE_KEYS
                if isinstance(changes.get(k), (str, int, float))]
        if bits:
            parts.append(", ".join(bits))
    if isinstance(payload.get("fee_paise"), (int, float)):
        parts.append("fee " + _rupees_text(payload["fee_paise"]))
    if isinstance(payload.get("amount_paise"), (int, float)):
        parts.append(_rupees_text(payload["amount_paise"]))
    if payload.get("override_block"):
        parts.append("booking block overridden")
    parts = [p for p in parts if p]
    return _safe_text(" · ".join(parts), 120) if parts else None


ACTIVITY_SOURCE_LABELS = {"whatsapp-agent": "WhatsApp assistant", "staff": "staff"}
_ACTIVITY_KINDS = {
    "booking": ("auto_booked", "staff_booked", "followup_scheduled"),
    "cancellation": ("auto_cancelled", "staff_cancelled", "closure_cancelled", "followup_cancelled", "followup_undone"),
    "reschedule": ("auto_rescheduled", "staff_rescheduled", "closure_moved", "closure_accepted", "closure_changed"),
    "escalation": ("escalated", "blocked", "conflict"),
    "request": ("requested",),
    "undo": ("undone", "closure_undone"),
    "follow-up": ("followup_scheduled", "followup_undone", "followup_visited", "followup_cancelled"),
    "closure": ("closure_moved", "closure_cancelled", "closure_undone", "closure_accepted", "closure_changed"),
}


def _activity_label(raw):
    from clinic import patient_activity           # imported late: patient_activity pulls in the entity resolver
    return patient_activity.LABELS.get(raw) or str(raw).replace("_", " ")


def _label_of(mapping):
    return lambda raw: mapping.get(raw) or str(raw).replace("_", " ")


def _month_label(raw):
    try:
        return date.fromisoformat(str(raw) + "-01").strftime("%b %Y")
    except ValueError:
        return str(raw)


_WEEKDAY_TITLES = tuple(w.title() for w in WEEKDAYS)


def _weekday_label(raw):
    try:
        return _WEEKDAY_TITLES[int(raw)]
    except (TypeError, ValueError, IndexError):
        return str(raw)


def _weekday_of(column):
    """0 = Monday ... 6 = Sunday, from an ISO date column (SQLite's %w is 0 = Sunday)."""
    return "((CAST(strftime('%w', {}) AS INTEGER) + 6) % 7)".format(column)


_WEEKDAY_CASE = ("CASE {c} " + " ".join("WHEN {} THEN '{}'".format(i, w) for i, w in enumerate(_WEEKDAY_TITLES)) + " END")

# -- the whitelist ------------------------------------------------------------
# For each entity: FROM (a fixed string, with `?` markers filled by "from_params"), the
# output columns (name -> SQL expression), the filters it supports, the date column (or
# a (start, end) pair for ranges), the default columns and ORDER BY, the sort words,
# the numeric measures and the group-bys. Nothing outside this table is ever put in SQL.


def _sorts(date=None, number=None, name=None, tie="1"):
    """The sort words an entity supports -> a fixed ORDER BY. `date` is a tuple of
    expressions (newest = all descending)."""
    out = {}
    if date:
        out["newest"] = ", ".join(e + " DESC" for e in date) + ", {} DESC".format(tie)
        out["oldest"] = ", ".join(date) + ", {}".format(tie)
    if number:
        out["highest"] = "{} DESC, {}".format(number, tie)
        out["lowest"] = "{}, {}".format(number, tie)
    if name:
        out["name"] = "{} COLLATE NOCASE, {}".format(name, tie)
    return out


def _money(raw):
    return (raw, "rupees")


_PATIENTS = {
    "from": "patients p",
    "fields": {"name": "p.name", "phone": "p.phone", "age": "p.age", "registered_at": "p.registered_at"},
    "default_fields": ("name", "phone", "age"),
    "date_column": "date(p.registered_at)",
    "filters": ("patient_name", "age_min", "age_max", "date", "date_to"),
    "name_filter": "patient_name", "name_column": "p.name",
    "order": "p.name COLLATE NOCASE, p.id",
    "sorts": _sorts(("p.registered_at",), "p.age", "p.name", "p.id"),
    "measures": {"age": ("p.age", "years")},
    "groups": {"month": ("substr(p.registered_at, 1, 7)", _month_label), "date": ("date(p.registered_at)", None)},
    "noun": ("patient", "patients"),
}
_APPOINTMENTS = {
    "default_measure": "duration_minutes",
    "from": ("appointments a LEFT JOIN patients p ON p.id = a.patient_id "
             "LEFT JOIN branches b ON b.id = COALESCE(a.branch_id, ?) LEFT JOIN doctors d ON d.id = a.doctor_id"),
    "from_params": lambda ctx: [ctx["default_branch"]],
    "fields": {
        "patient": "COALESCE(p.name, a.patient_name)", "phone": "COALESCE(p.phone, a.patient_phone)",
        "date": "a.appt_date", "time": "a.start_time", "doctor": "d.name", "branch": "b.name", "status": "a.status",
    },
    "default_fields": ("patient", "date", "time", "doctor", "branch", "status"),
    "date_column": "a.appt_date",
    "filters": ("patient_name", "date", "date_to", "status", "branch", "doctor"),
    "name_filter": "patient_name", "name_column": "COALESCE(p.name, a.patient_name, '')",
    "doctor_column": "d.name",
    "branch_sql": ("COALESCE(a.branch_id, ?) = ?", True),
    "order": "a.appt_date, a.start_time, a.id",
    "sorts": _sorts(("a.appt_date", "a.start_time"), "a.duration_minutes", "COALESCE(p.name, a.patient_name)", "a.id"),
    "measures": {"duration_minutes": ("a.duration_minutes", "minutes")},
    "groups": {
        "doctor": ("COALESCE(d.name, 'No doctor')", None), "branch": ("COALESCE(b.name, 'No branch')", None),
        "status": ("a.status", _label_of({"no_show": "no-show"})), "date": ("a.appt_date", None),
        "month": ("substr(a.appt_date, 1, 7)", _month_label), "weekday": (_weekday_of("a.appt_date"), _weekday_label),
    },
    "noun": ("appointment", "appointments"),
}
_FOLLOWUPS = {
    "from": ("followups f JOIN patients p ON p.id = f.patient_id LEFT JOIN doctors d ON d.id = f.doctor_id "
             "LEFT JOIN branches b ON b.id = f.branch_id"),
    # `diagnosis` (and batch / appointment ids) are deliberately NOT exposed.
    "fields": {"patient": "p.name", "phone": "p.phone", "due_date": "f.due_date", "status": "f.status",
               "time": "f.due_time", "doctor": "d.name", "branch": "b.name",
               "has_slot": "CASE WHEN f.appointment_id IS NOT NULL THEN 'yes' ELSE 'no' END"},
    "default_fields": ("patient", "phone", "due_date", "time", "doctor", "branch", "status"),
    "date_column": "f.due_date",
    "filters": ("patient_name", "date", "date_to", "status", "branch", "doctor"),
    "name_filter": "patient_name", "name_column": "p.name",
    "doctor_column": "d.name",
    "branch_sql": ("f.branch_id = ?", False),
    "default_status": "pending",
    "order": "f.due_date, f.id",
    "sorts": _sorts(("f.due_date", "COALESCE(f.due_time, '')"), None, "p.name", "f.id"),
    "groups": {
        "status": ("f.status", None), "doctor": ("COALESCE(d.name, 'No doctor')", None),
        "branch": ("COALESCE(b.name, 'No branch')", None), "date": ("f.due_date", None),
        "month": ("substr(f.due_date, 1, 7)", _month_label),
    },
    "noun": ("follow-up", "follow-ups"),
}
# Fees and expenses side by side, like the day-end cash book but for any day or range.
_CASHBOOK = {
    "default_measure": "amount_rupees",
    "from": ("(SELECT v.visit_date AS day, 'fee' AS kind, COALESCE(p.name, '') AS who, v.fee_paise AS paise "
             "FROM visits v LEFT JOIN patients p ON p.id = v.patient_id "
             "UNION ALL SELECT e.expense_date, 'expense', e.description, e.amount_paise FROM expenses e) c"),
    "fields": {"date": "c.day", "type": "c.kind", "description": "c.who", "amount_rupees": "ROUND(c.paise / 100.0, 2)"},
    "default_fields": ("date", "type", "description", "amount_rupees"),
    "date_column": "c.day",
    "filters": ("patient_name", "date", "date_to", "kind"),
    "name_filter": "patient_name", "name_column": "c.who",
    "kinds": {"fee": ("fee",), "expense": ("expense",)}, "kind_column": "c.kind",
    "order": "c.day, c.kind, c.who",
    "sorts": _sorts(("c.day",), "c.paise", "c.who", "c.kind"),
    "measures": {"amount_rupees": _money("c.paise")},
    "groups": {"type": ("c.kind", None), "date": ("c.day", None), "month": ("substr(c.day, 1, 7)", _month_label)},
    "noun": ("cash book entry", "cash book entries"),
}
_VISITS = {
    "default_measure": "fee_rupees",
    "from": "visits v JOIN patients p ON p.id = v.patient_id",
    # `notes` is deliberately NOT exposed.
    "fields": {"patient": "p.name", "date": "v.visit_date", "fee_rupees": "ROUND(v.fee_paise / 100.0, 2)"},
    "default_fields": ("patient", "date", "fee_rupees"),
    "date_column": "v.visit_date",
    "filters": ("patient_name", "date", "date_to"),
    "name_filter": "patient_name", "name_column": "p.name",
    "order": "v.visit_date DESC, v.id DESC",
    "sorts": _sorts(("v.visit_date",), "v.fee_paise", "p.name", "v.id"),
    "measures": {"fee_rupees": _money("v.fee_paise")},
    "groups": {"date": ("v.visit_date", None), "month": ("substr(v.visit_date, 1, 7)", _month_label), "patient": ("p.name", None)},
    "noun": ("visit", "visits"),
}
_EXPENSES = {
    "default_measure": "amount_rupees",
    "from": "expenses e",
    # `paid_to` is deliberately NOT exposed (a free-text name of whoever was paid).
    "fields": {"date": "e.expense_date", "description": "e.description", "amount_rupees": "ROUND(e.amount_paise / 100.0, 2)"},
    "default_fields": ("date", "description", "amount_rupees"),
    "date_column": "e.expense_date",
    "filters": ("text", "date", "date_to"),
    "name_filter": "text", "text_columns": ("e.description",),
    "order": "e.expense_date DESC, e.id DESC",
    "sorts": _sorts(("e.expense_date",), "e.amount_paise", "e.description", "e.id"),
    "measures": {"amount_rupees": _money("e.amount_paise")},
    "groups": {"description": ("lower(trim(e.description))", None), "month": ("substr(e.expense_date, 1, 7)", _month_label),
               "date": ("e.expense_date", None)},
    "noun": ("expense", "expenses"),
}
_STAFF = {
    "from": "staff s LEFT JOIN branches b ON b.id = s.branch_id",
    "fields": {"name": "s.name", "role": "s.role", "phone": "s.phone", "branch": "COALESCE(b.name, 'Any')"},
    "default_fields": ("name", "role", "phone", "branch"),
    "filters": ("text", "branch"),
    "name_filter": "text", "text_columns": ("s.name", "s.role"),
    "branch_sql": ("(s.branch_id = ? OR s.branch_id IS NULL)", False),
    "order": "s.name COLLATE NOCASE, s.id",
    "sorts": _sorts(None, None, "s.name", "s.id"),
    "groups": {"role": ("COALESCE(lower(s.role), 'no role')", None), "branch": ("COALESCE(b.name, 'Any')", None)},
    "noun": ("staff member", "staff members"),
}
_ATTENDANCE = {
    "from": ("attendance att JOIN staff s ON s.id = att.staff_id LEFT JOIN branches b ON b.id = s.branch_id"),
    "fields": {"staff": "s.name", "role": "s.role", "date": "att.attendance_date", "status": "att.status"},
    "default_fields": ("staff", "role", "date", "status"),
    "date_column": "att.attendance_date",
    "filters": ("text", "date", "date_to", "status", "branch"),
    "name_filter": "text", "text_columns": ("s.name", "s.role"),
    "branch_sql": ("(s.branch_id = ? OR s.branch_id IS NULL)", False),
    "order": "att.attendance_date DESC, s.name COLLATE NOCASE, att.id",
    "sorts": _sorts(("att.attendance_date",), None, "s.name", "att.id"),
    "groups": {
        "status": ("att.status", _label_of({"half_day": "half day"})), "staff": ("s.name", None),
        "role": ("COALESCE(lower(s.role), 'no role')", None), "date": ("att.attendance_date", None),
        "month": ("substr(att.attendance_date, 1, 7)", _month_label), "branch": ("COALESCE(b.name, 'Any')", None),
    },
    "noun": ("attendance entry", "attendance entries"),
}
# A branch is "open" on a day when it is not switched off, no whole-day booking block covers
# the day, and a doctor is scheduled there that weekday (clinic/branches.py).
_BRANCH_CLOSED = "(b.status = 'closed' OR b.day_blocks > 0 OR b.doctors_today = 0)"
_BRANCH_STATE = "CASE WHEN {} THEN 'closed' ELSE 'open' END".format(_BRANCH_CLOSED)
_BLOCK_COVERS_DAY = ("k.active = 1 AND k.doctor_id IS NULL AND k.start_time IS NULL "
                     "AND (k.branch_id IS NULL OR k.branch_id = b0.id) AND k.start_date <= ? AND k.end_date >= ?")


def _branch_from_params(ctx):
    day = ctx["spec"].get("date") or ctx["today"]
    weekday = date.fromisoformat(day).weekday()
    return [day, day, day, day, weekday, day, day]


_BRANCHES = {
    "from": ("(SELECT b0.*, "
             "(SELECT COUNT(*) FROM booking_blocks k WHERE " + _BLOCK_COVERS_DAY + ") AS day_blocks, "
             "(SELECT k.reason FROM booking_blocks k WHERE " + _BLOCK_COVERS_DAY + " ORDER BY k.id LIMIT 1) AS day_block_reason, "
             "(SELECT COUNT(*) FROM doctor_schedules s JOIN doctors d ON d.id = s.doctor_id WHERE s.branch_id = b0.id "
             "AND s.weekday = ? AND d.active = 1 AND (s.valid_from IS NULL OR s.valid_from <= ?) "
             "AND (s.valid_to IS NULL OR s.valid_to >= ?)) AS doctors_today "
             "FROM branches b0 WHERE b0.active = 1) b"),
    "from_params": _branch_from_params,
    # maps_url / latitude / longitude / the patient-facing closed message are not needed to answer.
    "fields": {
        "code": "b.code", "name": "b.name", "address": "b.address", "pin": "b.pin_code", "phone": "b.phone",
        "status": _BRANCH_STATE,
        "closed_reason": ("CASE WHEN b.status = 'closed' THEN COALESCE(NULLIF(b.closed_reason, ''), 'closed') "
                          "WHEN b.day_blocks > 0 THEN COALESCE(NULLIF(b.day_block_reason, ''), 'closed for the day') "
                          "WHEN b.doctors_today = 0 THEN 'no doctor scheduled that day' END"),
    },
    "default_fields": ("code", "name", "address", "phone", "status", "closed_reason"),
    "filters": ("text", "date", "status", "branch"),
    "name_filter": "text", "text_columns": ("b.name", "b.code"),
    "branch_sql": ("b.id = ?", False),
    "order": "b.sort_order, b.id",
    "sorts": _sorts(None, None, "b.name", "b.id"),
    "groups": {"status": (_BRANCH_STATE, None)},
    "noun": ("branch", "branches"),
}
_DOCTORS = {
    "from": "doctors d",
    "fields": {"name": "d.name", "title": "d.title", "specialty": "d.specialty"},
    "default_fields": ("name", "title", "specialty"),
    "filters": ("text",),
    "name_filter": "text", "text_columns": ("d.name", "d.specialty"),
    "base_where": ("d.active = 1",),
    "order": "d.id",
    "sorts": _sorts(None, None, "d.name", "d.id"),
    "groups": {"specialty": ("COALESCE(d.specialty, 'none given')", None)},
    "noun": ("doctor", "doctors"),
}
_SCHEDULES = {
    "from": ("doctor_schedules s JOIN doctors d ON d.id = s.doctor_id JOIN branches b ON b.id = s.branch_id"),
    "fields": {"doctor": "d.name", "branch": "b.name", "weekday": _WEEKDAY_CASE.format(c="s.weekday"),
               "start_time": "s.start_time", "end_time": "s.end_time"},
    "default_fields": ("doctor", "branch", "weekday", "start_time", "end_time"),
    "filters": ("text", "doctor", "branch", "weekday", "date", "time"),
    "name_filter": "text", "text_columns": ("d.name",),
    "doctor_column": "d.name",
    "branch_sql": ("s.branch_id = ?", False),
    "base_where": ("d.active = 1", "b.active = 1"),
    "order": "b.sort_order, s.weekday, s.start_time, s.id",
    "sorts": _sorts(None, None, "d.name", "s.id"),
    "groups": {"doctor": ("d.name", None), "branch": ("b.name", None), "weekday": ("s.weekday", _weekday_label)},
    "noun": ("schedule entry", "schedule entries"),
}
_REMINDER_PATIENT = ("COALESCE(p.name, a.patient_name, (SELECT p2.name FROM patients p2 WHERE n.wa_id IS NOT NULL "
                     "AND substr(p2.phone, -10) = substr(n.wa_id, -10) ORDER BY p2.id LIMIT 1), '')")
_REMINDER_WHEN = "datetime(COALESCE(n.sent_at, n.created_at), 'localtime')"
_REMINDERS = {
    "from": ("notifications n LEFT JOIN appointments a ON a.id = n.appointment_id LEFT JOIN patients p ON p.id = a.patient_id"),
    # Message text (`body`), phone / WhatsApp ids, interactive and template JSON are deliberately NOT exposed.
    "fields": {"patient": _REMINDER_PATIENT, "kind": "n.event", "status": "n.status", "when": _REMINDER_WHEN, "error": "n.error"},
    "default_fields": ("patient", "kind", "status", "when", "error"),
    "date_column": "date({})".format(_REMINDER_WHEN),
    "filters": ("patient_name", "date", "date_to", "status", "kind"),
    "name_filter": "patient_name", "name_column": _REMINDER_PATIENT,
    "base_where": ("n.event <> 'conv_reply'",),
    "kinds": _REMINDER_KINDS, "kind_column": "n.event",
    "order": "COALESCE(n.sent_at, n.created_at) DESC, n.id DESC",
    "sorts": _sorts(("COALESCE(n.sent_at, n.created_at)",), None, _REMINDER_PATIENT, "n.id"),
    "groups": {"status": ("n.status", _label_of(REMINDER_STATUS_LABELS)), "kind": ("n.event", _label_of(REMINDER_LABELS)),
               "date": ("date({})".format(_REMINDER_WHEN), None)},
    "noun": ("message", "messages"),
}
_CLOSURES = {
    "default_measure": "patients_moved",
    "from": ("closures c JOIN branches b ON b.id = c.branch_id LEFT JOIN doctors d ON d.id = c.doctor_id "
             "LEFT JOIN (SELECT closure_id, SUM(CASE WHEN action = 'move' AND result = 'done' THEN 1 ELSE 0 END) AS moved, "
             "SUM(CASE WHEN action = 'cancel' AND result = 'done' THEN 1 ELSE 0 END) AS cancelled "
             "FROM closure_moves GROUP BY closure_id) m ON m.closure_id = c.id"),
    # The patient-facing message and the per-patient moves are not exposed (only the counts).
    "fields": {"branch": "b.name", "doctor": "d.name", "start_date": "c.start_date", "end_date": "c.end_date",
               "reason": "c.reason", "status": "c.status", "patients_moved": "COALESCE(m.moved, 0)",
               "appointments_cancelled": "COALESCE(m.cancelled, 0)"},
    "default_fields": ("branch", "doctor", "start_date", "end_date", "reason", "status", "patients_moved"),
    "date_range": ("c.start_date", "c.end_date"),
    "filters": ("date", "date_to", "status", "branch", "doctor"),
    "doctor_column": "d.name",
    "branch_sql": ("c.branch_id = ?", False),
    "default_status": "applied",
    "order": "c.start_date, c.id",
    "sorts": _sorts(("c.start_date",), "COALESCE(m.moved, 0)", "b.name", "c.id"),
    "measures": {"patients_moved": ("COALESCE(m.moved, 0)", "patients")},
    "groups": {"branch": ("b.name", None), "status": ("c.status", None), "month": ("substr(c.start_date, 1, 7)", _month_label)},
    "noun": ("closure", "closures"),
}
_BLOCKS = {
    "from": ("booking_blocks k LEFT JOIN branches b ON b.id = k.branch_id LEFT JOIN doctors d ON d.id = k.doctor_id"),
    "fields": {"start_date": "k.start_date", "end_date": "k.end_date", "start_time": "k.start_time", "end_time": "k.end_time",
               "branch": "COALESCE(b.name, 'All branches')", "doctor": "COALESCE(d.name, 'All doctors')", "reason": "k.reason"},
    "default_fields": ("start_date", "end_date", "start_time", "end_time", "branch", "doctor", "reason"),
    "date_range": ("k.start_date", "k.end_date"),
    "filters": ("date", "date_to", "branch", "doctor"),
    "doctor_column": "d.name",
    "branch_sql": ("(k.branch_id = ? OR k.branch_id IS NULL)", False),
    "base_where": ("k.active = 1",),
    "order": "k.start_date, k.id",
    "sorts": _sorts(("k.start_date",), None, None, "k.id"),
    "groups": {"branch": ("COALESCE(b.name, 'All branches')", None), "doctor": ("COALESCE(d.name, 'All doctors')", None)},
    "noun": ("booking block", "booking blocks"),
}
_AUDIT_WHEN = "datetime(a.logged_at, 'localtime')"
_AUDIT = {
    "from": "audit_log a",
    # The raw payload is read only to build the safe summary (audit_summary); it is never a column of its own.
    "fields": {"when": _AUDIT_WHEN, "action": "a.intent", "record": "a.entity_type", "summary": "a.payload_json"},
    "extras": {"summary": {"_intent": "a.intent"}},
    "default_fields": ("when", "action", "record", "summary"),
    "date_column": "date({})".format(_AUDIT_WHEN),
    "filters": ("date", "date_to", "kind"),
    "kinds": _AUDIT_KINDS, "kind_column": "a.intent",
    "order": "a.logged_at DESC, a.id DESC",
    "sorts": _sorts(("a.logged_at",), None, None, "a.id"),
    "groups": {"action": ("a.intent", _label_of(AUDIT_LABELS)), "record": ("a.entity_type", _label_of(AUDIT_RECORD_LABELS)),
               "date": ("date({})".format(_AUDIT_WHEN), None)},
    "noun": ("audit entry", "audit entries"),
}
_ACTIVITY = {
    "from": "patient_activity pa LEFT JOIN patients p ON p.id = pa.patient_id",
    # wa_id (a phone number), meta_json and the proposal id are deliberately NOT exposed.
    "fields": {"when": "pa.created_at", "event": "pa.event", "patient": "COALESCE(p.name, pa.patient_name)",
               "source": "pa.source", "detail": "pa.detail"},
    "default_fields": ("when", "event", "patient", "source", "detail"),
    "date_column": "date(pa.created_at)",
    "filters": ("patient_name", "date", "date_to", "kind"),
    "name_filter": "patient_name", "name_column": "COALESCE(p.name, pa.patient_name, '')",
    "kinds": _ACTIVITY_KINDS, "kind_column": "pa.event",
    "order": "pa.created_at DESC, pa.id DESC",
    "sorts": _sorts(("pa.created_at",), None, "COALESCE(p.name, pa.patient_name)", "pa.id"),
    "groups": {"event": ("pa.event", _activity_label),
               "source": ("pa.source", _label_of(ACTIVITY_SOURCE_LABELS)), "date": ("date(pa.created_at)", None)},
    "noun": ("activity entry", "activity entries"),
}
_TABLES = {
    "patients": _PATIENTS, "appointments": _APPOINTMENTS, "followups": _FOLLOWUPS, "cashbook": _CASHBOOK,
    "staff": _STAFF, "attendance": _ATTENDANCE, "branches": _BRANCHES, "doctors": _DOCTORS, "schedules": _SCHEDULES,
    "visits": _VISITS, "expenses": _EXPENSES, "reminders": _REMINDERS, "closures": _CLOSURES, "blocks": _BLOCKS,
    "audit": _AUDIT, "activity": _ACTIVITY,
}

# What each entity can show / be filtered by, for the planner prompt and tests.
FIELDS = {name: tuple(table["fields"]) for name, table in _TABLES.items()}
ENTITY_FILTERS = {name: table["filters"] for name, table in _TABLES.items()}
# availability is not a table: clinic/next_available.py reads it. `date` is the day (or where a forward search starts),
# `date_to` ends a forward search, `doctor` limits it to that doctor's schedule.
ENTITY_FILTERS["availability"] = ("date", "date_to", "branch", "doctor")
FIELDS["availability"] = ()
ENTITY_MEASURES = {name: tuple(table.get("measures", {})) for name, table in _TABLES.items()}
ENTITY_GROUPS = {name: tuple(table.get("groups", {})) for name, table in _TABLES.items()}
# A name some entity has but this one does not is a slip ("fee" measured on the cash book); only a name NO entity has
# is something the app cannot answer yet (NotListed).
ALL_FIELDS = frozenset(f for fields in FIELDS.values() for f in fields)
ALL_GROUPS = frozenset(g for groups in ENTITY_GROUPS.values() for g in groups)
# Entities whose rows belong to a branch (a named branch limits them); only appointments also
# default to this computer's My branch, exactly as before.
BRANCH_ENTITIES = frozenset(name for name, table in _TABLES.items() if table.get("branch_sql")) | {"availability"}
MY_BRANCH_DEFAULT = frozenset(("appointments", "availability"))

# What a spoken measure means (the model may say "fee"; the column is fee_rupees).
_MEASURE_ALIASES = {"fee": "fee_rupees", "fees": "fee_rupees", "amount": "amount_rupees", "cost": "amount_rupees",
                    "duration": "duration_minutes", "length": "duration_minutes", "moved": "patients_moved",
                    "patients": "patients_moved", "ages": "age"}
ALL_MEASURES = frozenset(m for measures in ENTITY_MEASURES.values() for m in measures) | frozenset(_MEASURE_ALIASES)
_AGGREGATE_ALIASES = {"avg": "average", "mean": "average", "total": "sum", "minimum": "min", "maximum": "max",
                      "lowest": "min", "highest": "max", "number": "count", "how_many": "count"}
_SQL_AGGREGATE = {"sum": "SUM", "average": "AVG", "min": "MIN", "max": "MAX"}
_VALUE_PREFIX = {"sum": "total", "average": "average", "min": "lowest", "max": "highest"}


def _iso_date(value, name):
    if not isinstance(value, str) or not _ISO.match(value.strip()):
        raise QueryError("{} must be an ISO date".format(name))
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        raise QueryError("{} is not a real date".format(name))


def _whole(value, name, low, high):
    number = None
    if not isinstance(value, bool):
        try:
            number = int(value.strip()) if isinstance(value, str) else (int(value) if value == int(value) else None)
        except (TypeError, ValueError, OverflowError):
            number = None
    if number is None or not low <= number <= high:
        raise QueryError("{} must be a whole number from {} to {}".format(name, low, high))
    return number


def _words(text):
    return [w.rstrip("s") if len(w) > 3 else w for w in re.split(r"[^a-z0-9]+", text.lower()) if w]


def _kind_values(entity, text):
    """The stored values a spoken kind stands for ("follow-up reminders" -> both follow-up
    reminder events), looked up in the entity's fixed table. QueryError when none fits."""
    kinds = _TABLES[entity]["kinds"]
    wanted = _words(str(text))
    found = []
    for key, values in kinds.items():
        key_words = _words(key)
        if wanted and all(w in key_words for w in wanted):
            found += [v for v in values if v not in found]
    if not found:
        raise QueryError("unknown {} kind {!r}".format(entity, text))
    return tuple(found)


def _status_value(entity, text):
    word = str(text).strip().lower().replace(" ", "_").replace("-", "_")
    if entity == "appointments":
        allowed = APPOINTMENT_STATUSES
    elif entity == "followups":
        allowed = FOLLOWUP_STATUSES
    elif entity == "attendance":
        allowed = ATTENDANCE_STATUSES
        word = {"half": "half_day", "on_leave": "leave", "absent_today": "absent"}.get(word, word)
    elif entity == "branches":
        allowed = BRANCH_STATUSES
    elif entity == "closures":
        allowed = CLOSURE_STATUSES
    elif entity == "reminders":
        allowed = tuple(_REMINDER_STATUS_WORDS)
    else:
        raise QueryError("{} cannot be filtered by status".format(entity))
    if word not in allowed:
        raise QueryError("unknown {} status {!r}".format(entity, text))
    return word


def _normalise_order(entity, value, grouped):
    """'newest' / 'highest' ... or 'field desc' -> the stored form: a word, or 'field:asc|desc'."""
    text = str(value).strip().lower()
    if not text:
        return None
    word = _ORDER_SYNONYMS.get(text, text)
    if word in ORDER_WORDS:
        if not grouped and word not in _TABLES[entity]["sorts"]:
            raise QueryError("{} cannot be sorted by {}".format(entity, word))
        return word
    m = _ORDER_FIELD.match(text.replace("-", "_"))
    if m and not grouped:
        field = m.group(1)
        if field not in _TABLES[entity]["fields"]:
            raise (QueryError if field in ALL_FIELDS else NotListed)("{} has no field {!r} to sort by".format(entity, field))
        return "{}:{}".format(field, m.group(2))
    raise NotListed("unknown sort {!r}".format(value))


def validate_spec(spec):
    """The normalised spec (only whitelisted keys, checked values), or QueryError.
    Anything outside the whitelist -- an unknown entity, field, filter, measure,
    group-by or key, a filter the entity does not support -- is rejected (NotListed),
    never ignored."""
    if not isinstance(spec, dict):
        raise QueryError("a query spec is an object")
    allowed_keys = {"entity", "aggregate", "fields", "limit", "order", "measure", "group_by", "next"} | set(FILTERS)
    extra = sorted(str(k) for k in spec if k not in allowed_keys and spec[k] not in (None, ""))
    if extra:
        raise NotListed("unknown query key: {}".format(", ".join(extra)))
    entity = spec.get("entity")
    if entity not in ENTITIES:
        raise NotListed("unknown entity {!r}".format(entity))
    aggregate = spec.get("aggregate") or "list"
    aggregate = _AGGREGATE_ALIASES.get(str(aggregate).strip().lower(), str(aggregate).strip().lower())
    if aggregate not in AGGREGATES:
        raise NotListed("unknown aggregate {!r}".format(spec.get("aggregate")))
    out = {"entity": entity, "aggregate": aggregate}

    # patient_name and text are one filter: whichever name this entity filters by.
    spec = dict(spec)
    name_key = _TABLES[entity].get("name_filter") if entity in _TABLES else None
    named = [str(spec[k]).strip() for k in ("patient_name", "text") if spec.get(k) not in (None, "")]
    if named:
        if name_key is None:
            raise QueryError("{} cannot be filtered by name".format(entity))
        if len({n.casefold() for n in named}) > 1:
            raise QueryError("patient_name and text disagree")
        spec.pop("patient_name", None)
        spec.pop("text", None)
        spec[name_key] = named[0]

    for name in FILTERS:
        value = spec.get(name)
        if value in (None, ""):
            continue
        if name not in ENTITY_FILTERS[entity]:
            raise QueryError("{} cannot be filtered by {}".format(entity, name))
        if name in ("patient_name", "text"):
            text = str(value).strip()
            if not 1 <= len(text) <= 80:
                raise QueryError("{} must be 1-80 characters".format(name))
            out[name] = text
        elif name in ("date", "date_to"):
            out[name] = _iso_date(value, name)
        elif name == "status":
            out[name] = _status_value(entity, value)
        elif name in ("age_min", "age_max"):
            out[name] = _whole(value, name, 0, 130)
        elif name == "branch":
            out[name] = value        # a branch id / "all": resolved by the caller (clinic/nlu/tools.py)
        elif name == "doctor":
            text = str(value).strip()
            if not 1 <= len(text) <= 80:
                raise QueryError("doctor must be 1-80 characters")
            out[name] = text
        elif name == "kind":
            _kind_values(entity, value)         # validates
            out[name] = str(value).strip()[:60]
        elif name == "weekday":
            text = str(value).strip().lower()
            match = [w for w in WEEKDAYS if text == w or (len(text) >= 3 and w.startswith(text))]
            if len(match) != 1:
                raise QueryError("weekday must be monday to sunday")
            out[name] = match[0]
        elif name == "time":
            text = str(value).strip().lower()
            m = _HHMM.match(text)
            if text == "now":
                out[name] = "now"
            elif m:
                out[name] = "{:02d}:{}".format(int(m.group(1)), m.group(2))
            else:
                raise QueryError("time must be HH:MM or now")
    if "date" in out and "date_to" in out and out["date_to"] < out["date"]:
        raise QueryError("date_to is before date")
    if entity == "branches" and "date_to" in out:
        raise QueryError("a branch is open or closed on one day")
    if out.get("age_min") is not None and out.get("age_max") is not None and out["age_min"] > out["age_max"]:
        raise QueryError("age_min is above age_max")

    group_by = spec.get("group_by")
    if group_by not in (None, ""):
        key = str(group_by).strip().lower().replace(" ", "_").replace("-", "_")
        key = {"patient_name": "patient", "doctor_name": "doctor", "category": "description", "by_month": "month",
               "day": "date", "weekday_name": "weekday", "type_of_expense": "description"}.get(key, key)
        if key not in ENTITY_GROUPS.get(entity, ()):
            raise (QueryError if key in ALL_GROUPS else NotListed)("{} cannot be grouped by {!r}".format(entity, group_by))
        out["group_by"] = key
        if aggregate == "list":
            out["aggregate"] = aggregate = "count"      # "appointments per doctor" is a count per doctor

    if aggregate in MEASURE_AGGREGATES:
        raw = spec.get("measure")
        default = _TABLES[entity].get("default_measure")
        if raw in (None, "") and default:
            raw = default                  # one obvious number to add up (fees, an expense amount ...)
        if raw in (None, ""):
            raise QueryError("{} needs a measure".format(aggregate))
        measure = _MEASURE_ALIASES.get(str(raw).strip().lower(), str(raw).strip().lower())
        if measure not in ENTITY_MEASURES.get(entity, ()):
            raise (QueryError if measure in ALL_MEASURES else NotListed)("{} has no measure {!r}".format(entity, raw))
        out["measure"] = measure

    order = spec.get("order")
    if order not in (None, ""):
        if entity == "availability":
            word = str(order).strip().lower()
            if _ORDER_SYNONYMS.get(word, word) != "oldest" and word not in ("soonest", "next"):
                raise QueryError("free slots can only be asked for earliest first")
            out["next"] = True
        else:
            normalised = _normalise_order(entity, order, bool(out.get("group_by")))
            if normalised:
                out["order"] = normalised

    fields = spec.get("fields")
    if fields not in (None, "", []):
        if not isinstance(fields, (list, tuple)):
            raise QueryError("fields is a list of column names")
        chosen = []
        for field in fields:
            key = str(field).strip().lower().replace(" ", "_")
            if key not in FIELDS[entity]:
                raise (QueryError if key in ALL_FIELDS else NotListed)("{} has no field {!r}".format(entity, field))
            if key not in chosen:
                chosen.append(key)
        if chosen and aggregate == "list":
            out["fields"] = chosen
    if spec.get("limit") not in (None, ""):
        out["limit"] = _whole(spec["limit"], "limit", 1, MAX_ROWS)
    if _flag(spec.get("next")):
        if entity != "availability":
            raise QueryError("{} cannot be searched forward for the next free one".format(entity))
        out["next"] = True
    if entity == "availability":
        # A forward search is asked for by `next`, by a number of slots, or by an end date; one date alone is the
        # old one-day read. Slots are capped; the window (date .. date_to) is capped by clinic/next_available.py.
        if "limit" in out:
            out["limit"] = min(out["limit"], MAX_SLOTS)
            out["next"] = True
        if "date_to" in out:
            out["next"] = True
    return out


def _flag(value):
    """True for a yes in any of the ways a model writes it (true, "true", "yes", 1); False for no / empty."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "y", "1")
    return bool(value) and value is not None


def _like_value(text):
    return text.casefold()


def _where(entity, spec, branch_id, today, now, default_branch=None):
    """(sql fragments, params) for the filters in `spec`. The fragments come from
    this file; the values only ever travel as parameters."""
    table = _TABLES[entity]
    clauses, params = [], []
    name = spec.get("patient_name")
    if name:
        clauses.append("instr(lower({}), ?) > 0".format(table["name_column"]))
        params.append(_like_value(name))
    text = spec.get("text")
    if text:
        columns = table["text_columns"]
        clauses.append("(" + " OR ".join("instr(lower(COALESCE({}, '')), ?) > 0".format(c) for c in columns) + ")")
        params += [_like_value(text)] * len(columns)
    doctor = spec.get("doctor")
    if doctor:
        clauses.append("instr(lower(COALESCE({}, '')), ?) > 0".format(table["doctor_column"]))
        params.append(_like_value(doctor))
    start, end = spec.get("date"), spec.get("date_to")
    if table.get("date_range"):
        # a closure / block covers a stretch of days: it matches when that stretch overlaps the asked one
        low, high = table["date_range"]
        if start or end:
            clauses.append("{} >= ?".format(high))
            params.append(start or end)
            clauses.append("{} <= ?".format(low))
            params.append(end or start)
    elif table.get("date_column"):
        if start:
            clauses.append("{} >= ?".format(table["date_column"]))
            params.append(start)
        if start or end:
            clauses.append("{} <= ?".format(table["date_column"]))
            params.append(end or start)     # a lone date is that one day
    if spec.get("age_min") is not None:
        clauses.append("p.age >= ?")
        params.append(spec["age_min"])
    if spec.get("age_max") is not None:
        clauses.append("p.age <= ?")
        params.append(spec["age_max"])
    if branch_id is not None and table.get("branch_sql"):
        sql, uses_default = table["branch_sql"]
        clauses.append(sql)
        if uses_default:
            params.append(default_branch)
        params.append(int(branch_id))
    if spec.get("kind"):
        values = _kind_values(entity, spec["kind"])
        clauses.append("{} IN ({})".format(table["kind_column"], ", ".join("?" * len(values))))
        params += list(values)
    status = spec.get("status")
    if entity == "appointments":
        if status == "upcoming":
            clauses.append("a.status IN ('booked', 'confirmed') AND (a.appt_date > ? OR (a.appt_date = ? AND a.start_time >= ?))")
            params += [today, today, now]
        elif status:
            clauses.append("a.status = ?")
            params.append(status)
        else:
            clauses.append("a.status IN ({})".format(", ".join("?" * len(_DEFAULT_APPOINTMENT_STATUSES))))
            params += list(_DEFAULT_APPOINTMENT_STATUSES)
    elif entity == "followups":
        clauses.append("f.status = ?")
        params.append(status or table["default_status"])
    elif entity == "attendance" and status:
        clauses.append("att.status = ?")
        params.append(status)
    elif entity == "branches" and status:
        clauses.append("({}) = ?".format(_BRANCH_STATE))
        params.append(status)
    elif entity == "closures":
        clauses.append("c.status = ?")
        params.append(status or table["default_status"])
    elif entity == "reminders" and status:
        clauses.append("n.status = ?")
        params.append(_REMINDER_STATUS_WORDS[status])
    elif entity == "schedules":
        weekday = WEEKDAYS.index(spec["weekday"]) if spec.get("weekday") else None
        if start:
            if weekday is not None and date.fromisoformat(start).weekday() != weekday:
                clauses.append("0 = 1")           # asked for a Monday date AND a Tuesday weekday: nothing fits both
            weekday = date.fromisoformat(start).weekday()
            clauses.append("(s.valid_from IS NULL OR s.valid_from <= ?) AND (s.valid_to IS NULL OR s.valid_to >= ?)")
            params += [start, start]
        if weekday is not None:
            clauses.append("s.weekday = ?")
            params.append(weekday)
        at = spec.get("time")
        if at:
            at = now if at == "now" else at
            clauses.append("s.start_time <= ? AND s.end_time > ?")
            params += [at, at]
    return clauses, params


def _is_money(entity, measure):
    return _TABLES[entity]["measures"][measure][1] == "rupees"


def _value_sql(entity, spec):
    """(SELECT expression, key of the value in a result row) for count / sum / average / min / max."""
    aggregate = spec["aggregate"]
    if aggregate == "count":
        return "COUNT(*)", "count"
    raw, unit = _TABLES[entity]["measures"][spec["measure"]]
    function = _SQL_AGGREGATE[aggregate]
    if unit == "rupees":
        expression = "ROUND({}({}) / 100.0, 2)".format(function, raw)
    elif aggregate == "average":
        expression = "ROUND(AVG({}), 1)".format(raw)
    else:
        expression = "{}({})".format(function, raw)
    return expression, "{}_{}".format(_VALUE_PREFIX[aggregate], spec["measure"])


def _label(entity, group, raw):
    labeler = _TABLES[entity]["groups"][group][1]
    if raw is None:
        return "none"
    if callable(labeler):
        return labeler(raw)
    return raw


def _tidy(entity, column, value, row):
    """Post-process one displayed value: readable labels and the code-side safety filters."""
    if value is None:
        return None
    if entity == "reminders":
        if column == "kind":
            return REMINDER_LABELS.get(value) or str(value).replace("_", " ")
        if column == "status":
            return REMINDER_STATUS_LABELS.get(value) or str(value).replace("_", " ")
        if column == "error":
            return _safe_text(value, 80)
    elif entity == "audit":
        if column == "action":
            return AUDIT_LABELS.get(value) or str(value).replace("_", " ")
        if column == "record":
            return AUDIT_RECORD_LABELS.get(value) or str(value).replace("_", " ")
        if column == "summary":
            return audit_summary(row.get("_intent"), value)
    elif entity == "activity":
        if column == "event":
            return _activity_label(value)
        if column == "source":
            return ACTIVITY_SOURCE_LABELS.get(value) or str(value)
        if column == "detail":
            return _safe_text(value, 120)
    elif entity == "branches" and column == "closed_reason":
        return _safe_text(value, 80)
    elif entity in ("closures", "blocks") and column == "reason":
        return _safe_text(value, 80)
    return value


def _order_sql(entity, spec):
    table = _TABLES[entity]
    order = spec.get("order")
    if not order:
        return table["order"]
    if ":" in order:
        field, direction = order.split(":")
        tie = table["sorts"].get("newest") or table["sorts"].get("name") or table["order"]
        return "{} {}, {}".format(table["fields"][field], "DESC" if direction == "desc" else "ASC", tie)
    return table["sorts"][order]


def run(conn, spec, branch_id=None, today=None, now=None):
    """Run a validated spec. Returns QueryResult(rows, total, truncated, columns, value, value_key).
    list: rows (at most MAX_ROWS) and `total` matches; count: no rows, `total`;
    sum / average / min / max: `value` (rupees for money) and `total` rows measured; with group_by:
    one row per group, {group: label, <value_key>: number}. `branch_id` limits a branch-aware entity
    to that branch (None: every branch)."""
    spec = validate_spec(spec)
    entity = spec["entity"]
    if entity == "availability":
        raise QueryError("availability is answered by the free-slots read, not by a query")
    table = _TABLES[entity]
    today = today or date.today().isoformat()
    now = now or datetime.now().strftime("%H:%M")
    default_branch = branches.default_branch_id(conn) if table.get("branch_sql", (None, False))[1] or "from_params" in table else None
    context = {"today": today, "now": now, "default_branch": default_branch, "spec": spec}

    clauses, params = _where(entity, spec, branch_id, today, now, default_branch)
    from_params = table["from_params"](context) if "from_params" in table else []
    clauses = list(table.get("base_where", ())) + clauses
    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    aggregate = spec["aggregate"]
    group = spec.get("group_by")
    columns = spec.get("fields") or list(table["default_fields"])
    limit = min(spec.get("limit") or MAX_ROWS, MAX_ROWS)

    was_read_only = conn.execute("PRAGMA query_only").fetchone()[0]
    conn.execute("PRAGMA query_only = ON")
    try:
        total = conn.execute("SELECT COUNT(*) FROM {}{}".format(table["from"], where_sql), from_params + params).fetchone()[0]
        if aggregate == "count" and not group:
            return QueryResult([], total, False, columns)
        if group:
            return _grouped(conn, entity, spec, table, where_sql, from_params + params, total, limit)
        if aggregate in MEASURE_AGGREGATES:
            expression, key = _value_sql(entity, spec)
            value = conn.execute("SELECT {} FROM {}{}".format(expression, table["from"], where_sql), from_params + params).fetchone()[0]
            return QueryResult([], total, False, [key], value, key)
        extras = {}
        for column in columns:
            extras.update(table.get("extras", {}).get(column, {}))
        select = ", ".join(['{} AS "{}"'.format(table["fields"][c], c) for c in columns]
                           + ['{} AS "{}"'.format(expr, alias) for alias, expr in extras.items()])
        cursor = conn.execute(
            "SELECT {} FROM {}{} ORDER BY {} LIMIT ?".format(select, table["from"], where_sql, _order_sql(entity, spec)),
            from_params + params + [limit])
        names = columns + list(extras)
        rows = []
        for raw_row in cursor.fetchall():
            row = dict(zip(names, raw_row))
            rows.append({c: _tidy(entity, c, row[c], row) for c in columns})
    finally:
        conn.execute("PRAGMA query_only = {}".format("ON" if was_read_only else "OFF"))
    return QueryResult(rows, total, total > len(rows), columns)


def _grouped(conn, entity, spec, table, where_sql, params, total, limit):
    group = spec["group_by"]
    group_sql, labeler = table["groups"][group]
    expression, key = _value_sql(entity, spec)
    order = spec.get("order")
    by_key_default = group in ("date", "month", "weekday")
    if order in ("highest", None) and not (order is None and by_key_default):
        order_sql = "2 DESC, 1"
    elif order == "lowest":
        order_sql = "2, 1"
    elif order == "newest":
        order_sql = "1 DESC"
    else:                                   # oldest / name / the default for dates
        order_sql = "1"
    cursor = conn.execute(
        "SELECT {g} AS grp, {v} AS val FROM {f}{w} GROUP BY 1 ORDER BY {o} LIMIT ?".format(
            g=group_sql, v=expression, f=table["from"], w=where_sql, o=order_sql),
        params + [limit + 1])
    fetched = cursor.fetchall()
    truncated = len(fetched) > limit
    rows = [{group: _label(entity, group, raw), key: value} for raw, value in fetched[:limit]]
    return QueryResult(rows, total, truncated, [group, key], None, key)


# -- wording ------------------------------------------------------------------

def _day(iso):
    day = date.fromisoformat(iso)
    return "{} {} {}".format(day.strftime("%a"), day.day, day.strftime("%b"))


def rupees(value):
    """Rs 12,400 / Rs 1,24,000.50 -- Indian digit grouping, paise only when there are some."""
    value = round(float(value), 2)
    whole, paise = divmod(int(round(abs(value) * 100)), 100)
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    text = "Rs {}{}".format("-" if value < 0 else "", digits)
    return text + ".{:02d}".format(paise) if paise else text


# What a measure is called in the sentence: (the thing totalled, the single item).
_MEASURE_WORDS = {
    ("visits", "fee_rupees"): ("fees collected", "fee"),
    ("expenses", "amount_rupees"): ("expenses", "expense"),
    ("cashbook", "amount_rupees"): ("cash book amount", "cash book entry"),
    ("appointments", "duration_minutes"): ("appointment time", "appointment length"),
    ("patients", "age"): ("age", "age"),
    ("closures", "patients_moved"): ("patients moved", "patients moved"),
}
_GROUP_WORDS = {"month": "month", "date": "day", "weekday": "weekday", "doctor": "doctor", "branch": "branch", "status": "status",
                "patient": "patient", "description": "description", "type": "type", "staff": "staff member", "role": "role",
                "kind": "kind", "action": "action", "record": "record type", "source": "source", "event": "event",
                "specialty": "specialty"}


def _format_value(entity, measure, value):
    unit = _TABLES[entity]["measures"][measure][1]
    if value is None:
        return "none"
    if unit == "rupees":
        return rupees(value)
    if unit == "minutes":
        return "{:g} minutes".format(value)
    if unit == "years":
        return "{:g} years".format(value)
    return "{:g}".format(value)


def _month_scope(start, end, today):
    """'this month' / 'last month' / 'in October 2026' when [start, end] is exactly a calendar month."""
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    if first.day != 1 or (last + timedelta(days=1)).day != 1 or (first.year, first.month) != (last.year, last.month):
        return None
    if today:
        now = date.fromisoformat(today)
        this_first = now.replace(day=1)
        previous_first = (this_first - timedelta(days=1)).replace(day=1)
        if first == this_first:
            return "this month"
        if first == previous_first:
            return "last month"
    return "in {}".format(first.strftime("%B %Y"))


def _bits(spec, branch_name, today):
    bits = []
    if spec.get("patient_name"):
        bits.append("matching {}".format(spec["patient_name"]))
    if spec.get("text"):
        bits.append("matching {}".format(spec["text"]))
    if spec.get("doctor"):
        bits.append("with {}".format(spec["doctor"]))
    if spec.get("kind"):
        bits.append("of kind {}".format(spec["kind"]))
    if spec.get("age_min") is not None and spec.get("age_max") is not None:
        bits.append("aged {} to {}".format(spec["age_min"], spec["age_max"]))
    elif spec.get("age_min") is not None:
        bits.append("aged {} or more".format(spec["age_min"]))
    elif spec.get("age_max") is not None:
        bits.append("aged {} or less".format(spec["age_max"]))
    start, end = spec.get("date"), spec.get("date_to")
    if spec.get("weekday"):
        bits.append("on {}s".format(spec["weekday"].title()))
    if spec.get("time"):
        bits.append("at {}".format("this time" if spec["time"] == "now" else spec["time"]))
    if start and end and end != start:
        month = _month_scope(start, end, today)
        bits.append(month or "from {} to {}".format(_day(start), _day(end)))
    elif start or end:
        bits.append("on {}".format(_day(start or end)))
    if branch_name:
        bits.append("at {}".format(branch_name))
    return bits


def describe(spec, result, branch_name=None, today=None):
    """One plain sentence for the answer (fixed wording, never model-written). `today`
    (an ISO date) lets a whole-month range read "this month" / "last month"."""
    entity = spec["entity"]
    singular, plural = _TABLES[entity]["noun"]
    total = result.total
    status = spec.get("status") or _TABLES[entity].get("default_status") or None
    bits = _bits(spec, branch_name, today)
    qualifier = " ({})".format(status.replace("_", " ")) if status else ""
    tail = (" " + " ".join(bits)) if bits else ""
    aggregate = spec["aggregate"]
    group = spec.get("group_by")

    if aggregate in MEASURE_AGGREGATES or group:
        if not total:
            return "No {}{} found{}.".format(plural, qualifier, tail)
        by = " by {}".format(_GROUP_WORDS.get(group, group)) if group else ""
        if aggregate == "count":
            sentence = "{} {}{}{}{}.".format(total, singular if total == 1 else plural, qualifier, by, tail)
        else:
            total_phrase, item_phrase = _MEASURE_WORDS[(entity, spec["measure"])]
            lead = {"sum": "Total " + total_phrase, "average": "Average " + item_phrase,
                    "min": "Lowest " + item_phrase, "max": "Highest " + item_phrase}[aggregate]
            if group:
                sentence = "{}{}{}{}.".format(lead, by, qualifier, tail)
            else:
                sentence = "{}{}{}: {} ({} {}).".format(
                    lead, qualifier, tail, _format_value(entity, spec["measure"], result.value), total,
                    singular if total == 1 else plural)
        if group and result.truncated:
            sentence += " Showing the first {}.".format(len(result.rows))
        return sentence

    if aggregate == "list" and not total:
        return "No {}{} found{}.".format(plural, qualifier, tail)
    sentence = "{} {}{}{}.".format(total, singular if total == 1 else plural, qualifier, tail)
    if aggregate == "list" and result.truncated:
        sentence += " Showing the first {}.".format(len(result.rows))
    return sentence
