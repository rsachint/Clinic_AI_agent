"""Model-first routing: the planner reads every turn, code validates and executes (clinic/architecture.py).

Reached only from clinic/voice_turns.py `_route`, and only when the setting says "model_first" (or "model_reads", which
is this plus model-written reads: `_new_run` then uses clinic/nlu/sql_reads.py, imported nowhere else). Each turn:

  1. the sentence plus a STATE CARD (clinic/state_card.py: the question open, the task and what it has
     heard, the card on screen, the remembered patient, the list on screen, the last turns) goes to the
     tool-calling planner in ONE call, with the usual 19 tools and five conversation tools
     (choose_option, answer_slot, correct_card, new_patient, cancel_task; clinic/nlu/tools.py);
  2. the one tool call that comes back is validated, and then CODE decides what it means: it is mapped
     onto the same voice machinery the keyword rules and a tap use (voice_turns._continue /
     _answer_patient / apply_card_changes, pipeline.respond_to_intent, voice_context.next_question), so
     entity resolution, the booking-phone rule, doctor and schedule checks and the review cards are the
     very ones classic mode runs;
  3. where code can read the sentence itself it wins: a date, a time, a phone number, a branch, and the
     patient's name (only words the user actually said, and a person named in the sentence beats the
     remembered patient).

Nothing here can write clinic data and nothing here can approve: there is no approve / confirm tool, a call
that looks like one is refused, and every write is still a review card a person approves on the screen.
The model never emits an id: it answers with option NUMBERS, names as spoken and branch letters.

When the planner cannot answer (no key, timeout, open circuit breaker, no tool call, an invalid call) the
turn is handed to the classic routing, which runs exactly as it does with the setting off, and the log row
says "mf_fallback_classic". The branch-switch and close-a-branch rules stay rules in this mode.
"""

import logging
import re
import unicodedata

from clinic import architecture, branches, next_available, planner_log, voice_branch, voice_closure
from clinic import state_card
from clinic.entity_resolution import name_match
from clinic.nlu import date_guard, extract, planner, tools
from clinic.nlu.llm_slots import _WORD_SPLIT, match_known_name
from clinic.pipeline import (NOT_CLASSIFIED, NavigateResult, PipelineError, ReadResult, _unanswered_read,
                             respond_to_intent)
from clinic.queries import registered_names
from clinic import voice_context, voice_turns
from clinic.voice_context import AskResult, Note, PATIENT_INTENTS, question_text

_logger = logging.getLogger(__name__)

# What the model is told on top of the planner's usual rules (clinic/nlu/planner.py _RULES).
MODEL_FIRST_RULES = (
    "\nEvery command comes after a STATE CARD that says what is open in the conversation: the task in progress, "
    "the question you are waiting on (with numbered options), the card on screen, the remembered patient, the list "
    "on screen and the last turns. Read the command in its light.\n"
    "If a question is open and the command answers it: choose_option(index) for one of the numbered options (by number, "
    "position, name, or the last digits of the phone), answer_slot(slot, value) for a day, time, phone, branch or "
    "patient's name, new_patient(name) when the user says the person is a NEW patient with that name (the task keeps "
    "everything it has heard). If the command changes a field of the card on screen ('make it 7', 'actually Friday'): "
    "correct_card(field, value). 'Never mind' / 'forget it': cancel_task.\n"
    "A person NAMED in the command is always the patient. The remembered patient is used only when the command refers "
    "back ('him', 'her', 'uska', 'that patient') and names nobody; never copy a name from the STATE CARD into a command "
    "that names someone else. A question or look-up ('who is booked tomorrow?') is a new command: answer it with its "
    "tool and leave the open question alone. Anything else that starts something is a new command with the usual tools.\n"
    "'Find the next available appointment with Dr. X and book it for Y' is ONE command: call query(entity=availability, next=true, "
    "doctor=X); the app then asks the booking question itself. When the question open is a booking offer ('Book Y on ... at ...?'), "
    "a yes ('yes', 'haan', 'ok book it') is choose_option(1), 'another time' / 'the next one' / 'no, later' is choose_option(2), "
    "and a plain no / 'never mind' is cancel_task. A yes only produces the review card: nothing is booked until the user presses Approve.\n"
    "Nothing is ever approved, confirmed or saved by voice and no tool does it: the user presses Approve on the screen. "
    "Never write an id; options are chosen by number.\n"
)

