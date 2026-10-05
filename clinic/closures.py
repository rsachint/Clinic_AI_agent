"""Closing a branch (or one doctor at it) for a stretch of time, and moving the
patients who were booked in that stretch -- as ONE reviewed batch.

  plan()     what a closure would touch, and where each patient could go
             (nothing is written);
  apply()    stops new bookings (a booking block), then moves / cancels every
             appointment the person chose, each through the normal audited
             propose -> confirm path, and tells each patient on WhatsApp;
  undo()     puts the whole batch back (moves back, cancellations restored,
             the block removed), skipping anything the patient has changed
             since;
  accept() / changed()   the patient's answer to the WhatsApp notice.

Where a patient is proposed to go: the same date and time at the nearest other
open branch if that slot is free; otherwise the free time on that date closest
to the original (within CLOSE_ENOUGH_MINUTES). When nothing fits, the default is
"leave this one alone" (never a silent cancel) and the options list offers
other branches and the next few days. A person can change every row before
applying.
"""

import logging
from datetime import date, datetime, timedelta

from clinic import (auto_actions, booking_blocks, branches, closure_notify, core, patient_activity, scheduling)

_logger = logging.getLogger(__name__)

CLOSE_ENOUGH_MINUTES = 120     # a same-day substitute further from the original time than this is only an option
MAX_OPTIONS = 14               # alternatives listed per patient
LATER_DAYS = 3                 # how many following days to offer one option for
ACTIONS = ("move", "cancel", "leave")
SOURCE_PREFIX = "[staff:closure]"


class ClosureError(ValueError):
    """Bad input for a closure; the message is meant for staff."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now(now):
    return now or datetime.now()


def _scope(conn, branch_id, doctor_id):
    if branch_id in (None, ""):
        raise ClosureError("Choose the branch that is closing.")
    try:
        branch_id = int(branch_id)
    except (TypeError, ValueError):
        raise ClosureError("Choose the branch that is closing.")
    if branches.get_branch(conn, branch_id) is None:
        raise ClosureError("That branch does not exist.")
    if doctor_id in (None, ""):
        return branch_id, None
    doctor_id = int(doctor_id)
    if branches.get_doctor(conn, doctor_id) is None:
        raise ClosureError("That doctor does not exist.")
    return branch_id, doctor_id


def _details(conn, appointment_ids):
    """id -> the appointment's row with patient and branch names."""
    if not appointment_ids:
        return {}
    marks = ",".join("?" for _ in appointment_ids)
    default = branches.default_branch_id(conn)
    rows = conn.execute(
        "SELECT a.id, a.patient_id, a.appt_date, a.start_time, a.duration_minutes, a.status, a.queue_state, "
        "a.doctor_id, COALESCE(a.branch_id, ?) AS branch_id, COALESCE(p.name, a.patient_name) AS name, "
        "COALESCE(p.phone, a.patient_phone) AS phone "
        "FROM appointments a LEFT JOIN patients p ON p.id = a.patient_id WHERE a.id IN ({})".format(marks),
        [default] + list(appointment_ids)).fetchall()
    return {r["id"]: r for r in rows}


def _started(row, now):
    return (row["appt_date"], row["start_time"]) < (now.date().isoformat(), now.strftime("%H:%M"))


def affected(conn, branch_id, start_date, end_date, start_time=None, end_time=None, doctor_id=None, now=None):
    """The appointments a closure would touch, in time order: booked / confirmed,
    inside the window, not started, not already checked in."""
    now = _now(now)
    ids = [a["id"] for a in booking_blocks.appointments_in_window(
        conn, start_date, end_date, start_time, end_time, branch_id, doctor_id)]
    rows = _details(conn, ids)
    return [rows[i] for i in ids if i in rows and not _started(rows[i], now) and not rows[i]["queue_state"]]


