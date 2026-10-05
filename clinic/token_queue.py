"""Digital token queue: token numbers, queue position, and the staff queue
actions (check in / call / done / no-show).

A token is NOT stored. For a given date, an appointment's token number is
its rank ordered by (start_time, id) among that date's appointments whose
status is not 'cancelled'/'rescheduled'. Completed and no_show appointments
KEEP their rank, so tokens don't shift as the day progresses -- only a
cancellation, a reschedule away, or a new earlier-slot booking renumbers the
patients behind it. (clinic.appointments.last_notified_token records what a
patient was last *told*, so notify.py can fire "your token changed" only
when it really did.)

"N ahead of you" = how many appointments with a smaller token are still
outstanding (status booked/confirmed -- which covers checked_in and
in_consultation, since queue_state only ever lives on those). A completed or
no_show appointment ahead of you does not count.

One queue per branch per day (a branch has one doctor on duty at a time):
every function takes an optional `branch_id`, None meaning the default branch,
so a one-branch database behaves exactly as before. With more than one branch
a token label carries the branch code ("B-T04") so tokens never clash.

Everything here is deterministic SQL/Python -- no model ever produces a
token, a position, or an identity.
"""

from clinic import branches

OUTSTANDING_STATUSES = ("booked", "confirmed")
UNRANKED_STATUSES = ("cancelled", "rescheduled")

QUEUE_STATES = (None, "checked_in", "in_consultation")


class QueueActionError(Exception):
    """A queue action that doesn't make sense for the appointment's current
    state (e.g. checking in a cancelled appointment). The message is meant
    to be shown to staff as-is."""


def format_token(number, branch_code=None):
    """'T-04', or 'B-T04' when the branch code is given."""
    if branch_code:
        return "{}-T{:02d}".format(branch_code, number)
    return "T-{:02d}".format(number)


def _label_code(conn, branch_id):
    """The branch code to show on tokens: only when there is more than one branch."""
    if not branches.multi_branch(conn):
        return None
    branch = branches.get_branch(conn, branch_id)
    return branch["code"] if branch else None


def day_queue(conn, appt_date, branch_id=None):
    """Every ranked appointment at one branch on `appt_date`, in token order, as
    plain dicts. `ahead` is None for appointments that are no longer outstanding
    (completed / no_show)."""
    default = branches.default_branch_id(conn)
    branch_id = branches.resolve(conn, branch_id)
    rows = conn.execute(
        """
        SELECT a.id, a.appt_date, a.start_time, a.duration_minutes, a.status,
               a.queue_state, a.patient_id, a.last_notified_token,
               COALESCE(a.branch_id, ?) AS branch_id, a.doctor_id,
               COALESCE(p.name, a.patient_name) AS name,
               COALESCE(a.patient_phone, p.phone) AS phone
        FROM appointments a
        LEFT JOIN patients p ON p.id = a.patient_id
        WHERE a.appt_date = ? AND a.status NOT IN ('cancelled', 'rescheduled')
          AND COALESCE(a.branch_id, ?) = ?
        ORDER BY a.start_time, a.id
        """,
        (default, appt_date, default, branch_id),
    ).fetchall()
    code = _label_code(conn, branch_id)

    entries = []
    outstanding_so_far = 0
    for index, row in enumerate(rows, start=1):
        entry = dict(row)
        entry["token"] = index
        entry["token_label"] = format_token(index, code)
        entry["outstanding"] = row["status"] in OUTSTANDING_STATUSES
        entry["ahead"] = outstanding_so_far if entry["outstanding"] else None
        if entry["outstanding"]:
            outstanding_so_far += 1
        entries.append(entry)
    return entries


