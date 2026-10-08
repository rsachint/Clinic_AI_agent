"""Closing a branch (or a doctor's days) by voice.

"Close Branch A tomorrow, the doctor is ill", "Branch B kal band hai", "Dr. Rao
is on leave from Monday to Wednesday". Deterministic phrase rules (no model):
`is_close_command` recognises it, `parse` pulls out the branch / doctor, the
days and the reason. The result is only ever a PLAN shown as the batch review
card -- nothing is closed, moved or sent until a person presses Apply.
"""

import re
import unicodedata
from datetime import date, timedelta

from clinic import branches, voice_branch
from clinic.nlu import date_guard, datetime_extract

_EDGE_L = r"(?<![\wऀ-ॿ])"
_EDGE_R = r"(?![\wऀ-ॿ])"

_CLOSE_VERB = re.compile(
    _EDGE_L + r"(?:close|closed|closing|closure|shut|shutdown|shutting|band|bandh|बंद|बन्द)" + _EDGE_R, re.IGNORECASE)
_BRANCH_WORD = re.compile(_EDGE_L + r"(?:branch(?:es)?|clinic|ब्रांच|ब्रान्च|क्लिनिक|क्लीनिक)" + _EDGE_R, re.IGNORECASE)
_LEAVE_WORD = re.compile(
    r"on\s+leave|leave\s+(?:par|pe|per)|(?<![a-z])(?:ill|sick|unwell|absent|chutti|not\s+coming|nahi\s+aayenge|nahi\s+aa\s+rahe)(?![a-z])"
    r"|छुट्टी|बीमार|नहीं\s+आएंगे|नहीं\s+आयेंगे", re.IGNORECASE)
# Questions about hours or the state of things are reads, not a request to close.
_NOT_A_CLOSURE = re.compile(
    _EDGE_L + r"(?:show|list|how\s+many|what|when|which|why|who|whom|kitne|kitni|kab|kya|kyun|kyon|kaun|status|timing|timings|hours"
    r"|closing\s+time|opens?|open\s+hours|दिखा\w*|कितने|कितनी|कब|क्या|क्यों|कौन|समय"
    r"|^\s*(?:is|are|was|were|has|have|does|did))" + _EDGE_R, re.IGNORECASE)

_REASON = re.compile(
    r"(?:because(?:\s+of)?|since|due\s+to|reason(?:\s+is)?|kyunki|kyuki|क्योंकि|कारण)\s*[:,]?\s*(.+)$", re.IGNORECASE | re.DOTALL)
_UNTIL = re.compile(_EDGE_L + r"(?:till|until|through|thru|upto|up\s+to|to|tak|तक)" + _EDGE_R + r"\s+(.+)$", re.IGNORECASE)
_FOR_DAYS = re.compile(
    r"(?:for|next)\s+(\d{1,2}|one|two|three|four|five|six|seven)\s+days?|(\d{1,2})\s+(?:din|दिन)", re.IGNORECASE)
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
MAX_VOICE_DAYS = 31


def _norm(text):
    return unicodedata.normalize("NFC", text or "").lower()


def find_doctor(conn, text):
    """The one doctor named in `text` ("Dr. Rao", "Rao"), or None."""
    norm = _norm(text)
    found = {}
    for doctor in branches.list_doctors(conn):
        name = _norm(doctor["name"])
        words = [w for w in re.findall(r"[\wऀ-ॿ]+", name) if w not in ("dr", "doctor", "dr.")]
        candidates = [name] + words
        for candidate in candidates:
            if candidate and re.search(_EDGE_L + re.escape(candidate) + _EDGE_R, norm):
                found[doctor["id"]] = doctor
                break
    return next(iter(found.values())) if len(found) == 1 else None


def is_close_command(conn, text, mention=None):
    """True for a request to close a branch / take a doctor off for some days."""
    if not branches.multi_branch(conn):
        return False
    norm = _norm(text)
    if _NOT_A_CLOSURE.search(norm):
        return False
    named_branch = (mention.branch is not None) if mention is not None else False
    if _CLOSE_VERB.search(norm) and (named_branch or _BRANCH_WORD.search(norm)):
        return True
    return bool(_LEAVE_WORD.search(norm) and find_doctor(conn, text) is not None)


