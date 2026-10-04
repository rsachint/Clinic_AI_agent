"""Conversation memory for the staff voice assistant.

A `VoiceContext` lives in server memory for one browser connection (one
`VoiceSession`). It is NEVER written to the database and expires after
IDLE_SECONDS (10 minutes) without activity, or when the page reloads. It holds
only structured facts, not a transcript:

  - the last patient discussed and the last date discussed,
  - the appointment list currently on screen (so "cancel the second one" works),
  - the review card currently open (so "make it 6 pm" edits it),
  - a question the assistant has asked and is waiting for an answer to.

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

from clinic.nlu import datetime_extract, extract

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
        self.clear()

    def clear(self):
        self.patient = None        # {"id", "name"}
        self.date = None           # 'YYYY-MM-DD'
        self.last_intent = None
        self.list_rows = []        # appointments currently on screen, in order
        self.list_scope = None
        self.open_card = None      # {"card_id", "intent", "slots"}
        self.pending = None        # the question being asked, see ask()
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
        self.clear()
        return had

    def has_content(self):
        return bool(self.patient or self.date or self.list_rows or self.open_card or self.pending)

    # -- remembering ------------------------------------------------------

    def remember_patient(self, patient_id, name):
        if patient_id and name:
            self.patient = {"id": patient_id, "name": _display_name(name)}

    def remember_date(self, iso):
        if iso:
            self.date = iso

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

    def ask(self, intent, slots, kind, options=None, skipped=(), tries=0):
        self.pending = {
            "intent": intent, "slots": dict(slots), "kind": kind,
            "options": list(options or []), "skipped": set(skipped), "tries": tries,
        }

    # -- what the page and the model are told -----------------------------

    def snapshot(self):
        return {
            "patient": self.patient["name"] if self.patient else None,
            "date": self.date,
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
    if ctx.patient and has_patient_reference(normalized) and _BOOK_VERB.search(normalized):
        return "book_appointment"
    if ctx.patient and has_patient_reference(normalized) and _CANCEL_VERB.search(normalized):
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

    if intent in PATIENT_INTENTS and not slots.get("patient_name") and not slots.get("appointment_id"):
        # Only an explicit "him / her / उसका / same patient" carries the last
        # patient over. A bare "book an appointment" asks instead, so a stale
        # patient is never silently attached to the next card.
        if fresh and ctx.patient and has_patient_reference(text):
            slots["patient_name"] = ctx.patient["name"]

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
    "choose_patient": ("Which one?", "Kaun sa wala?"),
    "date": ("Which day?", "Kis din?"),
    "time": ("What time?", "Kitne baje?"),
}


def question_text(kind, language):
    english, hinglish = _QUESTIONS[kind]
    return hinglish if language == "hi-IN" else english


def ambiguous_patients(candidates):
    """The registered patients a heard name could equally be (e.g. two
    Mohans), or []. Needs at least two candidates scoring >= 0.6 within 0.08 of
    the best one, and the best one not an exact full-name match."""
    if len(candidates) < 2:
        return []
    best = candidates[0].score
    if best >= 0.999 or best < 0.6:
        return []
    close = [c for c in candidates if c.score >= 0.6 and best - c.score <= 0.08]
    return close if len(close) >= 2 else []


def next_question(conn, intent, slots, adapter, ctx, language="hi-IN", skipped=()):
    """The AskResult for whatever is still missing before a card is worth
    showing, or None when the card can be built now."""
    skipped = set(skipped)

    if intent in PATIENT_INTENTS and not slots.get("appointment_id"):
        name = (slots.get("patient_name") or "").strip()
        if not name:
            options = []
            if ctx is not None and ctx.patient:
                options = [{"label": ctx.patient["name"], "patient_name": ctx.patient["name"]}]
            return AskResult(intent, slots, "patient", question_text("patient", language), options)
        candidates = adapter.resolve_patient(conn, name, 4)
        close = ambiguous_patients(candidates)
        if close:
            options = [{"label": c.label, "patient_name": _display_name(c.label), "patient_id": c.id} for c in close]
            return AskResult(intent, slots, "choose_patient", question_text("choose_patient", language), options)

    if intent in ("book_appointment", "reschedule_appointment"):
        if not slots.get("appt_date") and "date" not in skipped:
            return AskResult(intent, slots, "date", question_text("date", language), [])
        if not slots.get("start_time") and "time" not in skipped:
            return AskResult(intent, slots, "time", question_text("time", language), [])
    return None


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
    "phone": "Phone", "age": "Age",
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


def describe_edits(changes):
    return ", ".join("{} -> {}".format(_FIELD_LABELS.get(k, k), v) for k, v in changes.items())
