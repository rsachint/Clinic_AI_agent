"""Conversation memory for the staff voice assistant.

A `VoiceContext` lives in server memory for one browser connection (one
`VoiceSession`). It is NEVER written to the database and expires after
IDLE_SECONDS (10 minutes) without activity, or when the page reloads. It holds
only structured facts, not a transcript:

  - the last patient discussed and the last date discussed,
  - the appointment list currently on screen (so "cancel the second one" works),
  - the branch the person last named (so "and what is free tomorrow?" stays at it),
  - the review card currently open (so "make it 6 pm" edits it),
  - a question the assistant has asked and is waiting for an answer to,
  - the last turn in one line (what was said, the command it became, what came
    back), so a short follow-up ("give me the names as well", "and the day
    after?") can be read by the tool-calling planner (clinic/nlu/planner.py).

Everything here only PRE-FILLS a review card, answers a read, or edits an open
card. Nothing in this module can write to the clinic's data: a human still
approves every card with the Approve button (there is no voice approval).

The helpers are pure and deterministic (regexes and small tables, no model):
the same rule as clinic/nlu/extract.py -- a value that ends up in a record is
never invented by a model.
"""

import re
import time
import unicodedata
from collections import namedtuple

from clinic import booking_phone, branches, entity_resolution
from clinic.nlu import datetime_extract, extract, llm_slots

IDLE_SECONDS = 600  # 10 minutes

# The assistant needs an answer before it can build the card.
AskResult = namedtuple("AskResult", ["intent", "slots", "kind", "question", "options"])
# A voice edit to the card that is already open on screen.
CardUpdate = namedtuple("CardUpdate", ["card_id", "intent", "changes", "summary"])
# A plain acknowledgement with nothing to show (e.g. "never mind").
Note = namedtuple("Note", ["message"])

# Intents that are about one patient, by name.
PATIENT_INTENTS = frozenset((
    "book_appointment", "cancel_appointment", "reschedule_appointment",
    "record_visit", "set_followup", "cancel_followup", "reschedule_followup",
    "patient_lookup", "next_appointment",
))
# Intents that need a booked appointment picked from the list on screen.
APPOINTMENT_INTENTS = frozenset(("cancel_appointment", "reschedule_appointment"))


def _norm(text):
    return unicodedata.normalize("NFC", text or "").lower()


def _display_name(label):
    """'Rakesh Verma (9000000001)' -> 'Rakesh Verma'."""
    return re.sub(r"\s*\(\d[\d\s+-]*\)\s*$", "", label or "").strip()


# ---------------------------------------------------------------------------
# The context object
# ---------------------------------------------------------------------------


