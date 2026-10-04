import re
import unicodedata
from datetime import date, timedelta

# Deterministic regex/lookup-table date & time extraction for appointment
# scheduling -- same hard rule as clinic/nlu/extract.py: a date/time is never
# produced by the LLM, only by code here, and the result is still shown as
# an editable field on the review card for a human to correct. Kept as a
# separate module from extract.py (which stays focused on phone/age/amount/
# days) since appointment date/time parsing is a meaningfully bigger, more
# structured job on its own.
#
# Both functions below follow extract.py's established discipline: return
# None rather than guess when a phrasing is genuinely ambiguous. A blank
# field a human fills in on the review card is always safer than a wrong
# guess (see nlu/patient_parser.py's own comment making the same call for
# "din baad").


def _normalize(text):
    return unicodedata.normalize("NFC", text).lower()


# ---------------------------------------------------------------------------
# Date extraction
# ---------------------------------------------------------------------------

_WEEKDAYS = {
    # English
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    # Hinglish
    "somwar": 0, "mangalwar": 1, "budhwar": 2, "guruwar": 3, "brihaspativar": 3,
    "shukrawar": 4, "shaniwar": 5, "ravivar": 6, "etwar": 6,
    # Devanagari
    "सोमवार": 0, "मंगलवार": 1, "बुधवार": 2, "गुरुवार": 3, "वीरवार": 3,
    "शुक्रवार": 4, "शनिवार": 5, "रविवार": 6,
}

_ABS_NUMERIC = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b")

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_ABS_DAY_MONTH_WORD = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)\b")


def _extract_absolute_date(normalized, today):
    m = _ABS_NUMERIC.search(normalized)
    if m:
        day_s, month_s, year_s = m.groups()
        day, month = int(day_s), int(month_s)
        if 1 <= month <= 12 and 1 <= day <= 31:
            try:
                if year_s:
                    year = int(year_s)
                    if len(year_s) == 2:
                        year = 2000 + year
                    return date(year, month, day)
                candidate = date(today.year, month, day)
                if candidate < today:
                    candidate = date(today.year + 1, month, day)
                return candidate
            except ValueError:
                pass

    for m in _ABS_DAY_MONTH_WORD.finditer(normalized):
        day_s, word = m.groups()
        month = _MONTHS.get(word)
        if not month:
            continue
        try:
            candidate = date(today.year, month, int(day_s))
        except ValueError:
            continue
        if candidate < today:
            candidate = date(today.year + 1, month, int(day_s))
        return candidate
    return _extract_worded_date(normalized, today) or _extract_bare_day(normalized, today)


# Spelled-out dates ("fourth of October", "twenty fifth October", "October
# fourth", "chaar october", "4 अक्टूबर", "अक्टूबर 4"). Same discipline as above:
# a miss returns None for the human to fill in, never a guess.
_MONTHS_DEVANAGARI = {
    "जनवरी": 1, "फरवरी": 2, "फ़रवरी": 2, "मार्च": 3, "अप्रैल": 4, "मई": 5, "जून": 6,
    "जुलाई": 7, "अगस्त": 8, "सितंबर": 9, "सितम्बर": 9, "अक्टूबर": 10, "अक्तूबर": 10,
    "नवंबर": 11, "नवम्बर": 11, "दिसंबर": 12, "दिसम्बर": 12,
}
_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11,
    "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
    "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
    "twentieth": 20, "thirtieth": 30,
}
_CARDINALS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30,
}
_HINGLISH_DAYS = {
    "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5,
    "chhe": 6, "chhah": 6, "che": 6, "saat": 7, "aath": 8, "nau": 9, "das": 10,
    "gyarah": 11, "barah": 12, "terah": 13, "chaudah": 14, "pandrah": 15,
    "solah": 16, "satrah": 17, "atharah": 18, "unnis": 19, "bees": 20,
    "ikkis": 21, "baais": 22, "teis": 23, "chaubis": 24, "pachchis": 25,
    "pachis": 25, "chhabbis": 26, "sattais": 27, "atthais": 28, "untis": 29,
    "tees": 30, "ikattis": 31,
}
_DEVANAGARI_DAYS = {
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "पाँच": 5, "छह": 6, "छः": 6,
    "सात": 7, "आठ": 8, "नौ": 9, "दस": 10, "ग्यारह": 11, "बारह": 12, "तेरह": 13,
    "चौदह": 14, "पंद्रह": 15, "सोलह": 16, "सत्रह": 17, "अठारह": 18, "उन्नीस": 19,
    "बीस": 20, "इक्कीस": 21, "बाईस": 22, "तेईस": 23, "चौबीस": 24, "पच्चीस": 25,
    "छब्बीस": 26, "सत्ताईस": 27, "अट्ठाईस": 28, "उनतीस": 29, "तीस": 30, "इकतीस": 31,
}


