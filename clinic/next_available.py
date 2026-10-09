"""The "next available appointment" read, and the "...and book it for <name>" tail of the same sentence.

Everything here is deterministic and READ-ONLY: no model, no write, no id from the model.

  * `search` walks forward day by day from a start date and returns the first free slots, using the same slot
    rules the booking card and the calendar use (clinic/scheduling.py generate_slots: the doctor's schedule at that
    branch, closures and booking blocks, existing bookings). For today it skips the times that have already
    passed (the clock is injected: `now`). Defaults and caps: DEFAULT_DAYS / MAX_DAYS days, 1 slot by default,
    MAX_SLOTS at most (the numbers live in clinic/query_tool.py, the whitelist).
  * `resolve_doctor` turns a spoken doctor ("Dr. Mehta", "Mehta", "डॉक्टर मेहता") into exactly one doctor, or says
    there is none or several: the caller then ASKS, it never guesses.
  * `read_sentence` fills what the plain words say when the planner did not (the "next / earliest / first
    available" cue, how many, the doctor, and a trailing "and book it for <name>"). A planner's value wins.
  * `read_answer` reads the short reply to the follow-up question ("Book Neha Gupta on Fri 9 Oct at 10:30 with
    Dr. Mehta?"): yes / another time / no. It only ever builds a booking REVIEW CARD (the normal one); Approve on
    the card is the one and only write.
"""

import re
import unicodedata
from datetime import date, datetime, timedelta

from clinic import branches, entity_resolution, query_tool, scheduling
from clinic.nlu import datetime_extract
from clinic.translit import has_devanagari, phonetic_key

DEFAULT_DAYS = query_tool.DEFAULT_SEARCH_DAYS
MAX_DAYS = query_tool.MAX_SEARCH_DAYS
MAX_SLOTS = query_tool.MAX_SLOTS

_EDGE_L = r"(?<![\wऀ-ॿ])"
_EDGE_R = r"(?![\wऀ-ॿ])"
_WORD = r"[^\s,.;:!?।\"'()]+"


def _norm(text):
    return unicodedata.normalize("NFC", text or "")


def _tokens(text):
    return re.findall(r"[\wऀ-ॿ]+", _norm(text).casefold())


def short_day(iso):
    """'Fri 9 Oct': unambiguous and the same in English and Hindi (the wording of the other day labels)."""
    day = date.fromisoformat(iso)
    return "{} {} {}".format(day.strftime("%a"), day.day, day.strftime("%b"))


# -- the doctor ---------------------------------------------------------------------------

_TITLE_WORDS = frozenset(("dr", "doctor", "डॉक्टर", "डॉ", "डा", "doc"))


def _doctor_words(name):
    return [w for w in _tokens(name) if w not in _TITLE_WORDS]


def _same_word(spoken, stored):
    if entity_resolution.name_match(spoken, stored) == 1.0:
        return True
    # a Devanagari spelling of a Roman name (मेहता / Mehta): the same consonant skeleton, and only ever used
    # when it picks one doctor (resolve_doctor)
    if has_devanagari(spoken) != has_devanagari(stored):
        key = phonetic_key(spoken)
        return len(key) >= 2 and key == phonetic_key(stored)
    return False


def resolve_doctor(conn, spoken):
    """("one", [doctor]) | ("none", []) | ("several", [doctors]) for a spoken doctor. Every spoken word (the title
    "Dr" / "Doctor" does not count) must be a word of the doctor's name: "Mehta" is Dr. Mehta, "Sharma" is
    every Dr. ... Sharma. A full name that equals the spoken words outranks a longer name that merely contains
    them ("Dr. Mehta" is not also "Dr. Anil Mehta")."""
    wanted = _doctor_words(spoken)
    if not wanted:
        return "none", []
    found = []
    for doctor in branches.list_doctors(conn):
        words = _doctor_words(doctor["name"])
        if words and all(any(_same_word(w, s) for s in words) for w in wanted):
            found.append(doctor)
    exact = [d for d in found if len(_doctor_words(d["name"])) == len(wanted)]
    if exact and len(found) > len(exact):
        found = exact
    if not found:
        return "none", []
    return ("one" if len(found) == 1 else "several"), found