class VoiceContext:
    def __init__(self, clock=time.monotonic, idle_seconds=IDLE_SECONDS):
        self._clock = clock
        self.idle_seconds = idle_seconds
        # What the browser tells us about itself (not conversation, so clear() keeps it).
        self.my_branch = None      # this computer's My branch
        self.view_branch = None    # the branch (or "all") the switcher shows
        self.clear()

    def clear(self):
        self.patient = None        # {"id", "name"}
        self.date = None           # 'YYYY-MM-DD'
        self.branch = None         # a branch id the person named, kept until another is named
        self.last_intent = None
        self.list_rows = []        # appointments currently on screen, in order
        self.list_scope = None
        self.open_card = None      # {"card_id", "intent", "slots"}
        self.pending = None        # the question being asked, see ask()
        self.last_turn = None      # {"text", "call", "result"}: the previous turn, see remember_turn()
        self.turns = []            # the last few turns, newest last (read by the model-first state card only)
        self.resume_pending = None # model-first: a question a read interrupted, kept alive (see take_resume())
        # Set while one turn is being handled and read when it ends (voice_turns._remember):
        self.turn_call = None      # the planner's tool call this turn, when it made one
        self.turn_command = None   # (intent, slots) this turn ran as
        self.planner_log_id = None # the planner_log row this turn wrote, to record the card's outcome
        self.lost_question = False # a question was open when the memory timed out (read for one turn)
        self._touched = self._clock()

    # -- lifetime ---------------------------------------------------------

    def touch(self):
        self._touched = self._clock()

    def seconds_left(self):
        return max(0, int(self.idle_seconds - (self._clock() - self._touched)))

    def expire_if_idle(self):
        """Forget everything if nothing happened for IDLE_SECONDS. Returns True
        when something was actually forgotten."""
        if self._clock() - self._touched <= self.idle_seconds:
            return False
        had = self.has_content()
        was_asking = bool(self.pending)
        self.clear()
        self.lost_question = was_asking     # so the next turn can say the question timed out
        return had

    def has_content(self):
        return bool(self.patient or self.date or self.branch or self.list_rows or self.open_card or self.pending)

    # -- remembering ------------------------------------------------------

    def remember_patient(self, patient_id, name):
        if patient_id and name:
            self.patient = {"id": patient_id, "name": _display_name(name)}

    def remember_date(self, iso):
        if iso:
            self.date = iso

    def remember_branch(self, branch_id):
        """The person named a branch: later commands stay at it. None forgets it
        (they said "my branch")."""
        self.branch = int(branch_id) if branch_id else None

    def set_client_branch(self, mine, view):
        """The page's My branch / switcher, as it changes (valid ids only: the
        caller checks)."""
        self.my_branch = int(mine) if mine else None
        self.view_branch = view if view == "all" else (int(view) if view else None)

    def current_branch(self):
        """The branch a command is about when none is named in it: the one
        named earlier, else this computer's My branch."""
        return self.branch or self.my_branch

    def remember_list(self, rows, scope, iso_date=None):
        self.list_rows = [dict(r) for r in rows]
        self.list_scope = scope
        if iso_date:
            self.date = iso_date

    def open_card_for(self, card_id, intent, slots):
        self.open_card = {"card_id": card_id, "intent": intent, "slots": dict(slots or {})}

    def close_card(self, card_id=None):
        if self.open_card and (card_id is None or self.open_card["card_id"] == card_id):
            self.open_card = None

    def remember_turn(self, text, call, result):
        self.last_turn = {"text": text, "call": call, "result": result}
        self.turns = (self.turns + [self.last_turn])[-3:]

    def take_resume(self):
        """The question a read paused (model-first mode only), handed back once; None otherwise."""
        pending, self.resume_pending = self.resume_pending, None
        return pending

    def ask(self, intent, slots, kind, options=None, skipped=(), tries=0):
        self.pending = {
            "intent": intent, "slots": dict(slots), "kind": kind,
            "options": list(options or []), "skipped": set(skipped), "tries": tries,
        }

    def hold_question(self, intent, slots, kind, options=None):
        """A question that goes with a READ answer ("Book Neha on Fri 9 Oct at 10:30?" after "next available"):
        the read's turn ends by clearing the pending question, so it is handed back once the turn is remembered
        (see take_resume()). Nothing is asked of the person until then; nothing is written."""
        self.resume_pending = {
            "intent": intent, "slots": dict(slots), "kind": kind,
            "options": list(options or []), "skipped": set(), "tries": 0,
        }

    # -- what the page and the model are told -----------------------------

    def snapshot(self):
        return {
            "patient": self.patient["name"] if self.patient else None,
            "date": self.date,
            "branch": self.branch,
            "awaiting": self.pending["kind"] if self.pending else None,
            "expires_in_s": self.seconds_left(),
        }

    def model_hint(self):
        """One short line for the intent model (names only, no phone numbers),
        so a terse follow-up ("cancel the second one", "book him") routes
        correctly. None when there is nothing to say."""
        bits = []
        if self.list_rows:
            bits.append("a list of {} appointment(s) is on screen".format(len(self.list_rows)))
        if self.open_card:
            bits.append("a '{}' card is open".format(self.open_card["intent"].replace("_", " ")))
        if self.patient:
            bits.append("the last patient discussed is {}".format(self.patient["name"]))
        if not bits:
            return None
        return "Screen context: " + "; ".join(bits) + "."


# ---------------------------------------------------------------------------
# Words and phrases
# ---------------------------------------------------------------------------

_EDGE_L = r"(?<![\wऀ-ॿ])"
_EDGE_R = r"(?![\wऀ-ॿ])"


def _phrase_regex(words):
    alt = "|".join(re.escape(unicodedata.normalize("NFC", w)) for w in sorted(words, key=len, reverse=True))
    return re.compile(_EDGE_L + "(?:" + alt + ")" + _EDGE_R, re.IGNORECASE)


