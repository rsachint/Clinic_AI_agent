import contextlib
import contextvars
import json
import logging
import os
import re

import httpx
from concurrent.futures import ThreadPoolExecutor

from clinic.entity_resolution import name_match

_logger = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434/api/chat"
# One local model for everything (the planner, the label picker, name and
# patient-intent extraction): Ollama keeps a single model resident, so no call
# ever waits for another to be unloaded. Gemma 4 12B replaced the 3B Qwen once
# the tool-calling planner needed a model that can follow tool schemas; set
# CLINIC_LLM_MODEL to try another (the planner follows it unless PLANNER_MODEL
# says otherwise, see clinic/nlu/planner.py).
DEFAULT_MODEL = "gemma4:12b"
MODEL = os.environ.get("CLINIC_LLM_MODEL", "").strip() or DEFAULT_MODEL

KEEP_ALIVE = "30m"


def ollama_options(**extra):
    """Generation options shared by EVERY local-model call (intent, name,
    patient intent). They must all agree: Ollama reloads the model whenever
    num_gpu changes between requests, so calls with different settings would
    keep evicting each other.

    num_gpu defaults to 0 = run on the CPU. Measured on this Mac (M4, 16 GB,
    swap almost full): the same 3B model generates ~22 tokens/s on the CPU but
    only ~3.5 tokens/s through Metal, so CPU is ~6x faster here. Set
    CLINIC_LLM_NUM_GPU=auto to let Ollama choose (try it after a reboot or on
    other hardware; scripts/bench_ollama.py prints both speeds)."""
    options = {"temperature": 0}
    try:
        # The planner's prompt (19 tool schemas + the day's calendar) needs more
        # than Ollama's default window, and a changed num_ctx also reloads the
        # model, so every caller asks for the same one.
        options["num_ctx"] = max(1024, int(os.environ.get("CLINIC_LLM_NUM_CTX", "4096")))
    except ValueError:
        options["num_ctx"] = 4096
    setting = os.environ.get("CLINIC_LLM_NUM_GPU", "0").strip().lower()
    if setting != "auto":
        try:
            options["num_gpu"] = int(setting)
        except ValueError:
            options["num_gpu"] = 0
    options.update(extra)
    return options


def staff_uses_sarvam():
    """PLANNER_BACKEND=sarvam: staff commands are understood by Sarvam's hosted model (clinic/nlu/sarvam.py)
    and then by the plain rules. The local model is not asked for them at all."""
    return os.environ.get("PLANNER_BACKEND", "").strip().lower() == "sarvam"


# True while a STAFF command is being parsed with the hosted planner selected: every local-model call
# on that path (name lookup, label picker) is then skipped. The patients' WhatsApp path never sets it.
_local_off = contextvars.ContextVar("clinic_local_model_off", default=False)


@contextlib.contextmanager
def staff_command():
    """Around the parsing of one staff command: with the hosted planner selected, the local model is
    not used inside. With the local planner it changes nothing."""
    token = _local_off.set(staff_uses_sarvam())
    try:
        yield
    finally:
        _local_off.reset(token)


def local_model_allowed():
    return not _local_off.get()


_PROMPT_PREFIX = (
    "Find the patient or staff name in this clinic command (English, Hindi or "
    "Hinglish, maybe Devanagari). Reply with ONLY that name, copied exactly as "
    "written in the command, nothing else. Reply NONE only if the command "
    "mentions no person at all.\n\n"
    "Examples:\n"
    "Command: book an appointment for Sunita tomorrow at 5 pm\nName: Sunita\n"
    "Command: मोहन शर्मा का फॉलो अप कैंसिल करो\nName: मोहन शर्मा\n"
    "Command: Ajay Rao came in today fee 300 rupees\nName: Ajay Rao\n"
    "Command: register a new patient Neha Jain phone 9812345678 age 30\nName: Neha Jain\n"
    "Command: what is today's cash book\nName: NONE\n\n"
    "Command: "
)
_PROMPT_SUFFIX = "\nName:"

# Registered patient / staff names for the command being parsed right now.
# parse() sets this so extract_name can recognise a name we already know with
# plain string matching, with no model call at all.
_known_names = contextvars.ContextVar("clinic_known_names", default=())

_WORD_SPLIT = re.compile(r"[\s,.;:!?।\"'’‘“”()\[\]{}]+")


def set_known_names(names):
    return _known_names.set(tuple(n for n in (names or ()) if n and n.strip()))


