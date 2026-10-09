"""Model-written reads: the planner step of the "Model does all read operations" mode (clinic/architecture.py).

Reached only from clinic/nlu/dialogue.py `_new_run`, and only when the setting says "model_reads"; with Classic or
New (model first) this module is never imported. Everything else in that mode is exactly model-first; what is new is
ONE more planner tool, `sql_read`, and the loop around it:

  * the model writes a read-only SELECT (one statement per call) over the curated `v_*` views (clinic/read_schema.py
    describes them in the prompt) and says what it is for: purpose="answer" (the final one, shown to the person) or purpose="lookup" ("run
    this and give me the rows, I will continue": at most MAX_SQL_STEPS queries in one command);
  * code lints it, lets the SQLite engine authorize every table, column and function, runs it on a read-only
    connection with a time and size limit (clinic/sql_read.py) and, for a lookup, hands a COMPACT copy of the rows
    back (phone numbers cut to their last four digits, ids hidden, MAX_FEEDBACK_ROWS rows) for the next step;
  * a query that is refused or fails is returned to the model ONCE with the plain reason; a second failure ends the
    attempt: the question is saved for a person (clinic/unanswered.py) exactly as an unlistable `query` is today;
  * the final rows are turned into the answer by CODE from their shape (no second model call): one value, label and
    number pairs, or a table. The model's only words in the answer are its caption, which carries no figures. The SQL
    is never shown to the user.

The model never emits an id and never writes: the tool has no write path (the connection is read-only and the
authorizer refuses everything but reading the views), and approval stays a human pressing Approve on the screen.
`query` stays for free slots and "next available" (clinic/next_available.py); if the model calls it for anything else,
the old read path runs exactly as before.
"""

import copy
import json
import logging
import re
import time
from collections import namedtuple
from datetime import timedelta

from clinic import pipeline, planner_log, query_tool, read_schema, sql_read, state_card
from clinic.nlu import dialogue, planner, tools
from clinic.pipeline import ReadResult

_logger = logging.getLogger(__name__)

SQL_READ = "sql_read"
MAX_SQL_STEPS = 3                   # queries in one command (lookups plus the final answer)
MAX_MODEL_CALLS = 6                 # the three queries and one repair each, at most
TOTAL_BUDGET_S = 8.0                # the whole command's model-and-query time
MIN_CALL_BUDGET_S = 1.0             # no new model call is started with less than this left
MAX_FEEDBACK_ROWS = 30
MAX_FEEDBACK_CHARS = 3000
MAX_CAPTION = 80
MAX_PAIR_ROWS = 12                  # "Branch A 3, Branch B 1, ..." up to this many; more is a table

_QUERY_NOTE = ("READ-ONLY lookup of FREE SLOTS only: entity=availability (free slots on a day; next=true: the next free "
               "one, with doctor, date, limit). For every other question about the records call sql_read, not this tool.")

SQL_READ_SCHEMA = {"type": "function", "function": {
    "name": SQL_READ,
    "description": "READ-ONLY: answer a question about the clinic's records with a SELECT (or WITH ... SELECT) over the "
                   "v_ views described in the instructions, one statement per call. Never changes anything. "
                   "purpose=lookup runs it and shows YOU the rows so you can query again; purpose=answer is the final "
                   "query, shown to the person.",
    "parameters": {"type": "object", "properties": {
        "sql": {"type": "string", "description": "A SELECT (or WITH ... SELECT) over the v_ views, one statement. No comments, no ids."},
        "purpose": {"type": "string", "enum": ["answer", "lookup"],
                    "description": "answer = the final query; lookup = run it and show me the rows first."},
        "caption": {"type": "string", "description": "A few plain words as a title, NO digits (at most 80 characters)."},
        "show_total": {"type": "boolean", "description": "true only to add up the LAST column of the answer."},
    }, "required": ["sql", "purpose"]}}}

_PARAMS = ("sql", "purpose", "caption", "show_total")