_PATIENT_REFERENCE = _phrase_regex([
    "him", "her", "them", "that patient", "this patient", "the same patient", "same patient",
    "same person", "that person",
    "usko", "use", "unko", "uska", "uski", "unka", "unki", "isko", "inka", "inki", "wahi", "vahi",
    "usi", "isi", "wo patient", "yeh patient",
    "उसे", "उसको", "उसका", "उसकी", "उनको", "उनका", "उनकी", "उन्हें", "इन्हें", "इनका", "इनकी",
    "वही", "उसी", "इसी", "उस मरीज़", "उस मरीज", "इस मरीज़", "इस मरीज",
])
_SAME_DAY = _phrase_regex([
    "same day", "that day", "same date", "that date", "usi din", "usi tareekh", "isi din",
    "उसी दिन", "उसी तारीख", "उसी तारीख़", "इसी दिन", "उस दिन",
])
_NEVER_MIND = _phrase_regex([
    "never mind", "nevermind", "forget it", "forget that", "drop it", "leave it", "stop",
    "rehne do", "rahne do", "chhodo", "chhod do", "nahi chahiye", "mat karo",
    "रहने दो", "रहने दीजिए", "छोड़ो", "छोड़ दो", "नहीं चाहिए", "मत करो", "भूल जाओ",
])
_SKIP = _phrase_regex([
    "skip", "skip it", "no time", "any time", "doesn't matter", "does not matter", "blank",
    "chhodo", "baad mein", "koi bhi", "koi bhi time",
    "छोड़ो", "बाद में", "कोई भी",
])
_EDIT_MARKER = _phrase_regex([
    "instead", "make it", "change it", "change the", "change to", "set it", "set the", "actually",
    "rather", "update", "no,", "not that", "correction",
    "badal", "badlo", "badal do", "ki jagah", "kar do", "kar dijiye", "nahi nahi",
    "बदल", "बदलो", "बदल दो", "की जगह", "के बजाय", "नहीं नहीं", "करो",
])
_BOOK_VERB = _phrase_regex(["book", "fix", "schedule", "बुक", "फिक्स", "शेड्यूल", "book karo", "fix karo"])
_CANCEL_VERB = _phrase_regex(["cancel", "कैंसिल", "रद्द", "radd", "hata do", "hatao", "हटा दो", "हटाओ"])
_RESCHEDULE_VERB = _phrase_regex([
    "reschedule", "postpone", "move", "shift", "रीशेड्यूल", "पोस्टपोन", "shift karo", "date badlo",
])


def has_patient_reference(text):
    return bool(_PATIENT_REFERENCE.search(_norm(text)))


# "her mobile number is ...", "uska number", "his phone": a pronoun that only owns a detail being given is not a
# reference back to the patient discussed ("book her tomorrow" is). Whole attribute phrases only.
_POSSESSIVE = ("her", "his", "their", "uska", "uski", "unka", "unki", "inka", "inki",
               "उसका", "उसकी", "उनका", "उनकी", "इनका", "इनकी")
_ATTRIBUTE = ("mobile", "phone", "cell", "contact", "whatsapp", "number", "num", "no", "new", "own", "correct",
              "age", "address", "email", "umar", "नंबर", "नम्बर", "फोन", "मोबाइल", "नया", "नई", "उम्र", "पता")
_POSSESSIVE_ATTRIBUTE = re.compile(
    _EDGE_L + "(?:" + "|".join(re.escape(unicodedata.normalize("NFC", w)) for w in _POSSESSIVE) + r")\s+"
    "(?:" + "|".join(re.escape(unicodedata.normalize("NFC", w)) for w in sorted(_ATTRIBUTE, key=len, reverse=True))
    + r")(?:\.?\s+(?:" + "|".join(re.escape(unicodedata.normalize("NFC", w)) for w in _ATTRIBUTE) + "))*" + _EDGE_R,
    re.IGNORECASE)


def has_anaphoric_reference(text):
    """True when the sentence points back at the patient just discussed ("book him", "cancel her", "uska
    appointment", "same patient"). A pronoun that is only a possessive on a detail ("her mobile number is
    ...", "uska number") does not count."""
    return bool(_PATIENT_REFERENCE.search(_POSSESSIVE_ATTRIBUTE.sub(" ", _norm(text))))


