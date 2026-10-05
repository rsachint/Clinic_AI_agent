"""Run a patient-initiated appointment action AUTOMATICALLY, and Undo it.

This is the one place where the "every write is a human-approved proposal"
rule is deliberately relaxed -- for patient-initiated book / cancel /
reschedule over the WhatsApp conversation agent, and only when
clinic/auto_policy.py says every schedule check passes. It does not bypass
the machinery, it uses it:

  * the commit is `core.propose()` + `core.confirm()` with the same
    adapter-seam handlers, so the immutable audit_log gets its row and the
    confirm-time double-booking guard (plus the booking-block guard) still
    decides;
  * the proposal's source_text starts with AUTO_PREFIX so it is plainly
    marked as automatic;
  * the same post-commit hooks the staff approval route runs (patient
    notification, token-change fan-out, Google Calendar enqueue) are passed
    in as `after_commit`, so there is exactly one copy of that logic (app.py);
  * a process-level lock serializes automatic commits, and core.confirm takes
    SQLite's write lock before the handler's slot check, so two patients
    asking for the same slot at the same instant cannot both get it.

Post-commit work (hooks, the activity log) can never fail or mask the write.
"""

import json
import logging
import threading
from collections import namedtuple
from datetime import datetime

from clinic import auto_policy, core, patient_activity, scheduling
from clinic.entity_resolution import last10_digits

_logger = logging.getLogger(__name__)

AUTO_PREFIX = "[auto:whatsapp-agent]"
UNDO_PREFIX = "[staff:undo]"
STAFF_PREFIX = "[staff:dashboard]"
SOURCE = "whatsapp-agent"

# One automatic commit at a time, process-wide (threaded server).
COMMIT_LOCK = threading.RLock()

# kind: 'committed' (done, nothing else to say), 'retry' (slot just stopped
# being usable: offer other times), 'escalate' (hand to staff with `reason`).
AutoOutcome = namedtuple(
    "AutoOutcome", ["kind", "reason", "code", "intent", "appointment_id", "proposal_id", "confirmation_queued"])

_EVENT_FOR_INTENT = {
    "book_appointment": "auto_booked",
    "cancel_appointment": "auto_cancelled",
    "reschedule_appointment": "auto_rescheduled",
}
_NOTIFY_EVENT_FOR_INTENT = {
    "book_appointment": "booking_confirmed",
    "cancel_appointment": "appointment_cancelled",
    "reschedule_appointment": "appointment_rescheduled",
}


def _outcome(kind, **kw):
    base = dict(reason=None, code=None, intent=None, appointment_id=None, proposal_id=None,
                confirmation_queued=False)
    base.update(kw)
    return AutoOutcome(kind=kind, **base)


def _appointment_label(row):
    return "{} {}".format(row["appt_date"], row["start_time"])


def _appointment_info(conn, appointment_id):
    return conn.execute(
        "SELECT a.id, a.patient_id, a.appt_date, a.start_time, a.status, a.branch_id, "
        "COALESCE(p.name, a.patient_name) AS name FROM appointments a "
        "LEFT JOIN patients p ON p.id = a.patient_id WHERE a.id = ?", (appointment_id,)).fetchone()


def _count_notifications(conn, appointment_id, event):
    return conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE appointment_id = ? AND event = ?", (appointment_id, event)
    ).fetchone()[0]


def _safe_hooks(after_commit, conn, intent, slots, entity_id, wa_id, language=None):
    if after_commit is None:
        return
    try:
        after_commit(conn, intent, slots, entity_id, wa_id, language)
    except Exception:
        _logger.exception("post-commit hooks raised after a committed %s write", intent)
        try:
            conn.rollback()
        except Exception:
            pass


