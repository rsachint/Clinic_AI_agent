import re
import unicodedata

from clinic.nlu.hindi_numbers import words_to_number

# Numbers are pulled with regex, never asked of the LLM (plan §5: "numbers/dates
# via regex — never via the LLM"). These are best-effort hints for the proposal
# screen, not trusted writes — the human still confirms every value.

_DIGIT_RUN = re.compile(r"[\d](?:[\d ]{6,}[\d])?")


def extract_phone(text):
    """Longest run of 8-10 digits, spaces stripped. May be short (ASR drops
    digits on long sequences) -- the proposal screen must show it for the
    human to correct, never write it straight through. A complete 10-digit number
    is also read when it is said in words ("nine eight seven ...") or with a
    +91 / 0 prefix (read_phone_exact); that exact reading wins over a short run."""
    best = None
    for match in _DIGIT_RUN.finditer(text):
        digits = match.group().replace(" ", "")
        if 8 <= len(digits) <= 10 and (best is None or len(digits) > len(best)):
            best = digits
    if best is not None and len(best) == 10:
        return best
    return read_phone_exact(text) or best


# Digits said one by one, the way phone numbers are read out (English, Hinglish, Devanagari).
_DIGIT_WORDS = {
    "zero": 0, "oh": 0, "shunya": 0, "sunya": 0, "शून्य": 0, "ज़ीरो": 0, "जीरो": 0,
    "one": 1, "ek": 1, "एक": 1,
    "two": 2, "do": 2, "दो": 2,
    "three": 3, "teen": 3, "तीन": 3,
    "four": 4, "char": 4, "chaar": 4, "चार": 4,
    "five": 5, "paanch": 5, "panch": 5, "pach": 5, "पांच": 5, "पाँच": 5,
    "six": 6, "chhe": 6, "chhah": 6, "che": 6, "cheh": 6, "छह": 6, "छः": 6, "छे": 6,
    "seven": 7, "saat": 7, "sat": 7, "सात": 7,
    "eight": 8, "aath": 8, "ath": 8, "आठ": 8,
    "nine": 9, "nau": 9, "nao": 9, "नौ": 9,
}
_REPEATS = {"double": 2, "dubal": 2, "डबल": 2, "triple": 3, "ट्रिपल": 3}
_PHONE_TOKEN = re.compile(r"[\w\u0900-\u097F+]+")


def _ten_digits(number):
    """The 10 digits of `number` (as read out: 10 digits, or 11 with a leading 0, or 12 with a leading 91),
    or None. Fewer or more is not a phone number: it is never guessed at."""
    if len(number) == 10 or (len(number) == 11 and number[0] == "0") or (len(number) == 12 and number[:2] == "91"):
        return number[-10:]
    return None


def spoken_phone(text):
    """The 10-digit phone number in an answer like "98765 00301", "+91 9876500301", "९८७६५००३०१" or digits read
    out one by one ("nine eight seven six ..."), or None when there is no such number. Fewer or more than
    10 digits (after a +91 / 0 prefix) is not a phone number: speech recognition drops digits on long
    sequences, so a short run is asked again rather than guessed at.
    This is the reader for the ANSWER to "What is the patient's phone number?": every digit in the answer
    counts, whatever else is said around it. For a whole command see phone_in_words."""
    digits, repeat = [], 1
    for token in _PHONE_TOKEN.findall(unicodedata.normalize("NFC", text or "").lower()):
        if token in _REPEATS:
            repeat = _REPEATS[token]
            continue
        if token in _DIGIT_WORDS:
            digits.extend([str(_DIGIT_WORDS[token])] * repeat)
        else:
            # a run of digits (any script's); anything else said around it is ignored
            digits.extend(str(unicodedata.digit(ch)) for ch in token if unicodedata.category(ch) == "Nd")
        repeat = 1
    return _ten_digits("".join(digits))


def _digit_runs(text):
    """The digits of each unbroken run of digit words / digit tokens in `text` ("nine eight 7 6 double five"),
    as strings. A run is broken by any other word, so the hour in "at five pm" or the day in "15 October"
    is never joined to a number said elsewhere in the sentence. "double"/"triple" repeat the next single
    digit; a "plus" opening a run (+91) is skipped."""
    runs, digits, repeat = [], [], 1

    def close():
        if digits:
            runs.append("".join(digits))
        digits.clear()

    for token in _PHONE_TOKEN.findall(unicodedata.normalize("NFC", text or "").lower()):
        if token == "plus" and not digits:
            repeat = 1
            continue
        if token in _REPEATS:
            if repeat != 1:
                close()
            repeat = _REPEATS[token]
            continue
        if token in _DIGIT_WORDS:
            digits.extend([str(_DIGIT_WORDS[token])] * repeat)
        else:
            number = token.lstrip("+")
            if number and all(unicodedata.category(ch) == "Nd" for ch in number):
                converted = [str(unicodedata.digit(ch)) for ch in number]
                if repeat != 1 and len(converted) != 1:
                    close()                         # "double 98765": not a repeat
                else:
                    digits.extend(converted * repeat)
            else:
                close()
        repeat = 1
    close()
    return runs


