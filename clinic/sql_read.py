"""Run ONE model-written SELECT over the read-only views, behind layered guardrails.

Used only by the "Model does all read operations" mode (clinic/architecture.py, clinic/nlu/sql_reads.py). The
model never gets a connection and never sees an error that names a file; this module takes its SQL text, checks it,
runs it on a separate read-only connection and hands back rows. No single layer has to be perfect:

  (a) LINT      one statement that starts with SELECT or WITH; no comments, no `;` but a trailing one, no NUL, at most
                MAX_SQL_CHARS characters; only the named parameters :today and :now; no WITH RECURSIVE (only the views
                recurse); no write, DDL, PRAGMA, ATTACH or transaction word; a CTE may not be named like a view or a
                table; a cap on joined sources and sub-selects; the person tests name_match / phone10 are used as
                tests (`= 1`), never compared to a column, never given a bind parameter or the wrong number of
                arguments, and patient_name / patient_phone / staff_name are never compared to a text.
  (b) AUTHORIZER the SQLite engine itself asks, for every table, column and function the statement touches, and
                refuses all but: reading the described columns of the `v_*` views, the short list of functions in
                clinic/read_schema.py and the three registered below. When a view is expanded SQLite also asks about the
                BASE tables it reads, with the view's name as the innermost context: those reads are allowed only in that
                context AND only for the columns that view really reads (its "footprint", traced once from the
                database's own view definitions), so a query that names a base table directly, or a CTE named like a
                view to borrow its context, is refused.
  (c) READ-ONLY CONNECTION a separate connection to the same database file (`file:...?mode=ro`) with `PRAGMA
                query_only = ON`. An in-memory database (the tests) is copied once with `Connection.backup` into a second
                in-memory connection that is then read-only the same way; the original is never touched.
  (d) LIMITS    the SQL is wrapped as `SELECT * FROM (<sql>) LIMIT 200`; a progress handler aborts it after
                TIME_LIMIT_S seconds (or the caller's own deadline); at most MAX_COLUMNS columns and MAX_CELLS cells;
                strings are cut at MAX_CELL_CHARS characters.
  (e) FUNCTIONS three deterministic ones are registered on that connection and nothing else: name_match(column, 'spoken')
                (clinic/entity_resolution.name_match, exactly), phone10(column, 'text') (the last 10 digits, exactly)
                and today_ist() (the injected clock's date).

Every refusal is a ReadError with a short plain message the model can use to repair its query; none names a path or
any internal detail.
"""

import re
import sqlite3
import time
import urllib.parse
from collections import namedtuple
from datetime import date

from clinic import entity_resolution, read_schema

MAX_ROWS = 200
MAX_SQL_CHARS = 1500
MAX_CELL_CHARS = 200
MAX_COLUMNS = 16
MAX_CELLS = 2400
MAX_SOURCES = 8
MAX_SELECTS = 10
MAX_REPLACES = 3                            # replace() can multiply a string; a few are plenty for tidying text
MAX_TOKENS = 700
TIME_LIMIT_S = 2.0
PROGRESS_STEPS = 1000                       # VM instructions between two looks at the clock

NAMED_PARAMETERS = (":today", ":now")

# sqlite3 authorizer action codes (the constants exist in Python's sqlite3 module on 3.7+, spelled out for clarity)
_OK, _DENY = sqlite3.SQLITE_OK, sqlite3.SQLITE_DENY
_SELECT, _READ, _FUNCTION, _RECURSIVE = 21, 20, 31, 33


class ReadError(Exception):
    """A query that cannot be run. `kind`: lint, denied, sql, limit or unavailable. The message is short, plain
    and safe to show to the model."""

    def __init__(self, message, kind="sql"):
        super().__init__(message)
        self.kind = kind


SqlResult = namedtuple("SqlResult", ["columns", "rows", "truncated", "total", "ms"])


# -- (a) lint --------------------------------------------------------------------------------------------------

_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<comment>--[^\n]*|/\*.*?\*/)
  | (?P<str>'(?:[^']|'')*')
  | (?P<qid>"(?:[^"]|"")*"|`[^`]*`|\[[^\]]*\])
  | (?P<param>[:@$][A-Za-z_]\w*|\?\d*)
  | (?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+)
  | (?P<word>[A-Za-z_][A-Za-z_0-9]*)
  | (?P<op>\|\||<=|>=|<>|!=|==|<<|>>|[-+*/%<>=(),.&|~;])
