"""Drive scripted conversations through the real voice assistant and score every turn.

For each conversation the engine builds a throw-away in-memory clinic (never clinic.db), freezes the clock at
Friday 2026-10-09 10:00 IST, and drives a real `VoiceContext` through the real `voice_turns.handle_turn` /
`handle_pick`, exactly as the live voice session does. It captures, for every turn, what the planner was sent
(the state card, in model-first mode), the tool it called, the route, what the app answered, the memory afterwards,
and scores each expectation separately (intent_ok, kind_ok, ask_kind_ok, slots_ok, patient_ok, note_ok, state_ok,
write_ok, card_ok).

A turn is `{say}`, `{pick}`, `{approve}`, `{idle_minutes}`, `{network}` or `{architecture}` (tests/replay_cases/dsl.py).
Nothing is written to clinic data except by an `approve` turn (the same propose + confirm /approve runs); a turn
that is not an approval is checked to have changed nothing (`write_ok`) unless it says `no_write=False`.

Offline configs install a tripwire so that nothing here can reach the network or a real model; the live configs
(scripts/replay/configs.py) are refused unless the caller passes `allow_live=True`.
"""

import contextlib
import hashlib
import os
import re
import socket
import time
from unittest import mock

from clinic import architecture, branches, core, db, planner_log
from clinic.adapters.local_sqlite import LocalSQLiteAdapter
from clinic.adapters.registry import build_write_handlers
from clinic.nlu import planner, sarvam, tools
from clinic.pipeline import (ClosurePlanResult, NavigateResult, ParsedResult, PipelineError, ReadResult,
                             SwitchBranchResult)
from clinic.voice_context import AskResult, CardUpdate, Note, VoiceContext
from clinic import state_card, voice_turns
from scripts.replay import clock, configs

# What the app treats as a write intent: a voice command for one of these only ever opens a review card.
# (A copy of app.DEFERRED_INTENTS; tests/test_replay_harness.py checks the two are the same.)
DEFERRED_INTENTS = frozenset({
    "register_patient", "register_staff", "record_visit",
    "set_followup", "log_attendance", "log_expense",
    "cancel_followup", "reschedule_followup",
    "book_appointment", "cancel_appointment", "reschedule_appointment",
    "queue_check_in", "queue_call_next", "queue_mark_done", "queue_mark_no_show",
})

PASS, FAIL, KNOWN_GAP, SKIPPED = "PASS", "FAIL", "KNOWN GAP", "SKIPPED"
CHECKS = ("intent_ok", "kind_ok", "ask_kind_ok", "slots_ok", "patient_ok", "note_ok", "state_ok", "write_ok", "route_ok",
          "card_ok")
LANGUAGE_CODE = {"en": "en-IN", "hinglish": "hi-IN", "hi": "hi-IN"}
_BUSINESS_TABLES = ("patients", "visits", "followups", "appointments", "staff", "attendance", "expenses", "proposals",
                    "audit_log", "closures", "closure_moves", "booking_blocks")


# A conversation written for a model-first config also runs under the model-reads one (it is model-first plus model-written reads).
_SUPERSET = {"model_first_scripted": "model_reads_scripted", "model_first_live": "model_reads_live"}


class LiveNotAllowed(Exception):
    """A live config was run without the caller explicitly allowing it."""


# -- the scripted planner ----------------------------------------------------------------------

class ScriptedBackend(planner.Backend):
    """Answers each planner call with the current turn's golden answer. `golden` is a (tool, args) tuple, None
    (no tool call), {"prose": words} (plain words, no tool call: `last_text` carries them), the word "timeout" (the
    call fails), or UNSET (nothing scripted: no tool call); or a LIST of those, one per planner call of the turn (the
    model-reads multi-step lookup: each call takes the next entry, an exhausted list answers nothing). While the
    network is "down" every call fails as Sarvam's timeout does. Every call is recorded in `calls`."""

    name = "scripted"

    def __init__(self):
        self.golden = None
        self.down = False
        self.calls = []
        self.histories = []
        self.last_text = None
        self.last_encoding = None

    def plan_with_history(self, system, user, tool_schemas, history):
        self.last_encoding = "tool"
        self.histories.append(list(history))
        return self.plan(system, user, tool_schemas)

    def plan(self, system, user, tool_schemas):
        names = [t["function"]["name"] for t in tool_schemas]
        self.calls.append({"system": system, "user": user, "tools": names})
        self.last_text = None
        if self.down:
            raise sarvam.SarvamError("timeout")
        golden = self.golden
        if isinstance(golden, list):
            golden = golden.pop(0) if golden else None
        if names == ["heard_name"]:                                  # the small hosted name read
            args = golden[1] if isinstance(golden, tuple) else {}
            name = args.get("patient_name") or args.get("staff_name") or args.get("name")
            return planner.ToolCall("heard_name", {"name": name}) if name else None
        if golden == "timeout":
            raise sarvam.SarvamError("timeout")
        if isinstance(golden, dict) and "prose" in golden:
            self.last_text = golden["prose"]
            return None
        if not isinstance(golden, tuple):
            return None
        return planner.ToolCall(golden[0], dict(golden[1]))


