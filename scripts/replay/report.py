"""Turn replay results into the Excel report, the raw JSON and the one-screen text summary.

`runs` is a list of {"config": name, "label": name shown, "ablate": dropped section or None, "results": [conversation
results from engine.run_cases]}. Sheets: Read me, Summary, Conversations, Turns, Failures, Comparison (more than one
run), Ablation (an ablation was run). Everything is plain text and numbers; transcripts are in the file, so keep it
out of git and out of chat (replay_results/ is git-ignored).
"""

import json
import statistics
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from scripts.replay import engine
from scripts.replay.engine import CHECKS, FAIL, KNOWN_GAP, PASS, SKIPPED
from scripts.replay.xlsx_writer import (BOLD, BOLD_WRAP, Cell, GAP, HEADER, LINK, PASS as PASS_STYLE, FAIL as FAIL_STYLE,
                                        SKIP, TITLE, VERDICT_STYLES, WRAP, Workbook)

VERDICTS = (PASS, FAIL, KNOWN_GAP, SKIPPED)
OFFLINE_WARNING = ("SCRIPTED CONFIGS DO NOT MEASURE THE MODEL. In classic_scripted, model_first_scripted and model_reads_scripted the "
                   "planner's answer on every turn is the golden answer written in the conversation file (for model reads, the "
                   "golden SQL), not what Sarvam would say. They test the app: the state card, the dispatcher, the validators, "
                   "the views and guardrails, the memory carry-over and the fallbacks. Only the *_live configs measure how well the real model understands.")


def now_utc():
    return datetime.now(timezone.utc)


def stamp(moment=None):
    return (moment or now_utc()).strftime("%Y%m%dT%H%M%SZ")


def _counts(items):
    out = OrderedDict((v, 0) for v in VERDICTS)
    for verdict in items:
        out[verdict] += 1
    return out


def _rate(counts):
    scored = counts[PASS] + counts[FAIL] + counts[KNOWN_GAP]
    return round(100.0 * counts[PASS] / scored, 1) if scored else None


def _percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


def run_stats(run):
    """The numbers the Summary sheet and the text summary show for one run."""
    convs = run["results"]
    turns = [t for c in convs for t in c["turns"]]
    planner_ms = [t["planner_ms"] for t in turns if t.get("planner_ms") is not None]
    live = run.get("live", False)
    return {
        "label": run["label"], "conversations": len(convs), "conv": _counts(c["verdict"] for c in convs),
        "turns": len(turns), "turn": _counts(t["verdict"] for t in turns),
        "planner_calls": sum(1 for t in turns if t.get("tokens_in") is not None) if live else None,
        "p50": _percentile(planner_ms, 0.5) if live else None, "p95": _percentile(planner_ms, 0.95) if live else None,
        "cost_rupees": round(sum((t.get("cost_paise") or 0) for t in turns) / 100.0, 2) if live else None,
    }


def text_summary(runs, path=None):
    lines = []
    if path:
        lines.append("Report: {}".format(path))
    lines.append("")
    lines.append("{:<34} {:>5} {:>6} {:>6} {:>9}   {:>6} {:>6} {:>6} {:>9}".format(
        "run", "convs", "PASS", "FAIL", "KNOWNGAP", "turns", "PASS", "FAIL", "KNOWNGAP"))
    for run in runs:
        s = run_stats(run)
        lines.append("{:<34} {:>5} {:>6} {:>6} {:>9}   {:>6} {:>6} {:>6} {:>9}".format(
            s["label"][:34], s["conversations"], s["conv"][PASS], s["conv"][FAIL], s["conv"][KNOWN_GAP],
            s["turns"], s["turn"][PASS], s["turn"][FAIL], s["turn"][KNOWN_GAP]))
        skipped = s["conv"][SKIPPED]
        if skipped:
            lines.append("{:<34} ({} conversation(s) skipped: not meaningful under this config)".format("", skipped))
    lines.append("")
    lines.append(OFFLINE_WARNING)
    return "\n".join(lines)