def schemas():
    """The planner's tools in model-reads mode: the usual 19 and 5 conversation tools (the `query` description now says
    it is for free slots only) plus `sql_read`. A copy: the registry in clinic/nlu/tools.py is never changed."""
    items = copy.deepcopy(tools.schemas(dialogue=True))
    for item in items:
        if item["function"]["name"] == "query":
            item["function"]["description"] = _QUERY_NOTE
    items.append(copy.deepcopy(SQL_READ_SCHEMA))
    return items


# -- validating the call ------------------------------------------------------------------------------------

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DAY_MONTH = re.compile(r"(?i)\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b(?:\s+\d{4})?")
_SQLISH = re.compile(r"[;()*=<>]|\bselect\b|\bv_[a-z]|\bwhere\b|\bjoin\b", re.IGNORECASE)


_DIGIT_RUN = re.compile(r"\d+")


def _command_digits(command):
    """The digit runs of the spoken command, also with spaces or dashes inside a number taken out ('98765 43210')."""
    text = str(command or "")
    compact = re.sub(r"(?<=\d)[ \-](?=\d)", "", text)
    return set(_DIGIT_RUN.findall(text)) | set(_DIGIT_RUN.findall(compact))


def _caption(value, command=None):
    """The caption, or tools.ToolError. A caption never carries a figure the model made up: digits are refused unless
    they are in one of the allowed date forms or the person said them in the command itself (a phone number, '6 PM',
    '10 days', a year)."""
    text = tools._text(value, MAX_CAPTION)
    stripped = _DAY_MONTH.sub("", _ISO_DATE.sub("", text))
    spoken = _command_digits(command)
    offending = []
    for run in _DIGIT_RUN.findall(stripped):
        if run not in spoken and run not in offending:
            offending.append(run)
    if offending:
        raise tools.ToolError("bad_value", "sql_read.caption: remove the digits {} from the caption (write words, or leave "
                              "the figure out)".format(", ".join("'{}'".format(run) for run in offending)), key="caption")
    if _SQLISH.search(text):
        raise tools.ToolError("bad_value", "sql_read.caption: a caption is a short plain title, not SQL", key="caption")
    return text