def queue_entry(conn, appointment_id):
    """The queue entry (token, position, ...) for one appointment, or None if
    it doesn't exist or is cancelled/rescheduled (no token)."""
    row = conn.execute("SELECT appt_date, branch_id FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
    if row is None:
        return None
    for entry in day_queue(conn, row["appt_date"], row["branch_id"]):
        if entry["id"] == appointment_id:
            return entry
    return None


def token_for(conn, appointment_id):
    entry = queue_entry(conn, appointment_id)
    return entry["token"] if entry else None


def find_by_token(conn, appt_date, number, branch_id=None):
    """The entry holding token `number` at a branch on `appt_date`, or None."""
    for entry in day_queue(conn, appt_date, branch_id):
        if entry["token"] == number:
            return entry
    return None


def entries_for_patient(conn, appt_date, patient_id, branch_id=None):
    return [e for e in day_queue(conn, appt_date, branch_id) if patient_id is not None and e["patient_id"] == patient_id]


def queue_options(conn, appt_date, branch_id=None):
    """Outstanding entries for a review-card dropdown, labelled
    'T-04 Sunita Devi 09:15'."""
    return [
        {
            "id": e["id"],
            "appt_date": e["appt_date"],
            "start_time": e["start_time"],
            "token": e["token"],
            "label": "{} {} {}".format(e["token_label"], e["name"] or "(no name)", e["start_time"]),
        }
        for e in day_queue(conn, appt_date, branch_id)
        if e["outstanding"]
    ]


def next_to_call(conn, appt_date, branch_id=None):
    """Who "call next" means: the lowest-token patient who has checked in and
    is not already with the doctor. None if nobody is waiting in the room."""
    for entry in day_queue(conn, appt_date, branch_id):
        if entry["outstanding"] and entry["queue_state"] == "checked_in":
            return entry
    return None


def queue_snapshot(conn, appt_date, branch_id=None):
    """Flat, display-ready summary for the read-only queue_status intent."""
    entries = day_queue(conn, appt_date, branch_id)
    serving = next((e for e in entries if e["outstanding"] and e["queue_state"] == "in_consultation"), None)
    nxt = next_to_call(conn, appt_date, branch_id)
    if nxt is None:
        nxt = next((e for e in entries if e["outstanding"] and e["queue_state"] != "in_consultation"), None)
    waiting = sum(1 for e in entries if e["outstanding"] and e["queue_state"] != "in_consultation")
    branch = branches.get_branch(conn, branches.resolve(conn, branch_id))
    return {
        "date": appt_date,
        "branch": branch["name"] if branch and branches.multi_branch(conn) else None,
        "now_serving": "{} {}".format(serving["token_label"], serving["name"] or "").strip() if serving else None,
        "next_up": "{} {}".format(nxt["token_label"], nxt["name"] or "").strip() if nxt else None,
        "waiting": waiting,
        "total_today": len(entries),
    }


# ---------------------------------------------------------------------------
# Queue actions (called by the write handlers in clinic/intents.py)
# ---------------------------------------------------------------------------

def _load(conn, appointment_id):
    if appointment_id is None:
        raise QueueActionError("No appointment selected.")
    row = conn.execute(
        "SELECT id, appt_date, status, queue_state, branch_id FROM appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    if row is None:
        raise QueueActionError("Appointment #{} not found.".format(appointment_id))
    return row


def _require_outstanding(row, verb):
    if row["status"] not in OUTSTANDING_STATUSES:
        raise QueueActionError("Cannot {}: appointment is {}.".format(verb, row["status"]))


def _label(conn, appointment_id):
    entry = queue_entry(conn, appointment_id)
    return entry["token_label"] if entry else "#{}".format(appointment_id)


def check_in(conn, appointment_id):
    row = _load(conn, appointment_id)
    _require_outstanding(row, "check in")
    if row["queue_state"] == "in_consultation":
        raise QueueActionError("Already in consultation.")
    conn.execute(
        "UPDATE appointments SET queue_state = 'checked_in', updated_at = datetime('now') WHERE id = ?",
        (appointment_id,),
    )
    return {"status": row["status"], "queue_state": "checked_in"}


def start_consultation(conn, appointment_id):
    row = _load(conn, appointment_id)
    _require_outstanding(row, "call")
    default = branches.default_branch_id(conn)
    other = conn.execute(
        "SELECT id FROM appointments WHERE appt_date = ? AND queue_state = 'in_consultation' "
        "AND status IN ('booked', 'confirmed') AND id != ? AND COALESCE(branch_id, ?) = COALESCE(?, ?)",
        (row["appt_date"], appointment_id, default, row["branch_id"], default),
    ).fetchone()
    if other is not None:
        raise QueueActionError(
            "{} is still in consultation -- mark them done first.".format(_label(conn, other["id"]))
        )
    conn.execute(
        "UPDATE appointments SET queue_state = 'in_consultation', updated_at = datetime('now') WHERE id = ?",
        (appointment_id,),
    )
    return {"status": row["status"], "queue_state": "in_consultation"}


def complete(conn, appointment_id):
    row = _load(conn, appointment_id)
    _require_outstanding(row, "mark done")
    conn.execute(
        "UPDATE appointments SET status = 'completed', queue_state = NULL, updated_at = datetime('now') WHERE id = ?",
        (appointment_id,),
    )
    return {"status": "completed", "queue_state": None}


def mark_no_show(conn, appointment_id):
    row = _load(conn, appointment_id)
    _require_outstanding(row, "mark no-show")
    conn.execute(
        "UPDATE appointments SET status = 'no_show', queue_state = NULL, updated_at = datetime('now') WHERE id = ?",
        (appointment_id,),
    )
    return {"status": "no_show", "queue_state": None}
