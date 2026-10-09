"""Which way a staff command is understood: the switch between the three architectures.

  classic      the rules decide first (a branch switch, a move, a patient count, a follow-up on what
               is on screen, the answer to an open question), the planner helps where the rules
               stop. This is how the app first worked; it is the rollback.
  model_first  the Sarvam planner reads every turn together with a short "state card" of the
               conversation (clinic/state_card.py), and code validates and executes what it says
               (clinic/nlu/dialogue.py). Experimental; falls back to classic for any turn the
               planner cannot answer.
  model_reads  model_first understanding PLUS model-written reads: the planner also writes the one read-only
               SELECT that answers a question over curated views, code checks it with layered guardrails,
               runs it read-only and composes the answer (clinic/sql_read.py, clinic/nlu/sql_reads.py).
               Everything else is exactly model_first. The default for an install that has never chosen.

The source of truth is the `intent_architecture` row of the app_settings table (clinic/settings.py),
read at EVERY command, so flipping it in Settings takes effect on the next sentence with no restart.
When that row is absent (or blank) the INTENT_ARCHITECTURE environment variable is used, and when that is unset
too the mode is DEFAULT ("model_reads"). Any other, unknown value, anywhere, reads as "classic": the safe direction.

Rollback is one setting. With "classic" every model-first hook in the app is skipped before any
model-first module is even imported, so the behaviour is exactly what it was before this existed. With "model_first"
the model-reads modules (clinic/sql_read.py, clinic/nlu/sql_reads.py) are not imported either.
"""

import os

from clinic import settings

KEY = "intent_architecture"          # the app_settings key
ENV = "INTENT_ARCHITECTURE"
CLASSIC = "classic"
MODEL_FIRST = "model_first"
MODEL_READS = "model_reads"
MODES = (CLASSIC, MODEL_FIRST, MODEL_READS)
DEFAULT = MODEL_READS            # a fresh install; an install that already chose keeps its choice

LABELS = {
    CLASSIC: "Classic (rules first)",
    MODEL_FIRST: "New (model first, experimental)",
    MODEL_READS: "Model does all read operations",
}
# One sentence shown under an option in Settings (only the options that need one).
HELP = {
    MODEL_READS: "Like the New mode, and the model also writes the read-only queries that answer your questions "
                 "(checked by code, never able to change data) instead of the fixed filters. Switching back to "
                 "Classic or New is instant.",
}


def normalize(value):
    """'classic' / 'model_first' / 'model_reads' for a stored or typed value (case, dashes and spaces ignored), else None."""
    text = str(value).strip().lower().replace("-", "_").replace(" ", "_") if value is not None else ""
    return text if text in MODES else None


def mode(conn=None):
    """The mode in force for the command being handled now: the stored setting, else the environment,
    else DEFAULT. Never raises; an unreadable or unknown value is classic (the safe direction)."""
    stored = None
    if conn is not None:
        try:
            stored = settings.get(conn, KEY)
        except Exception:
            return CLASSIC                       # the setting cannot be read: the safe direction
    if stored is not None and str(stored).strip() != "":
        return normalize(stored) or CLASSIC
    env = os.environ.get(ENV)
    if env is not None and env.strip() != "":
        return normalize(env) or CLASSIC
    return DEFAULT


def is_model_first(conn=None):
    """True for the model-first understanding: both "model_first" and "model_reads" (which adds model-written reads)."""
    return mode(conn) in (MODEL_FIRST, MODEL_READS)


def reads_by_model(conn=None):
    """True only for "model_reads": the planner writes the read queries (clinic/nlu/sql_reads.py)."""
    return mode(conn) == MODEL_READS


def source(conn=None):
    """Where the mode in force comes from: 'setting', 'environment' or 'default' (for the Settings card)."""
    if conn is not None:
        try:
            stored = settings.get(conn, KEY)
        except Exception:
            stored = None
        if stored is not None and str(stored).strip() != "":
            return "setting"
    return "environment" if os.environ.get(ENV, "").strip() else "default"


def set_mode(conn, value):
    """Save the mode (validated). Raises ValueError for anything but the known modes."""
    chosen = normalize(value)
    if chosen is None:
        raise ValueError("Choose Classic, New (model first) or Model does all read operations.")
    settings.set_value(conn, KEY, chosen)
    return chosen


def view(conn):
    """What the Settings card shows."""
    current = mode(conn)
    return {
        "mode": current, "label": LABELS[current], "source": source(conn),
        "options": [dict({"value": m, "label": LABELS[m]}, **({"help": HELP[m]} if m in HELP else {})) for m in MODES],
    }
