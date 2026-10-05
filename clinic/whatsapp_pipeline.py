import re
from datetime import datetime, timedelta

from clinic import scheduling
from clinic.entity_resolution import last10_digits
from clinic.nlu.datetime_extract import extract_appt_date, extract_appt_time
from clinic.nlu.llm_slots import extract_name
from clinic.nlu.patient_parser import UnrecognizedPatientMessage, parse_patient_message

# More than this many unresolved booking-request items from one WhatsApp
# number and further ones go to a human instead of piling up proposals.
MAX_OPEN_BOOKING_REQUESTS = 5
# How far ahead to look for a free slot when the patient didn't pick a date.
SUGGESTION_HORIZON_DAYS = 30

_FOLLOWUP_MENTION = re.compile(r"follow[\s-]*up|followup|फॉलो")


def _nearest_pending_followup(conn, patient_id):
    """V1 heuristic for 'which follow-up does this message refer to' when a
    patient has more than one pending: the nearest due date. Surfaced as an
    editable dropdown on the review card (not built yet) so a human can
    correct a wrong guess before approving -- a known limitation, not a
    solved problem."""
    row = conn.execute(
        "SELECT id FROM followups WHERE patient_id = ? AND status = 'pending' ORDER BY due_date LIMIT 1",
        (patient_id,),
    ).fetchone()
    return row["id"] if row else None


def sender_appointments(conn, wa_id, patient_id=None, now=None):
    """The sender's own outstanding appointments (booked/confirmed, today or
    later), nearest first. "Own" means the appointment is booked under their
    patient_id, or under a phone number whose last 10 digits match the
    sender's WhatsApp number -- nothing else, so a status answer can never
    leak another person's appointment."""
    now = now or datetime.now()
    target = last10_digits(wa_id or "")
    rows = conn.execute(
        """
        SELECT a.id, a.patient_id, a.appt_date, a.start_time, a.duration_minutes, a.branch_id,
               COALESCE(a.patient_phone, p.phone) AS phone
        FROM appointments a LEFT JOIN patients p ON p.id = a.patient_id
        WHERE a.status IN ('booked', 'confirmed') AND a.appt_date >= ?
        ORDER BY a.appt_date, a.start_time, a.id
        """,
        (now.date().isoformat(),),
    ).fetchall()
    mine = []
    for row in rows:
        by_patient = patient_id is not None and row["patient_id"] == patient_id
        by_phone = len(target) == 10 and last10_digits(row["phone"] or "") == target
        if by_patient or by_phone:
            mine.append({k: row[k] for k in ("id", "appt_date", "start_time", "duration_minutes", "branch_id")})
    return mine


def last_branch_id(conn, wa_id, patient_id=None, today=None):
    """The branch of the sender's most recent PAST appointment (anything but
    cancelled; an upcoming booking is not a visit), or None: the branch to
    suggest first next time. Matched
    the same way as sender_appointments (patient id or phone), so it never
    reveals anyone else's."""
    target = last10_digits(wa_id or "")
    rows = conn.execute(
        """
        SELECT a.patient_id, a.branch_id, COALESCE(a.patient_phone, p.phone) AS phone
        FROM appointments a LEFT JOIN patients p ON p.id = a.patient_id
        WHERE a.status != 'cancelled' AND a.branch_id IS NOT NULL AND a.appt_date <= ?
        ORDER BY a.appt_date DESC, a.start_time DESC, a.id DESC
        """,
        ((today or datetime.now().date().isoformat()),),
    ).fetchall()
    for row in rows:
        by_patient = patient_id is not None and row["patient_id"] == patient_id
        by_phone = len(target) == 10 and last10_digits(row["phone"] or "") == target
        if by_patient or by_phone:
            return row["branch_id"]
    return None


def _free_slots(conn, day, today, hhmm):
    slots = scheduling.generate_slots(conn, day.isoformat())
    if day == today:
        slots = [s for s in slots if s > hhmm]  # nothing already in the past
    return slots


