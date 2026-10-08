"""Follow-up visits that hold a real calendar slot and send the patient two
WhatsApp reminders.

A follow-up = the doctor has seen the patient and advised a return visit at a
date and time. Creating one books that slot in the calendar straight away (the
normal audited book_appointment path: doctor hours, double-booking, booking
blocks, tokens, notifications) and links it: `followups.appointment_id`. The
older voice / WhatsApp-staff `set_followup` (a bare "after N days", no time)
is untouched: such a follow-up has NO slot and NO reminders.

  plan()   what a batch of follow-ups would do, row by row (writes nothing);
  apply()  books every valid row (one row failing never stops the others) and
           records the follow-up; undo_batch() takes the whole batch back;
  reminders: two per follow-up (see reminder_times), kept in `followup_reminders`
           and handed to the notifications outbox when they are due -- see
           process_due(), which the scheduler calls every tick;
  sync_appointment() keeps a follow-up in line with its appointment when staff
           (or the patient) move, cancel or complete it by any existing path;
  patient_action() runs the patient's "Already visited" / "Cancel" button.

The reminders and everything else a patient sees NEVER carry the follow-up's
`diagnosis` (internal, staff-editable): see clinic/followup_notify.py.

Times are the clinic's local wall clock (IST), like every reminder in
clinic/notify.py. Nothing here talks to WhatsApp: it only inserts outbox rows;
notify.flush() delivers them (inside the 24-hour window as a normal message
with buttons, outside it as an approved template, else it waits as blocked).
"""

import json
import logging
import re
from datetime import date, datetime, time as dtime, timedelta

from clinic import (auto_actions, booking_phone, branches, core, followup_notify, notify, patient_activity, scheduling, settings)
from clinic.entity_resolution import last10_digits
from clinic.whatsapp import phone_to_wa_id

_logger = logging.getLogger(__name__)

KINDS = followup_notify.KINDS
EVENT = followup_notify.EVENT
MAX_BATCH_ROWS = 50
MAX_DIAGNOSIS = 500
HORIZON_DAYS = 366            # how far ahead a follow-up may be booked
SUGGEST_DAYS = 30             # how far to look for the next open day
SOURCE_PREFIX = "[staff:followups]"
ACTIVE_APPOINTMENT = ("booked", "confirmed")
KIND_LABEL = {"2d": "Early reminder", "4h": "Reminder before the visit"}
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_STAMP = "%Y-%m-%d %H:%M"


class FollowupError(ValueError):
    """Bad input for a follow-up; the message is meant for staff."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _as_now(now):
    """notify wants its two-clock Now; accept a plain local datetime too (the
    clinic's clock is IST, 5h30 ahead of UTC)."""
    if now is None:
        return notify.Now.real()
    if isinstance(now, notify.Now):
        return now
    return notify.Now(now, now - timedelta(hours=5, minutes=30))


def _int(value):
    try:
        return int(value) if value not in (None, "") and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def _date(text):
    try:
        return date.fromisoformat(text) if isinstance(text, str) and len(text) == 10 else None
    except ValueError:
        return None


def _hhmm(text):
    hours, minutes = text.split(":")
    return dtime(int(hours), int(minutes))


def slot_start(due_date, due_time):
    return datetime.combine(date.fromisoformat(due_date), _hhmm(due_time))


def _minutes(hhmm):
    return scheduling._to_minutes(hhmm)


def _day_label(iso):
    d = date.fromisoformat(iso)
    return "{} {} {}".format(notify._EN_DAYS[d.weekday()][:3], d.day, notify._EN_MONTHS[d.month - 1])


def _when_label(iso, hhmm):
    return "{} {}".format(_day_label(iso), hhmm)


# ---------------------------------------------------------------------------
# When the two reminders go out
# ---------------------------------------------------------------------------

def reminder_times(due_date, due_time, cfg=None):
    """{'2d': datetime, '4h': datetime or None} -- the clinic's local time each
    reminder is due.

    '2d'  `days_before` days before the follow-up DATE, at `send_time` (default 2 days, 10:00).
    '4h'  `hours_before` hours before the slot starts (default 4), but never earlier
          than `earliest_send` that day (default 07:00): a 09:00 visit is reminded at
          07:00, not 05:00. None when even that is not before the slot starts (nothing
          is ever sent at or after the slot itself)."""
    cfg = cfg or {"days_before": int(settings.DEFAULTS[settings.FU_DAYS_BEFORE]),
                  "send_time": settings.DEFAULTS[settings.FU_SEND_TIME],
                  "hours_before": int(settings.DEFAULTS[settings.FU_HOURS_BEFORE]),
                  "earliest_send": settings.DEFAULTS[settings.FU_EARLIEST_SEND]}
    day = date.fromisoformat(due_date)
    start = slot_start(due_date, due_time)
    early = datetime.combine(day - timedelta(days=cfg["days_before"]), _hhmm(cfg["send_time"]))
    late = max(start - timedelta(hours=cfg["hours_before"]), datetime.combine(day, _hhmm(cfg["earliest_send"])))
    return {"2d": early, "4h": late if late < start else None}


def _stamp(dt):
    return dt.strftime(_STAMP)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def get_followup(conn, followup_id):
    row = conn.execute(
        "SELECT f.*, p.name AS patient_name, p.phone AS patient_phone, a.status AS appointment_status, "
        "a.queue_state AS appointment_queue_state FROM followups f JOIN patients p ON p.id = f.patient_id "
        "LEFT JOIN appointments a ON a.id = f.appointment_id WHERE f.id = ?", (followup_id,)).fetchone()
    return dict(row) if row else None


def is_slot_followup(fu):
    return bool(fu and fu.get("appointment_id") and fu.get("due_time"))


def _live(fu):
    """A follow-up that still has a visit coming: pending, with its slot held by
    an active appointment that nobody has checked in yet."""
    return (is_slot_followup(fu) and fu["status"] == "pending"
            and fu["appointment_status"] in ACTIVE_APPOINTMENT and not fu["appointment_queue_state"])


# ---------------------------------------------------------------------------
# Reminder rows (everything here is commit-free: callers decide when to commit)
# ---------------------------------------------------------------------------

def cancel_reminders(conn, followup_id, reason, except_slot=None):
    """Stop the reminders of a follow-up that are not out yet. A scheduled one is
    just marked cancelled; one already in the outbox but not delivered (pending /
    blocked / failed) is removed from it (the outbox is not an audit record); one
    already delivered stays as it is. `except_slot` = (date, time) keeps that
    slot's reminders (the follow-up moved there)."""
    rows = conn.execute(
        "SELECT * FROM followup_reminders WHERE followup_id = ? AND state IN ('scheduled', 'enqueued')",
        (followup_id,)).fetchall()
    for r in rows:
        if except_slot and (r["slot_date"], r["slot_time"]) == tuple(except_slot):
            continue
        if r["state"] == "enqueued":
            note = conn.execute("SELECT status FROM notifications WHERE id = ?", (r["notification_id"],)).fetchone()
            if note is not None and note["status"] in ("sent", "dry_run"):
                continue                       # it went out: nothing to take back
            if note is not None:
                conn.execute("DELETE FROM notifications WHERE id = ?", (r["notification_id"],))
        conn.execute("UPDATE followup_reminders SET state = 'cancelled', reason = ?, updated_at = datetime('now') "
                     "WHERE id = ?", (reason, r["id"]))


def ensure_reminders(conn, followup_id):
    """Make the reminder rows match the follow-up's CURRENT slot: rows for the
    new slot (under their own keys, so a moved visit is reminded again), the old
    slot's unsent ones cancelled, all of them cancelled once the follow-up is
    not live. Idempotent."""
    fu = get_followup(conn, followup_id)
    if fu is None:
        return
    if not _live(fu):
        cancel_reminders(conn, followup_id, "the follow-up is no longer active")
        return
    slot = (fu["due_date"], fu["due_time"])
    cancel_reminders(conn, followup_id, "the visit was moved", except_slot=slot)
    # Back on a slot it had before (a moved visit put back, a reinstated cancellation):
    # that slot's cancelled reminders are due again; the ones already sent stay sent.
    conn.execute("UPDATE followup_reminders SET state = 'scheduled', reason = NULL, updated_at = datetime('now') "
                 "WHERE followup_id = ? AND slot_date = ? AND slot_time = ? AND state = 'cancelled' AND due_at IS NOT NULL",
                 (followup_id,) + slot)
    times = reminder_times(slot[0], slot[1], settings.followup_reminder_settings(conn))
    for kind in KINDS:
        due = times[kind]
        conn.execute(
            "INSERT OR IGNORE INTO followup_reminders (followup_id, kind, slot_date, slot_time, due_at, state, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (followup_id, kind, slot[0], slot[1], _stamp(due) if due else None,
             "scheduled" if due else "skipped", None if due else "too close to the visit to send"))