def _option(conn, branch, iso, hhmm):
    doctor = branches.doctor_label(conn, branches.doctor_at(conn, branch["id"], iso, hhmm))
    return {"branch_id": branch["id"], "branch": branch["name"], "date": iso, "time": hhmm, "doctor": doctor,
            "label": "{} · {} {} · {}".format(branch["name"], date.fromisoformat(iso).strftime("%a"),
                                                       date.fromisoformat(iso).strftime("%-d %b"), hhmm)}


def _slot_ok(conn, branch_id, iso, hhmm, duration, taken, now):
    if (branch_id, iso, hhmm) in taken:
        return False
    if (iso, hhmm) <= (now.date().isoformat(), now.strftime("%H:%M")):
        return False
    if scheduling.within_doctor_hours(conn, iso, hhmm, duration, branch_id) is None:
        return False
    return scheduling.is_slot_free(conn, iso, hhmm, duration, branch_id=branch_id)


def _candidates(conn, row, targets, taken, now):
    """Every free alternative for one appointment, best first: nearest branch
    first, then closest in time, on the same date; then one option per branch
    for each of the next LATER_DAYS days (closest in time)."""
    duration = row["duration_minutes"] or scheduling.SLOT_MINUTES
    want = scheduling._to_minutes(row["start_time"])
    options = []
    same_day = []
    for rank, branch in enumerate(targets):
        for hhmm in scheduling.slot_grid(conn, row["appt_date"], branch["id"]):
            if _slot_ok(conn, branch["id"], row["appt_date"], hhmm, duration, taken, now):
                same_day.append((rank, abs(scheduling._to_minutes(hhmm) - want), hhmm, branch))
    same_day.sort(key=lambda c: (c[1] > CLOSE_ENOUGH_MINUTES, c[0], c[1], c[2]))
    seen_per_branch = {}
    for rank, gap, hhmm, branch in same_day:
        if seen_per_branch.get(branch["id"], 0) >= 3 and len(options) >= 6:
            continue
        seen_per_branch[branch["id"]] = seen_per_branch.get(branch["id"], 0) + 1
        options.append((gap, _option(conn, branch, row["appt_date"], hhmm)))
    start = date.fromisoformat(row["appt_date"])
    for offset in range(1, LATER_DAYS + 1):
        iso = (start + timedelta(days=offset)).isoformat()
        for branch in targets:
            best = None
            for hhmm in scheduling.slot_grid(conn, iso, branch["id"]):
                if _slot_ok(conn, branch["id"], iso, hhmm, duration, taken, now):
                    gap = abs(scheduling._to_minutes(hhmm) - want)
                    if best is None or gap < best[0]:
                        best = (gap, hhmm)
            if best:
                options.append((10_000 + offset, _option(conn, branch, iso, best[1])))
    return options[:MAX_OPTIONS]