# Words that can follow "for" / "called" / "named" without being a person's name (command and function words,
# days, months, numbers' words, kinds of visit, pronouns in every spelling), on top of the name reader's list.
_NOT_A_NAME = frozenset("""
same another other each every everyone everybody all both any anyone consultation consult checkup check-up review
routine regular general medical health treatment test tests report reports dressing injection fever cold cough
branch branches clinic doctor dr again once twice later earlier sooner new old first
usi isi wahi vahi uske iske unke inke usko isko unko unhe inhe uska uski unka unki inka inki isi
वही उसी इसी उसके इसके उनके इनके उसको इसको उनको उन्हें इन्हें
""".split())
_NOT_A_NAME = _NOT_A_NAME | frozenset(datetime_extract._WEEKDAYS) | frozenset(datetime_extract._MONTHS)
_NAME_CUE = re.compile(
    _EDGE_L + r"(?:for|called|named|naam|name)\s+(?:is\s+|hai\s+)?([^\s,.;:!?।]+)"
    r"|([^\s,.;:!?।]+)\s+(?:ke|ki|ka|के|की|का)\s+(?:liye|lie|लिए)(?![\wऀ-ॿ])"
    r"|(?:नाम)\s+([^\s,.;:!?।]+)",
    re.IGNORECASE)
_DOCTOR_PHRASE = re.compile(_EDGE_L + r"(?:dr|doctor|डॉक्टर|डॉ)\.?\s+[^\s,.;:!?।]+", re.IGNORECASE)


def _could_be_name(word):
    word = re.sub(r"['\u2019]s$", "", word.strip()).casefold()
    return (len(word) >= 2 and any(ch.isalpha() for ch in word) and not any(ch.isdigit() for ch in word)
            and word not in _NOT_A_NAME and word not in llm_slots._NEIGHBOUR_WORDS)


def names_a_person(text, known_names=None):
    """True when the sentence names someone (so "her" / "him" in it is not a call-back to the remembered
    patient): a registered patient's name written in it (`known_names`, default the names the parser was given,
    "Dr ..." phrases left out), or a word after "for" / "called" / "named" / "naam" / "<word> ke liye" that is not
    a command, day, number or pronoun word. Deterministic; when it cannot tell it says True, because using the
    remembered patient for a sentence that names somebody else is the worse mistake."""
    normalized = unicodedata.normalize("NFC", text or "")
    if not normalized.strip():
        return False
    without_doctors = _DOCTOR_PHRASE.sub(" ", normalized)
    names = known_names if known_names is not None else llm_slots.current_known_names()
    if names and llm_slots.match_known_name(without_doctors, names):
        return True
    for m in _NAME_CUE.finditer(without_doctors):
        word = next((g for g in m.groups() if g), None)
        if word and _could_be_name(word):
            return True
    return False


def has_same_day_reference(text):
    return bool(_SAME_DAY.search(_norm(text)))


def is_never_mind(text):
    return bool(_NEVER_MIND.search(_norm(text)))


def is_skip(text):
    return bool(_SKIP.search(_norm(text)))


def looks_like_edit(text):
    return bool(_EDIT_MARKER.search(_norm(text)))


# ---------------------------------------------------------------------------
# "the second one", "the last one", "number 3"
# ---------------------------------------------------------------------------

_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "pehla": 1, "pehli": 1, "pehle": 1, "pahla": 1, "pahli": 1,
    "doosra": 2, "doosri": 2, "dusra": 2, "dusri": 2, "doosre": 2,
    "teesra": 3, "teesri": 3, "tisra": 3, "tisri": 3,
    "chautha": 4, "chauthi": 4, "paanchva": 5, "paanchvi": 5, "panchva": 5,
    "छठा": 6, "छठी": 6, "सातवां": 7, "आठवां": 8,
    "पहला": 1, "पहली": 1, "पहले": 1, "दूसरा": 2, "दूसरी": 2, "दूसरे": 2, "तीसरा": 3, "तीसरी": 3,
    "चौथा": 4, "चौथी": 4, "पांचवां": 5, "पाँचवाँ": 5, "पांचवी": 5, "पाँचवीं": 5,
}
_ORDINALS = {unicodedata.normalize("NFC", w): n for w, n in _ORDINALS.items()}
_LAST_WORDS = ("last", "akhri", "aakhri", "antim", "आखिरी", "आख़िरी", "अंतिम")
_ORDINAL_REGEX = re.compile(
    _EDGE_L + "(" + "|".join(sorted((re.escape(w) for w in _ORDINALS), key=len, reverse=True)) + ")" + _EDGE_R)