APPROVAL_REFUSAL = {
    "en": "I can't approve or confirm by voice. Check the card on the screen and press Approve there.",
    "hi": "मैं आवाज़ से अप्रूव या कन्फ़र्म नहीं कर सकता। स्क्रीन पर कार्ड देखकर वहीं Approve दबाइए।",
    "hinglish": "Main awaaz se approve ya confirm nahi kar sakta. Screen par card dekhkar wahin Approve dabaiye.",
}

_APPROVAL_TOOL = re.compile(r"approv|confirm|commit|submit|save|accept|authori|finali|execut|apply|proceed|go_?ahead|"
                            r"^yes|^ok(?:ay)?$|press|click|book_it|do_it", re.IGNORECASE)
_STRONG = frozenset("""approve approved approving confirm confirmed confirming save submit accept proceed yes yeah yep ok okay ahead
haan han हाँ हां कन्फर्म अप्रूव ठीक theek thik sahi सही""".split())
_FILLER = frozenset("""it this that please now do go the card done correct right fine is are be karo kar do dijiye
kijiye दो करो कर दीजिए कीजिए hai है ji जी""".split())


def is_approval_phrase(text):
    """True for a sentence that only says "yes / approve it / confirm / ok save it / haan kar do": someone trying to
    approve by voice. Short and made only of approval and filler words, so "confirm Amit's appointment" is not one."""
    words = [w.casefold() for w in re.findall(r"[\wऀ-ॿ]+", unicodedata.normalize("NFC", text or ""))]
    if not words or len(words) > 6 or not all(w in _STRONG or w in _FILLER for w in words):
        return False
    return any(w in _STRONG for w in words)


def approval_like_tool(name):
    return bool(name) and bool(_APPROVAL_TOOL.search(str(name)))


def _language_key(text, language):
    from clinic.unanswered import reply_language
    return reply_language(text, language)


def refusal_note(text, language):
    return Note(APPROVAL_REFUSAL[_language_key(text, language)])


# -- the planner call with the state card -----------------------------------------------------