def phone_in_words(text):
    """A complete phone number said inside a command, digits one by one in English, Hinglish or Devanagari
    ("nine eight seven six five four three two one zero", "double nine ...", "plus nine one ..."), or None.
    Exactly 10 digits (after a +91 / 0 prefix) in ONE unbroken run, else None: a short run such as the
    "five" of "five pm" is ignored and nothing is ever guessed from fewer or more digits. When two
    different numbers are said, None."""
    found = {_ten_digits(run) for run in _digit_runs(text)} - {None}
    return found.pop() if len(found) == 1 else None


def read_phone_exact(text):
    """The one complete 10-digit phone number in `text` (digits, a +91 / 0 prefix, or said in words), or
    None. The deterministic reading a model's phone never outranks."""
    runs = {m.group().replace(" ", "") for m in _DIGIT_RUN.finditer(text or "")}
    exact = {r for r in runs if len(r) == 10}
    if len(exact) > 1:
        return None
    return exact.pop() if exact else phone_in_words(text)


# "34 साल" / "40 years" -- number before the unit word. A negative lookbehind
# keeps this from matching a trailing fragment of an unrelated digit run (seen
# live: "...mobile number is 9876500401 age is 59" matched "401" here, the
# last 3 digits of the phone number, since they sit right before "age").
_AGE_DIGIT_BEFORE = re.compile(r"(?<!\d)(\d{1,3})\s*(?:साल|years?|yrs?|age)", re.IGNORECASE)
# "age is 59" / "age: 59" -- unit word before the number, seen live from Saaras
# phrasing "age" doesn't always come right after the number.
_AGE_DIGIT_AFTER = re.compile(r"\bage\b\s*(?:is|of)?\s*:?\s*(?<!\d)(\d{1,3})(?!\d)", re.IGNORECASE)
_AGE_WORD = re.compile(r"([^\s,]+)\s*साल")


def extract_age(text):
    m = _AGE_DIGIT_BEFORE.search(text)
    if m:
        return int(m.group(1))
    m = _AGE_DIGIT_AFTER.search(text)
    if m:
        return int(m.group(1))
    m = _AGE_WORD.search(text)
    if m:
        return words_to_number(m.group(1))
    return None


_AMOUNT_SUFFIX = re.compile(r"(\d+(?:\.\d+)?)\s*(?:rupees?|rupaye|रुपये|रुपए|rs\.?)", re.IGNORECASE)
# Saaras sometimes transcribes an amount as a "₹" prefix (e.g. "₹500")
# instead of a trailing word -- seen live on "consultation ₹500."
_AMOUNT_SYMBOL = re.compile(r"[₹](\d+(?:\.\d+)?)")


def extract_amount(text):
    m = _AMOUNT_SUFFIX.search(text) or _AMOUNT_SYMBOL.search(text)
    return float(m.group(1)) if m else None


_DAYS = re.compile(r"(\d+)\s*(?:din|days?|दिन)", re.IGNORECASE)


def extract_days(text):
    m = _DAYS.search(text)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Queue token numbers ("token 5", "टोकन नंबर पांच", "T-05", "5 number token")
# ---------------------------------------------------------------------------
# Same discipline as everything above: a regex/lookup, never the LLM, and a
# miss returns None for the review card's dropdown to settle -- not a guess.

_SPOKEN_NUMBERS = {
    "ek": 1, "one": 1, "do": 2, "two": 2, "teen": 3, "three": 3,
    "char": 4, "chaar": 4, "four": 4, "paanch": 5, "panch": 5, "five": 5,
    "chhe": 6, "chhah": 6, "che": 6, "chhey": 6, "six": 6, "saat": 7, "seven": 7,
    "aath": 8, "eight": 8, "nau": 9, "nine": 9, "das": 10, "ten": 10,
    "gyarah": 11, "eleven": 11, "barah": 12, "twelve": 12, "terah": 13, "thirteen": 13,
    "chaudah": 14, "fourteen": 14, "pandrah": 15, "fifteen": 15, "solah": 16, "sixteen": 16,
    "satrah": 17, "seventeen": 17, "atharah": 18, "eighteen": 18, "unnis": 19, "nineteen": 19,
    "bees": 20, "twenty": 20,
}
_TOKEN_THEN_NUMBER = re.compile(
    r"(?:token|टोकन)\s*(?:number|no\.?|num|नंबर|नम्बर)?\s*[:#-]?\s*(?:(?:is|hai|है)\s+)?(\S+)", re.IGNORECASE
)
_NUMBER_THEN_TOKEN = re.compile(
    r"(\S+)\s*(?:number|नंबर|नम्बर)\s*(?:wala|wali|वाला|वाली|ka|ki|का|की)?\s*(?:token|टोकन)", re.IGNORECASE
)
_T_DASH = re.compile(r"\bt-(\d{1,3})\b", re.IGNORECASE)


def _spoken_to_int(word):
    word = word.strip(".,?!।:;").lower()
    if word.isdigit():
        return int(word) if 0 < int(word) < 1000 else None
    if word in _SPOKEN_NUMBERS:
        return _SPOKEN_NUMBERS[word]
    if any("\u0900" <= ch <= "\u097f" for ch in word):
        return words_to_number(word)
    return None


def extract_token_number(text):
    """The queue token number mentioned in `text`, or None."""
    m = _T_DASH.search(text)
    if m:
        return int(m.group(1))
    for pattern in (_TOKEN_THEN_NUMBER, _NUMBER_THEN_TOKEN):
        for m in pattern.finditer(text):
            number = _spoken_to_int(m.group(1))
            if number:
                return number
    return None
