"""Run the intent router's labelled set against a live local Ollama model.

    PYTHONPATH=. .venv/bin/python scripts/eval_intent.py [model ...]

Reports accuracy, the misroutes, and median/p95 latency per model. Read-only:
it only calls Ollama; nothing touches the database. Used to pick the smallest
model that passes (target: >= 95% and p95 <= 2.5 s).
"""

import os
import statistics
import sys
import time

from clinic.nlu.intent_llm import pick_intent
from clinic.nlu.llm_slots import MODEL
from tests.intent_eval_cases import CASES

# Importing the tests package switches the router off (tests must never call a
# live model); this script is the live check, so switch it back on.
os.environ["INTENT_LLM_ENABLED"] = "1"


def evaluate(model):
    pick_intent("warm up", model=model, timeout=120)
    wrong, times = [], []
    for text, expected in CASES:
        start = time.perf_counter()
        got = pick_intent(text, model=model, timeout=30)
        times.append(time.perf_counter() - start)
        if got != expected:
            wrong.append((text, expected, got))
    total = len(CASES)
    print("\n== {} ==  {}/{} correct ({:.0%})".format(model, total - len(wrong), total, (total - len(wrong)) / total))
    print("latency: median {:.2f}s  p95 {:.2f}s".format(
        statistics.median(times), sorted(times)[int(len(times) * 0.95) - 1]))
    for text, expected, got in wrong:
        print("  MISS  {!r}\n        expected={} got={}".format(text, expected, got))
    return len(wrong)


if __name__ == "__main__":
    for name in (sys.argv[1:] or [MODEL]):
        evaluate(name)