class ModelFirstRun(planner.PlannerRun):
    """One model-first command's trip through the planner. The same machinery as PlannerRun (the backend, the
    time limit, validation, the date / time cross-check, the log row) with the state card in front of the
    sentence and the five conversation tools added."""

    def __init__(self, conn, text, context, source="voice", backend=None, today=None, drop=None):
        super().__init__(conn, text, context, source, backend, today)
        self.drop = drop
        self.state_card = None
        self.refused = None          # an approval-like tool name the model asked for (refused, never run)
        self.fallback = False

    def _plan(self):
        backend = self.backend or planner.get_backend()
        self.state_card = state_card.build(self.context, self.conn, self.today, self.drop)
        system = planner.build_system_prompt(self.conn, self.today) + MODEL_FIRST_RULES
        user = "{}\n\nNew command: {}".format(self.state_card, self.text)
        self.backend_name = getattr(backend, "name", "local")
        try:
            call = backend.plan(system, user, tools.schemas(dialogue=True))
        finally:
            self.usage = getattr(backend, "last_usage", None)
        if call is None:
            return self.no_tool_call(backend)          # plain words: a short question is shown, else logged and ignored
        return self._accept(call)

    def _accept(self, call):
        """Validate the one tool call the model made and map it onto an (intent, slots) pair: Planned, or None when
        it cannot be used (the reason is in the notes)."""
        self.tool, self.args = call.name, call.args
        if approval_like_tool(call.name) and call.name not in tools.BY_NAME and call.name not in tools.DIALOGUE_BY_NAME:
            self.refused = call.name
            self.error = "refused: an approval-like call"
            self.notes.append("refused: the model asked for {!r}; nothing is approved by voice".format(call.name))
            return None
        ctx = tools.ToolContext(self.conn, self.today, self.text)
        try:
            args = tools.validate(call.name, call.args, ctx, dialogue=True)
            args, overrides = date_guard.crosscheck(call.name, args, self.text, self.today)
            args = tools.validate(call.name, args, ctx, dialogue=True)
            intent, slots = tools.to_parse_result(call.name, args, ctx)
        except tools.ToolError as exc:
            self.error = "{}: {}".format(exc.code, exc)
            self.notes.append("rejected: {}".format(self.error))
            if planner._names_something_unlisted(call.name, exc) and isinstance(call.args, dict):
                self.rejected_query = {"spec": call.args, "reason": str(exc)[:200]}
            _logger.info("Model-first call rejected (%s) for transcript=%r: %s", call.name, self.text, self.error)
            return None
        self.args = args
        if call.name == "unsupported":
            self.wanted = args.get("wanted")
        self.notes.extend(overrides)
        _logger.info("Model-first planner chose %s for transcript=%r (%s)", tools.describe_call(call.name, args),
                     self.text, intent)
        return planner.Planned(intent, slots, call.name, args, list(overrides))

    def _write_log(self, final_intent, slots):
        usage = self.usage
        cost = 0
        if usage is not None:
            from clinic.nlu import sarvam
            cost = sarvam.cost_paise(usage)
        if self.fallback:
            route, detail = "rules", "mf_fallback_classic"
        else:
            route, detail = "planner", "mf:{}".format(self.tool)
        self.log_id = planner_log.record(
            self.conn, self.source, self.text, self.previous, self.tool, self.args, route, final_intent,
            self.latency_ms, self.notes, backend=self.backend_name,
            tokens_in=None if usage is None else usage.prompt_tokens,
            tokens_out=None if usage is None else usage.completion_tokens, cost_paise=cost, route_detail=detail,
            state_card=self.state_card)


# -- the routing -------------------------------------------------------------------------------

READ_INTENTS = frozenset(("patient_count", "patient_lookup", "check_availability", "list_appointments",
                          "next_appointment", "missed_followups", "day_end_cashbook", "queue_status", "query",
                          "open_calendar"))


def _stays_a_rule(conn, text):
    """The two precise routes kept as rules in this mode: a branch switch and closing a branch."""
    if voice_branch.is_switch_command(text):
        return True
    mention = voice_branch.find(conn, text)
    return voice_closure.is_close_command(conn, text, mention)


def route(ctx, conn, text, clinical_adapter, ops_adapter, language, defer_intents, classic):
    """The result of one model-first turn (what voice_turns._route returns). `classic` is the classic routing
    function, called with the same arguments for a turn the planner cannot answer."""
    args = (ctx, conn, text, clinical_adapter, ops_adapter, language, defer_intents)
    if not planner.planner_enabled() or _stays_a_rule(conn, text):
        return classic(*args)
    if ctx.open_card and is_approval_phrase(text) and not (ctx.pending and ctx.pending["kind"] == "book_slot"):
        # (a yes to the booking OFFER is not an approval: it answers that question and only builds a card)
        return refusal_note(text, language)

    run = _new_run(conn, text, ctx)
    planned = run.ask()
    if planned is None:
        return _without_planner(run, classic, args, text, language)

    final = planned.intent
    try:
        result = _dispatch(run, planned, ctx, conn, text, clinical_adapter, ops_adapter, language, defer_intents)
    except _NotApplicable as exc:
        run.notes.append("not applicable: {}; the classic routing ran".format(exc))
        return _without_planner(run, classic, args, text, language)
    except BaseException:
        run.finish(final)
        ctx.turn_call = run.call_summary()
        ctx.planner_log_id = run.log_id
        raise
    run.finish(getattr(result, "intent", None) or final)
    ctx.turn_call = run.call_summary()
    ctx.planner_log_id = run.log_id
    return result