# -- the sheets -------------------------------------------------------------------------------------

def _readme(book, runs, meta):
    sheet = book.add_sheet("Read me", widths=[150])
    rows = [
        (TITLE, "Replay report: how each conversation was evaluated"),
        (None, "Generated {} (UTC). Clinic clock during the replay: Friday 2026-10-09, 10:00 IST. Every name and number is made up.".format(meta["generated"])),
        (None, "Runs in this file: {}.".format("; ".join("{} ({})".format(r["label"], engine_desc(r)) for r in runs))),
        (None, ""),
        (BOLD_WRAP, "IMPORTANT LIMITATION"),
        (None, OFFLINE_WARNING),
        (None, ""),
        (BOLD, "How a conversation is scored"),
        (None, "Each conversation is a script of turns (what the user says, a golden planner answer, and what the app should do). The replay drives the real voice assistant with a fresh in-memory clinic, turn by turn, and checks every turn separately."),
        (None, "PASS = every check the turn made passed. FAIL = at least one check failed. KNOWN GAP = it failed, but the conversation is marked as behaviour that is not built yet (the reason is in the Conversations sheet). SKIPPED = the conversation is only meaningful under another config."),
        (None, "A conversation's verdict is FAIL if any turn FAILs or a final database check fails, otherwise KNOWN GAP if any turn is a known gap, otherwise PASS."),
        (None, ""),
        (BOLD, "Sheets"),
        (None, "Summary: per run, conversations and turns by verdict, pass rate (of scored items, skipped excluded), by category, planner latency p50 / p95 and cost (live runs only)."),
        (None, "Conversations: one row per conversation per run, with the verdict, the first failing turn, the final database checks and a link to that conversation's first row in Turns."),
        (None, "Turns: one row per turn per run (details below). Failures: only FAIL and KNOWN GAP turns, with the reason. Comparison: the verdict of every turn in each run side by side, and where they differ. Ablation: only when run (live): which turns stop passing when a piece of the state card is removed."),
        (None, ""),
        (BOLD, "Turns columns"),
        (None, "Said: what the user said (or the action: tap, approve, wait, network, setting). State card sent: the text sent to the planner in front of the sentence (model-first only). Planner tool + args: the tool call the planner log recorded (none when a rule decided). Route: planner / rules / fallback to classic."),
        (None, "App result: kind (ask, card, read, note, error, navigate, card_update ...), intent, question kind, slots, the patient the app resolved (by name key), options offered, the app's message. Expected: the turn's expectations in words."),
        (None, "Checks (PASS / FAIL, blank = the turn made no expectation): intent_ok (the intent), kind_ok (what kind of answer), ask_kind_ok (which question), slots_ok (slot values, number of options, rows), patient_ok (who was resolved), note_ok (what the message says), state_ok (the memory afterwards: question open, task, card, remembered patient, list), write_ok (nothing was written before a person pressed Approve), route_ok (which route decided it: planner, rules, fallback), card_ok (model-first: the state card shows what it should and no ids, long numbers or tokens, and is within the size limit)."),
        (None, "Why it failed: the failing checks in plain words. Latency / tokens / cost are filled only for live runs."),
        (None, ""),
        (BOLD, "Privacy"),
        (None, "This file contains the made-up conversations and, for live runs, the text the real model was sent. Do not put real patient data in conversation files. replay_results/ is ignored by git; keep reports out of chat and version control."),
        (None, ""),
        (BOLD, "Rollback"),
        (None, "Settings -> Command understanding switches the live app between Classic and New (model first) instantly; nothing here changes it."),
    ]
    for style, text in rows:
        sheet.append([Cell(text, style=style or WRAP)])


def engine_desc(run):
    from scripts.replay import configs
    cfg = configs.CONFIGS.get(run["config"])
    text = cfg.description if cfg else run["config"]
    return text + (" -- state card section dropped: {}".format(run["ablate"]) if run.get("ablate") else "")