def suggest_slot(conn, stated_date, stated_time, now):
    """Pick the date/time to PRE-FILL on a WhatsApp booking request.
    Returns (appt_date, start_time, suggested_fields, note). Fields the
    patient actually stated and that are free are kept as stated; anything
    else is a suggestion, named in `suggested_fields` and explained in
    `note` so staff never mistake it for the patient's own request. Purely
    deterministic -- no model involved. The confirm-time double-booking
    guard in clinic/intents.py still applies whatever staff approve."""
    today = now.date()
    hhmm = now.strftime("%H:%M")
    if stated_date is not None and stated_date < today.isoformat():
        stated_date = None  # a date in the past is not a usable request

    def first_free_from(start_day):
        for offset in range(SUGGESTION_HORIZON_DAYS + 1):
            day = start_day + timedelta(days=offset)
            slots = _free_slots(conn, day, today, hhmm)
            if slots:
                return day.isoformat(), slots[0]
        return None, None

    if stated_date and stated_time:
        day = datetime.strptime(stated_date, "%Y-%m-%d").date()
        if stated_time in _free_slots(conn, day, today, hhmm):
            return stated_date, stated_time, [], None
        slots = _free_slots(conn, day, today, hhmm)
        if slots:
            return stated_date, slots[0], ["start_time"], (
                "Suggested -- the requested time {} on {} isn't available. Pre-filled the earliest free slot that day instead."
                .format(stated_time, stated_date))
        found_date, found_time = first_free_from(day + timedelta(days=1))
        if found_date:
            return found_date, found_time, ["appt_date", "start_time"], (
                "Suggested -- {} has no free slot. Pre-filled the next free slot instead.".format(stated_date))
        return stated_date, stated_time, [], "No free slot found in the next {} days -- choose manually.".format(SUGGESTION_HORIZON_DAYS)

    if stated_date:
        day = datetime.strptime(stated_date, "%Y-%m-%d").date()
        slots = _free_slots(conn, day, today, hhmm)
        if slots:
            return stated_date, slots[0], ["start_time"], (
                "Suggested -- patient didn't specify a time. Pre-filled the earliest free slot on that date.")
        found_date, found_time = first_free_from(day + timedelta(days=1))
        if found_date:
            return found_date, found_time, ["appt_date", "start_time"], (
                "Suggested -- {} has no free slot. Pre-filled the next free slot instead.".format(stated_date))
        return stated_date, None, [], "No free slot found in the next {} days -- choose manually.".format(SUGGESTION_HORIZON_DAYS)

    if stated_time:
        for offset in range(SUGGESTION_HORIZON_DAYS + 1):
            day = today + timedelta(days=offset)
            if stated_time in _free_slots(conn, day, today, hhmm):
                return day.isoformat(), stated_time, ["appt_date"], (
                    "Suggested -- patient didn't specify a date. Pre-filled the next date with {} free.".format(stated_time))

    found_date, found_time = first_free_from(today)
    if found_date:
        return found_date, found_time, ["appt_date", "start_time"], (
            "Suggested -- patient didn't specify a date or time. Pre-filled the next free slot.")
    return None, None, [], "No free slot found in the next {} days -- choose manually.".format(SUGGESTION_HORIZON_DAYS)


def _booking_request(conn, wa_id, patient, text, parsed_slots, now):
    patient_id = patient.id if patient else None
    open_requests = conn.execute(
        "SELECT COUNT(*) AS n FROM wa_messages WHERE wa_id = ? AND intent = 'book_appointment' AND status = 'classified'",
        (wa_id,),
    ).fetchone()["n"]
    if open_requests >= MAX_OPEN_BOOKING_REQUESTS:
        # Too many unresolved requests from one number: stop creating
        # proposals; a human looks at it instead.
        return {"patient_id": patient_id, "intent": None, "slots": {}}

    appt_date, start_time, suggested, note = suggest_slot(
        conn, parsed_slots.get("appt_date"), parsed_slots.get("start_time"), now
    )
    slots = {
        "patient_id": patient_id,
        "patient_name": None,
        "patient_phone": None,
        "appt_date": appt_date,
        "start_time": start_time,
        "duration_minutes": None,
        "notes": None,
    }
    if patient is None:
        # Not a registered patient: use the existing unregistered-caller
        # fields. The name is only a hint for staff to check.
        try:
            slots["patient_name"] = extract_name(text)
        except Exception:
            slots["patient_name"] = None
        slots["patient_phone"] = last10_digits(wa_id)
    if suggested:
        slots["suggested"] = suggested
    if note:
        slots["suggestion_note"] = note
    return {"patient_id": patient_id, "intent": "book_appointment", "slots": slots}