def _new_run(conn, text, ctx):
    """The planner run for this turn. Only "model_reads" mode (clinic/architecture.py) gets the run that can also
    write read queries; its module is imported here and nowhere else, so model_first never loads it."""
    if architecture.reads_by_model(conn):
        from clinic.nlu import sql_reads
        return sql_reads.ModelReadsRun(conn, text, ctx)
    return ModelFirstRun(conn, text, ctx)


class _NotApplicable(Exception):
    """A conversation tool for something that is not open (an option number with no question waiting): the
    classic routing, which knows what to say, takes the turn."""


def _without_planner(run, classic, args, text, language):
    """The planner gave nothing usable. An approval-like call is refused; a read it understood but the app cannot
    run is saved for a person; anything else is handed to the classic routing (which must not ask the planner again)."""
    ctx, conn = args[0], args[1]
    final = "unclear"
    try:
        if run.refused:
            run.fallback = False
            run.tool = run.refused
            return refusal_note(text, args[5])
        if run.rejected_query:
            saved = _unanswered_read(conn, text, run, args[5], "voice")
            if saved is not None:
                return saved
        run.fallback = True
        with planner.suppressed():
            result = classic(*args)
        final = getattr(result, "intent", None) or final
        return result
    finally:
        run.finish(final)
        ctx.turn_call = None
        ctx.planner_log_id = run.log_id


def _dispatch(run, planned, ctx, conn, text, clinical_adapter, ops_adapter, language, defer_intents):
    tool = planned.tool
    if planned.intent == "sql_read":              # model-reads mode only: the query already ran (clinic/nlu/sql_reads.py)
        return run.answer(ctx, conn, language, clinical_adapter)
    if tool in tools.DIALOGUE_BY_NAME:
        handler = _DIALOGUE[tool]
        return handler(ctx, conn, text, planned.slots, clinical_adapter, ops_adapter, language, defer_intents)
    if planned.intent == "unclear":
        saved = _unanswered_read(conn, text, run, language, "voice")
        if saved is not None:
            return saved
        raise PipelineError(NOT_CLASSIFIED)          # the pending question (if any) stays open
    return _new_command(ctx, conn, text, planned.intent, dict(planned.slots), clinical_adapter, ops_adapter, language,
                        defer_intents)


# -- a new command (one of the 19 usual tools) -----------------------------------------------

def _new_command(ctx, conn, text, intent, slots, clinical_adapter, ops_adapter, language, defer_intents):
    mention = voice_branch.find(conn, text)
    if intent not in ("close_branch", "set_my_branch", "clarify"):
        if mention.branch:
            slots["branch_id"] = mention.branch["id"]
        elif mention.every:
            slots["all_branches"] = True
    if intent in PATIENT_INTENTS:
        slots = _apply_name_rules(ctx, conn, _words_said(ctx, text), slots)
    paused = ctx.pending if (ctx.pending and intent in READ_INTENTS) else None
    if paused is None:
        ctx.pending = None                       # a new command replaces the question that was open
    else:
        ctx.resume_pending = paused              # a read leaves it open (voice_turns._remember hands it back)
    if intent not in ("set_my_branch", "close_branch", "clarify"):
        if mention.branch:
            ctx.remember_branch(mention.branch["id"])
        elif mention.mine:
            ctx.remember_branch(None)
    try:
        result = respond_to_intent(conn, intent, slots, text, clinical_adapter, ops_adapter, language, defer_intents,
                                   context=ctx)
    except BaseException:
        ctx.resume_pending = None
        raise
    if paused is not None and isinstance(result, (ReadResult, NavigateResult)) and ctx.resume_pending is paused:
        result = _with_open_question(result, paused, language)       # (a read that opens a question of its own replaces it)
    return result