def _build_day_words():
    words = {}
    words.update(_ORDINALS)
    words.update(_CARDINALS)
    words.update(_HINGLISH_DAYS)
    words.update(_DEVANAGARI_DAYS)
    # "twenty first" / "twenty-fifth" / "twenty five" / "thirty first"
    units_ordinal = {w: n for w, n in _ORDINALS.items() if n <= 9}
    units_cardinal = {w: n for w, n in _CARDINALS.items() if n <= 9}
    for tens_word, tens in (("twenty", 20), ("thirty", 30)):
        for unit_word, unit in list(units_ordinal.items()) + list(units_cardinal.items()):
            words["{} {}".format(tens_word, unit_word)] = tens + unit
    return words


_DAY_WORDS = _build_day_words()
_DAY_ALTERNATION = "|".join(
    re.escape(w).replace(r"\ ", r"[\s-]+")
    for w in sorted(_DAY_WORDS, key=len, reverse=True)
)
_DAY_TOKEN = r"(?:\d{1,2}(?:st|nd|rd|th)?|" + _DAY_ALTERNATION + r")"
_MONTH_ALTERNATION = "|".join(
    sorted(list(_MONTHS) + list(_MONTHS_DEVANAGARI), key=len, reverse=True)
)
# Lookarounds instead of \b: \b misbehaves after Devanagari vowel signs.
_DAY_THEN_MONTH = re.compile(
    r"(?<!\w)(" + _DAY_TOKEN + r")\s+(?:of\s+)?(" + _MONTH_ALTERNATION + r")(?!\w)"
)
_MONTH_THEN_DAY = re.compile(
    r"(?<!\w)(" + _MONTH_ALTERNATION + r")\s+(?:the\s+)?(" + _DAY_TOKEN + r")(?!\w)"
)


def _day_from_token(token):
    token = re.sub(r"[\s-]+", " ", token.strip())
    digits = re.match(r"(\d{1,2})", token)
    if digits:
        return int(digits.group(1))
    return _DAY_WORDS.get(token)


def _month_from_word(word):
    return _MONTHS.get(word) or _MONTHS_DEVANAGARI.get(word)


def _resolve_day_month(day, month, today):
    if not day or not month or not 1 <= day <= 31:
        return None
    try:
        candidate = date(today.year, month, day)
    except ValueError:
        return None
    if candidate < today:
        try:
            candidate = date(today.year + 1, month, day)
        except ValueError:
            return None
    return candidate


def _extract_worded_date(normalized, today):
    for m in _DAY_THEN_MONTH.finditer(normalized):
        resolved = _resolve_day_month(_day_from_token(m.group(1)), _month_from_word(m.group(2)), today)
        if resolved:
            return resolved
    for m in _MONTH_THEN_DAY.finditer(normalized):
        resolved = _resolve_day_month(_day_from_token(m.group(2)), _month_from_word(m.group(1)), today)
        if resolved:
            return resolved
    return None


