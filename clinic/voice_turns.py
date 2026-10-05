"""One conversational turn of the staff voice assistant.

`handle_turn` is what the live voice session calls for every final transcript.
It decides, in this order, whether the utterance is

  1. "never mind" while the assistant is waiting for an answer -> drop it;
  2. the ANSWER to a question the assistant asked ("Which patient?" ...);
  3. an EDIT to the review card already open ("make it 6 pm instead");
  4. a NEW command -> the normal pipeline, with the conversation context.

and keeps the `VoiceContext` up to date. Nothing here writes clinic data: the
results are only read answers, review cards for a human to approve, questions,
and edits to a card that is still waiting for that approval.
"""

import uuid

from clinic import branches, voice_branch
from clinic.nlu.classify import classify
from clinic.nlu.llm_slots import extract_name
from clinic.pipeline import ParsedResult, PipelineError, respond_to_intent, transcript_to_response
from clinic.voice_context import (
    AskResult, CardUpdate, Note, PATIENT_INTENTS, answer_time, ambiguous_patients, describe_edits,
    extract_card_edits, is_never_mind, is_skip, looks_like_edit, pick_option, question_text,
)
from clinic import voice_context
from clinic.nlu import datetime_extract

_MAX_TRIES = 2  # re-asks of one question before the assistant lets it go
_MAX_SHORT_ANSWER_WORDS = 4


def handle_turn(ctx, conn, text, clinical_adapter, ops_adapter, language="hi-IN", defer_intents=frozenset()):
    """Returns a pipeline result (ReadResult, ParsedResult, NavigateResult ...)
    or one of voice_context.AskResult / CardUpdate / Note. Raises PipelineError
    with a human-readable reason when it cannot proceed."""
    ctx.expire_if_idle()
    ctx.touch()
    result = _route(ctx, conn, text, clinical_adapter, ops_adapter, language, defer_intents)
    _remember(ctx, result)
    return result


def handle_pick(ctx, conn, index, clinical_adapter, ops_adapter, language="hi-IN", defer_intents=frozenset()):
    """The person tapped one of the offered options instead of speaking.
    Returns (label, result), or None when no question is waiting."""
    ctx.expire_if_idle()
    pending = ctx.pending
    if not pending or not 0 <= index < len(pending["options"]):
        return None
    ctx.touch()
    option = pending["options"][index]
    ctx.pending = None
    slots = dict(pending["slots"])
    if pending["kind"] == "branch":
        slots["branch_id"] = option["branch_id"]
    else:
        slots["patient_name"] = option["patient_name"]
    result = _continue(ctx, conn, pending["intent"], slots, option["label"], clinical_adapter, ops_adapter,
                       language, defer_intents, pending["skipped"])
    _remember(ctx, result)
    return option["label"], result


# -- routing ---------------------------------------------------------------


def _route(ctx, conn, text, clinical_adapter, ops_adapter, language, defer_intents):
    if ctx.pending:
        if is_never_mind(text):
            ctx.pending = None
            return Note("Okay, dropped that." if language != "hi-IN" else "Theek hai, chhod diya.")
        answered = _answer_pending(ctx, conn, text, clinical_adapter, ops_adapter, language, defer_intents)
        if answered is not None:
            return answered
        ctx.pending = None  # not an answer: it is a new command

    edit = _try_card_edit(ctx, conn, text)
    if edit is not None:
        return edit

    return transcript_to_response(conn, text, clinical_adapter, ops_adapter, language, defer_intents, context=ctx)