def refresh_scheduled(conn):
    """Staff changed the reminder timing: recompute when every reminder that has
    not gone out yet is due."""
    cfg = settings.followup_reminder_settings(conn)
    for r in conn.execute("SELECT * FROM followup_reminders WHERE state = 'scheduled'").fetchall():
        due = reminder_times(r["slot_date"], r["slot_time"], cfg)[r["kind"]]
        if due is None:
            conn.execute("UPDATE followup_reminders SET state = 'skipped', reason = 'too close to the visit to send', "
                         "updated_at = datetime('now') WHERE id = ?", (r["id"],))
        else:
            conn.execute("UPDATE followup_reminders SET due_at = ?, updated_at = datetime('now') WHERE id = ?",
                         (_stamp(due), r["id"]))
    conn.commit()


def is_opted_out(conn, phone):
    digits = last10_digits(phone or "")
    if len(digits) < 10:
        return False
    return conn.execute("SELECT 1 FROM reminder_opt_outs WHERE phone10 = ?", (digits,)).fetchone() is not None


def set_opted_out(conn, phone, opted_out=True):
    """A patient wrote STOP (or START again). Their queued reminders are taken
    back at once; any later one is skipped when its time comes."""
    digits = last10_digits(phone or "")
    if len(digits) < 10:
        return False
    if opted_out:
        conn.execute("INSERT OR IGNORE INTO reminder_opt_outs (phone10) VALUES (?)", (digits,))
        for fu in conn.execute("SELECT f.id, p.phone FROM followups f JOIN patients p ON p.id = f.patient_id "
                               "WHERE f.status = 'pending' AND f.appointment_id IS NOT NULL").fetchall():
            if last10_digits(fu["phone"] or "") == digits:
                cancel_reminders(conn, fu["id"], "the patient asked to stop reminders")
    else:
        conn.execute("DELETE FROM reminder_opt_outs WHERE phone10 = ?", (digits,))
        for fu in conn.execute("SELECT f.id, p.phone FROM followups f JOIN patients p ON p.id = f.patient_id "
                               "WHERE f.status = 'pending' AND f.appointment_id IS NOT NULL").fetchall():
            if last10_digits(fu["phone"] or "") == digits:
                ensure_reminders(conn, fu["id"])
    conn.commit()
    return True


def _skip(conn, reminder_id, reason, state="skipped"):
    conn.execute("UPDATE followup_reminders SET state = ?, reason = ?, updated_at = datetime('now') WHERE id = ?",
                 (state, reason, reminder_id))


def process_due(conn, now=None):
    """Hand every reminder whose time has come to the outbox. Safe to call every
    tick (a reminder is enqueued once: its row changes state, and its outbox
    dedup key is per follow-up + slot + kind).

    Catch-up: a reminder whose time has passed while the visit is still ahead is
    sent now -- that is what makes a follow-up created inside the 2-day window,
    or a scheduler that was down, work with no special case. If BOTH are due,
    only the later one (4 hours) is sent and the earlier is marked superseded.
    Nothing is ever sent once the slot has started. Returns how many were enqueued."""
    now = _as_now(now)
    stamp = _stamp(now.local)
    rows = conn.execute(
        "SELECT r.id, r.followup_id, r.kind, r.slot_date, r.slot_time, r.due_at FROM followup_reminders r "
        "WHERE r.state = 'scheduled' AND r.due_at <= ? ORDER BY r.followup_id, r.due_at", (stamp,)).fetchall()
    by_followup = {}
    for r in rows:
        by_followup.setdefault(r["followup_id"], []).append(r)
    created = 0
    for followup_id, due in by_followup.items():
        try:
            created += _process_followup(conn, followup_id, due, now)
        except Exception:
            _logger.exception("could not process the reminders of follow-up %s", followup_id)
            try:
                conn.rollback()
            except Exception:
                pass
    return created


