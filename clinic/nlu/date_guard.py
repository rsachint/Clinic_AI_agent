"""The planner's date / time safety net.

A local model can be a week off on "next Monday" and has no calendar of its own.
The codebase's rule has always been that a date or time is read by code
(clinic/nlu/datetime_extract.py), so after the planner answers, `crosscheck`
compares what it produced with what the extractor reads from the same words:

  * the extractor can read the phrase and the model disagrees -> the
    extractor's value wins (and the override is returned as a note for the log);
  * the extractor cannot read the phrase ("parso", "next week") -> the model's
    value is kept;
  * more than one date (or time) phrase in the sentence -> nothing is
    overridden: the extractor reads only the first, so it cannot say which
    field a value belongs to. The one exception is a range for a closure or
    leave ("from Monday to Wednesday"), read in order, the end counted from
    the start.

"This month" / "last month" / "next month" ("is mahine", "pichle mahine") are read here too:
for a read they always mean the whole calendar month, first day to last (date..date_to).

It also owns the closure-length rule: "closed for the next one week" is
N days counted from the first day said, or from today when none was said, so
end = start + N - 1.
"""

import re
import unicodedata
from datetime import date, timedelta

from clinic import query_tool
from clinic.nlu import datetime_extract as dx
from clinic.voice_context import _AT_HOUR, clinic_hour

MAX_RANGE_DAYS = 31

# Tools and the date / time fields they carry. "primary" is the field a single
# date phrase in the sentence belongs to; for a range tool, "start" / "end".
_SINGLE_DATE = {
    "book_appointment": "date",
    "reschedule_appointment": "new_date",
    "reschedule_followup": "new_date",
    "query": "date",
}
_RANGE_DATE = {"close_branch": ("start_date", "end_date"), "doctor_leave": ("start_date", "end_date")}
_TIME_FIELD = {"book_appointment": "time", "reschedule_appointment": "new_time"}


def _norm(text):
    return unicodedata.normalize("NFC", text or "").lower()


# -- finding the phrases ---------------------------------------------------------

_EDGE = r"(?<![\wऀ-ॿ])"
_EDGE_END = r"(?![\wऀ-ॿ])"
_RELATIVE = re.compile(
    _EDGE + r"(?:day\s+after\s+tomorrow|aaj|today|kal|tomorrow|parso|parson|parsoon|आज|कल|परसों|परसो)" + _EDGE_END)
_WEEKDAY = re.compile(_EDGE + "(?:" + "|".join(sorted((re.escape(w) for w in dx._WEEKDAYS), key=len, reverse=True)) + ")" + _EDGE_END)
_ABSOLUTE = (dx._ABS_NUMERIC, dx._DAY_THEN_MONTH, dx._MONTH_THEN_DAY, dx._BARE_DAY_MARKER, dx._BARE_DAY_ORDINAL)
_DAY_MONTH_WORD = dx._ABS_DAY_MONTH_WORD


def _spans(text):
    """Every date phrase in `text`, as sorted, merged (start, end) spans."""
    found = []
    for pattern in (_RELATIVE, _WEEKDAY) + _ABSOLUTE:
        found += [m.span() for m in pattern.finditer(text)]
    for m in _DAY_MONTH_WORD.finditer(text):
        if m.group(2) in dx._MONTHS:
            found.append(m.span())
    found.sort()
    merged = []
    for start, end in found:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def date_phrases(text, today=None):
    """[(phrase, iso or None)] for each date phrase in `text`, in order: the ISO
    date the extractor reads from that phrase alone, or None when it cannot."""
    today = today or date.today()
    normalized = _norm(text)
    out = []
    for a, b in _spans(normalized):
        phrase = normalized[a:b]
        # The extractor reads the "tomorrow" inside "day after tomorrow" and says
        # tomorrow: that one is not readable, so the model's value stands.
        iso = None if re.search(r"day\s+after|parso|parson|परसों|परसो", phrase) else dx.extract_appt_date(phrase, today)
        out.append((phrase, iso))
    return out


_TIME_PATTERNS = (dx._TIME_COLON, dx._TIME_AMPM_NO_COLON, dx._TIME_BAJE, dx._TIME_FRACTION_WORD,
                  dx._TIME_DEDH_DHAI, dx._TIME_WORD_BAJE, dx._TIME_EN_WORDS, dx._TIME_EN_FRACTION, _AT_HOUR)


