import re
from collections import namedtuple
from datetime import date, datetime, timedelta

from clinic import (booking_phone, branches, closures, core, entity_resolution, next_available, query_tool, scheduling, unanswered,
                    voice_branch, voice_closure)
from clinic.nlu import extract as nlu_extract
from clinic.nlu import planner as planner_module
from clinic.nlu.answer import compose_answer, compose_navigation
from clinic.nlu.parser import QUEUE_WRITE_INTENTS, UnrecognizedCommand, parse
from clinic.queries import registered_names
from clinic.voice_context import APPOINTMENT_INTENTS, AskResult, Note, _display_name, apply_context, next_question


NOT_CLASSIFIED = "Could not classify this into a known command."


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
# `options`: answer buttons the page shows under the answer ([{"label": ...}]) when the read ends with a question
# (the booking offer after "next available ... and book it for <name>"); tapping one is the same as saying it.
ReadResult = namedtuple("ReadResult", ["intent", "data", "citation", "answer_text", "scope_caption", "options"],
                        defaults=(None, None))
ParsedResult = namedtuple("ParsedResult", ["intent", "slots", "resolved"])
# A navigation command (open_calendar): the dashboard switches tab / view and
# nothing is read or written. `mode` is "week" / "month" / "agenda" or None.
NavigateResult = namedtuple("NavigateResult", ["intent", "tab", "mode", "answer_text"])
# "Switch to Branch C": the page changes this computer's My branch (and the
# branch it is viewing). Nothing is read from or written to the clinic's data.
SwitchBranchResult = namedtuple("SwitchBranchResult", ["intent", "branch_id", "branch_name", "answer_text"])
# "Close Branch A tomorrow": the batch review card (clinic/closures.py). It is only a plan --
# nothing is closed, moved or sent until a person presses Apply on the card.
ClosurePlanResult = namedtuple("ClosurePlanResult", ["intent", "plan", "reason", "answer_text"])


def _local_now():
    return datetime.now()


def _short_day(day, weekday=True):
    """'Wed 7 Oct' (or '7 Oct') -- unambiguous and the same in English and Hindi."""
    label = "{} {}".format(day.day, day.strftime("%b"))
    return "{} {}".format(day.strftime("%a"), label) if weekday else label


def _patients_for(clinical_adapter, conn, name, slots):
    """The registered patients exactly meant: a patient already chosen (slots["patient_id"]), else the phone
    number in the command (exact match first), else the name. Each has score 1.0."""
    slots = slots or {}
    return clinical_adapter.resolve_patient(conn, name or "", 4, phone=slots.get("patient_phone"),
                                            patient_id=slots.get("patient_id"))


def _resolve_patient_or_raise(clinical_adapter, conn, name, slots=None):
    if not name and not (slots or {}).get("patient_phone") and not (slots or {}).get("patient_id"):
        raise PipelineError("Could not hear a patient name.")
    candidates = _patients_for(clinical_adapter, conn, name, slots)
    if not candidates:
        raise PipelineError("No patient found exactly matching '{}'.".format(name or (slots or {}).get("patient_phone")))
    if len(candidates) > 1:
        raise PipelineError("More than one patient matches '{}': {}. Say the phone number to pick one.".format(
            name, "; ".join(c.label for c in candidates)))
    return candidates[0]


def _resolve_staff_or_raise(ops_adapter, conn, name):
    if not name:
        raise PipelineError("Could not hear a staff name.")
    candidates = ops_adapter.resolve_staff(conn, name)
    if not candidates:
        raise PipelineError("No staff member found exactly matching '{}'.".format(name))
    if len(candidates) > 1:
        raise PipelineError("More than one staff member matches '{}': {}. Say the full name.".format(
            name, "; ".join(c.label for c in candidates)))
    return candidates[0]


def _best_effort_patient(clinical_adapter, conn, name, slots=None):
    """The one patient exactly meant, or None (nobody, or several: the caller never picks one of several)."""
    if not name and not (slots or {}).get("patient_phone") and not (slots or {}).get("patient_id"):
        return None
    candidates = _patients_for(clinical_adapter, conn, name, slots)
    return candidates[0] if len(candidates) == 1 else None


def _best_effort_staff(ops_adapter, conn, name):
    if not name:
        return None
    candidates = ops_adapter.resolve_staff(conn, name)
    return candidates[0] if len(candidates) == 1 else None