def plan(conn, branch_id, start_date, end_date, start_time=None, end_time=None, doctor_id=None, now=None):
    """The proposed batch. Writes nothing. Returns
    {'scope': {...}, 'moves': [ {appointment_id, name, phone, from, action, to, options, note} ], 'counts': {...}}."""
    now = _now(now)
    branch_id, doctor_id = _scope(conn, branch_id, doctor_id)
    try:
        start_date, end_date, start_time, end_time, _ = booking_blocks.normalize(start_date, end_date, start_time, end_time)
    except booking_blocks.BlockError as exc:
        raise ClosureError(str(exc))
    closing = branches.get_branch(conn, branch_id)
    targets = [b for b in branches.nearest_branches(conn, closing.get("pin_code"), include_closed=False)
               if b["id"] != branch_id]
    rows = affected(conn, branch_id, start_date, end_date, start_time, end_time, doctor_id, now)
    taken = set()
    moves = []
    for row in rows:
        duration = row["duration_minutes"] or scheduling.SLOT_MINUTES
        options = _candidates(conn, row, targets, taken, now)
        choice = None
        for gap, option in options:
            if option["date"] == row["appt_date"] and gap <= CLOSE_ENOUGH_MINUTES:
                choice = option
                break
        note = None
        if choice:
            taken.add((choice["branch_id"], choice["date"], choice["time"]))
            if choice["time"] != row["start_time"]:
                note = "Closest free time at {} ({} instead of {}).".format(choice["branch"], choice["time"], row["start_time"])
        else:
            note = ("No free time within {} hours at another branch that day. Pick another day, cancel, or leave it."
                    .format(CLOSE_ENOUGH_MINUTES // 60)) if targets else "There is no other open branch to move this patient to."
        moves.append({
            "appointment_id": row["id"], "name": row["name"], "phone": row["phone"], "duration": duration,
            "from": {"branch_id": row["branch_id"], "branch": branches.branch_label(conn, row["branch_id"]),
                     "date": row["appt_date"], "time": row["start_time"]},
            "action": "move" if choice else "leave", "to": choice,
            "options": [option for _, option in options], "note": note,
        })
    return {
        "scope": {"branch_id": branch_id, "branch": closing["name"], "doctor_id": doctor_id,
                  "doctor": branches.doctor_label(conn, doctor_id) if doctor_id else None,
                  "start_date": start_date, "end_date": end_date, "start_time": start_time, "end_time": end_time},
        "moves": moves,
        "counts": {"total": len(moves), "movable": sum(1 for m in moves if m["to"]),
                   "unresolved": sum(1 for m in moves if not m["to"])},
    }


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

def _friendly(exc):
    if isinstance(exc, scheduling.SlotBlockedError):
        return "that time is blocked"
    if isinstance(exc, scheduling.SlotConflictError):
        return "that time was just taken"
    return str(exc) or type(exc).__name__


def _clean_moves(conn, moves, allowed):
    """The requested moves, de-duplicated and limited to appointments that are part of this closure."""
    seen, out = set(), []
    for move in moves or []:
        try:
            appointment_id = int(move.get("appointment_id"))
        except (TypeError, ValueError, AttributeError):
            continue
        action = move.get("action")
        if appointment_id in seen or appointment_id not in allowed or action not in ACTIONS:
            continue
        seen.add(appointment_id)
        out.append({
            "appointment_id": appointment_id, "action": action,
            "to_branch_id": move.get("to_branch_id"), "to_date": move.get("to_date"), "to_time": move.get("to_time"),
        })
    return out


def apply(conn, *, branch_id, start_date, end_date, moves, handlers, after_commit, now, start_time=None,
          end_time=None, doctor_id=None, reason=None, message=None):
    """Stop new bookings in the window, then carry out the chosen moves. Returns
    {'ok', 'closure_id', 'results': [...], 'counts': {...}}. One patient failing
    never stops the others; the failure is listed so a person can handle it."""
    now = _now(now)
    branch_id, doctor_id = _scope(conn, branch_id, doctor_id)
    reason = (reason or "").strip()
    message = (message or "").strip()
    if len(message) > 300:
        raise ClosureError("Keep the message to patients under 300 characters.")
    rows = {r["id"]: r for r in affected(conn, branch_id, start_date, end_date, start_time, end_time, doctor_id, now)}
    chosen = _clean_moves(conn, moves, set(rows))
    with auto_actions.COMMIT_LOCK:
        try:
            block = booking_blocks.add_block(conn, start_date, end_date, start_time, end_time, reason=reason,
                                             now=now, branch_id=branch_id, doctor_id=doctor_id)
        except booking_blocks.BlockError as exc:
            raise ClosureError(str(exc))
        cur = conn.execute(
            "INSERT INTO closures (branch_id, doctor_id, start_date, end_date, start_time, end_time, reason, message, block_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (branch_id, doctor_id, block["start_date"], block["end_date"], block["start_time"], block["end_time"],
             reason, message, block["id"]))
        closure_id = cur.lastrowid
        conn.commit()
        results = []
        for move in chosen:
            if move["action"] == "leave":
                continue
            results.append(_carry_out(conn, closure_id, rows[move["appointment_id"]], move, handlers, after_commit, now))
    left = [rows[i] for i in rows if i not in {m["appointment_id"] for m in chosen if m["action"] != "leave"}]
    counts = {
        "moved": sum(1 for r in results if r["action"] == "move" and r["result"] == "done"),
        "cancelled": sum(1 for r in results if r["action"] == "cancel" and r["result"] == "done"),
        "failed": sum(1 for r in results if r["result"] == "failed"),
        "left": len(left),
    }
    return {"ok": True, "closure_id": closure_id, "block_id": block["id"], "results": results, "counts": counts,
            "left": [{"appointment_id": r["id"], "name": r["name"], "date": r["appt_date"], "time": r["start_time"]}
                     for r in left]}


def _carry_out(conn, closure_id, row, move, handlers, after_commit, now):
    """One appointment of the batch. Never raises."""
    appointment_id = row["id"]
    base = {"appointment_id": appointment_id, "name": row["name"], "action": move["action"],
            "from_date": row["appt_date"], "from_time": row["start_time"]}
    if move["action"] == "move":
        try:
            target = (int(move["to_branch_id"]), str(move["to_date"]), str(move["to_time"]))
            scheduling._to_minutes(target[2])
            date.fromisoformat(target[1])
        except (TypeError, ValueError):
            return _record(conn, closure_id, row, move, "failed", "choose a new branch, day and time", base)
        intent = "reschedule_appointment"
        slots = {"appointment_id": appointment_id, "appt_date": target[1], "start_time": target[2],
                 "branch_id": target[0], "require_active": True}
    else:
        intent = "cancel_appointment"
        slots = {"appointment_id": appointment_id, "by_clinic": True, "require_active": True}
    proposal_id = core.propose(conn, intent, slots, source_text="{} closure #{}".format(SOURCE_PREFIX, closure_id))
    try:
        _, entity_id = core.confirm(conn, proposal_id, handlers)
    except Exception as exc:
        auto_actions._reject_quietly(conn, proposal_id)
        return _record(conn, closure_id, row, move, "failed", _friendly(exc), base)
    result = _record(conn, closure_id, row, move, "done", None, base, proposal_id=proposal_id)
    patient_activity.log(
        conn, event="closure_moved" if move["action"] == "move" else "closure_cancelled", source="staff",
        patient_id=row["patient_id"], patient_name=row["name"], appointment_id=appointment_id, proposal_id=proposal_id,
        detail="{} {} {}".format("Moved out of the closure:" if move["action"] == "move" else "Cancelled for the closure:",
                                 row["appt_date"], row["start_time"]),
        meta={"closure_id": closure_id, "move_id": result["move_id"]}, now=now)
    quiet = dict(slots, quiet=True)          # the closure notice below replaces the usual "moved" / "cancelled" message
    auto_actions._safe_hooks(after_commit, conn, intent, quiet, entity_id, None)
    try:
        closure_notify.notify_move(conn, result["move_id"], now)
    except Exception:
        _logger.exception("could not queue the closure notice for appointment %s", appointment_id)
    return result


def _record(conn, closure_id, row, move, result, error, base, proposal_id=None):
    to = (move.get("to_branch_id"), move.get("to_date"), move.get("to_time")) if move["action"] == "move" else (None, None, None)
    cur = conn.execute(
        "INSERT INTO closure_moves (closure_id, appointment_id, action, from_branch_id, from_date, from_time, "
        "to_branch_id, to_date, to_time, result, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (closure_id, row["id"], move["action"], row["branch_id"], row["appt_date"], row["start_time"],
         to[0], to[1], to[2], result, error))
    conn.commit()
    return dict(base, move_id=cur.lastrowid, result=result, error=error, proposal_id=proposal_id,
                to_branch_id=to[0], to_date=to[1], to_time=to[2])


