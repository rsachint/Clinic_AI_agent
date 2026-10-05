import re
import unicodedata

# Deterministic keyword rules, not the LLM (plan §5: intent classification is
# rule-based; only slot extraction for free-text fields uses the small model,
# plus -- as of this session -- a second, still-non-slot-touching stage: see
# clinic/nlu/intent_llm.py, tried only when every rule below misses). First
# matching rule wins. Keyword lists are English/Hinglish/Devanagari variants
# seen (or expected) in Saaras transcripts; expand from the real
# command-accuracy corpus called for in the plan's §11 verification section.
_RULES = [
    ("missed_followups", ["nahi aaya", "नहीं आया", "missed", "worklist"]),
    ("day_end_cashbook", ["hisaab", "हिसाब", "cashbook", "cash book", "account", "today's account"]),
    # cancel_followup / reschedule_followup are checked BEFORE set_followup,
    # even though all three share "follow up"-ish vocabulary: set_followup's
    # own keyword list includes the bare phrase "follow up" (Hindi "फॉलो
    # अप"), and a real cancel/reschedule command almost always still
    # mentions "follow up" too ("Sunita ka follow up cancel karo") -- if
    # set_followup were checked first, its generic "follow up" keyword would
    # win that race and a cancel/reschedule command would misfire as a new
    # set_followup. (Confirmed live while adding these rules.)
    #
    # Keywords here are deliberately bare ("cancel", "reschedule", ...), same
    # register as classify_patient.py's WhatsApp equivalents (adapted here
    # for staff phrasing, not copied verbatim -- see that file's own comment
    # on why the two registers stay separate modules). Bare keywords mean
    # these two rules would also catch "cancel my appointment"/"reschedule
    # my appointment" -- that collision is resolved *before* this list is
    # ever reached, by _classify_appointment_related() below, which is
    # checked first in classify() and only claims a command when it
    # recognizes explicit appointment/booking/slot/time-of-day language. See
    # that function's own comment for exactly how much of the ambiguity
    # that does and doesn't resolve. confirm_followup deliberately has NO
    # voice keyword here (and never should, without a product decision --
    # see clinic/intents.py / plan §13.1: whether a confirmation should
    # write anything at all is an open question, not something to resolve
    # unilaterally here).
    ("cancel_followup", [
        "cancel follow up", "cancel followup", "cancel the follow up", "cancel my follow up",
        "cancel karo", "फॉलो अप कैंसिल", "फॉलो-अप कैंसिल",
        "cancel", "कैंसिल", "रद्द",
    ]),
    ("reschedule_followup", [
        "reschedule follow up", "reschedule followup", "reschedule the follow up",
        "फॉलो अप रीशेड्यूल", "reschedule", "रीशेड्यूल", "postpone", "date badlo", "दूसरे दिन",
        "किसी और दिन", "another day", "change the date",
    ]),
    ("set_followup", ["din baad", "दिन बाद", "follow up", "followup", "फॉलो अप", "bulao", "बुलाओ"]),
    ("log_attendance", [
        "present hai", "प्रेजेंट है", "प्रेजेंट", "उपस्थित",
        "half day", "हाफ डे", "absent", "अनुपस्थित", "chutti", "छुट्टी",
    ]),
    ("log_expense", ["diye", "दिए", "kharch", "खर्च", "expense", "paid to"]),
    ("record_visit", ["consultation", "कंसल्टेशन", "visit", "फीस", "fee", "rupee", "रुपए", "रुपये"]),
    # Checked before register_patient: both share the bare word "register",
    # so the more specific "staff" phrasing must win that race or a staff
    # registration would always misclassify as a patient one.
    ("register_staff", ["naya staff", "नया स्टाफ", "register staff", "add staff", "staff ko register"]),
    ("register_patient", ["naya patient", "नया पेशेंट", "नया मरीज", "new patient", "register"]),
    # Checked last, deliberately: "mobile/phone number" is generic enough to
    # appear inside a registration command too ("...his mobile number is...")
    # -- the more specific write-intent keywords above must win that race, or
    # a real registration silently misfires as a lookup for a patient who
    # doesn't exist yet (seen live: "register a new patient... mobile number
    # is" was misclassified as patient_lookup before this reordering).
    ("patient_lookup", [
        "phone number", "फोन नंबर", "mobile number", "मोबाइल नंबर",
        "number bata", "नंबर बता", "number kya hai", "नंबर क्या है",
        "patient details", "patient detail", "patient record", "patient records",
        "retrieve the patient", "retrieve patient", "look up patient", "lookup patient",
        "मरीज की जानकारी", "पेशेंट की जानकारी", "मरीज का रिकॉर्ड",
    ]),
]


