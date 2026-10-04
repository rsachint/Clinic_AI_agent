"""Real appointment/time-slot scheduling: clinic hours, slot generation, and
the double-booking guard. Plain functions, not a class, matching this
codebase's style elsewhere (queries.py, intents.py).

The clinic-hours/slot-length values below are just constants, not a
configuration UI -- a solo-GP-typical split shift (morning + evening), easy
to change later by editing this file.
"""

import sqlite3

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


def _booked_intervals(conn, appt_date, exclude_appointment_id=None):
    placeholders = ", ".join("?" for _ in _ACTIVE_STATUSES)
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


def blocked_ranges(conn, appt_date):
    """[(start_minute, end_minute, reason)] of the active booking blocks that
    cover `appt_date`. A block without times covers the whole day. A database
    that has no booking_blocks table has no blocks."""
    try:
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


def block_reason(conn, appt_date, start_time, duration_minutes=None):
    """None if the slot is not inside a booking block, else the block's
    reason text ('' when the block has no reason)."""
    start = _to_minutes(start_time)
    end = start + (duration_minutes or SLOT_MINUTES)
    for b_start, b_end, reason in blocked_ranges(conn, appt_date):
        if start < b_end and b_start < end:
            return reason
    return None


def is_slot_blocked(conn, appt_date, start_time, duration_minutes=None):
    return block_reason(conn, appt_date, start_time, duration_minutes) is not None


def day_blocked(conn, appt_date):
    """True if a whole-day block covers `appt_date`."""
    return any(b_start <= 0 and b_end >= 24 * 60 for b_start, b_end, _ in blocked_ranges(conn, appt_date))


def is_slot_free(conn, appt_date, start_time, duration_minutes, exclude_appointment_id=None, ignore_blocks=False):
    """The double-booking guard. Must be called again at confirm-time (i.e.
    from inside the write handler, not just once at initial NLU parse time)
    since the review card lets a human edit fields before approving -- the
    slot that looked free when the card was shown may not be free anymore
    by the time it's actually confirmed.

    A slot inside a staff-defined booking block is not free either, unless
    the caller passes ignore_blocks=True (the explicit staff override)."""
    start = _to_minutes(start_time)
    end = start + duration_minutes
    booked = _booked_intervals(conn, appt_date, exclude_appointment_id)
    if _overlaps(start, end, booked):
        return False
    if not ignore_blocks and is_slot_blocked(conn, appt_date, start_time, duration_minutes):
        return False
    return True


def generate_slots(conn, appt_date, include_blocked=False):
    """All CLINIC_HOURS slot start-times on `appt_date` that don't overlap
    an existing booked/confirmed appointment (and aren't inside a booking
    block, unless include_blocked), in chronological order."""
    booked = _booked_intervals(conn, appt_date)
    blocked = [] if include_blocked else [(a, b) for a, b, _ in blocked_ranges(conn, appt_date)]
    free = []
    for shift_start, shift_end in CLINIC_HOURS:
        t = _to_minutes(shift_start)
        end_of_shift = _to_minutes(shift_end)
        while t + SLOT_MINUTES <= end_of_shift:
            if not _overlaps(t, t + SLOT_MINUTES, booked) and not _overlaps(t, t + SLOT_MINUTES, blocked):
                free.append(_from_minutes(t))
            t += SLOT_MINUTES
    return free


def slot_grid():
    """Every bookable slot start in a clinic day (booked or not)."""
    starts = []
    for shift_start, shift_end in CLINIC_HOURS:
        t = _to_minutes(shift_start)
        end_of_shift = _to_minutes(shift_end)
        while t + SLOT_MINUTES <= end_of_shift:
            starts.append(_from_minutes(t))
            t += SLOT_MINUTES
    return starts


def blocked_only_slots(conn, appt_date):
    """Slot starts that are not booked but ARE blocked -- what staff would
    have to override to use."""
    free_ignoring_blocks = set(generate_slots(conn, appt_date, include_blocked=True))
    free = set(generate_slots(conn, appt_date))
    return [t for t in slot_grid() if t in free_ignoring_blocks and t not in free]
