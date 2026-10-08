"""Questions the assistant could not answer: captured, queued for a person, and
reported back to the user when they work (the additive `unanswered_questions` table).

The loop is deliberately human-in-the-loop:

  1. a staff command that is clearly a question / read request cannot be answered
     (the planner called `unsupported` with a `wanted`, or called `query` with
     something outside the whitelist in clinic/query_tool.py, or the command ended
     as "please rephrase" and reads like an information request);
  2. THIS module stores it (deduplicated on a normalised key, counting repeats) and
     the assistant says so with a FIXED reply -- never model-written text;
  3. a developer reads the queue (scripts/export_unanswered.py), adds a whitelist
     entry to clinic/query_tool.py, and marks the question resolved in the Audit
     log tab with a one-line note ("Ask: who's on duty now");
  4. the next time the Assistant tab loads, the user is told once that it works.

The app never writes, generates or enables a whitelist entry or any SQL by itself:
a question's text and the model's rejected spec are only stored here, as data, for a
person to read. Transcripts can contain patient names, so the table is as private as
`patients` (it lives in the local database only) and logging obeys the
`planner_log_enabled` setting (clinic/settings.py).
"""

import json
import logging
import re
import sqlite3
import unicodedata
from collections import namedtuple
from datetime import datetime

from clinic import settings

_logger = logging.getLogger(__name__)

STATUSES = ("new", "building", "resolved", "dismissed")
OPEN_STATUSES = ("new", "building")
SOURCES = ("voice", "typed")
MAX_NOTE = 200
MAX_TRANSCRIPT = 400
MAX_WANTED = 120
MAX_SPEC = 1500


# -- the key a question is deduplicated on ---------------------------------------

def normalise_key(text):
    """lowercase, punctuation stripped, spaces collapsed ("Who's on duty NOW?" -> "who s on duty now").
    Letters, digits and combining marks (Devanagari vowel signs) are kept."""
    kept = []
    for ch in unicodedata.normalize("NFC", str(text or "")).casefold():
        kept.append(ch if unicodedata.category(ch)[0] in "LNM" else " ")
    return " ".join("".join(kept).split())


# -- does a command read like a request for information? -------------------------
# Conservative on purpose: a command is only counted when it has BOTH a question /
# "show me" cue AND a clinic-business noun, and does not open with a write verb. Small
# talk ("tell me a joke", "what's the weather") has no clinic noun, a write ("book Amit
# tomorrow") has no cue or opens with a write verb.

_CUES = re.compile(
    r"(?<![\wऀ-ॿ])(?:what|which|who|whom|whose|when|where|why|how\s+(?:many|much|long|often)|show|list|display|"
    r"tell\s+me|give\s+me|find|count|total|is\s+there|are\s+there|do\s+we\s+have|did\s+we|has\s+any|have\s+any|"
    r"kitne|kitni|kitna|kaun|kaunsa|kaunse|kaunsi|kab|kahan|kya|dikhao|dikha|batao|bata|"
    r"कितने|कितनी|कितना|कौन|कौनसा|कौनसे|कब|कहाँ|कहां|क्या|दिखाओ|दिखा|बताओ|बता)(?![\wऀ-ॿ])", re.IGNORECASE)
_NOUNS = re.compile(
    r"(?<![\wऀ-ॿ])(?:patients?|mareez|mareezon|appointments?|doctors?|dr|staff|nurses?|compounders?|receptionists?|"
    r"employees?|branch(?:es)?|follow-?ups?|fees?|expenses?|kharch|visits?|reminders?|messages?|whatsapp|audit|schedules?|"
    r"slots?|tokens?|queue|closures?|cash|cashbook|salary|salaries|payroll|attendance|leaves?|records?|bills?|invoices?|"
    r"medicines?|prescriptions?|diagnos\w+|lab|reports?|revenue|income|profit|sales|inventory|stock|collection|hisaab|"
    r"paise|rupees?|rupaye|per\s+month|monthly|"
    r"मरीज़|मरीज|मरीजों|अपॉइंटमेंट|डॉक्टर|स्टाफ|नर्स|ब्रांच|फॉलो|फीस|खर्च|विज़िट|रिमाइंडर|मैसेज|वेतन|हाज़िरी|उपस्थिति|कमाई|हिसाब)"
    r"(?![\wऀ-ॿ])", re.IGNORECASE)
_WRITE_VERBS = frozenset((
    "book", "cancel", "reschedule", "shift", "move", "register", "add", "delete", "remove", "record", "log", "mark",
    "close", "schedule", "send", "set", "update", "change", "edit", "switch", "open", "approve",
    "badal", "radd", "hata", "jodo", "bhejo", "laga", "kar", "rakho", "daalo"))
_POLITE = re.compile(r"^(?:please|pls|kindly|ok|okay|hey|hi|hello|so|and|also|zara|ek\s+baar|can\s+you|could\s+you|would\s+you)\s+", re.IGNORECASE)