def _process_followup(conn, followup_id, due, now):
    fu = get_followup(conn, followup_id)
    current = (fu["due_date"], fu["due_time"]) if fu else None
    keep = []
    for r in due:
        if fu is None or (r["slot_date"], r["slot_time"]) != current:
            _skip(conn, r["id"], "the visit was moved", "cancelled")
        elif not _live(fu):
            _skip(conn, r["id"], "the follow-up is no longer active", "cancelled")
        elif now.local >= slot_start(r["slot_date"], r["slot_time"]):
            _skip(conn, r["id"], "the visit time had already started")
        else:
            keep.append(r)
    for r in keep[:-1]:
        _skip(conn, r["id"], "superseded by a later reminder")
    created = 0
    if keep:
        created = 1 if _enqueue_reminder(conn, fu, keep[-1], now) else 0
    conn.commit()
    return created


def _enqueue_reminder(conn, fu, r, now):
    """Compose one reminder and put it in the outbox. True when it was queued."""
    wa_id = phone_to_wa_id(fu["patient_phone"])
    if is_opted_out(conn, fu["patient_phone"]):
        _skip(conn, r["id"], "the patient asked to stop reminders")
        return False
    if wa_id is None:
        _skip(conn, r["id"], "no usable phone number")
        return False
    language = notify.patient_language(conn, wa_id)
    ctx = followup_notify.context(conn, fu["id"])
    text, interactive, template = followup_notify.compose(conn, ctx, r["kind"], language)
    key = "{}:{}:{}@{}".format(EVENT[r["kind"]], fu["id"], r["slot_date"], r["slot_time"])
    note_id = notify.enqueue(
        conn, event=EVENT[r["kind"]], dedup_key=key, body=text, wa_id=wa_id, appointment_id=fu["appointment_id"],
        language=language, now=now, interactive=interactive, template=template)
    if note_id is None:           # already queued (a crash between the two writes): adopt that row
        existing = conn.execute("SELECT id FROM notifications WHERE dedup_key = ?", (key,)).fetchone()
        note_id = existing["id"] if existing else None
    conn.execute("UPDATE followup_reminders SET state = 'enqueued', notification_id = ?, reason = NULL, "
                 "updated_at = datetime('now') WHERE id = ?", (note_id, r["id"]))
    return True


def tick(conn, now=None):
    """The scheduler's follow-up step: heal any follow-up that drifted from its
    appointment, then enqueue what is due. Returns the number enqueued."""
    now = _as_now(now)
    reconcile(conn, now)
    return process_due(conn, now)


def reconcile(conn, now=None):
    """Re-sync every pending slot follow-up that is still ahead (or was due
    yesterday) with its appointment. Normally the post-commit hook already did;
    this catches a write that bypassed it (or a crash between the write and the
    hook). Older ones are left alone so the pass stays cheap as history grows."""
    now = _as_now(now)
    since = (now.local.date() - timedelta(days=1)).isoformat()
    for row in conn.execute("SELECT appointment_id FROM followups WHERE status = 'pending' "
                            "AND appointment_id IS NOT NULL AND due_date >= ?", (since,)).fetchall():
        try:
            sync_appointment(conn, row["appointment_id"], now)
        except Exception:
            _logger.exception("could not sync the follow-up of appointment %s", row["appointment_id"])
            try:
                conn.rollback()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Writing: every change is a proposal + confirm, so audit_log records it
# ---------------------------------------------------------------------------

def _run(conn, intent, slots, handler, source=SOURCE_PREFIX):
    """core.propose + core.confirm for one follow-up write (so it lands in the
    immutable audit_log). `handler(conn, slots)` -> (entity_type, entity_id, payload)."""
    proposal_id = core.propose(conn, intent, slots, source_text="{} {}".format(source, intent))
    try:
        entity_type, entity_id = core.confirm(conn, proposal_id, {intent: handler})
    except BaseException:
        try:
            core.reject(conn, proposal_id)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    return proposal_id, entity_id


def _audit_extra(conn, proposal_id, intent, entity_type, entity_id, payload):
    """A second audit row for the same proposal (e.g. the appointment a follow-up
    action also cancelled)."""
    conn.execute("INSERT INTO audit_log (proposal_id, intent, entity_type, entity_id, payload_json) VALUES (?, ?, ?, ?, ?)",
                 (proposal_id, intent, entity_type, entity_id, json.dumps(payload, ensure_ascii=False)))
    conn.commit()


def sync_appointment(conn, appointment_id, now=None, reinstate=False):
    """Bring the follow-up linked to this appointment in line with it:

      booked / confirmed  -> the follow-up takes its date, time, branch and doctor, and its
                             reminders are recomputed (the old slot's unsent ones cancelled);
      completed           -> the follow-up is done, reminders stop;
      cancelled           -> the follow-up is cancelled, reminders stop;
      no_show             -> the follow-up stays pending (it shows under Missed follow-ups),
                             reminders stop.

    `reinstate` (an Undo put a cancelled appointment back) also brings a cancelled
    follow-up back. Returns the follow-up ids that changed."""
    now = _as_now(now)
    changed = []
    appt = conn.execute("SELECT status, appt_date, start_time, branch_id, doctor_id FROM appointments WHERE id = ?",
                        (appointment_id,)).fetchone()
    for fu in conn.execute("SELECT * FROM followups WHERE appointment_id = ?", (appointment_id,)).fetchall():
        update = {}
        status = appt["status"] if appt is not None else "cancelled"
        if fu["status"] == "pending" or (fu["status"] == "cancelled" and reinstate and status in ACTIVE_APPOINTMENT):
            if status in ACTIVE_APPOINTMENT:
                want = {"due_date": appt["appt_date"], "due_time": appt["start_time"],
                        "branch_id": branches.resolve(conn, appt["branch_id"]), "doctor_id": appt["doctor_id"]}
                update = {k: v for k, v in want.items() if fu[k] != v}
                if fu["status"] != "pending":
                    update["status"] = "pending"
            elif status == "completed" and fu["status"] == "pending":
                update = {"status": "done"}
            elif status == "cancelled" and fu["status"] == "pending":
                update = {"status": "cancelled"}
        if update:
            slots = {"followup_id": fu["id"], "appointment_id": appointment_id, "changes": update}
            _run(conn, "followup_synced", slots, _sync_handler(now), source="[sync]")
            changed.append(fu["id"])
        else:
            ensure_reminders(conn, fu["id"])
            conn.commit()
    return changed


