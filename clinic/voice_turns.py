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

import re
import uuid
from datetime import date

from clinic import architecture, branches, next_available, voice_branch
from clinic.nlu import tools
from clinic.nlu.classify import classify
from clinic.nlu.llm_slots import extract_name, staff_command
from clinic.pipeline import (ClosurePlanResult, NOT_CLASSIFIED, NavigateResult, ParsedResult, PipelineError, ReadResult,
                             SwitchBranchResult, offer_next, respond_to_intent, transcript_to_response)
from clinic.unanswered import reply_language
from clinic.voice_context import (
    AskResult, CardUpdate, Note, PATIENT_INTENTS, answer_time, ambiguous_patients, describe_edits,
    extract_card_edits, is_never_mind, is_skip, looks_like_edit, pick_option, question_text,
)
from clinic import voice_context
from clinic.nlu import datetime_extract

_MAX_TRIES = 2  # re-asks of one question before the assistant lets it go
_MAX_SHORT_ANSWER_WORDS = 4


# What to say when a bare answer ("8th of October") arrives but the question it answers is gone. Fixed text, three
# languages like the other voice replies. "timed out": we know a question was open and the 10-minute memory dropped it.
# "no question": nothing is open and the words look like an answer, not a command.
FORGOTTEN_REPLIES = {
    "timed out": {
        "en": "I stopped waiting for your answer after 10 minutes. Please say the whole command again.",
        "hi": "मैं 10 मिनट तक आपके जवाब का इंतज़ार करके रुक गया था। कृपया पूरा कमांड फिर से बोलिए।",
        "hinglish": "Main 10 minute tak aapke jawab ka intezaar karke ruk gaya tha. Kripya poora command phir se bolein.",
    },
    "no question": {
        "en": "I don't have an earlier question to match that to. Please say the whole command again, for example \"Book Amit tomorrow at 4 PM\".",
        "hi": "इस जवाब के लिए मेरे पास कोई पिछला सवाल नहीं है। कृपया पूरा कमांड फिर से बोलिए, जैसे \"अमित को कल शाम 4 बजे बुक करो\"।",
        "hinglish": "Is jawab ke liye mere paas koi pichla sawaal nahi hai. Kripya poora command phir se bolein, jaise \"Amit ko kal 4 baje book karo\".",
    },
}


_FILLER_WORDS = frozenset(("on", "at", "of", "the", "for", "in", "a", "m", "am", "pm", "p", "o", "clock", "please", "to", "by"))
_MAX_FRAGMENT_WORDS = 3


def _is_bare_day_or_time(text):
    """A short fragment that is only a day or a time ("8th of October", "at 4 pm", "next Monday at 5"), not a
    sentence that happens to contain a day word ("the weather is nice today")."""
    if not (datetime_extract.extract_appt_date(text, date.today()) or voice_context.spoken_time(text)):
        return False
    words = [w for w in re.findall(r"[\w\u0900-\u097F]+", text.lower()) if w not in _FILLER_WORDS]
    return len(words) <= _MAX_FRAGMENT_WORDS


def _forgotten_reply(ctx, text, language):
    """The clearer message for an unclassifiable utterance that is really the answer to a question the
    assistant no longer has, or None when it is just an unknown command (the usual message then stands)."""
    if ctx.lost_question:
        kind = "timed out"
    elif _is_bare_day_or_time(text):
        kind = "no question"          # only a day or a time was said: that answers something, it is not a command
    else:
        return None
    return FORGOTTEN_REPLIES[kind][reply_language(text, language)]


def handle_turn(ctx, conn, text, clinical_adapter, ops_adapter, language="hi-IN", defer_intents=frozenset()):
    """Returns a pipeline result (ReadResult, ParsedResult, NavigateResult ...)
    or one of voice_context.AskResult / CardUpdate / Note. Raises PipelineError
    with a human-readable reason when it cannot proceed."""
    ctx.expire_if_idle()
    ctx.touch()
    ctx.turn_call = ctx.turn_command = ctx.planner_log_id = None
    try:
        result = _route(ctx, conn, text, clinical_adapter, ops_adapter, language, defer_intents)
    except PipelineError as exc:
        reply = _forgotten_reply(ctx, text, language) if str(exc) == NOT_CLASSIFIED else None
        if reply is not None:
            raise PipelineError(reply) from exc
        raise
    finally:
        ctx.lost_question = False         # only the first turn after a timeout may say so
    _remember(ctx, result)
    _remember_turn(ctx, text, result)
    return result