def opens_with_write_verb(text):
    """True when the command starts with a verb that changes something ("book Amit ...", "cancel ...")."""
    lead = " ".join(str(text or "").split())
    while True:
        stripped = _POLITE.sub("", lead)
        if stripped == lead:
            break
        lead = stripped
    return re.split(r"[\s,.!?]+", lead.strip(), maxsplit=1)[0].casefold() in _WRITE_VERBS


def looks_like_information_request(text):
    """True for a command that is plainly asking to see / count / find clinic information."""
    text = " ".join(str(text or "").split())
    if not text or opens_with_write_verb(text):
        return False
    cue =bool(_CUES.search(text)) or text.rstrip().endswith(("?", "？"))
    return cue and bool(_NOUNS.search(text))


# -- the fixed replies ----------------------------------------------------------

REPLIES = {
    "new": {
        "en": "I can't answer that yet. I will work on it.",
        "hi": "मैं अभी इसका जवाब नहीं दे सकता। मैं इस पर काम करूँगा।",
        "hinglish": "Main abhi iska jawab nahi de sakta. Main is par kaam karunga.",
    },
    "repeat": {
        "en": "I can't answer that yet. I already have this question saved (asked {count} times) and it hasn't been added yet. I'll tell you when it works.",
        "hi": "मैं अभी इसका जवाब नहीं दे सकता। यह सवाल पहले से सहेजा हुआ है ({count} बार पूछा गया) और अभी जोड़ा नहीं गया है। जब यह काम करेगा तो मैं आपको बताऊँगा।",
        "hinglish": "Main abhi iska jawab nahi de sakta. Yeh sawaal pehle se save hai ({count} baar poocha gaya) aur abhi joda nahi gaya hai. Jab yeh kaam karega to main aapko bataunga.",
    },
    "unsaved": {
        "en": "I can't answer that yet.",
        "hi": "मैं अभी इसका जवाब नहीं दे सकता।",
        "hinglish": "Main abhi iska jawab nahi de sakta.",
    },
}
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def reply_language(text, language_code):
    """'hi' for a transcript written in Devanagari, 'hinglish' when the voice session says
    Hindi (hi-IN), else 'en' -- the same split the other fixed voice replies use."""
    if _DEVANAGARI.search(text or ""):
        return "hi"
    return "hinglish" if language_code == "hi-IN" else "en"


def reply_text(kind, language, count=1):
    return REPLIES[kind][language if language in REPLIES[kind] else "en"].format(count=count)


# -- storing ----------------------------------------------------------------------

Captured = namedtuple("Captured", ["id", "times_asked", "created", "reopened"])


def _stamp(now):
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def _clip(value, cap):
    text = " ".join(str(value).split()) if value not in (None, "") else ""
    return text[:cap] or None