def time_phrase_count(text):
    normalized = _norm(text)
    spans = sorted(m.span() for pattern in _TIME_PATTERNS for m in pattern.finditer(normalized))
    count, last_end = 0, -1
    for start, end in spans:
        if start >= last_end:
            count += 1
            last_end = end
        else:
            last_end = max(last_end, end)
    return count


def read_time(text):
    """The time the extractor reads from `text` (a bare "at 5" counts, as in the
    rest of the voice flow), or None."""
    found = dx.extract_appt_time(text)
    if found:
        return found
    m = _AT_HOUR.search(_norm(text))
    return clinic_hour(int(m.group(1))) if m else None


# -- "for the next one week" ---------------------------------------------------

_COUNT_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "ten": 10, "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "chhe": 6, "saat": 7,
                "das": 10, "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "पाँच": 5, "छह": 6, "सात": 7, "दस": 10}
_UNITS = {"day": 1, "days": 1, "din": 1, "दिन": 1, "week": 7, "weeks": 7, "hafta": 7, "hafte": 7, "haftey": 7,
          "हफ्ता": 7, "हफ्ते": 7, "हफ़्ता": 7, "हफ़्ते": 7, "सप्ताह": 7}
_DURATION = re.compile(
    _EDGE + r"(?:(?:for|next|coming|agle|agli|aane\s+wale|aane\s+wali|अगले|अगली|आने\s+वाले)\s+(?:the\s+)?)?"
    r"(\d{1,2}|" + "|".join(sorted((re.escape(w) for w in _COUNT_WORDS), key=len, reverse=True)) + r")\s*"
    r"(" + "|".join(sorted((re.escape(w) for w in _UNITS), key=len, reverse=True)) + r")" + _EDGE_END, re.IGNORECASE)


def duration_days(text):
    """How many days a closure phrase covers: "for 3 days", "next one week",
    "agle do hafte", "5 din" -> 3 / 7 / 14 / 5. None when there is no such phrase
    ("next week" alone is not a length). A bare "one week" with no for / next in
    front also counts only when a number word leads it."""
    normalized = _norm(text)
    for m in _DURATION.finditer(normalized):
        lead = normalized[:m.start(1)]
        explicit = bool(re.search(
            r"(?:for|next|coming|agle|agli|aane\s+wale|aane\s+wali|अगले|अगली|आने\s+वाले)\s+(?:the\s+)?$", lead))
        number = m.group(1)
        count = int(number) if number.isdigit() else _COUNT_WORDS.get(number)
        unit = _UNITS.get(m.group(2))
        if not count or not unit:
            continue
        # "for 3 days", "next one week", "5 din": a number then a unit. A bare "a day"
        # / "one week" inside other words ("one week old") needs the for / next lead.
        if number.isdigit() or explicit or m.group(2) in ("din", "दिन"):
            return min(count * unit, MAX_RANGE_DAYS)
    return None


_MERIDIEM_WORD = re.compile(
    r"(?<![a-z])(?:a\.?m\.?|p\.?m\.?|am|pm|morning|afternoon|evening|night)(?![a-z])|सुबह|दोपहर|शाम|रात|subah|dopahar|shaam|sham|raat",
    re.IGNORECASE)


def _same_hour_other_half(found, model_value, text):
    """"3 baje" with nothing about morning or evening: the extractor takes the hour literally
    (03:00) and a model that read it in clinic hours (15:00) is the better reading. Where the
    two differ only by twelve hours and the words give no am / pm, the model's value stands."""
    if not model_value or _MERIDIEM_WORD.search(_norm(text)):
        return False
    to_minutes = lambda value: int(value[:2]) * 60 + int(value[3:5])
    return abs(to_minutes(found) - to_minutes(model_value)) == 12 * 60


# -- "this month" / "last month" ---------------------------------------------------
# A month phrase is a whole calendar month: first day to last day. The extractor above reads
# single days, so months are read here, by code, and the result wins over the model's dates.

_MONTH_WORD = r"(?:month|mahine|mahina|maheene|mahinay|महीने|महीना|माह)"
_THIS_MONTH = re.compile(_EDGE + r"(?:this\s+month|iss?\s+" + _MONTH_WORD + r"|इस\s+" + _MONTH_WORD + r"|current\s+month)" + _EDGE_END)
_LAST_MONTH = re.compile(
    _EDGE + r"(?:last\s+month|previous\s+month|pichh?l[ea]\s+" + _MONTH_WORD + r"|पिछले\s+" + _MONTH_WORD + r")" + _EDGE_END)
