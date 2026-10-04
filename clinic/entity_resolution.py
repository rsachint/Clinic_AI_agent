from collections import namedtuple
from difflib import SequenceMatcher

from clinic.translit import cross_script_similarity

Candidate = namedtuple("Candidate", ["id", "label", "score"])

# Placeholder scorer: plain string similarity on the ASR hypothesis text.
# The plan (§3 rule 2) calls for Indic phonetic matching (Soundex-style) plus
# edit distance, since ASR output on Indic names is a phonetic guess, not a
# reliable spelling. Swap this out before relying on real Hinglish voice input.
def _similarity(a, b):
    plain = SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()
    # Devanagari speech vs a Roman-letter name (or the reverse) shares no
    # letters, so compare how the names sound instead (clinic/translit.py).
    return max(plain, cross_script_similarity(a, b))


def similarity(a, b):
    """Public alias of the placeholder name scorer, for callers that need to
    compare a heard name with names that aren't patient rows."""
    return _similarity(a, b)


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


def resolve_patient(conn, query, top_n=3):
    query = query.strip()
    query_digits = _normalize_phone(query)
    rows = conn.execute("SELECT id, name, phone FROM patients").fetchall()

    candidates = []
    for row in rows:
        score = _similarity(query, row["name"])
        if query_digits and query_digits == _normalize_phone(row["phone"]):
            score = 1.0
        candidates.append(Candidate(row["id"], "{} ({})".format(row["name"], row["phone"]), score))

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:top_n]


def resolve_staff(conn, query, top_n=3):
    query = query.strip()
    rows = conn.execute("SELECT id, name FROM staff").fetchall()

    candidates = [Candidate(row["id"], row["name"], _similarity(query, row["name"])) for row in rows]
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:top_n]