def _sync_handler(now):
    def handler(conn, slots):
        fid, changes = slots["followup_id"], dict(slots["changes"])
        if changes.get("status") == "done":
            changes["completed_at"] = now.local.strftime("%Y-%m-%d %H:%M:%S")
        columns = sorted(changes)
        conn.execute("UPDATE followups SET {} WHERE id = ?".format(", ".join(c + " = ?" for c in columns)),
                     [changes[c] for c in columns] + [fid])
        ensure_reminders(conn, fid)
        return "followup", fid, {"appointment_id": slots["appointment_id"], "changes": slots["changes"]}
    return handler


SYNC_INTENTS = frozenset(("cancel_appointment", "reschedule_appointment", "restore_appointment",
                          "queue_mark_done", "queue_mark_no_show"))


def after_write(conn, intent, slots, entity_id, now=None):
    """Post-commit hook (app.post_write_hooks): an appointment write may have
    moved, cancelled or completed a follow-up's slot. Cannot raise."""
    try:
        if intent not in SYNC_INTENTS:
            return
        appointment_id = (slots or {}).get("appointment_id")
        if appointment_id is None:
            return
        sync_appointment(conn, appointment_id, now, reinstate=intent == "restore_appointment")
    except Exception:
        _logger.exception("follow-up sync failed after %s (the write itself succeeded)", intent)
        try:
            conn.rollback()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Planning a batch
# ---------------------------------------------------------------------------

def _clean_row(row):
    row = row if isinstance(row, dict) else {}
    diagnosis = row.get("diagnosis")
    return {
        "patient_id": _int(row.get("patient_id")), "due_date": (row.get("due_date") or "").strip() if isinstance(row.get("due_date"), str) else None,
        "due_time": (row.get("due_time") or "").strip() if isinstance(row.get("due_time"), str) else None,
        "doctor_id": _int(row.get("doctor_id")), "branch_id": _int(row.get("branch_id")),
        "diagnosis": diagnosis.strip() if isinstance(diagnosis, str) and diagnosis.strip() else None,
    }


def _closed_window_reason(conn, iso, branch_id, doctor_id):
    """Why this whole day is not bookable at that branch for that doctor, or None."""
    branch = branches.get_branch(conn, branch_id)
    name = branch["name"] if branch else "That branch"
    weekday = notify._EN_DAYS[date.fromisoformat(iso).weekday()]
    if branch is not None and branch["status"] == "closed":
        return "{} is marked closed.".format(name)
    windows = branches.doctor_windows(conn, branch_id, iso)
    if not windows:
        return "{} has no doctor on duty on {}s.".format(name, weekday)
    if doctor_id and not any(w[2] == doctor_id for w in windows):
        return "{} does not work at {} on {}s.".format(branches.doctor_label(conn, doctor_id) or "That doctor", name, weekday)
    relevant = [w[2] for w in windows if not doctor_id or w[2] == doctor_id]
    for who in relevant:
        for start, end, why in scheduling.blocked_ranges(conn, iso, branch_id, who):
            if start <= 0 and end >= 24 * 60:
                return "Appointments are blocked that day{}.".format(" ({})".format(why) if why else "")
    return None


def _suggest(conn, branch_id, doctor_id, from_iso, want_time, first_offset, now, taken):
    """The nearest free slot for that doctor/branch: on `from_iso` (first_offset 0)
    or from the following days, closest to the wanted time. None when nothing is
    free in the next SUGGEST_DAYS days."""
    today = now.local.date()
    start = date.fromisoformat(from_iso)
    want = _minutes(want_time or "09:00")
    for offset in range(first_offset, SUGGEST_DAYS + 1):
        day = start + timedelta(days=offset)
        if day < today:
            continue
        iso = day.isoformat()
        if _closed_window_reason(conn, iso, branch_id, doctor_id):
            continue
        best = None
        for hhmm in scheduling.generate_slots(conn, iso, branch_id=branch_id):
            if (branch_id, iso, hhmm) in taken or (day == today and hhmm <= now.local.strftime("%H:%M")):
                continue
            if doctor_id and branches.doctor_at(conn, branch_id, iso, hhmm) != doctor_id:
                continue
            gap = abs(_minutes(hhmm) - want)
            if best is None or gap < best[0]:
                best = (gap, hhmm)
        if best:
            on_duty = branches.doctor_at(conn, branch_id, iso, best[1])
            return {"date": iso, "time": best[1], "label": _when_label(iso, best[1]),
                    "doctor": branches.doctor_label(conn, on_duty)}
    return None


