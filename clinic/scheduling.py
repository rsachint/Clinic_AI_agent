"""Real appointment/time-slot scheduling: clinic hours, slot generation, and
the double-booking guard. Plain functions, not a class, matching this
codebase's style elsewhere (queries.py, intents.py).

Multi-branch: every function takes an optional `branch_id` (None = the default
branch, so a one-branch database behaves exactly as before). A branch's bookable
hours are its doctors' schedule windows (clinic/branches.py); CLINIC_HOURS below
is the default Branch A seed and the fallback for a database without branches.
"""

import sqlite3

from clinic import branches

CLINIC_HOURS = [("09:00", "13:00"), ("16:00", "20:00")]
# One number for both the length of a new appointment and the slot grid, so
# offered times (09:00, 09:30, ...) never overlap an appointment of that length.
SLOT_MINUTES = 30

_ACTIVE_STATUSES = ("booked", "confirmed")


class SlotConflictError(Exception):
    """Raised when a booking/reschedule is attempted against a date+time
    that's no longer free by the time the write handler actually runs (a
    real race the human could hit between seeing the review card and
    tapping Approve) -- never silently overwritten or ignored."""


class SlotBlockedError(SlotConflictError):
    """The slot falls inside a staff-defined booking block (booking_blocks).
    A subclass of SlotConflictError so every existing "slot not available"
    handler also covers it; callers that care can tell the two apart. Staff
    can book into a block only by passing an explicit override flag."""