_NEXT_MONTH = re.compile(
    _EDGE + r"(?:next\s+month|agle\s+" + _MONTH_WORD + r"|aane\s+wale\s+" + _MONTH_WORD + r"|अगले\s+" + _MONTH_WORD + r")" + _EDGE_END)


def month_phrase(text):
    """-1 (last month), 0 (this month) or 1 (next month) when the sentence names exactly
    one of them, else None."""
    normalized = _norm(text)
    found = {offset for offset, pattern in ((0, _THIS_MONTH), (-1, _LAST_MONTH), (1, _NEXT_MONTH)) if pattern.search(normalized)}
    return found.pop() if len(found) == 1 else None


def month_range(offset, today):
    """(first day, last day) ISO dates of the month `offset` months from today's."""
    index = today.year * 12 + today.month - 1 + offset
    first = date(index // 12, index % 12 + 1, 1)
    following = date((index + 1) // 12, (index + 1) % 12 + 1, 1)
    return first.isoformat(), (following - timedelta(days=1)).isoformat()


# -- the cross-check ---------------------------------------------------------------

def crosscheck(tool, args, text, today=None):
    """(args, notes): `args` with dates and times the extractor can read from
    `text` taking precedence over the model's, and one human-readable note per
    override. Never invents a value the extractor cannot read, never touches a
    field of another kind, and does nothing when the phrase count is ambiguous."""
    today = today or date.today()
    args = dict(args)
    notes = []

    phrases = date_phrases(text, today)
    readable = [iso for _, iso in phrases]

    primary = _SINGLE_DATE.get(tool)
    if primary and len(phrases) == 1 and readable[0]:
        if args.get(primary) != readable[0]:
            notes.append("{} {} -> {} (read from {!r})".format(primary, args.get(primary), readable[0], phrases[0][0]))
            args[primary] = readable[0]

    if tool in _RANGE_DATE:
        start_field, end_field = _RANGE_DATE[tool]
        if len(phrases) == 1 and readable[0]:
            if args.get(start_field) != readable[0]:
                notes.append("{} {} -> {} (read from {!r})".format(start_field, args.get(start_field), readable[0], phrases[0][0]))
                args[start_field] = readable[0]
        elif len(phrases) == 2 and (readable[0] or (readable[1] and args.get(start_field))):
            # the first day may be one the extractor cannot read ("parso"): the model's start then
            # stands, and the last day is still counted from it ("parso se Friday tak")
            start = readable[0] or args.get(start_field)
            end = dx.extract_appt_date(phrases[1][0], date.fromisoformat(start)) if start else None
            if readable[0] and args.get(start_field) != start:
                notes.append("{} {} -> {} (read from {!r})".format(start_field, args.get(start_field), start, phrases[0][0]))
                args[start_field] = start
            if start and end and end > start and args.get(end_field) != end:
                notes.append("{} {} -> {} (read from {!r})".format(end_field, args.get(end_field), end, phrases[1][0]))
                args[end_field] = end
        days = duration_days(text)
        if days and days > 1 and not (len(phrases) == 2 and all(readable)):
            start = args.get(start_field) if phrases else None
            if not phrases:
                start = today.isoformat()           # no day said: the length counts from today
            if start:
                end = (date.fromisoformat(start) + timedelta(days=days - 1)).isoformat()
                if args.get(start_field) != start:
                    notes.append("{} {} -> {} (no day said: counted from today)".format(start_field, args.get(start_field), start))
                    args[start_field] = start
                if args.get(end_field) != end:
                    notes.append("{} {} -> {} ({} days from {})".format(end_field, args.get(end_field), end, days, start))
                    args[end_field] = end

    if tool == "query" and not phrases:
        offset = month_phrase(text)
        allowed = query_tool.ENTITY_FILTERS.get(args.get("entity"), ())
        if offset is not None and "date" in allowed and "date_to" in allowed:
            first, last = month_range(offset, today)
            if args.get("date") != first or args.get("date_to") != last:
                notes.append("date {} -> {}, date_to {} -> {} (a month phrase in the transcript)".format(
                    args.get("date"), first, args.get("date_to"), last))
                args["date"], args["date_to"] = first, last

    time_field = _TIME_FIELD.get(tool)
    if time_field and time_phrase_count(text) == 1:
        found = read_time(text)
        if found and args.get(time_field) != found and not _same_hour_other_half(found, args.get(time_field), text):
            notes.append("{} {} -> {} (read from the transcript)".format(time_field, args.get(time_field), found))
            args[time_field] = found
    return args, notes