def _validate(conn, row, index, now, taken):
    """One row of a batch: {'index', 'ok', 'errors', 'warnings', 'suggestion', 'row', ...display}."""
    clean = _clean_row(row)
    errors, warnings = [], []
    out = {"index": index, "errors": errors, "warnings": warnings, "suggestion": None, "row": clean,
           "patient": None, "doctor": None, "branch": None}

    patient = conn.execute("SELECT id, name, phone FROM patients WHERE id = ?", (clean["patient_id"],)).fetchone() \
        if clean["patient_id"] else None
    if patient is None:
        errors.append("Choose a registered patient.")
    else:
        out["patient"] = patient["name"]
        if not booking_phone.valid_phone(patient["phone"]):
            # The follow-up books a real appointment, and a new booking needs a phone: refused here (and
            # again by the booking handler at Apply), with the same message every other booking path gives.
            errors.append(booking_phone.PATIENT_NO_PHONE)
        elif is_opted_out(conn, patient["phone"]):
            warnings.append("This number asked to stop reminders, so none will be sent.")

    day = _date(clean["due_date"])
    if day is None:
        errors.append("Pick a valid follow-up date.")
    if not clean["due_time"] or not _TIME.match(clean["due_time"]):
        errors.append("Pick a time for the visit.")
    try:
        branch_id = branches.resolve(conn, clean["branch_id"])
        branch = branches.get_branch(conn, branch_id)
        if branch is None or not branch["active"]:
            raise ValueError
    except ValueError:
        errors.append("Choose a branch.")
        branch = None
    doctor_id = clean["doctor_id"]
    if doctor_id and branches.get_doctor(conn, doctor_id) is None:
        errors.append("That doctor does not exist.")
    if errors:
        return _finish(conn, out)

    branch_id = branch["id"]
    clean["branch_id"] = branch_id
    out["branch"] = branch["name"]
    iso, hhmm, today = clean["due_date"], clean["due_time"], now.local.date()
    if (iso, hhmm) <= (today.isoformat(), now.local.strftime("%H:%M")):
        errors.append("That time has already passed.")
        return _finish(conn, out)
    if day > today + timedelta(days=HORIZON_DAYS):
        errors.append("That is more than a year away. Pick a nearer date.")
        return _finish(conn, out)

    # the whole day: closed branch, doctor not working that weekday, leave / closure / booking block
    problem = _closed_window_reason(conn, iso, branch_id, doctor_id)
    if problem:
        errors.append(problem)
        out["suggestion"] = _suggest(conn, branch_id, doctor_id, iso, hhmm, 1, now, taken)
        return _finish(conn, out)

    on_duty = scheduling.within_doctor_hours(conn, iso, hhmm, scheduling.SLOT_MINUTES, branch_id)
    if hhmm not in scheduling.slot_grid(conn, iso, branch_id) or on_duty is None:
        errors.append("{} has no doctor on duty at {} (open {}).".format(
            branch["name"], hhmm, branches.hours_summary(conn, branch_id, iso)))
    elif doctor_id and on_duty != doctor_id:
        errors.append("{} is not on duty then; {} is.".format(
            branches.doctor_label(conn, doctor_id), branches.doctor_label(conn, on_duty) or "another doctor"))
    elif scheduling.block_reason(conn, iso, hhmm, scheduling.SLOT_MINUTES, branch_id) is not None:
        why = scheduling.block_reason(conn, iso, hhmm, scheduling.SLOT_MINUTES, branch_id)
        errors.append("Appointments are blocked at that time{}.".format(" ({})".format(why) if why else ""))
    elif (branch_id, iso, hhmm) in taken:
        errors.append("Another row of this batch already uses that slot.")
    elif not scheduling.is_slot_free(conn, iso, hhmm, scheduling.SLOT_MINUTES, ignore_blocks=True, branch_id=branch_id):
        errors.append("That slot is already booked.")
    if errors:
        out["suggestion"] = _suggest(conn, branch_id, doctor_id, iso, hhmm, 0, now, taken)
        return _finish(conn, out)

    clean["doctor_id"] = on_duty or None
    out["doctor"] = branches.doctor_label(conn, on_duty) if on_duty else None
    if patient is not None and conn.execute(
            "SELECT 1 FROM followups WHERE patient_id = ? AND status = 'pending' AND due_date = ? AND appointment_id IS NOT NULL",
            (patient["id"], iso)).fetchone():
        warnings.append("This patient already has a follow-up booked on that day.")
    return _finish(conn, out)


def _finish(conn, out):
    out["ok"] = not out["errors"]
    row = out["row"]
    if out["doctor"] is None and row.get("doctor_id"):
        out["doctor"] = branches.doctor_label(conn, row["doctor_id"])
    return out


def _check_size(rows):
    if not isinstance(rows, list) or not rows:
        raise FollowupError("Add at least one follow-up.")
    if len(rows) > MAX_BATCH_ROWS:
        raise FollowupError("A batch can hold at most {} follow-ups.".format(MAX_BATCH_ROWS))


def plan(conn, rows, now=None):
    """Check every row of a batch and write nothing. {'rows': [...], 'counts': {...}}."""
    _check_size(rows)
    now = _as_now(now)
    taken, out = set(), []
    for index, row in enumerate(rows):
        result = _validate(conn, row, index, now, taken)
        if result["ok"]:
            taken.add((result["row"]["branch_id"], result["row"]["due_date"], result["row"]["due_time"]))
        out.append(result)
    valid = sum(1 for r in out if r["ok"])
    return {"rows": out, "counts": {"total": len(out), "valid": valid, "invalid": len(out) - valid}}


# ---------------------------------------------------------------------------
# Applying a batch, and taking it back
# ---------------------------------------------------------------------------

def _create_handler(now, appointment_id, batch_id):
    def handler(conn, slots):
        cur = conn.execute(
            "INSERT INTO followups (patient_id, due_date, due_time, doctor_id, branch_id, appointment_id, diagnosis, batch_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (slots["patient_id"], slots["due_date"], slots["due_time"], slots["doctor_id"], slots["branch_id"],
             appointment_id, slots["diagnosis"], batch_id))
        fid = cur.lastrowid
        ensure_reminders(conn, fid)
        payload = dict(slots, appointment_id=appointment_id, batch_id=batch_id)
        return "followup", fid, payload
    return handler


def apply(conn, rows, *, handlers, after_commit, now=None):
    """Book and record every valid row. One row failing (a slot taken since the
    review, a refused write) never stops the others: it is returned with its
    reason, never dropped. {'ok', 'batch_id', 'results': [...], 'counts': {...}}."""
    _check_size(rows)
    now = _as_now(now)
    results, taken, batch_id = [], set(), None
    with auto_actions.COMMIT_LOCK:
        for index, row in enumerate(rows):
            checked = _validate(conn, row, index, now, taken)
            base = {"index": index, "patient": checked["patient"], "due_date": checked["row"]["due_date"],
                    "due_time": checked["row"]["due_time"]}
            if not checked["ok"]:
                results.append(dict(base, ok=False, error=" ".join(checked["errors"]), suggestion=checked["suggestion"]))
                continue
            if batch_id is None:
                batch_id = conn.execute("INSERT INTO followup_batches (created_at) VALUES (?)",
                                        (now.local.strftime("%Y-%m-%d %H:%M:%S"),)).lastrowid
                conn.commit()
            results.append(_apply_row(conn, checked, base, batch_id, handlers, after_commit, now))
            if results[-1]["ok"]:
                taken.add((checked["row"]["branch_id"], checked["row"]["due_date"], checked["row"]["due_time"]))
        created = sum(1 for r in results if r["ok"])
        if batch_id is not None and not created:
            conn.execute("DELETE FROM followup_batches WHERE id = ?", (batch_id,))      # nothing was made: no batch to undo
            conn.commit()
            batch_id = None
    return {"ok": True, "batch_id": batch_id, "results": results,
            "counts": {"created": created, "failed": len(results) - created}}