def _normalize(text):
    return unicodedata.normalize("NFC", text).lower()


def _any(normalized, words):
    return any(_normalize(w) in normalized for w in words)


# ---------------------------------------------------------------------------
# Day-of queue commands (checked FIRST in classify(), before everything else)
# ---------------------------------------------------------------------------
#
# queue_check_in / queue_call_next / queue_mark_done / queue_mark_no_show
# (writes, always behind a review card) and queue_status (read-only).
#
# These share vocabulary with rules further down, so they are deliberately
# conservative: a phrase is only claimed here when it cannot plausibly be
# one of the older intents. Collisions checked (each has a regression test in
# tests/test_nlu.py):
#   - "present hai" / "प्रेजेंट" stay log_attendance. "present" only means
#     check-in when the word "token" is also in the sentence.
#   - "nahi aaya" / "नहीं आया" stay missed_followups ("kaun nahi aaya").
#     They mean no-show only alongside "token". And every arrival phrase is
#     negation-guarded, so "token 5 abhi tak nahi aaya" is never a check-in.
#   - "bulao" stays set_followup ("Sunita ko 7 din baad bulao"). A call-in
#     needs "token", a "next patient"-style phrase, or "andar bhejo/bulao".
#   - "consultation", "visit", "fee", "rupees" stay record_visit: a "done"
#     phrase is dropped whenever the sentence also mentions money.
#   - "next patient" (call) vs "next appointment" (next_appointment) and
#     "who's next" (status) vs "when is my next appointment".
#   - "cancel" / "reschedule" / "book" / "slot" are not queue words at all.
# Honest limitation: "Ramesh aa gaya" (patient arrived) is a check-in even
# without "token"; a *staff* member announced the same way would be
# misrouted -- the review card shows the resolved patient, and "staff" in
# the sentence disables it.

_TOKEN_WORD = re.compile(r"token|टोकन|\bt-\d{1,3}\b")
_MONEY_WORDS = ["fee", "fees", "फीस", "rupee", "rupees", "rupaye", "रुपए", "रुपये", "rs", "₹", "paid", "kharch", "खर्च"]
_NEGATION = re.compile(r"\b(?:not|nahi|nahin|na)\b|n't\b|नहीं|नही|\bnhi\b")

_WHOS_NEXT = re.compile(r"\bwho(?:'s|s| is)?\s+(?:the\s+)?(?:next|waiting|up next)\b|\bkaun\s+(?:next|agla)\b|\bagla\s+kaun\b|अगला\s+कौन|कौन\s+(?:अगला|नेक्स्ट)")
_STATUS_PHRASES = [
    "queue status", "queue mein", "queue me ", "queue kya", "queue में", "क्यू में", "कतार",
    "now serving", "waiting room", "line mein kaun", "kitne patient baaki", "kitne mareez baaki",
    "kitne log baaki", "कितने मरीज बाकी", "कितने पेशेंट बाकी", "how many patients are waiting",
    "how many are waiting", "how many waiting", "who is in the queue", "who's in the queue", "queue",
]

