"""The local planner command log (the additive `planner_log` table).

One row per staff command (planner-asked or decided by a precise rule: route_detail says which). It is there so a
person can later turn real commands into labelled test cases
(scripts/export_planner_log.py); it never leaves the clinic's own database.
Transcripts contain patient names, so the table is as private as `patients`.

Logging must never get in the way of a command: every function here swallows
database errors (a missing table on an old connection, a locked database) and
says so only in the application log. It is on by default and switched off with
the `planner_log_enabled` app setting (clinic/settings.py).
"""

import json
import logging
import sqlite3
from datetime import timedelta

from clinic import settings

_logger = logging.getLogger(__name__)

ROUTES = ("rules", "planner", "label_fallback", "rephrase")
SOURCES = ("voice", "wa_staff")
OUTCOMES = ("approved", "rejected", "edited")


def record(conn, source, transcript, previous_turn, tool, args, route, final_intent, latency_ms, notes,
           backend=None, tokens_in=None, tokens_out=None, cost_paise=0, route_detail=None, state_card=None):
    """Write one row and return its id, or None when logging is off or fails. `backend` is 'local' or
    'sarvam'; tokens and cost (whole paise, from Sarvam's published prices) are set only for a hosted
    call that answered -- a failed or rate-limited call is not billed and has no tokens.
    `route_detail` (free text, NULL for a planner-routed command) says which precise rule decided a
    'rules' row: "rule:move", "rule:context", "rule:count", "rule:branch", "rule:closure", "rule:keywords",
    "rule:other"; "+name_fill" is appended when the small hosted name read ran (the route_taken CHECK
    constraint of existing databases cannot take new values, so the detail lives in its own column).
    `state_card` is written only in model-first mode (clinic/architecture.py): the card sent with the command.
    Without one the INSERT is exactly the one it always was."""
    if conn is None or route not in ROUTES or source not in SOURCES:
        return None
    try:
        if not settings.planner_log_enabled(conn):
            return None
        columns = ("source, transcript, previous_turn, planner_tool, planner_args_json, route_taken, final_intent, "
                   "latency_ms, override_notes, backend, tokens_in, tokens_out, cost_paise, route_detail")
        values = [source, transcript, previous_turn, tool, json.dumps(args, ensure_ascii=False) if args is not None else None,
                  route, final_intent, None if latency_ms is None else int(latency_ms),
                  "; ".join(notes) if notes else None, backend, tokens_in, tokens_out, int(cost_paise or 0), route_detail]
        if state_card is not None:
            columns += ", state_card"
            values.append(state_card)
        cur = conn.execute("INSERT INTO planner_log ({}) VALUES ({})".format(columns, ", ".join("?" * len(values))), values)
        conn.commit()
        return cur.lastrowid
    except sqlite3.Error:
        _logger.warning("Could not write the planner log row", exc_info=True)
        return None


def set_outcome(conn, log_id, outcome):
    """What the person did with the card this command produced. Ignored for an
    unknown id or outcome."""
    if not log_id or outcome not in OUTCOMES:
        return False
    try:
        cur = conn.execute("UPDATE planner_log SET outcome = ? WHERE id = ?", (outcome, int(log_id)))
        conn.commit()
        return cur.rowcount > 0
    except (sqlite3.Error, TypeError, ValueError):
        _logger.warning("Could not set the planner log outcome", exc_info=True)
        return False


def rows(conn, since=None, outcome=None, limit=None):
    """Log rows, oldest first. `since` is an ISO date or datetime prefix."""
    sql, params = "SELECT * FROM planner_log WHERE 1 = 1", []
    if since:
        sql += " AND ts >= ?"
        params.append(since)
    if outcome:
        sql += " AND outcome = ?"
        params.append(outcome)
    sql += " ORDER BY id"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


IST_OFFSET = timedelta(hours=5, minutes=30)


def sarvam_usage(conn, now):
    """What the Sarvam planner has cost in the calendar month of `now` (the clinic's local wall clock,
    IST): {month, commands, tokens_in, tokens_out, spend_paise, spend_rupees}. Only commands Sarvam
    answered count (a failed or rate-limited call has no tokens and was not billed). `ts` is stored in
    UTC, so the month's edges are converted to UTC rather than the rows to local time. Never raises."""
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1))
    usage = {"month": start.strftime("%Y-%m"), "commands": 0, "tokens_in": 0, "tokens_out": 0, "spend_paise": 0,
             "spend_rupees": 0.0}
    try:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(tokens_in), 0), COALESCE(SUM(tokens_out), 0), COALESCE(SUM(cost_paise), 0) "
            "FROM planner_log WHERE backend = 'sarvam' AND tokens_in IS NOT NULL AND ts >= ? AND ts < ?",
            ((start - IST_OFFSET).strftime("%Y-%m-%d %H:%M:%S"), (end - IST_OFFSET).strftime("%Y-%m-%d %H:%M:%S"))).fetchone()
    except sqlite3.Error:
        _logger.warning("Could not read the Sarvam usage", exc_info=True)
        return usage
    usage.update(commands=row[0], tokens_in=row[1], tokens_out=row[2], spend_paise=row[3], spend_rupees=round(row[3] / 100, 2))
    return usage