def _apply_row(conn, checked, base, batch_id, handlers, after_commit, now):
    row = checked["row"]
    patient = conn.execute("SELECT id, name, phone FROM patients WHERE id = ?", (row["patient_id"],)).fetchone()
    slots = {"patient_id": patient["id"], "appt_date": row["due_date"], "start_time": row["due_time"],
             "duration_minutes": None, "branch_id": row["branch_id"], "notes": "Follow-up visit"}
    try:
        appointment_id, _ = auto_actions.staff_action(
            conn, "book_appointment", slots, handlers, after_commit, now.local,
            note="Schedule follow-ups, batch #{}".format(batch_id), event="followup_scheduled",
            patient_id=patient["id"], patient_name=patient["name"], wa_id=phone_to_wa_id(patient["phone"]),
            detail="Follow-up booked {} {} at {}".format(row["due_date"], row["due_time"], branches.branch_label(conn, row["branch_id"])),
            meta={"batch_id": batch_id, "appt_date": row["due_date"], "start_time": row["due_time"], "branch_id": row["branch_id"]})
    except auto_actions.ActionError as exc:
        return dict(base, ok=False, error=str(exc))
    try:
        proposal_id, followup_id = _run(conn, "schedule_followup", row, _create_handler(now, appointment_id, batch_id))
    except Exception as exc:
        _logger.exception("could not record the follow-up after booking appointment %s; cancelling it", appointment_id)
        try:
            auto_actions.staff_action(
                conn, "cancel_appointment", {"appointment_id": appointment_id, "require_active": True, "quiet": True},
                handlers, after_commit, now.local, note="Follow-up could not be recorded", event="staff_cancelled",
                patient_id=patient["id"], patient_name=patient["name"], appointment_id=appointment_id,
                detail="Cancelled: the follow-up could not be recorded")
        except Exception:
            _logger.exception("could not cancel appointment %s either", appointment_id)
        return dict(base, ok=False, error="Could not save the follow-up ({}).".format(type(exc).__name__))
    return dict(base, ok=True, followup_id=followup_id, appointment_id=appointment_id, doctor=checked["doctor"],
                branch=checked["branch"])


def _cancel_handler(handlers, followup_id, appointment_slots, status, reason):
    """Atomic: the follow-up changes status, its reminders stop and its appointment
    is cancelled (through the normal cancel handler) in ONE transaction."""
    def handler(conn, slots):
        done_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if status == "done" else None
        moved = conn.execute("UPDATE followups SET status = ?, completed_at = ? WHERE id = ? AND status = 'pending'",
                             (status, done_at, followup_id)).rowcount
        if not moved:
            raise ValueError("This follow-up is no longer pending.")
        cancel_reminders(conn, followup_id, reason)
        _, appointment_id, _payload = handlers["cancel_appointment"](conn, dict(appointment_slots))
        return "followup", followup_id, {"status": status, "appointment_id": appointment_id, "reason": reason}
    return handler


def undo_batch(conn, batch_id, *, handlers, after_commit, now=None):
    """Take a whole batch back: every follow-up it made that is still pending is
    cancelled, its reminders stop and its appointment is cancelled. Anything that
    has moved on (done, cancelled, the patient is already in) is left and listed.
    {'ok', 'cancelled', 'skipped': [{name, why}]}."""
    now = _as_now(now)
    with auto_actions.COMMIT_LOCK:
        batch = conn.execute("SELECT * FROM followup_batches WHERE id = ?", (batch_id,)).fetchone()
        if batch is None:
            return {"ok": False, "error": "That batch was not found."}
        claimed = conn.execute("UPDATE followup_batches SET status = 'undone', undone_at = ? WHERE id = ? AND status = 'applied'",
                               (now.local.strftime("%Y-%m-%d %H:%M:%S"), batch_id)).rowcount
        conn.commit()
        if not claimed:
            return {"ok": False, "error": "That batch is already undone."}
        cancelled, skipped = 0, []
        for fu in [dict(r) for r in conn.execute("SELECT id FROM followups WHERE batch_id = ? ORDER BY id", (batch_id,)).fetchall()]:
            fu = get_followup(conn, fu["id"])
            name = fu["patient_name"]
            if fu["status"] != "pending":
                skipped.append({"name": name, "why": "it is already {}".format(fu["status"])})
                continue
            if fu["appointment_status"] not in ACTIVE_APPOINTMENT or fu["appointment_queue_state"]:
                skipped.append({"name": name, "why": "the patient has already been seen or checked in"
                                if fu["appointment_queue_state"] or fu["appointment_status"] == "completed"
                                else "its appointment is already {}".format(fu["appointment_status"])})
                continue
            told = conn.execute("SELECT 1 FROM notifications WHERE appointment_id = ? AND event = 'booking_confirmed' "
                                "AND status = 'sent'", (fu["appointment_id"],)).fetchone() is not None
            # The patient is only told "cancelled" if they were told "booked".
            cancel_slots = {"appointment_id": fu["appointment_id"], "require_active": True, "quiet": not told}
            try:
                proposal_id, _ = _run(conn, "followup_batch_undone", {"followup_id": fu["id"], "batch_id": batch_id},
                                      _cancel_handler(handlers, fu["id"], cancel_slots, "cancelled", "the batch was undone"))
            except Exception as exc:
                skipped.append({"name": name, "why": str(exc) or type(exc).__name__})
                continue
            _audit_extra(conn, proposal_id, "cancel_appointment", "appointment", fu["appointment_id"], {"status": "cancelled"})
            patient_activity.log(
                conn, event="followup_undone", source="staff", patient_id=fu["patient_id"], patient_name=name,
                appointment_id=fu["appointment_id"], proposal_id=proposal_id,
                detail="Follow-up {} {} cancelled with its batch".format(fu["due_date"], fu["due_time"]),
                meta={"batch_id": batch_id}, now=now.local)
            auto_actions._safe_hooks(after_commit, conn, "cancel_appointment", cancel_slots, fu["appointment_id"],
                                     phone_to_wa_id(fu["patient_phone"]))
            cancelled += 1
    return {"ok": True, "cancelled": cancelled, "skipped": skipped}


# ---------------------------------------------------------------------------
# The patient's buttons (Already visited / Cancel)
# ---------------------------------------------------------------------------