_NO_SHOW_STRONG = ["no show", "no-show", "noshow", "नो शो", "नो-शो", "did not show up", "didn't show up", "show up nahi"]
_NO_SHOW_WITH_TOKEN = [
    "nahi aaya", "nahi aayi", "nahi aaye", "नहीं आया", "नहीं आई", "नहीं आए",
    "absent", "gayab", "गायब", "not come", "did not come", "didn't come", "not here", "not arrived",
]
_DONE_STRONG = [
    "consultation done", "consultation complete", "consultation completed", "consultation over",
    "consultation finished", "consultation ho gaya", "consultation ho gayi", "consultation khatam",
    "कंसल्टेशन हो गया", "कंसल्टेशन पूरा", "कंसल्टेशन खत्म", "कंसल्टेशन डन",
    "mark done", "mark as done", "mark complete", "mark completed", "marked done",
    "patient done", "dikha liya", "dekh liya", "दिखा लिया", "देख लिया", "nipat gaya", "निपट गया",
]
_DONE_WITH_TOKEN = [
    "done", "complete", "completed", "finished", "over", "ho gaya", "ho gayi", "हो गया", "हो गई",
    "khatam", "खत्म", "पूरा", "nipat", "निपट",
]
_CALL_STRONG = [
    "call next", "next patient", "next please", "agla patient", "agla mareez", "agla marij",
    "अगला मरीज", "अगला पेशेंट", "अगले मरीज", "अगले पेशेंट", "andar bhejo", "andar bulao",
    "अंदर भेजो", "अंदर बुलाओ", "send in the next", "send the next", "call in the next",
]
_CALL_WITH_TOKEN = [
    "bulao", "bula lo", "bulaiye", "बुलाओ", "बुला", "call", "andar", "अंदर", "bhejo", "भेजो",
    "send in", "send", "next", "नेक्स्ट",
]
_ARRIVED = [
    "check in", "check-in", "checkin", "checked in", "चेक इन", "चेक-इन", "चेकइन",
    "arrived", "pahunch gaya", "pahunch gayi", "pahunch gaye", "पहुंच गया", "पहुँच गया",
    "पहुंच गई", "पहुँच गई", "पहुंच गए", "पहुँच गए",
    "aa gaya", "aa gayi", "aa gaye", "आ गया", "आ गई", "आ गए", "आ गयी", "आ गये",
]
_ARRIVED_WITH_TOKEN = ["present", "hazir", "हाजिर", "हाज़िर", "reached", "here"]


def _phrase_in(normalized, phrase):
    """ASCII phrases must match on word boundaries ("over" must not fire
    inside "recover"); anything with Devanagari matches as a substring."""
    p = _normalize(phrase)
    if p.isascii():
        return re.search(r"(?<![a-z])" + re.escape(p) + r"(?![a-z])", normalized) is not None
    return p in normalized


def _any_phrase(normalized, phrases):
    return any(_phrase_in(normalized, p) for p in phrases)


def _classify_queue_related(normalized):
    has_token = bool(_TOKEN_WORD.search(normalized))
    has_money = _any_phrase(normalized, _MONEY_WORDS)

    if _WHOS_NEXT.search(normalized):
        return "queue_status"

    if _any_phrase(normalized, _NO_SHOW_STRONG) or (has_token and _any_phrase(normalized, _NO_SHOW_WITH_TOKEN)):
        return "queue_mark_no_show"

    if not has_money and (
        _any_phrase(normalized, _DONE_STRONG) or (has_token and _any_phrase(normalized, _DONE_WITH_TOKEN))
    ):
        return "queue_mark_done"

    if _any_phrase(normalized, _CALL_STRONG) or (has_token and _any_phrase(normalized, _CALL_WITH_TOKEN)):
        return "queue_call_next"

    if not _NEGATION.search(normalized) and "staff" not in normalized and (
        _any_phrase(normalized, _ARRIVED) or (has_token and _any_phrase(normalized, _ARRIVED_WITH_TOKEN))
    ):
        return "queue_check_in"

    if _any_phrase(normalized, _STATUS_PHRASES):
        return "queue_status"

    return None