def _resolve_queue_command(conn, intent, slots, clinical_adapter):
    """Deferred queue write (check in / call / done / no-show): work out which
    of TODAY's appointments the command means, deterministically, and hand
    the review card that guess plus every outstanding appointment as dropdown
    options. The guess comes from the token number if one was heard, else
    "call next" = the lowest-token checked-in patient, else a name match.
    Nothing here raises or writes; a wrong or missing guess is a blank/wrong
    dropdown for the human to fix before approving."""
    today = date.today().isoformat()
    branch_id = slots.get("branch_id")        # the branch whose queue is meant (None: the only / default one)
    options = clinical_adapter.queue_options(conn, today, branch_id)
    outstanding_ids = {o["id"] for o in options}
    entries = clinical_adapter.queue_for_date(conn, today, branch_id)

    appointment_id = None
    note = None
    token = slots.get("token")
    name = slots.get("patient_name")
    if token:
        entry = clinical_adapter.find_by_token(conn, today, token, branch_id)
        if entry and entry["id"] in outstanding_ids:
            appointment_id = entry["id"]
        else:
            note = "Token {} is not waiting in today's queue.".format(token)
    elif name or slots.get("patient_id"):
        waiting = [e for e in entries if e["id"] in outstanding_ids]
        if slots.get("patient_id") is not None:
            # The person was already chosen: their id decides, the name is only for display.
            mine = [e for e in waiting if str(e["patient_id"]) == str(slots["patient_id"])]
        else:
            ids = {c.id for c in _patients_for(clinical_adapter, conn, name, slots)}
            # Walk-ins booked under a raw name have no patient row: their written name is compared exactly.
            mine = [e for e in waiting if e["patient_id"] in ids
                    or (e["patient_id"] is None and name and entity_resolution.name_match(name, e["name"] or "") == 1.0)]
        people = {("patient", e["patient_id"]) if e["patient_id"] is not None
                  else ("walk-in", (e["name"] or "").strip().casefold(), entity_resolution.last10_digits(e.get("phone") or ""))
                  for e in mine}
        if len(people) > 1:
            # Never pick one of several people: the dropdown of everyone waiting is for a human to choose from.
            note = "More than one person named '{}' is waiting in today's queue. Choose the right one from the list.".format(
                name or "that name")
        elif mine:
            appointment_id = mine[0]["id"]
        else:
            note = "No one named '{}' is waiting in today's queue.".format(name or "that name")
    elif intent == "queue_call_next":
        nxt = clinical_adapter.next_to_call(conn, today, branch_id)
        if nxt:
            appointment_id = nxt["id"]
        else:
            note = "Nobody has checked in yet."

    resolved = {"appointments": options}
    if note:
        resolved["note"] = note
    return ParsedResult(intent, {"appointment_id": appointment_id}, resolved)


def transcript_to_response(conn, text, clinical_adapter, ops_adapter, language_code="hi-IN",
                           defer_intents=frozenset(), context=None, source="voice"):
    """Parse a transcript and either propose a write, answer a query, or (for
    intents named in `defer_intents`) hand back the raw parsed slots with no
    validation or database write at all. Raises PipelineError with a
    human-readable reason if it can't proceed -- the caller shows that
    instead of guessing at a write.

    `context` (a clinic.voice_context.VoiceContext, voice flow only) lets a
    follow-up lean on what was just discussed, and lets the assistant ask for
    a missing patient / day / time instead of opening a half-empty card.

    With the tool-calling planner on (clinic/nlu/planner.py), the command goes to
    it wherever the one-word label picker used to be asked; the precise rules
    (a branch switch, closing a branch, a move, a patient count, a follow-up on
    what is on screen) still come first and the planner is only consulted for a
    closing command the rules read too little of. `source` is recorded in the
    planner log."""
    mention = voice_branch.find(conn, text)
    if mention.branch is None and voice_branch.is_switch_command(text):
        mention = voice_branch.find(conn, text, tail=True)        # "change my branch to b"
    run = planner_module.start_run(conn, text, context, source)
    final_intent = "unclear"
    final_slots = None
    try:
        if voice_branch.is_switch_command(text):
            intent, slots = "set_my_branch", {"branch_id": mention.branch["id"] if mention.branch else None}
            if run is not None:
                run.note_rule("branch")
        elif voice_closure.is_close_command(conn, text, mention):
            intent, slots = "close_branch", voice_closure.parse(conn, text, mention.text, mention, today=date.today())
            if run is not None:
                run.note_rule("closure")
            if run is not None and voice_closure.needs_reading(conn, text, slots):
                slots = _read_closing_command(conn, text, slots, run)
        else:
            try:
                intent, slots = parse(mention.text, known_names=registered_names(conn), context=context, planner=run)
            except UnrecognizedCommand:
                saved = _unanswered_read(conn, text, run, language_code, source)
                if saved is not None:
                    return saved          # a question the app cannot answer yet: saved for a person, the user is told
                raise PipelineError(NOT_CLASSIFIED)
            if run is not None and run.rejected_query and (intent in defer_intents or intent in QUEUE_WRITE_INTENTS):
                # The planner understood a READ it cannot run; the keyword rules then guessed a write card.
                # A card nobody asked for is worse than "not yet": save the question instead.
                saved = _unanswered_read(conn, text, run, language_code, source)
                if saved is not None:
                    return saved
            if intent in ("close_branch", "set_my_branch", "clarify"):
                pass          # the planner named the branch itself; a branch in the words is not a second answer
            elif mention.branch:
                slots["branch_id"] = mention.branch["id"]
            elif mention.every:
                slots["all_branches"] = True
        final_intent, final_slots = intent, slots
    finally:
        if run is not None:
            run.finish(final_intent, final_slots)
            if context is not None:
                context.turn_call = run.call_summary()
                context.planner_log_id = run.log_id
    if context is not None and intent not in ("set_my_branch", "close_branch", "clarify"):
        if mention.branch:
            context.remember_branch(mention.branch["id"])     # later commands stay at it
        elif mention.mine:
            context.remember_branch(None)
    return respond_to_intent(conn, intent, slots, text, clinical_adapter, ops_adapter, language_code,
                             defer_intents, context=context)