# A day of the month with NO month named: "7 तारीख", "सात तारीख को", "saat
# tareekh", "7th". The clinic's rule is that this always means the CURRENT
# month (even when that day has already passed -- staff correct it on the
# review card if they meant next month).
_DATE_MARKER = r"(?:तारीख|तारिख|तारीक|tareekh|tarikh|tarik)"
_BARE_DAY_MARKER = re.compile(r"(?<![\w])(" + _DAY_TOKEN + r")\s+" + _DATE_MARKER + r"(?![\w\u0900-\u097F])")
_BARE_DAY_ORDINAL = re.compile(r"(?<![\w])(\d{1,2})(?:st|nd|rd|th)(?!\w)")


def _extract_bare_day(normalized, today):
    for pattern in (_BARE_DAY_MARKER, _BARE_DAY_ORDINAL):
        for m in pattern.finditer(normalized):
            day = _day_from_token(m.group(1))
            if day and 1 <= day <= 31:
                try:
                    return date(today.year, today.month, day)
                except ValueError:
                    continue
    return None


# Unambiguous words that say "a date was named" (no "may"/"mar": ordinary
# English words). Used to refuse to guess when a date was clearly spoken but
# could not be read.
_DATE_REFERENCE = re.compile(
    r"(?<![\w\u0900-\u097F])(?:" + _DATE_MARKER + "|"
    + "|".join(sorted((w for w in list(_MONTHS) + list(_MONTHS_DEVANAGARI) if w not in ("may", "mar", "sep", "sept", "dec", "jan", "feb", "apr", "jun", "jul", "aug", "nov", "oct")), key=len, reverse=True)) + "|"
    + "|".join(sorted(_WEEKDAYS, key=len, reverse=True)) + r")(?![\w\u0900-\u097F])"
)


def mentions_unreadable_date(text, today=None):
    """True when `text` clearly names a date (a month, a weekday or "तारीख")
    that extract_appt_date could not resolve. Callers that would otherwise
    silently fall back to today use this to ask the person to repeat instead."""
    normalized = _normalize(text)
    return bool(_DATE_REFERENCE.search(normalized)) and extract_appt_date(text, today) is None


def _extract_weekday(normalized):
    for word, idx in _WEEKDAYS.items():
        if re.search(r"\b" + re.escape(word) + r"\b", normalized):
            return idx
    return None


def extract_appt_date(text, today=None):
    """Resolve a spoken date reference in `text` to 'YYYY-MM-DD', or None if
    nothing recognizable was said. `today` is injectable for tests; defaults
    to date.today().

    Resolution order (first match wins), and the reasoning for each:

    1. An absolute date ("25 September", "25/09", "25-09-2026") -- most
       specific, so it wins over anything else mentioned in the same
       sentence (e.g. a stray "kal" earlier in a rambling command).
    2. "aaj"/"आज"/"today" -- including "aaj Monday"-style phrasing that
       names a weekday only to confirm what today is; that combination is
       still resolved as *today*, not as "the next Monday".
    3. "kal"/"कल"/"tomorrow". Per this codebase's existing convention (see
       nlu/patient_parser.py's _extract_relative_date), "kal" always means
       tomorrow here, never yesterday -- a look-back date makes no sense
       when booking/rescheduling a future appointment.
    4. A bare weekday name (English/Hindi/Hinglish) with no "aaj" alongside
       it -- resolved as its NEXT UPCOMING occurrence, never today, even if
       today happens to be that weekday. E.g. said on a Monday, "book on
       Monday" resolves to next Monday (7 days out), not today; if today
       was meant, the caller would ordinarily say "today"/"aaj" too (step 2
       above already covers that combination). This is a judgment call on a
       genuinely ambiguous phrasing, not a certainty -- documented here
       rather than silently guessed.

    Returns None (never a guess) when none of the above matched.
    """
    today = today or date.today()
    normalized = _normalize(text)

    absolute = _extract_absolute_date(normalized, today)
    if absolute:
        return absolute.isoformat()

    has_aaj = bool(re.search(r"\b(?:aaj|today)\b", normalized)) or "आज" in normalized
    if has_aaj:
        return today.isoformat()

    has_kal = bool(re.search(r"\b(?:kal|tomorrow)\b", normalized)) or "कल" in normalized
    if has_kal:
        return (today + timedelta(days=1)).isoformat()

    weekday = _extract_weekday(normalized)
    if weekday is not None:
        delta = (weekday - today.weekday()) % 7
        if delta == 0:
            delta = 7  # "next upcoming occurrence, not today" -- see docstring point 4
        return (today + timedelta(days=delta)).isoformat()

    return None


