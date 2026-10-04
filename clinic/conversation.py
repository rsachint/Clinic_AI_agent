"""WhatsApp conversational agent for patients: a deterministic, multi-turn
dialog manager that COLLECTS the details of a booking / reschedule /
cancellation and answers read-only token questions.

What this module is -- and is not
---------------------------------
* It never writes an appointment ITSELF. The only tables it writes are
  `wa_sessions` (conversation memory) and `slot_holds` (a slot reserved for
  the patient who just confirmed a request that went to staff). When a
  request is complete it hands it to the `auto` callable (see
  clinic/auto_actions.py): if the schedule checks of clinic/auto_policy.py all
  pass, THAT code commits it through core.propose + core.confirm (adapter
  seam, immutable audit_log, confirm-time double-booking guard) and the
  confirmation the patient gets is the normal booking / cancel / reschedule
  notification. If a check fails -- or `auto` is None, as in the pure unit
  tests -- it returns a `Handoff` -- (intent, slots, patient_id) -- which the
  caller stores as an ordinary `classified` inbox row, with the reason, and a
  human taps Approve in the dashboard exactly as before. If the slot was
  taken (or blocked) in the instant before the commit, the patient is shown
  fresh free times instead.
* Every reply is a fixed template (clinic/conv_templates.py) with dates,
  times and names inserted by code. No model writes any text.
* No model decides a date, a time, a name, a number or a write. Dates and
  times come from clinic/nlu/datetime_extract.py (plus a few extra
  deterministic phrases below); the patient is identified by the sender's
  phone (entity_resolution.resolve_patient_by_phone). The one optional use
  of the local LLM is `intent_picker`: it may pick ONE label from a closed
  list for a message the keyword rules could not place, and any failure
  means "unclear".
* No network and no wall clock in here: `handle_inbound` takes the clock
  (`now`, the clinic's local naive datetime) and the intent picker as
  arguments, so the whole thing is unit-testable.

Order per message
-----------------
emergency check -> (human takeover => silent) -> rate limit -> clinical
check -> "talk to a person" -> a tapped choice, or the answer to the
pending question (date / time / name / yes-no), or a deterministic intent
(clinic/nlu/classify_patient.py + the extra keyword sets below) -> only if
nothing matched and no question is pending, the closed-enum LLM picker.
"""

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from clinic import conv_templates as ct
from clinic import notify, scheduling, whatsapp
from clinic.entity_resolution import last10_digits, resolve_patient_by_phone
from clinic.nlu import classify_patient
from clinic.nlu.datetime_extract import extract_appt_date, extract_appt_time
from clinic.whatsapp_pipeline import sender_appointments

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
SESSION_IDLE_MINUTES = 30      # goal / collected slots reset after this much silence (mode persists)
HOLD_HOURS = 2                 # a confirmed request holds its slot this long (or until staff resolve it)
MAX_ACTIVE_APPOINTMENTS = 3    # booked/confirmed appointments (+ open requests) per person
RATE_LIMIT_TURNS = 30          # inbound messages per sender per hour before everything goes to staff
RATE_WINDOW_MINUTES = 60
MAX_CONFUSED_TURNS = 2         # the 2nd confused turn escalates to a human
BOOKING_HORIZON_DAYS = 30      # how far ahead a patient can ask for
SLOT_OFFERS = 5                # how many free times are offered at once
DAY_BUTTONS = 3                # how many "pick a day" buttons are offered
AGENT_ENABLED_ENV = "WHATSAPP_AGENT_ENABLED"

GOALS = ("book", "reschedule", "cancel")
_INTENT_TO_GOAL = {"book": "book", "reschedule": "reschedule", "cancel": "cancel"}
_HANDOFF_INTENT = {"book": "book_appointment", "reschedule": "reschedule_appointment", "cancel": "cancel_appointment"}


def agent_enabled(environ=None):
    """WHATSAPP_AGENT_ENABLED: on unless it is exactly "0" (the rollback
    switch that restores the previous single-message behaviour)."""
    import os
    value = (environ if environ is not None else os.environ).get(AGENT_ENABLED_ENV, "1")
    return str(value).strip() != "0"


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class Reply:
    """One outbound message. `buttons` = reply buttons [(id, title)] (max 3);
    `rows` = list-message rows [(id, title)] (max 10) with `list_button` as the
    label of the button that opens the list. kind='status' means "answer with
    the existing my_status template for `appointment_id`" -- the caller
    enqueues it through notify.notify_status_reply."""
    text: str
    buttons: list = None
    rows: list = None
    list_button: str = None
    kind: str = "message"
    appointment_id: int = None

    def interactive(self):
        """The JSON-able spec stored in notifications.interactive_json and
        turned into a Cloud API body by whatsapp.build_interactive_body."""
        if self.buttons:
            return {"type": "button", "buttons": [{"id": i, "title": t} for i, t in self.buttons]}
        if self.rows:
            return {"type": "list", "button": self.list_button or "Choose",
                    "rows": [{"id": i, "title": t} for i, t in self.rows]}
        return None


@dataclass
class Handoff:
    """A finished request, to be stored as a `classified` inbox proposal."""
    intent: str
    slots: dict
    patient_id: int = None
    note: str = ""


@dataclass
class Result:
    replies: list = field(default_factory=list)
    handoff: Handoff = None
    escalate: str = None       # reason, when a human must reply
    emergency: bool = False
    clinical: bool = False
    silent: bool = False       # staff have taken over: send nothing
    language: str = "en"
    patient_id: int = None
    auto: object = None        # the AutoOutcome, when the request went through the automatic path
    activity: list = field(default_factory=list)   # patient_activity rows to write (clinic/conv_runtime.py)

    @property
    def flag(self):
        """Why this message needs a human, or None. Priority order."""
        if self.emergency:
            return "emergency"
        if self.escalate:
            return "human_mode" if self.escalate == "human_mode" else "escalation"
        if self.clinical:
            return "clinical"
        return None


# ---------------------------------------------------------------------------
# Choice ids: a compact, deterministic scheme shared by the buttons we send
# and the interactive replies we receive.
#   menu:book | menu:reschedule | menu:cancel | menu:status
#   day:YYYY-MM-DD            slot:YYYY-MM-DDTHH:MM
#   appt:<appointment id>     confirm:yes | confirm:no | confirm:change
# ---------------------------------------------------------------------------

_CHOICE = re.compile(
    r"^(?:(menu):(book|reschedule|cancel|status)"
    r"|(day):(\d{4}-\d{2}-\d{2})"
    r"|(slot):(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})"
    r"|(appt):(\d{1,9})"
    r"|(confirm):(yes|no|change))$"
)


def menu_choice(goal):
    return "menu:" + goal


def day_choice(iso_date):
    return "day:" + iso_date


def slot_choice(iso_date, hhmm):
    return "slot:{}T{}".format(iso_date, hhmm)


def appt_choice(appointment_id):
    return "appt:{}".format(appointment_id)


def confirm_choice(answer):
    return "confirm:" + answer


def parse_choice(choice_id):
    """('menu', 'book') / ('day', '2026-10-02') / ('slot', '2026-10-02T09:15')
    / ('appt', '12') / ('confirm', 'yes') -- or None for anything else."""
    if not choice_id:
        return None
    m = _CHOICE.match(str(choice_id).strip())
    if not m:
        return None
    groups = [g for g in m.groups() if g is not None]
    return groups[0], groups[1]


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_PUNCT = re.compile(r"[.,!?;:।'\"()\[\]{}*_~`|]")


def normalize(text):
    t = unicodedata.normalize("NFC", text or "").lower().translate(_DEVANAGARI_DIGITS)
    t = t.replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", t).strip()


