"""Turn the planner's local command log into labelled test cases for a person to review.

    PYTHONPATH=. .venv/bin/python scripts/export_planner_log.py --db clinic.db > new_cases.py
    PYTHONPATH=. .venv/bin/python scripts/export_planner_log.py --db clinic.db --format json --route label_fallback

Every staff command is a row in the `planner_log` table (clinic/planner_log.py): one the planner answered, or
one a precise rule decided (route "rules" with a route_detail such as "rule:move"; those carry no tool call, only
which slots were found or missing).
This writes them in the same shape as tests/planner_eval_cases.py --

    (text, expected_tool, expected_args, history)

-- with what the planner called as the starting "expected" value, and a comment saying how
the command ended (which route decided it, whether the card was approved or rejected). It is
a draft: read each case, correct the tool and arguments where the planner was wrong, and
only then add it to a case file. A command that fell back, was rejected, or whose card a
person rejected is the most useful kind (`--problems`).

PRIVACY: transcripts contain patient names. The log lives only in the local database and so
does this export: keep the output file out of git and out of chat. The database is opened
read-only; nothing is changed.
"""

import argparse
import ast
import json
import re
import sqlite3
import sys
from pathlib import Path

_PREVIOUS = re.compile(r"^user said: (?P<text>'.*?'|\".*?\"); you called (?P<call>.*?); result: (?P<result>.*)$", re.S)


def open_read_only(path):
    """The database without any chance of changing it (and without creating the tables)."""
    uri = "file:{}?mode=ro".format(Path(path).resolve().as_posix())
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def parse_previous(previous):
    """The planner log's "user said: 'x'; you called f(...); result: y" back into
    (previous_user_text, tool_called, result_text), or None."""
    if not previous:
        return None
    m = _PREVIOUS.match(previous)
    if not m:
        return None
    try:
        said = ast.literal_eval(m.group("text"))
    except (ValueError, SyntaxError):
        said = m.group("text").strip("'\"")
    return (said, m.group("call"), m.group("result"))


def fetch(conn, since=None, route=None, outcome=None, problems=False, limit=None):
    sql, params = "SELECT * FROM planner_log WHERE 1 = 1", []
    if since:
        sql += " AND ts >= ?"
        params.append(since)
    if route:
        sql += " AND route_taken = ?"
        params.append(route)
    if outcome:
        sql += " AND outcome = ?"
        params.append(outcome)
    if problems:
        # A rule-routed command (route_detail set) is a problem only when something went wrong around it (a failed
        # hosted name read leaves a note) or its card was rejected.
        has_detail = any(r[1] == "route_detail" for r in conn.execute("PRAGMA table_info(planner_log)"))     # an older database lacks it
        sql += " AND ((route_taken != 'planner'{}) OR outcome = 'rejected' OR override_notes IS NOT NULL)".format(
            " AND route_detail IS NULL" if has_detail else "")
    sql += " ORDER BY id"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def to_case(row):
    """One log row as (text, expected_tool, expected_args, history, note)."""
    args = json.loads(row["planner_args_json"]) if row["planner_args_json"] and row["planner_tool"] else {}
    note = "{} route={}{} intent={}{}{}".format(
        row["ts"], row["route_taken"], "/" + row["route_detail"] if row.get("route_detail") else "", row["final_intent"],
        " outcome=" + row["outcome"] if row["outcome"] else "",
        " notes=" + row["override_notes"] if row["override_notes"] else "")
    return (row["transcript"], row["planner_tool"], args if isinstance(args, dict) else {}, parse_previous(row["previous_turn"]), note)


def as_python(cases):
    lines = [
        '"""Draft planner cases exported from the local planner log: REVIEW EVERY ONE before using it.',
        "",
        "The expected tool and arguments are what the planner called, not what is right. Correct them,",
        "then move the case into tests/planner_eval_cases.py. Transcripts contain patient names: do not",
        'commit or share this file."""',
        "",
        "CASES = [",
    ]
    for text, tool, args, history, note in cases:
        lines.append("    # " + note.replace("\n", " "))
        lines.append("    ({!r}, {!r}, {!r}, {!r}),".format(text, tool, args, history))
    lines.append("]")
    return "\n".join(lines) + "\n"


def as_json(cases):
    return json.dumps([{"text": t, "expected_tool": tool, "expected_args": args, "history": history, "note": note}
                       for t, tool, args, history, note in cases], ensure_ascii=False, indent=1) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default="clinic.db", help="the clinic database (opened read-only)")
    ap.add_argument("--format", choices=("py", "json"), default="py")
    ap.add_argument("--since", help="only rows at or after this date / time (YYYY-MM-DD[ HH:MM:SS])")
    ap.add_argument("--route", choices=("rules", "planner", "label_fallback", "rephrase"))
    ap.add_argument("--outcome", choices=("approved", "rejected", "edited"))
    ap.add_argument("--problems", action="store_true", help="only commands that fell back, were overridden or were rejected")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args(argv)
    if not Path(args.db).exists():
        sys.exit("No such database: {}".format(args.db))
    try:
        conn = open_read_only(args.db)
        rows = fetch(conn, args.since, args.route, args.outcome, args.problems, args.limit)
    except sqlite3.OperationalError as exc:
        sys.exit("Could not read the planner log from {}: {} (it is created the first time the app runs with the planner on)".format(args.db, exc))
    cases = [to_case(row) for row in rows]
    sys.stdout.write(as_python(cases) if args.format == "py" else as_json(cases))
    sys.stderr.write("{} case(s) exported. They contain patient names: keep them local.\n".format(len(cases)))


if __name__ == "__main__":
    main()