_TITLED = re.compile(_EDGE_L + r"(?:doctor|dr|डॉक्टर|डॉ|डा)\.?\s+(" + _WORD + r")(?:\s+(" + _WORD + r"))?", re.IGNORECASE)
_WITH_CUE = re.compile(
    _EDGE_L + r"(?:with|ke\s+saath|ke\s+sath|के\s+साथ)\s+(" + _WORD + r")|(" + _WORD + r")\s+(?:ke\s+saath|ke\s+sath|ke\s+paas|के\s+साथ|के\s+पास)",
    re.IGNORECASE)

# Words that are not a name even right after a title ("the doctor on duty", "doctor ke saath").
_NOT_NAME = frozenset("""
a an the and or to for of on at in by from with about as is are was were be am pm please can could would will shall you
me my our your his her him them it its this that these those there here also just only now then so if but no not yes ok
okay hello hi sir madam ji ke ka ki ko se mein me par pe hai hain ho free available availability busy slot slots
appointment appointments book booking next first earliest soonest today tomorrow tonight kal aaj parso at branch
""".split())


_NOT_NAME = _NOT_NAME | frozenset(datetime_extract._WEEKDAYS) | frozenset(datetime_extract._MONTHS)


def _could_be_name(word):
    word = re.sub(r"['’]s$", "", word or "").casefold()
    return len(word) >= 2 and any(ch.isalpha() for ch in word) and not any(ch.isdigit() for ch in word) \
        and word not in _NOT_NAME


def doctor_in_sentence(conn, text):
    """The doctor a sentence names, as spoken ("Dr. Mehta", "Dr. Anil Sharma"), or None. A title in front of a
    name ("Dr. Mehta", "Doctor Mehta", "डॉक्टर मेहता") always counts, even for a name nobody has (the caller then
    asks); a bare name counts only after "with" / before "ke saath" and only when it is a doctor's name."""
    norm = _norm(text)
    for m in _TITLED.finditer(norm):
        first, second = m.group(1), m.group(2)
        if not _could_be_name(first):
            continue
        if second and _could_be_name(second) and resolve_doctor(conn, "{} {}".format(first, second))[0] != "none":
            return "Dr. {} {}".format(first, second)
        return "Dr. {}".format(first)
    for m in _WITH_CUE.finditer(norm):
        word = m.group(1) or m.group(2)
        if word and _could_be_name(word) and resolve_doctor(conn, word)[0] != "none":
            return "Dr. {}".format(word)
    return None


# -- the sentence --------------------------------------------------------------------------

_NEXT_CUE = re.compile(
    _EDGE_L + r"(?:next\s+(?:available|free|open|vacant)|earliest|soonest|first\s+(?:available|free|open|vacant|slot|appointment|opening)"
    r"|next\s+(?:slot|appointment|opening)s?\s+(?:available|free|open)|next\s+few|next\s+(?:\d{1,2}|two|three|four|five)\s+"
    r"(?:available|free|slots?|appointments?|openings?)"
    r"|agla\s+(?:available|free|khaali|khali|slot|khali\s+slot)|agle\s+(?:available|free|khaali|khali|slot)"
    r"|pehla\s+(?:available|free|khaali|khali|slot)|pehle\s+(?:available|free|khaali|khali)|sabse\s+pehle|jaldi\s+se\s+jaldi"
    r"|अगला\s+(?:उपलब्ध|खाली|फ्री|स्लॉट)|अगले\s+(?:उपलब्ध|खाली|फ्री|स्लॉट)|पहला\s+(?:उपलब्ध|खाली|फ्री|स्लॉट)|सबसे\s+पहले|जल्दी\s+से\s+जल्दी)"
    + _EDGE_R, re.IGNORECASE)