# ---------------------------------------------------------------------------
# Appointment vs. follow-up disambiguation (checked before every rule above)
# ---------------------------------------------------------------------------
#
# "cancel my appointment" and "cancel my follow-up" can sound nearly
# identical after ASR, and cancel_followup/reschedule_followup above use
# deliberately bare keywords ("cancel", "reschedule") to catch the many ways
# a follow-up cancellation gets phrased. To keep a bare "cancel"/"reschedule"
# defaulting to the follow-up intents (the older, more common voice command)
# rather than always guessing appointment, the *appointment* intents below
# are checked FIRST, and only fire when the text also contains explicit
# appointment/booking/slot vocabulary, or a recognizable time-of-day phrase
# (an hour + baje/am/pm, or a सुबह/दोपहर/शाम/रात-style qualifier), or (for
# availability specifically) a bare date word.
#
# This is a real, only-partially-solved ambiguity, not a fully clean split:
# a caller who cancels an appointment without ever saying "appointment",
# "booking", "slot", or any time (e.g. just "cancel that thing for Monday")
# is NOT caught here and falls through to cancel_followup below, wrongly.
# Expand the word lists below from real transcripts, the same way this
# file's other collision comments describe.
_APPT_NOUN = ["appointment", "अपॉइंटमेंट", "booking", "बुकिंग", "slot", "स्लॉट"]
_CANCEL_WORDS = ["cancel", "कैंसिल", "रद्द"]
_RESCHEDULE_WORDS = ["reschedule", "रीशेड्यूल", "postpone", "date badlo", "दूसरे दिन", "किसी और दिन", "shift karo"]
# "move Amit to Branch B at 3", "shift his appointment": whole words only ("remove" is not "move").
_MOVE_WORD = re.compile(r"(?<![a-z])(?:move|moved|transfer|transferred|shift|shifted)(?![a-z])|शिफ्ट|ट्रांसफर")
_AVAILABILITY_WORDS = ["free", "available", "उपलब्ध", "khaali", "खाली", "khali", "vacant"]
_DATE_WORDS = ["kal", "आज", "aaj", "today", "tomorrow"]
_NEXT_APPOINTMENT_PHRASES = [
    "next appointment", "agla appointment", "अगली अपॉइंटमेंट", "अगला अपॉइंटमेंट",
    "kab hai appointment", "appointment kab hai", "when is the appointment", "when is my appointment",
    "when is her appointment", "when is his appointment",
]
_SCHEDULE_OVERVIEW_PHRASES = [
    "today's schedule", "todays schedule", "this week's schedule", "this week schedule",
    "aaj ka schedule", "is hafte ka schedule", "what's scheduled", "whats scheduled",
    "who's scheduled", "whos scheduled", "appointments today", "appointments this week",
    "आज का शेड्यूल", "इस हफ्ते का शेड्यूल",
]
_BOOK_PHRASES = [
    "book appointment", "book an appointment", "booking", "अपॉइंटमेंट बुक",
    "बुकिंग", "schedule appointment", "fix appointment", "अपॉइंटमेंट चाहिए", "appointment chahiye",
]

# Asking to SEE appointments ("get all the appointments for tomorrow") is a
# read. Without this, the appointment-noun catch-all below read it as a booking.
_READ_VERBS = [
    "get", "fetch", "show", "list", "display", "pull up", "view", "see all",
    "dikhao", "dikha do", "dikhana", "batao", "bata do", "बताओ", "दिखाओ", "दिखा दो",
    "how many", "kitne", "कितने", "which appointments", "what appointments",
    "who is coming", "who all", "saare", "all appointments", "all the appointments",
    "what are", "what is", "what's", "whats", "what do we have", "do we have", "kya hain", "kya hai",
]
_WRITE_BOOK_PHRASES = [p for p in _BOOK_PHRASES if p not in ("booking", "बुकिंग")]