# ---------------------------------------------------------------------------
# Time extraction
# ---------------------------------------------------------------------------

_QUALIFIER_AM = ["सुबह", "subah"]
_QUALIFIER_PM = ["दोपहर", "dopahar", "शाम", "shaam", "sham", "रात", "raat"]

_TIME_COLON = re.compile(r"\b(\d{1,2}):(\d{2})\s*(am|pm|a\.m\.|p\.m\.)?\b")
_TIME_AMPM_NO_COLON = re.compile(r"\b(\d{1,2})\s*(am|pm|a\.m\.|p\.m\.)\b")
# No trailing \b after "बजे": Python's \b treats Devanagari vowel-sign
# characters (matras, Unicode category Mn) as non-word characters, so a
# word ending in one (बजे ends in the े matra) never satisfies a trailing
# \b at end-of-string/before whitespace the way an ASCII word would --
# confirmed live (re.search(r"बजे\b", "2 बजे") is None while
# re.search(r"\bबजे", ...) matches fine). "baje" (the Latin transliteration)
# keeps its trailing \b since ASCII letters don't have this issue.
_TIME_BAJE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(?:baje\b|बजे)")


_HOUR_WORDS = {
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "पाँच": 5, "छह": 6, "छः": 6, "छ": 6,
    "सात": 7, "आठ": 8, "नौ": 9, "दस": 10, "ग्यारह": 11, "बारह": 12,
    "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5,
    "chhe": 6, "chhah": 6, "che": 6, "chah": 6, "saat": 7, "aath": 8, "nau": 9,
    "das": 10, "gyarah": 11, "barah": 12,
}
_HOUR_WORDS = {unicodedata.normalize("NFC", w): n for w, n in _HOUR_WORDS.items()}
_HOUR_WORD_ALT = "|".join(sorted(_HOUR_WORDS, key=len, reverse=True))
_EDGE_BEFORE = r"(?<![\w\u0900-\u097F])"
_BAJE = r"\s*(?:बजे|baje)"
_FRACTIONS = {  # spoken fraction word -> (minutes past the hour, hour offset)
    "साढ़े": (30, 0), "साढे": (30, 0), "saadhe": (30, 0), "sadhe": (30, 0), "sade": (30, 0), "saade": (30, 0), "saarhe": (30, 0),
    "सवा": (15, 0), "sawa": (15, 0), "sava": (15, 0),
    "पौने": (45, -1), "paune": (45, -1), "pone": (45, -1),
}
_FRACTIONS = {unicodedata.normalize("NFC", w): v for w, v in _FRACTIONS.items()}
_FRACTION_ALT = "|".join(sorted(_FRACTIONS, key=len, reverse=True))
_TIME_FRACTION_WORD = re.compile(
    _EDGE_BEFORE + "(" + _FRACTION_ALT + r")\s+(" + _HOUR_WORD_ALT + r"|\d{1,2})" + _BAJE)
_TIME_DEDH_DHAI = re.compile(
    _EDGE_BEFORE + "(" + "|".join(unicodedata.normalize("NFC", w) for w in ("डेढ़", "डेढ", "dedh", "derh", "ढाई", "dhai", "dhaai")) + ")" + _BAJE)
_TIME_WORD_BAJE = re.compile(_EDGE_BEFORE + "(" + _HOUR_WORD_ALT + ")" + _BAJE)