_COUNT_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5,
                "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "पाँच": 5}
_MANY = re.compile(
    _EDGE_L + r"(?:next|first|earliest|agle|agli|pehle|अगले|पहले)\s+(\d{1,2}|" + "|".join(_COUNT_WORDS) + r"|few|couple(?:\s+of)?)\s+"
    r"(?:available\s+|free\s+|khaali\s+|खाली\s+)?(?:slots?|appointments?|openings?|times?|स्लॉट|अपॉइंटमेंट)" + _EDGE_R, re.IGNORECASE)
_PLURAL = re.compile(
    _EDGE_L + r"(?:next|first|earliest|agle|अगले)\s+(?:available\s+|free\s+|khaali\s+|खाली\s+|उपलब्ध\s+)(?:slots|appointments|openings|times)" + _EDGE_R,
    re.IGNORECASE)


def asks_next(text):
    """True when the words ask for the NEXT / earliest / first free slot ("next available", "earliest",
    "agla available", "अगला उपलब्ध")."""
    return bool(_NEXT_CUE.search(_norm(text)))


def how_many(text):
    """How many slots a "next few / next three / first 2 available slots" asks for, or None (the default, one)."""
    norm = _norm(text)
    m = _MANY.search(norm)
    if m:
        word = m.group(1).casefold()
        if word.isdigit():
            return max(1, min(int(word), MAX_SLOTS))
        return _COUNT_WORDS.get(word) or (2 if word.startswith("couple") else 3)
    if _PLURAL.search(norm) or re.search(_EDGE_L + r"next\s+few" + _EDGE_R, norm, re.IGNORECASE):
        return 3
    return None


# "... and book it for <name>": a conservative reader of the booking tail. It must be a clause of its own (after
# a comma, a full stop, "and" / "then" / "also"), start with the verb book / fix / schedule and say who for, so a
# sentence that merely contains "book" and a name somewhere else is not a booking request.
# The English tail must come after a comma / full stop / "and" / "then" / "also"; "to book for Neha" is not one.
_LEAD = r"(?:[,.;]\s*|" + _EDGE_L + r"(?:and|then|also|plus)\s+)(?:(?:and|then|also|plus|please)\s+)*"
_REF = (r"(?:(?:it|that|this|that\s+slot|this\s+slot|the\s+slot|that\s+one|this\s+one|the\s+appointment"
        r"|the\s+(?:first|earliest|next)(?:\s+(?:one|slot|available|appointment))?)\s+)?")