def _summary(book, runs):
    sheet = book.add_sheet("Summary", widths=[36, 15, 14, 8, 11, 9, 11, 8, 8, 8, 12, 11, 11, 9, 9, 9])
    sheet.append(["Run", "Conversations", "PASS", "FAIL", "KNOWN GAP", "SKIPPED", "Pass rate %", "Turns", "T PASS", "T FAIL",
                  "T KNOWN GAP", "T SKIPPED", "Turn pass %", "p50 ms", "p95 ms", "Cost Rs"], header=True)
    for run in runs:
        s = run_stats(run)
        sheet.append([s["label"], s["conversations"], s["conv"][PASS], s["conv"][FAIL], s["conv"][KNOWN_GAP], s["conv"][SKIPPED],
                      _rate(s["conv"]), s["turns"], s["turn"][PASS], s["turn"][FAIL], s["turn"][KNOWN_GAP], s["turn"][SKIPPED],
                      _rate(s["turn"]),
                      s["p50"] if s["p50"] is not None else "n/a", s["p95"] if s["p95"] is not None else "n/a",
                      s["cost_rupees"] if s["cost_rupees"] is not None else "n/a"])
    sheet.append([])
    sheet.append([Cell("SCRIPTED CONFIGS DO NOT MEASURE THE MODEL: their planner answers are the golden ones in the conversation files (see Read me).", style=BOLD)])
    sheet.append([])
    sheet.append([Cell("By category (conversations)", style=BOLD)])
    sheet.append(["Run", "Category", "Conversations", "PASS", "FAIL", "KNOWN GAP", "SKIPPED", "Pass rate %"], header=True)
    for run in runs:
        by_cat = OrderedDict()
        for conv in run["results"]:
            by_cat.setdefault(conv["category"], []).append(conv["verdict"])
        for category, verdicts in by_cat.items():
            c = _counts(verdicts)
            sheet.append([run["label"], category, len(verdicts), c[PASS], c[FAIL], c[KNOWN_GAP], c[SKIPPED], _rate(c)])


def _conversations(book, runs, turn_rows):
    sheet = book.add_sheet("Conversations", widths=[30, 16, 46, 16, 10, 7, 11, 9, 60, 60, 14], freeze=(1, 0), autofilter=True)
    sheet.append(["Run", "Conversation", "Title", "Category", "Language", "Turns", "Verdict", "First failing turn",
                  "Why (first failure)", "Final database checks", "Open turns"], header=True)
    for run in runs:
        for conv in run["results"]:
            first = next((t for t in conv["turns"] if t["turn"] == conv["first_fail"]), None)
            why = (first["why"] if first else "") or (conv.get("skip_reason") or "")
            if conv.get("known_gap") and conv["verdict"] != PASS:
                why = "KNOWN GAP: {}. {}".format(conv["known_gap"], why).strip()
            db_text = "\n".join("{} {}".format("PASS" if c["ok"] else "FAIL", c["check"] + ("" if c["ok"] else " -- " + c["why"]))
                                for c in conv["final_db"])
            target = turn_rows.get((run["label"], conv["id"]), 1)
            sheet.append([run["label"], conv["id"], conv["title"], conv["category"], conv["language"], len(conv["turns"]),
                          Cell(conv["verdict"], style=VERDICT_STYLES[conv["verdict"]]), conv["first_fail"] or "",
                          Cell(why, style=WRAP), Cell(db_text, style=WRAP), Cell("turns", style=LINK, link=("Turns", target))])


def _slots_text(slots):
    return ", ".join("{}={}".format(k, v) for k, v in slots.items() if v not in (None, "", [], False)) if slots else ""