def handle_pick(ctx, conn, index, clinical_adapter, ops_adapter, language="hi-IN", defer_intents=frozenset()):
    """The person tapped one of the offered options instead of speaking.
    Returns (label, result), or None when no question is waiting."""
    ctx.expire_if_idle()
    pending = ctx.pending
    if not pending or not 0 <= index < len(pending["options"]):
        return None
    ctx.touch()
    ctx.turn_call = ctx.turn_command = ctx.planner_log_id = None
    option = pending["options"][index]
    ctx.pending = None
    if pending["kind"] == "book_slot":
        result = answer_book_slot(ctx, conn, pending, option.get("choice") or "yes", option["label"], clinical_adapter,
                                  ops_adapter, language, defer_intents)
        _remember(ctx, result)
        _remember_turn(ctx, option["label"], result)
        return option["label"], result
    slots = _slots_with_option(pending, option)
    result = _continue(ctx, conn, pending["intent"], slots, option["label"], clinical_adapter, ops_adapter,
                       language, defer_intents, pending["skipped"])
    _remember(ctx, result)
    _remember_turn(ctx, option["label"], result)
    return option["label"], result


def _slots_with_option(pending, option):
    """The open question's slots with the chosen option merged in (a tap, or the model's choose_option)."""
    slots = dict(pending["slots"])
    if pending["kind"] == "branch":
        slots["branch_id"] = option["branch_id"]
    elif pending["kind"] == "doctor":
        slots["doctor"] = option["doctor_name"]
    else:
        slots["patient_name"] = option["patient_name"]
        if option.get("patient_id") is not None:
            slots["patient_id"] = option["patient_id"]     # which of two same-named patients was tapped
    return slots


# -- routing ---------------------------------------------------------------


def _route(ctx, conn, text, clinical_adapter, ops_adapter, language, defer_intents):
    """Which architecture decides this sentence (clinic/architecture.py), read at every command. Classic is the
    original rules-first routing below, untouched; model first hands the sentence to clinic/nlu/dialogue.py,
    which falls back to the classic routing for any turn the planner cannot answer. The model-first modules are
    imported only inside that branch, so with classic nothing of them is even loaded."""
    if architecture.is_model_first(conn):
        from clinic.nlu import dialogue
        return dialogue.route(ctx, conn, text, clinical_adapter, ops_adapter, language, defer_intents,
                              classic=_route_classic)
    return _route_classic(ctx, conn, text, clinical_adapter, ops_adapter, language, defer_intents)


def _route_classic(ctx, conn, text, clinical_adapter, ops_adapter, language, defer_intents):
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


def answer_book_slot(ctx, conn, pending, choice, text, clinical_adapter, ops_adapter, language, defer_intents):
    """The reply to the booking offer ("Book Neha Gupta on Fri 9 Oct at 10:30 with Dr. Mehta?"): yes, another time,
    or no. YES only builds the normal booking review card for that slot (patient, date, time, branch, doctor note
    filled; a new patient's phone is still asked for); it never approves anything, nothing is written until a person
    presses Approve on the card. The question is already closed (ctx.pending is None) when this runs."""
    ctx.pending = None
    offer = pending["slots"]["_offer"]
    if choice == "yes":
        return _continue(ctx, conn, "book_appointment", next_available.booking_slots(offer), text, clinical_adapter,
                         ops_adapter, language, defer_intents, set())
    if choice == "next":
        return offer_next(conn, offer, ctx, clinical_adapter, language, text)
    return Note(next_available.DROPPED[offer["lang"]])