_WHO = r"(?:a\s+|an\s+)?(?:new\s+)?(?:patient\s+)?(?:(?:named|called)\s+)?"
_NAMED = r"(?P<name>" + _WORD + r"(?:\s+" + _WORD + r"){0,2})"
_TAILS = (
    # English: "and book it for Neha Gupta", "then book the first one for a patient named Neha Gupta"
    re.compile(_LEAD + _EDGE_L + r"(?:book|fix|schedule)" + _EDGE_R + r"\s+" + _REF
               + r"(?:for|to|under|in\s+the\s+name\s+of)\s+" + _WHO + _NAMED, re.IGNORECASE),
    # English, no "it ... for": "and book a patient named Neha Gupta", "and book new patient called Neha"
    re.compile(_LEAD + _EDGE_L + r"(?:book|fix|schedule)" + _EDGE_R + r"\s+" + _REF
               + r"(?:a\s+|an\s+)?(?:new\s+)?patient\s+(?:named|called)\s+" + _NAMED, re.IGNORECASE),
    # Hinglish: "aur Neha Gupta ke liye book karo", "phir Neha Gupta ko usme book kar do"
    re.compile(r"(?:[,.;]\s*|\s)" + _EDGE_L + r"(?:aur|phir|fir|uske\s+baad)\s+(?:use\s+|usko\s+|isko\s+)?" + _NAMED
               + r"\s+(?:ke\s+liye|ke\s+lie|ko|ke\s+naam\s+se)\s+(?:(?:use|usko|isko|woh|wo|yeh|ye|vo|usme|isme)\s+)?(?:book|fix|schedule)"
               + _EDGE_R, re.IGNORECASE),
    # Hinglish, verb first: "aur book kar do Neha Gupta ke liye"
    re.compile(r"(?:[,.;]\s*|\s)" + _EDGE_L + r"(?:aur|phir|fir)\s+(?:(?:use|usko|isko)\s+)?(?:book|fix)\s+(?:kar\s*do|karo|kardo|kijiye)\s+"
               + _NAMED + r"\s+(?:ke\s+liye|ke\s+lie|ko)" + _EDGE_R, re.IGNORECASE),
    # Devanagari: "और नेहा गुप्ता के लिए बुक करो"
    re.compile(r"(?:[,.;]\s*|\s)(?:और|फिर|फ़िर)\s+" + _NAMED + r"\s+(?:के\s+लिए|को)\s+(?:(?:उसे|इसे|वो|यह|उसमें)\s+)?(?:बुक|फिक्स|शेड्यूल)", re.IGNORECASE),
)
_NEVER_A_NAME = frozenset("""it that this him her them me us use usko isko woh wo yeh ye vo one slot the first next earliest
appointment appointments patient patients someone somebody anyone""".split())


def _clean_name(raw):
    """The name words (at most three) at the front of `raw`: stops at the first word that is not a name."""
    words = []
    for word in raw.split():
        bare = re.sub(r"['’]s$", "", word.strip(".,;:!?।"))
        if bare.casefold() in _NEVER_A_NAME or not _could_be_name(bare):
            break
        words.append(bare)
    return " ".join(words[:3]) or None


def book_tail(text):
    """(name, find_clause): the person a trailing "and book it for <name>" is about, and the sentence without
    that tail; (None, text) when the sentence has no such tail. Deterministic and conservative."""
    norm = _norm(text)
    for pattern in _TAILS:
        for m in pattern.finditer(norm):
            name = _clean_name(m.group("name"))
            before = norm[:m.start()].strip(" ,.;")
            if name and before:
                return name, before
    return None, text


def read_sentence(conn, text, slots):
    """`slots` of a check_availability command filled from the words where the planner left them out: the
    next-available cue, how many, the doctor and the booking tail. Never overrides a value that is already set."""
    slots = dict(slots)
    name, find = book_tail(text)
    if name and not slots.get("then_book_for"):
        slots["then_book_for"] = name
    if not slots.get("next_available") and asks_next(find):
        slots["next_available"] = True
    if slots.get("next_available") and not slots.get("limit"):
        count = how_many(find)
        if count:
            slots["limit"] = count
    if not slots.get("doctor"):
        spoken = doctor_in_sentence(conn, find)
        if spoken:
            slots["doctor"] = spoken
    elif slots.get("doctor"):
        # the sentence's own title-and-name beats a model's reading of it (the deterministic reader wins)
        said = doctor_in_sentence(conn, find)
        if said and resolve_doctor(conn, said)[0] == "one" and resolve_doctor(conn, slots["doctor"])[0] != "one":
            slots["doctor"] = said
    return slots


# -- the search ------------------------------------------------------------------------------

def search_window(start, date_to=None):
    """How many days to look at from `start`: until `date_to` when one was given, else DEFAULT_DAYS; never more
    than MAX_DAYS."""
    if date_to:
        days = (date.fromisoformat(date_to) - date.fromisoformat(start)).days + 1
        return max(1, min(days, MAX_DAYS))
    return DEFAULT_DAYS