def _unanswered_hint(text, run):
    """{'wanted': ..., 'spec': ...} when a command the parser could not place was plainly a request
    for information the app cannot read yet, else None. Three ways in: (a) the planner called
    `unsupported` and said what the user wanted; (b) it called `query` with something outside the
    whitelist; (c) the command ended as "please rephrase" and reads like a question. Small talk and
    commands that change something are never counted."""
    if run is not None and run.tool == "unsupported" and run.wanted and not unanswered.opens_with_write_verb(text):
        return {"wanted": run.wanted}
    if run is not None and run.rejected_query:
        return {"spec": run.rejected_query["spec"]}
    if (run is None or run.route == "rephrase") and unanswered.looks_like_information_request(text):
        return {}
    return None


def _unanswered_read(conn, text, run, language_code, source):
    """Save an unanswerable read (clinic/unanswered.py) and return the fixed reply, or None when
    the command is not one."""
    hint = _unanswered_hint(text, run)
    if hint is None:
        return None
    message = unanswered.reply(conn, text, "voice" if source == "voice" else "typed", language_code,
                               wanted=hint.get("wanted"), rejected_spec=hint.get("spec"))
    return Note(message)


def _read_closing_command(conn, text, slots, run):
    """A closing command the plain phrase rules read too little of (no first day, a
    length such as "for the next one week", a second branch named as the place
    to move everyone). The planner reads it; if it cannot, the deterministic
    reading rules (clinic/voice_closure.py) do what they can. Either way the
    result is only a plan on a review card."""
    planned = run.ask()
    if planned is not None and planned.intent == "close_branch":
        merged = dict(planned.slots)
        if slots.get("reason") and not merged.get("reason"):
            merged["reason"] = slots["reason"]
        return merged
    run.note_route("rules")
    return voice_closure.apply_reading_rules(conn, text, slots, today=date.today())


def _generic_query(conn, slots, clinical_adapter, context, language_code):
    """The planner's generic read (clinic/query_tool.py): a whitelisted entity,
    filters, columns, sort and sums, never model-written SQL. Branch-aware for the
    entities that belong to a branch (a named branch limits them; appointments also
    default to My branch)."""
    spec = {k: v for k, v in slots.items() if k not in ("branch_id", "all_branches")}
    scoped = spec.get("entity") in query_tool.BRANCH_ENTITIES
    branch_id, every, branch_name = _branch_scope(conn, slots) if scoped else (None, False, None)
    now = _local_now()
    try:
        result = query_tool.run(conn, spec, branch_id=branch_id, today=now.date().isoformat(), now=now.strftime("%H:%M"))
    except query_tool.QueryError:
        raise PipelineError("I could not run that lookup. Please say it another way.")
    spec = query_tool.validate_spec(spec)
    citation = clinical_adapter.citation()
    sentence = query_tool.describe(spec, result, None if spec["entity"] == "branches" else branch_name, today=now.date().isoformat())
    caption = branch_name if spec["entity"] != "branches" else None
    if result.truncated and (spec["aggregate"] == "list" or spec.get("group_by")):
        caption = "{}Showing the first {} of {}".format("{} \u00b7 ".format(caption) if caption else "",
                                                       len(result.rows), result.total) if not spec.get("group_by") \
            else "{}Showing the first {} groups".format("{} \u00b7 ".format(caption) if caption else "", len(result.rows))
    if context is not None and spec.get("date") and not spec.get("date_to"):
        context.remember_date(spec["date"])
    scalar = not spec.get("group_by") and spec["aggregate"] != "list"
    data = None if scalar else result.rows
    return ReadResult("query", data, citation, "{} Source: {}, {}.".format(sentence, citation.source, citation.as_of), caption)


def _person_key(row):
    """Who an appointment row is for, by identity and not by the name written on it: the registered patient's
    id, else (a walk-in) the name with the phone written on the appointment."""
    if row.get("patient_id") is not None:
        return ("patient", row["patient_id"])
    return ("walk-in", (row.get("who") or "").strip().casefold(), entity_resolution.last10_digits(row.get("patient_phone") or ""))


def _phone_tail(phone):
    digits = entity_resolution.last10_digits(phone or "")
    return "\u2026{}".format(digits[-4:]) if digits else ""


