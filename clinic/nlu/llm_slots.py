import contextvars
import json
import logging
import os
import re

import httpx
from concurrent.futures import ThreadPoolExecutor

_logger = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen2.5:3b-instruct"

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
    setting = os.environ.get("CLINIC_LLM_NUM_GPU", "0").strip().lower()
    if setting != "auto":
        try:
            options["num_gpu"] = int(setting)
        except ValueError:
            options["num_gpu"] = 0
    options.update(extra)
    return options


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

_WORD_SPLIT = re.compile(r"[\s,.;:!?।\"'()\[\]{}]+")


def set_known_names(names):
    return _known_names.set(tuple(n for n in (names or ()) if n and n.strip()))


def reset_known_names(token):
    _known_names.reset(token)


def match_known_name(text, names):
    """The registered name (exactly as stored) whose every word appears as a
    whole word in `text`, or None. The name with the most words wins; two
    different names tying (e.g. two patients both called "Mohan") is
    ambiguous, so it returns None and the caller falls back to the model. A
    Devanagari command naming a patient stored in Roman letters doesn't match
    here and so goes to the model as before."""
    if not text or not names:
        return None
    words = {w.casefold() for w in _WORD_SPLIT.split(text) if w}
    best, best_len, tied = None, 0, False
    for name in names:
        parts = [p.casefold() for p in name.split() if p]
        if not parts or not all(p in words for p in parts):
            continue
        if len(parts) > best_len:
            best, best_len, tied = name.strip(), len(parts), False
        elif len(parts) == best_len and name.strip().casefold() != (best or "").casefold():
            tied = True
    return None if tied else best


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
            "options": ollama_options(num_predict=24),
            "keep_alive": KEEP_ALIVE,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return _clean_name(response.json()["message"]["content"])


def prefetch_name(text):
    """Start the name lookup in the background for `text` (skipped when a
    registered name already matches, which needs no model)."""
    if match_known_name(text, _known_names.get()):
        return
    context = contextvars.copy_context()
    _prefetched.set((text, _executor.submit(context.run, _ask_model_for_name, text)))


def clear_prefetch():
    _prefetched.set(None)


def extract_name(text, model=MODEL, timeout=30):
    known = match_known_name(text, _known_names.get())
    if known:
        return known
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