_LAST_REGEX = _phrase_regex(_LAST_WORDS)
_DIGIT_ORDINAL = re.compile(r"(?<![\w])(\d{1,2})(?:st|nd|rd|th)(?![\w])")
_NUMBER_REF = re.compile(r"(?:number|no\.?|#|नंबर|नम्बर)\s*(\d{1,2})(?![\w])", re.IGNORECASE)


def reference_index(text, count):
    """Which row of an on-screen list did the person mean? Returns a 0-based
    index, or None when no row was referred to. Raises ValueError with a
    human-readable message when a row beyond the end of the list was named.
    "second of October" style dates are not row references."""
    normalized = datetime_extract._DAY_THEN_MONTH.sub(" ", _norm(text))
    number = None
    m = _NUMBER_REF.search(normalized) or _DIGIT_ORDINAL.search(normalized)
    if m:
        number = int(m.group(1))
    else:
        m = _ORDINAL_REGEX.search(normalized)
        if m:
            number = _ORDINALS[m.group(1)]
        elif _LAST_REGEX.search(normalized):
            return count - 1 if count else None
    if number is None:
        return None
    if number < 1 or number > count:
        raise ValueError("There are only {} appointment(s) in the list on screen.".format(count))
    return number - 1


# ---------------------------------------------------------------------------
# Carrying context into a new command
# ---------------------------------------------------------------------------


def contextual_intent(text, ctx):
    """Rules-only routing for follow-ups that only make sense with context.
    Used as a backup when the keyword rules and the model both say nothing."""
    if ctx is None:
        return None
    normalized = _norm(text)
    if ctx.list_rows and _has_row_reference(normalized, len(ctx.list_rows)):
        if _CANCEL_VERB.search(normalized):
            return "cancel_appointment"
        if _RESCHEDULE_VERB.search(normalized):
            return "reschedule_appointment"
    # "book him / cancel her" is a call-back to the remembered patient only when the sentence names nobody
    # ("book a consultation for Priya, her mobile number is ..." is a new person: the planner reads it).
    if ctx.patient and has_anaphoric_reference(normalized) and not names_a_person(text):
        if _BOOK_VERB.search(normalized):
            return "book_appointment"
        if _CANCEL_VERB.search(normalized):
            return "cancel_appointment"
    return None


def _has_row_reference(normalized, count):
    try:
        return reference_index(normalized, count) is not None
    except ValueError:
        return True


def apply_context(intent, slots, text, ctx, fresh=True):
    """Fill what the person left out from what was just discussed. Returns the
    (possibly extended) slots; raises ValueError for a row number that does not
    exist. `fresh` is False when `text` is only the answer to a question."""
    slots = dict(slots)
    if ctx is None:
        return slots

    if fresh and intent in APPOINTMENT_INTENTS and ctx.list_rows and not slots.get("appointment_id"):
        index = reference_index(text, len(ctx.list_rows))
        if index is not None:
            row = ctx.list_rows[index]
            slots["appointment_id"] = row["id"]
            if row.get("patient_name"):
                slots["patient_name"] = row["patient_name"]

    if intent in PATIENT_INTENTS and not slots.get("appointment_id") and not slots.get("patient_id"):
        # Only an explicit "him / her / उसका / same patient" carries the last
        # patient over. A bare "book an appointment" asks instead, so a stale
        # patient is never silently attached to the next card. The remembered patient's ID
        # is carried with the name, so two patients who share a name are never mixed up.
        if fresh and ctx.patient and has_anaphoric_reference(text):
            spoken = (slots.get("patient_name") or "").strip()
            if spoken:
                # (a model may fill in the remembered name for "him"; a different name spoken is a new person)
                carry = entity_resolution.name_match(spoken, ctx.patient["name"]) == 1.0
            else:
                # no name read: the remembered patient, unless the sentence itself names someone
                carry = not names_a_person(text)
            if carry:
                slots["patient_name"] = ctx.patient["name"]
                slots["patient_id"] = ctx.patient["id"]

    if fresh and ctx.date and has_same_day_reference(text):
        if intent in ("book_appointment", "reschedule_appointment", "check_availability") and not slots.get("appt_date"):
            slots["appt_date"] = ctx.date
        elif intent == "list_appointments" and not slots.get("date"):
            slots["date"] = ctx.date
            slots.pop("date_unreadable", None)
        elif intent == "reschedule_followup" and not slots.get("new_due_date"):
            slots["new_due_date"] = ctx.date
    return slots