class FakeMonotonic:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# -- the environment ----------------------------------------------------------------------------

@contextlib.contextmanager
def sandbox(config, drop=frozenset()):
    """Everything a run needs to be repeatable: the frozen clock, the environment switches, and (offline) a tripwire
    on every way out of the machine. Live configs keep the network and the real key but use the same clock."""
    env = {"INTENT_LLM_ENABLED": "1", "INTENT_PLANNER_ENABLED": "0" if config.planner == "off" else "1",
           "PLANNER_BACKEND": "sarvam", "NETWORK_PROBE_ENABLED": "0", "WHATSAPP_NOTIFY_MODE": "dry_run"}
    if not config.live:
        env["SARVAM_API_KEY"] = ""
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ, env))
        stack.enter_context(clock.frozen())
        if not config.live:
            def boom(*args, **kwargs):
                raise AssertionError("the replay reached the network in an offline config")
            for target in ("httpx.post", "httpx.get", "httpx.Client.send", "httpx.Client.post", "socket.create_connection",
                           "socket.getaddrinfo", "clinic.nlu.planner.OllamaBackend.plan", "clinic.nlu.sarvam.SarvamBackend.plan",
                           "clinic.nlu.planner.OllamaBackend.plan_with_history",
                           "clinic.nlu.sarvam.SarvamBackend.plan_with_history"):
                stack.enter_context(mock.patch(target, side_effect=boom))
        token = state_card.dropped.set(frozenset(drop))
        stack.callback(state_card.dropped.reset, token)
        yield


# -- building the clinic -------------------------------------------------------------------------

def build_clinic(setup):
    """(conn, ids): an in-memory clinic with the setup's branches, patients, staff and appointments. `ids` maps
    each key to its database id (and each branch code to its id)."""
    conn = db.connect(":memory:")
    ids = {}
    for item in setup.get("branches", []):
        branch_id = branches.add_branch(conn, item["code"], item["name"], item.get("address"), pin_code=item.get("pin_code"))
        doctor_id = branches.add_doctor(conn, item["doctor"])
        for weekday in range(7):
            branches.add_schedule(conn, doctor_id, branch_id, weekday, *item["hours"])
        ids["branch:" + item["code"]] = branch_id
    ids["branch:A"] = 1
    for patient in setup.get("patients", []):
        cur = conn.execute("INSERT INTO patients (name, phone, age) VALUES (?, ?, ?)",
                           (patient["name"], patient["phone"], patient.get("age", 40)))
        ids[patient["key"]] = cur.lastrowid
    for member in setup.get("staff", []):
        cur = conn.execute("INSERT INTO staff (name, role) VALUES (?, ?)", (member["name"], member.get("role")))
        ids[member["key"]] = cur.lastrowid
    for appt in setup.get("appointments", []):
        branch_id = ids["branch:" + appt.get("branch", "A")]
        patient_id = ids.get(appt.get("patient"))
        cur = conn.execute(
            "INSERT INTO appointments (patient_id, patient_name, patient_phone, appt_date, start_time, duration_minutes, "
            "status, branch_id, doctor_id) VALUES (?, ?, ?, ?, ?, 30, ?, ?, ?)",
            (patient_id, None if patient_id else appt.get("name"), None if patient_id else appt.get("phone"),
             appt["date"], appt["time"], appt.get("status", "booked"), branch_id,
             branches.doctor_at(conn, branch_id, appt["date"], appt["time"])))
        if appt.get("key"):
            ids[appt["key"]] = cur.lastrowid
    for visit in setup.get("visits", []):                  # {"patient": key, "date": ISO, "fee": rupees}
        conn.execute("INSERT INTO visits (patient_id, visit_date, fee_paise) VALUES (?, ?, ?)",
                     (ids[visit["patient"]], visit["date"], int(round(visit["fee"] * 100))))
    for expense in setup.get("expenses", []):              # {"date": ISO, "description": text, "amount": rupees}
        conn.execute("INSERT INTO expenses (expense_date, description, amount_paise) VALUES (?, ?, ?)",
                     (expense["date"], expense["description"], int(round(expense["amount"] * 100))))
    conn.commit()
    return conn, ids