def _turns(book, runs):
    """The Turns sheet; returns {(run label, conversation id): first row number}."""
    widths = [28, 18, 6, 8, 40, 60, 40, 26, 10, 18, 12, 40, 18, 30, 50, 44] + [9] * len(CHECKS) + [11, 56, 9, 9, 9, 9, 9, 44]
    sheet = book.add_sheet("Turns", widths=widths, freeze=(1, 4), autofilter=True)
    sheet.append(["Run", "Conversation", "Turn", "Action", "Said", "State card sent (model-first)", "Planner tool + args",
                  "Route", "App kind", "App intent", "Question kind", "Slots", "Patient resolved", "Options offered",
                  "App message", "Expected"] + list(CHECKS) + ["Verdict", "Why it failed", "Planner ms", "Turn ms",
                  "Tokens in", "Tokens out", "Cost paise", "Memory after"], header=True)
    first_rows = {}
    for run in runs:
        for conv in run["results"]:
            for t in conv["turns"]:
                row_number = sheet.next_row()
                first_rows.setdefault((run["label"], conv["id"]), row_number)
                result = t["result"] or {}
                tool = ""
                if t.get("tool"):
                    tool = "{}({})".format(t["tool"], t.get("tool_args") or "")
                elif t.get("action") == "say":
                    tool = "none"
                memory = t.get("memory") or {}
                memory_text = "; ".join("{}={}".format(k, v) for k, v in memory.items() if v not in (None, 0, "")) if memory else ""
                checks = []
                for name in CHECKS:
                    ok = (t["checks"].get(name) or {}).get("ok")
                    checks.append(Cell("PASS" if ok else "FAIL", style=PASS_STYLE if ok else FAIL_STYLE) if ok is not None else "")
                sheet.append([run["label"], conv["id"], t["turn"], t["action"] or "", Cell(t["said"], style=WRAP),
                              Cell(t["state_card"] or "", style=WRAP), Cell(tool, style=WRAP), Cell(t["route"] or "", style=WRAP),
                              result.get("kind") or "", result.get("intent") or "", result.get("ask_kind") or "",
                              Cell(_slots_text(result.get("slots")), style=WRAP), result.get("patient") or "",
                              Cell("\n".join(result.get("options") or []), style=WRAP), Cell(result.get("note") or "", style=WRAP),
                              Cell(t["expected"], style=WRAP)] + checks +
                             [Cell(t["verdict"], style=VERDICT_STYLES[t["verdict"]]), Cell(t["why"], style=WRAP),
                              t.get("planner_ms") if t.get("planner_ms") is not None else "", t.get("turn_ms") if t.get("turn_ms") is not None else "",
                              t.get("tokens_in") if t.get("tokens_in") is not None else "",
                              t.get("tokens_out") if t.get("tokens_out") is not None else "",
                              t.get("cost_paise") if t.get("cost_paise") is not None else "", Cell(memory_text, style=WRAP)])
    return first_rows


def _failures(book, runs):
    sheet = book.add_sheet("Failures", widths=[28, 18, 6, 11, 40, 70, 44, 50, 30], freeze=(1, 0), autofilter=True)
    sheet.append(["Run", "Conversation", "Turn", "Verdict", "Said", "Why it failed", "Expected", "App message", "Planner tool"], header=True)
    known = {}
    for run in runs:
        for conv in run["results"]:
            known[(run["label"], conv["id"])] = conv.get("known_gap")
            for t in conv["turns"]:
                if t["verdict"] in (FAIL, KNOWN_GAP):
                    why = t["why"]
                    if t["verdict"] == KNOWN_GAP:
                        why = "KNOWN GAP ({}): {}".format(conv.get("known_gap"), why)
                    sheet.append([run["label"], conv["id"], t["turn"], Cell(t["verdict"], style=VERDICT_STYLES[t["verdict"]]),
                                  Cell(t["said"], style=WRAP), Cell(why, style=WRAP), Cell(t["expected"], style=WRAP),
                                  Cell((t["result"] or {}).get("note") or "", style=WRAP), t.get("tool") or "none"])
            for c in conv["final_db"]:
                if not c["ok"]:
                    sheet.append([run["label"], conv["id"], "final", Cell(KNOWN_GAP if conv.get("known_gap") else FAIL,
                                  style=GAP if conv.get("known_gap") else FAIL_STYLE), "(end of conversation)",
                                  Cell("final database check failed: {} -- {}".format(c["check"], c["why"]), style=WRAP), "", "", ""])