def _extract_worded_time(normalized):
    m = _TIME_FRACTION_WORD.search(normalized)
    if m:
        minutes, offset = _FRACTIONS[m.group(1)]
        token = m.group(2)
        hour = int(token) if token.isdigit() else _HOUR_WORDS[token]
        hour += offset
        if hour == 0:
            hour = 12
        return _resolve_hour(hour, minutes, None, normalized)
    m = _TIME_DEDH_DHAI.search(normalized)
    if m:
        hour = 1 if m.group(1) in ("डेढ़", "डेढ", "dedh", "derh") else 2
        return _resolve_hour(hour, 30, None, normalized)
    m = _TIME_WORD_BAJE.search(normalized)
    if m:
        return _resolve_hour(_HOUR_WORDS[m.group(1)], 0, None, normalized)
    return None


def _resolve_hour(hour, minute, meridiem, normalized):
    if not (0 <= minute <= 59):
        return None
    meridiem = (meridiem or "").replace(".", "")

    if meridiem == "am":
        if not (1 <= hour <= 12):
            return None
        return "{:02d}:{:02d}".format(hour % 12, minute)
    if meridiem == "pm":
        if not (1 <= hour <= 12):
            return None
        return "{:02d}:{:02d}".format((hour % 12) + 12, minute)

    if hour > 12:
        # Already unambiguous 24-hour form (e.g. "14:30") -- no qualifier needed.
        if hour > 23:
            return None
        return "{:02d}:{:02d}".format(hour, minute)

    # "12 baje" is noon in everyday speech, with or without सुबह / दोपहर (a
    # clinic is never booked for midnight at 12 in the morning). Only रात
    # (night) makes it midnight. Plain "12 am" / "12 pm" are handled above.
    if hour == 12:
        if any(q in normalized for q in ("रात", "raat")):
            return "00:{:02d}".format(minute)
        return "12:{:02d}".format(minute)

    if any(q in normalized for q in _QUALIFIER_AM):
        return "{:02d}:{:02d}".format(hour % 12, minute)
    if any(q in normalized for q in _QUALIFIER_PM):
        return "{:02d}:{:02d}".format((hour % 12) + 12, minute)

    # No am/pm marker and no सुबह/दोपहर/शाम/रात qualifier anywhere in the
    # sentence: rather than guess AM vs PM, take the stated hour literally
    # (so "11 baje" -> 11:00, matching how "11am" would also resolve to
    # 11:00 -- see this module's docstring section). This is a deliberate,
    # documented default, not cleverness: clinic hours or extra context
    # aren't consulted here to disambiguate.
    return "{:02d}:{:02d}".format(hour, minute)


def extract_appt_time(text):
    """Resolve a spoken time reference in `text` to 24-hour 'HH:MM', or None
    if nothing recognizable was said.

    Handles explicit times ("11 baje", "11am", "3:30 pm") and Hindi
    time-of-day qualifiers (सुबह=morning, दोपहर=afternoon, शाम=evening,
    रात=night) combined with an hour to disambiguate AM/PM, e.g.
    "शाम 4 बजे" -> "16:00", "सुबह 4 बजे" -> "04:00". A bare hour+baje with no
    am/pm and no qualifier at all is taken literally (see _resolve_hour) --
    this function never invents an AM/PM guess from context alone.
    """
    normalized = _normalize(text)

    m = _TIME_COLON.search(normalized)
    if m:
        return _resolve_hour(int(m.group(1)), int(m.group(2)), m.group(3), normalized)

    m = _TIME_AMPM_NO_COLON.search(normalized)
    if m:
        return _resolve_hour(int(m.group(1)), 0, m.group(2), normalized)

    m = _TIME_BAJE.search(normalized)
    if m:
        minute = int(m.group(2)) if m.group(2) else 0
        return _resolve_hour(int(m.group(1)), minute, None, normalized)

    return _extract_worded_time(normalized)