def fingerprint(conn):
    """A hash of every business table: equal before and after means nothing was written."""
    digest = hashlib.sha256()
    for table in _BUSINESS_TABLES:
        try:
            rows = conn.execute("SELECT * FROM {} ORDER BY 1".format(table)).fetchall()
        except Exception:
            continue
        digest.update(table.encode())
        digest.update(repr([tuple(r) for r in rows]).encode("utf-8"))
    return digest.hexdigest()


def table_count(conn, table):
    return conn.execute("SELECT COUNT(*) FROM {}".format(table)).fetchone()[0]


# -- describing what came back -------------------------------------------------------------------

def _patient_key(ids_by_id, value):
    return ids_by_id.get(value) if value is not None else None


def summarize(result, error, ids_by_id):
    """The app's answer for one turn as plain data."""
    out = {"kind": None, "intent": None, "ask_kind": None, "slots": {}, "options": [], "note": "", "patient": None,
           "patient_label": None, "rows": None}
    if error is not None:
        out.update(kind="error", note=str(error))
        return out
    if isinstance(result, AskResult):
        out.update(kind="ask", intent=result.intent, ask_kind=result.kind, slots=dict(result.slots or {}),
                   options=[o.get("label") for o in result.options], note=result.question)
    elif isinstance(result, ParsedResult):
        resolved = result.resolved or {}
        out.update(kind="card", intent=result.intent, slots=dict(result.slots or {}),
                   patient_label=resolved.get("patient_label"), rows=len(resolved.get("appointments") or []),
                   note=" | ".join(list(resolved.get("notes") or []) + [resolved[k] for k in ("note", "phone_problem") if resolved.get(k)]))
        out["patient"] = _patient_key(ids_by_id, resolved.get("patient_id") or (result.slots or {}).get("patient_id"))
    elif isinstance(result, ReadResult):
        out.update(kind="read", intent=result.intent, note=result.answer_text or "",
                   rows=len(result.data) if isinstance(result.data, (list, tuple)) else None)
    elif isinstance(result, CardUpdate):
        out.update(kind="card_update", intent=result.intent, slots=dict(result.changes), note=result.summary)
    elif isinstance(result, Note):
        out.update(kind="note", note=result.message)
    elif isinstance(result, NavigateResult):
        out.update(kind="navigate", intent=result.intent, note=result.answer_text or "")
    elif isinstance(result, SwitchBranchResult):
        out.update(kind="switch_branch", intent=result.intent, note=result.answer_text or "",
                   slots={"branch_id": result.branch_id})
    elif isinstance(result, ClosurePlanResult):
        out.update(kind="closure", intent=result.intent, note=result.answer_text or "")
    else:
        out.update(kind="other", note=repr(result)[:200])
    return out


def memory_after(ctx, ids_by_id):
    pending = ctx.pending
    return {
        "pending": pending["kind"] if pending else None,
        "task": pending["intent"] if pending else None,
        "options": len(pending["options"]) if pending else 0,
        "card": ctx.open_card["intent"] if ctx.open_card else None,
        "remembered": _patient_key(ids_by_id, ctx.patient["id"]) if ctx.patient else None,
        "list_rows": len(ctx.list_rows),
        "branch": ctx.branch,
    }