def _tokens(norm):
    return _PUNCT.sub(" ", norm).split()


def _compile(patterns):
    """Alternation of patterns over normalized text. ASCII patterns match on
    word boundaries (a trailing '*' allows any suffix); Devanagari patterns
    match as substrings (Python's \\b is unreliable around matras)."""
    parts = []
    for raw in patterns:
        prefix = raw.endswith("*")
        p = normalize(raw.rstrip("*"))
        if p.isascii():
            parts.append(r"(?<![a-z0-9])" + re.escape(p) + ("" if prefix else r"(?![a-z0-9])"))
        else:
            parts.append(re.escape(p))
    return re.compile("|".join(parts))


# Emergencies: fail-safe, deliberately broad. A false positive costs one
# fixed message and one highlighted inbox item; a miss could cost a life.
_EMERGENCY = _compile([
    "chest pain", "chest mein dard", "chest me dard", "chest mai dard", "seene mein dard", "seene me dard",
    "seene mai dard", "heart attack", "can't breathe", "cant breathe", "cannot breathe", "can not breathe",
    "unable to breathe", "difficulty breathing", "difficulty in breathing", "trouble breathing",
    "short of breath", "shortness of breath", "breathless*", "saans nahi", "saans nhi", "saans lene mein",
    "saans phool*", "sans nahi", "dum ghut*", "heavy bleeding", "bleeding heavily", "bleeding a lot",
    "bleeding badly", "won't stop bleeding", "wont stop bleeding", "khoon bahut", "bahut khoon",
    "zyada khoon", "khoon beh*", "khoon aa raha", "unconscious", "passed out", "fainted", "behosh*",
    "not responding", "seizure*", "convulsion*", "mirgi", "daura pad*", "accident", "accidents", "emergency", "ambulance",
    "stroke", "poison*", "zeher", "overdose", "suicide", "suicidal", "want to die", "aatmahatya", "choking",
    "सीने में दर्द", "सीने मे दर्द", "छाती में दर्द", "छाती मे दर्द", "सांस नहीं", "साँस नहीं",
    "सांस लेने में", "साँस लेने में", "सांस फूल", "साँस फूल", "दम घुट", "बहुत खून", "ज्यादा खून",
    "ज़्यादा खून", "खून बह", "खून बहुत", "बेहोश", "बेहोशी", "मिर्गी", "दौरा पड़", "एक्सीडेंट", "दुर्घटना",
    "इमरजेंसी", "आपातकाल", "एम्बुलेंस", "एंबुलेंस", "ज़हर", "जहर", "आत्महत्या", "हार्ट अटैक",
    "दिल का दौरा", "स्ट्रोक",
])

# Clinical questions: symptoms, medicines, doses, prescriptions, results.
_CLINICAL = _compile([
    "fever", "bukhar", "cough*", "khansi", "cold", "sardi", "headache*", "head ache", "sar dard", "sir dard",
    "pet dard", "pet mein dard", "stomach", "vomit*", "ulti", "loose motion*", "diarrhea", "diarrhoea",
    "rash", "allergy", "allergic", "infection", "pain*", "dard", "swelling", "sujan", "symptom*", "medicine*",
    "medication*", "dawai", "dawa", "dawaiyan", "tablet*", "capsule*", "syrup", "dose", "dosage", "prescription*",
    "prescribe*", "side effect*", "report", "reports", "test result*", "results", "blood test*", "x-ray", "xray",
    "scan", "sugar", "diabetes", "blood pressure", "bp", "pregnan*", "dizzy", "dizziness", "chakkar",
    "itching", "khujli", "weakness", "kamzori", "injury", "chot", "fracture", "wound",
    "बुखार", "खांसी", "खाँसी", "सर्दी", "जुकाम", "सिरदर्द", "सिर दर्द", "पेट दर्द", "पेट में दर्द", "दर्द",
    "उल्टी", "दस्त", "चक्कर", "खुजली", "सूजन", "एलर्जी", "इन्फेक्शन", "संक्रमण", "दवा", "दवाई", "गोली",
    "टैबलेट", "खुराक", "डोज़", "डोज", "पर्चा", "प्रिस्क्रिप्शन", "रिपोर्ट", "टेस्ट रिजल्ट", "शुगर", "बीपी",
    "ब्लड प्रेशर", "लक्षण", "कमजोरी", "चोट", "फ्रैक्चर",
]) 
_MG = re.compile(r"\b\d+\s*mg\b")

# "Let me talk to a person".
_HUMAN = _compile([
    "talk to a person", "talk to someone", "talk to a human", "talk to human", "talk to staff",
    "talk to the doctor", "talk to receptionist", "speak to a person", "speak to someone", "speak to a human",
    "speak to human", "speak to staff", "speak with someone", "speak with a person", "talk with someone",
    "real person", "human", "receptionist", "customer care", "customer support", "call me", "call back",
    "callback", "representative", "doctor se baat", "kisi se baat", "insaan se baat", "insan se baat",
    "baat karni hai", "baat karna hai", "baat karwa*", "baat kara*", "staff se baat", "mujhe call",
    "phone karo", "call karo", "इंसान से बात", "किसी से बात", "स्टाफ से बात", "रिसेप्शन", "बात करनी है",
    "बात करना है", "बात कराइए", "फोन करो", "फोन करें", "कॉल करो", "कॉल करें", "डॉक्टर से बात",
])

# Extra keyword sets, checked in this order after clinic/nlu/classify_patient.
_CANCEL = _compile(["cancel*", "रद्द", "कैंसल", "कैंसिल", "radd kar*", "nahi aana", "nahi aa paunga"])
_RESCHEDULE = _compile([
    "reschedule*", "postpone*", "preponed", "change the date", "change my appointment", "change the time",
    "change date", "change time", "change day", "change the day", "change slot", "move my appointment",
    "move the appointment", "shift my appointment", "shift the appointment", "another day", "another time",
    "different day", "different time", "badal*", "date badlo", "time badlo", "doosre din", "dusre din",
    "दूसरे दिन", "किसी और दिन", "रीशेड्यूल", "बदल", "समय बदलें",
])
_STATUS = _compile([
    "token*", "टोकन", "status", "queue", "my turn", "mera turn", "meri baari", "मेरी बारी", "kitne log",
    "kitne patient", "kitne mareez", "how long", "kitna time", "kab tak", "number kab", "waiting",
    "कितने लोग", "कतार",
])
_BOOK = _compile([
    "book*", "appointment*", "अपॉइंटमेंट", "अपोइंटमेंट", "एपॉइंटमेंट", "slot*", "milna", "मिलना",
    "dikhana", "दिखाना", "checkup", "check-up", "check up", "consult*", "see the doctor", "see doctor",
    "visit",
])