def _answer_pending(ctx, conn, text, clinical_adapter, ops_adapter, language, defer_intents):
    pending = ctx.pending
    intent, kind = pending["intent"], pending["kind"]
    # A sentence that clearly is a different command (the keyword rules
    # recognise it) is never swallowed as an "answer".
    if classify(text) is not None:
        return None

    slots = dict(pending["slots"])
    skipped = set(pending["skipped"])

    if kind in ("date", "time") and is_skip(text):
        skipped.add(kind)
        ctx.pending = None
        return _continue(ctx, conn, intent, slots, text, clinical_adapter, ops_adapter, language,
                         defer_intents, skipped)

    if kind == "patient":
        name = _heard_name(text)
        if not name:
            return _ask_again(ctx, pending, language)
        if intent in voice_context.APPOINTMENT_INTENTS and clinical_adapter.upcoming_appointments_named(conn, name):
            # Cancel / reschedule are about an existing booking: the name as
            # heard goes to the appointment search (it finds registered patients
            # and walk-ins alike and keeps the best-sounding person), instead
            # of being swapped for the nearest registered patient.
            slots["patient_name"] = name
            ctx.pending = None
            return _continue(ctx, conn, intent, slots, text, clinical_adapter, ops_adapter, language,
                             defer_intents, skipped)
        candidates = clinical_adapter.resolve_patient(conn, name, 4)
        close = ambiguous_patients(candidates)
        if close:
            options = [{"label": c.label, "patient_name": voice_context._display_name(c.label), "patient_id": c.id}
                       for c in close]
            ctx.ask(intent, slots, "choose_patient", options, skipped)
            return AskResult(intent, slots, "choose_patient", question_text("choose_patient", language), options)
        if candidates and candidates[0].score >= 0.6:
            slots["patient_name"] = voice_context._display_name(candidates[0].label)
        elif intent == "book_appointment":
            slots["patient_name"] = name  # a new, unregistered patient
        else:
            return _ask_again(ctx, pending, language, "I couldn't find {}. ".format(name))
    elif kind == "branch":
        named = voice_branch.find(conn, text, bare=True).branch
        if named is None:
            return _ask_again(ctx, pending, language)
        slots["branch_id"] = named["id"]
    elif kind == "choose_patient":
        index = pick_option(text, pending["options"])
        if index is None:
            return _ask_again(ctx, pending, language)
        slots["patient_name"] = pending["options"][index]["patient_name"]
    elif kind == "date":
        found = datetime_extract.extract_appt_date(text)
        if not found:
            return _ask_again(ctx, pending, language)
        slots["appt_date"] = found
        time_said = voice_context.spoken_time(text)
        if time_said:
            slots["start_time"] = time_said
    elif kind == "time":
        found = answer_time(text)
        if not found:
            return _ask_again(ctx, pending, language)
        slots["start_time"] = found
    else:
        return None

    ctx.pending = None
    return _continue(ctx, conn, intent, slots, text, clinical_adapter, ops_adapter, language,
                     defer_intents, skipped)


def _continue(ctx, conn, intent, slots, text, clinical_adapter, ops_adapter, language, defer_intents, skipped):
    """Carry on with the command once an answer has been merged in. If another
    detail is still missing the assistant asks for it, remembering which ones
    the person already chose to skip."""
    result = respond_to_intent(conn, intent, slots, text, clinical_adapter, ops_adapter, language,
                               defer_intents, context=ctx, skipped=skipped, fresh=False)
    if isinstance(result, AskResult):
        ctx.ask(result.intent, result.slots, result.kind, result.options, skipped)
    return result


def _ask_again(ctx, pending, language, prefix=""):
    tries = pending["tries"] + 1
    if tries >= _MAX_TRIES:
        ctx.pending = None
        raise PipelineError("{}I couldn't get that, so I stopped asking. Say the whole command again.".format(prefix))
    ctx.ask(pending["intent"], pending["slots"], pending["kind"], pending["options"], pending["skipped"], tries)
    return AskResult(pending["intent"], pending["slots"], pending["kind"],
                     prefix + question_text(pending["kind"], language), pending["options"])


def _heard_name(text):
    """The answer to "Which patient?" is usually just the name: use it as
    heard when it is short, and ask the name extractor only for longer
    sentences."""
    cleaned = text.strip().strip("\"'.,।!? ")
    if cleaned and len(cleaned.split()) <= _MAX_SHORT_ANSWER_WORDS and not any(ch.isdigit() for ch in cleaned):
        return cleaned
    try:
        return extract_name(text)
    except Exception:
        return None


def _try_card_edit(ctx, conn, text):
    card = ctx.open_card
    if not card:
        return None
    rules = classify(text)
    if rules is not None and not (rules == card["intent"] and looks_like_edit(text)):
        return None  # a different command, or a fresh one of the same kind
    branch = None
    if card["intent"] in ("book_appointment", "reschedule_appointment"):
        mention = voice_branch.find(conn, text)
        branch, text = mention.branch, mention.text      # "make it Branch C" changes the branch field
    changes = extract_card_edits(card["intent"], text)
    if branch:
        changes["branch_id"] = branch["id"]
    if not changes:
        return None
    card["slots"].update(changes)
    shown = dict(changes, **({"branch_id": branch["name"]} if branch else {}))
    return CardUpdate(card["card_id"], card["intent"], changes, describe_edits(shown))


# -- keeping the context current ------------------------------------------


def _remember(ctx, result):
    if isinstance(result, AskResult):
        if not (ctx.pending and ctx.pending["kind"] == result.kind and ctx.pending["intent"] == result.intent):
            ctx.ask(result.intent, result.slots, result.kind, result.options)
        ctx.last_intent = result.intent
    elif isinstance(result, ParsedResult):
        ctx.pending = None
        ctx.last_intent = result.intent
        ctx.open_card_for(uuid.uuid4().hex[:8], result.intent, result.slots)
        resolved = result.resolved or {}
        if resolved.get("patient_id") and resolved.get("patient_label"):
            ctx.remember_patient(resolved["patient_id"], resolved["patient_label"])
        ctx.remember_date(result.slots.get("appt_date") or result.slots.get("new_due_date"))
    elif isinstance(result, CardUpdate):
        ctx.remember_date(result.changes.get("appt_date"))
    elif isinstance(result, Note):
        pass
    else:
        ctx.pending = None
        ctx.last_intent = getattr(result, "intent", ctx.last_intent)