def handle_request(conn, *, intent, slots, wa_id, patient_id, patient_name, msg_id, now, handlers,
                   after_commit=None, source_note=None, language=None):
    """Decide and, if allowed, commit one finished WhatsApp request.

    Returns an AutoOutcome; never raises for an ordinary failure (those become
    'retry' or 'escalate'). `slots` are the handler slots; `after_commit(conn,
    intent, slots, entity_id, wa_id)` runs the shared post-commit hooks."""
    base_meta = {"intent": intent, "wa_message_id": msg_id}
    patient_activity.log(
        conn, event="requested", source=SOURCE, wa_id=wa_id, patient_id=patient_id, patient_name=patient_name,
        appointment_id=slots.get("appointment_id"), detail=_request_detail(conn, intent, slots),
        meta=dict(base_meta, **_slot_meta(slots)), now=now)

    with COMMIT_LOCK:
        try:
            decision = auto_policy.evaluate(conn, intent, slots, wa_id, now)
        except Exception:
            _logger.exception("auto policy failed; sending the request to staff")
            decision = auto_policy.Decision(False, "the automatic check could not run", "policy_error")
        if not decision.auto:
            return _not_auto(conn, decision, intent, slots, wa_id, patient_id, patient_name, now, base_meta)
        return _commit(conn, intent, slots, wa_id, patient_id, patient_name, msg_id, now, handlers, after_commit,
                       base_meta, source_note, language)


def _request_detail(conn, intent, slots):
    if intent == "book_appointment":
        return "Asked to book {} {}".format(slots.get("appt_date"), slots.get("start_time"))
    row = _appointment_info(conn, slots.get("appointment_id")) if slots.get("appointment_id") else None
    old = _appointment_label(row) if row else "an appointment"
    if intent == "cancel_appointment":
        return "Asked to cancel {}".format(old)
    return "Asked to move {} to {} {}".format(old, slots.get("appt_date"), slots.get("start_time"))


def _slot_meta(slots):
    return {k: slots[k] for k in ("appt_date", "start_time", "appointment_id", "branch_id") if slots.get(k) is not None}


def _not_auto(conn, decision, intent, slots, wa_id, patient_id, patient_name, now, meta):
    meta = dict(meta, code=decision.code, reason=decision.reason, **_slot_meta(slots))
    if decision.code in auto_policy.RETRY_CODES:
        event = "blocked" if decision.code == "blocked" else "conflict"
        patient_activity.log(
            conn, event=event, source=SOURCE, wa_id=wa_id, patient_id=patient_id, patient_name=patient_name,
            appointment_id=slots.get("appointment_id"),
            detail="Not booked: {}".format(decision.reason), meta=meta, now=now)
        return _outcome("retry", reason=decision.reason, code=decision.code, intent=intent)
    patient_activity.log(
        conn, event="escalated", source=SOURCE, wa_id=wa_id, patient_id=patient_id, patient_name=patient_name,
        appointment_id=slots.get("appointment_id"), detail="Sent to staff: {}".format(decision.reason),
        meta=meta, now=now)
    return _outcome("escalate", reason=decision.reason, code=decision.code, intent=intent)


