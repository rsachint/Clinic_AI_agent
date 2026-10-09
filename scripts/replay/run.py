"""Replay scripted conversations through the voice assistant and write an Excel report.

    PYTHONPATH=. .venv/bin/python -m scripts.replay.run --config classic_rules_only classic_scripted model_first_scripted
    PYTHONPATH=. .venv/bin/python -m scripts.replay.run --list
    PYTHONPATH=. .venv/bin/python -m scripts.replay.run --config model_first_scripted --cases nalin rahul --category regression

Offline configs (no network, no model, no key) are the default and free. The live configs call the real Sarvam
model and cost money: they need --live AND --yes AND SARVAM_API_KEY in the environment, print the estimated number of
calls and rupees first, and run one call at a time under about three per second:

    PYTHONPATH=. .venv/bin/python -m scripts.replay.run --config classic_live model_first_live --live --yes
    PYTHONPATH=. .venv/bin/python -m scripts.replay.run --config model_first_live --live --yes --ablate pending,card,remembered,list,last_turns,task

--ablate (live only) runs model_first_live once more for each listed state card section dropped, so the Ablation sheet
shows which turns break without each piece of conversation memory. The scripted configs ignore it.
Output: replay_results/replay_<UTC timestamp>.xlsx and a sibling .json of the raw results (git-ignored).
"""

import argparse
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.replay import configs, engine, report            # noqa: E402


def load_cases():
    from tests.replay_cases import ALL_CASES
    return ALL_CASES


def select(cases, ids=None, categories=None):
    out = []
    for case in cases:
        if ids and not any(needle in case["id"] for needle in ids):
            continue
        if categories and case["category"] not in categories:
            continue
        out.append(case)
    return out


def load_key_from_dotenv():
    """The Sarvam key from the app's .env into the environment (only that one value, only if the environment has none),
    read only after the live flags were checked. Nothing is printed."""
    if (os.environ.get("SARVAM_API_KEY") or "").strip():
        return
    try:
        from dotenv import dotenv_values
        key = (dotenv_values().get("SARVAM_API_KEY") or "").strip()
    except Exception:
        key = ""
    if key:
        os.environ["SARVAM_API_KEY"] = key


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", nargs="+", default=list(configs.OFFLINE), help="configs to run (default: the offline ones)")
    ap.add_argument("--cases", nargs="+", help="only conversations whose id contains one of these words")
    ap.add_argument("--category", nargs="+", help="only these categories")
    ap.add_argument("--ablate", help="live only: comma-separated state card sections to drop one at a time")
    ap.add_argument("--live", action="store_true", help="allow the live configs (real Sarvam model)")
    ap.add_argument("--yes", action="store_true", help="confirm the cost of a live run")
    ap.add_argument("--out", default="replay_results", help="output folder (default: replay_results)")
    ap.add_argument("--list", action="store_true", help="list the conversations and exit")
    ap.add_argument("--verbose", action="store_true", help="print every conversation's verdict as it finishes")
    return ap.parse_args(argv)


def run(argv=None, out=sys.stdout):
    args = parse_args(argv)
    cases = select(load_cases(), args.cases, args.category)
    if not cases:
        out.write("No conversation matches.\n")
        return 2
    if args.list:
        for case in cases:
            out.write("{:<34} {:<14} {:<9} {:>2} turns  {}\n".format(case["id"], case["category"], case["language"],
                                                                      len(case["turns"]), case["title"]))
        out.write("{} conversation(s), {} turns\n".format(len(cases), sum(len(c["turns"]) for c in cases)))
        return 0
    try:
        names = args.config
        for name in names:
            configs.get(name)
        configs.check_live(names, args.live, args.yes, require_key=False)
    except configs.ConfigError as exc:
        out.write(configs.scrub("Refused: {}\n".format(exc)))
        return 2

    sections = [s.strip() for s in (args.ablate or "").split(",") if s.strip()]
    from clinic import state_card
    unknown = [s for s in sections if s not in state_card.SECTIONS]
    if unknown:
        out.write("Unknown --ablate section(s): {}. Choose from {}.\n".format(", ".join(unknown), ", ".join(state_card.SECTIONS)))
        return 2
    live_names = [n for n in names if configs.get(n).live]
    if live_names:
        calls = configs.planner_calls_estimate(cases, names, sections)
        out.write("LIVE RUN: up to {} planner call(s), about Rs {:.2f} at Rs {} per call, one at a time under {:.0f} calls a second.\n".format(
            calls, configs.cost_estimate_rupees(calls), configs.RUPEES_PER_CALL, configs.MAX_CALLS_PER_SECOND))
        load_key_from_dotenv()
        try:
            configs.check_live(names, args.live, args.yes)
        except configs.ConfigError as exc:
            out.write(configs.scrub("Refused: {}\n".format(exc)))
            return 2
    if sections and not any(configs.get(n).live and configs.get(n).architecture == "model_first" for n in names):
        out.write("Note: --ablate only applies to model_first_live; the scripted configs ignore it.\n")

    runs = []

    def show(config, result):
        if args.verbose:
            out.write("  {:<28} {:<9} {}\n".format(result["id"][:28], result["verdict"], config.name))

    for name in names:
        config = configs.get(name)
        out.write("{}: {}\n".format(name, config.description))
        results = engine.run_cases(cases, config, allow_live=config.live and args.live and args.yes, progress=show)
        runs.append({"config": name, "label": name, "ablate": None, "live": config.live, "results": results})
        if sections and config.live and config.architecture == "model_first":
            for section in sections:
                out.write("{} without the state card's {} section\n".format(name, section))
                results = engine.run_cases(cases, config, drop=frozenset({section}), allow_live=True, progress=show)
                runs.append({"config": name, "label": "{} -{}".format(name, section), "ablate": section, "live": True,
                             "results": results})
    xlsx, raw = report.write_report(runs, args.out)
    out.write(configs.scrub(report.text_summary(runs, str(xlsx))) + "\n")
    out.write("Raw results: {}\n".format(raw))
    return 0


if __name__ == "__main__":
    sys.exit(run())