def _answer_pending(ctx, conn, text, clinical_adapter, ops_adapter, language, defer_intents):
    pending = ctx.pending
    intent, kind = pending["intent"], pending["kind"]
    if kind == "book_slot":
        # yes / "ok book it" / another time / no answer the offer BEFORE the keyword rules look at it ("book it"
        # would otherwise read as a new booking command)
        choice = next_available.read_answer(text)
        if choice is not None:
            ctx.pending = None
            return answer_book_slot(ctx, conn, pending, choice, text, clinical_adapter, ops_adapter, language, defer_intents)
    # A sentence that clearly is a different command (the keyword rules
    # recognise it) is never swallowed as an "answer". The one exception: a
    # phone number said as "my phone number is ..." reads like a lookup to the
    # keyword rules, but with a whole number in it, it is the answer.
    rules = classify(text)
    if rules is not None and not (kind == "phone" and rules == "patient_lookup" and voice_context.spoken_phone(text)):
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
        settled = _answer_patient(ctx, conn, pending, slots, skipped, name, text, clinical_adapter, ops_adapter,
                                  language, defer_intents)
        if settled is not None:
            return settled
    elif kind == "phone":
        number = voice_context.spoken_phone(text)
        if not number:
            return _ask_again(ctx, pending, language, heard=text)
        slots["patient_phone"] = number
    elif kind == "branch":
        named = voice_branch.find(conn, text, bare=True).branch
        if named is None:
            return _ask_again(ctx, pending, language)
        slots["branch_id"] = named["id"]
    elif kind == "doctor":
        picked = pick_option(text, pending["options"]) if pending["options"] else None
        if picked is not None:
            slots["doctor"] = pending["options"][picked]["doctor_name"]
        else:
            status, found = next_available.resolve_doctor(conn, text)
            if status != "one":
                return _ask_again(ctx, pending, language)
            slots["doctor"] = found[0]["name"]
    elif kind == "choose_patient":
        index = pick_option(text, pending["options"])
        if index is None:
            return _ask_again(ctx, pending, language)
        chosen = pending["options"][index]
        slots["patient_name"] = chosen["patient_name"]
        if chosen.get("patient_id") is not None:
            # Two patients can have the same name: remember WHICH one was chosen, not just the name.
            slots["patient_id"] = chosen["patient_id"]
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


def _answer_patient(ctx, conn, pending, slots, skipped, name, text, clinical_adapter, ops_adapter, language,
                    defer_intents):
    """A patient's name answering "which patient?". Merges it into `slots` and returns None to carry on, or
    returns the result that settles it here (a continued command, "which one?", or the question again).
    Shared by the keyword route above and the model's answer_slot / new_patient (clinic/nlu/dialogue.py)."""
    intent = pending["intent"]
    slots.pop("patient_id", None)          # a new name answers "which patient?": an older id must not outvote it
    if intent in voice_context.APPOINTMENT_INTENTS and clinical_adapter.upcoming_appointments_named(conn, name):
        # Cancel / reschedule are about an existing booking: the name as
        # heard goes to the appointment search (it finds registered patients
        # and walk-ins alike and keeps the best-sounding person), instead
        # of being swapped for the nearest registered patient.
        slots["patient_name"] = name
        ctx.pending = None
        return _continue(ctx, conn, intent, slots, text, clinical_adapter, ops_adapter, language,
                         defer_intents, skipped)
    candidates = clinical_adapter.resolve_patient(conn, name, 4, phone=slots.get("patient_phone"))
    close = ambiguous_patients(candidates)
    if close:
        options = [{"label": c.label, "patient_name": voice_context._display_name(c.label), "patient_id": c.id}
                   for c in close]
        ctx.ask(intent, slots, "choose_patient", options, skipped)
        return AskResult(intent, slots, "choose_patient", question_text("choose_patient", language), options)
    if len(candidates) == 1:
        slots["patient_name"] = voice_context._display_name(candidates[0].label)
        slots["patient_id"] = candidates[0].id          # resolved once: carried from here on
    elif intent == "book_appointment":
        slots["patient_name"] = name  # a new, unregistered patient
    else:
        return _ask_again(ctx, pending, language, "I couldn't find {}. ".format(name))
    return None