_GREETING_CORE = frozenset((
    "hi", "hii", "hiii", "hello", "hellow", "helo", "hey", "heyy", "hlo", "namaste", "namaskar", "namaskaar",
    "pranam", "salaam", "assalamualaikum", "hola", "menu", "help", "start", "options", "option", "good",
    "morning", "afternoon", "evening", "gm", "नमस्ते", "नमस्कार", "प्रणाम", "हाय", "हैलो", "हेलो", "मदद",
    "मेन्यू", "शुरू",
))
_GREETING_FILLER = frozenset((
    "ji", "sir", "madam", "maam", "doctor", "dr", "clinic", "team", "there", "all", "everyone", "please",
    "pls", "kripya", "जी", "सर", "मैडम", "डॉक्टर",
))
_THANKS = frozenset((
    "thanks", "thank", "you", "thankyou", "thx", "ty", "shukriya", "dhanyawad", "dhanyavad", "धन्यवाद",
    "शुक्रिया", "ok", "okay", "k", "kk", "theek", "thik", "hai", "ठीक", "है", "accha", "achha", "acha",
    "अच्छा", "great", "fine", "cool", "noted", "got", "it", "alright", "bye", "goodbye", "ji", "जी", "so", "much",
))
_YES = frozenset((
    "yes", "y", "yeah", "yep", "yup", "ok", "okay", "confirm", "confirmed", "sure", "haan", "han", "ha", "haa",
    "ji", "theek", "thik", "done", "correct", "sahi", "hanji", "haanji", "हाँ", "हां", "हा", "जी", "ठीक",
    "कन्फर्म", "सही",
))
_YES_FILLER = frozenset(("hai", "है", "please", "pls", "karo", "kar", "do", "de", "dijiye", "kijiye"))
_NO = frozenset((
    "no", "n", "nope", "nahi", "nahin", "nai", "na", "change", "badlo", "badal", "badlein", "badle",
    "later", "stop", "wrong", "नहीं", "ना", "बदलें", "बदलो", "बदल", "नही", "गलत",
))


# Roman-script Hindi words that whatsapp.detect_message_language doesn't know
# but that are unmistakable in this conversation ("namaste", "shaam 4").
_EXTRA_HINGLISH = frozenset((
    "namaste", "namaskar", "namaskaar", "pranam", "shaam", "subah", "dopahar", "raat", "karni", "karwa",
    "sakta", "sakti", "kripya", "parso", "parson", "shukrawar", "somwar", "mangalwar", "budhwar", "guruwar",
    "shaniwar", "ravivar", "mareez", "naam", "haanji", "hanji",
))


def _has_letters(norm):
    return any(ch.isalpha() for ch in norm)


def parse_yes_no(text, goal=None):
    """'yes' / 'no' / None for a typed answer to a confirmation question.
    At a cancel confirmation a bare "cancel" means yes; at a booking /
    reschedule confirmation it means no."""
    toks = _tokens(normalize(text))
    if not toks:
        return None
    if toks == ["cancel"]:
        if goal == "cancel":
            return "yes"
        if goal in ("book", "reschedule"):
            return "no"
    if not all(t in _YES or t in _NO or t in _YES_FILLER for t in toks):
        return None
    yes = any(t in _YES for t in toks)
    no = any(t in _NO for t in toks)
    if yes and not no:
        return "yes"
    if no and not yes:
        return "no"
    return None


def deterministic_intent(text):
    """book / reschedule / cancel / status / greeting / register / None --
    from keyword rules only (clinic/nlu/classify_patient.py first, then the
    extra sets above, in the same priority order)."""
    norm = normalize(text)
    if not norm:
        return None
    base = classify_patient.classify(text)
    mapped = {"cancel_followup": "cancel", "reschedule_followup": "reschedule",
              "my_status": "status", "book_appointment": "book"}.get(base)
    if mapped:
        return mapped
    if _CANCEL.search(norm):
        return "cancel"
    if _RESCHEDULE.search(norm):
        return "reschedule"
    if _STATUS.search(norm):
        return "status"
    if _BOOK.search(norm):
        return "book"
    if base == "register_patient":
        return "register"
    toks = _tokens(norm)
    if toks and any(t in _GREETING_CORE for t in toks) and all(t in _GREETING_CORE or t in _GREETING_FILLER for t in toks):
        return "greeting"
    return None


def is_emergency(text):
    return bool(_EMERGENCY.search(normalize(text)))


def is_clinical_question(text):
    norm = normalize(text)
    return bool(_CLINICAL.search(norm) or _MG.search(norm))


def asks_for_human(text):
    return bool(_HUMAN.search(normalize(text)))


def _is_thanks(norm):
    toks = _tokens(norm)
    return bool(toks) and all(t in _THANKS for t in toks)


# --- names -------------------------------------------------------------------

_NAME_PREFIX = re.compile(
    r"^(?:my name is|my name's|name is|i am|i'm|im|this is|it is|its|mera naam|mera name|naam|name|"
    r"मेरा नाम|नाम|मैं|मै|main|mai)\s+", re.IGNORECASE)
_NAME_SUFFIX = re.compile(r"\s+(?:hai|hain|hoon|hun|है|हैं|हूँ|हूं)$", re.IGNORECASE)


def parse_name(text):
    """A short reply with no digits is taken as the patient's name (shown
    editable on the staff card). None if it doesn't look like one."""
    t = (text or "").strip()
    if not t or any(ch.isdigit() for ch in t):
        return None
    for _ in range(2):
        t = _NAME_PREFIX.sub("", t).strip()
    t = _NAME_SUFFIX.sub("", t).strip()
    t = re.sub(r"[.,!?;:।]+$", "", t).strip()
    words = t.split()
    if not 1 <= len(words) <= 4 or len(t) > 40:
        return None
    for ch in t:
        if not (ch.isalpha() or ch in " .'-" or unicodedata.category(ch).startswith("M")):
            return None
    norm_tokens = _tokens(normalize(t))
    reserved = _YES | _NO | _THANKS | _GREETING_CORE | _GREETING_FILLER
    if all(tok in reserved for tok in norm_tokens):
        return None
    if deterministic_intent(t) or asks_for_human(t) or is_clinical_question(t):
        return None
    if t.isascii() and (t.islower() or t.isupper()):
        t = t.title()
    return t


# --- dates and times -----------------------------------------------------------

_HINDI_MONTHS = {
    "जनवरी": 1, "फरवरी": 2, "फ़रवरी": 2, "मार्च": 3, "अप्रैल": 4, "अप्रेल": 4, "मई": 5, "जून": 6, "जुलाई": 7,
    "अगस्त": 8, "सितंबर": 9, "सितम्बर": 9, "अक्टूबर": 10, "अक्तूबर": 10, "नवंबर": 11, "नवम्बर": 11,
    "दिसंबर": 12, "दिसम्बर": 12,
}
_HINDI_MONTHS = {unicodedata.normalize("NFC", k): v for k, v in _HINDI_MONTHS.items()}
_HINDI_DAY_MONTH = re.compile(r"(\d{1,2})\s*(" + "|".join(sorted(_HINDI_MONTHS, key=len, reverse=True)) + ")")
_DAY_AFTER = re.compile(r"day after tomorrow|(?<![a-z])parso(?![a-z])|(?<![a-z])parson(?![a-z])|परसों|परसो")
_BARE_NUMBER = re.compile(r"^\s*(\d{1,2})\s*(?:tarikh|tareekh|तारीख)?\s*$")


def parse_day(text, today, allow_bare_day_of_month=False):
    """'YYYY-MM-DD' for a day reference in `text`, or None. Deterministic:
    clinic/nlu/datetime_extract.extract_appt_date plus 'day after tomorrow',
    Devanagari month names and (at a day question only) a bare day-of-month."""
    norm = normalize(text)
    if not norm:
        return None
    if _DAY_AFTER.search(norm):
        return (today + timedelta(days=2)).isoformat()
    m = _HINDI_DAY_MONTH.search(norm)
    if m:
        day, month = int(m.group(1)), _HINDI_MONTHS[m.group(2)]
        try:
            candidate = date(today.year, month, day)
            if candidate < today:
                candidate = date(today.year + 1, month, day)
            return candidate.isoformat()
        except ValueError:
            return None
    found = extract_appt_date(norm, today=today)
    if found:
        return found
    if allow_bare_day_of_month:
        m = _BARE_NUMBER.match(norm)
        if m and 1 <= int(m.group(1)) <= 31:
            day = int(m.group(1))
            for months_ahead in (0, 1, 2):
                month = (today.month - 1 + months_ahead) % 12 + 1
                year = today.year + (today.month - 1 + months_ahead) // 12
                try:
                    candidate = date(year, month, day)
                except ValueError:
                    continue
                if candidate >= today:
                    return candidate.isoformat()
    return None