def describe_expect(expect):
    """The expectations of a turn in plain words, for the report."""
    parts = []
    for key, value in expect.items():
        if key == "slots":
            parts.append("slots " + ", ".join("{}={}".format(k, v) for k, v in value.items()))
        elif key == "note_contains":
            parts.append("note contains " + (value if isinstance(value, str) else " + ".join(value)))
        elif key == "no_write":
            parts.append("may write" if value is False else "writes nothing")
        elif key == "kind" and not isinstance(value, str):
            parts.append("kind={}".format(" or ".join(value)))
        else:
            parts.append("{}={}".format(key, value))
    return "; ".join(parts) if parts else "nothing specific (and writes nothing)"


# -- scoring -------------------------------------------------------------------------------------

_PRIVACY_BAD = (re.compile(r"\d{5,}"), re.compile(r"\b(?:patient|appointment|branch|doctor|wa)_id\b", re.I),
                re.compile(r"\bid\s*[=:]\s*\d", re.I), re.compile(r"\btoken\b", re.I))


def _same(expected, actual):
    if expected is None:
        return actual in (None, "", [], False)
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.strip().casefold() == actual.strip().casefold()
    return expected == actual


def _contains_all(needles, haystack):
    needles = [needles] if isinstance(needles, str) else list(needles)
    low = (haystack or "").casefold()
    return [n for n in needles if n.casefold() not in low]


def evaluate(expect, summary, memory, wrote, approving, model_first, state_card_text, branch_codes, route=None):
    """{check name: (True / False / None, why)} for one turn. None = the turn made no expectation of this kind."""
    results = {name: (None, "") for name in CHECKS}

    def record(name, ok, why=""):
        results[name] = (ok, "" if ok else why)

    if "kind" in expect:
        wanted = expect["kind"]
        allowed = [wanted] if isinstance(wanted, str) else list(wanted)
        record("kind_ok", summary["kind"] in allowed, "expected a {} but the app gave a {}{}".format(
            " or ".join(allowed), summary["kind"], " ({})".format(summary["note"][:90]) if summary["kind"] in ("error", "note") else ""))
    if "intent" in expect:
        record("intent_ok", summary["intent"] == expect["intent"],
               "expected intent {} but got {}".format(expect["intent"], summary["intent"]))
    if "ask_kind" in expect:
        record("ask_kind_ok", summary["ask_kind"] == expect["ask_kind"],
               "expected the question {} but got {}".format(expect["ask_kind"], summary["ask_kind"]))

    mismatches = []
    view = dict(summary["slots"])
    if "branch_id" in view:
        view["branch"] = branch_codes.get(view["branch_id"])
    for key, wanted in (expect.get("slots") or {}).items():
        if not _same(wanted, view.get(key)):
            mismatches.append("{} is {!r}, expected {!r}".format(key, view.get(key), wanted))
    if "options_count" in expect and len(summary["options"]) != expect["options_count"]:
        mismatches.append("{} options offered, expected {}".format(len(summary["options"]), expect["options_count"]))
    if "options_contain" in expect:
        joined = " | ".join(summary["options"])
        missing = _contains_all(expect["options_contain"], joined)
        if missing:
            mismatches.append("options do not show {}".format(", ".join(missing)))
    if "rows" in expect and summary["rows"] != expect["rows"]:
        mismatches.append("{} rows, expected {}".format(summary["rows"], expect["rows"]))
    if "slots" in expect or "options_count" in expect or "options_contain" in expect or "rows" in expect:
        record("slots_ok", not mismatches, "; ".join(mismatches))

    if "patient" in expect:
        record("patient_ok", _same(expect["patient"], summary["patient"]),
               "the patient resolved is {!r}, expected {!r}".format(summary["patient"], expect["patient"]))

    text_checks = []
    if "note_contains" in expect:
        missing = _contains_all(expect["note_contains"], summary["note"])
        if missing:
            text_checks.append("the message does not say {!r} (it says {!r})".format(", ".join(missing), summary["note"][:120]))
    if "note_lacks" in expect:
        present = [n for n in ([expect["note_lacks"]] if isinstance(expect["note_lacks"], str) else expect["note_lacks"])
                   if n.casefold() in summary["note"].casefold()]
        if present:
            text_checks.append("the message should not say {!r}".format(", ".join(present)))
    if "note_contains" in expect or "note_lacks" in expect:
        record("note_ok", not text_checks, "; ".join(text_checks))

    state_problems = []
    for key, got in (("pending_after", memory["pending"]), ("task_after", memory["task"]), ("card_after", memory["card"]),
                     ("remembered_after", memory["remembered"]), ("list_after", memory["list_rows"])):
        if key in expect and not _same(expect[key], got):
            state_problems.append("{} is {!r}, expected {!r}".format(key.replace("_after", ""), got, expect[key]))
    if any(k in expect for k in ("pending_after", "task_after", "card_after", "remembered_after", "list_after")):
        record("state_ok", not state_problems, "; ".join(state_problems))

    if "route_contains" in expect:
        missing = _contains_all(expect["route_contains"], route or "")
        record("route_ok", not missing, "the route was {!r}, expected it to say {}".format(route, ", ".join(missing)))

    if not approving and expect.get("no_write", True):
        record("write_ok", not wrote, "something was written before anyone pressed Approve")
    elif approving:
        pass

    if model_first and state_card_text is not None:
        problems = []
        for pattern in _PRIVACY_BAD:
            found = pattern.search(state_card_text)
            if found:
                problems.append("the state card contains {!r}".format(found.group()))
        if len(state_card_text) > state_card.MAX_CHARS + 80:
            problems.append("the state card is {} characters (limit {})".format(len(state_card_text), state_card.MAX_CHARS))
        if "card_has" in expect:
            missing = _contains_all(expect["card_has"], state_card_text)
            if missing:
                problems.append("the state card does not show {}".format(", ".join(missing)))
        if "card_lacks" in expect:
            present = [n for n in ([expect["card_lacks"]] if isinstance(expect["card_lacks"], str) else expect["card_lacks"])
                       if n.casefold() in state_card_text.casefold()]
            if present:
                problems.append("the state card should not show {}".format(", ".join(present)))
        record("card_ok", not problems, "; ".join(problems))
    return results


