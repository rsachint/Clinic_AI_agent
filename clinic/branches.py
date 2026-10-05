"""Branches, doctors and their schedules: one brand, several branches, doctors
who may work at more than one of them.

A branch is bookable exactly when one of its doctors is scheduled there (a
`doctor_schedules` row for that weekday), the branch is open, and nothing in
`booking_blocks` covers the time. At most one doctor is on duty at a branch at
any time (checked here when a schedule is saved), so a branch has one queue
and one set of slots per day; a doctor cannot be at two branches at once.

Everything is plain SQL and Python, in the same style as scheduling.py. A
database with a single branch behaves exactly as the app did before branches
existed: every function that takes a `branch_id` accepts None, meaning "the
default branch".
"""

import sqlite3
from datetime import date as _date

DEFAULT_BRANCH_SETTING = "default_branch_id"
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_HOURS = ("09:00", "13:00"), ("16:00", "20:00")


class BranchError(ValueError):
    """A branch / doctor / schedule change that doesn't make sense. The
    message is meant to be shown to staff as-is."""


def _to_minutes(hhmm):
    h, m = str(hhmm).split(":")
    return int(h) * 60 + int(m)


def _valid_time(text):
    try:
        h, m = str(text).split(":")
        return 0 <= int(h) <= 23 and 0 <= int(m) <= 59 and len(m) == 2
    except (ValueError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------

def _row(row):
    return dict(row) if row is not None else None


def list_branches(conn, include_inactive=False):
    sql = "SELECT * FROM branches"
    if not include_inactive:
        sql += " WHERE active = 1"
    sql += " ORDER BY sort_order, id"
    try:
        return [dict(r) for r in conn.execute(sql).fetchall()]
    except sqlite3.OperationalError:
        return []


def get_branch(conn, branch_id):
    try:
        return _row(conn.execute("SELECT * FROM branches WHERE id = ?", (branch_id,)).fetchone())
    except sqlite3.OperationalError:
        return None


def get_branch_by_code(conn, code):
    code = (code or "").strip().upper()
    return _row(conn.execute("SELECT * FROM branches WHERE upper(code) = ?", (code,)).fetchone()) if code else None


def default_branch_id(conn):
    """The branch used when none is named: the 'default_branch_id' setting if
    it points at an active branch, else the first active branch, else 1."""
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (DEFAULT_BRANCH_SETTING,)).fetchone()
        if row and row[0] and str(row[0]).isdigit():
            found = conn.execute("SELECT id FROM branches WHERE id = ? AND active = 1", (int(row[0]),)).fetchone()
            if found:
                return found[0]
        first = conn.execute("SELECT id FROM branches WHERE active = 1 ORDER BY sort_order, id LIMIT 1").fetchone()
        if first:
            return first[0]
    except sqlite3.OperationalError:
        pass
    return 1


def resolve(conn, branch_id):
    """None -> the default branch; anything else is returned as an int."""
    return int(branch_id) if branch_id not in (None, "") else default_branch_id(conn)


def multi_branch(conn):
    """True when more than one branch is active (tokens then carry the branch code)."""
    return len(list_branches(conn)) > 1


def branch_label(conn, branch_id):
    branch = get_branch(conn, resolve(conn, branch_id))
    return branch["name"] if branch else "Branch"


