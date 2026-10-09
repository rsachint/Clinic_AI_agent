"""The "state card": what the planner is told about the conversation in model-first mode.

Built fresh for every turn from the per-connection VoiceContext (clinic/voice_context.py) and a few
database facts, as compact plain text sent in front of the user's sentence. It lets the model read a
short answer ("the second one", "it's a new patient called Nalin", "make it 7") in the light of the
question that is open, the card on screen and the patient just discussed.

Privacy rules (tested, tests/test_state_card.py):
  * NO database ids, ever. The app keeps the real option -> id table; the model only ever sees numbered
    options and answers with a number.
  * Names as spoken or stored, and the LAST FOUR digits of a phone number ("...3210"); never a full number.
  * No diagnosis, visit or appointment note, message body, token or WhatsApp id: only a whitelist of
    booking fields is shown.
  * Phone-like digit runs anywhere in earlier turns are masked to their last four digits.
  * Size capped (about 600 tokens, MAX_CHARS): long lists are cut and the card says so.

Each section can be dropped (`drop=`: task, pending, card, remembered, list, last_turns) so a replay can
measure what each piece of memory is worth (scripts/replay, "--ablate"). Production never drops any.
"""

import contextvars
import re
from datetime import date

from clinic import branches

SECTIONS = ("task", "pending", "card", "remembered", "list", "last_turns")

MAX_CHARS = 1800                 # ~600 tokens for mixed English / Hinglish / Devanagari text
MAX_OPTIONS = 8
MAX_LIST_ROWS = 8
MAX_TURN_CHARS = 170

# Per-run switch used by the replay's ablation; production leaves it empty.
dropped = contextvars.ContextVar("state_card_dropped", default=frozenset())

# The task slots shown to the model, in this order. Anything else (notes, ids, internal flags) is left out.
_SLOT_ORDER = ("patient_name", "name", "staff_name", "doctor", "patient_phone", "phone", "appt_date", "start_time", "branch_id",
               "new_due_date", "days_from_now", "fee_rupees", "amount_rupees", "status", "age", "role")
_REQUIRED = {
    "book_appointment": ("patient_name", "appt_date", "start_time"),
    "reschedule_appointment": ("patient_name", "appt_date", "start_time"),
    "cancel_appointment": ("patient_name",),
    "record_visit": ("patient_name", "fee_rupees"),
    "set_followup": ("patient_name", "days_from_now"),
    "cancel_followup": ("patient_name",),
    "reschedule_followup": ("patient_name", "new_due_date"),
    "log_attendance": ("staff_name", "status"),
    "register_patient": ("name", "phone"),
}
_PHONE_RUN = re.compile(r"(?<!\w)\+?\d[\d \-]{5,}\d(?!\w)")
_SOURCE_TAIL = re.compile(r"\s*Source:.*$", re.S)
_LABEL_PHONE = re.compile(r"\s*\((\+?[\d\s-]+)\)\s*$")


def tail4(phone):
    """'...3210' for a phone number (any format), '' when it has fewer than 4 digits."""
    digits = re.sub(r"\D", "", str(phone or ""))
    return "...{}".format(digits[-4:]) if len(digits) >= 4 else ""


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def mask_phones(text):
    """Every phone-like run of digits in `text` shown as its last four digits only (ISO dates are left alone)."""
    out, last = [], 0
    for date_match in _ISO_DATE.finditer(text or ""):
        out.append(_PHONE_RUN.sub(lambda m: tail4(m.group()) or m.group(), text[last:date_match.start()]))
        out.append(date_match.group())
        last = date_match.end()
    out.append(_PHONE_RUN.sub(lambda m: tail4(m.group()) or m.group(), (text or "")[last:]))
    return "".join(out)


def person_label(label):
    """'Rahul Sharma (9876543210)' -> 'Rahul Sharma ...3210'; a label without a number is returned as is."""
    label = (label or "").strip()
    found = _LABEL_PHONE.search(label)
    if not found:
        return mask_phones(label)
    return "{} {}".format(label[:found.start()].strip(), tail4(found.group(1))).strip()