def _appointment_details(row):
    """One appointment as a review-card option: id, date, time and length (the keys other callers rely on)
    plus what the cancel / reschedule card shows as text, the same as the calendar's hover: who, end time, phone,
    status, branch and doctor names. The notes and diagnosis are never carried."""
    row = dict(row)
    start, length = row["start_time"], row.get("duration_minutes") or scheduling.SLOT_MINUTES
    return {
        "id": row["id"], "appt_date": row["appt_date"], "start_time": start,
        "duration_minutes": row.get("duration_minutes"),
        "end_time": scheduling._from_minutes(scheduling._to_minutes(start) + length),
        "patient_name": row.get("who") or row.get("patient_name"),
        "patient_phone": row.get("patient_phone"), "status": row.get("status"),
        "branch": row.get("branch"), "doctor": row.get("doctor"), "branch_id": row.get("branch_id"),
    }


def _add_tokens(clinical_adapter, conn, options):
    """Give each option its queue token (A-T04), as the calendar's hover shows it. Best effort: a token that
    cannot be worked out is simply not shown."""
    tokens = {}
    for key in {(o["appt_date"], o.get("branch_id")) for o in options}:
        try:
            for entry in clinical_adapter.queue_for_date(conn, key[0], key[1]):
                tokens[entry["id"]] = entry["token_label"]
        except Exception:
            continue
    for o in options:
        if tokens.get(o["id"]):
            o["token"] = tokens[o["id"]]


def _appointment_options(clinical_adapter, conn, appointment_id, name=None, patient_id=None):
    """The cancel / reschedule card's dropdown, plus the one picked from the on-screen list.
    Returns (options, suggested_id).

    Once the person is settled (`patient_id`: chosen, matched by their phone number, or the owner of the
    appointment picked from the list) only THAT patient's upcoming appointments are listed, found by id,
    never by name. Without an id the heard `name` is searched among the written / registered names (this
    covers walk-ins with no patient record and names in the other script). When that search finds more
    than one person (told apart by patient id or phone, not by the name text), every label carries the
    last four digits of the phone and nothing is suggested. An appointment is only suggested when it is
    unambiguous (one person's nearest appointment)."""
    rows = []
    if patient_id is not None:
        rows = list(clinical_adapter.upcoming_appointments_by_patient_id(conn, patient_id))
    elif name:
        rows = list(clinical_adapter.upcoming_appointments_named(conn, name))
    people = {_person_key(r) for r in rows}
    several = len(people) > 1
    options = []
    for row in rows:
        label = "{} {} - {}".format(row["appt_date"], row["start_time"], row["who"])
        tail = _phone_tail(row.get("patient_phone")) if several else ""
        if tail:
            label += " ({})".format(tail)
        options.append(dict(_appointment_details(row), label=label))
    if appointment_id and not any(str(o["id"]) == str(appointment_id) for o in options):
        row = clinical_adapter.appointment_option(conn, appointment_id)
        if row:
            options.insert(0, _appointment_details(row))
    options.sort(key=lambda o: (o["appt_date"], o["start_time"]))
    _add_tokens(clinical_adapter, conn, options)
    suggested = appointment_id
    if not suggested and options and not several:
        suggested = options[0]["id"]
    return options, suggested


def _branch_scope(conn, slots):
    """(branch_id, all_branches, branch_name) for a read. With one branch: (None,
    False, None) so nothing mentions branches; a branch id filters to it; "all
    branches" lists every one."""
    if not branches.multi_branch(conn):
        return None, False, None
    if slots.get("all_branches"):
        return None, True, None
    branch_id = slots.get("branch_id")
    return branch_id, False, (branches.branch_label(conn, branch_id) if branch_id else None)


def _switch_branch(conn, slots, context, language_code):
    """"Switch to Branch C": tell the page which branch this computer works for."""
    items = branches.list_branches(conn)
    if len(items) < 2:
        from clinic.voice_context import Note
        return Note("There is only one branch, so there is nothing to switch.")
    branch_id = slots.get("branch_id")
    if not branch_id:
        from clinic.voice_context import AskResult, question_text
        options = [{"label": b["name"], "branch_id": b["id"]} for b in items]
        return AskResult("set_my_branch", slots, "branch", question_text("branch", language_code), options)
    name = branches.branch_label(conn, branch_id)
    if context is not None:
        context.my_branch = int(branch_id)
        context.branch = None
    if language_code == "hi-IN":
        text = "Theek hai, ab aap {} par kaam kar rahe hain.".format(name)
    else:
        text = "Done. This computer now works for {}.".format(name)
    return SwitchBranchResult("set_my_branch", int(branch_id), name, text)