# ---------------------------------------------------------------------------
# Reading, undoing, and the patient's answer
# ---------------------------------------------------------------------------

def list_closures(conn, limit=20):
    out = []
    for c in conn.execute("SELECT * FROM closures ORDER BY id DESC LIMIT ?", (limit,)).fetchall():
        moves = conn.execute(
            "SELECT m.*, COALESCE(p.name, a.patient_name) AS name FROM closure_moves m "
            "JOIN appointments a ON a.id = m.appointment_id LEFT JOIN patients p ON p.id = a.patient_id "
            "WHERE m.closure_id = ? ORDER BY m.from_date, m.from_time, m.id", (c["id"],)).fetchall()
        item = {k: c[k] for k in c.keys()}
        item["branch"] = branches.branch_label(conn, c["branch_id"])
        item["doctor"] = branches.doctor_label(conn, c["doctor_id"]) if c["doctor_id"] else None
        item["moves"] = [dict(m, to_branch=(branches.branch_label(conn, m["to_branch_id"]) if m["to_branch_id"] else None),
                              from_branch=branches.branch_label(conn, m["from_branch_id"])) for m in moves]
        done = [m for m in moves if m["result"] == "done"]
        item["counts"] = {
            "moved": sum(1 for m in done if m["action"] == "move"), "cancelled": sum(1 for m in done if m["action"] == "cancel"),
            "failed": sum(1 for m in moves if m["result"] == "failed"),
            "accepted": sum(1 for m in done if m["response"] == "accepted"),
            "changed": sum(1 for m in done if m["response"] == "changed"),
            "waiting": sum(1 for m in done if m["action"] == "move" and m["response"] == "none"),
            "undone": sum(1 for m in moves if m["result"] == "undone"),
            "skipped": sum(1 for m in moves if m["result"] == "skipped"),
        }
        out.append(item)
    return out