def branch_ids_for(conn, branch_id=None, all_branches=False, doctor_id=None):
    """The branches to look in. A named branch: just it. "All branches": every active one. A doctor and no branch:
    wherever that doctor is scheduled. Nothing named: the default branch ([None] stands for it)."""
    if not branches.multi_branch(conn):
        return [None]
    if all_branches:
        return [b["id"] for b in branches.list_branches(conn)]
    if branch_id:
        return [int(branch_id)]
    if doctor_id is not None:
        active = {b["id"] for b in branches.list_branches(conn)}
        found = sorted({row["branch_id"] for row in branches.list_schedule(conn, doctor_id=doctor_id)} & active)
        return found
    return [None]


def works_at(conn, doctor_id, branch_id):
    """True when the doctor has any schedule at that branch (one-branch clinics: always)."""
    if branch_id is None or not branches.multi_branch(conn):
        return True
    return bool(branches.list_schedule(conn, branch_id=int(branch_id), doctor_id=doctor_id))


def search(conn, start, now, days=DEFAULT_DAYS, limit=1, branch_ids=(None,), doctor_id=None, after=None):
    """The first `limit` free slots (earliest first) in the next `days` days from `start` (never before today),
    as [{date, time, branch_id, doctor_id}]. `now` is a datetime (the clock is injected: today's times that have
    passed are skipped). `after` is a (date, time) the slots must come strictly later than. Read-only; at most
    MAX_SLOTS slots over at most MAX_DAYS days."""
    limit = max(1, min(int(limit or 1), MAX_SLOTS))
    days = max(1, min(int(days or DEFAULT_DAYS), MAX_DAYS))
    first = max(date.fromisoformat(start), now.date())
    today, clock = now.date().isoformat(), now.strftime("%H:%M")
    found = []
    was_read_only = conn.execute("PRAGMA query_only").fetchone()[0]
    conn.execute("PRAGMA query_only = ON")
    try:
        for offset in range(days):
            iso = (first + timedelta(days=offset)).isoformat()
            day_slots = []
            for branch_id in branch_ids:
                resolved = branches.resolve(conn, branch_id)
                for at in scheduling.generate_slots(conn, iso, branch_id=resolved, only_doctor_id=doctor_id):
                    if iso == today and at <= clock:
                        continue
                    if after and (iso, at) <= tuple(after):
                        continue
                    day_slots.append((at, resolved))
            for at, resolved in sorted(day_slots):
                found.append({"date": iso, "time": at, "branch_id": resolved,
                              "doctor_id": branches.doctor_at(conn, resolved, iso, at)})
                if len(found) >= limit:
                    return found
    finally:
        conn.execute("PRAGMA query_only = {}".format("ON" if was_read_only else "OFF"))
    return found


def day_rows(conn, slots):
    """The table the page shows: one row per (day, branch) with the times as a list (the page wraps them as
    chips), the branch and the doctor."""
    rows = []
    for slot in slots:
        key = (slot["date"], slot["branch_id"])
        if rows and rows[-1]["_key"] == key:
            rows[-1]["slots"].append(slot["time"])
            continue
        rows.append({"_key": key, "date": short_day(slot["date"]), "slots": [slot["time"]],
                     "branch": branches.branch_label(conn, slot["branch_id"]),
                     "doctor": branches.doctor_label(conn, slot.get("doctor_id")) or ""})
    for row in rows:
        row.pop("_key")
    return rows


# -- the follow-up question ----------------------------------------------------------------------