def reset_known_names(token):
    _known_names.reset(token)


def current_known_names():
    """The registered names parse() made available for the command being read (empty outside parse())."""
    return _known_names.get()


# Words that may stand right next to a ONE-word registered name without being part of another name
# (English, Hindi, Hinglish; Devanagari spelled out). Anything else next to it -- "Manju Sharma", "Sharma
# Manju" -- might be a different, new person who only shares the first word, so the name is not taken.
# Single letters, numbers and anything with a digit also count as non-names (a branch letter, a time).
_NEIGHBOUR_WORDS = frozenset("""
a an the and or to for of on at in by from with about into as is are was were be am pm o clock s please can could would
will shall you me my our your his her him them it its this that these those there here also just only now then so if but
no not yes ok okay hello hi sir madam ji dr mr mrs ms miss shri smt sri doctor
book books booked booking bookings cancel cancelled move moved shift shifted reschedule rescheduled change changed
postpone preponed prepone delay set schedule fix arrange check show tell give get find look lookup search list open call
mark add remove delete update edit fetch bring see view put make take do does done send log record visit visits came come
appointment appointments appt appts patient patients slot slots token tokens follow followup followups up number phone
fee fees name day days time date week weeks month today tomorrow yesterday tonight morning afternoon evening night noon
next last this coming before after till until between during one two three four five six seven eight nine ten eleven
twelve first second third absent present leave half late off sick chutti chhutti aya aaya aayi gaya gayi nahi
rupees rupee rs paid payment cash upi free available availability busy who what when where how kab kya kaun kahan kyun kaise
ka ki ke ko ne se me mein par pe tak liye lie wala wali wale aur bhi hai hain ho hoga hogi tha thi the karo kar kare karna
kardo kardijiye kijiye kijie dena de dijiye dijie batao bata bataiye dikhao dikha dikhaiye lagao laga hatao hata badlo
badal aaj kal parso abhi subah shaam dopahar raat baje bje agle agla pichle pichla is us uska uski iska iski unka mera
meri hamara mujhe humein ek do teen char chaar paanch panch chhe saat aath nau das gyarah barah
का की के को से में पर पे तक लिए वाला वाली वाले और भी है हैं हो था थी करो कर करना दो दे बताओ बताइए दिखाओ दिखाइए
अपॉइंटमेंट अपॉइन्टमेंट अपोइंटमेंट मरीज मरीज़ पेशेंट आज कल परसों अभी सुबह शाम दोपहर रात बजे अगले अगला पिछले
बुक कैंसल कैन्सल कैंसिल शिफ्ट मूव रीशेड्यूल बदलो बदल नंबर फोन फीस एक तीन चार पांच पाँच छह सात आठ नौ दस
अनुपस्थित हाज़िर हाजिर छुट्टी हाफ आया आई गया गई नहीं रुपये रुपए कब क्या कौन कहाँ
""".split())

# "new patient Manju", "register Manju": a person being added, not one we know.
_NEW_PERSON_WORDS = frozenset(("new", "naya", "nayi", "naye", "नया", "नई", "नयी", "नये", "register", "registration"))


def _neighbour_ok(token):
    """True when `token` (or no token at all) is something that cannot be the rest of a person's name."""
    if token is None:
        return True
    folded = token.casefold()
    return folded in _NEIGHBOUR_WORDS or len(folded) == 1 or any(ch.isdigit() for ch in folded)


def _match_one_word_name(text, names):
    """The ONE-word registered name written in `text`, or None when unsure. It is taken only when
      * the word fits exactly one registered entry (name_match: the whole word, any case, a Devanagari
        spelling of the same Roman name): two Amits, or "Manju" and "Manju Verma", is ambiguous;
      * that entry is itself one word (a lone first name of a two-word patient is never taken here); and
      * the words around it cannot be more of a name: the next word and the one before it are command or
        function words, a number or the end of the sentence ("Book Manju Sharma tomorrow" is a different
        person, "new patient Manju" is not one we know).
    A possessive ("Manju's") or a Hindi particle ("Manju ka") is fine. When unsure, None: the caller
    then falls back (the hosted name reader, the local model, or the question "Which patient?")."""
    tokens = [t for t in _WORD_SPLIT.split(text) if t]
    hits = []
    for index, token in enumerate(tokens):
        if _neighbour_ok(token):
            continue            # a function word, a single letter or a number is never a name
        fitting = [n for n in names if n and n.strip() and name_match(token, n) == 1.0]
        if fitting:
            hits.append((index, fitting))
    if len(hits) != 1:
        return None
    index, fitting = hits[0]
    if len(fitting) != 1 or len(fitting[0].split()) != 1:
        return None
    before = tokens[index - 1] if index > 0 else None
    after = tokens[index + 1] if index + 1 < len(tokens) else None
    if not (_neighbour_ok(before) and _neighbour_ok(after)):
        return None
    if any(t.casefold() in _NEW_PERSON_WORDS for t in tokens[max(0, index - 2):index]):
        return None
    return fitting[0].strip()


