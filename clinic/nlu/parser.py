import logging

from clinic.nlu import extract
from clinic.nlu.classify import calendar_mode, classify, is_move_command, is_patient_count
from clinic.nlu.datetime_extract import extract_appt_date, extract_appt_time, mentions_unreadable_date
from clinic.nlu.intent_llm import llm_enabled, pick_intent
from clinic.voice_context import contextual_intent, spoken_time
from clinic.nlu.llm_slots import (clear_prefetch, extract_name, local_model_allowed, prefetch_name, reset_known_names,
                                  set_known_names, staff_command)


_logger = logging.getLogger(__name__)


def parse(text, known_names=None, context=None, planner=None):
    """parse(), with `known_names` (registered patient / staff names) made
    available to the name extractor so a name we already know is recognised by
    plain string matching, with no model call (clinic/nlu/llm_slots.py).

    `planner` (a clinic.nlu.planner.PlannerRun, passed by the pipeline when the
    tool-calling planner is on) is asked where the one-word label picker used to
    be asked; when it cannot help, the label picker runs exactly as before.

    With PLANNER_BACKEND=sarvam the local model is not used here at all (no label
    picker, no name lookup): whatever the planner could not place is left to the
    keyword rules and the deterministic readers."""
    token = set_known_names(known_names)
    try:
        with staff_command():
            if planner is None:
                return _parse(text, context)
            intent, slots = _parse(text, context, planner)
            return intent, _fill_missing_name(text, intent, slots, planner)
    finally:
        clear_prefetch()
        reset_known_names(token)


# Intents that never use a name: no point looking one up in the background.
_NAMELESS_INTENTS = frozenset((
    "check_availability", "day_end_cashbook", "missed_followups",
    "queue_status", "open_calendar", "log_expense", "patient_count",
))


def _note_rule(planner, rule):
    """Tell the planner run (if any) which precise rule decided the intent, for the log and the name read."""
    if planner is not None:
        planner.note_rule(rule)


def _spoken_name_or_none(text):
    """The person named in a list command. A list never needed the model before,
    so if the name model is unreachable the list is still shown (as a plain day
    list) rather than the command failing."""
    try:
        return extract_name(text)
    except Exception:
        _logger.warning("Could not look up a name in list command %r", text, exc_info=True)
        return None


# A rule decided the intent and the deterministic readers found no name: the one place the hosted planner is asked
# for just the name (clinic/nlu/planner.py fill_name). "move": an explicit move / shift of an appointment;
# "keywords": the planner scope is 'unmatched' and the keyword rules placed the command. A follow-up on what is on
# screen ("cancel the second one") never asks: its person comes from the screen or the conversation.
_NAME_FILL_RULES = frozenset(("move", "keywords"))
_NAME_SLOT = {intent: "patient_name" for intent in (
    "book_appointment", "cancel_appointment", "reschedule_appointment", "record_visit", "set_followup",
    "cancel_followup", "reschedule_followup", "patient_lookup", "next_appointment")}
_NAME_SLOT["log_attendance"] = "staff_name"


def _fill_missing_name(text, intent, slots, planner):
    """When a precise rule picked the intent and no reader found who the command is about, ask the hosted planner
    for the name words only (one small call) and put them in the name slot. The intent is never changed; the name
    goes through the normal exact entity resolution like any other heard name. A failure leaves the slot empty,
    exactly as before, and the person is asked."""
    key = _NAME_SLOT.get(intent)
    if (key is None or planner is None or planner.asked or planner.rule not in _NAME_FILL_RULES
            or slots.get(key) or slots.get("patient_phone") or slots.get("appointment_id")):
        return slots
    name = planner.fill_name()
    if not name:
        return slots
    return dict(slots, **{key: name})


class UnrecognizedCommand(Exception):
    pass


QUEUE_WRITE_INTENTS = frozenset(("queue_check_in", "queue_call_next", "queue_mark_done", "queue_mark_no_show"))