OFFER_QUESTION = {
    "en": "Book {name} on {day} at {time}{doctor}{branch}?",
    "hinglish": "{name} ko {day} {time} baje{doctor}{branch} book karun?",
    "hi": "{name} को {day} {time} बजे{doctor}{branch} बुक करूँ?",
}
_WITH_DOCTOR = {"en": " with {}", "hinglish": " {} ke saath", "hi": " {} के साथ"}
_AT_BRANCH = {"en": " ({})", "hinglish": " ({})", "hi": " ({})"}
OFFER_CHOICES = {
    "en": ("Yes, book it", "Another time"),
    "hinglish": ("Haan, book karo", "Doosra time"),
    "hi": ("हाँ, बुक करो", "दूसरा समय"),
}
DROPPED = {
    "en": "Okay, I won't book it.",
    "hinglish": "Theek hai, book nahi kiya.",
    "hi": "ठीक है, बुक नहीं किया।",
}
NO_OTHER = {
    "en": "No other free slot{doctor} in the next {days} days. Nothing was booked.",
    "hinglish": "Agle {days} din mein{doctor} koi aur slot khaali nahi hai. Kuch book nahi hua.",
    "hi": "अगले {days} दिन में{doctor} कोई और स्लॉट खाली नहीं है। कुछ बुक नहीं हुआ।",
}
_NO_OTHER_DOCTOR = {"en": " with {}", "hinglish": " {} ke saath", "hi": " {} के साथ"}


def offer_question(lang, name, slot, doctor_name, branch_label):
    return OFFER_QUESTION[lang].format(
        name=name, day=short_day(slot["date"]), time=slot["time"],
        doctor=_WITH_DOCTOR[lang].format(doctor_name) if doctor_name else "",
        branch=_AT_BRANCH[lang].format(branch_label) if branch_label else "")


def no_other_text(lang, doctor_name, days):
    return NO_OTHER[lang].format(doctor=_NO_OTHER_DOCTOR[lang].format(doctor_name) if doctor_name else "", days=days)


# -- the reply to that question ---------------------------------------------------------------------

_YES = frozenset("""yes yeah yep yup ya ok okay sure fine haan han haa ha haanji ji theek thik bilkul zaroor confirm proceed
हाँ हां हा जी ठीक बिल्कुल ज़रूर जरूर""".split())
_YES_FILLER = frozenset("""please it that this go ahead do kar karo kardo kijiye dijiye de do book now then and hai hain
कर दो करो दीजिए कीजिए बुक इसे उसे है हैं""".split())
_NO = frozenset("no nope nah nahi nahin not don dont cancel skip नहीं नही ना मत".split())
_NO_FILLER = frozenset("thanks thank you please it that this do book karo kar mat t रहने दो".split())
_NEXT = re.compile(
    _EDGE_L + r"(?:next|another|other|different|later|else|alternative|agla|agle|agli|dusra|dusre|dusri|doosra|doosre|doosri|"
    r"koi\s+aur|kuch\s+aur|aur\s+koi|अगला|अगले|दूसरा|दूसरे|दूसरी|कोई\s+और|और\s+कोई)" + _EDGE_R, re.IGNORECASE)


def read_answer(text):
    """"yes" | "next" | "no" | None for the short reply to the booking question. "yes" is yes / haan / ok / book it;
    "next" is another time / the next one (also "no, the next one"); "no" is a plain no / nahi. Anything longer or
    about something else is None: it is a new command, not an answer."""
    words = _tokens(text)
    if not words or len(words) > 8:
        return None
    if _NEXT.search(_norm(text)):
        return "next"
    if any(w in _NO for w in words) and all(w in _NO or w in _NO_FILLER for w in words):
        return "no"
    if any(w in _YES or w == "book" or w == "बुक" for w in words) and all(w in _YES or w in _YES_FILLER for w in words):
        return "yes"
    return None


def booking_slots(offer):
    """The slots of the normal booking card for the slot that was offered (a copy: the offer is never edited)."""
    return {k: v for k, v in offer["booking"].items() if v not in (None, "")}


def language_key(text, language_code):
    """'hi' for Devanagari, 'hinglish' for a Hindi session, else 'en' (the split the other fixed replies use)."""
    if re.search(r"[ऀ-ॿ]", text or ""):
        return "hi"
    return "hinglish" if language_code == "hi-IN" else "en"
