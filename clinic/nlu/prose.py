"""What to do when the hosted / local planner answers in plain words instead of calling a tool.

A model asked for ONE tool call sometimes replies with a sentence ("What date and time would you like?"). It used
to be dropped and the keyword rules guessed instead, which turned a perfectly good question into a wrong card or a
"which patient?" nobody had asked for. Two things happen to such a reply now (clinic/nlu/planner.py `PlannerRun`,
clinic/nlu/dialogue.py `ModelFirstRun`):

  * it is LOGGED (scrubbed and capped, `scrub()`), in the planner_log row's override_notes;
  * when it is a short, plain question (`as_question()`), it becomes the `clarify` tool: the assistant shows it as
    its own question, and the user's next sentence is planned again with that question as the previous turn,
    exactly as for a `clarify` call.

The words are only ever DISPLAYED. They are never executed, never read for slot values, ids or approvals, and a reply
that claims something was done ("I've booked it", "ho gaya") is refused outright: the app has done nothing, and a
sentence that says otherwise must never reach the screen as the assistant's own words. Refused or odd text falls back
as before (the keyword rules), still logged.

This is a conservative word filter, not an understanding of the text: see `claims_action()` for its limits.
"""

import re
import unicodedata

LOG_CAP = 300             # characters of a reply kept in the log
QUESTION_MAX = 240        # the longest reply that may be shown as a question

# Anything shaped like a secret: the app's own key variables, "Bearer x", header-style assignments, vendor key
# prefixes, and any long unbroken run of letters and digits (a real word is never 28+ characters of [A-Za-z0-9_-]).
_SECRET = re.compile(
    r"(?i)(?:api[-_ ]?(?:subscription[-_ ]?)?key|authorization|secret|token|password|passwd)\s*[:=]\s*(?:bearer\s+)?\S+|\bbearer\s+\S+"
    r"|\b(?:sk|pk|rk|key|sarvam|ghp|xox[abp])[-_][A-Za-z0-9_\-]{8,}"
    r"|\b[A-Za-z0-9_\-]{28,}\b")