def match_known_name(text, names):
    """The registered name (exactly as stored) written in `text`, or None.
    Names of two or more words: every word must appear as a whole word; the name with the most words wins;
    two different names tying (e.g. "Mohan Lal aur Mohan Das") is ambiguous, so it returns None and the
    caller falls back to the model.
    Names of ONE word (a patient or staff member known only by a first name) are taken by
    _match_one_word_name's stricter rule. A Devanagari command naming a patient stored in Roman letters
    matches only a one-word name (the Roman spelling is compared exactly); a longer one goes to the model."""
    if not text or not names:
        return None
    words = {w.casefold() for w in _WORD_SPLIT.split(text) if w}
    best, best_len, tied = None, 0, False
    for name in names:
        parts = [p.casefold() for p in name.split() if p]
        if len(parts) < 2 or not all(p in words for p in parts):
            continue
        if len(parts) > best_len:
            best, best_len, tied = name.strip(), len(parts), False
        elif len(parts) == best_len and name.strip().casefold() != (best or "").casefold():
            tied = True
    if tied:
        return None
    return best or _match_one_word_name(text, names)


def _clean_name(content):
    """The model is asked for just the name; tolerate quotes, a "Name:" label,
    a trailing full stop or a JSON object, and treat NONE / an empty or
    sentence-sized answer as no name."""
    value = str(content).strip()
    if value.startswith("{"):
        try:
            value = json.loads(value).get("name") or ""
        except Exception:
            return None
    value = value.splitlines()[0] if value else ""
    value = re.sub(r"^\s*(?:name|नाम)\s*[:\-]\s*", "", value, flags=re.IGNORECASE)
    value = value.strip().strip("`\"'.,।* ").strip()
    if not value or value.lower() in ("none", "null", "n/a", "no name") or len(value.split()) > 5:
        return None
    return value


# The name call does not depend on which intent the router picks, so parse()
# starts it in the background while the intent model is still thinking and
# extract_name() later just collects the answer (measured: ~0.4 s for both
# instead of ~0.9 s one after the other).
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="name-prefetch")
_prefetched = contextvars.ContextVar("clinic_prefetched_name", default=None)


def _ask_model_for_name(text, model=MODEL, timeout=30):
    response = httpx.post(
        OLLAMA_URL,
        json={
            "model": model,
            "messages": [{"role": "user", "content": _PROMPT_PREFIX + text + _PROMPT_SUFFIX}],
            "stream": False,
            "think": False,
            "options": ollama_options(num_predict=24),
            "keep_alive": KEEP_ALIVE,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return _clean_name(response.json()["message"]["content"])


def prefetch_name(text):
    """Start the name lookup in the background for `text` (skipped when a
    registered name already matches, which needs no model, and when the staff
    command is not to use the local model)."""
    if not local_model_allowed() or match_known_name(text, _known_names.get()):
        return
    context = contextvars.copy_context()
    _prefetched.set((text, _executor.submit(context.run, _ask_model_for_name, text)))


def clear_prefetch():
    _prefetched.set(None)


def extract_name(text, model=MODEL, timeout=30):
    known = match_known_name(text, _known_names.get())
    if known:
        return known
    if not local_model_allowed():
        return None      # a name is only a hint: the review card (or "Which patient?") gets it from the person
    try:
        pending = _prefetched.get()
        if pending is not None and pending[0] == text and model == MODEL:
            return pending[1].result(timeout=timeout)
        return _ask_model_for_name(text, model=model, timeout=timeout)
    except Exception as exc:
        # The local model is not running or did not answer. A name is only a
        # hint for a field the person reviews anyway, so leave it blank rather
        # than failing the whole command.
        _logger.warning("Name extraction unavailable (%s); leaving the name blank for the review card.", exc)
        return None