def verdict_of(results, known_gap):
    failed = [name for name, (ok, _) in results.items() if ok is False]
    if not failed:
        return PASS
    return KNOWN_GAP if known_gap else FAIL


# -- final database checks -----------------------------------------------------------------------

def check_final_db(conn, ids, checks, start_print):
    """[(description, True / False, why)] for the conversation-level database expectations."""
    out = []
    for check in checks:
        (kind, spec), = check.items()
        if kind == "appointment":
            sql = ("SELECT a.id, a.patient_id, a.patient_name, p.name AS pname, a.appt_date, a.start_time, a.status, b.code "
                   "FROM appointments a LEFT JOIN patients p ON p.id = a.patient_id LEFT JOIN branches b ON b.id = a.branch_id "
                   "WHERE a.status = ?")
            rows = conn.execute(sql, (spec.get("status", "booked"),)).fetchall()

            def fits(row):
                if "patient" in spec and row["patient_id"] != ids.get(spec["patient"]):
                    return False
                if "name" in spec and spec["name"].casefold() not in ((row["pname"] or row["patient_name"] or "").casefold(),):
                    return False
                return all(spec.get(k) in (None, row[col]) for k, col in
                           (("date", "appt_date"), ("time", "start_time"), ("branch", "code")))
            found = [r for r in rows if fits(r)]
            want = spec.get("count", 1)
            desc = "appointments {}".format(", ".join("{}={}".format(k, v) for k, v in spec.items() if k != "count"))
            out.append(("{}: {}".format(desc, want), len(found) == want,
                        "{} matching appointment(s), expected {}".format(len(found), want)))
        elif kind == "patient":
            rows = conn.execute("SELECT id FROM patients WHERE lower(name) = lower(?)", (spec["name"],)).fetchall()
            want = spec.get("count", 1)
            out.append(("patients named {}: {}".format(spec["name"], want), len(rows) == want,
                        "{} patient(s) named {}, expected {}".format(len(rows), spec["name"], want)))
        elif kind == "patients_total":
            count = table_count(conn, "patients")
            out.append(("patients in total: {}".format(spec), count == spec, "{} patients, expected {}".format(count, spec)))
        elif kind == "appointments_total":
            count = conn.execute("SELECT COUNT(*) FROM appointments WHERE status IN ('booked', 'confirmed')").fetchone()[0]
            out.append(("active appointments in total: {}".format(spec), count == spec, "{} active, expected {}".format(count, spec)))
        elif kind == "no_writes":
            out.append(("nothing was written", fingerprint(conn) == start_print, "the clinic's data changed"))
        elif kind == "no_duplicate_patients":
            dupes = conn.execute("SELECT name, phone, COUNT(*) c FROM patients GROUP BY lower(name), phone HAVING c > 1").fetchall()
            out.append(("no duplicate patients", not dupes, "duplicates: {}".format(", ".join(r["name"] for r in dupes))))
        else:
            out.append(("unknown final_db check {}".format(kind), False, "the harness does not know this check"))
    return out