def add_branch(conn, code, name, address=None, maps_url=None, phone=None, pin_code=None, color=None):
    code = (code or "").strip().upper()
    name = (name or "").strip()
    if not code or not name:
        raise BranchError("A branch needs a code and a name.")
    if len(code) > 6 or not code.replace("-", "").isalnum():
        raise BranchError("The branch code must be 1-6 letters or digits (for example A or SP56).")
    if get_branch_by_code(conn, code):
        raise BranchError("There is already a branch with code {}.".format(code))
    _check_pin(pin_code)
    order = (conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM branches").fetchone()[0])
    cur = conn.execute(
        "INSERT INTO branches (code, name, address, maps_url, phone, pin_code, color, sort_order) VALUES (?,?,?,?,?,?,?,?)",
        (code, name, address, maps_url, phone, (pin_code or None), color, order))
    conn.commit()
    return cur.lastrowid


_EDITABLE = ("code", "name", "address", "maps_url", "phone", "pin_code", "color", "closed_message", "sort_order")


def _check_pin(pin_code):
    if pin_code and not (str(pin_code).isdigit() and len(str(pin_code)) == 6):
        raise BranchError("A PIN code is six digits.")


def update_branch(conn, branch_id, **fields):
    if get_branch(conn, branch_id) is None:
        raise BranchError("That branch does not exist.")
    changes = {k: v for k, v in fields.items() if k in _EDITABLE}
    if "code" in changes:
        code = (changes["code"] or "").strip().upper()
        other = get_branch_by_code(conn, code)
        if not code or (other and other["id"] != int(branch_id)):
            raise BranchError("That branch code is empty or already used.")
        changes["code"] = code
    if "name" in changes and not (changes["name"] or "").strip():
        raise BranchError("A branch needs a name.")
    if "pin_code" in changes:
        _check_pin(changes["pin_code"])
        changes["pin_code"] = changes["pin_code"] or None
    if not changes:
        return
    conn.execute("UPDATE branches SET {} WHERE id = ?".format(", ".join(k + " = ?" for k in changes)),
                 list(changes.values()) + [branch_id])
    conn.commit()


def set_status(conn, branch_id, status, reason=None, message=None):
    """Open or close a branch indefinitely (for a dated closure use a booking block)."""
    if status not in ("open", "closed"):
        raise BranchError("A branch is either open or closed.")
    if get_branch(conn, branch_id) is None:
        raise BranchError("That branch does not exist.")
    conn.execute("UPDATE branches SET status = ?, closed_reason = ?, closed_message = ? WHERE id = ?",
                 (status, reason if status == "closed" else None, message if status == "closed" else None, branch_id))
    conn.commit()


def set_default_branch(conn, branch_id):
    if not get_branch(conn, branch_id):
        raise BranchError("That branch does not exist.")
    conn.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (DEFAULT_BRANCH_SETTING, str(int(branch_id))))
    conn.commit()


