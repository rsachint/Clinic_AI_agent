"""The configurations a replay can run under, and the rules for the live ones.

  classic_rules_only    classic mode, planner OFF: the keyword rules and the deterministic readers only. Fully
                        offline; shows what the rule layer can and cannot do by itself.
  classic_scripted      classic mode; the planner's answer is the turn's golden `model` (a scripted fake backend).
  model_first_scripted  model-first mode; same golden answers. This tests the state card, the dispatcher, the
                        validators, the memory carry-over and the fallback plumbing. It does NOT measure how
                        well a real model understands: the "model" is the script.
  model_reads_scripted  "Model does all read operations": model-first understanding plus model-written reads
                        (clinic/nlu/sql_reads.py). The golden answers of a turn may be a LIST of tool calls (a
                        multi-step lookup: each planner call of the turn gets the next one). Tests the views, the
                        guardrails, the loop, the composer and the fallbacks. Offline.
  classic_live / model_first_live / model_reads_live
                        the real Sarvam backend. Refused unless --live AND --yes are given and a key is set;
                        the planner's own 5 s budget and circuit breaker apply, and calls are paced under about
                        3 per second. These are the only configs that measure real accuracy and latency.

Nothing here reaches the network except a live config the person explicitly asked for.
"""

import math
import os
import re
from collections import namedtuple

Config = namedtuple("Config", ["name", "architecture", "planner", "live", "description"])

CONFIGS = {c.name: c for c in (
    Config("classic_rules_only", "classic", "off", False,
           "classic mode, planner off: keyword rules and deterministic readers only (offline)"),
    Config("classic_scripted", "classic", "scripted", False,
           "classic mode, golden planner answers from a scripted fake backend (offline)"),
    Config("model_first_scripted", "model_first", "scripted", False,
           "model-first mode, golden planner answers: tests plumbing, NOT the model's accuracy (offline)"),
    Config("model_reads_scripted", "model_reads", "scripted", False,
           "model-reads mode, golden planner answers incl. multi-step lookups: tests the views, guardrails and composer, "
           "NOT the model's SQL (offline)"),
    Config("classic_live", "classic", "live", True, "classic mode on the real Sarvam backend (costs money)"),
    Config("model_first_live", "model_first", "live", True, "model-first mode on the real Sarvam backend (costs money)"),
    Config("model_reads_live", "model_reads", "live", True,
           "model-reads mode on the real Sarvam backend: the model writes the read queries (costs money; a lookup turn "
           "makes up to 3 calls)"),
)}
OFFLINE = tuple(n for n, c in CONFIGS.items() if not c.live)
RUPEES_PER_CALL = 0.11          # the planner's measured cost per call (about 3,700 prompt tokens at Sarvam's prices)
MAX_CALLS_PER_SECOND = 3.0


class ConfigError(Exception):
    """A configuration cannot be run as asked (unknown name, or a live one without the flags)."""


def get(name):
    if name not in CONFIGS:
        raise ConfigError("Unknown config {!r}. Choose from: {}.".format(name, ", ".join(CONFIGS)))
    return CONFIGS[name]


def check_live(names, live=False, yes=False, env=None, require_key=True):
    """Raise ConfigError unless every live config in `names` has everything it needs: --live, --yes and a key
    in the environment. Offline configs always pass. Nothing is printed or returned that contains the key."""
    env = os.environ if env is None else env
    wanted = [n for n in names if get(n).live]
    if not wanted:
        return
    problems = []
    if not live:
        problems.append("add --live to say you want the real model")
    if not yes:
        problems.append("add --yes to confirm the cost")
    if require_key and not (env.get("SARVAM_API_KEY") or "").strip():
        problems.append("SARVAM_API_KEY is not set in the environment")
    if problems:
        raise ConfigError("{} would call the real Sarvam model: {}.".format(", ".join(wanted), "; ".join(problems)))


def planner_calls_estimate(cases, names, ablate=()):
    """An upper bound on how many planner calls a live run makes: one per spoken turn, per live config, and the
    model-first config once more for every ablated section. A model-reads turn can make a second call (a lookup or
    a repair), so it is counted twice."""
    spoken = sum(1 for case in cases for turn in case["turns"] if "say" in turn)
    total = 0
    for name in names:
        if not get(name).live:
            continue
        runs = 1 + (len(ablate) if get(name).architecture == "model_first" else 0)
        total += spoken * runs * (2 if get(name).architecture == "model_reads" else 1)
    return total


def cost_estimate_rupees(calls):
    return round(calls * RUPEES_PER_CALL, 2)


def scrub(text, env=None):
    """`text` with the Sarvam key (and anything shaped like a key header) removed, for anything printed or written."""
    env = os.environ if env is None else env
    key = (env.get("SARVAM_API_KEY") or "").strip()
    if key:
        text = text.replace(key, "***")
    return re.sub(r"(api-subscription-key\s*[:=]\s*)\S+", r"\1***", text, flags=re.IGNORECASE)


def pace_seconds():
    return math.ceil(1000.0 / MAX_CALLS_PER_SECOND) / 1000.0