def _short(text, limit):
    text = re.sub(r"\s+", " ", mask_phones(_SOURCE_TAIL.sub("", str(text or "")))).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _branch_code(conn, branch_id):
    if branch_id in (None, "") or conn is None:
        return None
    found = branches.get_branch(conn, branch_id)
    return found["code"] if found else None


def _slot_text(conn, slots, intent=None):
    """'patient=Rahul Sharma, date=2026-10-10' for the whitelisted slots that hold a value."""
    parts = []
    for key in _SLOT_ORDER:
        value = (slots or {}).get(key)
        if value in (None, "", [], False):
            continue
        if key == "branch_id":
            code = _branch_code(conn, value)
            if code:
                parts.append("branch={}".format(code))
        elif key in ("patient_phone", "phone"):
            parts.append("phone heard {}".format(tail4(value) or "(incomplete)"))
        elif key == "appt_date":
            parts.append("date={}".format(value))
        elif key == "start_time":
            parts.append("time={}".format(value))
        elif key in ("patient_name", "name", "staff_name"):
            parts.append("patient={}".format(_short(value, 60)) if key != "staff_name" else "staff={}".format(_short(value, 60)))
        elif key == "doctor":
            parts.append("doctor={}".format(_short(value, 60)))
        else:
            parts.append("{}={}".format(key, _short(value, 40)))
    return ", ".join(parts) if parts else "nothing yet"


def _missing(slots, intent):
    need = _REQUIRED.get(intent, ())
    slots = slots or {}
    out = []
    for key in need:
        if slots.get(key) in (None, ""):
            if key == "patient_name" and (slots.get("patient_id") or slots.get("patient_phone") or slots.get("appointment_id")):
                continue
            out.append({"patient_name": "patient", "appt_date": "date", "start_time": "time", "name": "patient name",
                        "staff_name": "staff"}.get(key, key))
    return out


def _task_lines(ctx, conn):
    pending = ctx.pending
    if not pending or pending.get("intent") in (None, "clarify"):
        return []
    intent = pending["intent"]
    slots = pending.get("slots") or {}
    line = "Task in progress: {}. Heard: {}.".format(intent, _slot_text(conn, slots, intent))
    missing = _missing(slots, intent)
    if pending.get("kind") in ("phone",) and "phone" not in missing:
        missing.append("phone")
    if missing:
        line += " Missing: {}.".format(", ".join(missing))
    skipped = sorted(pending.get("skipped") or ())
    if skipped:
        line += " The user chose to skip: {}.".format(", ".join(skipped))
    return [line]


def _pending_lines(ctx, conn, max_options):
    pending = ctx.pending
    if not pending:
        return []
    kind = pending["kind"]
    if kind == "clarify":
        slots = pending.get("slots") or {}
        return ["Waiting for the user's answer to your question: \"{}\" (they first said: \"{}\"). Answer by calling the "
                "tool for the WHOLE command: what they first said plus this answer.".format(
                    _short(slots.get("question"), 120), _short(slots.get("text"), 120))]
    from clinic.voice_context import pending_question         # not at the top: voice_context imports a lot
    lines = ["Waiting for the user's answer to: \"{}\" ({}).".format(_short(pending_question(pending, "en-IN"), 160), kind)]
    if kind == "book_slot":
        lines[0] += " This is a booking OFFER: a yes only produces the review card, a person still presses Approve."
    options = pending.get("options") or []
    if options:
        lines[0] += " Options (answer with choose_option and the number):"
        for number, option in enumerate(options[:max_options], 1):
            lines.append("  {}. {}".format(number, person_label(option.get("label"))))
        if len(options) > max_options:
            lines.append("  (+{} more options not shown)".format(len(options) - max_options))
    return lines


def _card_lines(ctx, conn):
    card = ctx.open_card
    if not card:
        return []
    return ["Card on screen: {} ({}). Nothing is saved until a person presses Approve; the user can change its fields by "
            "voice (correct_card).".format(card["intent"], _slot_text(conn, card.get("slots"), card["intent"]))]