def _commit(conn, intent, slots, wa_id, patient_id, patient_name, msg_id, now, handlers, after_commit, meta,
            source_note, language):
    handler_slots = {k: v for k, v in slots.items() if k not in ("agent_note",)}
    # Nobody reviewed this booking, so it may write the appointment and nothing
    # else: it does not register the sender as a patient (a staff-approved
    # booking does).
    handler_slots["unattended"] = True
    before = None
    if intent != "book_appointment":
        handler_slots["require_active"] = True
        before = _appointment_info(conn, slots["appointment_id"])
    source_text = "{} wa_message #{}{}".format(AUTO_PREFIX, msg_id, " -- {}".format(source_note) if source_note else "")
    proposal_id = core.propose(conn, intent, handler_slots, source_text=source_text)
    try:
        entity_type, entity_id = core.confirm(conn, proposal_id, handlers)
    except scheduling.SlotConflictError as exc:
        _reject_quietly(conn, proposal_id)
        blocked = isinstance(exc, scheduling.SlotBlockedError)
        reason = "the slot was just taken" if not blocked else "the slot is inside a booking block"
        patient_activity.log(
            conn, event="blocked" if blocked else "conflict", source=SOURCE, wa_id=wa_id, patient_id=patient_id,
            patient_name=patient_name, appointment_id=slots.get("appointment_id"),
            detail="Not booked: {}".format(reason), proposal_id=proposal_id,
            meta=dict(meta, code="blocked" if blocked else "slot_taken", reason=reason, **_slot_meta(slots)), now=now)
        return _outcome("retry", reason=reason, code="blocked" if blocked else "slot_taken", intent=intent,
                        proposal_id=proposal_id)
    except Exception as exc:
        _logger.exception("automatic %s failed at commit; sending it to staff", intent)
        _reject_quietly(conn, proposal_id)
        reason = "the automatic update failed ({})".format(type(exc).__name__)
        patient_activity.log(
            conn, event="escalated", source=SOURCE, wa_id=wa_id, patient_id=patient_id, patient_name=patient_name,
            appointment_id=slots.get("appointment_id"), detail="Sent to staff: {}".format(reason),
            proposal_id=proposal_id, meta=dict(meta, code="commit_error", reason=reason, **_slot_meta(slots)), now=now)
        return _outcome("escalate", reason=reason, code="commit_error", intent=intent, proposal_id=proposal_id)

    # The write is committed. Everything below is best effort.
    appointment_id = entity_id if intent == "book_appointment" else slots.get("appointment_id")
    notify_event = _NOTIFY_EVENT_FOR_INTENT[intent]
    queued_before = _safe_count(conn, appointment_id, notify_event)

    activity_meta = dict(meta, **_slot_meta(slots))
    activity_meta["appointment_id"] = appointment_id
    if intent == "book_appointment":
        detail = "Booked {} {}".format(slots["appt_date"], slots["start_time"])
    elif intent == "cancel_appointment":
        detail = "Cancelled {}".format(_appointment_label(before) if before else "appointment")
        if before is not None:
            activity_meta.update(old_date=before["appt_date"], old_time=before["start_time"], old_status=before["status"])
    else:
        detail = "Moved {} to {} {}".format(
            _appointment_label(before) if before else "appointment", slots["appt_date"], slots["start_time"])
        if before is not None:
            activity_meta.update(old_date=before["appt_date"], old_time=before["start_time"],
                                 old_branch_id=before["branch_id"])
    # Written while still holding the commit lock, so the daily-cap counter
    # (which reads this table) can never lag behind a booking that happened.
    patient_activity.log(
        conn, event=_EVENT_FOR_INTENT[intent], source=SOURCE, wa_id=wa_id, patient_id=patient_id,
        patient_name=patient_name or (before["name"] if before else None), appointment_id=appointment_id,
        detail=detail, proposal_id=proposal_id, meta=activity_meta, now=now)

    _safe_hooks(after_commit, conn, intent, handler_slots, entity_id, wa_id, language)
    queued = _safe_count(conn, appointment_id, notify_event) > queued_before
    return _outcome("committed", intent=intent, appointment_id=appointment_id, proposal_id=proposal_id,
                    confirmation_queued=queued)


def _session_language(conn, wa_id):
    """The language the patient was last talking to the assistant in, or None."""
    try:
        row = conn.execute("SELECT language FROM wa_sessions WHERE wa_id = ?", (wa_id,)).fetchone()
        return row["language"] if row and row["language"] in ("en", "hi", "hinglish") else None
    except Exception:
        return None


def _safe_count(conn, appointment_id, event):
    try:
        return _count_notifications(conn, appointment_id, event)
    except Exception:
        _logger.exception("could not count notifications")
        return 0


