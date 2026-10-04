from collections import namedtuple
from datetime import date, datetime, timedelta

from clinic import core, entity_resolution
from clinic.nlu.answer import compose_answer, compose_navigation
from clinic.nlu.parser import QUEUE_WRITE_INTENTS, UnrecognizedCommand, parse
from clinic.queries import registered_names
from clinic.voice_context import APPOINTMENT_INTENTS, _display_name, apply_context, next_question


class PipelineError(Exception):
    pass


# A write intent yields a pending proposal (unchanged from before); a read
# intent now actually executes and returns its answer + citation, instead of
# telling the caller which query to go run separately. A deferred intent
# (see `defer_intents`) returns the raw parsed slots with no validation or
# database write at all -- the caller (the dashboard's structured review
# card) is responsible for a human reviewing/editing/approving before
# anything is written. `resolved` carries a best-effort entity match (never
# raises) so the UI can pre-select a patient/staff dropdown; it's a hint,
# not a decision -- the human's final dropdown choice at approve-time wins.
WriteResult = namedtuple("WriteResult", ["proposal_id", "intent", "description"])
# `scope_caption` is a short muted line the page shows above a table when what was
# listed is not obvious from the table itself (see the list_appointments branch).
ReadResult = namedtuple("ReadResult", ["intent", "data", "citation", "answer_text", "scope_caption"],
                        defaults=(None,))
ParsedResult = namedtuple("ParsedResult", ["intent", "slots", "resolved"])
# A navigation command (open_calendar): the dashboard switches tab / view and
# nothing is read or written. `mode` is "week" / "month" / "agenda" or None.
NavigateResult = namedtuple("NavigateResult", ["intent", "tab", "mode", "answer_text"])


def _local_now():
    return datetime.now()


def _short_day(day, weekday=True):
    """'Wed 7 Oct' (or '7 Oct') -- unambiguous and the same in English and Hindi."""
    label = "{} {}".format(day.day, day.strftime("%b"))
    return "{} {}".format(day.strftime("%a"), label) if weekday else label


def _resolve_patient_or_raise(clinical_adapter, conn, name):
    if not name:
        raise PipelineError("Could not hear a patient name.")
    candidates = clinical_adapter.resolve_patient(conn, name)
    if not candidates or candidates[0].score < 0.6:
        raise PipelineError("No confident patient match for '{}'.".format(name))
    return candidates[0]


def _resolve_staff_or_raise(ops_adapter, conn, name):
    if not name:
        raise PipelineError("Could not hear a staff name.")
    candidates = ops_adapter.resolve_staff(conn, name)
    if not candidates or candidates[0].score < 0.6:
        raise PipelineError("No confident staff match for '{}'.".format(name))
    return candidates[0]


def _best_effort_patient(clinical_adapter, conn, name):
    if not name:
        return None
    candidates = clinical_adapter.resolve_patient(conn, name)
    if candidates and candidates[0].score >= 0.6:
        return candidates[0]
    return None


def _best_effort_staff(ops_adapter, conn, name):
    if not name:
        return None
    candidates = ops_adapter.resolve_staff(conn, name)
    if candidates and candidates[0].score >= 0.6:
        return candidates[0]
    return None


def _resolve_queue_command(conn, intent, slots, clinical_adapter):
    """Deferred queue write (check in / call / done / no-show): work out which
    of TODAY's appointments the command means, deterministically, and hand
    the review card that guess plus every outstanding appointment as dropdown
    options. The guess comes from the token number if one was heard, else
    "call next" = the lowest-token checked-in patient, else a name match.
    Nothing here raises or writes; a wrong or missing guess is a blank/wrong
    dropdown for the human to fix before approving."""
    today = date.today().isoformat()
    options = clinical_adapter.queue_options(conn, today)
    outstanding_ids = {o["id"] for o in options}
    entries = clinical_adapter.queue_for_date(conn, today)

    appointment_id = None
    note = None
    token = slots.get("token")
    name = slots.get("patient_name")
    if token:
        entry = clinical_adapter.find_by_token(conn, today, token)
        if entry and entry["id"] in outstanding_ids:
            appointment_id = entry["id"]
        else:
            note = "Token {} is not waiting in today's queue.".format(token)
    elif name:
        patient = _best_effort_patient(clinical_adapter, conn, name)
        mine = [e for e in entries if e["id"] in outstanding_ids and patient and e["patient_id"] == patient.id]
        if not mine:
            # Walk-ins booked under a raw name have no patient row: fall back
            # to a close name match among today's queue.
            scored = sorted(
                ((entity_resolution.similarity(name, e["name"] or ""), e) for e in entries if e["id"] in outstanding_ids),
                key=lambda pair: pair[0], reverse=True,
            )
            mine = [scored[0][1]] if scored and scored[0][0] >= 0.75 else []
        if mine:
            appointment_id = mine[0]["id"]
        else:
            note = "No one named '{}' is waiting in today's queue.".format(name)
    elif intent == "queue_call_next":
        nxt = clinical_adapter.next_to_call(conn, today)
        if nxt:
            appointment_id = nxt["id"]
        else:
            note = "Nobody has checked in yet."

    resolved = {"appointments": options}
    if note:
        resolved["note"] = note
    return ParsedResult(intent, {"appointment_id": appointment_id}, resolved)