def _slot_starts():
    """Every bookable slot start in a clinic day (the scheduling grid)."""
    starts = []
    for shift_start, shift_end in scheduling.CLINIC_HOURS:
        t = scheduling._to_minutes(shift_start)
        end = scheduling._to_minutes(shift_end)
        while t + scheduling.SLOT_MINUTES <= end:
            starts.append(scheduling._from_minutes(t))
            t += scheduling.SLOT_MINUTES
    return starts


def _in_hours(hhmm):
    t = scheduling._to_minutes(hhmm)
    return any(
        scheduling._to_minutes(a) <= t and t + scheduling.SLOT_MINUTES <= scheduling._to_minutes(b)
        for a, b in scheduling.CLINIC_HOURS
    )


def parse_time(text, allow_bare_hour=False):
    """'HH:MM' for a time reference, or None. datetime_extract.extract_appt_time
    first; at a time question only, a bare hour ("4") counts if exactly one of
    its AM/PM readings falls inside clinic hours."""
    norm = normalize(text)
    found = extract_appt_time(norm)
    if found:
        return found
    if allow_bare_hour:
        m = re.match(r"^\s*(\d{1,2})\s*(?:o'?clock|ko|tak)?\s*$", norm)
        if m:
            hour = int(m.group(1))
            if 1 <= hour <= 12:
                options = [h for h in {hour, (hour % 12) + 12 if hour != 12 else 12} if _in_hours("{:02d}:00".format(h))]
                if len(options) == 1:
                    return "{:02d}:00".format(options[0])
                return "{:02d}:00".format(hour)
            if 13 <= hour <= 23:
                return "{:02d}:00".format(hour)
    return None


def _spread(items, n):
    """Up to n items spread across the list (first and last included)."""
    if len(items) <= n:
        return list(items)
    picks = sorted({round(i * (len(items) - 1) / (n - 1)) for i in range(n)})
    return [items[i] for i in picks]


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

_TS = "%Y-%m-%d %H:%M:%S"


def _ts(dt):
    return dt.strftime(_TS)


def _parse_ts(text):
    return datetime.strptime(text[:19], _TS) if text else None


def _blank_session(wa_id):
    return {"wa_id": wa_id, "goal": None, "step": None, "slots": {}, "language": None, "mode": "agent",
            "confusion_count": 0, "turn_count": 0, "rate_window_start": None, "expired_goal": None}


def load_session(conn, wa_id, now):
    """The sender's session, with an idle one (>= 30 min) reset: goal, step,
    slots and confusion are cleared; mode, language and the rate counter
    persist. `expired_goal` records what was lost, for the "timed out" reply."""
    s = _blank_session(wa_id)
    row = conn.execute("SELECT * FROM wa_sessions WHERE wa_id = ?", (wa_id,)).fetchone()
    if row is None:
        return s
    s.update(goal=row["goal"], step=row["step"], language=row["language"], mode=row["mode"] or "agent",
             confusion_count=row["confusion_count"] or 0, turn_count=row["turn_count"] or 0,
             rate_window_start=row["rate_window_start"])
    try:
        s["slots"] = json.loads(row["slots_json"] or "{}")
    except ValueError:
        s["slots"] = {}
    expires = _parse_ts(row["expires_at"])
    if expires is not None and now >= expires:
        s["expired_goal"] = row["goal"]
        s.update(goal=None, step=None, slots={}, confusion_count=0)
    return s


def save_session(conn, s, now):
    conn.execute(
        "INSERT INTO wa_sessions (wa_id, goal, step, slots_json, language, mode, confusion_count, turn_count, "
        "rate_window_start, updated_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(wa_id) DO UPDATE SET goal=excluded.goal, step=excluded.step, slots_json=excluded.slots_json, "
        "language=excluded.language, mode=excluded.mode, confusion_count=excluded.confusion_count, "
        "turn_count=excluded.turn_count, rate_window_start=excluded.rate_window_start, "
        "updated_at=excluded.updated_at, expires_at=excluded.expires_at",
        (s["wa_id"], s["goal"], s["step"], json.dumps(s["slots"], ensure_ascii=False), s["language"], s["mode"],
         s["confusion_count"], s["turn_count"], s["rate_window_start"], _ts(now),
         _ts(now + timedelta(minutes=SESSION_IDLE_MINUTES))),
    )
    conn.commit()


def get_mode(conn, wa_id):
    row = conn.execute("SELECT mode FROM wa_sessions WHERE wa_id = ?", (wa_id,)).fetchone()
    return row["mode"] if row else "agent"


def set_mode(conn, wa_id, mode, now=None):
    """Staff Take over / Resume agent. Either way the half-finished flow is
    dropped so the agent never resumes a stale question."""
    if mode not in ("agent", "human"):
        raise ValueError("mode must be 'agent' or 'human'")
    now = now or datetime.now()
    s = load_session(conn, wa_id, now)
    s.update(mode=mode, goal=None, step=None, slots={}, confusion_count=0)
    save_session(conn, s, now)


# ---------------------------------------------------------------------------
# Slot holds
# ---------------------------------------------------------------------------

def purge_expired_holds(conn, now):
    conn.execute("DELETE FROM slot_holds WHERE expires_at <= ?", (_ts(now),))


def create_hold(conn, wa_id, appt_date, start_time, now, wa_message_id=None):
    conn.execute(
        "INSERT INTO slot_holds (wa_id, wa_message_id, appt_date, start_time, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (wa_id, wa_message_id, appt_date, start_time, _ts(now), _ts(now + timedelta(hours=HOLD_HOURS))),
    )


def release_holds(conn, wa_message_id):
    """Staff resolved (approved / rejected / dismissed) the inbox item that
    holds were taken for."""
    if wa_message_id is None:
        return 0
    cur = conn.execute("DELETE FROM slot_holds WHERE wa_message_id = ?", (wa_message_id,))
    conn.commit()
    return cur.rowcount


def held_times(conn, appt_date, now, exclude_wa_id=None):
    """Start times on `appt_date` held for someone OTHER than `exclude_wa_id`."""
    mine = last10_digits(exclude_wa_id) if exclude_wa_id else None
    held = set()
    for row in conn.execute(
        "SELECT wa_id, start_time FROM slot_holds WHERE appt_date = ? AND expires_at > ?", (appt_date, _ts(now))
    ).fetchall():
        own = exclude_wa_id is not None and (row["wa_id"] == exclude_wa_id or last10_digits(row["wa_id"]) == mine)
        if not own:
            held.add(row["start_time"])
    return held


def free_times(conn, appt_date, now, for_wa_id=None):
    """The free slot start times that may be OFFERED to `for_wa_id` on
    `appt_date`: not booked, not already past today, not held for someone
    else. (The confirm-time guard in clinic/intents.py stays the final word.)"""
    slots = scheduling.generate_slots(conn, appt_date)
    if appt_date == now.date().isoformat():
        hhmm = now.strftime("%H:%M")
        slots = [t for t in slots if t > hhmm]
    held = held_times(conn, appt_date, now, exclude_wa_id=for_wa_id)
    return [t for t in slots if t not in held]


# ---------------------------------------------------------------------------
# The dialog
# ---------------------------------------------------------------------------