# -- running a conversation ------------------------------------------------------------------------

def _golden_for(config, turn):
    """What the scripted planner answers on this turn under `config` (see tests/replay_cases/dsl.py)."""
    if "model" not in turn:
        return None
    golden = turn["model"]
    if isinstance(golden, list):
        golden = list(golden)            # the scripted backend consumes it; the conversation itself stays as written
    if config.architecture == "classic":
        if "classic_model" in turn:
            return turn["classic_model"]
        if isinstance(golden, tuple) and golden[0] in tools.DIALOGUE_BY_NAME:
            return None                  # the classic planner has no such tool: it would have said nothing useful
    return golden


def _route_label(row, planner_on, had_pending):
    if row is None:
        if not planner_on:
            return "rules (planner off)"
        return "rules (answer to the open question or card edit)" if had_pending else "rules (no planner call)"
    detail = row.get("route_detail") or ""
    if detail.startswith("mf_fallback"):
        return "fallback to classic (planner gave nothing usable)"
    if row["route_taken"] == "planner":
        return "planner ({})".format(detail) if detail.startswith("sql:") else "planner"
    if row["route_taken"] == "rules":
        return "rules ({})".format(detail) if detail else "rules"
    return row["route_taken"]


def run_conversation(case, config, drop=frozenset(), allow_live=False, pace=None, sleep=time.sleep):
    """One conversation under one config -> a result dict (see report.py for how it is shown). The caller is
    responsible for `sandbox()`; this function only needs the frozen clock to already be in place."""
    if config.live and not allow_live:
        raise LiveNotAllowed("{} calls the real Sarvam model and was not explicitly allowed".format(config.name))
    only = case.get("configs")
    if only:
        only = list(only) + [_SUPERSET[n] for n in only if n in _SUPERSET]      # model-reads is model-first plus more
    result = {"id": case["id"], "title": case["title"], "category": case["category"], "language": case["language"],
              "known_gap": case.get("known_gap"), "config": config.name, "turns": [], "final_db": [], "verdict": None,
              "first_fail": None, "note": case.get("note")}
    if only and config.name not in only:
        result["verdict"] = SKIPPED
        result["skip_reason"] = "this conversation is only meaningful under {}".format(", ".join(only))
        for number, turn in enumerate(case["turns"], 1):
            result["turns"].append(_skipped_turn(case, config.name, number, turn, result["skip_reason"]))
        return result

    conn, ids = build_clinic(case.get("setup") or {})
    people = {p["key"] for p in (case.get("setup") or {}).get("patients", [])}
    ids_by_id = {v: k for k, v in ids.items() if k in people}          # patient ids only (staff and appointments have their own)
    branch_codes = {v: k.split(":", 1)[1] for k, v in ids.items() if k.startswith("branch:")}
    adapter = LocalSQLiteAdapter()
    handlers = build_write_handlers(adapter, adapter)
    monotonic = FakeMonotonic()
    ctx = VoiceContext(clock=monotonic)
    ctx.set_client_branch(ids["branch:A"], ids["branch:A"])
    language = LANGUAGE_CODE.get(case["language"], "en-IN")
    architecture.set_mode(conn, config.architecture)
    backend = None
    if config.planner == "scripted":
        backend = ScriptedBackend()
        planner.set_backend(backend)
    elif config.planner == "live":
        planner.set_backend(None)
    start_print = fingerprint(conn)
    last_log_id = 0
    card = {"slots": None, "resolved": {}, "transcript": ""}
    try:
        for number, turn in enumerate(case["turns"], 1):
            record = {"conv": case["id"], "config": config.name, "turn": number, "action": None, "said": "",
                      "state_card": None, "tool": None, "tool_args": None, "route": None, "route_detail": None,
                      "result": None, "expected": describe_expect(turn.get("expect") or {}), "checks": {},
                      "why": "", "verdict": None, "planner_ms": None, "turn_ms": None, "tokens_in": None,
                      "tokens_out": None, "cost_paise": None, "memory": None, "notes": ""}
            expect = turn.get("expect") or {}
            before = fingerprint(conn)
            had_pending = bool(ctx.pending)
            summary, error, approving = None, None, False
            started = time.perf_counter()

            if "say" in turn or "pick" in turn:
                if backend is not None:
                    backend.golden = _golden_for(config, turn)
                if "say" in turn:
                    record["action"], record["said"] = "say", turn["say"]
                    try:
                        outcome = voice_turns.handle_turn(ctx, conn, turn["say"], adapter, adapter, language,
                                                          defer_intents=DEFERRED_INTENTS)
                    except PipelineError as exc:
                        outcome, error = None, exc
                else:
                    record["action"] = "tap"
                    picked = voice_turns.handle_pick(ctx, conn, turn["pick"], adapter, adapter, language,
                                                     defer_intents=DEFERRED_INTENTS)
                    if picked is None:
                        outcome, error = None, PipelineError("no question with that option was open")
                        record["said"] = "(tap option {})".format(turn["pick"] + 1)
                    else:
                        record["said"], outcome = "(tap) " + picked[0], picked[1]
                summary = summarize(outcome, error, ids_by_id)
                if isinstance(outcome, ParsedResult):
                    card = {"slots": None, "resolved": dict(outcome.resolved or {}), "transcript": record["said"],
                            "log_id": ctx.planner_log_id}
                if isinstance(outcome, CardUpdate) and {"patient_name", "patient_phone"} & set(outcome.changes):
                    card["resolved"] = {}
                if config.live and "say" in turn:
                    sleep(pace if pace is not None else configs.pace_seconds())
            elif "approve" in turn:
                record["action"], record["said"], approving = "approve", "(a person presses Approve)", True
                summary = _approve(conn, ctx, card, handlers, ids_by_id)
            elif "idle_minutes" in turn:
                record["action"], record["said"] = "idle", "(wait {} minutes)".format(turn["idle_minutes"])
                monotonic.now += turn["idle_minutes"] * 60.0
                ctx.expire_if_idle()
                summary = summarize(Note("(the clock moved on)"), None, ids_by_id)
            elif "network" in turn:
                record["action"], record["said"] = "network", "(the planner is {})".format("unreachable" if turn["network"] == "down" else "reachable again")
                if backend is not None:
                    backend.down = turn["network"] == "down"
                summary = summarize(Note("(no command)"), None, ids_by_id)
            elif "architecture" in turn:
                record["action"], record["said"] = "switch", "(Settings: command understanding -> {})".format(turn["architecture"])
                architecture.set_mode(conn, turn["architecture"])
                summary = summarize(Note("(setting saved)"), None, ids_by_id)
            record["turn_ms"] = int((time.perf_counter() - started) * 1000)

            row = _new_log_row(conn, last_log_id)
            if row:
                last_log_id = row["id"]
                record.update(tool=row["planner_tool"], tool_args=row["planner_args_json"], route_detail=row["route_detail"],
                              state_card=row.get("state_card"), planner_ms=row["latency_ms"], tokens_in=row["tokens_in"],
                              tokens_out=row["tokens_out"], cost_paise=row["cost_paise"], notes=row["override_notes"] or "")
            if record["action"] in ("say", "tap"):
                record["route"] = _route_label(row, config.planner != "off" and config.architecture is not None, had_pending)
            memory = memory_after(ctx, ids_by_id)
            wrote = fingerprint(conn) != before
            skip_checks = record["action"] in ("idle", "network", "architecture") and not expect
            if skip_checks:
                checks = {name: (None, "") for name in CHECKS}
            else:
                checks = evaluate(expect, summary, memory, wrote, approving, config.architecture in ("model_first", "model_reads"),
                                  record["state_card"], branch_codes, record["route"])
            if approving:
                checks.update(_approve_checks(expect, summary))
            record["result"], record["memory"] = summary, memory
            record["checks"] = {k: {"ok": ok, "why": why} for k, (ok, why) in checks.items()}
            record["verdict"] = verdict_of(checks, case.get("known_gap"))
            record["why"] = "; ".join("{}: {}".format(k, why) for k, (ok, why) in checks.items() if ok is False)
            result["turns"].append(record)

        checks = check_final_db(conn, ids, case.get("final_db") or [], start_print)
        result["final_db"] = [{"check": d, "ok": ok, "why": "" if ok else why} for d, ok, why in checks]
    finally:
        if config.planner != "off":
            planner.set_backend(None)
        conn.close()

    verdicts = [t["verdict"] for t in result["turns"]]
    failed_db = any(not c["ok"] for c in result["final_db"])
    if FAIL in verdicts or (failed_db and not case.get("known_gap")):
        result["verdict"] = FAIL
    elif KNOWN_GAP in verdicts or failed_db:
        result["verdict"] = KNOWN_GAP
    else:
        result["verdict"] = PASS
    for t in result["turns"]:
        if t["verdict"] in (FAIL, KNOWN_GAP):
            result["first_fail"] = t["turn"]
            break
    return result


