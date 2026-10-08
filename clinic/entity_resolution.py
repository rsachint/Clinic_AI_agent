import re
import unicodedata
from collections import namedtuple

from clinic.translit import has_devanagari, to_latin

Candidate = namedtuple("Candidate", ["id", "label", "score"])

# Names and phone numbers are matched EXACTLY: a score is 1.0 (it is that person) or 0.0 (it is not),
# never in between. A spelling the speech recogniser got slightly wrong is a miss, not a near-match.
_TITLES = {"mr", "mrs", "ms", "miss", "mister", "dr", "shri", "shree", "sri", "smt", "श्री", "श्रीमती", "डॉ", "डा"}


def _words(name):
    """The words of a name, case-folded, without punctuation or a leading title (Mr, Dr, Shri ...)."""
    text = unicodedata.normalize("NFC", name or "").casefold()
    text = "".join(ch if unicodedata.category(ch)[0] in "LMN" else " " for ch in text)
    words = text.split()
    while len(words) > 1 and words[0] in _TITLES:
        words = words[1:]
    return words


def _sound_form(word):
    """A Devanagari or Roman word as plain Roman letters with long vowels shortened (the written form of a
    spoken name differs by script: Dua / दुआ). Only used when the two names are in different scripts."""
    return re.sub(r"([aeiou])\1+", r"\1", to_latin(word).casefold())


def name_match(spoken, stored):
    """1.0 when `spoken` is exactly `stored` (same words, any case), or is a single word equal to the
    stored name's first word ("Amit" for "Amit Dua"); otherwise 0.0. Never a part of a word
    ("Ami" is not "Amit") and never a different spelling. A Devanagari name is compared with a Roman one
    by its Roman spelling (अमित = Amit), still exactly."""
    a, b = _words(spoken), _words(stored)
    if not a or not b:
        return 0.0
    if has_devanagari(spoken) != has_devanagari(stored):
        a, b = [_sound_form(w) for w in a], [_sound_form(w) for w in b]
    if a == b or (len(a) == 1 and a[0] == b[0]):
        return 1.0
    return 0.0


def similarity(a, b):
    """The name score used everywhere a heard name is compared with a written one: exactly 1.0 or 0.0
    (see name_match). Kept under this name for the callers that compare with names that aren't patient rows."""
    return name_match(a, b)


def _normalize_phone(phone):
    return "".join(ch for ch in phone if ch.isdigit())


def last10_digits(phone):
    """India-specific heuristic, not a general E.164 solution: assumes a
    10-digit local number optionally preceded by a country code, and compares
    on the last 10 digits. A WhatsApp wa_id like "919876543210" and a stored
    patients.phone of "9876543210" both reduce to the same value this way."""
    digits = _normalize_phone(phone)
    return digits[-10:] if len(digits) >= 10 else digits


def resolve_patient_by_phone(conn, wa_id):
    """Exact match only, unlike resolve_patient's fuzzy scoring -- a phone
    number is a hard identifier, not something to guess at. Returns the
    matching Candidate or None (never raises)."""
    target = last10_digits(wa_id)
    if not target:
        return None
    rows = conn.execute("SELECT id, name, phone FROM patients").fetchall()
    for row in rows:
        if last10_digits(row["phone"]) == target:
            return Candidate(row["id"], "{} ({})".format(row["name"], row["phone"]), 1.0)
    return None


def full_number(text):
    """The 10-digit phone number written in `text` (a +91 or 0 prefix is dropped), or ''. A run of any other
    length is not a usable number: it is never matched on, and never cut down to 10 digits."""
    digits = _normalize_phone(text or "")
    if len(digits) == 12 and digits.startswith("91"):
        return digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        return digits[1:]
    return digits if len(digits) == 10 else ""


def _candidate(row):
    return Candidate(row["id"], "{} ({})".format(row["name"], row["phone"]), 1.0)


def resolve_patient(conn, query, top_n=3, phone=None, patient_id=None):
    """The registered patients that are exactly who was meant, each with score 1.0 (none when nobody is).
    1. `patient_id`, when the person was already chosen, is that patient.
    2. A 10-digit phone number, given as `phone` or written in `query`, is matched first and exactly
       (last 10 digits). Whoever has that number is the answer; if several people share it, the spoken
       name picks among them when it can. A number that nobody has matches nobody: it is not
       "corrected" to a similar name. A number that is not 10 digits is ignored.
    3. Otherwise the name: the full name, or a single word equal to a patient's first name. Several patients
       can match (two called Amit): all of them are returned so the caller can ask which one."""
    query = (query or "").strip()
    rows = conn.execute("SELECT id, name, phone FROM patients ORDER BY id").fetchall()

    if patient_id is not None:
        return [_candidate(row) for row in rows if str(row["id"]) == str(patient_id)][:1]

    number = full_number(phone) or full_number(query)
    if number:
        said = "".join(ch for ch in query if not ch.isdigit() and ch != "+").strip()
        found = [row for row in rows if last10_digits(row["phone"]) == number]
        if len(found) > 1 and said:
            found = [row for row in found if name_match(said, row["name"]) == 1.0] or found
        return [_candidate(row) for row in found][:top_n]

    return [_candidate(row) for row in rows if name_match(query, row["name"]) == 1.0][:top_n]


def resolve_staff(conn, query, top_n=3):
    """The staff members exactly named (full name, or the first name alone), score 1.0; none when nobody is."""
    query = (query or "").strip()
    rows = conn.execute("SELECT id, name FROM staff ORDER BY id").fetchall()
    return [Candidate(row["id"], row["name"], 1.0) for row in rows if name_match(query, row["name"]) == 1.0][:top_n]