def scrub(text, secrets=()):
    """`text` as one line of at most LOG_CAP characters, with anything that looks like a key removed (and any
    value in `secrets` removed literally). Safe to write to the log. Never raises."""
    if not isinstance(text, str):
        return ""
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "***")
    text = "".join(ch if (ch.isprintable() or ch.isspace()) else " " for ch in unicodedata.normalize("NFC", text))
    text = _SECRET.sub("***", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= LOG_CAP else text[:LOG_CAP - 1].rstrip() + "…"


# -- claims that something was done -----------------------------------------------------------

# English: a past-tense / "done" claim, a promise to do it, or a confirm / approve / save wording (no voice approval).
_CLAIM_EN = re.compile(
    r"\b(?:booked|scheduled|rescheduled|re-?booked|confirmed|confirm|cancel(?:l)?ed|saved|save|registered|recorded|"
    r"logged|updated|added|created|completed|approved|approve|submit(?:ted)?|finali[sz]ed?|deleted|removed|sent|"
    r"reserved|fixed|noted|done|all set|you(?:'| a)re set|taken care of|go(?:ing)? ahead|"
    r"successfully|already|"
    r"i(?:'ve| have| had| did|'ll| will|'m going to| am going to| went ahead)|"
    r"has been|have been|had been|was made|is now|are now|will be|moved)\b",
    re.IGNORECASE)

# Hinglish (Roman letters): "ho gaya", "kar diya", "book kar di", "save ho gaya", "kar chuka" ...
_CLAIM_HINGLISH = re.compile(
    r"\b(?:ho\s+(?:gaya|gayi|gaye|gya|chuka|chuki|chuke)|kar\s+(?:diya|dia|di|diye|liya|li|chuka|chuki|chuke|dunga|dungi|"
    r"denge)|kiya\s+(?:gaya|ja\s+chuka)|kar\s+di\s+gayi|(?:book|cancel|save|add|confirm|register|update)\s+(?:kar\s+)?"
    r"(?:di|diya|ho|hua|hui|kiya)|bana\s+(?:diya|di)|bhej\s+(?:diya|di)|le\s+liya|rakh\s+(?:diya|di)|"
    r"dar[ji]+\s+kar|pakka|confirm\s+(?:ho|kar)|tay\s+kar|ho\s+jayega|ho\s+jaega|kar\s+deta|kar\s+deti)\b",
    re.IGNORECASE)

# Hindi (Devanagari); no \b around Devanagari: the combining marks make it unreliable.
_CLAIM_HINDI = re.compile(
    r"हो\s*(?:गया|गई|गयी|गए|गये|चुका|चुकी|चुके|जाएगा|जायेगा|जाएगी)|कर\s*(?:दिया|दी|दिए|दिये|लिया|ली|चुका|चुकी|चुके|दूँगा|दूंगा|दूँगी|"
    r"दूंगी|देता|देती)|किया\s*(?:गया|जा\s*चुका)|की\s*(?:गई|गयी)|(?:बुक|कैंसिल|रद्द|सेव|एड|कन्फर्म|कन्फ़र्म|अपडेट|रजिस्टर)\s*"
    r"(?:कर\s*)?(?:दिया|दी|हो|हुआ|हुई|किया)|दर्ज\s*कर|जोड़\s*(?:दिया|दी)|बना\s*(?:दिया|दी)|भेज\s*(?:दिया|दी)|"
    r"कन्फ[ऱ]्म|कन्फ़र्म|पक्का|सफलतापूर्वक|तय\s*कर")


def claims_action(text):
    """True when `text` says (or promises) that the assistant booked, cancelled, saved, confirmed ... something. The
    app has done nothing when the model only spoke, so such a sentence is never shown as the assistant's own words.

    A word filter: it catches the common English, Hinglish and Hindi wordings (past tense, "done", "I'll ...", "ho
    gaya", "kar diya", "बुक कर दिया") and errs on the side of refusing (a harmless question that happens to use
    "booked" or "confirm" is refused too and falls back). A paraphrase it does not list can slip through; that is
    harmless because the words are only displayed and nothing is written without a person pressing Approve."""
    text = unicodedata.normalize("NFC", text or "").replace("\u2019", "'").replace("\u2018", "'")
    return bool(_CLAIM_EN.search(text) or _CLAIM_HINGLISH.search(text) or _CLAIM_HINDI.search(text))


# -- a short plain question -----------------------------------------------------------------------

_URL = re.compile(r"(?i)https?:|www\.|\b[\w-]+\.(?:com|in|org|net|io|ai|co|app|dev|me|ly|xyz)\b|\b\w+@\w+")
_CODE = re.compile(r"```|`|[{}<>\[\]\\|]|\*\*|^\s*#|^\s*(?:[-*•]|\d+[.)])\s", re.MULTILINE)
_JSONISH = re.compile(r"(?i)\"\s*:|tool_?calls?|function_?call|\barguments?\s*[:=]|\bjson\b|\bsql\b|\bselect\b.+\bfrom\b|"
                      r"\b(?:patient|appointment|branch|doctor|staff)_?id\b|\bids?\s*[:=#]\s*\d|#\d")
# Telling the user to do something outside the app (or asking for credentials / money).
_OUTSIDE = re.compile(
    r"(?i)\b(?:click|press|tap|download|install|visit|navigate|go\s+to|log\s*in|sign\s*in|open\s+(?:the|your|a)|"
    r"call\s+(?:us|the|him|her|them|me|\d)|e-?mail|whatsapp|text\s+(?:me|us)|password|otp|upi|pay(?:ment)?|transfer|wire|"
    r"account\s+number|card\s+number|cvv|api|sudo|terminal|run\s+(?:the|this|a))")

_WH_START = re.compile(
    r"(?i)^(?:please\s+|kindly\s+|kripya\s+|zara\s+)?(?:which|what|when|who|whom|whose|where|how|"
    r"kab|kaun|kaunsa|kaunsi|kya|kis|kitne|kitna|kitni|kahan|kaise|"
    r"कब|कौन|कौनसा|कौन\s*सा|कौन\s*सी|क्या|किस|कितने|कितना|कितनी|कहाँ|कहां|कैसे)(?![\w])")
_ASKS_FOR_DETAIL = re.compile(
    r"(?i)\b(?:tell|let\s+me\s+know|say|give|provide|share|mention|specify|batao|bataiye|bataye|bataen|bolo|boliye)\b|"
    r"बताओ|बताइए|बताएं|बताये|बतायें|बोलो|बोलिए")
_DETAIL_WORD = re.compile(
    r"(?i)\b(?:date|day|days|time|name|patient|phone|number|mobile|branch|doctor|slot|fee|amount|when|today|tomorrow|"
    r"morning|afternoon|evening|week|month|din|tarikh|samay|waqt|naam|mareez|kal|aaj|parso)\b|"
    r"तारीख|दिन|समय|वक़्त|नाम|मरीज़|मरीज|फोन|फ़ोन|नंबर|ब्रांच|डॉक्टर|कल|आज|परसों|सुबह|शाम")
# "How can I help you?" is small talk, not a missing detail.
_GENERIC = re.compile(
    r"(?i)\b(?:help\s+you|assist\s+you|anything\s+else|something\s+else|what\s+can\s+i\s+do|how\s+(?:can|may)\s+i|"
    r"can\s+i\s+help|may\s+i\s+help|is\s+there\s+anything|madad|help\s+karu)|मदद|और\s+कुछ")


def as_question(text, secrets=()):
    """The cleaned question to show for the model's plain-words reply, or None when it must not be shown.

    Accepted: at most QUESTION_MAX characters, one or two short lines of plain text, nothing that looks like a tool
    call / JSON / code / markdown / a link / an id, no instruction to do something outside the app, no claim that
    something was done (`claims_action`), it is question-shaped (a question mark, a who / which / what / when opening,
    or "tell me the day"), and it is about a detail (a wh-word opening or a word such as date, time, name, patient, phone,
    day) rather than small talk ("How can I help you?"). It is the SAME text only scrubbed of key-looking strings and
    whitespace."""
    if not isinstance(text, str):
        return None
    raw = unicodedata.normalize("NFC", text).strip()
    if not raw or len(raw) > QUESTION_MAX or len([ln for ln in raw.splitlines() if ln.strip()]) > 2:
        return None
    if _CODE.search(raw) or _URL.search(raw) or _JSONISH.search(raw) or _OUTSIDE.search(raw):
        return None
    from clinic.nlu import tools            # not at the top: tools imports the rest of the NLU package
    if any(name in raw for name in list(tools.BY_NAME) + list(tools.DIALOGUE_BY_NAME) if "_" in name):
        return None
    if claims_action(raw):
        return None
    clean = scrub(raw, secrets)
    if not clean or "***" in clean or len(clean) > QUESTION_MAX:
        return None
    shaped = (clean.endswith(("?", "\uff1f")) or bool(_WH_START.match(clean))
              or (bool(_ASKS_FOR_DETAIL.search(clean)) and bool(_DETAIL_WORD.search(clean))))
    # a question that is not about a detail ("How can I help you?", "Would you like to see the list?") is small talk
    about_a_detail = bool(_WH_START.match(clean)) or bool(_DETAIL_WORD.search(clean))
    return clean if shaped and about_a_detail and not _GENERIC.search(clean) else None