_SAME_DAY_WORDS = ["same day", "that day", "same date", "usi din", "उसी दिन", "उसी तारीख"]
_BOOK_VERB_WORDS = ["book ", "fix ", "बुक", "फिक्स"]
_AT_HOUR = re.compile(r"(?<![\w])(?:at|around|by)\s+\d{1,2}(?![\w:./-])")

_TIME_HINT = re.compile(
    r"\d{1,2}\s*(?::\d{2})?\s*(?:am|pm|baje|बजे)"
    r"|सुबह|दोपहर|शाम|रात|subah|dopahar|shaam|sham|raat",
    re.IGNORECASE,
)


def _has_time_hint(normalized):
    return bool(_TIME_HINT.search(normalized))


def _has_appt_noun(normalized):
    return _any(normalized, _APPT_NOUN)


# "How many patients have been registered so far?", "total patients", "kitne patients hain":
# a COUNT of patient records. Must beat the bare "register" keyword (the word "registered"
# is in the question), so it is decided by this precise rule before the keyword list is read,
# and the parser trusts it over the model. Queue and appointment questions ("how many
# patients are waiting", "how many appointments") are not claimed.
_COUNT_WORD = re.compile(
    r"how\s+many|(?<![a-z])(?:total|count|number\s+of|kitne|kitni|kul)(?![a-z])|कितने|कितनी|कुल|संख्या")
_PATIENT_NOUN = re.compile(r"(?<![a-z])(?:patients?|mareez|marij|patient\s+records)(?![a-z])|मरीज|पेशेंट|रोगी")
_NOT_A_PATIENT_COUNT = re.compile(
    r"waiting|queue|appointment|booked|booking|slot|token|follow\s*up|visit|consultation|fee|today's\s+cash|expense"
    r"|इंतज़ार|इंतजार|कतार|अपॉइंटमेंट|फॉलो|फीस|खर्च|intezaar|intezar|baaki|baki|बाकी|aaye|आए|आये")


def is_patient_count(text):
    normalized = _normalize(text)
    return bool(_COUNT_WORD.search(normalized) and _PATIENT_NOUN.search(normalized)
                and not _NOT_A_PATIENT_COUNT.search(normalized))


def is_move_command(text):
    """"move Amit to Branch B at 3", "Amit ko shift karo kal 11 baje": an explicit
    move/shift word AND the keyword rules read it as a reschedule. Precise
    enough to route without asking the model (which reads "move" as a booking
    and "shift karo" as attendance)."""
    normalized = _normalize(text)
    return (bool(_MOVE_WORD.search(normalized)) or "shift karo" in normalized) \
        and _classify_appointment_related(normalized) == "reschedule_appointment"


def _classify_appointment_related(normalized):
    is_appt_context = _has_appt_noun(normalized) or _has_time_hint(normalized)

    cancel_word = _any(normalized, _CANCEL_WORDS)
    if cancel_word and is_appt_context:
        return "cancel_appointment"

    reschedule_word = _any(normalized, _RESCHEDULE_WORDS) or bool(_MOVE_WORD.search(normalized))
    if reschedule_word and is_appt_context:
        return "reschedule_appointment"

    if _any(normalized, _AVAILABILITY_WORDS) and (
        is_appt_context or _any(normalized, _DATE_WORDS)
    ):
        return "check_availability"

    if _any(normalized, _NEXT_APPOINTMENT_PHRASES):
        return "next_appointment"

    if _any(normalized, _SCHEDULE_OVERVIEW_PHRASES):
        return "list_appointments"

    if (_has_appt_noun(normalized) and _any(normalized, _READ_VERBS)
            and not _any(normalized, _WRITE_BOOK_PHRASES) and not _CALENDAR_WORD.search(normalized)):
        return "list_appointments"

    if _any(normalized, _BOOK_PHRASES) or (_has_appt_noun(normalized) and not (cancel_word or reschedule_word)):
        return "book_appointment"

    # "book Sunita tomorrow at 5" -- a booking verb plus a day or a time, even
    # without the word "appointment".
    if (not (cancel_word or reschedule_word) and _any(normalized, _BOOK_VERB_WORDS)
            and (_has_time_hint(normalized) or _any(normalized, _DATE_WORDS + _SAME_DAY_WORDS)
                 or _AT_HOUR.search(normalized))):
        return "book_appointment"

    return None


