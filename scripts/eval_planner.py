"""Run the tool-calling planner against a live local Ollama model (sequential, slow).

    PYTHONPATH=. .venv/bin/python scripts/eval_planner.py            # the 65 planner cases
    PYTHONPATH=. .venv/bin/python scripts/eval_planner.py --cases reads   # only the new read cases (record types, sums, sort, month ranges)
    PYTHONPATH=. .venv/bin/python scripts/eval_planner.py --replay   # the 82 routing cases: new path vs the old label picker

Both modes talk only to the local model (clinic/nlu/llm_slots.py MODEL, Gemma 4 12B
by default) and use a throw-away in-memory database: nothing touches clinic.db and
nothing is written anywhere. Calls are made one after another, as the machine cannot
hold two big models; expect 4-8 s per command (more when the Mac is swapping).

Planner cases (tests/planner_eval_cases.py): each command is planned exactly as the
app plans it (the real prompt, validation, the date / time safety net). Two scores
are printed: "raw" is the model's own call as it answered; "final" is what the app
would act on after validation and the safety net. A miss lists the expected and
actual call. "write when it should refuse" counts commands that should have been a
clarifying question or a refusal but became a write-type call.

Replay (tests/intent_eval_cases.py): every labelled command is routed twice on the
same model -- by the old one-word label picker (intent_llm.pick_intent) and by the
new path (precise rules, then the planner, then the picker as fallback) -- and every
REGRESSION (old right, new wrong) is listed with the improvements. The score of the
"unmatched" scope (the planner only for commands the keyword rules cannot place) is
worked out from the same answers.
"""

import argparse
import json
import os
import statistics
import sys
import time
from datetime import date
from unittest.mock import patch

from tests.intent_eval_cases import CASES as LABEL_CASES
from tests.planner_eval_cases import CASES, READ_CASES

# Importing the tests package switches the live model off (tests must never call it);
# this script is the live check, so switch it back on.
os.environ["INTENT_LLM_ENABLED"] = "1"
os.environ["INTENT_PLANNER_ENABLED"] = "1"
os.environ.setdefault("PLANNER_TIMEOUT_S", "90")      # measure the model, not the app's 12 s limit

from clinic import branches, db  # noqa: E402
from clinic.nlu import llm_slots, parser, planner, tools  # noqa: E402
from clinic.nlu.classify import classify  # noqa: E402
from clinic.nlu.intent_llm import pick_intent  # noqa: E402

TODAY = date(2026, 10, 6)       # the cases were written for this Tuesday
REFUSALS = ("clarify", "unsupported")
WRITES = set(tools.WRITE_TOOLS) | {"close_branch", "doctor_leave"}


def scratch_db():
    """Branch A + Dr. Mehta (from the schema), Branches B and C, Dr. Rao and Dr. Iyer."""
    conn = db.connect(":memory:")
    b = branches.add_branch(conn, "B", "Branch B", pin_code="122011")
    c = branches.add_branch(conn, "C", "Branch C", pin_code="122018")
    rao = branches.add_doctor(conn, "Dr. Rao")
    iyer = branches.add_doctor(conn, "Dr. Iyer")
    for weekday in range(7):
        branches.add_schedule(conn, rao, b, weekday, "10:00", "14:00")
        branches.add_schedule(conn, iyer, c, weekday, "09:00", "12:00")
    return conn