def _close_branch(conn, slots, context, language_code):
    """"Close Branch A tomorrow": ask for whatever is missing, then show the batch plan."""
    from clinic.voice_context import AskResult, Note, question_text
    if not branches.multi_branch(conn):
        return Note("Closing a branch needs more than one branch. Use a booking block to stop bookings.")
    if slots.get("date_unreadable"):
        raise PipelineError("I couldn't read the day. Please say it again, for example 'tomorrow', 'Monday' or '9 October'.")
    start = slots.get("appt_date")
    if not start:
        return AskResult("close_branch", slots, "date", question_text("date", language_code), [])
    branch_id = slots.get("branch_id")
    if not branch_id and slots.get("doctor_id"):
        branch_id = voice_closure.doctor_branch(conn, slots["doctor_id"], start)
    if not branch_id and context is not None and not slots.get("doctor_id"):
        branch_id = context.current_branch()
    if not branch_id:
        options = [{"label": b["name"], "branch_id": b["id"]} for b in branches.list_branches(conn)]
        return AskResult("close_branch", slots, "branch", question_text("branch", language_code), options)
    try:
        plan = closures.plan(conn, branch_id, start, slots.get("end_date") or start, doctor_id=slots.get("doctor_id"),
                             now=_local_now(), preferred_branch_id=slots.get("destination_branch_id"))
    except closures.ClosureError as exc:
        raise PipelineError(str(exc))
    if context is not None:
        context.remember_date(start)
    counts = plan["counts"]
    preferred = plan["scope"].get("preferred_branch")
    if language_code == "hi-IN":
        text = "{} mein {} mareez booked hain. Neeche dekhkar Apply dabayein; tab tak kuch nahi badlega.".format(
            plan["scope"]["branch"], counts["total"])
        if preferred:
            text += " Pehle {} mein jagah dekhi gayi hai.".format(preferred)
    else:
        text = "{} patient{} booked at {} in that window. Review the batch below; nothing changes until you press Apply.".format(
            counts["total"], "" if counts["total"] == 1 else "s", plan["scope"]["branch"])
        if preferred:
            text += " {} was tried first for everyone{}.".format(
                preferred, "; " + plan["scope"]["preferred_note"].lower() if plan["scope"].get("preferred_note") else "")
    return ClosurePlanResult("close_branch", plan, slots.get("reason") or "", text)


def _slot_notes(conn, intent, slots):
    """What staff should know about the time on a booking / move card before
    approving: the doctor on duty, or why the time cannot be had at that
    branch (no doctor then, already taken) and what is free instead. Advisory
    only: the check at approval time is the real one."""
    notes = []
    appointment_id = slots.get("appointment_id")
    branch_id = slots.get("branch_id")
    if intent == "reschedule_appointment":
        row = conn.execute("SELECT branch_id FROM appointments WHERE id = ?", (appointment_id,)).fetchone() \
            if appointment_id else None
        current = branches.resolve(conn, row["branch_id"]) if row else None
        if branch_id in (None, ""):
            branch_id = current
        elif current is not None and int(branch_id) != current:
            notes.append("Moving from {} to {}.".format(branches.branch_label(conn, current),
                                                        branches.branch_label(conn, branch_id)))
    branch_id = branches.resolve(conn, branch_id)
    name = branches.branch_label(conn, branch_id)
    appt_date, start = slots.get("appt_date"), slots.get("start_time")
    if not (appt_date and start):
        return notes
    try:
        doctor_id = scheduling.within_doctor_hours(conn, appt_date, start, scheduling.SLOT_MINUTES, branch_id)
        problem = None
        if doctor_id is None:
            problem = "{} has no doctor on duty at {} on {} (that day: {}).".format(
                name, start, appt_date, branches.hours_summary(conn, branch_id, appt_date))
        elif not scheduling.is_slot_free(conn, appt_date, start, scheduling.SLOT_MINUTES,
                                         exclude_appointment_id=appointment_id, branch_id=branch_id):
            problem = "{} is already booked or blocked at {}.".format(start, name)
        if problem:
            free = scheduling.generate_slots(conn, appt_date, branch_id=branch_id)
            notes.append(problem)
            notes.append("Free at {} that day: {}.".format(name, ", ".join(free[:6]) if free else "nothing"))
        else:
            doctor = branches.doctor_label(conn, doctor_id or None)
            notes.append("{}{}.".format(name, " \u00b7 {}".format(doctor) if doctor else ""))
    except ValueError:
        pass          # an unreadable date: the card's own field check says so
    return notes


# -- "next available" and the booking offer that can follow it ------------------------------------------------

_UNKNOWN_DOCTOR = {
    "en": "I couldn't find a doctor called {}. {}",
    "hinglish": "{} naam ke doctor nahi mile. {}",
    "hi": "{} नाम के डॉक्टर नहीं मिले। {}",
}


def _insert_before_source(answer, suffix):
    """The answer with `suffix` (a question) ahead of its "Source: ..." tail."""
    head, sep, source = (answer or "").partition(" Source:")
    return "{} {}{}{}".format(head, suffix, sep, source)


def _ask_doctor(conn, slots, status, found, spoken, text, language_code):
    """Which doctor? when the spoken doctor matches nobody or several: the candidates (or every doctor) are the
    options; the answer carries on with the same read. Never a guess."""
    from clinic.voice_context import question_text
    doctors = found if status == "several" else branches.list_doctors(conn)
    if not doctors:
        raise PipelineError("No doctors are set up yet.")
    options = [{"label": d["name"], "doctor_name": d["name"]} for d in doctors[:8]]
    lang = next_available.language_key(text, language_code)
    question = question_text("doctor", language_code, heard=text)
    if status == "none":
        question = _UNKNOWN_DOCTOR[lang].format(re.sub(r"(?i)^\s*(?:dr|doctor|डॉक्टर|डॉ)\.?\s+", "", spoken).strip() or spoken, question)
    keep = {k: v for k, v in slots.items() if k != "doctor"}
    return AskResult("check_availability", dict(keep, doctor=spoken), "doctor", question, options)