def _continue(ctx, conn, intent, slots, text, clinical_adapter, ops_adapter, language, defer_intents, skipped):
    """Carry on with the command once an answer has been merged in. If another
    detail is still missing the assistant asks for it, remembering which ones
    the person already chose to skip."""
    result = respond_to_intent(conn, intent, slots, text, clinical_adapter, ops_adapter, language,
                               defer_intents, context=ctx, skipped=skipped, fresh=False)
    if isinstance(result, AskResult):
        ctx.ask(result.intent, result.slots, result.kind, result.options, skipped)
    return result


def _ask_again(ctx, pending, language, prefix="", heard=None):
    tries = pending["tries"] + 1
    if tries >= _MAX_TRIES:
        ctx.pending = None
        raise PipelineError("{}I couldn't get that, so I stopped asking. Say the whole command again.".format(prefix))
    ctx.ask(pending["intent"], pending["slots"], pending["kind"], pending["options"], pending["skipped"], tries)
    return AskResult(pending["intent"], pending["slots"], pending["kind"],
                     prefix + voice_context.pending_question(pending, language, heard), pending["options"])


def _heard_name(text):
    """The answer to "Which patient?" is usually just the name: use it as
    heard when it is short, and ask the name extractor only for longer
    sentences."""
    cleaned = text.strip().strip("\"'.,।!? ")
    if cleaned and len(cleaned.split()) <= _MAX_SHORT_ANSWER_WORDS and not any(ch.isdigit() for ch in cleaned):
        return cleaned
    try:
        with staff_command():            # with the hosted planner selected, no local model for this either
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
    return apply_card_changes(ctx, conn, card, changes, branch)


def apply_card_changes(ctx, conn, card, changes, branch=None):
    """Put `changes` ({slot: value}) on the open card and say so. A change of WHO the card is about drops the
    id it carried (an old id never outvotes what was just said), and the remembered patient with it. Shared by
    the keyword card edit above and the model's correct_card (clinic/nlu/dialogue.py)."""
    dropped = voice_context.drop_stale_identity(conn, card["slots"], changes)
    if dropped is not None and ctx.patient and str(ctx.patient["id"]) == str(dropped):
        ctx.patient = None          # "him" must not mean the person the card was about before the edit
    card["slots"].update(changes)
    shown = dict(changes, **({"branch_id": branch["name"]} if branch else {}))
    return CardUpdate(card["card_id"], card["intent"], changes, describe_edits(shown))


# -- keeping the context current ------------------------------------------

_SOURCE_TAIL = re.compile(r"\s*Source:.*$")


def _result_summary(result):
    """What came back, in a few words, for the previous-turn line the planner reads."""
    if isinstance(result, ReadResult):
        return _SOURCE_TAIL.sub("", result.answer_text or "").strip() or "an answer was shown"
    if isinstance(result, AskResult):
        return "asked the user: {}".format(result.question)
    if isinstance(result, ParsedResult):
        return "showed a review card for {} (nothing saved until approved)".format(result.intent)
    if isinstance(result, (ClosurePlanResult, NavigateResult, SwitchBranchResult)):
        return _SOURCE_TAIL.sub("", result.answer_text or "").strip()
    if isinstance(result, CardUpdate):
        return "updated the open card: {}".format(result.summary)
    return None


def _remember_turn(ctx, text, result):
    """Keep this turn as one line -- what was said, the command it became, what
    came back -- so a short follow-up can be read in its light. The call is the
    planner's own when it made one; otherwise it is derived from the intent the
    rules chose, in the same notation."""
    summary = _result_summary(result)
    if summary is None:
        return
    call = ctx.turn_call
    if not call and ctx.turn_command:
        intent, slots = ctx.turn_command
        call = tools.describe_intent(intent, slots, date.today().isoformat())
    if call:
        ctx.remember_turn(text, call, summary)


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
        ctx.pending = ctx.take_resume()      # None, except in model-first mode where a read keeps an open question
        ctx.last_intent = getattr(result, "intent", ctx.last_intent)
