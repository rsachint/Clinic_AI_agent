import re

from clinic.nlu.hindi_numbers import words_to_number

# Numbers are pulled with regex, never asked of the LLM (plan §5: "numbers/dates
# via regex — never via the LLM"). These are best-effort hints for the proposal
# screen, not trusted writes — the human still confirms every value.

_DIGIT_RUN = re.compile(r"[\d](?:[\d ]{6,}[\d])?")


def extract_phone(text):
    """Longest run of 8-10 digits, spaces stripped. May be short (ASR drops
    digits on long sequences) -- the proposal screen must show it for the
    human to correct, never write it straight through."""
    best = None
    for match in _DIGIT_RUN.finditer(text):
        digits = match.group().replace(" ", "")
        if 8 <= len(digits) <= 10 and (best is None or len(digits) > len(best)):
            best = digits
    return best


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