def _slot_label(conn, slot, multi):
    return {"day": next_available.short_day(slot["date"]), "time": slot["time"],
            "branch": branches.branch_label(conn, slot["branch_id"]) if multi else None}


def _present_slots(conn, found, doctor, ids, days, start, clinical_adapter, language_code, text, context, offer_base):
    """The "next available" answer: the sentence, a table of days with their times, the citation, and, when the
    same sentence asked to book ("... and book it for Neha"), ONE follow-up question held open as a pending
    question. The question only ever leads to a normal booking review card; nothing is written here."""
    multi = branches.multi_branch(conn)
    doctor_name = doctor["name"] if doctor else None
    labels = [_slot_label(conn, s, multi) for s in found]
    only = {s["branch_id"] for s in found} if found else {branches.resolve(conn, i) for i in ids}
    branch_name = branches.branch_label(conn, next(iter(only))) if multi and len(only) == 1 else None
    data = {"slots": labels, "doctor": doctor_name, "days": days}
    citation = clinical_adapter.citation()
    answer = compose_answer("next_available", data, citation, language_code, branch=branch_name)
    options = None
    if found and offer_base is not None and context is not None:
        lang = offer_base.get("lang") or next_available.language_key(text, language_code)
        slot = found[0]
        booking = dict(offer_base["booking"], appt_date=slot["date"], start_time=slot["time"])
        if multi:
            booking["branch_id"] = slot["branch_id"]
        question = next_available.offer_question(lang, offer_base["name"], slot, doctor_name,
                                                 labels[0]["branch"] if multi and branch_name is None else None)
        offer = dict(offer_base, booking=booking, slot=slot, lang=lang, branch_ids=list(ids), days=days, start=start)
        choices = next_available.OFFER_CHOICES[lang]
        options = [{"label": choices[0], "choice": "yes"}, {"label": choices[1], "choice": "next"}]
        context.hold_question("book_appointment", dict(booking, _question=question, _offer=offer), "book_slot", options)
        answer = _insert_before_source(answer, question)
        options = [{"label": o["label"]} for o in options]
    if context is not None and found:
        context.remember_date(found[0]["date"])
    rows = next_available.day_rows(conn, found) if found else None
    return ReadResult("check_availability", rows, citation, answer, None, options)


def _next_available(conn, slots, text, clinical_adapter, context, language_code):
    """check_availability with a doctor and / or a forward search: the first free slot(s) from a day, the doctor's
    own schedule at that branch, closures, blocks and bookings all honoured (clinic/next_available.py), or one
    day's free slots for one doctor. The doctor is resolved in code: nobody or several is a question, not a guess."""
    doctor = None
    spoken = (slots.get("doctor") or "").strip()
    if spoken:
        status, found_doctors = next_available.resolve_doctor(conn, spoken)
        if status != "one":
            return _ask_doctor(conn, slots, status, found_doctors, spoken, text, language_code)
        doctor = found_doctors[0]
    doctor_id = doctor["id"] if doctor else None
    now = _local_now()
    branch_id, every, branch_name = _branch_scope(conn, slots)
    ids = next_available.branch_ids_for(conn, branch_id, every, doctor_id)
    start = slots.get("appt_date") or now.date().isoformat()
    citation = clinical_adapter.citation()

    if doctor and branch_id and not next_available.works_at(conn, doctor_id, branch_id):
        data = {"slots": [], "doctor": doctor["name"], "days": 0, "not_at_branch": branches.branch_label(conn, branch_id)}
        return ReadResult("check_availability", None, citation, compose_answer("next_available", data, citation, language_code))

    if not slots.get("next_available"):
        # one day's free slots, for one doctor
        rows = []
        for i in ids:
            times = scheduling.generate_slots(conn, start, branch_id=branches.resolve(conn, i), only_doctor_id=doctor_id)
            rows.append((branches.resolve(conn, i), times))
        if context is not None:
            context.remember_date(start)
        if not rows:                       # a doctor who is scheduled at no branch: nothing is free
            data = {"date": start, "slots": [], "doctor": doctor["name"] if doctor else None}
            return ReadResult("check_availability", data, citation, compose_answer("check_availability", data, citation, language_code))
        if len(rows) == 1:
            data = {"date": start, "slots": rows[0][1], "doctor": doctor["name"] if doctor else None}
            if branch_name or branches.multi_branch(conn):
                data["branch"] = branch_name or branches.branch_label(conn, rows[0][0])
            return ReadResult("check_availability", data, citation,
                              compose_answer("check_availability", data, citation, language_code, branch=data.get("branch")))
        data = [{"branch": branches.branch_label(conn, b), "date": start, "free_slots": len(t),
                 "times": ", ".join(t[:8]) + (" ..." if len(t) > 8 else "")} for b, t in rows]
        return ReadResult("check_availability", data, citation, compose_answer("check_availability", data, citation, language_code))

    days = next_available.search_window(start, slots.get("end_date"))
    found = next_available.search(conn, start, now, days, slots.get("limit") or 1, ids, doctor_id)
    offer_base = None
    if slots.get("then_book_for") and context is not None:
        phone = nlu_extract.read_phone_exact(text or "") or nlu_extract.extract_phone(text or "")
        offer_base = {"name": slots["then_book_for"], "doctor_id": doctor_id,
                      "lang": next_available.language_key(text, language_code),
                      "booking": {"patient_name": slots["then_book_for"], "patient_phone": phone, "duration_minutes": None,
                                  "notes": "Requested: {}".format(doctor["name"]) if doctor else None}}
    return _present_slots(conn, found, doctor, ids, days, start, clinical_adapter, language_code, text, context, offer_base)