""", re.VERBOSE | re.DOTALL)

Token = namedtuple("Token", ["kind", "text"])

_FORBIDDEN_WORDS = frozenset((
    "recursive", "insert", "update", "delete", "drop", "create", "alter", "attach", "detach", "pragma", "vacuum",
    "reindex", "analyze", "begin", "commit", "rollback", "savepoint", "release", "explain", "load_extension",
    "truncate", "upsert", "returning",
))
_CLAUSE_ENDS = frozenset(("where", "group", "having", "order", "limit", "union", "except", "intersect", "window", "select"))


def _tokenize(text):
    tokens, pos = [], 0
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if match is None:
            if text[pos] in "'\"`[":
                raise ReadError("an unterminated quote in the SQL", "lint")
            raise ReadError("unreadable SQL near position {}".format(pos), "lint")
        kind = match.lastgroup
        if kind == "comment":
            raise ReadError("comments are not allowed in the SQL", "lint")
        if kind != "ws":
            tokens.append(Token(kind, match.group()))
        pos = match.end()
    for before, after in zip(tokens, tokens[1:]):
        if before.kind == "op" and before.text == "/" and after.kind == "op" and after.text == "*":
            if text.find("/*") != -1:
                raise ReadError("comments are not allowed in the SQL", "lint")
    return tokens


def _identifier(token):
    text = token.text
    if token.kind == "qid":
        text = text[1:-1]
    return text.lower()


def _cte_names(tokens):
    """The names of the CTEs a statement that starts with WITH defines, in order. ReadError if it is malformed."""
    names, i = [], 1
    while True:
        if i >= len(tokens) or tokens[i].kind not in ("word", "qid"):
            raise ReadError("a WITH clause needs a name after WITH", "lint")
        names.append(_identifier(tokens[i]))
        i += 1
        if i < len(tokens) and tokens[i].text == "(":          # (column, names)
            depth = 0
            while i < len(tokens):
                depth += (tokens[i].text == "(") - (tokens[i].text == ")")
                i += 1
                if depth == 0:
                    break
        if i >= len(tokens) or tokens[i].text.lower() != "as":
            raise ReadError("a WITH clause is `name AS (SELECT ...)`", "lint")
        i += 1
        while i < len(tokens) and tokens[i].text.lower() in ("not", "materialized"):
            i += 1
        if i >= len(tokens) or tokens[i].text != "(":
            raise ReadError("a WITH clause is `name AS (SELECT ...)`", "lint")
        depth = 0
        while i < len(tokens):
            depth += (tokens[i].text == "(") - (tokens[i].text == ")")
            i += 1
            if depth == 0:
                break
        if i < len(tokens) and tokens[i].text == ",":
            i += 1
            continue
        return names


def _count_sources(tokens):
    """About how many tables / views / sub-selects a statement joins: every FROM, JOIN and the commas of a FROM list."""
    sources, depth, in_from = 0, 0, {}
    for token in tokens:
        word = token.text.lower() if token.kind == "word" else None
        if word == "from":
            sources += 1
            in_from[depth] = True
        elif word == "join":
            sources += 1
        elif word in _CLAUSE_ENDS:
            in_from[depth] = False
        elif token.kind == "op":
            if token.text == "(":
                depth += 1
                in_from[depth] = False
            elif token.text == ")":
                in_from.pop(depth, None)
                depth = max(0, depth - 1)
            elif token.text == "," and in_from.get(depth):
                sources += 1
    return sources


_HUGE_WIDTH = re.compile(r"%[-+ 0#]*(?:\*|\d{4,})|%[-+ 0#]*\d*\.(?:\*|\d{4,})")


def _check_string_builders(tokens):
    """printf / format take a plain string literal for their pattern (so a width cannot be computed into a gigabyte
    string), and replace() may appear only a few times (nesting it multiplies a string's length)."""
    replaces = 0
    for i, token in enumerate(tokens):
        word = token.text.lower() if token.kind == "word" else None
        followed_by_paren = i + 1 < len(tokens) and tokens[i + 1].text == "("
        if word in ("printf", "format") and followed_by_paren:
            pattern = tokens[i + 2] if i + 2 < len(tokens) else None
            after = tokens[i + 3] if i + 3 < len(tokens) else None
            if (pattern is None or pattern.kind != "str" or _HUGE_WIDTH.search(pattern.text)
                    or after is None or after.text not in (",", ")")):
                raise ReadError("{}() needs a plain text pattern with no large widths".format(word), "lint")
        elif word == "replace" and followed_by_paren:
            replaces += 1
            if replaces > MAX_REPLACES:
                raise ReadError("too many replace() calls (at most {})".format(MAX_REPLACES), "lint")


# -- the person tests: name_match / phone10 are tests that return 1 or 0, never values -------------------------------

_PERSON_TESTS = {"name_match": "name", "phone10": "phone"}
_PERSON_COLUMNS = {"patient_name": "name_match", "staff_name": "name_match", "patient_phone": "phone10"}
_COMPARE_OPS = frozenset(("=", "==", "!=", "<>", "<", ">", "<=", ">="))
_EQUALITY_OPS = frozenset(("=", "==", "!=", "<>"))
_LIKE_WORDS = frozenset(("like", "glob", "in"))


def _person_form(function):
    if function == "phone10":
        return "Use phone10(<column>, 'digits') = 1 to find a phone number; it returns 1 or 0, so never compare a column to it."
    return "Use name_match(<column>, 'spoken name') = 1 to find a person; it returns 1 or 0, so never compare a column to it."


def _column_form(column):
    function = _PERSON_COLUMNS[column]
    if function == "phone10":
        return ("Use phone10({0}, 'digits') = 1 to find a phone number; never compare {0} to a text with =, !=, IN or "
                "LIKE.".format(column))
    return ("Use name_match({0}, 'spoken name') = 1 to find a person; never compare {0} to a text with =, !=, IN or "
            "LIKE.".format(column))


def _is_01(token):
    return token is not None and token.kind == "num" and token.text in ("0", "1")


def _check_person_tests(tokens):
    """name_match(col, 'text') and phone10(col, 'digits') return 1 or 0: the legal uses are the call on its own or
    compared with a 0 or 1. A column or a text compared to the call, a bind parameter, the wrong number of arguments,
    and a person column (patient_name, staff_name, patient_phone) compared to a text, are refused with the right form."""
    count = len(tokens)

    def at(i):
        return tokens[i] if 0 <= i < count else None

    for i, token in enumerate(tokens):
        word = token.text.lower() if token.kind == "word" else None
        if word in _PERSON_TESTS and at(i + 1) is not None and tokens[i + 1].text == "(":
            depth, j, args, current = 0, i + 1, [], []
            while j < count:
                text = tokens[j].text
                if tokens[j].kind == "op" and text == "(":
                    depth += 1
                    if depth > 1:
                        current.append(tokens[j])
                elif tokens[j].kind == "op" and text == ")":
                    depth -= 1
                    if depth == 0:
                        break
                    current.append(tokens[j])
                elif tokens[j].kind == "op" and text == "," and depth == 1:
                    args.append(current)
                    current = []
                else:
                    current.append(tokens[j])
                j += 1
            args.append(current)
            if args == [[]]:
                args = []
            if len(args) != 2 or any(not part for part in args):
                raise ReadError("{0} takes exactly two arguments. {1}".format(word, _person_form(word)), "lint")
            if any(t.kind == "param" for part in args for t in part):
                raise ReadError("give {0} the column and the spoken text in quotes, never a bind parameter. {1}".format(
                    word, _person_form(word)), "lint")
            before, further = at(i - 1), at(i - 2)
            if before is not None:
                if before.kind == "op" and before.text in _COMPARE_OPS and not _is_01(further):
                    raise ReadError(_person_form(word), "lint")
                if before.kind == "word" and before.text.lower() in _LIKE_WORDS:
                    raise ReadError(_person_form(word), "lint")
                if before.kind == "op" and before.text == "(" and further is not None and further.text.lower() in _LIKE_WORDS:
                    raise ReadError(_person_form(word), "lint")
            after = at(j + 1)
            if after is not None:
                nxt = at(j + 2)
                if after.kind == "op" and after.text in _COMPARE_OPS and not _is_01(nxt):
                    raise ReadError(_person_form(word), "lint")
                if after.kind == "word" and (after.text.lower() in _LIKE_WORDS or after.text.lower() == "between"
                                             or (after.text.lower() == "not" and nxt is not None
                                                 and nxt.text.lower() in _LIKE_WORDS)):
                    raise ReadError(_person_form(word), "lint")
            continue
        if token.kind in ("word", "qid"):
            column = _identifier(token)
            if column in _PERSON_COLUMNS and not (at(i + 1) is not None and tokens[i + 1].text in ("(", ".")):
                _check_person_column(tokens, i, column)
        elif token.kind == "str" and token.text != "''":
            # 'text' = column (the other way round), the column possibly qualified: a.patient_name
            after, k = at(i + 1), i + 2
            if after is not None and after.kind == "op" and after.text in _EQUALITY_OPS:
                name = at(k)
                if name is not None and name.kind in ("word", "qid") and at(k + 1) is not None and tokens[k + 1].text == ".":
                    name = at(k + 2)
                if name is not None and name.kind in ("word", "qid") and _identifier(name) in _PERSON_COLUMNS \
                        and not (at(k + 1) is not None and tokens[k + 1].text == "("):
                    raise ReadError(_column_form(_identifier(name)), "lint")


def _check_person_column(tokens, i, column):
    """`patient_name = 'x'`, `... IN ('x')`, `... LIKE 'x%'` (also `NOT IN`, and `lower(patient_name) = 'x'`)."""
    count = len(tokens)
    k = i + 1
    while k < count and tokens[k].text == ")":
        k += 1
    if k < count and tokens[k].kind == "word" and tokens[k].text.lower() == "not":
        k += 1
    if k >= count:
        return
    op = tokens[k]

    def literal(index):
        return index < count and tokens[index].kind == "str" and tokens[index].text != "''"

    if op.kind == "op" and op.text in _EQUALITY_OPS and literal(k + 1):
        raise ReadError(_column_form(column), "lint")
    if op.kind == "word" and op.text.lower() in ("like", "glob") and literal(k + 1):
        raise ReadError(_column_form(column), "lint")
    if op.kind == "word" and op.text.lower() == "in" and k + 1 < count and tokens[k + 1].text == "(" and literal(k + 2):
        raise ReadError(_column_form(column), "lint")


def lint(sql, reserved=()):
    """The SQL text cleaned for running (a trailing `;` dropped), or ReadError. `reserved` are table and view names
    (lower case) a CTE may not take."""
    if not isinstance(sql, str):
        raise ReadError("the SQL must be text", "lint")
    if "\x00" in sql:
        raise ReadError("the SQL has a NUL character", "lint")
    text = sql.strip()
    if not text:
        raise ReadError("the SQL is empty", "lint")
    if len(text) > MAX_SQL_CHARS:
        raise ReadError("the SQL is too long (at most {} characters)".format(MAX_SQL_CHARS), "lint")
    tokens = _tokenize(text)
    if len(tokens) > MAX_TOKENS:
        raise ReadError("the SQL is too long", "lint")
    semicolons = [i for i, t in enumerate(tokens) if t.kind == "op" and t.text == ";"]
    if semicolons:
        if semicolons != [len(tokens) - 1]:
            raise ReadError("send exactly one statement", "lint")
        tokens = tokens[:-1]
        text = text[:text.rstrip().rfind(";")].rstrip()
    if not tokens or tokens[0].kind != "word" or tokens[0].text.lower() not in ("select", "with"):
        raise ReadError("the SQL must start with SELECT (or WITH)", "lint")
    _check_person_tests(tokens)
    for token in tokens:
        if token.kind == "word" and token.text.lower() in _FORBIDDEN_WORDS:
            raise ReadError("{} is not allowed: this is read only".format(token.text.upper()), "lint")
        if token.kind == "param" and token.text.lower() not in NAMED_PARAMETERS:
            if token.text.lstrip(":@$").lower() in _PERSON_TESTS:
                raise ReadError("{0} is a function, not a parameter. {1}".format(token.text.lstrip(":@$").lower(),
                                _person_form(token.text.lstrip(":@$").lower())), "lint")
            raise ReadError("the only parameters are :today and :now", "lint")
    if sum(1 for t in tokens if t.kind == "word" and t.text.lower() == "select") > MAX_SELECTS:
        raise ReadError("too many sub-selects (at most {})".format(MAX_SELECTS), "lint")
    if _count_sources(tokens) > MAX_SOURCES:
        raise ReadError("too many joined sources (at most {})".format(MAX_SOURCES), "lint")
    _check_string_builders(tokens)
    if tokens[0].text.lower() == "with":
        blocked = {n.lower() for n in reserved} | set(read_schema.VIEW_NAMES) | read_schema.INTERNAL_CTES
        for name in _cte_names(tokens):
            if name in blocked or name.startswith(("v_", "sqlite_", "pragma_", "_")):
                raise ReadError("a WITH name may not be {!r}: pick another name".format(name), "lint")
    return text


# -- (e) the registered functions -----------------------------------------------------------------------------

def _name_match(column, spoken):
    if column is None or spoken is None:
        return 0
    return 1 if entity_resolution.name_match(str(spoken), str(column)) == 1.0 else 0


def _phone10(column, text):
    if column is None or text is None:
        return 0
    number = entity_resolution.full_number(str(text))
    return 1 if number and entity_resolution.last10_digits(str(column)) == number else 0


# -- (b) the authorizer ---------------------------------------------------------------------------------------

_footprints = {}          # a hash of the views' SQL -> {view: frozenset((base table, column))}


def _allow_all(action, arg1, arg2, arg3, arg4):
    return _OK


def _database_file(conn):
    """The file the connection has open, or None for an in-memory / temporary database."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return row[2] or None
    return None


class ReadSession:
    """One read-only connection with the guardrails installed. Open it, `run()` queries, close it (a context
    manager). `source` is the application's connection (or a path); it is only ever read from, to find the file
    or to copy an in-memory database. `today` is the injected clinic date, `now_hhmm` the injected time ('HH:MM').
    `clock` is the monotonic clock (tests inject one)."""

    def __init__(self, source, today, now_hhmm="00:00", time_limit=TIME_LIMIT_S, clock=time.monotonic):
        self.today = today if isinstance(today, date) else date.fromisoformat(str(today))
        self.now_hhmm = now_hhmm
        self.time_limit = time_limit
        self.clock = clock
        self.conn = None
        self._deadline = None
        self._violation = None
        self._footprint = {}
        self._expanded = frozenset()
        self._reserved = set()
        self._open(source)

    # -- opening ---------------------------------------------------------------------------------------

    def _open(self, source):
        # A database file is opened read-only by URI; if that cannot be used (for example a WAL file in a folder the
        # process cannot write to) and the caller gave a live connection, the database is copied into memory instead.
        try:
            try:
                self._setup(self._connect(source, copy=False))
            except sqlite3.Error:
                if isinstance(source, str):
                    raise
                self._discard()
                self._setup(self._connect(source, copy=True))
        except ReadError:
            self.close()
            raise
        except sqlite3.Error:
            self.close()
            raise ReadError("reading is not available right now", "unavailable") from None

    def _discard(self):
        conn, self.conn = self.conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def _setup(self, conn):
        self.conn = conn
        conn.execute("PRAGMA query_only = ON")
        conn.create_function("name_match", 2, _name_match, deterministic=True)
        conn.create_function("phone10", 2, _phone10, deterministic=True)
        iso = self.today.isoformat()
        conn.create_function("today_ist", 0, lambda: iso, deterministic=True)
        self._reserved = {row[0].lower() for row in conn.execute("SELECT name FROM sqlite_master")}
        missing = [v for v in read_schema.VIEW_NAMES if v not in self._reserved]
        if missing:
            raise ReadError("reading is not available yet", "unavailable")
        self._footprint = self._trace_footprints()
        conn.set_authorizer(self._authorize)
        conn.set_progress_handler(self._progress, PROGRESS_STEPS)

    def _connect(self, source, copy):
        if isinstance(source, str):
            path = None if source in ("", ":memory:") else source
            source_conn = None
        else:
            source_conn = source
            path = _database_file(source)
        if path and not copy:
            uri = "file:{}?mode=ro".format(urllib.parse.quote(path))
            return sqlite3.connect(uri, uri=True, check_same_thread=False)
        if source_conn is None:
            raise ReadError("reading is not available right now", "unavailable")
        duplicate = sqlite3.connect(":memory:", check_same_thread=False)       # an in-memory database: copy it once
        source_conn.backup(duplicate)
        return duplicate

    def _trace_footprints(self):
        """{view: the (base table, column) pairs it reads}, traced by preparing `SELECT * FROM view LIMIT 0` under a
        recording authorizer. Refused when a view reads anything outside read_schema.ALLOWED_BASE."""
        rows = self.conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'view' AND name IN ({})".format(
            ", ".join("?" * len(read_schema.VIEW_NAMES))), read_schema.VIEW_NAMES).fetchall()
        key = hash(tuple(sorted((name, sql) for name, sql in rows)) + (tuple(sorted(read_schema.ALLOWED_BASE)),))
        if key in _footprints:
            return _footprints[key]
        found = {}
        current = {"view": None}
        views = set(read_schema.VIEW_NAMES)

        def record(action, a1, a2, a3, a4):
            if action == _READ and a1 not in views:
                found.setdefault(current["view"], set()).add((a1, a2))
            return _OK

        self.conn.set_authorizer(record)
        try:
            for view in read_schema.VIEW_NAMES:
                current["view"] = view
                found.setdefault(view, set())
                self.conn.execute("SELECT * FROM {} LIMIT 0".format(view)).fetchall()
        finally:
            self.conn.set_authorizer(_allow_all)            # (Python 3.9 cannot clear an authorizer with None)
        for view, pairs in found.items():
            for table, column in pairs:
                if column not in read_schema.ALLOWED_BASE.get(table, ()):
                    raise ReadError("reading is not available: a view reads an unlisted column", "unavailable")
        footprint = {view: frozenset(pairs) for view, pairs in found.items()}
        _footprints[key] = footprint
        return footprint

    # -- the engine's questions -----------------------------------------------------------------------------

    def _deny(self, message):
        if self._violation is None:
            self._violation = message
        return _DENY

    def _authorize(self, action, arg1, arg2, arg3, arg4):
        if action == _SELECT:
            return _OK
        if action == _READ:
            table, column, database, context = arg1, arg2, arg3, arg4
            if database not in (None, "main"):
                return self._deny("table not allowed: {}".format(table))
            if table in read_schema.COLUMNS:
                if column == "" or column in read_schema.COLUMNS[table]:
                    return _OK
                return self._deny("no such column: {}".format(column))
            if context in self._footprint and (table, column) in self._footprint[context]:
                return _OK           # a view reading its own base tables, and only what it reads
            if column == "" and context is None and table in self._expanded:
                return _OK           # SQLite touching a table a view in this statement reads (count(*)): no column
            return self._deny("table not allowed: {} (read only the v_ views)".format(table))
        if action == _FUNCTION:
            if str(arg2).lower() in read_schema.ALLOWED_FUNCTIONS:
                return _OK
            return self._deny("function not allowed: {}".format(arg2))
        if action == _RECURSIVE:
            if arg4 in read_schema.INTERNAL_CTES:
                return _OK
            return self._deny("recursive queries are not allowed")
        return self._deny("only SELECT is allowed")

    def _progress(self):
        return 1 if self._deadline is not None and self.clock() > self._deadline else 0

    # -- running ------------------------------------------------------------------------------------------

    def run(self, sql, limit=MAX_ROWS, deadline=None):
        """Lint and run one SELECT. `deadline` is an absolute time on this session's clock (the caller's overall
        budget); the query also stops after `time_limit` seconds. Returns SqlResult, or raises ReadError."""
        if self.conn is None:
            raise ReadError("reading is not available right now", "unavailable")
        clean = lint(sql, self._reserved)
        started = self.clock()
        self._deadline = started + self.time_limit
        if deadline is not None:
            self._deadline = min(self._deadline, deadline)
        self._violation = None
        params = {"today": self.today.isoformat(), "now": self.now_hhmm}
        wrapped = "SELECT * FROM ({}) LIMIT {}".format(clean, int(limit) + 1)
        try:
            self._expanded = self._prescan(wrapped, params)
            cursor = self.conn.execute(wrapped, params)
            names = _unique([d[0] for d in (cursor.description or ())])
            if len(names) > MAX_COLUMNS:
                raise ReadError("too many columns (at most {}): select only what is needed".format(MAX_COLUMNS), "limit")
            fetched = cursor.fetchall()
            truncated = len(fetched) > limit
            rows = [tuple(_cell(v) for v in row) for row in fetched[:limit]]
            if len(rows) * max(1, len(names)) > MAX_CELLS:
                rows = rows[:max(1, MAX_CELLS // max(1, len(names)))]
                truncated = True
            total = None
            if truncated:
                total = self._count(clean, params)
        except ReadError:
            raise
        except sqlite3.Error as exc:
            raise self._translate(exc, clean) from None
        finally:
            self._deadline = None
        return SqlResult(names, rows, truncated, total, int((self.clock() - started) * 1000))

    def _prescan(self, wrapped, params):
        """The base tables the views in this statement read, found by preparing it once under a recording
        authorizer (nothing runs). SQLite may flatten a view and then touch its base table with no column at all
        (count(*)); that one table-level touch is allowed only for such tables, so a base table named directly,
        however it is used, is still refused."""
        seen = []
        self.conn.set_authorizer(lambda *args: (seen.append(args), _OK)[1])
        try:
            self.conn.execute("EXPLAIN " + wrapped, params).fetchall()
        except sqlite3.Error:
            pass                       # the same error comes back, translated, when the statement really runs
        finally:
            self.conn.set_authorizer(self._authorize)
        return frozenset(args[1] for args in seen
                         if args[0] == _READ and args[4] in read_schema.COLUMNS and args[1] not in read_schema.COLUMNS)

    def _count(self, clean, params):
        try:
            return self.conn.execute("SELECT COUNT(*) FROM ({})".format(clean), params).fetchone()[0]
        except sqlite3.Error:
            return None

    @staticmethod
    def _with_columns(message, sql):
        """A "no such column" / "no such table" message plus the real columns of every v_ view the query uses (from
        read_schema, so the list is never out of step), or the view names when the query names none."""
        if not message.lower().startswith(("no such column", "no such table")) or not sql:
            return message
        used = []
        try:
            for token in _tokenize(sql):
                if token.kind in ("word", "qid") and _identifier(token) in read_schema.COLUMNS and _identifier(token) not in used:
                    used.append(_identifier(token))
        except ReadError:
            used = []
        if used:
            return "{}. Columns: {}".format(message, "; ".join("{} ({})".format(v, ", ".join(read_schema.COLUMNS[v])) for v in used))
        if message.lower().startswith("no such table"):
            return "{}. Views: {}".format(message, ", ".join(read_schema.VIEW_NAMES))
        return message

    def _translate(self, exc, sql=None):
        """A short plain message for an engine error: never a path, never more than the model wrote or a view column."""
        if self._violation:
            return ReadError(self._with_columns(self._violation, sql), "denied")
        text = str(exc)
        lowered = text.lower()
        if "interrupted" in lowered:
            return ReadError("the query took too long: make it simpler or narrow the dates", "limit")
        if lowered.startswith(("no such column", "ambiguous column name", "no such function", "wrong number of arguments")):
            return ReadError(self._with_columns(text[:160], sql), "sql")
        if lowered.startswith("no such table"):
            return ReadError(self._with_columns("{} (read only the v_ views)".format(text[:120]), sql), "denied")
        if lowered.startswith(("syntax error", "near ", "incomplete input")):
            return ReadError(text[:160], "sql")
        if "not authorized" in lowered or "authoriz" in lowered:
            return ReadError("that is not allowed: read only the described v_ views", "denied")
        return ReadError("the query could not be run: check the SQL", "sql")

    def close(self):
        conn, self.conn = self.conn, None
        if conn is not None:
            try:
                conn.set_authorizer(_allow_all)
                conn.set_progress_handler(None, 0)
                conn.close()
            except sqlite3.Error:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


def _unique(names):
    seen, out = {}, []
    for name in names:
        name = str(name)
        count = seen.get(name.lower(), 0)
        seen[name.lower()] = count + 1
        out.append(name if count == 0 else "{}_{}".format(name, count + 1))
    return out


def _cell(value):
    if isinstance(value, bytes):
        return "[binary]"
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return value[:MAX_CELL_CHARS - 1] + "…"
    return value


def run_query(source, sql, today, now_hhmm="00:00", time_limit=TIME_LIMIT_S, deadline=None, clock=time.monotonic):
    """Open a read session on `source`, run one query, close it. Raises ReadError."""
    with ReadSession(source, today, now_hhmm, time_limit, clock) as session:
        return session.run(sql, deadline=deadline)