# ---------------------------------------------------------------------------
# Questions the assistant asks instead of opening a half-empty card
# ---------------------------------------------------------------------------

_QUESTIONS = {
    "patient": ("Which patient?", "Kaun sa patient?"),
    # Asked when the command named no patient we could read: says so instead of looking like a guess. English,
    # Hinglish, and (third) Devanagari for a command spoken in Hindi script. The plain "patient" wording above
    # is the re-ask after an answer that was not understood.
    "patient_unheard": ("I couldn't tell who the patient is. Which patient?",
                        "Mujhe samajh nahi aaya ki patient kaun hai. Kaun sa patient?",
                        "मुझे समझ नहीं आया कि मरीज़ कौन है। कौन सा मरीज़?"),
    "choose_patient": ("Which one?", "Kaun sa wala?"),
    "date": ("Which day?", "Kis din?"),
    "time": ("What time?", "Kitne baje?"),
    "branch": ("Which branch?", "Kaun si branch?"),
    "doctor": ("Which doctor?", "Kaun se doctor?", "कौन से डॉक्टर?"),
    # the booking offer after "next available ... and book it for <name>" carries its own full question (slots["_question"])
    "book_slot": ("Shall I book it?", "Book kar doon?", "क्या बुक कर दूँ?"),
    # English, Hinglish, and (third) the Devanagari wording for an answer spoken in Hindi script.
    "phone": ("What is the patient's phone number?", "Patient ka phone number kya hai?",
              "मरीज़ का फ़ोन नंबर क्या है?"),
}

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def question_text(kind, language, heard=None):
    """The fixed question. A question that has a Devanagari wording uses it when the words just heard
    were in Devanagari; otherwise Hinglish for a Hindi session and English for the rest."""
    texts = _QUESTIONS[kind]
    if len(texts) > 2 and _DEVANAGARI.search(heard or ""):
        return texts[2]
    return texts[1] if language == "hi-IN" else texts[0]


def pending_question(pending, language, heard=None):
    """The question an open `pending` asks: its own full wording when it has one (the booking offer: "Book Neha
    Gupta on Fri 9 Oct at 10:30 with Dr. Mehta?"), else the fixed question for its kind."""
    return (pending.get("slots") or {}).get("_question") or question_text(pending["kind"], language, heard)


def ambiguous_patients(candidates):
    """The registered patients a heard name or number could equally be (e.g. two Mohans, or two people on
    one phone), or []. Matching is exact, so this is simply "two or more exact matches"."""
    exact = [c for c in candidates if c.score >= 1.0]
    return exact if len(exact) >= 2 else []