# Fallback for patient_lookup only, checked after every _RULES entry above
# (including patient_lookup's own fixed-phrase list). "Look up a patient's
# info" keeps arriving in new phrasings that don't share an exact substring
# with each other (seen live: "...patient details...", then separately
# "...retrieve the information about the patient...") -- rather than keep
# enumerating exact phrases one at a time, treat any mention of "patient"
# alongside a lookup-ish word as a lookup. This is still deterministic
# keyword matching, not the LLM, and it's safe to check last because every
# write intent's more specific keywords above already win that race first
# (e.g. a registration mentioning "patient" is caught by register_patient
# long before reaching this fallback).
_PATIENT_WORD = ["patient", "मरीज", "पेशेंट"]
_LOOKUP_WORD = [
    "retrieve", "information", "details", "detail", "record", "records",
    "look up", "lookup", "pull up", "find", "phone number", "फोन नंबर",
    "mobile number", "मोबाइल नंबर", "जानकारी", "रिकॉर्ड",
]


def _classify_legacy(normalized):
    """Every rule that existed before calendar navigation: unchanged."""
    queue_intent = _classify_queue_related(normalized)
    if queue_intent:
        return queue_intent

    appt_intent = _classify_appointment_related(normalized)
    if appt_intent:
        return appt_intent

    if is_patient_count(normalized):
        return "patient_count"

    for intent, keywords in _RULES:
        for kw in keywords:
            if _normalize(kw) in normalized:
                return intent
    if _any(normalized, _PATIENT_WORD) and _any(normalized, _LOOKUP_WORD):
        return "patient_lookup"
    return None