def _unchanged_since(conn, move):
    """The appointment is still exactly where the closure put it (the patient has
    not moved or cancelled it since), so it is safe to put back."""
    row = conn.execute("SELECT status, appt_date, start_time, branch_id FROM appointments WHERE id = ?",
                       (move["appointment_id"],)).fetchone()
    if row is None:
        return False, "the appointment no longer exists"
    if move["action"] == "cancel":
        return (row["status"] == "cancelled"), "the appointment is {} now".format(row["status"])
    here = (row["status"] in ("booked", "confirmed") and row["appt_date"] == move["to_date"]
            and row["start_time"] == move["to_time"] and branches.resolve(conn, row["branch_id"]) == move["to_branch_id"])
    return here, "the patient has changed it since"


def undo(conn, closure_id, handlers, after_commit, now):
    """Put a whole closure back. Returns {'ok', 'restored', 'skipped': [{name, why}]}."""
    now = _now(now)
    with auto_actions.COMMIT_LOCK:
        closure = conn.execute("SELECT * FROM closures WHERE id = ?", (closure_id,)).fetchone()
        if closure is None:
            return {"ok": False, "error": "That closure was not found."}
        if closure["status"] != "applied":
            return {"ok": False, "error": "That closure is already undone."}
        claimed = conn.execute("UPDATE closures SET status = 'undone', undone_at = ? WHERE id = ? AND status = 'applied'",
                               (now.strftime("%Y-%m-%d %H:%M:%S"), closure_id)).rowcount
        conn.commit()
        if not claimed:
            return {"ok": False, "error": "That closure is already undone."}
        if closure["block_id"]:
            booking_blocks.remove_block(conn, closure["block_id"])      # first: the old slots must be free to go back to
        restored, skipped = 0, []
        for move in conn.execute("SELECT * FROM closure_moves WHERE closure_id = ? AND result = 'done' ORDER BY id",
                                 (closure_id,)).fetchall():
            ok, why = _unchanged_since(conn, move)
            name = conn.execute("SELECT COALESCE(p.name, a.patient_name) FROM appointments a LEFT JOIN patients p "
                                "ON p.id = a.patient_id WHERE a.id = ?", (move["appointment_id"],)).fetchone()
            name = name[0] if name else "appointment #{}".format(move["appointment_id"])
            if not ok:
                conn.execute("UPDATE closure_moves SET result = 'skipped', error = ? WHERE id = ?", (why, move["id"]))
                conn.commit()
                skipped.append({"name": name, "why": why})
                continue
            if move["action"] == "cancel":
                intent, slots = "restore_appointment", {"appointment_id": move["appointment_id"], "status": "booked"}
            else:
                intent = "reschedule_appointment"
                slots = {"appointment_id": move["appointment_id"], "appt_date": move["from_date"],
                         "start_time": move["from_time"], "branch_id": move["from_branch_id"], "require_active": True,
                         "restore": True}
            proposal_id = core.propose(conn, intent, slots, source_text="{} undo of closure #{}".format(SOURCE_PREFIX, closure_id))
            try:
                _, entity_id = core.confirm(conn, proposal_id, handlers)
            except Exception as exc:
                auto_actions._reject_quietly(conn, proposal_id)
                conn.execute("UPDATE closure_moves SET result = 'skipped', error = ? WHERE id = ?", (_friendly(exc), move["id"]))
                conn.commit()
                skipped.append({"name": name, "why": _friendly(exc)})
                continue
            conn.execute("UPDATE closure_moves SET result = 'undone' WHERE id = ?", (move["id"],))
            conn.commit()
            restored += 1
            patient_activity.log(
                conn, event="closure_undone", source="staff", patient_name=name, appointment_id=move["appointment_id"],
                proposal_id=proposal_id, detail="Put back after the closure was undone",
                meta={"closure_id": closure_id, "move_id": move["id"]}, now=now)
            auto_actions._safe_hooks(after_commit, conn, intent, slots, entity_id, None)
    return {"ok": True, "restored": restored, "skipped": skipped}