def _remembered_lines(ctx, conn):
    patient = ctx.patient
    if not patient:
        return []
    tail = ""
    if conn is not None and patient.get("id") is not None:
        row = conn.execute("SELECT phone FROM patients WHERE id = ?", (patient["id"],)).fetchone()
        tail = tail4(row["phone"]) if row else ""
    return ["Remembered patient (the last one discussed; use only if the sentence refers back to them, "
            "such as him / her / that patient, and names nobody else): {}.".format(
                "{} {}".format(_short(patient["name"], 60), tail).strip())]


def _list_lines(ctx, conn, max_rows):
    rows = ctx.list_rows
    if not rows:
        return []
    lines = ["List on screen ({}):".format(_short(ctx.list_scope or "appointments", 70))]
    for number, row in enumerate(rows[:max_rows], 1):
        who = row.get("patient_name") or row.get("who") or row.get("patient") or "?"
        lines.append("  {}. {}, {} {}".format(number, _short(who, 40), row.get("appt_date") or "", row.get("start_time") or "").rstrip())
    if len(rows) > max_rows:
        lines.append("  (+{} more rows not shown)".format(len(rows) - max_rows))
    return lines


def _turn_lines(ctx, count):
    turns = list(getattr(ctx, "turns", None) or ([ctx.last_turn] if ctx.last_turn else []))[-count:]
    if not turns:
        return []
    lines = ["Recent turns (oldest first):"]
    for turn in turns:
        lines.append("  - user said \"{}\"; you called {}; result: {}".format(
            _short(turn.get("text"), 100), _short(turn.get("call"), 90), _short(turn.get("result"), MAX_TURN_CHARS)))
    return lines


def _header(ctx, conn, today):
    lines = ["STATE CARD (names and last four phone digits only; no ids)",
             "Today: {} {}.".format(today.strftime("%A"), today.isoformat())]
    if conn is not None and len(branches.list_branches(conn)) > 1:
        mine = _branch_code(conn, ctx.my_branch)
        named = _branch_code(conn, ctx.branch)
        text = "Branch: {} (this computer's branch)".format(mine) if mine else "Branch: none chosen on this computer"
        if named:
            text += "; the user last named branch {}".format(named)
        lines.append(text + ".")
    return lines


def build(ctx, conn, today=None, drop=None, max_chars=MAX_CHARS):
    """The state card as text. `drop` is a collection of section names to leave out (default: the run's
    ablation, usually none). Never raises for a missing piece: a section it cannot read is left out."""
    today = today or date.today()
    drop = frozenset(drop if drop is not None else dropped.get())
    unknown = drop - set(SECTIONS)
    if unknown:
        raise ValueError("unknown state card section: {}".format(", ".join(sorted(unknown))))

    def sections(max_options, max_rows, turns):
        out = _header(ctx, conn, today)
        for name, make in (("task", lambda: _task_lines(ctx, conn)),
                           ("pending", lambda: _pending_lines(ctx, conn, max_options)),
                           ("card", lambda: _card_lines(ctx, conn)),
                           ("remembered", lambda: _remembered_lines(ctx, conn)),
                           ("list", lambda: _list_lines(ctx, conn, max_rows)),
                           ("last_turns", lambda: _turn_lines(ctx, turns))):
            if name in drop:
                continue
            try:
                out += make()
            except Exception:
                continue
        if len(out) == len(_header(ctx, conn, today)):
            out.append("Nothing is open: no question is waiting, no card is on screen, nobody is remembered.")
        return "\n".join(out)

    text = ""
    for max_options, max_rows, turns in ((MAX_OPTIONS, MAX_LIST_ROWS, 3), (6, 5, 2), (4, 3, 1), (3, 2, 1)):
        text = sections(max_options, max_rows, turns)
        if len(text) <= max_chars:
            return text
    return text[:max_chars - 40].rstrip() + "\n(state card cut to fit the size limit)"


def estimate_tokens(text):
    """A rough token count (3 characters per token: safe for Devanagari-heavy text)."""
    return (len(text) + 2) // 3