def transcript_to_response(conn, text, clinical_adapter, ops_adapter, language_code="hi-IN",
                           defer_intents=frozenset(), context=None):
    """Parse a transcript and either propose a write, answer a query, or (for
    intents named in `defer_intents`) hand back the raw parsed slots with no
    validation or database write at all. Raises PipelineError with a
    human-readable reason if it can't proceed -- the caller shows that
    instead of guessing at a write.

    `context` (a clinic.voice_context.VoiceContext, voice flow only) lets a
    follow-up lean on what was just discussed, and lets the assistant ask for
    a missing patient / day / time instead of opening a half-empty card."""
    try:
        intent, slots = parse(text, known_names=registered_names(conn), context=context)
    except UnrecognizedCommand:
        raise PipelineError("Could not classify this into a known command.")
    return respond_to_intent(conn, intent, slots, text, clinical_adapter, ops_adapter, language_code,
                             defer_intents, context=context)


def _appointment_options(clinical_adapter, conn, appointment_id, name=None):
    """The cancel / reschedule card's dropdown: upcoming appointments whose
    written or registered name sounds like the heard `name` (this covers
    walk-ins with no patient record and names in the other script, and keeps
    only the best-fitting person), plus the one picked from the on-screen list.
    Returns (options, suggested_id); the id is only suggested when it is
    unambiguous (one person's nearest appointment)."""
    options, people = [], set()
    for row in clinical_adapter.upcoming_appointments_named(conn, name) if name else []:
        options.append({"id": row["id"], "appt_date": row["appt_date"], "start_time": row["start_time"],
                        "duration_minutes": row["duration_minutes"],
                        "label": "{} {} - {}".format(row["appt_date"], row["start_time"], row["who"])})
        people.add(row["who"].strip().casefold())
    if appointment_id and not any(str(o["id"]) == str(appointment_id) for o in options):
        row = clinical_adapter.appointment_option(conn, appointment_id)
        if row:
            options.insert(0, dict(row))
    options.sort(key=lambda o: (o["appt_date"], o["start_time"]))
    suggested = appointment_id
    if not suggested and options and len(people) <= 1:
        suggested = options[0]["id"]
    return options, suggested