def _words_said(ctx, text):
    """What the user has said that this command may name a person from: the sentence itself, and, while a clarifying
    question is open, the sentences that question (and any before it in the same chain) was about."""
    said = [text]
    if ctx.pending and ctx.pending["kind"] == "clarify":
        for turn in reversed(ctx.turns):
            if not str(turn.get("call") or "").startswith("clarify("):
                break
            said.insert(0, turn.get("text") or "")
    return " ".join(said)


def _with_open_question(result, pending, language):
    """The read's answer followed by the question that is still open, so it is asked again after the read."""
    if pending["kind"] == "clarify":
        question = (pending.get("slots") or {}).get("question") or ""
    else:
        question = voice_context.pending_question(pending, language)
    if not question:
        return result
    suffix = " Still waiting for your answer: {}".format(question)
    answer = result.answer_text or ""
    head, sep, source = answer.partition(" Source:")
    return result._replace(answer_text="{}{}{}{}".format(head, suffix, sep, source))


def spoken_name(model_name, text):
    """The words of `model_name` that the user actually said in `text`, as they were said ("Priya" for a model
    that expanded it to "Priya Shah" from memory), or ''. A word counts as said when it is the same word
    (any case) or the same name in the other script (राहुल / Rahul)."""
    tokens = [t for t in _WORD_SPLIT.split(unicodedata.normalize("NFC", text or "")) if t]
    found = []
    for word in (model_name or "").split():
        for token in tokens:
            if token.casefold() == word.casefold() or name_match(token, word) == 1.0:
                if token not in found:
                    found.append(token)
                break
    return " ".join(found)


def _apply_name_rules(ctx, conn, text, slots):
    """Code decides who the patient is: only the user's own words count. A name the model took from the state card
    (the remembered patient, an option) instead of from the sentence is dropped, and a person named in the sentence
    is found by exact matching; the remembered patient is used only for "him / her / that patient" (apply_context),
    and a row of the list on screen only when the sentence names nobody (the screen is what "the second one" means)."""
    name = (slots.get("patient_name") or "").strip()
    if not name:
        return slots
    said = spoken_name(name, text)
    if said:
        return dict(slots, patient_name=said)
    known = registered_names(conn)
    names_someone = voice_context.names_a_person(text, known)
    if ctx.patient and voice_context.has_anaphoric_reference(text) and not names_someone:
        return slots                    # "book him": apply_context carries the remembered patient (and its id)
    on_screen = {(row.get("patient_name") or row.get("who") or "").strip().casefold() for row in ctx.list_rows}
    if ctx.list_rows and not names_someone and name.casefold() in on_screen:
        return slots                    # "cancel the one at 4": the name is the list row's
    registered = match_known_name(text, known)
    return dict(slots, patient_name=registered or None)


# -- the conversation tools --------------------------------------------------------------------

def _nothing_open(ctx, why="nothing is open to answer"):
    """A conversation tool with nothing open for it: the classic routing takes the turn (its replies, such as
    "I stopped waiting after 10 minutes", come from voice_turns.handle_turn)."""
    raise _NotApplicable(why)


def _choose_option(ctx, conn, text, slots, clinical_adapter, ops_adapter, language, defer_intents):
    pending = ctx.pending
    if not pending or pending["intent"] == "clarify":
        return _nothing_open(ctx)
    if not pending["options"]:
        return voice_turns._ask_again(ctx, pending, language)          # a question with no options to choose from
    index = slots["index"] - 1
    if not 0 <= index < len(pending["options"]):
        return voice_turns._ask_again(ctx, pending, language)          # a number that is not an option: ask again
    option = pending["options"][index]
    ctx.pending = None
    if pending["kind"] == "book_slot":
        return voice_turns.answer_book_slot(ctx, conn, pending, option.get("choice") or "yes", text, clinical_adapter,
                                            ops_adapter, language, defer_intents)
    merged = voice_turns._slots_with_option(pending, option)
    return voice_turns._continue(ctx, conn, pending["intent"], merged, text, clinical_adapter, ops_adapter, language,
                                 defer_intents, pending["skipped"])


