"""Branches in the staff voice assistant.

Three small jobs, all deterministic (regexes over the branch list, no model):

  * `find()` -- which branch did the person name in a command ("book Amit at
    Branch B", "B mein", "ब्रांच बी"), did they say "all branches", or "my
    branch"? The matched words are cut out of the text, so the name extractor
    never mistakes "Branch B" for a person.
  * `is_switch_command()` -- "switch to Branch C", "I'm at Branch B": the
    person is telling the app which branch THIS computer works for.
  * `default_branch()` -- which branch a command is about when none was
    named: the one named earlier in the conversation, else this computer's My
    branch. Booking and day-to-day reads use it; moving an appointment never
    does (the appointment already has its own branch).

With a single branch none of this is active: the text and slots pass through
untouched, so a one-branch clinic hears nothing about branches.
"""

import re
import unicodedata
from collections import namedtuple

from clinic import branches, query_tool

# branch: the matched branch dict (the last one named); every: "all branches";
# mine: "my branch"; text: the command with those words removed.
Mention = namedtuple("Mention", ["branch", "every", "mine", "text"])

# Intents whose card or answer is about one branch, and how a missing branch is filled.
BOOK_INTENTS = frozenset(("book_appointment",))
READ_INTENTS = frozenset(("check_availability", "list_appointments", "queue_status", "query"))
QUEUE_INTENTS = frozenset(("queue_check_in", "queue_call_next", "queue_mark_done", "queue_mark_no_show"))
# An appointment keeps its own branch unless the person names another.
MOVE_INTENTS = frozenset(("reschedule_appointment",))

_BRANCH_WORDS = r"(?:branch(?:es)?|ब्रांच|ब्रान्च|शाखा|शाखाएं|शाखाओं)"
_EDGE_L = r"(?<![\wऀ-ॿ])"
_EDGE_R = r"(?![\wऀ-ॿ])"

# How a branch code letter is said or transcribed: "B" -> b / bee / be / bi / बी.
_LETTER_FORMS = {
    "a": ("a", "ay", "ae", "ए", "ऐ"),
    "b": ("b", "bee", "be", "bi", "बी", "बि", "बे"),
    "c": ("c", "see", "sea", "si", "सी", "सि"),
    "d": ("d", "dee", "di", "डी", "डि"),
    "e": ("e", "ee", "ई"),
    "f": ("f", "ef", "एफ"),
}

_EVERY = re.compile(
    _EDGE_L + r"(?:all|every|each|sab|sabhi|saari|saare|sari|sare|poori|puri|सभी|सारी|सारे|सब|पूरी)\s+(?:the\s+)?(?:of\s+the\s+)?"
    + _BRANCH_WORDS + _EDGE_R
    + r"|across\s+(?:all\s+)?(?:the\s+)?" + _BRANCH_WORDS, re.IGNORECASE)
_MINE = re.compile(
    _EDGE_L + r"(?:my|this|our|meri|mera|apni|apna|hamari|iss|is|मेरी|मेरा|अपनी|अपना|हमारी|इस)\s+" + _BRANCH_WORDS + _EDGE_R,
    re.IGNORECASE)
_SWITCH_VERB = re.compile(
    _EDGE_L + r"(?:switch|change|set|swap|login|log\s+in|i\s*am\s+(?:at|in|working)|i'?m\s+(?:at|in|working)|working\s+(?:at|in)"
    r"|mein\s+hoon|me\s+hoon|main\s+.*\s+hoon|hoon|hun|स्विच|बदलो|बदल|सेट|हूँ|हूं)" + _EDGE_R, re.IGNORECASE)
# Words that make a sentence about something other than which branch this computer works for.
_NOT_A_SWITCH = re.compile(
    _EDGE_L + r"(?:appointment|appointments|book|booking|cancel|reschedule|postpone|move|shift|slot|slots|patient|token|queue"
    r"|list|show|how\s+many|free|available|availability|date|time|din|tarikh|follow\s*up|visit|fee|expense"
    r"|dikha\w*|batao|bata|dekho|dekh|nikalo|nikaal\w*|kitne|kitni"
    r"|अपॉइंटमेंट|बुक|कैंसिल|रीशेड्यूल|पेशेंट|मरीज|टोकन|कतार|तारीख|दिन|समय|दिखा|बताओ|बता|देखो|निकालो|कितने|कितनी)" + _EDGE_R, re.IGNORECASE)


def _norm(text):
    return unicodedata.normalize("NFC", text or "").lower()


# The small linking words that travel with a branch phrase and go when it is cut out.
_LEAD = re.compile(r"(?:\b(?:at|in|to|on|from|for)|के\s+लिए|ke\s+liye)\s+$")
_TRAIL = re.compile(r"^\s+(?:में|मे|पर|से|का|की|के|mein|me|par|pe|se|ka|ki|ke)(?![\wऀ-ॿ])")


def _widen(norm, start, end):
    lead = _LEAD.search(norm[:start])
    trail = _TRAIL.match(norm[end:])
    return (lead.start() if lead else start), (end + trail.end() if trail else end)


def _code_forms(code):
    code = _norm(code)
    return _LETTER_FORMS.get(code, (code,)) if code else ()


def _alt(words):
    return "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True))