def next_question(conn, intent, slots, adapter, ctx, language="hi-IN", skipped=(), heard=None):
    """The AskResult for whatever is still missing before a card is worth
    showing, or None when the card can be built now. `heard` (the words just
    said) only picks the script of the fixed question."""
    skipped = set(skipped)

    if intent in PATIENT_INTENTS and not slots.get("appointment_id"):
        name = (slots.get("patient_name") or "").strip()
        # A phone number in the command is matched first (exactly); the name is then only needed to tell
        # apart people who share that number.
        by_phone = adapter.resolve_patient(conn, "", 4, phone=slots.get("patient_phone")) \
            if entity_resolution.full_number(slots.get("patient_phone")) else []
        if not name and len(by_phone) == 1:
            slots["patient_name"] = name = _display_name(by_phone[0].label)
        if not name and not by_phone:
            options = []
            if ctx is not None and ctx.patient:
                options = [{"label": ctx.patient["name"], "patient_name": ctx.patient["name"]}]
            return AskResult(intent, slots, "patient", question_text("patient_unheard", language, heard), options)
        candidates = adapter.resolve_patient(conn, name, 4, phone=slots.get("patient_phone"),
                                             patient_id=slots.get("patient_id"))
        close = ambiguous_patients(candidates)
        if close:
            options = [{"label": c.label, "patient_name": _display_name(c.label), "patient_id": c.id} for c in close]
            return AskResult(intent, slots, "choose_patient", question_text("choose_patient", language), options)

    if intent in ("book_appointment", "reschedule_appointment"):
        if not slots.get("appt_date") and "date" not in skipped:
            return AskResult(intent, slots, "date", question_text("date", language), [])
        if not slots.get("start_time") and "time" not in skipped:
            return AskResult(intent, slots, "time", question_text("time", language), [])
    if intent == "book_appointment" and booking_phone_missing(conn, slots, adapter):
        # A phone number is mandatory for a new booking and cannot be skipped: for a patient who is not
        # registered (or whose number on file is unusable) it is asked for here, so the card is not
        # one that can only fail at Approve.
        return AskResult(intent, slots, "phone", question_text("phone", language, heard), [])
    return None


def booking_phone_missing(conn, slots, adapter):
    """True when this booking has no usable phone yet: the named patient is not registered (or has no
    valid number on file) and no valid number was given. Deterministic: names are resolved in code."""
    name = (slots.get("patient_name") or "").strip()
    patient_id = slots.get("patient_id")
    if not patient_id and (name or slots.get("patient_phone")):
        candidates = adapter.resolve_patient(conn, name, 2, phone=slots.get("patient_phone"))
        if len(candidates) == 1:
            patient_id = candidates[0].id
    return booking_phone.problem(conn, dict(slots, patient_id=patient_id)) is not None


def spoken_phone(text):
    """The 10-digit phone number in an answer to "What is the patient's phone number?" ("98765 00301",
    "+91 9876500301", digits read out one by one), or None. The reader lives in clinic/nlu/extract.py (the
    first command reads a number said in words with it too); kept here under its old name."""
    return extract.spoken_phone(text)


def clinic_hour(hour):
    """A bare hour said in answer to "What time?": the reading that falls in
    clinic hours (09-13 and 16-20), e.g. 5 -> 17:00, 10 -> 10:00."""
    if 9 <= hour <= 12:
        return "{:02d}:00".format(hour)
    if 1 <= hour <= 8:
        return "{:02d}:00".format(hour + 12)
    return None


_BARE_NUMBER = re.compile(r"(?<![\w:./-])(\d{1,2})(?![\w:./-])")


def bare_hour(text):
    """'5' / 'make it 6' (a lone number, nothing else numeric) -> 'HH:MM'."""
    numbers = _BARE_NUMBER.findall(_norm(text))
    if len(numbers) != 1 or re.search(r"\d", _BARE_NUMBER.sub("", _norm(text))):
        return None
    return clinic_hour(int(numbers[0]))


_AT_HOUR = re.compile(r"(?<![\w])(?:at|around|by|@)\s+(\d{1,2})(?![\w:./-])", re.IGNORECASE)


def at_hour(text):
    """'... tomorrow at 5' (no am/pm) -> the clinic-hours reading, '17:00'."""
    m = _AT_HOUR.search(_norm(text))
    return clinic_hour(int(m.group(1))) if m else None


def spoken_time(text):
    """A time said in any supported way: '5 pm', '5 baje', 'paanch baje', 'at 5'."""
    return datetime_extract.extract_appt_time(text) or at_hour(text)


def answer_time(text):
    """The answer to 'What time?' -- also accepts a lone '5'."""
    return spoken_time(text) or bare_hour(text)