class _Turn:
    def __init__(self, conn, wa_id, text, choice, now, msg_id, picker, auto=None):
        self.conn = conn
        self.auto = auto
        self.wa_id = wa_id
        self.text = text or ""
        self.norm = normalize(self.text)
        self.choice = choice
        self.now = now
        self.today = now.date()
        self.msg_id = msg_id
        self.picker = picker
        self.s = load_session(conn, wa_id, now)
        self.result = Result()
        self.patient = resolve_patient_by_phone(conn, wa_id)
        self.patient_id = self.patient.id if self.patient else None
        self._patient_name = None
        self._own_cache = None

    # -- small helpers ---------------------------------------------------------
    @property
    def lang(self):
        return ct.norm_lang(self.s["language"])

    @property
    def slots(self):
        return self.s["slots"]

    def say(self, key, buttons=None, rows=None, list_button=None, **values):
        self.result.replies.append(Reply(ct.text(key, self.lang, **values), buttons, rows, list_button))

    def fdate(self, iso):
        return notify.format_date(iso, self.lang)

    def ftime(self, hhmm):
        return notify.format_time(hhmm, self.lang)

    def btn(self, key):
        return ct.button(key, self.lang)

    def free(self, iso):
        return free_times(self.conn, iso, self.now, for_wa_id=self.wa_id)

    def patient_name(self):
        if self.patient is None:
            return self.slots.get("name")
        if self._patient_name is None:
            row = self.conn.execute("SELECT name FROM patients WHERE id = ?", (self.patient.id,)).fetchone()
            self._patient_name = row["name"] if row else None
        return self._patient_name

    def note_activity(self, event, detail, **meta):
        """Queue a patient_activity row for the caller to write (this module
        stays free of that table)."""
        self.result.activity.append({"event": event, "detail": detail, "meta": meta,
                                     "appointment_id": self.slots.get("appointment_id")})

    def reset_flow(self, step=None):
        self.s.update(goal=None, step=step, slots={}, confusion_count=0)

    # -- language ----------------------------------------------------------------
    def update_language(self):
        s = self.s
        current = s["language"]
        if self.choice or not self.text.strip():
            s["language"] = current or "en"
            return
        detected = whatsapp.detect_message_language(self.text)
        if detected == "en" and any(w in _EXTRA_HINGLISH for w in _tokens(self.norm)):
            detected = "hinglish"
        if detected in ("hi", "hinglish"):
            s["language"] = detected
        elif detected == "en":
            words = re.findall(r"[a-z]+", self.norm)
            # A short ASCII reply ("ok", "Sunita", "9:15") doesn't switch a
            # Hindi/Hinglish conversation to English.
            s["language"] = current if current in ("hi", "hinglish") and len(words) < 3 else "en"
        else:
            s["language"] = current or "en"

    # -- the pipeline ------------------------------------------------------------
    def run(self):
        s = self.s
        purge_expired_holds(self.conn, self.now)
        self.update_language()
        self.tick_rate()
        typed = not self.choice and bool(self.text.strip())

        if typed and is_emergency(self.text):
            # Runs before everything, even a staff takeover or the rate limit.
            self.result.emergency = True
            self.say("emergency")
            return self.finish()

        if s["mode"] == "human":
            self.result.silent = True
            self.result.escalate = "human_mode"
            return self.finish()

        if s["turn_count"] > RATE_LIMIT_TURNS:
            self.result.escalate = "rate_limit"
            if s["turn_count"] == RATE_LIMIT_TURNS + 1:
                self.say("escalate")
            else:
                self.result.silent = True
            return self.finish()

        det = deterministic_intent(self.text) if typed else None
        if typed and is_clinical_question(self.text):
            self.result.clinical = True
            self.say("clinical")
            if det not in GOALS and det != "status":
                return self.finish()
            # A booking request that merely mentions a symptom: the refusal
            # above covers the symptom, the booking carries on.

        if typed and asks_for_human(self.text):
            self.escalate("human_request")
            return self.finish()

        self.dispatch(det)
        return self.finish()

    def tick_rate(self):
        s = self.s
        started = _parse_ts(s["rate_window_start"])
        if started is None or self.now - started >= timedelta(minutes=RATE_WINDOW_MINUTES):
            s["rate_window_start"] = _ts(self.now)
            s["turn_count"] = 0
        s["turn_count"] += 1

    def finish(self):
        self.result.language = self.s["language"] or "en"
        self.result.patient_id = self.patient_id
        save_session(self.conn, self.s, self.now)
        return self.result

    # -- routing -----------------------------------------------------------------
    def dispatch(self, det):
        s = self.s
        if s["expired_goal"] and not self.choice and det is None:
            # They are answering a question from a conversation that timed out.
            s["expired_goal"] = None
            self.say("session_expired", buttons=self.menu_buttons())
            return
        if self.choice:
            return self.on_choice()
        if s["goal"]:
            return self.on_goal_text(det)
        return self.on_idle_text(det)

    def menu_buttons(self):
        return [(menu_choice(g), self.btn(g)) for g in GOALS]

    def show_menu(self, key="menu"):
        self.reset_flow()
        name = None
        if self.patient is not None:
            full = self.patient_name()
            name = full.split()[0] if full else None
        self.say(key, buttons=self.menu_buttons(), greet=ct.greet(self.lang, name))

    # -- idle (no active goal) ---------------------------------------------------
    def on_idle_text(self, det):
        s = self.s
        norm = self.norm
        if not norm:
            return self.confused_idle()
        if det == "greeting":
            return self.show_menu()
        if det == "status":
            return self.answer_status()
        if det in _INTENT_TO_GOAL:
            return self.start_goal(_INTENT_TO_GOAL[det], self.text)
        if det == "register":
            return self.escalate("register")
        if _is_thanks(norm):
            return self.say("thanks")
        if parse_yes_no(self.text) is not None:
            if s["step"] == "submitted":
                return self.say("already_submitted")
            if s["step"] == "done":
                return self.say("already_done")
            return self.confused_idle()
        if not _has_letters(norm):
            return self.confused_idle()     # digits / emoji only: nothing for a model to classify
        label = self.pick_label()
        if label == "greeting":
            return self.show_menu()
        if label == "status":
            return self.answer_status()
        if label in _INTENT_TO_GOAL:
            return self.start_goal(label, self.text)
        if label == "human":
            return self.escalate("human_request")
        return self.confused_idle()

    def pick_label(self):
        """The closed-enum LLM picker; any failure or surprise is 'unclear'."""
        try:
            picker = self.picker
            if picker is None:
                from clinic.nlu.intent_llm import pick_patient_intent as picker
            label = picker(self.text)
        except Exception as exc:
            _logger.warning("intent picker failed (%s); treating as unclear", exc)
            return "unclear"
        return label if label in ("book", "reschedule", "cancel", "status", "greeting", "human") else "unclear"

    def confused_idle(self):
        s = self.s
        s["confusion_count"] += 1
        if s["confusion_count"] >= MAX_CONFUSED_TURNS:
            return self.escalate("confusion")
        self.show_menu_keep_confusion("menu_unclear")

    def show_menu_keep_confusion(self, key):
        keep = self.s["confusion_count"]
        self.show_menu(key)
        self.s["confusion_count"] = keep

    # -- active goal, typed text -------------------------------------------------
    def on_goal_text(self, det):
        s = self.s
        goal = s["goal"]
        if s["step"] == "confirm":
            answer = parse_yes_no(self.text, goal)
            if answer is not None:
                return self.on_confirm(answer)
        if det == "greeting":
            return self.show_menu()
        if det == "status":
            return self.answer_status()
        if det in _INTENT_TO_GOAL and _INTENT_TO_GOAL[det] != goal:
            return self.start_goal(_INTENT_TO_GOAL[det], self.text)
        if det == "register":
            return self.escalate("register")
        step = s["step"]
        if step == "name":
            return self.answer_name()
        if step == "which":
            return self.answer_which()
        if step == "day":
            return self.answer_day()
        if step == "time":
            return self.answer_time()
        if step == "confirm":
            return self.answer_confirm_text()
        # Unknown step: start the goal over rather than guess.
        return self.start_goal(goal, self.text)

    def confused(self, reask):
        s = self.s
        s["confusion_count"] += 1
        if s["confusion_count"] >= MAX_CONFUSED_TURNS:
            return self.escalate("confusion")
        reask()

    def escalate(self, reason, key="escalate"):
        if self.s["goal"] in GOALS:
            self.note_activity("escalated", "Conversation handed to staff: {}".format(reason), code=reason)
        self.reset_flow()
        self.result.escalate = reason
        self.say(key, **({"days": BOOKING_HORIZON_DAYS} if key == "no_availability" else {}))

    # -- choices -----------------------------------------------------------------
    def on_choice(self):
        s = self.s
        kind, value = self.choice
        if kind == "menu":
            if value == "status":
                return self.answer_status()
            return self.start_goal(value, None)
        goal = s["goal"]
        if goal is None:
            if s["step"] == "submitted" and kind == "confirm":
                return self.say("already_submitted")
            if s["step"] == "done" and kind == "confirm":
                return self.say("already_done")
            s["expired_goal"] = None
            return self.say("session_expired", buttons=self.menu_buttons())
        step = s["step"]
        if kind in ("day", "slot") and goal in ("book", "reschedule") and step in ("day", "time", "confirm"):
            if kind == "day":
                iso = value
                if iso != self.slots.get("appt_date"):
                    self.slots.pop("start_time", None)
                self.slots["appt_date"] = iso
            else:
                iso, hhmm = value.split("T")
                self.slots["appt_date"], self.slots["start_time"] = iso, hhmm
            s["confusion_count"] = 0
            return self.advance()
        if kind == "appt" and step == "which":
            appt = self.find_own(int(value))
            if appt is None:
                return self.reprompt()
            return self.select_appointment(appt)
        if kind == "confirm" and step == "confirm":
            return self.on_confirm(value)
        # A button from an older question: just ask the current one again.
        return self.reprompt()

    # -- goals: entering ---------------------------------------------------------
    def start_goal(self, goal, trigger_text):
        s = self.s
        keep_name = self.slots.get("name")
        self.reset_flow()
        s["expired_goal"] = None
        if keep_name:
            self.slots["name"] = keep_name
        if goal == "book":
            return self.enter_book(trigger_text)
        if goal == "reschedule":
            return self.enter_reschedule(trigger_text)
        return self.enter_cancel()

    def prefill(self, text):
        if not text:
            return
        day = parse_day(text, self.today)
        hhmm = parse_time(text)
        if day:
            self.slots["appt_date"] = day
        if hhmm:
            self.slots["start_time"] = hhmm

    def open_requests(self):
        rows = self.conn.execute(
            "SELECT id, intent, slots_json FROM wa_messages WHERE wa_id = ? AND status = 'classified' "
            "AND intent IN ('book_appointment', 'cancel_appointment', 'reschedule_appointment')",
            (self.wa_id,),
        ).fetchall()
        out = []
        for row in rows:
            try:
                slots = json.loads(row["slots_json"] or "{}")
            except ValueError:
                slots = {}
            out.append((row["intent"], slots))
        return out

    def own_upcoming(self):
        """The sender's OWN booked/confirmed appointments that haven't started
        yet (by patient_id or by phone) -- the only ones they can act on."""
        if self._own_cache is None:
            now_key = (self.today.isoformat(), self.now.strftime("%H:%M"))
            self._own_cache = [
                a for a in sender_appointments(self.conn, self.wa_id, self.patient_id, self.now)
                if (a["appt_date"], a["start_time"]) >= now_key
            ]
        return self._own_cache

    def find_own(self, appointment_id):
        return next((a for a in self.own_upcoming() if a["id"] == appointment_id), None)

    def enter_book(self, trigger_text):
        s = self.s
        active = len(self.own_upcoming()) + sum(1 for intent, _ in self.open_requests() if intent == "book_appointment")
        if active >= MAX_ACTIVE_APPOINTMENTS:
            self.reset_flow()
            return self.say("cap_reached", n=MAX_ACTIVE_APPOINTMENTS)
        s["goal"] = "book"
        self.prefill(trigger_text)
        self.advance()

    def enter_reschedule(self, trigger_text):
        s = self.s
        appts = self.own_upcoming()
        if not appts:
            self.reset_flow()
            return self.say("no_appt", buttons=[(menu_choice("book"), self.btn("book"))])
        s["goal"] = "reschedule"
        self.prefill(trigger_text)
        if len(appts) == 1:
            return self.select_appointment(appts[0])
        self.ask_which(appts)

    def enter_cancel(self):
        s = self.s
        appts = self.own_upcoming()
        if not appts:
            self.reset_flow()
            return self.say("no_appt", buttons=[(menu_choice("book"), self.btn("book"))])
        s["goal"] = "cancel"
        if len(appts) == 1:
            return self.select_appointment(appts[0])
        self.ask_which(appts)

    # -- which appointment ---------------------------------------------------------
    def appt_title(self, appt):
        d = date.fromisoformat(appt["appt_date"])
        if self.lang == "hi":
            return "{} {}, {}".format(d.day, notify._HI_MONTHS[d.month - 1], self.ftime(appt["start_time"]))
        return "{} {} {}, {}".format(notify._EN_DAYS[d.weekday()][:3], d.day, notify._EN_MONTHS[d.month - 1],
                                      self.ftime(appt["start_time"]))

    def ask_which(self, appts, key="which_appt"):
        self.s["step"] = "which"
        rows = [(appt_choice(a["id"]), self.appt_title(a)) for a in appts[:10]]
        self.say(key, rows=rows, list_button=self.btn("choose"))

    def answer_which(self):
        day = parse_day(self.text, self.today)
        matches = [a for a in self.own_upcoming() if day and a["appt_date"] == day]
        if len(matches) == 1:
            return self.select_appointment(matches[0])
        self.confused(lambda: self.ask_which(self.own_upcoming(), "reask_which"))

    def select_appointment(self, appt):
        s = self.s
        for intent, slots in self.open_requests():
            if intent in ("cancel_appointment", "reschedule_appointment") and slots.get("appointment_id") == appt["id"]:
                self.reset_flow()
                return self.say("dup_request")
        self.slots.update(appointment_id=appt["id"], old_date=appt["appt_date"], old_time=appt["start_time"])
        s["confusion_count"] = 0
        if s["goal"] == "cancel":
            s["step"] = "confirm"
            return self.say(
                "confirm_cancel", date=self.fdate(appt["appt_date"]), time=self.ftime(appt["start_time"]),
                buttons=[(confirm_choice("yes"), self.btn("yes")), (confirm_choice("no"), self.btn("no"))],
            )
        self.advance()

    # -- book / reschedule: ask what is missing ----------------------------------
    def day_options(self, limit=DAY_BUTTONS, horizon=14):
        out = []
        for offset in range(horizon):
            day = self.today + timedelta(days=offset)
            if self.free(day.isoformat()):
                out.append(day.isoformat())
                if len(out) == limit:
                    break
        return out

    def day_label(self, iso):
        d = date.fromisoformat(iso)
        if d == self.today:
            return ct.DAY_WORDS["today"][self.lang]
        if d == self.today + timedelta(days=1):
            return ct.DAY_WORDS["tomorrow"][self.lang]
        if self.lang == "hi":
            return "{} {}".format(d.day, notify._HI_MONTHS[d.month - 1])
        return "{}, {} {}".format(notify._EN_DAYS[d.weekday()][:3], d.day, notify._EN_MONTHS[d.month - 1])

    def next_day_with_slots(self, after_iso=None):
        start = date.fromisoformat(after_iso) + timedelta(days=1) if after_iso else self.today
        for offset in range(BOOKING_HORIZON_DAYS + 1):
            day = start + timedelta(days=offset)
            if day > self.today + timedelta(days=BOOKING_HORIZON_DAYS):
                break
            if self.free(day.isoformat()):
                return day.isoformat()
        return None

    def ask_day(self, key=None, **values):
        s = self.s
        s["step"] = "day"
        options = self.day_options()
        if not options and self.next_day_with_slots() is None:
            return self.escalate("no_availability", key="no_availability")
        if key is None:
            key = "ask_day_resched" if s["goal"] == "reschedule" else "ask_day"
        if key == "ask_day_resched":
            values = {"date": self.fdate(self.slots["old_date"]), "time": self.ftime(self.slots["old_time"])}
        buttons = [(day_choice(d), self.day_label(d)) for d in options] or None
        self.say(key, buttons=buttons, **values)

    def offer(self, key, iso, **values):
        """Up to SLOT_OFFERS free times on `iso`, as buttons (<=3) or a list."""
        free = self.free(iso)
        picks = _spread(free, SLOT_OFFERS)
        self.s["step"] = "time"
        values.setdefault("date", self.fdate(iso))
        pairs = [(slot_choice(iso, t), self.ftime(t)) for t in picks]
        if len(pairs) <= 3:
            self.say(key, buttons=pairs, **values)
        else:
            self.say(key, rows=pairs, list_button=self.btn("choose_time"), **values)

    def hours_text(self):
        spans = ["{}-{}".format(self.ftime(a), self.ftime(b)) for a, b in scheduling.CLINIC_HOURS]
        return ct.text("and", self.lang).join(spans)

    def date_problem(self, iso):
        if iso < self.today.isoformat():
            return "date_past"
        if iso > (self.today + timedelta(days=BOOKING_HORIZON_DAYS)).isoformat():
            return "date_far"
        return None

    def time_problem(self, iso, hhmm):
        if not _in_hours(hhmm):
            return "time_closed"
        if iso == self.today.isoformat() and hhmm <= self.now.strftime("%H:%M"):
            return "time_past"
        if scheduling.is_slot_blocked(self.conn, iso, hhmm):
            return "time_blocked"
        return "time_taken"

    def advance(self, finalize=False, changed=False):
        """Ask for whatever is still missing -- name, then day, then time --
        validating what the patient already gave; when everything is valid,
        show the confirmation summary (or, with finalize, hand off)."""
        s, sl = self.s, self.s["slots"]
        if s["goal"] == "book" and self.patient is None and not sl.get("name"):
            s["step"] = "name"
            return self.say("ask_name")

        iso = sl.get("appt_date")
        if iso:
            problem = self.date_problem(iso)
            if problem:
                sl.pop("appt_date", None)
                values = {"days": BOOKING_HORIZON_DAYS} if problem == "date_far" else {}
                s["step"] = "day"
                return self.say(problem, buttons=self.day_buttons(), **values)
        if not iso:
            return self.ask_day("ask_day_changed" if changed else None)

        free = self.free(iso)
        if not free:
            nxt = self.next_day_with_slots(after_iso=iso)
            if nxt is None:
                return self.escalate("no_availability", key="no_availability")
            sl["appt_date"] = nxt
            sl.pop("start_time", None)
            if scheduling.day_blocked(self.conn, iso):
                self.note_activity("blocked", "Asked for {}, a day the clinic is not taking appointments".format(iso),
                                   code="blocked", appt_date=iso)
                return self.offer("day_blocked", nxt, date=self.fdate(iso), next_date=self.fdate(nxt))
            return self.offer("offer_next_day", nxt, date=self.fdate(iso), next_date=self.fdate(nxt))

        hhmm = sl.get("start_time")
        if not hhmm:
            return self.offer("offer_slots", iso)
        if hhmm not in free:
            sl.pop("start_time", None)
            problem = self.time_problem(iso, hhmm)
            values = {}
            if problem == "time_closed":
                values = {"hours": self.hours_text()}
            elif problem in ("time_taken", "time_blocked"):
                values = {"time": self.ftime(hhmm)}
            if problem == "time_blocked":
                self.note_activity("blocked", "Asked for {} {}, inside a booking block".format(iso, hhmm),
                                   code="blocked", appt_date=iso, start_time=hhmm)
            elif finalize and problem == "time_taken":
                self.note_activity("conflict", "Asked for {} {}, which was taken before they confirmed".format(iso, hhmm),
                                   code="slot_taken", appt_date=iso, start_time=hhmm)
            if finalize and problem in ("time_taken", "time_blocked"):
                # The patient had already confirmed this exact time: say it was
                # just taken (or is not available), not a bare "not available".
                problem = "slot_just_taken" if problem == "time_taken" else "slot_blocked"
            return self.offer(problem, iso, **values)

        if finalize:
            return self.handoff_booking()
        s["step"] = "confirm"
        self.summary()

    def day_buttons(self):
        return [(day_choice(d), self.day_label(d)) for d in self.day_options()] or None

    def summary(self):
        sl = self.slots
        buttons = [(confirm_choice("yes"), self.btn("confirm")), (confirm_choice("change"), self.btn("change"))]
        if self.s["goal"] == "book":
            self.say("confirm_book", buttons=buttons, name=self.patient_name() or "-",
                     date=self.fdate(sl["appt_date"]), time=self.ftime(sl["start_time"]))
        else:
            self.say("confirm_reschedule", buttons=buttons, old_date=self.fdate(sl["old_date"]),
                     old_time=self.ftime(sl["old_time"]), date=self.fdate(sl["appt_date"]),
                     time=self.ftime(sl["start_time"]))

    # -- answers -----------------------------------------------------------------
    def answer_name(self):
        name = parse_name(self.text)
        if name is None:
            return self.confused(lambda: self.say("ask_name_retry"))
        self.slots["name"] = name
        self.s["confusion_count"] = 0
        self.advance()

    def answer_day(self):
        iso = parse_day(self.text, self.today, allow_bare_day_of_month=True)
        hhmm = parse_time(self.text)
        if iso is None:
            if hhmm:
                self.slots["start_time"] = hhmm
            return self.confused(lambda: self.ask_day("ask_day_retry", **{}))
        self.slots["appt_date"] = iso
        if hhmm:
            self.slots["start_time"] = hhmm   # else a time given earlier is kept
        self.s["confusion_count"] = 0
        self.advance()

    def answer_time(self):
        iso = parse_day(self.text, self.today)
        hhmm = parse_time(self.text, allow_bare_hour=iso is None)
        if iso is None and hhmm is None:
            return self.confused(lambda: self.reoffer("offer_retry"))
        if iso is not None and iso != self.slots.get("appt_date"):
            self.slots["appt_date"] = iso
            self.slots.pop("start_time", None)
        if hhmm:
            self.slots["start_time"] = hhmm
        self.s["confusion_count"] = 0
        self.advance()

    def reoffer(self, key):
        iso = self.slots.get("appt_date")
        if iso and self.free(iso):
            return self.offer(key, iso)
        self.advance()

    def answer_confirm_text(self):
        """Typed something other than yes/no at the summary: a new day or
        time changes the request; anything else is a confused turn."""
        iso = parse_day(self.text, self.today)
        hhmm = parse_time(self.text, allow_bare_hour=iso is None)
        if iso is None and hhmm is None:
            return self.confused(self.reask_confirm)
        if iso is not None and iso != self.slots.get("appt_date"):
            self.slots["appt_date"] = iso
            self.slots.pop("start_time", None)
        if hhmm:
            self.slots["start_time"] = hhmm
        self.s["confusion_count"] = 0
        self.advance()

    def reask_confirm(self):
        if self.s["goal"] == "cancel":
            return self.say("reask_yesno", buttons=[(confirm_choice("yes"), self.btn("yes")),
                                                     (confirm_choice("no"), self.btn("no"))])
        self.say("reask_confirm", buttons=[(confirm_choice("yes"), self.btn("confirm")),
                                           (confirm_choice("change"), self.btn("change"))])

    def reprompt(self):
        """Ask the current question again (no confusion counted)."""
        s = self.s
        step = s["step"]
        if step == "name":
            return self.say("ask_name")
        if step == "which":
            return self.ask_which(self.own_upcoming())
        if step == "day":
            return self.ask_day()
        if step == "time":
            return self.reoffer("offer_slots")
        if step == "confirm":
            if s["goal"] == "cancel":
                return self.select_appointment_summary()
            return self.summary()
        self.show_menu()

    def select_appointment_summary(self):
        sl = self.slots
        self.say("confirm_cancel", date=self.fdate(sl["old_date"]), time=self.ftime(sl["old_time"]),
                 buttons=[(confirm_choice("yes"), self.btn("yes")), (confirm_choice("no"), self.btn("no"))])

    # -- confirmation -------------------------------------------------------------
    def on_confirm(self, answer):
        goal = self.s["goal"]
        if goal == "cancel":
            if answer == "yes":
                return self.handoff_cancel()
            self.reset_flow()
            return self.say("nothing_cancelled")
        if answer == "yes":
            return self.advance(finalize=True)
        # "no" / "change": pick again
        self.slots.pop("appt_date", None)
        self.slots.pop("start_time", None)
        self.s["confusion_count"] = 0
        self.advance(changed=True)

    # -- status -------------------------------------------------------------------
    def answer_status(self):
        self.reset_flow()
        appts = self.own_upcoming()
        if not appts:
            return self.say("status_none", buttons=[(menu_choice("book"), self.btn("book"))])
        self.result.replies.append(Reply("", kind="status", appointment_id=appts[0]["id"]))

    # -- hand-offs ----------------------------------------------------------------
    def handoff_booking(self):
        sl = self.slots
        goal = self.s["goal"]
        if goal == "book":
            slots = {
                "patient_id": self.patient_id,
                "patient_name": None if self.patient else sl.get("name"),
                "patient_phone": None if self.patient else last10_digits(self.wa_id),
                "appt_date": sl["appt_date"], "start_time": sl["start_time"],
                "duration_minutes": None,
                "notes": "Booked via WhatsApp conversation",
            }
            note = "Collected by the WhatsApp assistant: {} asked for {} at {}. ".format(
                self.patient_name() or "the patient", sl["appt_date"], sl["start_time"])
        else:
            if self.find_own(sl.get("appointment_id")) is None:
                self.reset_flow()
                return self.say("no_appt", buttons=[(menu_choice("book"), self.btn("book"))])
            slots = {"appointment_id": sl["appointment_id"], "appt_date": sl["appt_date"],
                     "start_time": sl["start_time"]}
            note = "Collected by the WhatsApp assistant: move the appointment from {} {} to {} {}. ".format(
                sl["old_date"], sl["old_time"], sl["appt_date"], sl["start_time"])
        slots["via"] = "conversation"
        self.complete(_HANDOFF_INTENT[goal], slots, note)

    def handoff_cancel(self):
        sl = self.slots
        appt = self.find_own(sl.get("appointment_id"))
        if appt is None:
            self.reset_flow()
            return self.say("no_appt", buttons=[(menu_choice("book"), self.btn("book"))])
        note = "Collected by the WhatsApp assistant: the patient asked to cancel {} {}.".format(
            appt["appt_date"], appt["start_time"])
        self.complete("cancel_appointment", {"appointment_id": appt["id"], "via": "conversation"}, note)

    def complete(self, intent, slots, note):
        """A finished, patient-confirmed request. With an `auto` callable the
        automatic path gets first refusal (see the module docstring); anything
        it declines -- or no callable at all -- is handed to staff."""
        outcome = None
        if self.auto is not None:
            outcome = self.auto(
                self.conn, intent=intent, slots=dict(slots), wa_id=self.wa_id, patient_id=self.patient_id,
                patient_name=self.patient_name() or self.slots.get("name"), msg_id=self.msg_id, now=self.now,
                language=self.lang)
            self.result.auto = outcome

        if outcome is not None and outcome.kind == "committed":
            return self.finish_auto(intent, slots, outcome)
        if outcome is not None and outcome.kind == "retry":
            return self.offer_again(outcome)

        # Escalated (or no automatic path): the request goes to the staff
        # inbox; the patient's slot is held for them meanwhile.
        if intent != "cancel_appointment":
            create_hold(self.conn, self.wa_id, slots["appt_date"], slots["start_time"], self.now, self.msg_id)
            note += "The slot is held for the patient for {} hours.".format(HOLD_HOURS)
        slots["agent_note"] = note
        if outcome is not None and outcome.reason and outcome.code != "switch_off":
            slots["needs_staff_reason"] = outcome.reason
        self.result.handoff = Handoff(intent, slots, self.patient_id, note)
        self.reset_flow(step="submitted")
        self.say("handoff_received")

    def finish_auto(self, intent, slots, outcome):
        """The request was committed automatically. The patient's confirmation
        is the normal booking / cancel / reschedule notification; only if that
        could not be queued does this send a plain fixed-template confirmation."""
        sl = dict(self.slots)
        self.reset_flow(step="done")
        if outcome.confirmation_queued:
            return
        if intent == "cancel_appointment":
            key, iso, hhmm = "auto_done_cancel", sl.get("old_date"), sl.get("old_time")
        else:
            key = "auto_done_book" if intent == "book_appointment" else "auto_done_reschedule"
            iso, hhmm = slots.get("appt_date"), slots.get("start_time")
        if iso and hhmm:
            self.say(key, date=self.fdate(iso), time=self.ftime(hhmm))

    def offer_again(self, outcome):
        """The slot stopped being usable between the patient's confirmation
        and the commit (someone else took it / a block was added): not an
        escalation -- show fresh free times."""
        sl = self.slots
        iso = sl.get("appt_date")
        hhmm = sl.pop("start_time", None)
        self.s["confusion_count"] = 0
        key = "slot_blocked" if outcome.code == "blocked" else "slot_just_taken"
        if iso and hhmm and self.free(iso):
            return self.offer(key, iso, time=self.ftime(hhmm))
        self.advance()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def handle_inbound(conn, wa_id, text, choice_id=None, now=None, msg_id=None, intent_picker=None, auto=None):
    """Process one inbound patient message and return a Result.

    `now` is the clinic's local wall clock (naive datetime, injectable).
    `msg_id` is the inbound wa_messages row id (links a slot hold to the
    inbox item that staff will resolve). `intent_picker(text) -> label` is the
    closed-enum LLM stage (defaults to intent_llm.pick_patient_intent);
    inject a fake in tests. `auto(conn, *, intent, slots, wa_id, patient_id,
    patient_name, msg_id, now, language) -> AutoOutcome` is the automatic-commit hook
    (clinic/auto_actions.py via app.py); None means every finished request
    goes to staff. Writes only wa_sessions and slot_holds itself."""
    now = (now or datetime.now()).replace(microsecond=0)
    choice = parse_choice(choice_id)
    turn = _Turn(conn, wa_id, text, choice, now, msg_id, intent_picker, auto)
    return turn.run()