class Recording(planner.OllamaBackend):
    """The real Ollama backend that remembers the call exactly as the model made it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last = None

    def plan(self, system, user, tool_schemas):
        self.last = None
        self.last = super().plan(system, user, tool_schemas)
        return self.last


def _norm(value):
    return str(value).strip().casefold().replace("dr.", "").replace("dr ", "").replace("branch ", "").strip()


def arg_ok(want, got):
    if got is None:
        return False
    if want == "*":                      # any non-empty value (the model's own words)
        return bool(str(got).strip())
    if isinstance(want, int):
        try:
            return int(got) == want
        except (TypeError, ValueError):
            return False
    got_n = _norm(got)
    return any(got_n == _norm(w) or (len(_norm(w)) > 2 and _norm(w) in got_n) for w in str(want).split("|"))


def score(expected_tool, expected_args, tool, args):
    """(right tool, right tool and every expected argument, the argument names that were wrong)."""
    if tool != expected_tool:
        return False, False, []
    args = args or {}
    # `text` and `patient_name` are one filter in the read tool (clinic/query_tool.py): either spelling is right
    alias = {"text": "patient_name", "patient_name": "text"}
    bad = [k for k, want in expected_args.items() if not arg_ok(want, args.get(k) if args.get(k) is not None else args.get(alias.get(k)))]
    return True, not bad, bad


def previous_of(history):
    if not history:
        return None
    text, call, result = history
    return planner.format_previous({"text": text, "call": call, "result": result})


def save(rows, out):
    """Keep the results so far on disk: a run can take hours on a busy machine."""
    if out:
        with open(out, "w") as handle:
            json.dump(rows, handle, ensure_ascii=False, indent=1, default=str)


def plan_cases(conn, backend, limit=0, out="", cases=None):
    cases = cases if cases is not None else CASES
    cases = cases[:limit] if limit else cases
    planner.PlannerRun(conn, "Thank you", today=TODAY, backend=backend).ask()            # warm-up: load the model
    rows, times = [], []
    for text, expected_tool, expected_args, history in cases:
        run = planner.PlannerRun(conn, text, today=TODAY, backend=backend)
        run.previous = previous_of(history)
        started = time.perf_counter()
        planned = run.ask()
        seconds = time.perf_counter() - started
        times.append(seconds)
        raw = backend.last
        raw_tool, raw_args = (raw.name, raw.args) if raw else (None, {})
        raw_tool_ok, raw_ok, raw_bad = score(expected_tool, expected_args, raw_tool, raw_args if isinstance(raw_args, dict) else {})
        final_tool, final_args = (planned.tool, planned.args) if planned else (None, {})
        tool_ok, args_ok, bad = score(expected_tool, expected_args, final_tool, final_args)
        rows.append({"text": text, "expected": expected_tool, "expected_args": expected_args,
                     "raw_tool": raw_tool, "raw_args": raw_args, "raw_tool_ok": raw_tool_ok, "raw_args_ok": raw_ok,
                     "tool": final_tool, "args": final_args, "tool_ok": tool_ok, "args_ok": args_ok, "bad_args": bad,
                     "rejected": run.error if planned is None else None, "notes": run.notes, "sec": round(seconds, 2)})
        print("{} {:>5.1f}s {!r} -> {} {}".format("OK  " if args_ok else ("TOOL" if tool_ok else "MISS"), seconds, text[:52],
                                                   final_tool or raw_tool, bad or (run.error or "")), flush=True)
        save(rows, out)
    return rows, times


def report_plan(rows, times, model):
    n = len(rows)
    refused_wrongly = [r for r in rows if r["expected"] in REFUSALS and (r["raw_tool"] in WRITES)]
    refused_final = [r for r in rows if r["expected"] in REFUSALS and (r["tool"] in WRITES)]
    ordered = sorted(times)
    print("\n== {} planner ==  {} cases".format(model, n))
    print("raw   (model as it answered):  right tool {:.0%}  tool+args {:.0%}".format(
        sum(r["raw_tool_ok"] for r in rows) / n, sum(r["raw_args_ok"] for r in rows) / n))
    print("final (after validation + safety net):  right tool {:.0%}  tool+args {:.0%}  rejected calls {}".format(
        sum(r["tool_ok"] for r in rows) / n, sum(r["args_ok"] for r in rows) / n, sum(1 for r in rows if r["rejected"])))
    print("write when it should clarify/refuse:  raw {}  final {}".format(len(refused_wrongly), len(refused_final)))
    print("latency: median {:.1f}s  p95 {:.1f}s  max {:.1f}s".format(statistics.median(times), ordered[max(0, int(n * 0.95) - 1)], ordered[-1]))
    overridden = [r for r in rows if r["notes"] and any("->" in note for note in r["notes"])]
    print("date/time overrides by the safety net: {}".format(len(overridden)))
    for r in overridden:
        print("  OVERRIDE {!r}: {}".format(r["text"], "; ".join(r["notes"])))
    for r in rows:
        if not r["args_ok"]:
            print("  MISS {!r}\n       expected {} {}\n       final    {} {}  {}\n       raw      {} {}".format(
                r["text"], r["expected"], r["expected_args"], r["tool"], r["args"], r["rejected"] or r["bad_args"],
                r["raw_tool"], r["raw_args"]))


def report_entities(rows):
    """Which record types the model mixes up: expected entity -> the entity it used, for the read cases."""
    confused = {}
    for r in rows:
        want = (r["expected_args"] or {}).get("entity")
        got = (r["args"] or {}).get("entity") if r["tool"] == "query" else (r["raw_args"] or {}).get("entity") if isinstance(r["raw_args"], dict) else None
        if want and r["tool"] == "query" and got != want:
            confused.setdefault((want, got), []).append(r["text"])
    wrong_tool = [r for r in rows if not r["tool_ok"]]
    print("\nentity confusions (expected -> used): {}".format(sum(len(v) for v in confused.values())))
    for (want, got), texts in sorted(confused.items(), key=lambda kv: -len(kv[1])):
        print("  {} -> {}: {}".format(want, got, "; ".join(repr(t) for t in texts)))
    print("calls to the wrong tool: {}".format(len(wrong_tool)))
    for r in wrong_tool:
        print("  {!r}: expected {} got {}".format(r["text"], r["expected"], r["tool"] or r["raw_tool"]))
    by_kind = {}
    for r in rows:
        kind = "unanswerable" if r["expected"] == "unsupported" else "read"
        by_kind.setdefault(kind, []).append(r)
    for kind, group in by_kind.items():
        print("{}: {} cases, right tool {:.0%}, tool+args {:.0%}".format(
            kind, len(group), sum(r["tool_ok"] for r in group) / len(group), sum(r["args_ok"] for r in group) / len(group)))


def replay(conn, backend, limit=0, out=""):
    cases = LABEL_CASES[:limit] if limit else LABEL_CASES
    pick_intent("warm up", model=llm_slots.MODEL, timeout=120)
    rows = []
    for text, expected in cases:
        started = time.perf_counter()
        old = pick_intent(text, model=llm_slots.MODEL, timeout=60)
        old_s = time.perf_counter() - started
        run = planner.PlannerRun(conn, text, today=date.today(), backend=backend)
        started = time.perf_counter()
        try:
            with patch("clinic.nlu.parser.extract_name", return_value=None), patch("clinic.nlu.parser.prefetch_name"):
                new, _ = parser.parse(text, planner=run)
        except parser.UnrecognizedCommand:
            new = None
        new_s = time.perf_counter() - started
        rules = classify(text)
        rows.append({"text": text, "expected": expected, "old": old, "new": new, "route": run.route, "rules": rules,
                     "tool": run.tool, "old_s": old_s, "new_s": new_s, "error": run.error})
        flag = "ok  " if new == expected else "MISS"
        print("{} old={} new={} ({}) {!r}".format(flag, old, new, run.route, text[:50]), flush=True)
        save(rows, out)
    return rows


def report_replay(rows, model):
    n = len(rows)
    old_right = [r for r in rows if r["old"] == r["expected"]]
    new_right = [r for r in rows if r["new"] == r["expected"]]
    # "unmatched" scope: the keyword rules decide when they match, the new path otherwise
    unmatched_right = [r for r in rows if (r["rules"] if r["rules"] is not None else r["new"]) == r["expected"]]
    print("\n== {} replay of the {} labelled commands ==".format(model, n))
    print("old label picker: {}/{} ({:.0%})   new path (scope all): {}/{} ({:.0%})   new path (scope unmatched): {}/{} ({:.0%})".format(
        len(old_right), n, len(old_right) / n, len(new_right), n, len(new_right) / n,
        len(unmatched_right), n, len(unmatched_right) / n))
    print("latency: old picker median {:.1f}s p95 {:.1f}s   new path median {:.1f}s p95 {:.1f}s".format(
        statistics.median(r["old_s"] for r in rows), sorted(r["old_s"] for r in rows)[max(0, int(n * 0.95) - 1)],
        statistics.median(r["new_s"] for r in rows), sorted(r["new_s"] for r in rows)[max(0, int(n * 0.95) - 1)]))
    routes = {}
    for r in rows:
        routes[r["route"]] = routes.get(r["route"], 0) + 1
    print("routes taken by the new path: {}".format(routes))
    regressions = [r for r in rows if r["old"] == r["expected"] and r["new"] != r["expected"]]
    improvements = [r for r in rows if r["old"] != r["expected"] and r["new"] == r["expected"]]
    print("\nREGRESSIONS (old right, new wrong): {}".format(len(regressions)))
    for r in regressions:
        print("  REGRESSION {!r}\n       expected={} old={} new={} (route {}, tool {}, {})".format(
            r["text"], r["expected"], r["old"], r["new"], r["route"], r["tool"], r["error"] or ""))
    print("improvements (old wrong, new right): {}".format(len(improvements)))
    for r in improvements:
        print("  FIXED {!r}: expected={} old={} new={}".format(r["text"], r["expected"], r["old"], r["new"]))
    both = [r for r in rows if r["old"] != r["expected"] and r["new"] != r["expected"]]
    print("wrong on both: {}".format(len(both)))
    for r in both:
        print("  BOTH {!r}: expected={} old={} new={} (route {}, tool {})".format(r["text"], r["expected"], r["old"], r["new"], r["route"], r["tool"]))
    unmatched_regressions = [r for r in rows if r["old"] == r["expected"] and (r["rules"] if r["rules"] is not None else r["new"]) != r["expected"]]
    print("scope 'unmatched' would regress: {}".format(len(unmatched_regressions)))
    for r in unmatched_regressions:
        print("  UNMATCHED-REGRESSION {!r}: expected={} rules={}".format(r["text"], r["expected"], r["rules"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", action="store_true", help="route the 82 labelled commands: new path vs the old label picker")
    ap.add_argument("--cases", choices=("planner", "reads"), default="planner",
                    help="planner: the 65 original cases; reads: only the new read cases (tests/planner_eval_cases.py READ_CASES)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="", help="write the per-case results as JSON")
    ap.add_argument("--report", default="", help="print the report for a saved --out file (also for a run that was stopped early)")
    args = ap.parse_args()
    if args.report:
        with open(args.report) as handle:
            saved = json.load(handle)
        if saved and "old" in saved[0]:
            report_replay(saved, llm_slots.MODEL)
        elif saved:
            report_plan(saved, [r["sec"] for r in saved], planner.PLANNER_MODEL)
        return
    conn = scratch_db()
    backend = Recording(model=planner.PLANNER_MODEL)
    print("model {}  planner timeout {}s  num_gpu {}".format(planner.PLANNER_MODEL, planner.planner_timeout(),
                                                              llm_slots.ollama_options().get("num_gpu", "auto")))
    if args.replay:
        rows = replay(conn, backend, args.limit, args.out)
        report_replay(rows, planner.PLANNER_MODEL)
    else:
        rows, times = plan_cases(conn, backend, args.limit, args.out, READ_CASES if args.cases == "reads" else CASES)
        report_plan(rows, times, planner.PLANNER_MODEL)
        if args.cases == "reads":
            report_entities(rows)


if __name__ == "__main__":
    sys.exit(main())