def _owner_move(conn, move_id, wa_id):
    """The 'done' move row, only when its appointment belongs to the sender."""
    from clinic.entity_resolution import last10_digits
    move = conn.execute("SELECT * FROM closure_moves WHERE id = ? AND result = 'done'", (move_id,)).fetchone()
    if move is None:
        return None
    row = conn.execute("SELECT COALESCE(p.phone, a.patient_phone) AS phone FROM appointments a "
                       "LEFT JOIN patients p ON p.id = a.patient_id WHERE a.id = ?", (move["appointment_id"],)).fetchone()
    mine = last10_digits(wa_id or "")
    if row is None or len(mine) != 10 or last10_digits(row["phone"] or "") != mine:
        return None
    return move


def respond(conn, move_id, wa_id, response, now=None):
    """The patient answered the notice: 'accepted' or 'changed' (they chose to pick
    another). Returns the move row, or None when it is not theirs / not applicable."""
    if response not in ("accepted", "changed"):
        return None
    move = _owner_move(conn, move_id, wa_id)
    if move is None or move["action"] != "move":
        return None
    conn.execute("UPDATE closure_moves SET response = ?, responded_at = ? WHERE id = ?",
                 (response, _now(now).strftime("%Y-%m-%d %H:%M:%S"), move_id))
    conn.commit()
    return conn.execute("SELECT * FROM closure_moves WHERE id = ?", (move_id,)).fetchone()


def pending_for_sender(conn, wa_id):
    """The newest closure move this sender has not yet answered (for a typed "ok")."""
    from clinic.entity_resolution import last10_digits
    mine = last10_digits(wa_id or "")
    if len(mine) != 10:
        return None
    for move in conn.execute("SELECT m.id FROM closure_moves m JOIN closures c ON c.id = m.closure_id "
                             "WHERE m.result = 'done' AND m.action = 'move' AND m.response = 'none' AND c.status = 'applied' "
                             "ORDER BY m.id DESC LIMIT 50").fetchall():
        if _owner_move(conn, move["id"], wa_id) is not None:
            return conn.execute("SELECT * FROM closure_moves WHERE id = ?", (move["id"],)).fetchone()
    return None