def _reject_quietly(conn, proposal_id):
    try:
        core.reject(conn, proposal_id)   # don't leave a dead 'pending' proposal behind
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Staff direct actions (dashboard buttons): same machinery, human is the click
# ---------------------------------------------------------------------------

class ActionError(Exception):
    """A staff action that was refused; `blocked` marks "inside a booking
    block" so the UI can offer 'Book anyway?'."""

    def __init__(self, message, blocked=False):
        super().__init__(message)
        self.blocked = blocked


def staff_action(conn, intent, slots, handlers, after_commit, now, *, note, event, patient_id=None,
                 patient_name=None, wa_id=None, appointment_id=None, detail=None, meta=None):
    """Propose + confirm immediately (audited, no review card) for a click a
    staff member made, then run the hooks and write the activity row. Raises
    ActionError if the write is refused. Returns (entity_id, proposal_id)."""
    with COMMIT_LOCK:
        proposal_id = core.propose(conn, intent, slots, source_text="{} {}".format(STAFF_PREFIX, note))
        try:
            _, entity_id = core.confirm(conn, proposal_id, handlers)
        except scheduling.SlotConflictError as exc:
            _reject_quietly(conn, proposal_id)
            raise ActionError(str(exc), blocked=isinstance(exc, scheduling.SlotBlockedError))
        except Exception as exc:
            _reject_quietly(conn, proposal_id)
            raise ActionError(str(exc))
        appt_id = entity_id if intent == "book_appointment" else slots.get("appointment_id")
        patient_activity.log(
            conn, event=event, source="staff", wa_id=wa_id, patient_id=patient_id, patient_name=patient_name,
            appointment_id=appointment_id or appt_id, detail=detail, proposal_id=proposal_id,
            meta=dict(meta or {}, appointment_id=appt_id), now=now)
    _safe_hooks(after_commit, conn, intent, slots, entity_id, wa_id)
    return entity_id, proposal_id


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------

def _meta(row):
    try:
        return json.loads(row["meta_json"]) if row["meta_json"] else {}
    except ValueError:
        return {}


def _later_change(conn, row):
    """The first audit_log row for this appointment AFTER the audit row of the
    action being undone -- if there is one, that action is no longer the
    appointment's latest change."""
    audit = conn.execute(
        "SELECT id, entity_id FROM audit_log WHERE proposal_id = ?", (row["proposal_id"],)).fetchone()
    if audit is None:
        return "has no audit record"
    later = conn.execute(
        "SELECT intent FROM audit_log WHERE entity_type = 'appointment' AND entity_id = ? AND id > ? "
        "ORDER BY id LIMIT 1", (audit["entity_id"], audit["id"])).fetchone()
    return later["intent"] if later else None


def undo_status(conn, row):
    """(available, why_not) for an auto_* activity row. Cheap, read-only."""
    if row["undone"]:
        return False, "already undone"
    if row["event"] not in patient_activity.AUTO_EVENTS:
        return False, "only automatic actions can be undone"
    if row["proposal_id"] is None or row["appointment_id"] is None:
        return False, "no record of the write to undo"
    later = _later_change(conn, row)
    if later == "has no audit record":
        return False, "no audit record of the write"
    if later:
        return False, "the appointment has changed since ({})".format(later.replace("_", " "))
    return True, None