def pick_option(text, options):
    """Which of the offered options did the person pick? By position ("the
    second", "doosra", "2") or by a distinguishing word of the name ("Lal").
    Returns an index or None."""
    count = len(options)
    try:
        index = reference_index(text, count)
    except ValueError:
        return None
    if index is not None:
        return index
    normalized = _norm(text)
    digits = re.fullmatch(r"\s*(\d{1,2})\s*", normalized)
    if digits and 1 <= int(digits.group(1)) <= count:
        return int(digits.group(1)) - 1
    words = set(re.findall(r"[\wऀ-ॿ]+", normalized))
    hits = []
    for i, option in enumerate(options):
        parts = set(re.findall(r"[\wऀ-ॿ]+", _norm(option.get("patient_name") or option.get("label", ""))))
        others = set()
        for j, other in enumerate(options):
            if j != i:
                others |= set(re.findall(r"[\wऀ-ॿ]+", _norm(other.get("patient_name") or other.get("label", ""))))
        distinguishing = parts - others
        if distinguishing & words:
            hits.append(i)
    return hits[0] if len(hits) == 1 else None


# ---------------------------------------------------------------------------
# Editing the card that is already open ("make it 6 pm instead")
# ---------------------------------------------------------------------------

_STATUS_WORDS = (
    ("half_day", ("half day", "half-day", "हाफ")),
    ("absent", ("absent", "अनुपस्थित")),
    ("leave", ("leave", "छुट्टी")),
    ("present", ("present", "उपस्थित", "हाज़िर")),
)

_FIELD_LABELS = {
    "appt_date": "Date", "start_time": "Start time", "patient_phone": "Phone", "fee_rupees": "Fee",
    "days_from_now": "Days from now", "new_due_date": "New due date", "status": "Status",
    "phone": "Phone", "age": "Age", "branch_id": "Branch",
}


def extract_card_edits(card_intent, text):
    """The fields of the open card the person just changed, as {slot: value}.
    Deterministic extractors only; {} when nothing recognisable was said."""
    edits = {}
    if card_intent in ("book_appointment", "reschedule_appointment"):
        appt_date = datetime_extract.extract_appt_date(text)
        if appt_date:
            edits["appt_date"] = appt_date
        start = spoken_time(text) or bare_hour(text)
        if start:
            edits["start_time"] = start
        if card_intent == "book_appointment":
            phone = extract.extract_phone(text)
            if phone:
                edits["patient_phone"] = phone
    elif card_intent == "record_visit":
        amount = extract.extract_amount(text)
        if amount:
            edits["fee_rupees"] = amount
    elif card_intent == "set_followup":
        days = extract.extract_days(text)
        if days:
            edits["days_from_now"] = days
    elif card_intent == "reschedule_followup":
        due = datetime_extract.extract_appt_date(text)
        if due:
            edits["new_due_date"] = due
    elif card_intent == "log_attendance":
        normalized = _norm(text)
        for status, words in _STATUS_WORDS:
            if any(w in normalized for w in words):
                edits["status"] = status
                break
    elif card_intent == "register_patient":
        phone = extract.extract_phone(text)
        age = extract.extract_age(text)
        if phone:
            edits["phone"] = phone
        if age:
            edits["age"] = age
    return edits


def drop_stale_identity(conn, slots, changes):
    """A voice edit to an open card that changes WHO the card is about (the patient's name, or a phone number
    other than the registered patient's own) makes the id the card carried stale: drop it, so an old id
    never outvotes what the person just said. A phone given to a patient who has none on file is not a
    change of person. Returns the id that was dropped, or None."""
    old_id = slots.get("patient_id")
    if old_id is None:
        return None
    stale = False
    if changes.get("patient_name") and entity_resolution.name_match(changes["patient_name"], _registered_name(conn, old_id)) != 1.0:
        stale = True
        slots.pop("appointment_id", None)          # that appointment was the old person's
    number = entity_resolution.full_number(changes.get("patient_phone"))
    if number:
        on_file = conn.execute("SELECT phone FROM patients WHERE id = ?", (old_id,)).fetchone()
        own = entity_resolution.full_number(on_file["phone"]) if on_file else ""
        stale = stale or (bool(own) and own != number)
    if stale:
        slots.pop("patient_id", None)
        slots.pop("patient_label", None)
        return old_id
    return None


def _registered_name(conn, patient_id):
    row = conn.execute("SELECT name FROM patients WHERE id = ?", (patient_id,)).fetchone()
    return row["name"] if row else ""


def describe_edits(changes):
    return ", ".join("{} -> {}".format(_FIELD_LABELS.get(k, k), v) for k, v in changes.items())