def _answer_slot(ctx, conn, text, slots, clinical_adapter, ops_adapter, language, defer_intents):
    pending = ctx.pending
    if not pending or pending["intent"] == "clarify":
        return _nothing_open(ctx)
    slot, value = slots["slot"], slots["value"]
    merged = dict(pending["slots"])
    skipped = set(pending["skipped"])
    intent = pending["intent"]

    if pending["kind"] == "book_slot":
        # whatever slot the model named, the reply to the booking offer is read from the words: yes / another time / no
        choice = next_available.read_answer(text)
        if choice is None:
            return voice_turns._ask_again(ctx, pending, language)
        ctx.pending = None
        return voice_turns.answer_book_slot(ctx, conn, pending, choice, text, clinical_adapter, ops_adapter, language,
                                            defer_intents)
    if pending["kind"] == "doctor":
        picked = voice_context.pick_option(text, pending["options"]) if pending["options"] else None
        if picked is not None:
            merged["doctor"] = pending["options"][picked]["doctor_name"]
        else:
            said = next_available.doctor_in_sentence(conn, text) or value
            status, found = next_available.resolve_doctor(conn, said)
            if status != "one":
                return voice_turns._ask_again(ctx, pending, language)
            merged["doctor"] = found[0]["name"]
        ctx.pending = None
        return voice_turns._continue(ctx, conn, intent, merged, text, clinical_adapter, ops_adapter, language,
                                     defer_intents, skipped)

    if slot == "patient_name":
        options = pending["options"]
        picked = voice_context.pick_option(text, options) if options else None
        if picked is not None:
            return _choose_option(ctx, conn, text, {"index": picked + 1}, clinical_adapter, ops_adapter, language,
                                  defer_intents)
        name = spoken_name(value, text) or voice_turns._heard_name(text)
        if not name:
            return voice_turns._ask_again(ctx, pending, language)
        settled = voice_turns._answer_patient(ctx, conn, pending, merged, skipped, name, text, clinical_adapter,
                                              ops_adapter, language, defer_intents)
        if settled is not None:
            return settled
    elif slot == "date":
        found = _read_date(text) or value
        merged["appt_date"] = found
        said_time = voice_context.spoken_time(text)
        if said_time:
            merged["start_time"] = said_time
    elif slot == "time":
        merged["start_time"] = voice_context.answer_time(text) or value
    elif slot == "phone":
        number = extract.spoken_phone(text) or extract.read_phone_exact(text) or (value if len(value) == 10 else None)
        if not number:
            return voice_turns._ask_again(ctx, pending, language, heard=text)
        merged["patient_phone"] = number
    elif slot == "branch":
        named = voice_branch.find(conn, text, bare=True).branch or branches.get_branch_by_code(conn, value)
        if named is None:
            return voice_turns._ask_again(ctx, pending, language)
        merged["branch_id"] = named["id"]
    ctx.pending = None
    return voice_turns._continue(ctx, conn, intent, merged, text, clinical_adapter, ops_adapter, language,
                                 defer_intents, skipped)


def _read_date(text):
    """The day the sentence itself says, when code can read it and the sentence names exactly one (the model's
    date is only used when code cannot read one)."""
    phrases = date_guard.date_phrases(text)
    if len(phrases) == 1 and phrases[0][1]:
        return phrases[0][1]
    return None


def _new_patient(ctx, conn, text, slots, clinical_adapter, ops_adapter, language, defer_intents):
    pending = ctx.pending
    if not pending or pending["intent"] == "clarify":
        return _nothing_open(ctx)
    if pending["kind"] in ("book_slot", "doctor"):
        return Note("That is not what I asked. {}".format(voice_context.pending_question(pending, language)))
    if pending["intent"] != "book_appointment":
        return Note("A new patient has no appointment to {} yet. Say \"register a new patient ...\" first."
                    .format("move" if pending["intent"] == "reschedule_appointment" else "cancel")
                    if pending["intent"] in voice_context.APPOINTMENT_INTENTS
                    else "I can only add a new patient while booking an appointment. Say \"register a new patient ...\".")
    name = spoken_name(slots["name"], text)
    if not name:
        return voice_turns._ask_again(ctx, pending, language)
    merged = dict(pending["slots"])
    merged.pop("patient_id", None)               # the new name outvotes whoever was matched before
    merged.pop("appointment_id", None)
    merged["patient_name"] = name                # an unregistered patient unless the name is a registered one exactly
    ctx.pending = None
    return voice_turns._continue(ctx, conn, "book_appointment", merged, text, clinical_adapter, ops_adapter, language,
                                 defer_intents, pending["skipped"])


