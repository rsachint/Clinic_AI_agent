"""Patient appointment activity log (the additive `patient_activity` table).

One row every time a patient initiates an appointment event over WhatsApp --
including requests that were escalated to staff, blocked, or lost a race -- and
one for every staff direct edit and every Undo. It exists so the assistant (and
staff) can see a patient's appointment history, and it backs the Automation
tab's feed. It is NOT the audit trail: audit_log stays the immutable record of
every write, and nothing here is consulted to decide whether a write happens
(except the daily-cap counter, which only reads this table).

`log()` never raises: an activity-log problem must not fail or mask a write
that already succeeded.
"""

import json
import logging
from datetime import datetime

from clinic.entity_resolution import last10_digits

_logger = logging.getLogger(__name__)

EVENTS = (
    "requested", "auto_booked", "auto_cancelled", "auto_rescheduled", "escalated", "blocked", "conflict",
    "undone", "staff_booked", "staff_cancelled", "staff_rescheduled",
)
SOURCES = ("whatsapp-agent", "staff")
AUTO_EVENTS = ("auto_booked", "auto_cancelled", "auto_rescheduled")
# What the feed shows: outcomes, not the bare "requested" marker (that one
# shows in a patient's own timeline).
FEED_EVENTS = AUTO_EVENTS + ("escalated", "blocked", "conflict", "undone")

LABELS = {
    "requested": "Asked for an appointment change",
    "auto_booked": "Booked automatically",
    "auto_cancelled": "Cancelled automatically",
    "auto_rescheduled": "Rescheduled automatically",
    "escalated": "Sent to staff",
    "blocked": "Time not available (booking block)",
    "conflict": "Slot was just taken",
    "undone": "Automatic action undone",
    "staff_booked": "Booked by staff",
    "staff_cancelled": "Cancelled by staff",
    "staff_rescheduled": "Rescheduled by staff",
}


def _stamp(now):
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def log(conn, *, event, source, wa_id=None, patient_id=None, patient_name=None, appointment_id=None,
        detail=None, meta=None, proposal_id=None, now=None):
    """Append one activity row; returns its id, or None if it could not be
    written (logged, never raised)."""
    try:
        if event not in EVENTS:
            raise ValueError("unknown patient_activity event {!r}".format(event))
        if source not in SOURCES:
            raise ValueError("unknown patient_activity source {!r}".format(source))
        cur = conn.execute(
            "INSERT INTO patient_activity (patient_id, wa_id, patient_name, appointment_id, event, source, detail, "
            "created_at, proposal_id, meta_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (patient_id, wa_id, patient_name, appointment_id, event, source, detail, _stamp(now), proposal_id,
             json.dumps(meta, ensure_ascii=False) if meta else None),
        )
        conn.commit()
        return cur.lastrowid
    except Exception:
        _logger.exception("could not write the patient activity row (event=%s); the write itself is unaffected", event)
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def _row(r):
    item = {k: r[k] for k in r.keys()}
    try:
        item["meta"] = json.loads(r["meta_json"]) if r["meta_json"] else {}
    except ValueError:
        item["meta"] = {}
    item.pop("meta_json", None)
    item["label"] = LABELS.get(r["event"], r["event"])
    return item


def count_on(conn, event, day):
    """How many `event` rows were written on calendar day `day` (YYYY-MM-DD,
    the clinic's local day)."""
    return conn.execute(
        "SELECT COUNT(*) FROM patient_activity WHERE event = ? AND substr(created_at, 1, 10) = ?",
        (event, day),
    ).fetchone()[0]


def automated_bookings_on(conn, day):
    """Automated bookings created on `day` -- the number the daily cap limits.
    Counts every one ever created that day, undone or not (it is a flood
    guard on how many the automation did, not on what is still standing)."""
    return count_on(conn, "auto_booked", day)


def timeline_for_patient(conn, patient_id, limit=100):
    """A registered patient's appointment activity, newest first. Matches
    rows by patient_id, and -- for requests made before they were registered
    (or by an unmatched number) -- by the WhatsApp number's last 10 digits
    against their phone."""
    patient = conn.execute("SELECT id, name, phone FROM patients WHERE id = ?", (patient_id,)).fetchone()
    if patient is None:
        return []
    target = last10_digits(patient["phone"] or "")
    out = []
    for r in conn.execute("SELECT * FROM patient_activity ORDER BY id DESC").fetchall():
        by_id = r["patient_id"] == patient_id
        by_phone = (r["patient_id"] is None and len(target) == 10 and r["wa_id"]
                    and last10_digits(r["wa_id"]) == target)
        if by_id or by_phone:
            out.append(_row(r))
            if len(out) >= limit:
                break
    return out


def feed(conn, query=None, limit=100):
    """The Automation tab's feed, newest first. `query` filters on the
    patient's name (case-insensitive substring) or the WhatsApp number's
    digits. Each item says whether Undo is currently available."""
    from clinic import auto_actions  # late: auto_actions imports this module

    q = (query or "").strip().lower()
    q_digits = "".join(ch for ch in q if ch.isdigit())
    marks = ",".join("?" for _ in FEED_EVENTS)
    items = []
    for r in conn.execute(
        "SELECT * FROM patient_activity WHERE source = 'whatsapp-agent' AND event IN ({}) "
        "OR event = 'undone' ORDER BY id DESC".format(marks),
        FEED_EVENTS,
    ).fetchall():
        name = (r["patient_name"] or "").lower()
        if q and q not in name and not (q_digits and q_digits in "".join(ch for ch in (r["wa_id"] or "") if ch.isdigit())):
            continue
        item = _row(r)
        item["undo_available"] = False
        item["undo_blocked_reason"] = None
        if r["event"] in AUTO_EVENTS and not r["undone"]:
            ok, why = auto_actions.undo_status(conn, r)
            item["undo_available"], item["undo_blocked_reason"] = ok, why
        items.append(item)
        if len(items) >= limit:
            break
    return items