def _parse(text, context=None, planner=None):
    """Turn a raw ASR transcript into (intent, raw_slots). raw_slots carries
    ASR-hint values only (a name string, a phone digit-string, etc) -- these
    are NOT resolved against the database yet. The caller must still run
    entity resolution (patient/staff name -> id) before this can become a
    proposal (plan §3 rule 2: the model never emits an entity id directly)."""
    # The local model decides; the keyword rules are the backup for when it
    # is unavailable, times out, or says "unclear". Either way the label then
    # runs the identical slot-extraction -> entity-resolution -> propose/read
    # path, so a wrong label can only ever produce a review card a human
    # reads, never a silent write (clinic/nlu/intent_llm.py).
    rules_intent = classify(text)
    # A follow-up that only makes sense with what is on screen ("cancel the
    # second one" with a list showing, "book him" after a lookup) is routed by
    # that precise rule: a bare "cancel" would otherwise default to a
    # follow-up cancellation, and the model has no screen to look at.
    contextual = contextual_intent(text, context) if context is not None else None
    if contextual is not None:
        if rules_intent not in (None, contextual):
            _logger.info("Context rule: %s overrides rules=%s for transcript=%r", contextual, rules_intent, text)
        intent = contextual
        _note_rule(planner, "context")
    elif is_move_command(text):
        intent = rules_intent      # an explicit "move / shift ... appointment": no need to ask the model
        _note_rule(planner, "move")
    elif is_patient_count(text):
        intent = "patient_count"   # "how many patients are registered": a count, never a registration
        _note_rule(planner, "count")
    else:
        planned = planner.ask() if planner is not None and planner.wants(rules_intent) else None
        if planned is not None:
            # The tool call was valid: its (intent, slots) is what the extractors
            # below would have produced for this sentence, so it goes straight to
            # entity resolution and the review card. "unsupported" is final.
            if planned.intent == "unclear":
                raise UnrecognizedCommand(text)
            return planned.intent, dict(planned.slots)
        if planner is not None and planner.skips_model(rules_intent):
            intent = rules_intent      # planner scope "unmatched": the keyword rules stand
            _note_rule(planner, "keywords")
        elif not local_model_allowed():
            # The hosted planner could not help (or is off) and the local model is not used for staff
            # commands: straight to the keyword rules below, no label picker, no name model.
            intent = None
            if planner is not None and planner.asked:
                planner.note_route("rules")
        else:
            if llm_enabled() and rules_intent not in _NAMELESS_INTENTS:
                prefetch_name(text)  # runs while the intent model below is thinking
            intent = pick_intent(text, hint=context.model_hint() if context is not None else None)
            if planner is not None and planner.asked:
                planner.note_route("label_fallback" if intent is not None else "rules")
    if contextual is None and intent is not None and rules_intent is not None and intent != rules_intent:
        _logger.info("Router disagreement: model=%s rules=%s transcript=%r (model wins)", intent, rules_intent, text)
    if intent is None:
        intent = rules_intent
    if intent is None and extract.extract_phone(text):
        # Neither stage found an intent, but a phone number is a strong signal
        # on its own -- only registration involves one (e.g. if ASR clips a
        # leading "naya patient" and leaves just the name/age/phone).
        intent = "register_patient"
    if intent is None:
        if planner is not None and planner.asked:
            planner.note_route("rephrase")
        raise UnrecognizedCommand(text)

    if intent == "register_patient":
        return intent, {
            "name": extract_name(text),
            "phone": extract.extract_phone(text),
            "age": extract.extract_age(text),
        }
    if intent == "register_staff":
        # Role is open-vocabulary (compounder, nurse, receptionist, ...) and
        # not worth a fragile regex -- left blank for the human to fill in on
        # the prefilled form rather than guessed at.
        return intent, {"name": extract_name(text), "role": None}
    if intent == "record_visit":
        return intent, {
            "patient_name": extract_name(text),
            "fee_rupees": extract.extract_amount(text),
        }
    if intent == "set_followup":
        return intent, {
            "patient_name": extract_name(text),
            "days_from_now": extract.extract_days(text),
        }
    if intent == "cancel_followup":
        return intent, {"patient_name": extract_name(text)}
    if intent == "reschedule_followup":
        return intent, {
            "patient_name": extract_name(text),
            "new_due_date": extract_appt_date(text),
        }
    if intent == "patient_lookup":
        return intent, {"patient_name": extract_name(text)}
    if intent == "log_attendance":
        status = "present"
        if "half" in text.lower() or "हाफ" in text:
            status = "half_day"
        elif "absent" in text.lower() or "अनुपस्थित" in text:
            status = "absent"
        elif "leave" in text.lower() or "छुट्टी" in text:
            status = "leave"
        return intent, {"staff_name": extract_name(text), "status": status}
    if intent == "log_expense":
        return intent, {
            "description": text,
            "amount_rupees": extract.extract_amount(text),
        }
    if intent == "book_appointment":
        return intent, {
            "patient_name": extract_name(text),
            "patient_phone": extract.extract_phone(text),
            "appt_date": extract_appt_date(text),
            "start_time": spoken_time(text),
            "duration_minutes": None,
            "notes": None,
        }
    if intent == "cancel_appointment":
        return intent, {"patient_name": extract_name(text)}
    if intent == "reschedule_appointment":
        return intent, {
            "patient_name": extract_name(text),
            "appt_date": extract_appt_date(text),
            "start_time": spoken_time(text),
        }
    if intent == "check_availability":
        slots = {"appt_date": extract_appt_date(text)}
        if mentions_unreadable_date(text):
            slots["date_unreadable"] = True
        return intent, slots
    if intent == "list_appointments":
        # "today" vs "this week" -- deterministic, no LLM: a bare "week"/
        # "hafte" mention widens the range to 7 days, otherwise it's just today.
        normalized = text.lower()
        is_week = any(w in normalized for w in ["week", "हफ्ते", "hafte"])
        if is_week:
            slots = {"range": "week"}
        else:
            # A named day ("tomorrow", "Monday", "15 October"); none means today.
            slots = {"range": "today", "date": extract_appt_date(text)}
            if mentions_unreadable_date(text):
                slots["date_unreadable"] = True
        # "Amit's appointments": a person asked for, not a day. Only added when a
        # name was heard, so a plain day / week list parses exactly as before.
        name = _spoken_name_or_none(text)
        if name:
            slots["patient_name"] = name
        return intent, slots
    if intent == "next_appointment":
        return intent, {"patient_name": extract_name(text)}
    if intent in ("missed_followups", "day_end_cashbook", "queue_status", "patient_count"):
        return intent, {}
    if intent == "open_calendar":
        # Read-only navigation: which embed view (week / month / agenda), or
        # None to keep whatever view is showing.
        return intent, {"mode": calendar_mode(text)}
    if intent in QUEUE_WRITE_INTENTS:
        # Who? By token number ("token 5", regex) if one was said, else by
        # name -- and only then is the (LLM) name extractor consulted. Both
        # are just hints: clinic/pipeline.py resolves them against today's
        # queue deterministically and the review card lets a human correct it.
        token = extract.extract_token_number(text)
        name = None
        mentions_next = any(w in text.lower() for w in ("next", "agla", "agle")) or "अगला" in text or "अगले" in text
        if token is None and not mentions_next:
            name = extract_name(text)
        return intent, {"token": token, "patient_name": name}

    raise UnrecognizedCommand(text)