def _skipped_turn(case, config_name, number, turn, why):
    return {"conv": case["id"], "config": config_name, "turn": number, "action": "say" if "say" in turn else "other",
            "said": turn.get("say", ""), "state_card": None, "tool": None, "tool_args": None, "route": None,
            "route_detail": None, "result": {"kind": None, "intent": None, "ask_kind": None, "slots": {}, "options": [],
                                             "note": "", "patient": None, "patient_label": None, "rows": None},
            "expected": describe_expect(turn.get("expect") or {}), "checks": {n: {"ok": None, "why": ""} for n in CHECKS},
            "why": why, "verdict": SKIPPED, "planner_ms": None, "turn_ms": None, "tokens_in": None, "tokens_out": None,
            "cost_paise": None, "memory": None, "notes": ""}


def _new_log_row(conn, last_id):
    row = conn.execute("SELECT * FROM planner_log WHERE id > ? ORDER BY id DESC LIMIT 1", (last_id,)).fetchone()
    return dict(row) if row else None


def _approve(conn, ctx, card, handlers, ids_by_id):
    """A person presses Approve on the card on screen: the same propose + confirm /approve runs."""
    open_card = ctx.open_card
    if not open_card:
        out = summarize(None, PipelineError("there is no card on screen to approve"), ids_by_id)
        out["kind"] = "approve_failed"
        return out
    slots = dict(open_card["slots"])
    resolved_id = (card.get("resolved") or {}).get("patient_id")
    if resolved_id is not None and not slots.get("patient_id"):
        slots["patient_id"] = resolved_id
    intent = open_card["intent"]
    try:
        proposal = core.propose(conn, intent, slots, source_text=card.get("transcript"))
        entity_type, entity_id = core.confirm(conn, proposal, handlers)
    except Exception as exc:
        out = summarize(None, exc, ids_by_id)
        out["kind"] = "approve_failed"
        return out
    planner_log.set_outcome(conn, card.get("log_id"), "approved")
    ctx.close_card(open_card["card_id"])
    ctx.touch()
    return {"kind": "approved", "intent": intent, "ask_kind": None, "slots": slots, "options": [],
            "note": "Saved: {} #{}".format(entity_type, entity_id), "patient": ids_by_id.get(slots.get("patient_id")),
            "patient_label": None, "rows": None}


def _approve_checks(expect, summary):
    out = {}
    want = expect.get("approved", None)
    if want is not None:
        ok = (summary["kind"] == "approved") == bool(want)
        out["kind_ok"] = (ok, "" if ok else "approving {} (the app said: {})".format(
            "failed" if want else "should have failed", summary["note"][:120]))
    return out


def run_cases(cases, config, drop=frozenset(), allow_live=False, only_ids=None, progress=None, pace=None, sleep=time.sleep):
    """Every conversation under one config (inside one sandbox) -> list of results."""
    if config.live and not allow_live:
        raise LiveNotAllowed("{} calls the real Sarvam model and was not explicitly allowed".format(config.name))
    results = []
    with sandbox(config, drop):
        for case in cases:
            if only_ids and case["id"] not in only_ids:
                continue
            results.append(run_conversation(case, config, drop, allow_live, pace, sleep))
            if progress:
                progress(config, results[-1])
    return results