def _comparison(book, runs):
    sheet = book.add_sheet("Comparison", widths=[18, 6, 46] + [18] * len(runs) + [10, 70], freeze=(1, 3), autofilter=True)
    sheet.append(["Conversation", "Turn", "Said"] + [r["label"] for r in runs] + ["Differs?", "Why (first run that failed)"], header=True)
    index = []
    for run in runs:
        index.append({(c["id"], t["turn"]): t for c in run["results"] for t in c["turns"]})
    keys = OrderedDict()
    for run in runs:
        for c in run["results"]:
            for t in c["turns"]:
                keys.setdefault((c["id"], t["turn"]), t["said"])
    for (conv_id, number), said in keys.items():
        verdicts = [(table.get((conv_id, number)) or {}).get("verdict", "") for table in index]
        why = next(((table.get((conv_id, number)) or {}).get("why", "") for table, v in zip(index, verdicts) if v in (FAIL, KNOWN_GAP)), "")
        differs = len({v for v in verdicts if v and v != SKIPPED}) > 1
        sheet.append([conv_id, number, Cell(said, style=WRAP)] +
                     [Cell(v, style=VERDICT_STYLES[v]) if v else "" for v in verdicts] +
                     [Cell("YES" if differs else "", style=BOLD if differs else None), Cell(why, style=WRAP)])


def _ablation(book, runs):
    baseline = next((r for r in runs if r["config"] == "model_first_live" and not r.get("ablate")), None)
    dropped = [r for r in runs if r.get("ablate")]
    sheet = book.add_sheet("Ablation", widths=[16, 18, 6, 44, 12, 12, 70], freeze=(1, 0), autofilter=True)
    sheet.append(["Dropped section", "Conversation", "Turn", "Said", "Baseline", "Without it", "Why it failed"], header=True)
    if baseline is None:
        sheet.append(["(no baseline model_first_live run in this file)"])
        return
    base = {(c["id"], t["turn"]): t for c in baseline["results"] for t in c["turns"]}
    for run in dropped:
        for c in run["results"]:
            for t in c["turns"]:
                before = base.get((c["id"], t["turn"]))
                if before and before["verdict"] == PASS and t["verdict"] in (FAIL, KNOWN_GAP):
                    sheet.append([run["ablate"], c["id"], t["turn"], Cell(t["said"], style=WRAP), Cell(PASS, style=PASS_STYLE),
                                  Cell(t["verdict"], style=VERDICT_STYLES[t["verdict"]]), Cell(t["why"], style=WRAP)])


def build_workbook(runs, meta=None):
    meta = dict(meta or {})
    meta.setdefault("generated", now_utc().strftime("%Y-%m-%d %H:%M"))
    book = Workbook()
    _readme(book, runs, meta)
    _summary(book, runs)
    # Conversations needs the Turns row numbers, so the sheets are built in an order that lets it link, then reordered.
    conv_book = Workbook()
    turn_rows = _turns(conv_book, runs)
    _conversations(book, runs, turn_rows)
    first_rows = _turns(book, runs)
    assert first_rows == turn_rows
    _failures(book, runs)
    if len(runs) > 1:
        _comparison(book, runs)
    if any(r.get("ablate") for r in runs):
        _ablation(book, runs)
    return book


def write_report(runs, out_dir="replay_results", meta=None, moment=None):
    """Write replay_<UTC stamp>.xlsx and .json into out_dir. Returns (xlsx path, json path)."""
    moment = moment or now_utc()
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    name = "replay_{}".format(stamp(moment))
    xlsx = folder / (name + ".xlsx")
    meta = dict(meta or {}, generated=moment.strftime("%Y-%m-%d %H:%M"))
    build_workbook(runs, meta).save(str(xlsx))
    raw = folder / (name + ".json")
    raw.write_text(json.dumps({"meta": meta, "runs": runs}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return xlsx, raw
