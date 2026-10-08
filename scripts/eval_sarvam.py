"""Throwaway benchmark: the tool-calling planner on Sarvam's hosted chat model.

    PYTHONPATH=. .venv/bin/python scripts/eval_sarvam.py [--cases planner|reads] [--reasoning off|low|medium|high] [--limit N] --out FILE

The same cases, planner prompt, validation and date safety net as scripts/eval_planner.py (so the numbers
compare with the local models); only the model call differs. It sends ONLY the made-up sentences in
tests/planner_eval_cases.py (fake names) plus the planner prompt (calendar, branch and doctor names of a
throw-away in-memory database). No clinic data. The key comes from SARVAM_API_KEY (environment or .env) and is
never printed. Token usage is totalled and priced at Sarvam's published rates (Rs per 1M tokens).
"""

import argparse
import json
import os
import statistics
import sys

from dotenv import load_dotenv

from scripts import eval_planner as ev          # noqa: E402  (sets the env switches the planner needs)
from clinic.nlu import sarvam                   # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
MODEL = sarvam.MODEL


class SarvamBackend(sarvam.SarvamBackend):
    """The app's own Sarvam backend (clinic/nlu/sarvam.py) with this benchmark's bookkeeping on top:
    a long time limit, a breaker that never opens, and totals of the calls and tokens."""

    def __init__(self, model=MODEL, reasoning="off", timeout=60.0):
        super().__init__(model=model, timeout=timeout, retry_delay=2.0, reasoning_effort=None if reasoning == "off" else reasoning,
                         breaker=sarvam.CircuitBreaker(failures=10 ** 9))
        if not sarvam.api_key():
            sys.exit("SARVAM_API_KEY is not set (environment or .env)")
        self.last = None
        self.calls = 0
        self.errors = 0
        self.prompt_tokens = self.completion_tokens = self.cached_tokens = 0
        self.sample_usage = None

    def plan(self, system, user, tool_schemas):
        self.last = None
        try:
            self.last = super().plan(system, user, tool_schemas)
        except sarvam.SarvamError:
            self.errors += 1
            raise
        self.calls += 1
        usage = self.last_usage
        if usage is not None:
            self.sample_usage = self.sample_usage or usage
            self.prompt_tokens += usage.prompt_tokens
            self.completion_tokens += usage.completion_tokens
            self.cached_tokens += usage.cached_tokens
        return self.last

    def cost(self):
        """(Rs counting every prompt token at the uncached rate, Rs counting any cached tokens reported)."""
        per_m = 1e6
        uncached = self.prompt_tokens - self.cached_tokens
        upper = self.prompt_tokens * float(sarvam.RATE_INPUT_RS_PER_M) / per_m + self.completion_tokens * float(sarvam.RATE_OUTPUT_RS_PER_M) / per_m
        actual = (uncached * float(sarvam.RATE_INPUT_RS_PER_M) + self.cached_tokens * float(sarvam.RATE_CACHED_INPUT_RS_PER_M)
                  + self.completion_tokens * float(sarvam.RATE_OUTPUT_RS_PER_M)) / per_m
        return upper, actual


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", choices=("planner", "reads"), default="planner")
    ap.add_argument("--reasoning", choices=("off", "low", "medium", "high"), default="off")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    backend = SarvamBackend(args.model, args.reasoning)
    print("model {}  reasoning {}  cases {}".format(args.model, args.reasoning, args.cases), flush=True)
    conn = ev.scratch_db()
    cases = ev.READ_CASES if args.cases == "reads" else ev.CASES
    rows, times = ev.plan_cases(conn, backend, args.limit, args.out, cases)
    ev.report_plan(rows, times, args.model)
    upper, actual = backend.cost()
    n = max(backend.calls, 1)
    print("\nSARVAM USAGE  calls {}  errors {}  prompt tokens {}  (cached {})  completion tokens {}".format(
        backend.calls, backend.errors, backend.prompt_tokens, backend.cached_tokens, backend.completion_tokens))
    print("avg per call: {:.0f} in / {:.0f} out tokens".format(backend.prompt_tokens / n, backend.completion_tokens / n))
    print("COST  Rs {:.2f} at the uncached input rate;  Rs {:.2f} counting any cached tokens reported;  per call Rs {:.3f}".format(
        upper, actual, upper / n))
    print("sample usage: {}".format(json.dumps(backend.sample_usage._asdict() if backend.sample_usage else None)))


if __name__ == "__main__":
    sys.exit(main())