def _patterns_for(branch):
    """The regexes that mean this branch, each with the words they match."""
    forms = [f for f in _code_forms(branch.get("code")) if f]
    out = []
    name = _norm(branch.get("name")).strip()
    if name:
        out.append(re.compile(_EDGE_L + re.escape(name) + _EDGE_R))
    if forms:
        alt = _alt(forms)
        out.append(re.compile(_EDGE_L + _BRANCH_WORDS + r"\s+(?:" + alt + r")" + _EDGE_R))          # "branch b", "ब्रांच बी"
        # "b branch" / "बी ब्रांच"; the bare English article "a" never counts.
        after = [f for f in forms if f != "a"]
        if after:
            out.append(re.compile(_EDGE_L + r"(?:" + _alt(after) + r")\s+" + _BRANCH_WORDS + _EDGE_R))
        # "B mein", "बी पर", "C wali": a letter followed by a Hindi place word.
        place = r"(?:mein|me|par|pe|wali|wale|vali|vale|में|मे|पर|वाली|वाले)"
        tail = [f for f in forms if f != "a" and (len(f) > 1 or f.isascii())]
        if tail:
            out.append(re.compile(_EDGE_L + r"(?:" + _alt(tail) + r")\s+" + place + _EDGE_R))
    return out


def find(conn, text, bare=False, tail=False):
    """The Mention for `text`. `bare=True` is for the answer to "Which branch?":
    a short reply that is just the code ("B", "बी", "bee") counts too. `tail=True`
    (a switch command) also reads a code left at the very end ("change my branch to b")."""
    norm = _norm(text)
    items = branches.list_branches(conn) if branches.multi_branch(conn) else []
    if not items:
        return Mention(None, False, False, text)

    spans = []          # (start, end) of every matched phrase, to cut out
    hits = []           # (start, branch)
    for branch in items:
        for pattern in _patterns_for(branch):
            for m in pattern.finditer(norm):
                spans.append(_widen(norm, *m.span()))
                hits.append((m.start(), branch))
    if bare and not hits:
        words = re.findall(r"[\wऀ-ॿ']+", norm)
        if 1 <= len(words) <= 3:
            for branch in items:
                if any(w in _code_forms(branch.get("code")) for w in words):
                    hits.append((0, branch))
    if tail and not hits:
        last = re.findall(r"[\wऀ-ॿ']+", norm)[-1:]
        for branch in items:
            if last and last[0] in _code_forms(branch.get("code")):
                hits.append((len(norm), branch))
    every = _EVERY.search(norm)
    mine = _MINE.search(norm)
    for m in (every, mine):
        if m:
            spans.append(_widen(norm, *m.span()))

    branch = None
    if hits:
        hits.sort(key=lambda h: h[0])
        branch = hits[-1][1]               # the last one named is the destination ("from A to B")

    cut = text
    if spans:
        # Work on the original string: lowercasing keeps offsets for these scripts.
        keep, pos = [], 0
        for start, end in sorted(set(spans)):
            if start < pos:
                continue
            keep.append(text[pos:start])
            pos = end
        keep.append(text[pos:])
        cut = re.sub(r"\s{2,}", " ", " ".join(keep)).strip()
    return Mention(branch, bool(every) and branch is None, bool(mine) and branch is None and not every, cut)


def named_branches(conn, text):
    """Every distinct branch named in `text`, in the order they are said (the
    closing one first in "Branch B will be closed, move everyone to Branch C")."""
    norm = _norm(text)
    found = {}
    for branch in (branches.list_branches(conn) if branches.multi_branch(conn) else []):
        for pattern in _patterns_for(branch):
            for m in pattern.finditer(norm):
                found[branch["id"]] = min(found.get(branch["id"], (m.start(), branch))[0], m.start()), branch
    return [branch for _, branch in sorted(found.values(), key=lambda item: item[0])]


def is_switch_command(text):
    """True for "switch to Branch C" / "change my branch to B" / "I'm at Branch B":
    a statement about which branch this computer works for, not a request about
    appointments."""
    norm = _norm(text)
    if not re.search(_BRANCH_WORDS, norm) and not re.search(r"\b(?:b|c|d)\s+(?:mein|me|par|pe)\b", norm):
        return False
    if _NOT_A_SWITCH.search(norm):
        return False
    return bool(_SWITCH_VERB.search(norm))


def default_branch(conn, intent, slots, context):
    """Fill in (or drop) the branch slots for `intent`. Single-branch clinics
    never carry any. With several: a named branch is kept; otherwise booking,
    the day-to-day reads and the queue commands use the branch discussed
    earlier, else this computer's My branch. A reschedule keeps the appointment's
    own branch unless one was named, and "all branches" only means something to
    the reads."""
    slots = dict(slots)
    if not branches.multi_branch(conn):
        slots.pop("branch_id", None)
        slots.pop("all_branches", None)
        return slots
    if intent not in READ_INTENTS:
        slots.pop("all_branches", None)
    if intent in MOVE_INTENTS or slots.get("branch_id") or slots.get("all_branches"):
        return slots
    if intent in ("list_appointments", "query") and slots.get("patient_name"):
        return slots          # one person's appointments: wherever they are, unless a branch was named
    if intent == "query" and slots.get("entity") not in query_tool.MY_BRANCH_DEFAULT:
        return slots          # staff, doctors, closures ...: clinic-wide unless a branch was named
    if intent in BOOK_INTENTS or intent in READ_INTENTS or intent in QUEUE_INTENTS:
        chosen = context.current_branch() if context is not None else None
        if chosen:
            slots["branch_id"] = chosen
    return slots


def label(conn, branch_id):
    return branches.branch_label(conn, branch_id)