def patient_view(conn, followup_id, wa_id, now):
    """The follow-up as `wa_id` may act on it, or None -- not theirs, not a slot
    follow-up, no longer pending, its appointment gone / started / checked in, or
    the visit time already past. (The same answer for all of those: the patient
    is told it is no longer active, and nothing is revealed about anyone else's.)"""
    fu = get_followup(conn, followup_id)
    if fu is None or not _live(fu):
        return None
    mine = last10_digits(wa_id or "")
    if len(mine) != 10 or last10_digits(fu["patient_phone"] or "") != mine:
        return None
    if (fu["due_date"], fu["due_time"]) <= (now.date().isoformat(), now.strftime("%H:%M")):
        return None
    return fu


def patient_action(conn, *, action, followup_id, wa_id, now, handlers=None, after_commit=None, msg_id=None, **_):
    """The patient tapped 'Already visited' or confirmed 'Cancel'. Either way the
    follow-up is closed (done / cancelled), its queued reminders stop and its
    booked slot is freed (cancelled -- not a no-show), in ONE audited
    transaction, and the usual post-commit hooks run. No "your appointment was
    cancelled" message goes out: the conversation replies with its own.
    Returns {'ok': True, ...} or {'ok': False, 'error': ...}."""
    if action not in ("visited", "cancel"):
        return {"ok": False, "error": "unknown action"}
    if handlers is None:
        from clinic import intents
        handlers = intents.HANDLERS
    now = now if isinstance(now, datetime) else _as_now(now).local
    with auto_actions.COMMIT_LOCK:
        fu = patient_view(conn, followup_id, wa_id, now)
        if fu is None:
            return {"ok": False, "error": "This follow-up is no longer active."}
        status = "done" if action == "visited" else "cancelled"
        reason = "the patient said they had already visited" if action == "visited" else "the patient cancelled"
        cancel_slots = {"appointment_id": fu["appointment_id"], "require_active": True, "quiet": True}
        source = "{} wa_message #{} -- follow-up button '{}'".format(auto_actions.AUTO_PREFIX, msg_id, action)
        try:
            proposal_id, _ = _run(conn, "followup_" + ("visited" if action == "visited" else "cancelled_by_patient"),
                                  {"followup_id": followup_id, "appointment_id": fu["appointment_id"]},
                                  _cancel_handler(handlers, followup_id, cancel_slots, status, reason), source=source)
        except Exception as exc:
            _logger.warning("follow-up %s: the patient's %s could not be applied: %s", followup_id, action, exc)
            return {"ok": False, "error": str(exc) or type(exc).__name__}
        _audit_extra(conn, proposal_id, "cancel_appointment", "appointment", fu["appointment_id"], {"status": "cancelled"})
    auto_actions._safe_hooks(after_commit, conn, "cancel_appointment", cancel_slots, fu["appointment_id"], wa_id)
    return {"ok": True, "followup_id": followup_id, "appointment_id": fu["appointment_id"], "status": status,
            "due_date": fu["due_date"], "due_time": fu["due_time"]}


# ---------------------------------------------------------------------------
# Staff: the list, editing the diagnosis, retrying, sending by hand
# ---------------------------------------------------------------------------

def edit_diagnosis(conn, followup_id, text, now=None):
    """Staff edit the internal diagnosis note (audited, old and new value)."""
    if text is not None and not isinstance(text, str):
        raise FollowupError("The diagnosis must be text.")
    text = (text or "").strip() or None
    if text and len(text) > MAX_DIAGNOSIS:
        raise FollowupError("Keep the diagnosis under {} characters.".format(MAX_DIAGNOSIS))
    fu = get_followup(conn, followup_id)
    if fu is None:
        raise FollowupError("That follow-up was not found.")
    if fu["diagnosis"] == text:
        return fu["diagnosis"]

    def handler(conn, slots):
        conn.execute("UPDATE followups SET diagnosis = ? WHERE id = ?", (slots["new"], slots["followup_id"]))
        return "followup", slots["followup_id"], {"diagnosis_old": slots["old"], "diagnosis_new": slots["new"]}
    _run(conn, "followup_diagnosis_edited", {"followup_id": followup_id, "old": fu["diagnosis"], "new": text}, handler)
    return text


def _reminder_status(conn, r):
    """(status, detail, notification) for the UI: queued / sent / blocked / failed / skipped."""
    if r["state"] == "scheduled":
        return "queued", "due {}".format(_when_label(r["due_at"][:10], r["due_at"][11:16])) if r["due_at"] else "", None
    if r["state"] in ("skipped", "cancelled"):
        return "skipped", r["reason"] or "", None
    note = conn.execute("SELECT id, status, error, sent_at FROM notifications WHERE id = ?", (r["notification_id"],)).fetchone()
    if r["state"] == "manual":
        return "sent", "sent by hand", note
    if note is None:
        return "skipped", "the message is gone", None
    status = note["status"]
    if status == "pending":
        return "queued", "waiting to be sent", note
    if status == "sent":
        return "sent", "", note
    if status == "dry_run":
        return "sent", "recorded only (dry-run mode, nothing was sent)", note
    if status == "blocked_no_window":
        return "blocked", "outside WhatsApp's 24-hour window and no approved template", note
    if status == "failed":
        return "failed", note["error"] or "", note
    return "skipped", "no phone number on file" if status == "skipped_no_phone" else status, note


def _reminders_for(conn, fu):
    out = []
    if not is_slot_followup(fu):
        return out
    for r in conn.execute("SELECT * FROM followup_reminders WHERE followup_id = ? AND slot_date = ? AND slot_time = ? "
                          "ORDER BY CASE kind WHEN '2d' THEN 0 ELSE 1 END", (fu["id"], fu["due_date"], fu["due_time"])).fetchall():
        status, detail, note = _reminder_status(conn, r)
        out.append({"id": r["id"], "kind": r["kind"], "label": KIND_LABEL[r["kind"]], "status": status, "detail": detail,
                    "due_at": r["due_at"], "notification_id": r["notification_id"],
                    "retryable": r["state"] == "enqueued" and note is not None and note["status"] in ("blocked_no_window", "failed")})
    return out


def _branch_clause(conn, branch):
    """(sql, params) limiting a list to the viewed branch: 'all' / None = every branch."""
    if branch in (None, "", "all"):
        return "", []
    return " AND COALESCE(f.branch_id, ?) = ?", [branches.default_branch_id(conn), int(branch)]