# ---------------------------------------------------------------------------
# Calendar navigation ("open calendar", "kal ka calendar", "show month view")
# ---------------------------------------------------------------------------
#
# open_calendar is READ-ONLY navigation: the dashboard switches to the
# Appointments tab (and to week / month / agenda). It writes nothing and
# never needs a review card. Counting / answering questions about the
# schedule stay with the existing read intents against our own database --
# nothing is ever routed to Google.
#
# It is decided AFTER the legacy rules have classified the sentence, and may
# only claim a sentence the legacy rules left alone (None) or classified as
# book_appointment purely because it contains the bare noun "appointment" /
# "slot" (see _classify_appointment_related's last branch). Every other
# legacy verdict wins, so nothing that used to be a write, a queue command, an
# availability / schedule / next-appointment read, or a lookup can change.
# Collisions checked (each has a regression test in tests/test_calendar_voice.py):
#   - "book / cancel / reschedule ... in the calendar" keep their write
#     intents (explicit write words block navigation, and cancel/reschedule
#     are never None/book_appointment anyway);
#   - "show appointments on the calendar" -> navigation (legacy would say
#     book_appointment from the noun alone), but a bare "appointment calendar"
#     with no display verb is left to the legacy rule;
#   - "how many appointments on the calendar tomorrow", "who is on the
#     calendar" -> counting / who-questions stay with the legacy rules (DB);
#   - "today's schedule" / "this week's schedule" -> list_appointments,
#     "free slots tomorrow" -> check_availability, "who's next" -> queue_status,
#     "next appointment" -> next_appointment: still the legacy read intents
#     even when the sentence also says "calendar" or "view".
_CALENDAR_WORD = re.compile(
    r"(?<![a-z])(?:calendar|calender|calandar|kalendar)s?(?![a-z])"
    r"|कैलेंडर|कैलेन्डर|कैलंडर|कलेंडर|केलेंडर|कैलेण्डर"
)
_VIEW_WORD = re.compile(r"(?<![a-z])views?(?![a-z])|व्यू")
_VIEW_MODE_WORD = re.compile(
    r"(?<![a-z])(?:month|monthly|week|weekly|agenda|day|daily|mahina|mahine|hafta|hafte)(?![a-z])"
    r"|मंथ|वीक|एजेंडा|डे|महीन|हफ्त|हफ़्त|सप्ताह"
)
_DISPLAY_VERB = re.compile(
    r"(?<![a-z])(?:open|show|display|see|view|go\s+to|switch|pull\s+up|dikha\w*|dekh\w*|khol\w*)(?![a-z])"
    r"|दिखा|खोल|देख"
)
_COUNT_OR_WHO_QUESTION = re.compile(
    r"how\s+many|how\s+much|(?<![a-z])(?:count|number\s+of|who|kitne|kitni|kitna|kaun)(?![a-z])"
    r"|कितने|कितनी|कितना|कौन"
)
_WRITE_VERB = re.compile(
    r"(?<![a-z])(?:book|booking|cancel|reschedule|postpone|register|add|create|delete|remove|move|shift"
    r"|daal\w*|dal[oa]|jodo|jod|likho|banao|lagao|rakho)(?![a-z])"
    r"|बुक|कैंसिल|रद्द|रीशेड्यूल|रजिस्टर|डालो|डाल|जोड़|लिखो|बनाओ|लगाओ|रखो"
)

_MODE_AGENDA = re.compile(r"(?<![a-z])(?:agenda|list)(?![a-z])|एजेंडा|लिस्ट")
_MODE_MONTH = re.compile(r"(?<![a-z])(?:month|monthly|mahina|mahine)(?![a-z])|मंथ|महीन")
_MODE_WEEK = re.compile(r"(?<![a-z])(?:week|weekly|weeks|hafta|hafte)(?![a-z])|वीक|हफ्त|हफ़्त|सप्ताह")
_MODE_DAY = re.compile(
    r"(?<![a-z])(?:day|daily|today|tomorrow|kal|aaj|parson|din|roz)(?![a-z])|आज|कल|परसों|दिन|डे|रोज"
)


def _is_calendar_navigation(normalized, legacy):
    if legacy not in (None, "book_appointment"):
        return False
    has_calendar = bool(_CALENDAR_WORD.search(normalized))
    has_view = bool(_VIEW_WORD.search(normalized) and _VIEW_MODE_WORD.search(normalized))
    if not (has_calendar or has_view):
        return False
    if _COUNT_OR_WHO_QUESTION.search(normalized):
        return False
    if _WRITE_VERB.search(normalized) or _any(normalized, _BOOK_PHRASES):
        return False
    if legacy == "book_appointment" and not _DISPLAY_VERB.search(normalized):
        return False  # only the bare "appointment" noun matched: need a clear "show / open"
    return True


def calendar_mode(text):
    """Which embed view a calendar command asked for: 'agenda', 'month',
    'week' or None (not stated). "day" / "today" / "tomorrow" map to agenda:
    Google's embed has no day view."""
    normalized = _normalize(text)
    for mode, pattern in (("agenda", _MODE_AGENDA), ("month", _MODE_MONTH), ("week", _MODE_WEEK), ("agenda", _MODE_DAY)):
        if pattern.search(normalized):
            return mode
    return None


def classify(text):
    normalized = _normalize(text)
    legacy = _classify_legacy(normalized)
    if _is_calendar_navigation(normalized, legacy):
        return "open_calendar"
    return legacy