# card field -> the slot it is on, per card intent (only fields the card on screen really has: static/review_card.js)
_CARD_SLOT = {
    "date": {"book_appointment": "appt_date", "reschedule_appointment": "appt_date", "reschedule_followup": "new_due_date"},
    "time": {"book_appointment": "start_time", "reschedule_appointment": "start_time"},
    "phone": {"book_appointment": "patient_phone", "register_patient": "phone"},
    "branch": {"book_appointment": "branch_id", "reschedule_appointment": "branch_id"},
    "patient_name": {"book_appointment": "patient_name", "register_patient": "name"},
    "fee": {"record_visit": "fee_rupees"},
    "days": {"set_followup": "days_from_now"},
    "age": {"register_patient": "age"},
    "status": {"log_attendance": "status"},
}


def _correct_card(ctx, conn, text, slots, clinical_adapter, ops_adapter, language, defer_intents):
    card = ctx.open_card
    if not card:
        return Note("There is no card open to change.")
    field, value = slots["field"], slots["value"]
    slot = _CARD_SLOT[field].get(card["intent"])
    if slot is None:
        return Note("That card has no {} to change.".format(field.replace("_", " ")))
    branch = None
    extra = {}
    if field == "branch":
        branch = voice_branch.find(conn, text).branch or branches.get_branch_by_code(conn, value)
        if branch is None:
            raise PipelineError("I couldn't tell which branch. Please say it again, for example \"Branch B\".")
        new = branch["id"]
    elif field == "patient_name":
        new = spoken_name(value, text) or value
        if card["intent"] == "book_appointment":
            found = clinical_adapter.resolve_patient(conn, new, 4, phone=card["slots"].get("patient_phone"))
            if len(found) > 1:
                return Note("More than one patient is called {}. Pick the right one in the Patient list on the card.".format(new))
            # the card carries WHO it is about as an id: a new name changes it with the name (a registered patient
            # is selected, anyone else clears it), so the old patient's id never rides along with the new name
            extra["patient_id"] = found[0].id if found else None
            if found:
                new = voice_context._display_name(found[0].label)
    else:
        new = int(value) if field in ("fee", "days", "age") else value
        read = voice_context.extract_card_edits(card["intent"], text)      # what code reads from the sentence wins
        if slot in read:
            new = read[slot]
    changes = dict({slot: new}, **extra)
    update = voice_turns.apply_card_changes(ctx, conn, card, changes, branch)
    if field == "patient_name" and card["intent"] == "book_appointment":
        if extra.get("patient_id") is not None:
            ctx.remember_patient(extra["patient_id"], new)
        else:
            ctx.patient = None
        update = update._replace(summary=voice_context.describe_edits({slot: new}))
    return update


def _cancel_task(ctx, conn, text, slots, clinical_adapter, ops_adapter, language, defer_intents):
    if ctx.pending:
        ctx.pending = None
        return Note("Okay, dropped that." if language != "hi-IN" else "Theek hai, chhod diya.")
    if ctx.open_card:
        return Note("Nothing was changed. The card on screen is still waiting: press Reject there to dismiss it.")
    return Note("Okay." if language != "hi-IN" else "Theek hai.")


_DIALOGUE = {
    "choose_option": _choose_option,
    "answer_slot": _answer_slot,
    "new_patient": _new_patient,
    "correct_card": _correct_card,
    "cancel_task": _cancel_task,
}