def list_followups(conn, branch=None, now=None, history_days=14):
    """Pending follow-ups plus those of the last `history_days` days, soonest first,
    each with the state of its two reminders."""
    now = _as_now(now)
    clause, params = _branch_clause(conn, branch)
    since = (now.local.date() - timedelta(days=history_days)).isoformat()
    rows = conn.execute(
        "SELECT f.id FROM followups f WHERE (f.status = 'pending' OR f.due_date >= ?){} "
        "ORDER BY (f.status != 'pending'), f.due_date, COALESCE(f.due_time, ''), f.id LIMIT 300".format(clause),
        [since] + params).fetchall()
    out = []
    for r in rows:
        fu = get_followup(conn, r["id"])
        doctor = branches.doctor_label(conn, fu["doctor_id"]) if fu["doctor_id"] else None
        branch_id = branches.resolve(conn, fu["branch_id"])          # an old follow-up with no branch belongs to the default
        out.append({
            "id": fu["id"], "patient_id": fu["patient_id"], "patient_name": fu["patient_name"], "phone": fu["patient_phone"],
            "doctor": doctor, "doctor_id": fu["doctor_id"], "branch": branches.branch_label(conn, branch_id), "branch_id": branch_id,
            "due_date": fu["due_date"], "due_time": fu["due_time"], "status": fu["status"], "has_slot": is_slot_followup(fu),
            "appointment_id": fu["appointment_id"], "appointment_status": fu["appointment_status"],
            "diagnosis": fu["diagnosis"] or "", "opted_out": is_opted_out(conn, fu["patient_phone"]),
            "reminders": _reminders_for(conn, fu),
        })
    return out


def manual_list(conn, branch=None):
    """Reminders staff may have to send by hand: blocked (outside the 24-hour
    window, no approved template yet) or failed, for follow-ups still live.
    Each carries the exact text so it can be copied into WhatsApp."""
    clause, params = _branch_clause(conn, branch)
    out = []
    for r in conn.execute(
            "SELECT r.id, r.kind, r.followup_id, r.notification_id FROM followup_reminders r "
            "JOIN followups f ON f.id = r.followup_id JOIN notifications n ON n.id = r.notification_id "
            "WHERE r.state = 'enqueued' AND n.status IN ('blocked_no_window', 'failed') AND f.status = 'pending'{} "
            "ORDER BY f.due_date, f.due_time, r.id".format(clause), params).fetchall():
        fu = get_followup(conn, r["followup_id"])
        note = conn.execute("SELECT body, status, error FROM notifications WHERE id = ?", (r["notification_id"],)).fetchone()
        out.append({
            "reminder_id": r["id"], "followup_id": fu["id"], "notification_id": r["notification_id"], "kind": r["kind"],
            "label": KIND_LABEL[r["kind"]], "patient_name": fu["patient_name"], "phone": fu["patient_phone"],
            "visit": _when_label(fu["due_date"], fu["due_time"]), "text": note["body"],
            "status": "blocked" if note["status"] == "blocked_no_window" else "failed", "error": note["error"] or "",
        })
    return out


def retry_reminder(conn, reminder_id):
    """Staff clicked Retry: the outbox row goes back to pending. True if it was eligible."""
    r = conn.execute("SELECT notification_id, state FROM followup_reminders WHERE id = ?", (reminder_id,)).fetchone()
    if r is None or r["state"] != "enqueued" or r["notification_id"] is None:
        return False
    return notify.retry(conn, r["notification_id"])


def mark_sent_manually(conn, reminder_id, now=None):
    """Staff sent this reminder by hand (copied it into WhatsApp). It is recorded
    as sent in the outbox -- so it is never retried or counted as unsent -- and in
    the audit log. True if it was eligible."""
    now = _as_now(now)
    r = conn.execute("SELECT r.*, n.status AS note_status FROM followup_reminders r "
                     "LEFT JOIN notifications n ON n.id = r.notification_id WHERE r.id = ?", (reminder_id,)).fetchone()
    if r is None or r["state"] != "enqueued" or r["note_status"] not in ("blocked_no_window", "failed"):
        return False

    def handler(conn, slots):
        conn.execute("UPDATE notifications SET status = 'sent', error = 'Sent by hand by staff', sent_at = ? WHERE id = ?",
                     (now.utc.strftime("%Y-%m-%d %H:%M:%S"), r["notification_id"]))
        conn.execute("UPDATE followup_reminders SET state = 'manual', reason = 'sent by hand', updated_at = datetime('now') WHERE id = ?",
                     (reminder_id,))
        return "followup", r["followup_id"], {"reminder": r["kind"], "notification_id": r["notification_id"], "sent_by_hand": True}
    _run(conn, "followup_reminder_sent_by_hand", {"reminder_id": reminder_id}, handler)
    return True


# ---------------------------------------------------------------------------
# The slot picker for the batch form
# ---------------------------------------------------------------------------

def free_slots(conn, iso, branch_id, doctor_id, now=None):
    """{'free': [HH:MM], 'doctor', 'hours', 'problem', 'suggestion'} for one day at
    one branch (and one doctor, when given): what the Time dropdown offers."""
    now = _as_now(now)
    branch_id = branches.resolve(conn, branch_id)
    problem = _closed_window_reason(conn, iso, branch_id, doctor_id)
    out = {"free": [], "doctor": None, "hours": branches.hours_summary(conn, branch_id, iso), "problem": problem,
           "suggestion": None}
    if problem:
        out["suggestion"] = _suggest(conn, branch_id, doctor_id, iso, "09:00", 1, now, set())
        return out
    for hhmm in scheduling.generate_slots(conn, iso, branch_id=branch_id):
        if iso == now.local.date().isoformat() and hhmm <= now.local.strftime("%H:%M"):
            continue
        if doctor_id and branches.doctor_at(conn, branch_id, iso, hhmm) != doctor_id:
            continue
        out["free"].append(hhmm)
    if out["free"]:
        out["doctor"] = branches.doctor_label(conn, branches.doctor_at(conn, branch_id, iso, out["free"][0]))
    else:
        out["suggestion"] = _suggest(conn, branch_id, doctor_id, iso, "09:00", 1, now, set())
    return out


def template_settings(conn):
    """Every Meta template with whether staff marked it approved, for Settings."""
    approved = settings.approved_templates(conn)
    return [{"name": t["name"], "reminder": t["reminder"], "written_in": t["written_in"], "approved": t["name"] in approved,
             "body": t["components"]["body"]["text"]} for t in followup_notify.meta_templates()]