def undo(conn, activity_id, handlers, after_commit, now):
    """Undo an automatic action by running the opposite write through
    propose/confirm (so it is audited), telling the patient, and marking the
    original entry so it can never be undone twice. Returns
    {'ok': True, 'message': ...} or {'ok': False, 'error': ...}; never leaves
    anything half done."""
    with COMMIT_LOCK:
        row = conn.execute("SELECT * FROM patient_activity WHERE id = ?", (activity_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": "That entry was not found."}
        available, why = undo_status(conn, row)
        if not available:
            return {"ok": False, "error": "Cannot undo: {}.".format(why)}
        meta = _meta(row)
        appointment_id = row["appointment_id"]
        current = _appointment_info(conn, appointment_id)
        if current is None:
            return {"ok": False, "error": "Cannot undo: the appointment no longer exists."}

        if row["event"] == "auto_booked":
            if current["status"] not in ("booked", "confirmed"):
                return {"ok": False, "error": "Cannot undo: the appointment is already {}.".format(current["status"])}
            intent = "cancel_appointment"
            slots = {"appointment_id": appointment_id, "by_clinic": True, "require_active": True}
            done = "Booking undone -- the appointment was cancelled and the patient was told the clinic had to cancel."
        elif row["event"] == "auto_cancelled":
            intent = "restore_appointment"
            slots = {"appointment_id": appointment_id, "status": meta.get("old_status") or "booked"}
            done = "Cancellation undone -- the appointment is back in its original slot and the patient was told."
        else:
            if current["status"] not in ("booked", "confirmed"):
                return {"ok": False, "error": "Cannot undo: the appointment is already {}.".format(current["status"])}
            if not meta.get("old_date") or not meta.get("old_time"):
                return {"ok": False, "error": "Cannot undo: the original slot was not recorded."}
            intent = "reschedule_appointment"
            slots = {"appointment_id": appointment_id, "appt_date": meta["old_date"], "start_time": meta["old_time"],
                     "require_active": True}
            if meta.get("old_branch_id"):      # the move also changed branch: put it back there
                slots["branch_id"] = meta["old_branch_id"]
                slots["restore"] = True        # back where it was, even if that was outside the doctor's hours
            done = "Reschedule undone -- the appointment is back in its original slot and the patient was told."

        # Claim the entry first: a second click (or a second browser) loses here.
        claimed = conn.execute(
            "UPDATE patient_activity SET undone = 1 WHERE id = ? AND undone = 0", (activity_id,)).rowcount
        conn.commit()
        if not claimed:
            return {"ok": False, "error": "Cannot undo: already undone."}

        proposal_id = core.propose(
            conn, intent, slots, source_text="{} undo of automatic action #{} ({})".format(
                UNDO_PREFIX, activity_id, row["event"]))
        try:
            _, entity_id = core.confirm(conn, proposal_id, handlers)
        except Exception as exc:
            _reject_quietly(conn, proposal_id)
            conn.execute("UPDATE patient_activity SET undone = 0 WHERE id = ?", (activity_id,))
            conn.commit()
            if isinstance(exc, scheduling.SlotBlockedError):
                why = "the original slot is now inside a booking block"
            elif isinstance(exc, scheduling.SlotConflictError):
                why = "the original slot is no longer free"
            else:
                why = str(exc) or type(exc).__name__
            return {"ok": False, "error": "Cannot undo: {}.".format(why)}

        patient_activity.log(
            conn, event="undone", source="staff", wa_id=row["wa_id"], patient_id=row["patient_id"],
            patient_name=row["patient_name"], appointment_id=appointment_id,
            detail="Undid '{}' (entry #{})".format(patient_activity.LABELS.get(row["event"], row["event"]), activity_id),
            proposal_id=proposal_id, meta={"undid_activity_id": activity_id, "undid_event": row["event"]}, now=now)
    _safe_hooks(after_commit, conn, intent, slots, entity_id, row["wa_id"], _session_language(conn, row["wa_id"]))
    return {"ok": True, "message": done}


def committed_for_message(conn, msg_id):
    """True if an automatic write was already committed for inbound WhatsApp
    message `msg_id` -- so that, if anything fails AFTER the commit, the
    caller must not also hand the same request to staff."""
    try:
        row = conn.execute(
            "SELECT 1 FROM proposals WHERE status = 'confirmed' AND source_text LIKE ? LIMIT 1",
            ("{} wa_message #{}%".format(AUTO_PREFIX, int(msg_id)),),
        ).fetchone()
        return row is not None
    except Exception:
        return False