def classify_text_message(conn, wa_id, text, clinical_adapter, now=None):
    """Classify an already-known text (for audio messages, the caller
    transcribes first and passes the transcript here too). Returns a dict
    matching the columns classify_and_update_wa_message will write:
    {patient_id, intent, slots} -- intent is None (and slots {}) when the
    message doesn't map to a known, actionable request; the caller is
    responsible for treating that as 'needs_human_reply', never as license
    to guess at an action.

    `now` is the clinic's local wall clock (injectable for tests).

    Cancel / reschedule: if the sender has an upcoming appointment, the
    proposal is for that appointment (the nearest one) -- cancel_appointment
    / reschedule_appointment. Only when they have none does the older
    follow-up behaviour apply. If they have BOTH, the appointment is
    preferred (a wrong guess is a dropdown a human corrects), except when the
    message explicitly says "follow-up" and they have a pending one.

    my_status is the one intent that is answered without approval: it is
    read-only and the caller replies only to the sender's own number.
    """
    now = now or datetime.now()
    patient = clinical_adapter.resolve_patient_by_phone(conn, wa_id)
    patient_id = patient.id if patient else None

    try:
        intent, slots = parse_patient_message(text, today=now.date())
    except UnrecognizedPatientMessage:
        return {"patient_id": patient_id, "intent": None, "slots": {}}

    if intent == "register_patient":
        slots["phone"] = last10_digits(wa_id)
        return {"patient_id": patient_id, "intent": intent, "slots": slots}

    if intent == "book_appointment":
        return _booking_request(conn, wa_id, patient, text, slots, now)

    if intent == "my_status":
        appointments = sender_appointments(conn, wa_id, patient_id, now)
        if not appointments:
            return {"patient_id": patient_id, "intent": None, "slots": {}}
        return {"patient_id": patient_id, "intent": "my_status", "slots": {"appointment_id": appointments[0]["id"]}}

    if intent in ("cancel_followup", "reschedule_followup"):
        appointments = sender_appointments(conn, wa_id, patient_id, now)
        followup_id = _nearest_pending_followup(conn, patient_id) if patient_id else None
        explicit_followup = bool(_FOLLOWUP_MENTION.search(text.lower())) and followup_id is not None
        if appointments and not explicit_followup:
            nearest = appointments[0]["id"]
            if intent == "cancel_followup":
                return {"patient_id": patient_id, "intent": "cancel_appointment", "slots": {"appointment_id": nearest}}
            return {"patient_id": patient_id, "intent": "reschedule_appointment", "slots": {
                "appointment_id": nearest,
                "appt_date": extract_appt_date(text, today=now.date()),
                "start_time": extract_appt_time(text),
            }}
        if followup_id is None:
            return {"patient_id": patient_id, "intent": None, "slots": {}}
        slots["followup_id"] = followup_id
        return {"patient_id": patient_id, "intent": intent, "slots": slots}

    if intent == "confirm_followup":
        if patient_id is None:
            # A follow-up action from a number we don't recognize as an
            # existing patient -- nothing to act on, and not a reason to guess.
            return {"patient_id": None, "intent": None, "slots": {}}
        followup_id = _nearest_pending_followup(conn, patient_id)
        if followup_id is None:
            return {"patient_id": patient_id, "intent": None, "slots": {}}
        slots["followup_id"] = followup_id
        return {"patient_id": patient_id, "intent": intent, "slots": slots}

    return {"patient_id": patient_id, "intent": None, "slots": {}}
