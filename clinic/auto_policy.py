"""The guard policy for AUTOMATIC patient-initiated appointment actions.

`evaluate()` answers one question: may this finished WhatsApp request be
committed by the app itself, without a staff member approving it? It is pure
(reads the database, writes nothing) and deterministic -- no model is
involved. Only three intents can ever be automatic: book_appointment,
cancel_appointment and reschedule_appointment. Everything else keeps its
approval gate.

A failed check never refuses the patient: the caller falls back to the staff
proposal path and shows `Decision.reason` on the inbox card ("Needs staff:
..."). `Decision.code` is a stable machine-readable tag for the same thing.

Checks, in order
----------------
all intents   automation switched on; staff have not taken over the chat
book          valid slot on the slot grid; not in the past; within 30
              days; inside the chosen branch's doctor hours; not in a staff block; slot free;
              fewer than 3 active bookings for this number; a name for an
              unregistered number; daily automation cap not reached
cancel        the appointment exists, is the SENDER'S OWN (matched by
              patient or phone), is still booked/confirmed, has not been
              checked in, and has not already started. There is NO
              cut-off: cancelling minutes before the slot is automatic.
reschedule    the same ownership / state checks, then the new slot passes the
              booking checks above (excluding the appointment's own slot).
"""

from collections import namedtuple
from datetime import date, datetime, timedelta

from clinic import branches, patient_activity, scheduling, settings
from clinic.entity_resolution import last10_digits, resolve_patient_by_phone
from clinic.whatsapp_pipeline import sender_appointments

AUTO_INTENTS = ("book_appointment", "cancel_appointment", "reschedule_appointment")
MAX_ACTIVE_PER_NUMBER = 3
MAX_DAYS_AHEAD = 30
MAX_NAME_LEN = 60

# Codes that mean "the slot itself is no longer usable": the patient is shown
# other times instead of being sent to staff.
RETRY_CODES = ("slot_taken", "blocked")

Decision = namedtuple("Decision", ["auto", "reason", "code"])


def _ok():
    return Decision(True, None, None)


def _no(code, reason):
    return Decision(False, reason, code)


def _sender_mode(conn, wa_id):
    row = conn.execute("SELECT mode FROM wa_sessions WHERE wa_id = ?", (wa_id,)).fetchone()
    return row["mode"] if row else "agent"


def _slot_checks(conn, appt_date, start_time, now, exclude_appointment_id=None, branch_id=None):
    """Shared by book and reschedule: is this exact slot one automation may
    use at that branch (the default when none is named)? Returns a Decision
    (auto=True when it may)."""
    try:
        day = date.fromisoformat(str(appt_date))
        scheduling._to_minutes(str(start_time))
        if len(str(start_time)) != 5:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        return _no("invalid_slot", "the requested date or time is not valid")
    start_time = str(start_time)
    today = now.date()
    if day < today or (day == today and start_time <= now.strftime("%H:%M")):
        return _no("past", "the requested time has already passed")
    if day > today + timedelta(days=MAX_DAYS_AHEAD):
        return _no("too_far", "the requested date is more than {} days ahead".format(MAX_DAYS_AHEAD))
    if start_time not in scheduling.slot_grid(conn, str(appt_date), branch_id):
        return _no("outside_hours", "the requested time is outside clinic hours" if not branches.multi_branch(conn)
                   else "the requested time is outside the branch's doctor hours")
    reason = scheduling.block_reason(conn, str(appt_date), start_time, scheduling.SLOT_MINUTES, branch_id)
    if reason is not None:
        return _no("blocked", "the requested time is inside a booking block{}".format(
            " ({})".format(reason) if reason else ""))
    if not scheduling.is_slot_free(conn, str(appt_date), start_time, scheduling.SLOT_MINUTES,
                                   exclude_appointment_id=exclude_appointment_id, branch_id=branch_id):
        return _no("slot_taken", "the requested slot is already booked")
    return _ok()