def validate_args(args, command=None):
    """The clean arguments of one `sql_read` call {sql, purpose, caption?, show_total?}, or tools.ToolError. Unknown
    arguments reject the call (there is no id or other argument). `command` is the spoken text: digits that are in it
    may appear in the caption."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            raise tools.ToolError("bad_value", "arguments are not JSON")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise tools.ToolError("bad_value", "arguments must be an object")
    clean = {}
    for key, value in args.items():
        if key not in _PARAMS:
            if value not in (None, "", []):
                raise tools.ToolError("unknown_arg", "sql_read has no argument {!r}".format(key), key=key)
            continue
        if value is None or value == "":
            continue
        if key == "sql":
            if not isinstance(value, str):
                raise tools.ToolError("bad_value", "sql_read.sql: expected text", key=key)
            clean[key] = value
        elif key == "purpose":
            word = str(value).strip().lower()
            if word not in ("answer", "lookup"):
                raise tools.ToolError("bad_value", "sql_read.purpose: use answer or lookup", key=key)
            clean[key] = word
        elif key == "caption":
            clean[key] = _caption(value, command)
        elif key == "show_total":
            if isinstance(value, bool):
                clean[key] = value
            elif isinstance(value, str) and value.strip().lower() in ("true", "yes", "false", "no"):
                clean[key] = value.strip().lower() in ("true", "yes")
            else:
                raise tools.ToolError("bad_value", "sql_read.show_total: expected true or false", key=key)
    if "sql" not in clean:
        raise tools.ToolError("missing_required", "sql_read needs sql")
    clean.setdefault("purpose", "answer")
    return clean


# -- the prompt -----------------------------------------------------------------------------------------------

def week_lines(today):
    monday = today - timedelta(days=today.weekday())
    this = (monday, monday + timedelta(days=6))
    last = (monday - timedelta(days=7), monday - timedelta(days=1))
    return "This week {} to {}; last week {} to {}.\n".format(this[0].isoformat(), this[1].isoformat(),
                                                             last[0].isoformat(), last[1].isoformat())


def example_queries(today):
    """[(question, caption, sql)]: two worked examples with invented content (no real patient). The dates come from
    `today`, never typed, so they cannot go stale."""
    monday = today - timedelta(days=today.weekday())
    first, last = monday.isoformat(), (monday + timedelta(days=6)).isoformat()
    return [
        ("where does Kavita Joshi have her appointment and who is on duty there that day?", "Appointment and doctor on duty",
         "SELECT a.appt_date, a.start_time, a.branch_name, r.doctor_name AS on_duty FROM v_appointments a JOIN "
         "v_roster_days r ON r.roster_date = a.appt_date AND r.branch_code = a.branch_code WHERE "
         "name_match(a.patient_name, 'Kavita Joshi') = 1 AND a.status IN ('booked', 'confirmed') "
         "ORDER BY a.appt_date, a.start_time"),
        ("how many appointments did each doctor have this week?", "Appointments per doctor",
         "SELECT d.name AS doctor, COUNT(a.id) AS appointments FROM v_doctors d LEFT JOIN v_appointments a ON "
         "a.doctor_name = d.name AND a.status IN ('booked', 'confirmed') AND a.appt_date BETWEEN '{}' AND '{}' "
         "GROUP BY d.name ORDER BY d.name".format(first, last)),
    ]


def example_lines(today):
    return "".join("Example {}, \"{}\" (caption '{}'):\n{}\n".format(n, question, caption, sql)
                   for n, (question, caption, sql) in enumerate(example_queries(today), 1))


def prompt_extra(conn, today):
    """What the system prompt gains in model-reads mode: the reading rules, the week ranges, two worked examples and
    the views with the meaning of every column (read from the database's own view columns)."""
    return ("\nREADING MODE\n" + read_schema.DOMAIN_RULES + week_lines(today) + example_lines(today)
            + "The v_ views (the only data you may read):\n" + read_schema.schema_text(conn))


# -- running one query and feeding it back -----------------------------------------------------------------------

Outcome = namedtuple("Outcome", ["args", "result", "feedback"])
_HIDDEN_COLUMNS = ("id",)
_PHONE_COLUMN = re.compile(r"(?i)phone|mobile|contact")


def _feedback_cell(column, value):
    if value is None:
        return "-"
    if _PHONE_COLUMN.search(column):
        return state_card.tail4(value) or "-"
    if isinstance(value, str):
        return state_card.mask_phones(value).replace("\n", " ")
    return str(value)


def compact_result(result):
    """The rows as a small text table for the model's next step: no id column, phone numbers cut to their last four
    digits, at most MAX_FEEDBACK_ROWS rows and MAX_FEEDBACK_CHARS characters. Only what the views hold."""
    keep = [i for i, name in enumerate(result.columns) if name.lower() not in _HIDDEN_COLUMNS]
    names = [result.columns[i] for i in keep]
    if not result.rows:
        return "0 rows. Columns: {}.".format(", ".join(names))
    shown = result.rows[:MAX_FEEDBACK_ROWS]
    total = result.total if result.truncated and result.total else len(result.rows)
    lines = [" | ".join(names)]
    for row in shown:
        lines.append(" | ".join(_feedback_cell(result.columns[i], row[i]) for i in keep))
    text = "{} row(s):\n{}".format(total, "\n".join(lines))
    if len(shown) < total:
        text += "\n(showing the first {})".format(len(shown))
    if len(text) > MAX_FEEDBACK_CHARS:
        text = text[:MAX_FEEDBACK_CHARS - 1].rstrip() + "\u2026"
    return text


# -- composing the answer from the rows' shape (no model call) ----------------------------------------------------

Composed = namedtuple("Composed", ["sentence", "data", "scope_caption", "shape"])

_WORDS = {
    "en": {"none": "Nothing found for that.", "results": "{n} result(s).", "total": "Total {x}.",
           "first": "Showing the first {n} of {m}", "first_only": "Showing the first {n}", "default": "Result"},
    "hi": {"none": "Is ke liye kuch nahi mila.", "results": "{n} nateeje.", "total": "Kul {x}.",
           "first": "Pehle {n} dikha raha hoon, kul {m}", "first_only": "Pehle {n} dikha raha hoon", "default": "Nateeja"},
}
_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})")


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _plain_number(value):
    if isinstance(value, float):
        value = round(value, 2)
        return str(int(value)) if value == int(value) else "{:.2f}".format(value).rstrip("0").rstrip(".")
    return str(value)