def _spec_json(spec):
    if not isinstance(spec, dict) or not spec:
        return None
    try:
        text = json.dumps(spec, ensure_ascii=False, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return None
    return text if len(text) <= MAX_SPEC else text[:MAX_SPEC]


def capture(conn, transcript, source="voice", wanted=None, rejected_spec=None, now=None):
    """Store (or count again) one unanswered question. Returns Captured, or None when logging
    is off (planner_log_enabled = 0), the text is empty, or the database refuses. Never raises.
    A question that was resolved or dismissed and is asked (and fails) again is reopened."""
    key = normalise_key(transcript)
    if conn is None or not key:
        return None
    source = source if source in SOURCES else "typed"
    try:
        if not settings.planner_log_enabled(conn):
            return None
        stamp = _stamp(now)
        wanted, spec = _clip(wanted, MAX_WANTED), _spec_json(rejected_spec)
        row = conn.execute("SELECT id, times_asked, status, wanted, rejected_spec_json FROM unanswered_questions WHERE key = ?",
                           (key,)).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO unanswered_questions (first_asked_at, last_asked_at, times_asked, key, example_transcript, wanted, "
                "rejected_spec_json, source, status) VALUES (?, ?, 1, ?, ?, ?, ?, ?, 'new')",
                (stamp, stamp, key, _clip(transcript, MAX_TRANSCRIPT), wanted, spec, source))
            conn.commit()
            return Captured(cur.lastrowid, 1, True, False)
        reopened = row["status"] in ("resolved", "dismissed")
        conn.execute(
            "UPDATE unanswered_questions SET last_asked_at = ?, times_asked = times_asked + 1, wanted = COALESCE(?, wanted), "
            "rejected_spec_json = COALESCE(?, rejected_spec_json)"
            + (", status = 'new', resolved_note = NULL, resolved_at = NULL, notified_at = NULL" if reopened else "")
            + " WHERE id = ?", (stamp, wanted, spec, row["id"]))
        conn.commit()
        return Captured(row["id"], row["times_asked"] + 1, False, reopened)
    except sqlite3.Error:
        _logger.warning("Could not save the unanswered question", exc_info=True)
        return None


def reply(conn, transcript, source, language_code, wanted=None, rejected_spec=None, now=None):
    """Capture the question and return the fixed reply to show the user."""
    language = reply_language(transcript, language_code)
    captured = capture(conn, transcript, source, wanted, rejected_spec, now)
    if captured is None:
        return reply_text("unsaved", language)
    if captured.times_asked > 1:
        return reply_text("repeat", language, captured.times_asked)
    return reply_text("new", language)


# -- the staff view and the developer queue ------------------------------------------

_COLUMNS = ("id, first_asked_at, last_asked_at, times_asked, key, example_transcript, wanted, rejected_spec_json, source, "
            "status, resolved_note, resolved_at, notified_at")


def _item(row):
    item = dict(row)
    item["question"] = item.pop("example_transcript")
    return item


def list_items(conn, closed_limit=30):
    """{'open': [...most asked first], 'closed': [...most recently handled first]}."""
    marks = ", ".join("?" * len(OPEN_STATUSES))
    open_rows = conn.execute(
        "SELECT {} FROM unanswered_questions WHERE status IN ({}) ORDER BY times_asked DESC, last_asked_at DESC, id".format(_COLUMNS, marks),
        OPEN_STATUSES).fetchall()
    closed_rows = conn.execute(
        "SELECT {} FROM unanswered_questions WHERE status NOT IN ({}) ORDER BY COALESCE(resolved_at, last_asked_at) DESC, id DESC LIMIT ?".format(
            _COLUMNS, marks), OPEN_STATUSES + (int(closed_limit),)).fetchall()
    return {"open": [_item(r) for r in open_rows], "closed": [_item(r) for r in closed_rows]}


def get(conn, item_id):
    row = conn.execute("SELECT {} FROM unanswered_questions WHERE id = ?".format(_COLUMNS), (item_id,)).fetchone()
    return _item(row) if row else None


class UnansweredError(ValueError):
    """A bad status change; the message is shown to staff."""


def set_status(conn, item_id, status, note=None, now=None):
    """Move a question to `status`. 'resolved' needs a one-line note (what they can now ask).
    Returns the updated item."""
    if status not in STATUSES:
        raise UnansweredError("Unknown status.")
    if get(conn, item_id) is None:
        raise UnansweredError("That question does not exist.")
    clean = _clip(note, MAX_NOTE + 1)
    if status == "resolved":
        if not clean:
            raise UnansweredError("Write one line about what can be asked now, for example: Ask: who is on duty now.")
        if len(clean) > MAX_NOTE:
            raise UnansweredError("Keep the note under {} characters.".format(MAX_NOTE))
        conn.execute("UPDATE unanswered_questions SET status = 'resolved', resolved_note = ?, resolved_at = ?, notified_at = NULL WHERE id = ?",
                     (clean, _stamp(now), item_id))
    elif status == "new":
        conn.execute("UPDATE unanswered_questions SET status = 'new', resolved_note = NULL, resolved_at = NULL, notified_at = NULL WHERE id = ?",
                     (item_id,))
    elif status == "dismissed":
        conn.execute("UPDATE unanswered_questions SET status = 'dismissed', resolved_note = NULL, resolved_at = ?, notified_at = NULL WHERE id = ?",
                     (_stamp(now), item_id))
    else:
        conn.execute("UPDATE unanswered_questions SET status = 'building', resolved_note = NULL, resolved_at = NULL, notified_at = NULL WHERE id = ?",
                     (item_id,))
    conn.commit()
    return get(conn, item_id)


def pending_notices(conn):
    """Resolved questions the user has not been told about yet: [{id, question, note}]."""
    rows = conn.execute("SELECT id, example_transcript, resolved_note FROM unanswered_questions "
                        "WHERE status = 'resolved' AND notified_at IS NULL ORDER BY resolved_at, id").fetchall()
    return [{"id": r["id"], "question": r["example_transcript"], "note": r["resolved_note"]} for r in rows]


def mark_notified(conn, item_id, now=None):
    """The user has been told this one works: never show it again. True when a row changed."""
    cur = conn.execute("UPDATE unanswered_questions SET notified_at = ? WHERE id = ? AND status = 'resolved' AND notified_at IS NULL",
                       (_stamp(now), item_id))
    conn.commit()
    return cur.rowcount > 0


def notice_text(item):
    """The one-line notice shown to the user (fixed wording)."""
    return "You asked '{}' earlier. It works now: {}. Try it again.".format(item["question"], str(item["note"]).rstrip(". "))


def export_open(conn):
    """The open questions as plain text for a developer: most asked first, with the model's rejected spec."""
    lines = []
    for number, item in enumerate(list_items(conn)["open"], start=1):
        lines.append("{}. [{}] asked {}x ({} to {}, via {})".format(
            number, item["status"], item["times_asked"], item["first_asked_at"], item["last_asked_at"], item["source"]))
        lines.append("   question: {}".format(item["question"]))
        if item["wanted"]:
            lines.append("   wanted:   {}".format(item["wanted"]))
        if item["rejected_spec_json"]:
            lines.append("   rejected spec (the planner's guess, NOT trusted): {}".format(item["rejected_spec_json"]))
        lines.append("   id {} -- mark it resolved in the Audit log tab once the whitelist entry exists".format(item["id"]))
    return "\n".join(lines)