def _days_count(text):
    m = _FOR_DAYS.search(text)
    if not m:
        return None
    raw = (m.group(1) or m.group(2) or "").lower()
    return int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw)


def parse(conn, text, cleaned_text, mention, today=None):
    """Slots for a close command: branch_id / doctor_id, appt_date (first day),
    end_date (last day, when a range was said), reason. Missing pieces are left
    out so the assistant can ask."""
    today = today or date.today()
    slots = {}
    doctor = find_doctor(conn, text) if _LEAVE_WORD.search(_norm(text)) else None
    if doctor:
        slots["doctor_id"] = doctor["id"]
    if mention is not None and mention.branch:
        slots["branch_id"] = mention.branch["id"]

    # the days: "tomorrow", "Monday to Wednesday", "for 3 days"
    until = _UNTIL.search(cleaned_text)
    first_part = cleaned_text[:until.start()] if until else cleaned_text
    start = datetime_extract.extract_appt_date(first_part, today)
    if start:
        slots["appt_date"] = start
        end = None
        if until:
            end = datetime_extract.extract_appt_date(until.group(1), today)
        if end is None:
            days = _days_count(cleaned_text)
            if days and days > 1:
                end = (date.fromisoformat(start) + timedelta(days=min(days, MAX_VOICE_DAYS) - 1)).isoformat()
        if end and end > start:
            slots["end_date"] = end
    elif datetime_extract.mentions_unreadable_date(cleaned_text, today):
        slots["date_unreadable"] = True

    m = _REASON.search(text)
    if m:
        slots["reason"] = re.sub(r"\s+", " ", m.group(1)).strip(" .,।!?")[:200]
    elif doctor and _LEAVE_WORD.search(_norm(text)):
        slots["reason"] = "{} is on leave".format(doctor["name"])
    return slots


_MOVE_CUE = re.compile(
    _EDGE_L + r"(?:move|moved|shift|shifted|send|sent|transfer|transferred|redirect|reassign|भेज\w*|शिफ्ट|ट्रांसफर)" + _EDGE_R,
    re.IGNORECASE)


def needs_reading(conn, text, slots):
    """True when the phrase rules above probably missed something in a closing
    command: no first day, a length ("for the next one week", "agle 3 din") with no
    last day, or a second branch named (where everyone should go). Only then is
    the planner worth asking (clinic/pipeline.py); a plain "close Branch A
    tomorrow" never is."""
    if "appt_date" not in slots:
        return True
    if "end_date" not in slots and date_guard.duration_days(text):
        return True
    return len(voice_branch.named_branches(conn, text)) >= 2


def apply_reading_rules(conn, text, slots, today=None):
    """What the plain rules can still do for such a command when the planner could
    not help (deterministic, no model): the first branch named is the one closing
    and, after a "move / send", the last named is where everyone goes; a length
    counts from the first day said, or from today. Returns new slots."""
    today = today or date.today()
    slots = dict(slots)
    named = voice_branch.named_branches(conn, text)
    if len(named) >= 2 and _MOVE_CUE.search(text):
        slots["branch_id"] = named[0]["id"]
        slots["destination_branch_id"] = named[-1]["id"]
    days = date_guard.duration_days(text)
    if days and days > 1 and "end_date" not in slots:
        start = slots.get("appt_date") or today.isoformat()
        slots["appt_date"] = start
        slots["end_date"] = (date.fromisoformat(start) + timedelta(days=days - 1)).isoformat()
        slots.pop("date_unreadable", None)
    return slots


def doctor_branch(conn, doctor_id, iso_date):
    """The branch a doctor is scheduled at on that date, or None when there is
    none (or the doctor is at more than one that day -- then a person chooses)."""
    weekday = date.fromisoformat(iso_date).weekday()
    ids = {row["branch_id"] for row in branches.list_schedule(conn, doctor_id=doctor_id) if row["weekday"] == weekday}
    return next(iter(ids)) if len(ids) == 1 else None
