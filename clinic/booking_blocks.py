"""Staff-defined booking blocks: days (or a time range within a day) on which
NEW appointments cannot be created -- the doctor is away, the clinic is being
renovated, and so on.

The block itself is enforced where slots are generated and where bookings are
written (clinic/scheduling.py + the write handlers in clinic/intents.py), so
the WhatsApp agent never offers a blocked slot and no route can book into one
without an explicit override. Appointments that already exist inside a block
are NEVER touched automatically; `appointments_in_window` is how the UI shows
staff how many there are so they can deal with them.
"""

import re
from datetime import date, datetime

from clinic import scheduling

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^\d{2}:\d{2}$")
MAX_SPAN_DAYS = 366
MAX_REASON_LEN = 200


class BlockError(ValueError):
    """Bad input for a block; the message is meant for staff."""


def _valid_date(text):
    if not isinstance(text, str) or not _DATE.match(text):
        raise BlockError("Dates must look like 2026-10-05.")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise BlockError("That is not a real date: {}.".format(text))


def _valid_time(text):
    if not isinstance(text, str) or not _TIME.match(text):
        raise BlockError("Times must look like 09:30.")
    hours, minutes = int(text[:2]), int(text[3:])
    if hours > 23 or minutes > 59:
        raise BlockError("That is not a real time: {}.".format(text))
    return text


def normalize(start_date, end_date=None, start_time=None, end_time=None, reason=None):
    """Validated (start_date, end_date, start_time, end_time, reason); empty
    strings count as 'not given'. Times must come as a pair."""
    start_time = start_time or None
    end_time = end_time or None
    end_date = end_date or start_date
    first, last = _valid_date(start_date), _valid_date(end_date)
    if last < first:
        raise BlockError("The end date is before the start date.")
    if (last - first).days > MAX_SPAN_DAYS:
        raise BlockError("A block can cover at most {} days.".format(MAX_SPAN_DAYS))
    if bool(start_time) != bool(end_time):
        raise BlockError("Give both a start time and an end time, or neither (for the whole day).")
    if start_time:
        _valid_time(start_time)
        _valid_time(end_time)
        if scheduling._to_minutes(end_time) <= scheduling._to_minutes(start_time):
            raise BlockError("The end time must be after the start time.")
    reason = (reason or "").strip()
    if len(reason) > MAX_REASON_LEN:
        raise BlockError("Keep the reason under {} characters.".format(MAX_REASON_LEN))
    return start_date, end_date, start_time, end_time, reason


def appointments_in_window(conn, start_date, end_date, start_time=None, end_time=None):
    """Active (booked/confirmed) appointments that fall inside the window --
    the ones a new block would NOT automatically move or cancel."""
    rows = conn.execute(
        "SELECT a.id, a.appt_date, a.start_time, a.duration_minutes, "
        "COALESCE(p.name, a.patient_name) AS name, COALESCE(a.patient_phone, p.phone) AS phone "
        "FROM appointments a LEFT JOIN patients p ON p.id = a.patient_id "
        "WHERE a.status IN ('booked', 'confirmed') AND a.appt_date >= ? AND a.appt_date <= ? "
        "ORDER BY a.appt_date, a.start_time, a.id",
        (start_date, end_date),
    ).fetchall()
    out = []
    for row in rows:
        if start_time and end_time:
            start = scheduling._to_minutes(row["start_time"])
            end = start + row["duration_minutes"]
            if not (start < scheduling._to_minutes(end_time) and scheduling._to_minutes(start_time) < end):
                continue
        out.append({k: row[k] for k in ("id", "appt_date", "start_time", "name", "phone")})
    return out


def add_block(conn, start_date, end_date=None, start_time=None, end_time=None, reason=None, now=None):
    """Create a block. Returns {'id', ..., 'affected': [existing appointments
    inside it]} -- the caller shows that list."""
    start_date, end_date, start_time, end_time, reason = normalize(start_date, end_date, start_time, end_time, reason)
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO booking_blocks (start_date, end_date, start_time, end_time, reason, active, created_at) "
        "VALUES (?, ?, ?, ?, ?, 1, ?)",
        (start_date, end_date, start_time, end_time, reason, stamp),
    )
    conn.commit()
    return {
        "id": cur.lastrowid, "start_date": start_date, "end_date": end_date, "start_time": start_time,
        "end_time": end_time, "reason": reason,
        "affected": appointments_in_window(conn, start_date, end_date, start_time, end_time),
    }


def remove_block(conn, block_id):
    cur = conn.execute("UPDATE booking_blocks SET active = 0 WHERE id = ? AND active = 1", (block_id,))
    conn.commit()
    return bool(cur.rowcount)


def list_blocks(conn, today=None, include_past=False):
    """Active blocks, soonest first, each with the existing appointments that
    sit inside it. Blocks wholly in the past are hidden unless include_past."""
    today = today or date.today().isoformat()
    rows = conn.execute(
        "SELECT * FROM booking_blocks WHERE active = 1 ORDER BY start_date, COALESCE(start_time, ''), id"
    ).fetchall()
    blocks = []
    for row in rows:
        if row["end_date"] < today and not include_past:
            continue
        block = {k: row[k] for k in ("id", "start_date", "end_date", "start_time", "end_time", "reason")}
        block["affected"] = appointments_in_window(
            conn, row["start_date"], row["end_date"], row["start_time"], row["end_time"])
        blocks.append(block)
    return blocks


def describe(block):
    """'5 Oct 2026' / '5-7 Oct 2026', plus the time range when there is one."""
    first, last = date.fromisoformat(block["start_date"]), date.fromisoformat(block["end_date"])
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    if first == last:
        days = "{} {} {}".format(first.day, months[first.month - 1], first.year)
    else:
        days = "{} {} - {} {} {}".format(first.day, months[first.month - 1], last.day, months[last.month - 1], last.year)
    if block.get("start_time"):
        return "{}, {}-{}".format(days, block["start_time"], block["end_time"])
    return days + " (whole day)"