def _own_appointment(conn, appointment_id, wa_id, patient_id):
    """(row, None) when `appointment_id` is the sender's own; (None, Decision)
    when it is missing or someone else's."""
    row = conn.execute(
        "SELECT a.id, a.patient_id, a.appt_date, a.start_time, a.status, a.queue_state, a.branch_id, "
        "COALESCE(a.patient_phone, p.phone) AS phone "
        "FROM appointments a LEFT JOIN patients p ON p.id = a.patient_id WHERE a.id = ?",
        (appointment_id,),
    ).fetchone()
    if row is None:
        return None, _no("not_found", "the appointment could not be found")
    target = last10_digits(wa_id or "")
    by_patient = patient_id is not None and row["patient_id"] == patient_id
    by_phone = len(target) == 10 and last10_digits(row["phone"] or "") == target
    if not (by_patient or by_phone):
        return None, _no("not_own", "the appointment is not under the sender's number")
    return row, None


def _state_checks(row, now):
    if row["status"] not in ("booked", "confirmed"):
        return _no("not_active", "the appointment is already {}".format(row["status"]))
    if row["queue_state"]:
        return _no("checked_in", "the patient has already checked in")
    if (row["appt_date"], row["start_time"]) < (now.date().isoformat(), now.strftime("%H:%M")):
        return _no("started", "the appointment time has already passed")
    return _ok()


def evaluate(conn, intent, slots, wa_id, now):
    """Decision(auto, reason, code) for one finished WhatsApp request.
    `slots` are the slots the write handler would receive; `now` is the
    clinic's local wall clock (naive datetime)."""
    if intent not in AUTO_INTENTS:
        return _no("not_automatable", "this kind of request always needs a staff member")
    if not settings.auto_enabled(conn):
        return _no("switch_off", "automatic appointments are switched off")
    if _sender_mode(conn, wa_id) == "human":
        return _no("human_mode", "staff have taken over this chat")

    patient = resolve_patient_by_phone(conn, wa_id)
    patient_id = patient.id if patient else None

    if intent == "book_appointment":
        branch_id = branches.resolve(conn, slots.get("branch_id"))
        decision = _slot_checks(conn, slots.get("appt_date"), slots.get("start_time"), now, branch_id=branch_id)
        if not decision.auto:
            return decision
        # Active = booked/confirmed and not yet started (the same notion the
        # dialog uses for its own per-person limit).
        now_key = (now.date().isoformat(), now.strftime("%H:%M"))
        active = sum(1 for a in sender_appointments(conn, wa_id, patient_id, now)
                     if (a["appt_date"], a["start_time"]) >= now_key)
        if active >= MAX_ACTIVE_PER_NUMBER:
            return _no("number_cap", "this number already has {} active bookings".format(MAX_ACTIVE_PER_NUMBER))
        if patient_id is None:
            name = (slots.get("patient_name") or "").strip()
            if not name:
                return _no("no_name", "no patient name was given for this unregistered number")
            if len(name) > MAX_NAME_LEN:
                return _no("no_name", "the patient name is too long to trust")
        cap = settings.auto_daily_cap(conn)
        if patient_activity.automated_bookings_on(conn, now.date().isoformat()) >= cap:
            return _no("daily_cap", "daily automation cap reached ({} automated bookings today)".format(cap))
        return _ok()

    row, problem = _own_appointment(conn, slots.get("appointment_id"), wa_id, patient_id)
    if problem is not None:
        return problem
    decision = _state_checks(row, now)
    if not decision.auto:
        return decision
    if intent == "cancel_appointment":
        return _ok()
    # A reschedule stays at the appointment's own branch unless the request names another.
    target_branch = slots.get("branch_id") if slots.get("branch_id") not in (None, "") else row["branch_id"]
    return _slot_checks(conn, slots.get("appt_date"), slots.get("start_time"), now,
                        exclude_appointment_id=row["id"], branch_id=branches.resolve(conn, target_branch))