def deactivate_branch(conn, branch_id):
    """Retire a branch. Refused while it still has upcoming appointments or is the
    only active branch: nothing is ever orphaned."""
    if len(list_branches(conn)) <= 1:
        raise BranchError("You cannot remove the only branch.")
    default = default_branch_id(conn)
    upcoming = conn.execute(
        "SELECT COUNT(*) FROM appointments WHERE COALESCE(branch_id, ?) = ? AND status IN ('booked','confirmed') "
        "AND appt_date >= date('now','localtime')", (default, branch_id)).fetchone()[0]
    if upcoming:
        raise BranchError("This branch still has {} upcoming appointment(s). Move or cancel them first.".format(upcoming))
    conn.execute("UPDATE branches SET active = 0 WHERE id = ?", (branch_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Doctors and their schedules
# ---------------------------------------------------------------------------

def list_doctors(conn, include_inactive=False):
    sql = "SELECT * FROM doctors" + ("" if include_inactive else " WHERE active = 1") + " ORDER BY id"
    try:
        return [dict(r) for r in conn.execute(sql).fetchall()]
    except sqlite3.OperationalError:
        return []


def get_doctor(conn, doctor_id):
    try:
        return _row(conn.execute("SELECT * FROM doctors WHERE id = ?", (doctor_id,)).fetchone())
    except sqlite3.OperationalError:
        return None


def doctor_label(conn, doctor_id):
    doctor = get_doctor(conn, doctor_id) if doctor_id else None
    return doctor["name"] if doctor else None


def add_doctor(conn, name, title=None, specialty=None):
    name = (name or "").strip()
    if not name:
        raise BranchError("A doctor needs a name.")
    cur = conn.execute("INSERT INTO doctors (name, title, specialty) VALUES (?, ?, ?)", (name, title, specialty))
    conn.commit()
    return cur.lastrowid


def update_doctor(conn, doctor_id, name=None, title=None, specialty=None, active=None):
    if get_doctor(conn, doctor_id) is None:
        raise BranchError("That doctor does not exist.")
    if name is not None and not name.strip():
        raise BranchError("A doctor needs a name.")
    sets, values = [], []
    for column, value in (("name", name and name.strip()), ("title", title), ("specialty", specialty),
                          ("active", None if active is None else (1 if active else 0))):
        if value is not None:
            sets.append(column + " = ?")
            values.append(value)
    if sets:
        conn.execute("UPDATE doctors SET {} WHERE id = ?".format(", ".join(sets)), values + [doctor_id])
        conn.commit()


def list_schedule(conn, branch_id=None, doctor_id=None):
    sql = ("SELECT s.*, d.name AS doctor_name, b.code AS branch_code, b.name AS branch_name FROM doctor_schedules s "
           "JOIN doctors d ON d.id = s.doctor_id JOIN branches b ON b.id = s.branch_id WHERE 1 = 1")
    params = []
    if branch_id is not None:
        sql += " AND s.branch_id = ?"
        params.append(branch_id)
    if doctor_id is not None:
        sql += " AND s.doctor_id = ?"
        params.append(doctor_id)
    sql += " ORDER BY s.branch_id, s.weekday, s.start_time"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def add_schedule(conn, doctor_id, branch_id, weekday, start_time, end_time, valid_from=None, valid_to=None):
    """Put a doctor at a branch for one weekday window. Refused when it would put two
    doctors at the same branch at once, or the same doctor at two branches at once."""
    if get_doctor(conn, doctor_id) is None:
        raise BranchError("That doctor does not exist.")
    if get_branch(conn, branch_id) is None:
        raise BranchError("That branch does not exist.")
    try:
        weekday = int(weekday)
    except (TypeError, ValueError):
        raise BranchError("Pick a weekday.")
    if not 0 <= weekday <= 6:
        raise BranchError("Pick a weekday.")
    if not (_valid_time(start_time) and _valid_time(end_time)) or _to_minutes(start_time) >= _to_minutes(end_time):
        raise BranchError("Use times like 09:00 and 13:00, with the end after the start.")
    start, end = _to_minutes(start_time), _to_minutes(end_time)
    for row in list_schedule(conn):
        if row["weekday"] != weekday or not _validity_overlaps(row, valid_from, valid_to):
            continue
        if not (start < _to_minutes(row["end_time"]) and _to_minutes(row["start_time"]) < end):
            continue
        if row["branch_id"] == int(branch_id) and row["doctor_id"] != int(doctor_id):
            raise BranchError("{} is already at {} on {} {}-{}: only one doctor per branch at a time.".format(
                row["doctor_name"], row["branch_name"], WEEKDAY_NAMES[weekday], row["start_time"], row["end_time"]))
        if row["doctor_id"] == int(doctor_id) and row["branch_id"] != int(branch_id):
            raise BranchError("{} is already at {} on {} {}-{}: a doctor cannot be at two branches at once.".format(
                row["doctor_name"], row["branch_name"], WEEKDAY_NAMES[weekday], row["start_time"], row["end_time"]))
        if row["doctor_id"] == int(doctor_id) and row["branch_id"] == int(branch_id):
            raise BranchError("That overlaps an existing window for the same doctor and branch.")
    cur = conn.execute(
        "INSERT INTO doctor_schedules (doctor_id, branch_id, weekday, start_time, end_time, valid_from, valid_to) "
        "VALUES (?,?,?,?,?,?,?)", (doctor_id, branch_id, weekday, start_time, end_time, valid_from, valid_to))
    conn.commit()
    return cur.lastrowid


def _validity_overlaps(row, valid_from, valid_to):
    a_from, a_to = row.get("valid_from") or "0000-00-00", row.get("valid_to") or "9999-99-99"
    b_from, b_to = valid_from or "0000-00-00", valid_to or "9999-99-99"
    return a_from <= b_to and b_from <= a_to


def remove_schedule(conn, schedule_id):
    cur = conn.execute("DELETE FROM doctor_schedules WHERE id = ?", (schedule_id,))
    conn.commit()
    return cur.rowcount > 0


def doctor_windows(conn, branch_id, appt_date):
    """[(start_minute, end_minute, doctor_id)] of the doctor windows at `branch_id`
    on `appt_date` (an ISO date), in time order. Empty when the branch is closed,
    retired, or has no doctor that weekday."""
    branch_id = resolve(conn, branch_id)
    try:
        branch = conn.execute("SELECT status, active FROM branches WHERE id = ?", (branch_id,)).fetchone()
        if branch is not None and (branch["status"] == "closed" or not branch["active"]):
            return []
        weekday = _date.fromisoformat(str(appt_date)).weekday()
        rows = conn.execute(
            "SELECT s.doctor_id, s.start_time, s.end_time FROM doctor_schedules s "
            "JOIN doctors d ON d.id = s.doctor_id "
            "WHERE s.branch_id = ? AND s.weekday = ? AND d.active = 1 "
            "AND (s.valid_from IS NULL OR s.valid_from <= ?) AND (s.valid_to IS NULL OR s.valid_to >= ?) "
            "ORDER BY s.start_time", (branch_id, weekday, appt_date, appt_date)).fetchall()
    except (sqlite3.OperationalError, ValueError):
        return []
    return [(_to_minutes(r["start_time"]), _to_minutes(r["end_time"]), r["doctor_id"]) for r in rows]


def doctor_at(conn, branch_id, appt_date, start_time):
    """The doctor on duty at that branch at that time, or None."""
    t = _to_minutes(start_time)
    for start, end, doctor_id in doctor_windows(conn, branch_id, appt_date):
        if start <= t < end:
            return doctor_id
    return None


def hours_summary(conn, branch_id, appt_date):
    """'09:00-13:00, 16:00-20:00' for one branch on one date, or 'closed'."""
    windows = doctor_windows(conn, branch_id, appt_date)
    if not windows:
        return "closed"
    return ", ".join("{:02d}:{:02d}-{:02d}:{:02d}".format(s // 60, s % 60, e // 60, e % 60) for s, e, _ in windows)


# ---------------------------------------------------------------------------
# "Nearest branch" from a PIN code (no coordinates needed)
# ---------------------------------------------------------------------------

def pin_closeness(pin_a, pin_b):
    """A sort key: smaller = nearer. Indian PIN codes are hierarchical (zone,
    region, sorting district, then the delivery area), so the longer the shared
    leading prefix the nearer two places are; ties are broken by how close the
    numbers themselves are. Returns None when either PIN is missing or invalid."""
    a, b = str(pin_a or "").strip(), str(pin_b or "").strip()
    if not (a.isdigit() and b.isdigit() and len(a) == len(b) == 6):
        return None
    shared = 0
    for x, y in zip(a, b):
        if x != y:
            break
        shared += 1
    return (6 - shared, abs(int(a) - int(b)))


def nearest_branches(conn, pin_code, include_closed=True):
    """Active branches ordered nearest first for a patient's PIN code. Branches
    without a usable PIN go last, in their fixed order. Each item is the branch
    dict plus `near` (a short phrase such as 'same area') and `rank`."""
    branches = [b for b in list_branches(conn) if include_closed or b["status"] == "open"]
    keyed = []
    for position, branch in enumerate(branches):
        closeness = pin_closeness(pin_code, branch.get("pin_code"))
        keyed.append((closeness is None, closeness or (9, 0), position, branch, closeness))
    keyed.sort(key=lambda item: item[:3])
    ordered = []
    for rank, (_, _, _, branch, closeness) in enumerate(keyed, start=1):
        item = dict(branch, rank=rank, near=_near_phrase(closeness))
        ordered.append(item)
    return ordered


def _near_phrase(closeness):
    if closeness is None:
        return ""
    unshared = closeness[0]
    if unshared == 0:
        return "your PIN code"
    if unshared <= 1:
        return "very close"
    if unshared <= 2:
        return "close by"
    if unshared <= 3:
        return "nearby"
    return ""


# ---------------------------------------------------------------------------
# One-time example data for a real database (branches B and C, example doctors)
# ---------------------------------------------------------------------------

_SEED_FLAG = "branches_seeded"


def ensure_seed(conn):
    """On the first open of a real database create the example Branch B and Branch C
    and two more doctors (one shared between them), all editable in Settings. Runs
    once (a flag in app_settings); a database that already has more than one
    branch is left alone. Branch A / its doctor come from schema.sql."""
    try:
        flag = conn.execute("SELECT value FROM app_settings WHERE key = ?", (_SEED_FLAG,)).fetchone()
    except sqlite3.OperationalError:
        return False
    if flag:
        return False
    if len(list_branches(conn, include_inactive=True)) > 1:
        _mark_seeded(conn)
        return False
    b = add_branch(conn, "B", "Branch B", "Example address, edit in Settings", pin_code="122011", color="#2e9e5b")
    c = add_branch(conn, "C", "Branch C", "Example address, edit in Settings", pin_code="122018", color="#d9822b")
    rao = add_doctor(conn, "Dr. Rao", "Dr.", "General physician")
    iyer = add_doctor(conn, "Dr. Iyer", "Dr.", "General physician")
    # Dr. Rao: Mon/Wed/Fri at B and Tue/Thu/Sat at C. Dr. Iyer covers the other days at each.
    plan = {(rao, b): (0, 2, 4), (rao, c): (1, 3, 5), (iyer, b): (1, 3, 5), (iyer, c): (0, 2, 4)}
    for (doctor, branch), days in plan.items():
        for weekday in days:
            for start, end in _HOURS:
                add_schedule(conn, doctor, branch, weekday, start, end)
    _mark_seeded(conn)
    return True


def _mark_seeded(conn):
    conn.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (?, '1', datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value = '1'", (_SEED_FLAG,))
    conn.commit()


def backfill_branch(conn):
    """Give appointments from before branches existed the default branch (and the
    doctor on duty then, when there is one). Idempotent; returns how many rows changed."""
    default = default_branch_id(conn)
    rows = conn.execute("SELECT id, appt_date, start_time FROM appointments WHERE branch_id IS NULL").fetchall()
    for row in rows:
        conn.execute("UPDATE appointments SET branch_id = ?, doctor_id = COALESCE(doctor_id, ?) WHERE id = ?",
                     (default, doctor_at(conn, default, row["appt_date"], row["start_time"]), row["id"]))
    conn.commit()
    return len(rows)