def _to_minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _from_minutes(total):
    return "{:02d}:{:02d}".format(total // 60, total % 60)


def _overlaps(start, end, intervals):
    return any(start < ie and existing_start < end for existing_start, ie in intervals)


def _booked_intervals(conn, appt_date, exclude_appointment_id=None, branch_id=None):
    placeholders = ", ".join("?" for _ in _ACTIVE_STATUSES)
    default = branches.default_branch_id(conn)
    branch_id = branches.resolve(conn, branch_id)
    try:
        rows = conn.execute(
            "SELECT id, start_time, duration_minutes FROM appointments "
            "WHERE appt_date = ? AND status IN ({}) AND COALESCE(branch_id, ?) = ?".format(placeholders),
            (appt_date,) + _ACTIVE_STATUSES + (default, branch_id),
        ).fetchall()
    except sqlite3.OperationalError:       # a database from before branches existed
        rows = conn.execute(
            "SELECT id, start_time, duration_minutes FROM appointments "
            "WHERE appt_date = ? AND status IN ({})".format(placeholders),
            (appt_date,) + _ACTIVE_STATUSES,
        ).fetchall()
    intervals = []
    for row in rows:
        if exclude_appointment_id is not None and row["id"] == exclude_appointment_id:
            continue
        start = _to_minutes(row["start_time"])
        intervals.append((start, start + row["duration_minutes"]))
    return intervals


def blocked_ranges(conn, appt_date, branch_id=None, doctor_id=None):
    """[(start_minute, end_minute, reason)] of the active booking blocks that
    cover `appt_date` for that branch (a block with no branch applies to every
    branch) and, when a doctor is given, that doctor (a block with no doctor
    applies to every doctor). A block without times covers the whole day. A
    database that has no booking_blocks table has no blocks."""
    branch_id = branches.resolve(conn, branch_id)
    try:
        rows = conn.execute(
            "SELECT start_time, end_time, reason FROM booking_blocks "
            "WHERE active = 1 AND start_date <= ? AND end_date >= ? "
            "AND (branch_id IS NULL OR branch_id = ?) AND (doctor_id IS NULL OR doctor_id = ?)",
            (appt_date, appt_date, branch_id, doctor_id if doctor_id is not None else -1),
        ).fetchall()
    except sqlite3.OperationalError:
        try:   # booking_blocks from before branches existed (no branch/doctor columns)
            rows = conn.execute(
                "SELECT start_time, end_time, reason FROM booking_blocks "
                "WHERE active = 1 AND start_date <= ? AND end_date >= ?",
                (appt_date, appt_date),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    ranges = []
    for row in rows:
        if row[0] and row[1]:
            ranges.append((_to_minutes(row[0]), _to_minutes(row[1]), row[2] or ""))
        else:
            ranges.append((0, 24 * 60, row[2] or ""))
    return ranges


def block_reason(conn, appt_date, start_time, duration_minutes=None, branch_id=None):
    """None if the slot is not inside a booking block, else the block's
    reason text ('' when the block has no reason)."""
    start = _to_minutes(start_time)
    end = start + (duration_minutes or SLOT_MINUTES)
    doctor_id = branches.doctor_at(conn, branch_id, appt_date, start_time)
    for b_start, b_end, reason in blocked_ranges(conn, appt_date, branch_id, doctor_id):
        if start < b_end and b_start < end:
            return reason
    return None


def is_slot_blocked(conn, appt_date, start_time, duration_minutes=None, branch_id=None):
    return block_reason(conn, appt_date, start_time, duration_minutes, branch_id) is not None


def day_blocked(conn, appt_date, branch_id=None):
    """True if a whole-day block covers `appt_date` at that branch."""
    return any(b_start <= 0 and b_end >= 24 * 60 for b_start, b_end, _ in blocked_ranges(conn, appt_date, branch_id))


def _windows(conn, appt_date, branch_id):
    """[(start, end, doctor_id)] bookable windows at a branch on a date: its
    doctors' schedule. A database from before branches existed falls back to
    the old fixed CLINIC_HOURS."""
    try:
        conn.execute("SELECT 1 FROM doctor_schedules LIMIT 1")
    except sqlite3.OperationalError:
        return [(_to_minutes(a), _to_minutes(b), None) for a, b in CLINIC_HOURS]
    return branches.doctor_windows(conn, branch_id, appt_date)


def within_doctor_hours(conn, appt_date, start_time, duration_minutes, branch_id=None):
    """The doctor_id on duty for the whole of [start, start+duration) at that
    branch, or None when the branch has no doctor then (closed, off day, or the
    time spills outside the doctor's window)."""
    start = _to_minutes(start_time)
    end = start + duration_minutes
    for w_start, w_end, doctor_id in _windows(conn, appt_date, branch_id):
        if w_start <= start and end <= w_end:
            return doctor_id if doctor_id is not None else 0
    return None


def is_slot_free(conn, appt_date, start_time, duration_minutes, exclude_appointment_id=None, ignore_blocks=False,
                 branch_id=None):
    """The double-booking guard. Must be called again at confirm-time (i.e.
    from inside the write handler, not just once at initial NLU parse time)
    since the review card lets a human edit fields before approving -- the
    slot that looked free when the card was shown may not be free anymore
    by the time it's actually confirmed.

    A slot inside a staff-defined booking block is not free either, unless
    the caller passes ignore_blocks=True (the explicit staff override)."""
    start = _to_minutes(start_time)
    end = start + duration_minutes
    booked = _booked_intervals(conn, appt_date, exclude_appointment_id, branch_id)
    if _overlaps(start, end, booked):
        return False
    if not ignore_blocks and is_slot_blocked(conn, appt_date, start_time, duration_minutes, branch_id):
        return False
    return True


def generate_slots(conn, appt_date, include_blocked=False, branch_id=None):
    """All bookable slot start-times at a branch on `appt_date` (inside a
    doctor's window) that don't overlap an existing booked/confirmed
    appointment there (and aren't inside a booking block, unless
    include_blocked), in chronological order."""
    booked = _booked_intervals(conn, appt_date, None, branch_id)
    free = set()
    for w_start, w_end, doctor_id in _windows(conn, appt_date, branch_id):
        blocked = [] if include_blocked else [(a, b) for a, b, _ in blocked_ranges(conn, appt_date, branch_id, doctor_id)]
        t = w_start
        while t + SLOT_MINUTES <= w_end:
            if not _overlaps(t, t + SLOT_MINUTES, booked) and not _overlaps(t, t + SLOT_MINUTES, blocked):
                free.add(_from_minutes(t))
            t += SLOT_MINUTES
    return sorted(free)


def slot_grid(conn=None, appt_date=None, branch_id=None):
    """Every bookable slot start in a day (booked or not). With no arguments:
    the default clinic day (CLINIC_HOURS); with a connection and a date: that
    branch's doctor windows that day."""
    if conn is None or appt_date is None:
        windows = [(_to_minutes(a), _to_minutes(b)) for a, b in CLINIC_HOURS]
    else:
        windows = [(a, b) for a, b, _ in _windows(conn, appt_date, branch_id)]
    starts = set()
    for t0, end_of_shift in windows:
        t = t0
        while t + SLOT_MINUTES <= end_of_shift:
            starts.add(_from_minutes(t))
            t += SLOT_MINUTES
    return sorted(starts)


def blocked_only_slots(conn, appt_date, branch_id=None):
    """Slot starts that are not booked but ARE blocked -- what staff would
    have to override to use."""
    free_ignoring_blocks = set(generate_slots(conn, appt_date, include_blocked=True, branch_id=branch_id))
    free = set(generate_slots(conn, appt_date, branch_id=branch_id))
    return [t for t in slot_grid(conn, appt_date, branch_id) if t in free_ignoring_blocks and t not in free]