def _is_money(column):
    return str(column).lower().endswith("_rupees") or "rupee" in str(column).lower()


def format_value(column, value):
    """One value as it reads in a sentence: dates like "Fri 9 Oct", times as stored, money "Rs 300", else as is."""
    if value is None:
        return "-"
    if _is_number(value):
        return query_tool.rupees(value) if _is_money(column) else _plain_number(value)
    text = str(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return pipeline._short_day(pipeline.date.fromisoformat(text))
        except ValueError:
            return text
    found = _TIMESTAMP.match(text)
    if found and len(text) <= 19:
        try:
            return "{} {}".format(pipeline._short_day(pipeline.date.fromisoformat(found.group(1))), found.group(2))
        except ValueError:
            return text
    return text


def _headings(columns):
    out, seen = [], set()
    for column in columns:
        heading = read_schema.heading(column)
        while heading.lower() in seen:
            heading += " "
        seen.add(heading.lower())
        out.append(heading)
    return out


def compose(result, caption, show_total, language, default_caption=None):
    """Composed(sentence, data, scope_caption, shape) for the final rows. `shape` is "none", "scalar", "pairs" or
    "table". `data` (a table's rows as dicts with readable headings, the `id` column left out) is None unless the shape
    is a table. English wording unless `language` is "hi-IN" (Hinglish, like clinic/nlu/answer.py)."""
    words = _WORDS["hi" if language == "hi-IN" else "en"]
    title = caption or default_caption or words["default"]
    keep = [i for i, name in enumerate(result.columns) if name.lower() not in _HIDDEN_COLUMNS] or list(range(len(result.columns)))
    names = [result.columns[i] for i in keep]
    rows = [[row[i] for i in keep] for row in result.rows]
    shown_note = None
    if result.truncated:
        shown_note = (words["first"].format(n=len(rows), m=result.total) if result.total
                      else words["first_only"].format(n=len(rows)))

    if not rows:
        return Composed("{}: {}".format(title, words["none"]), None, None, "none")

    total_text = ""
    if show_total and not result.truncated and names and all(_is_number(r[-1]) or r[-1] is None for r in rows) \
            and any(_is_number(r[-1]) for r in rows):
        total_text = " " + words["total"].format(x=format_value(names[-1], sum(r[-1] for r in rows if _is_number(r[-1]))))

    if len(names) == 1 and len(rows) == 1 and not result.truncated:
        return Composed("{}: {}.".format(title, format_value(names[0], rows[0][0])), None, None, "scalar")

    if (len(names) == 2 and len(rows) <= MAX_PAIR_ROWS and not result.truncated
            and all(isinstance(r[0], str) for r in rows) and all(_is_number(r[1]) or r[1] is None for r in rows)
            and any(_is_number(r[1]) for r in rows)):
        pairs = ", ".join("{} {}".format(format_value(names[0], r[0]), format_value(names[1], r[1] if r[1] is not None else 0))
                          for r in rows)
        return Composed("{}: {}.{}".format(title, pairs, total_text), None, None, "pairs")

    count = result.total if result.truncated and result.total else len(rows)
    heads = _headings(names)
    data = [dict(zip(heads, row)) for row in rows]
    return Composed("{}: {}{}".format(title, words["results"].format(n=count), total_text), data, shown_note, "table")


# -- the run -----------------------------------------------------------------------------------------------------

class ModelReadsRun(dialogue.ModelFirstRun):
    """One model-reads command. The same machinery as the model-first run (prompt, state card, validation of every
    other tool, the log row), with `sql_read` added and the multi-step loop around it. Holds no state beyond that one
    command."""

    def __init__(self, conn, text, context, source="voice", backend=None, today=None, drop=None, clock=time.monotonic):
        super().__init__(conn, text, context, source, backend, today, drop)
        self.clock = clock
        self.outcome = None            # the last successful query (the one answered with)
        self.sql_queries = 0           # queries that ran and returned rows
        self.sql_attempts = 0          # queries sent to the engine, including refused ones
        self.sql_ms = 0
        self.repaired = False
        self.encoding = None
        self.cost_total = 0
        self.tokens_in_total = None
        self.tokens_out_total = None

    # -- accounting -----------------------------------------------------------------------------------------

    def _count_usage(self, backend):
        usage = getattr(backend, "last_usage", None)
        self.usage = usage
        if usage is None:
            return
        from clinic.nlu import sarvam           # not imported at the top: it imports the planner
        self.cost_total += sarvam.cost_paise(usage)
        self.tokens_in_total = (self.tokens_in_total or 0) + usage.prompt_tokens
        self.tokens_out_total = (self.tokens_out_total or 0) + usage.completion_tokens

    # -- the loop -------------------------------------------------------------------------------------------

    def _plan(self):
        backend = self.backend or planner.get_backend()
        self.state_card = state_card.build(self.context, self.conn, self.today, self.drop)
        system = (planner.build_system_prompt(self.conn, self.today) + dialogue.MODEL_FIRST_RULES
                  + prompt_extra(self.conn, self.today))
        user = "{}\n\nNew command: {}".format(self.state_card, self.text)
        self.backend_name = getattr(backend, "name", "local")
        tool_schemas = schemas()
        started = self.clock()
        deadline = started + TOTAL_BUDGET_S
        history = []
        repair_left = True
        calls = 0
        while True:
            if calls and (calls >= MAX_MODEL_CALLS or deadline - self.clock() < MIN_CALL_BUDGET_S):
                return self._out_of_budget("time" if calls < MAX_MODEL_CALLS else "calls")
            try:
                if history:
                    call = backend.plan_with_history(system, user, tool_schemas, history)
                else:
                    call = backend.plan(system, user, tool_schemas)
            finally:
                self._count_usage(backend)
                calls += 1
                self.encoding = getattr(backend, "last_encoding", None) if history else self.encoding
            if call is None:
                return self.no_tool_call(backend)
            if call.name != SQL_READ:
                return self._accept(call)
            self.tool, self.args = call.name, call.args
            outcome, problem = self._run_sql(call, deadline)
            if problem is not None:
                self.notes.append("sql rejected: {}".format(problem))
                if repair_left and calls < MAX_MODEL_CALLS:
                    repair_left, self.repaired = False, True
                    history.append((call, "Error: {}. Fix only what the error says, keep every part of the question, and call sql_read "
                                  "again.".format(problem.rstrip("."))))
                    continue
                return self._give_up(call, problem)
            self.outcome = outcome
            self.args = outcome.args
            self.sql_queries += 1
            repair_left = True
            if outcome.args["purpose"] == "answer":
                return self._answered()
            if self.sql_queries >= MAX_SQL_STEPS:
                self.notes.append("sql: the step limit was reached; answered with the last result")
                return self._answered()
            history.append((call, outcome.feedback))

    def _run_sql(self, call, deadline):
        """(Outcome, None) when the query ran, else (None, the plain reason to give the model)."""
        try:
            args = validate_args(call.args, self.text)
        except tools.ToolError as exc:
            return None, str(exc)
        self.sql_attempts += 1
        now = pipeline._local_now()
        try:
            result = sql_read.run_query(self.conn, args["sql"], self.today, now.strftime("%H:%M"), deadline=deadline,
                                        clock=self.clock)
        except sql_read.ReadError as exc:
            return None, str(exc)
        self.sql_ms += result.ms
        return Outcome(args, result, compact_result(result)), None

    def _answered(self):
        outcome = self.outcome
        _logger.info("Model-reads chose sql_read (%d step(s)) for transcript=%r", self.sql_queries, self.text)
        return planner.Planned("sql_read", {"sql": outcome.args["sql"]}, SQL_READ, outcome.args, [])

    def _out_of_budget(self, why):
        self.notes.append("sql: out of {} after {} step(s)".format(why, self.sql_queries))
        if self.outcome is not None:
            return self._answered()
        self.error = "sql: out of {}".format(why)
        return None

    def _give_up(self, call, problem):
        """The second failure of a query: nothing is shown but the fixed "I can't answer that yet" reply, and the
        question is saved for a person (clinic/unanswered.py) with the SQL as a hint for the developer."""
        sql = call.args.get("sql") if isinstance(call.args, dict) else None
        self.error = "sql: {}".format(problem)[:200]
        self.rejected_query = {"spec": {"sql": str(sql or "")[:300]}, "reason": problem[:200]}
        return None

    # -- the answer -----------------------------------------------------------------------------------------

    def answer(self, ctx, conn, language, clinical_adapter):
        """The ReadResult for the final rows, composed by code. Appointment-shaped rows are remembered as the list on
        screen ("cancel the second one"), and an open question is asked again after the answer, as for every read."""
        outcome = self.outcome
        args = outcome.args
        composed = compose(outcome.result, args.get("caption"), args.get("show_total", False), language)
        paused = ctx.pending
        if paused is not None:
            ctx.resume_pending = paused
        self._remember_rows(ctx, outcome.result, args.get("caption"))
        citation = clinical_adapter.citation()
        sentence = "{} Source: {}, {}.".format(composed.sentence, citation.source, citation.as_of)
        result = ReadResult("sql_read", composed.data, citation, sentence, composed.scope_caption)
        if paused is not None:
            result = dialogue._with_open_question(result, paused, language)
        return result

    @staticmethod
    def _remember_rows(ctx, result, caption):
        if {"id", "appt_date", "start_time"} <= set(result.columns):
            rows = [dict(zip(result.columns, row)) for row in result.rows]
            ctx.remember_list(rows, caption or "appointments", None)

    # -- the log row ------------------------------------------------------------------------------------------

    def call_summary(self):
        if self.route == "planner" and self.tool == SQL_READ:
            args = self.args or {}
            return tools.describe_call(SQL_READ, {"purpose": args.get("purpose"), "caption": args.get("caption")})
        return super().call_summary()

    def _write_log(self, final_intent, slots):
        if self.fallback:
            route, detail = "rules", "mf_fallback_classic"
        elif self.sql_attempts:
            route, detail = "planner", "sql:{}".format(self.sql_queries)
        else:
            route, detail = "planner", "mf:{}".format(self.tool)
        notes = list(self.notes)
        if self.sql_attempts:
            rows = len(self.outcome.result.rows) if self.outcome is not None else 0
            summary = "sql: {} step(s), {} rows, {} ms".format(self.sql_queries, rows, self.sql_ms)
            if self.repaired:
                summary += ", repaired"
            if self.encoding:
                summary += ", {}".format(self.encoding)
            notes.append(summary)
        self.log_id = planner_log.record(
            self.conn, self.source, self.text, self.previous, self.tool, self.args, route, final_intent,
            self.latency_ms, notes, backend=self.backend_name, tokens_in=self.tokens_in_total,
            tokens_out=self.tokens_out_total, cost_paise=self.cost_total, route_detail=detail, state_card=self.state_card)