def offer_next(conn, offer, context, clinical_adapter, language_code, text):
    """"Another time" to the booking question: the next free slot after the one offered (same doctor, branches and
    window), offered the same way; or a note that nothing else is free. Read-only."""
    doctor = branches.get_doctor(conn, offer["doctor_id"]) if offer.get("doctor_id") else None
    slot = offer["slot"]
    found = next_available.search(conn, offer["start"], _local_now(), offer["days"], 1, offer["branch_ids"],
                                  offer.get("doctor_id"), after=(slot["date"], slot["time"]))
    if not found:
        return Note(next_available.no_other_text(offer["lang"], doctor["name"] if doctor else None, offer["days"]))
    base = {k: offer[k] for k in ("name", "doctor_id", "lang", "booking")}
    return _present_slots(conn, found, doctor, offer["branch_ids"], offer["days"], offer["start"], clinical_adapter,
                          language_code, text, context, base)


def respond_to_intent(conn, intent, slots, text, clinical_adapter, ops_adapter, language_code="hi-IN",
                      defer_intents=frozenset(), context=None, skipped=(), fresh=True):
    """Everything after the transcript has become an (intent, slots) pair.
    `fresh` is False when `text` is only the answer to a question the assistant
    asked, so it is not re-read as a new command."""
    if context is not None:
        context.turn_command = (intent, dict(slots))          # for the next turn's "previous turn" line

    if intent == "clarify":
        # The planner needs one detail it was not given. The question is shown as the
        # assistant's own; the next utterance is planned with it as the previous turn.
        question = slots.get("question") or "Could you say that again?"
        if context is None:
            raise PipelineError(question)
        return AskResult("clarify", {"question": question, "text": text}, "clarify", question, [])

    if intent == "open_calendar":
        mode = slots.get("mode")
        return NavigateResult(intent, "appointments", mode, compose_navigation(intent, mode, language_code))

    if intent == "set_my_branch":
        return _switch_branch(conn, slots, context, language_code)

    if intent == "close_branch":
        return _close_branch(conn, slots, context, language_code)

    if intent == "check_availability" and fresh:
        # "next available", "with Dr. Mehta", "... and book it for Neha": read from the words where the planner
        # left them out (clinic/next_available.py); a value the planner already gave is never overridden
        slots = next_available.read_sentence(conn, text, slots)

    slots = voice_branch.default_branch(conn, intent, slots, context)

    if context is not None:
        try:
            slots = apply_context(intent, slots, text, context, fresh=fresh)
        except ValueError as exc:
            raise PipelineError(str(exc))
        question = next_question(conn, intent, slots, clinical_adapter, context, language_code, skipped, heard=text)
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
            patient = _best_effort_patient(clinical_adapter, conn, slots.get("patient_name"), slots)
            if intent in APPOINTMENT_INTENTS:
                # The person is "settled" when an id was carried, a phone number matched them, or the
                # appointment picked from the list on screen says whose it is; the list then holds only
                # their appointments. A name alone is searched among everyone it fits (walk-ins too).
                anchor_id = slots.get("patient_id")
                if anchor_id is None and patient and entity_resolution.full_number(slots.get("patient_phone")):
                    anchor_id = patient.id
                if anchor_id is None and patient is None and slots.get("appointment_id"):
                    anchor_id = clinical_adapter.appointment_patient_id(conn, slots["appointment_id"])
                    if anchor_id is not None:
                        patient = _best_effort_patient(clinical_adapter, conn, None, dict(slots, patient_id=anchor_id))
                options, suggested = _appointment_options(
                    clinical_adapter, conn, slots.get("appointment_id"), slots.get("patient_name"), patient_id=anchor_id)
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
        if intent == "book_appointment":
            # No usable phone yet (a patient who is not registered, none spoken, or a number on file that is
            # not 10 digits): flagged on the card from the start, not only when Approve is refused.
            phone_problem = booking_phone.problem(conn, dict(slots, patient_id=resolved.get("patient_id")))
            if phone_problem:
                resolved = dict(resolved, phone_problem=phone_problem)
        if intent == "book_appointment" and patient and entity_resolution.full_number(slots.get("patient_phone")) \
                and slots.get("patient_name") and not entity_resolution.name_match(slots["patient_name"], patient.label.rsplit(" (", 1)[0]):
            # The phone number was matched first and exactly; say so when the name heard is someone else's.
            resolved = dict(resolved, notes=["This phone number belongs to {}; you said {}.".format(
                patient.label, slots["patient_name"])])
        if intent in ("book_appointment", "reschedule_appointment") and branches.multi_branch(conn):
            notes = _slot_notes(conn, intent, slots)
            if notes:
                resolved = dict(resolved, notes=list(resolved.get("notes", [])) + list(notes))
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
        patient = _resolve_patient_or_raise(clinical_adapter, conn, name, slots)
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
        patient = _resolve_patient_or_raise(clinical_adapter, conn, name, slots)
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
        patient = _best_effort_patient(clinical_adapter, conn, name, slots)
        if patient:
            slots["patient_id"] = patient.id
            patient_label = patient.label
        else:
            patient_label = name or slots.get("patient_phone") or "unregistered caller"
        if not slots.get("appt_date") or not slots.get("start_time"):
            raise PipelineError("Could not hear a date and time for the appointment.")
        phone_problem = booking_phone.problem(conn, slots)
        if phone_problem:
            raise PipelineError(phone_problem)
        pid = core.propose(conn, intent, slots, source_text=text)
        description = "appointment for {} on {} at {}".format(patient_label, slots["appt_date"], slots["start_time"])
        return WriteResult(pid, intent, description)

    if intent in ("cancel_appointment", "reschedule_appointment"):
        name = slots.pop("patient_name", None)
        patient = _resolve_patient_or_raise(clinical_adapter, conn, name, slots)
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
        patient = _resolve_patient_or_raise(clinical_adapter, conn, name, slots)
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

    if intent == "patient_count":
        data = clinical_adapter.patient_counts(conn)
        citation = clinical_adapter.citation()
        return ReadResult(intent, data, citation, compose_answer(intent, data, citation, language_code))

    if intent == "queue_status":
        branch_id, every, branch_name = _branch_scope(conn, slots)
        today_iso = date.today().isoformat()
        if every:
            data = [dict(clinical_adapter.queue_snapshot(conn, today_iso, b["id"]), branch=b["name"])
                    for b in branches.list_branches(conn)]
        else:
            data = clinical_adapter.queue_snapshot(conn, today_iso, branch_id)
        citation = clinical_adapter.citation()
        return ReadResult(intent, data, citation,
                          compose_answer(intent, data, citation, language_code, branch=None if every else branch_name))

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

    if intent == "query":
        return _generic_query(conn, slots, clinical_adapter, context, language_code)

    if intent == "check_availability" and (slots.get("doctor") or slots.get("next_available")):
        return _next_available(conn, slots, text, clinical_adapter, context, language_code)

    if intent == "check_availability":
        appt_date = slots.get("appt_date") or date.today().isoformat()
        branch_id, every, branch_name = _branch_scope(conn, slots)
        if every:
            data = []
            for b in branches.list_branches(conn):
                free = clinical_adapter.available_slots(conn, appt_date, b["id"])
                data.append({"branch": b["name"], "date": appt_date, "free_slots": len(free),
                             "times": ", ".join(free[:8]) + (" ..." if len(free) > 8 else "")})
        else:
            data = {"date": appt_date, "slots": clinical_adapter.available_slots(conn, appt_date, branch_id)}
            if branch_name:
                data["branch"] = branch_name
        if context is not None:
            context.remember_date(appt_date)
        citation = clinical_adapter.citation()
        return ReadResult(intent, data, citation,
                          compose_answer(intent, data, citation, language_code, branch=branch_name))

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
        # A named person is looked up at every branch unless a branch was named;
        # a day list is one branch's (My branch by default) or, on request, all.
        branch_id, every, branch_name = _branch_scope(conn, slots)
        if name:
            data = [dict(row) for row in clinical_adapter.appointments_named(conn, name, start, end, branch_id)]
            caption = "Appointments for {} \u00b7 {}".format(name, when)
        else:
            data = [dict(row) for row in clinical_adapter.scheduled_appointments(conn, start, end, branch_id)]
        if branch_name:
            caption = "{} \u00b7 {}".format(caption or scope or when, branch_name)
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
                          compose_answer(intent, data, citation, language_code, scope=scope, name=name or None,
                                         branch=branch_name),
                          caption)

    if intent == "next_appointment":
        name = slots.pop("patient_name", None)
        patient = _resolve_patient_or_raise(clinical_adapter, conn, name, slots)
        row = clinical_adapter.next_appointment_for_patient(conn, patient.id)
        data = dict(row) if row else None
        if data:
            data["patient_label"] = patient.label
        if context is not None:
            context.remember_patient(patient.id, patient.label)
        citation = clinical_adapter.citation()
        return ReadResult(intent, data, citation, compose_answer(intent, data, citation, language_code))

    raise PipelineError("Unhandled intent: {}".format(intent))