def respond_to_intent(conn, intent, slots, text, clinical_adapter, ops_adapter, language_code="hi-IN",
                      defer_intents=frozenset(), context=None, skipped=(), fresh=True):
    """Everything after the transcript has become an (intent, slots) pair.
    `fresh` is False when `text` is only the answer to a question the assistant
    asked, so it is not re-read as a new command."""
    if intent == "open_calendar":
        mode = slots.get("mode")
        return NavigateResult(intent, "appointments", mode, compose_navigation(intent, mode, language_code))

    if context is not None:
        try:
            slots = apply_context(intent, slots, text, context, fresh=fresh)
        except ValueError as exc:
            raise PipelineError(str(exc))
        question = next_question(conn, intent, slots, clinical_adapter, context, language_code, skipped)
        if question is not None:
            return question

    if intent in defer_intents and intent in QUEUE_WRITE_INTENTS:
        return _resolve_queue_command(conn, intent, slots, clinical_adapter)

    if intent in defer_intents:
        resolved = {}
        if intent in (
            "record_visit", "set_followup", "cancel_followup", "reschedule_followup",
            "book_appointment", "cancel_appointment", "reschedule_appointment",
        ):
            patient = _best_effort_patient(clinical_adapter, conn, slots.get("patient_name"))
            if intent in APPOINTMENT_INTENTS:
                options, suggested = _appointment_options(
                    clinical_adapter, conn, slots.get("appointment_id"), slots.get("patient_name"))
                if patient:
                    resolved = {"patient_id": patient.id, "patient_label": patient.label}
                resolved["appointments"] = options
                if suggested and not slots.get("appointment_id"):
                    slots = dict(slots, appointment_id=suggested)
                if not options:
                    resolved["note"] = "No upcoming appointment found for {}.".format(slots.get("patient_name") or "that name")
            elif patient:
                resolved = {"patient_id": patient.id, "patient_label": patient.label}
                # Populate the review card's "followup"/"appointment" dropdown
                # options for this specific patient (see static/review_card.js
                # -- context.followups / context.appointments), the same way
                # the WhatsApp inbox already does via item.pending_followups.
                if intent in ("cancel_followup", "reschedule_followup"):
                    resolved["followups"] = [
                        dict(f) for f in clinical_adapter.pending_followups_for_patient(conn, patient.id)
                    ]
        elif intent == "log_attendance":
            staff = _best_effort_staff(ops_adapter, conn, slots.get("staff_name"))
            if staff:
                resolved = {"staff_id": staff.id, "staff_label": staff.label}
        return ParsedResult(intent, slots, resolved)

    if intent == "register_patient":
        if not slots.get("name") or not slots.get("phone"):
            raise PipelineError("Missing name or phone in what was heard.")
        pid = core.propose(conn, intent, slots, source_text=text)
        return WriteResult(pid, intent, "register {name} ({phone}), age {age}".format(**slots))

    if intent == "register_staff":
        if not slots.get("name"):
            raise PipelineError("Could not hear a staff name.")
        pid = core.propose(conn, intent, slots, source_text=text)
        return WriteResult(pid, intent, "register staff {} ({})".format(slots.get("name"), slots.get("role") or "no role given"))

    if intent in ("record_visit", "set_followup"):
        name = slots.pop("patient_name", None)
        patient = _resolve_patient_or_raise(clinical_adapter, conn, name)
        if intent == "record_visit" and not slots.get("fee_rupees"):
            raise PipelineError("Could not hear a fee amount.")
        if intent == "set_followup" and not slots.get("days_from_now"):
            raise PipelineError("Could not hear a follow-up interval.")
        slots["patient_id"] = patient.id
        pid = core.propose(conn, intent, slots, source_text=text)
        if intent == "record_visit":
            description = "visit for {}: Rs {}".format(patient.label, slots["fee_rupees"])
        else:
            description = "follow-up for {} in {} days".format(patient.label, slots["days_from_now"])
        return WriteResult(pid, intent, description)

    if intent in ("cancel_followup", "reschedule_followup"):
        name = slots.pop("patient_name", None)
        patient = _resolve_patient_or_raise(clinical_adapter, conn, name)
        followup_id = clinical_adapter.nearest_pending_followup(conn, patient.id)
        if followup_id is None:
            raise PipelineError("No pending follow-up found for {}.".format(patient.label))
        slots["followup_id"] = followup_id
        if intent == "reschedule_followup" and not slots.get("new_due_date"):
            raise PipelineError("Could not hear a new due date.")
        pid = core.propose(conn, intent, slots, source_text=text)
        if intent == "cancel_followup":
            description = "cancel follow-up for {}".format(patient.label)
        else:
            description = "reschedule follow-up for {} to {}".format(patient.label, slots["new_due_date"])
        return WriteResult(pid, intent, description)

    if intent == "book_appointment":
        name = slots.pop("patient_name", None)
        patient = _best_effort_patient(clinical_adapter, conn, name)
        if patient:
            slots["patient_id"] = patient.id
            patient_label = patient.label
        else:
            patient_label = name or slots.get("patient_phone") or "unregistered caller"
        if not slots.get("appt_date") or not slots.get("start_time"):
            raise PipelineError("Could not hear a date and time for the appointment.")
        pid = core.propose(conn, intent, slots, source_text=text)
        description = "appointment for {} on {} at {}".format(patient_label, slots["appt_date"], slots["start_time"])
        return WriteResult(pid, intent, description)

    if intent in ("cancel_appointment", "reschedule_appointment"):
        name = slots.pop("patient_name", None)
        patient = _resolve_patient_or_raise(clinical_adapter, conn, name)
        appointment = clinical_adapter.next_appointment_for_patient(conn, patient.id)
        if appointment is None:
            raise PipelineError("No upcoming appointment found for {}.".format(patient.label))
        slots["appointment_id"] = appointment["id"]
        if intent == "reschedule_appointment" and (not slots.get("appt_date") or not slots.get("start_time")):
            raise PipelineError("Could not hear a new date and time for the appointment.")
        pid = core.propose(conn, intent, slots, source_text=text)
        if intent == "cancel_appointment":
            description = "cancel appointment for {}".format(patient.label)
        else:
            description = "reschedule appointment for {} to {} at {}".format(
                patient.label, slots["appt_date"], slots["start_time"]
            )
        return WriteResult(pid, intent, description)

    if intent == "patient_lookup":
        name = slots.pop("patient_name", None)
        patient = _resolve_patient_or_raise(clinical_adapter, conn, name)
        data = clinical_adapter.patient_lookup(conn, patient.id)
        if context is not None:
            context.remember_patient(patient.id, patient.label)
        citation = clinical_adapter.citation()
        return ReadResult(intent, data, citation, compose_answer(intent, data, citation, language_code))

    if intent == "log_attendance":
        name = slots.pop("staff_name", None)
        staff = _resolve_staff_or_raise(ops_adapter, conn, name)
        slots["staff_id"] = staff.id
        pid = core.propose(conn, intent, slots, source_text=text)
        return WriteResult(pid, intent, "attendance for {}: {}".format(staff.label, slots["status"]))

    if intent == "log_expense":
        if not slots.get("amount_rupees"):
            raise PipelineError("Could not hear an amount.")
        pid = core.propose(conn, intent, slots, source_text=text)
        return WriteResult(pid, intent, "expense: Rs {} -- {}".format(slots["amount_rupees"], slots["description"]))

    if intent == "queue_status":
        data = clinical_adapter.queue_snapshot(conn, date.today().isoformat())
        citation = clinical_adapter.citation()
        return ReadResult(intent, data, citation, compose_answer(intent, data, citation, language_code))

    if intent == "missed_followups":
        data = clinical_adapter.missed_followups(conn)
        citation = clinical_adapter.citation()
        return ReadResult(intent, data, citation, compose_answer(intent, data, citation, language_code))

    if intent == "day_end_cashbook":
        data = ops_adapter.day_end_cashbook(conn)
        citation = ops_adapter.citation()
        return ReadResult(intent, data, citation, compose_answer(intent, data, citation, language_code))

    if intent in ("check_availability", "list_appointments") and slots.get("date_unreadable"):
        # A day was clearly named but could not be read. Showing today's data
        # as if it answered the question would be a wrong answer that looks
        # right, so ask again instead.
        raise PipelineError(
            "I couldn't read the date. Please say it again, for example '7 October', 'kal' or 'Monday'."
        )

    if intent == "check_availability":
        appt_date = slots.get("appt_date") or date.today().isoformat()
        data = {"date": appt_date, "slots": clinical_adapter.available_slots(conn, appt_date)}
        if context is not None:
            context.remember_date(appt_date)
        citation = clinical_adapter.citation()
        return ReadResult(intent, data, citation, compose_answer(intent, data, citation, language_code))

    if intent == "list_appointments":
        today = date.today()
        name = (slots.get("patient_name") or "").strip()
        is_week = slots.get("range") == "week"
        named_day = slots.get("date")
        caption = None
        if is_week:
            start, end = today.isoformat(), (today + timedelta(days=6)).isoformat()
            scope = "this week ({} - {})".format(_short_day(today, weekday=False), _short_day(today + timedelta(days=6), weekday=False))
            when = "this week"
        elif named_day or not name:
            start = end = named_day or today.isoformat()
            day = date.fromisoformat(start)
            scope = _short_day(day) + (" (today)" if day == today else "")
            when = _short_day(day)
            if not named_day:
                caption = "Today \u00b7 {}".format(_short_day(day))
        else:
            # A person and no day: every date, past and upcoming.
            start = end = scope = None
            when = "all dates"
        if name:
            data = [dict(row) for row in clinical_adapter.appointments_named(conn, name, start, end)]
            caption = "Appointments for {} \u00b7 {}".format(name, when)
        else:
            data = [dict(row) for row in clinical_adapter.scheduled_appointments(conn, start, end)]
        if is_week:
            # A forward-looking list: today's appointments whose time has
            # already passed are not "coming up". (A one-day list keeps the
            # whole day, since "today's schedule" is also a record of the day.)
            now = _local_now()
            data = [r for r in data if not (r["appt_date"] == now.date().isoformat() and r["start_time"] < now.strftime("%H:%M"))]
        if context is not None:
            context.remember_list(data, "{}, {}".format(name, scope or when) if name else scope, None if is_week else start)
        citation = clinical_adapter.citation()
        # Nothing matching a heard name says so (compose_answer) -- never today's list instead.
        return ReadResult(intent, data, citation,
                          compose_answer(intent, data, citation, language_code, scope=scope, name=name or None),
                          caption)

    if intent == "next_appointment":
        name = slots.pop("patient_name", None)
        patient = _resolve_patient_or_raise(clinical_adapter, conn, name)
        row = clinical_adapter.next_appointment_for_patient(conn, patient.id)
        data = dict(row) if row else None
        if data:
            data["patient_label"] = patient.label
        if context is not None:
            context.remember_patient(patient.id, patient.label)
        citation = clinical_adapter.citation()
        return ReadResult(intent, data, citation, compose_answer(intent, data, citation, language_code))

    raise PipelineError("Unhandled intent: {}".format(intent))
