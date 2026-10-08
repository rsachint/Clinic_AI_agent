from datetime import date, timedelta

from clinic import branches


def _appointment_select(conn, with_patient_id=False):
    """The columns every appointment listing returns, plus the joins they need.
    `branch` / `doctor` are display names; `branch_id` is the appointment's own
    branch (an old row with none belongs to the default branch). `with_patient_id` adds the
    registered patient's id (None for a walk-in); the on-screen listings leave it out."""
    default = int(branches.default_branch_id(conn))
    columns = (
        "a.id, a.appt_date, a.start_time, a.duration_minutes, a.status, a.notes, "
        "COALESCE(p.name, a.patient_name) AS patient_name, COALESCE(p.phone, a.patient_phone) AS patient_phone, "
        "COALESCE(a.branch_id, {d}) AS branch_id, a.doctor_id, b.name AS branch, b.code AS branch_code, d.name AS doctor"
    ).format(d=default)
    if with_patient_id:
        columns += ", a.patient_id AS patient_id"
    joins = (
        "LEFT JOIN patients p ON p.id = a.patient_id "
        "LEFT JOIN branches b ON b.id = COALESCE(a.branch_id, {d}) "
        "LEFT JOIN doctors d ON d.id = a.doctor_id"
    ).format(d=default)
    return columns, joins, default


def missed_followups(conn, as_of=None):
    as_of = as_of or date.today().isoformat()
    return conn.execute(
        """
        SELECT f.id, p.name, p.phone, f.due_date
        FROM followups f
        JOIN patients p ON p.id = f.patient_id
        LEFT JOIN appointments a ON a.id = f.appointment_id
        WHERE f.status = 'pending'
          AND (
            (f.appointment_id IS NULL AND f.due_date <= ?)
            -- a follow-up with a booked slot is only overdue once its day is past
            -- (or the patient was marked a no-show), not while the visit is still ahead today
            OR (f.appointment_id IS NOT NULL AND (f.due_date < ? OR a.status = 'no_show'))
          )
        ORDER BY f.due_date
        """,
        (as_of, as_of),
    ).fetchall()


def day_end_cashbook(conn, on_date=None):
    on_date = on_date or date.today().isoformat()
    fees_paise = conn.execute(
        "SELECT COALESCE(SUM(fee_paise), 0) AS total FROM visits WHERE visit_date = ?",
        (on_date,),
    ).fetchone()["total"]
    expenses_paise = conn.execute(
        "SELECT COALESCE(SUM(amount_paise), 0) AS total FROM expenses WHERE expense_date = ?",
        (on_date,),
    ).fetchone()["total"]
    return {
        "date": on_date,
        "fees_paise": fees_paise,
        "expenses_paise": expenses_paise,
        "net_paise": fees_paise - expenses_paise,
    }


def nearest_pending_followup(conn, patient_id):
    """Which pending follow-up a bare 'cancel/reschedule my follow-up'
    command refers to, when a patient has more than one: the nearest due
    date. A best-effort heuristic, not a certainty -- surfaced as an
    editable dropdown on the review card (see static/review_card.js's
    "followup" field type) so a human can correct a wrong guess before
    approving. Returns the followup id, or None if there isn't one."""
    row = conn.execute(
        "SELECT id FROM followups WHERE patient_id = ? AND status = 'pending' ORDER BY due_date LIMIT 1",
        (patient_id,),
    ).fetchone()
    return row["id"] if row else None


def pending_followups_for_patient(conn, patient_id):
    """All of a patient's pending follow-ups, for the review card's
    "followup" dropdown (context.followups) to offer as alternatives to the
    best guess above."""
    return conn.execute(
        "SELECT id, due_date FROM followups WHERE patient_id = ? AND status = 'pending' ORDER BY due_date",
        (patient_id,),
    ).fetchall()


def upcoming_appointments_for_patient(conn, patient_id):
    """A patient's upcoming (booked/confirmed, not yet past) appointments,
    for the review card's "appointment" dropdown (context.appointments)."""
    return conn.execute(
        """
        SELECT id, appt_date, start_time, duration_minutes
        FROM appointments
        WHERE patient_id = ? AND status IN ('booked', 'confirmed')
          AND (appt_date > date('now') OR (appt_date = date('now') AND start_time >= time('now')))
        ORDER BY appt_date, start_time
        """,
        (patient_id,),
    ).fetchall()


def next_appointment_for_patient(conn, patient_id):
    """The single nearest upcoming appointment for a patient, or None --
    backs both the 'cancel/reschedule my appointment' best-guess and the
    read-only next_appointment intent ("when's this patient's next
    appointment")."""
    return conn.execute(
        """
        SELECT id, appt_date, start_time, duration_minutes, status
        FROM appointments
        WHERE patient_id = ? AND status IN ('booked', 'confirmed')
          AND (appt_date > date('now') OR (appt_date = date('now') AND start_time >= time('now')))
        ORDER BY appt_date, start_time
        LIMIT 1
        """,
        (patient_id,),
    ).fetchone()


def patient_counts(conn, today=None):
    """How many patients are registered: all of them, those added today, and
    those added in the last 7 days (today included). Read-only."""
    today = date.fromisoformat(today) if today else date.today()
    week_start = (today - timedelta(days=6)).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "COALESCE(SUM(CASE WHEN date(registered_at) = ? THEN 1 ELSE 0 END), 0) AS added_today, "
        "COALESCE(SUM(CASE WHEN date(registered_at) >= ? THEN 1 ELSE 0 END), 0) AS added_this_week "
        "FROM patients", (today.isoformat(), week_start)).fetchone()
    return {"total_patients": row["total"], "added_today": row["added_today"], "added_this_week": row["added_this_week"]}


def scheduled_appointments(conn, start_date, end_date=None, branch_id=None):
    """Booked/confirmed appointments in [start_date, end_date] -- backs the
    read-only list_appointments intent ("what's scheduled today/this
    week"). end_date defaults to start_date (a single day). `branch_id` limits
    it to one branch; None lists every branch."""
    end_date = end_date or start_date
    columns, joins, default = _appointment_select(conn)
    where, params = ["a.appt_date BETWEEN ? AND ?", "a.status IN ('booked', 'confirmed')"], [start_date, end_date]
    if branch_id is not None:
        where.append("COALESCE(a.branch_id, ?) = ?")
        params += [default, int(branch_id)]
    return conn.execute(
        "SELECT {} FROM appointments a {} WHERE {} ORDER BY a.appt_date, a.start_time".format(
            columns, joins, " AND ".join(where)),
        params,
    ).fetchall()


def attendance_register(conn, on_date=None):
    on_date = on_date or date.today().isoformat()
    return conn.execute(
        """
        SELECT s.name, a.status
        FROM attendance a
        JOIN staff s ON s.id = a.staff_id
        WHERE a.attendance_date = ?
        ORDER BY s.name
        """,
        (on_date,),
    ).fetchall()


def registered_names(conn):
    """Names the voice name-extractor can recognise without asking a model: every patient and every staff
    member. A name of ONE word is only taken by the matcher's stricter rule (clinic/nlu/llm_slots.py: exactly
    one person fits it and the sentence does not continue it with another name), so a lone first name is
    never mistaken for the start of a different, new person's name. Best effort: any database problem just
    means "no shortcut", never an error."""
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM patients") if r[0] and r[0].strip()]
        names += [r[0] for r in conn.execute("SELECT name FROM staff") if r[0] and r[0].strip()]
        return names
    except Exception:
        return []


def appointment_option(conn, appointment_id):
    """One booked appointment as a review-card option, or None: its id, date, time and length, plus what the
    card shows as text (who, phone, status, branch and doctor names; never the notes)."""
    columns, joins, _ = _appointment_select(conn)
    row = conn.execute(
        "SELECT {} FROM appointments a {} WHERE a.id = ? AND a.status IN ('booked', 'confirmed')".format(columns, joins),
        (appointment_id,),
    ).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "appt_date": row["appt_date"], "start_time": row["start_time"],
            "duration_minutes": row["duration_minutes"], "who": row["patient_name"],
            "patient_phone": row["patient_phone"], "status": row["status"],
            "branch_id": row["branch_id"], "branch": row["branch"], "doctor": row["doctor"]}


# What a person-name search lists: everything that actually happened or is still
# going to, but not what was cancelled or moved away.
NAMED_LIST_STATUSES = ("booked", "confirmed", "completed", "no_show")


def _name_score(name, who):
    """1.0 when the heard `name` is exactly the written `who` (or a single word equal to its first
    name: "Amit" for "Amit Dua"), otherwise 0.0."""
    from clinic.entity_resolution import similarity

    return similarity(name, who)


def _appointments_matching(conn, name, start, end, statuses, min_score, closeness, branch_id=None,
                           with_patient_id=False):
    """Appointments (with the person shown in `patient_name`, as in
    scheduled_appointments) in [start, end] with one of `statuses` (any, if
    empty) whose person sounds like `name`: (score, row) pairs of the
    best-fitting names only, in date and time order. Includes walk-ins whose
    name is only written on the appointment (no patient record) and names in
    the other script (Devanagari vs Roman). Others must score within
    `closeness` of the best."""
    name = (name or "").strip()
    if not name:
        return []
    columns, joins, default = _appointment_select(conn, with_patient_id)
    where, params = ["1 = 1"], []
    if branch_id is not None:
        where.append("COALESCE(a.branch_id, ?) = ?")
        params.extend([default, int(branch_id)])
    if statuses:
        where.append("a.status IN ({})".format(", ".join("?" * len(statuses))))
        params.extend(statuses)
    if start:
        where.append("a.appt_date >= ?")
        params.append(start)
    if end:
        where.append("a.appt_date <= ?")
        params.append(end)
    rows = conn.execute(
        "SELECT {} FROM appointments a {} WHERE {} ORDER BY a.appt_date, a.start_time".format(
            columns, joins, " AND ".join(where)),
        params,
    ).fetchall()
    scored = [(_name_score(name, r["patient_name"]), r) for r in rows if r["patient_name"]]
    scored = [(score, r) for score, r in scored if score >= min_score]
    if not scored:
        return []
    best = max(score for score, _ in scored)
    return [(score, r) for score, r in scored if best - score <= closeness]


def appointments_named(conn, name, start=None, end=None, statuses=NAMED_LIST_STATUSES,
                       min_score=0.6, closeness=0.08, branch_id=None):
    """One person's appointments, in date and time order, shaped exactly like
    scheduled_appointments' rows (so the on-screen table and the conversation
    memory work the same) -- backs "show Amit's appointments". Without `start` /
    `end` it spans every date, past and upcoming; cancelled ones are left out
    by default. The person is found by sound (see _appointments_matching), and a
    spoken first name alone finds every person with that first name."""
    # Who is meant is decided over every appointment, whatever its day or status;
    # only then is the person's list narrowed. Otherwise "Amit Anand" with nothing
    # on the day asked would quietly turn into whichever other Amit has something.
    matches = _appointments_matching(conn, name, None, None, (), min_score, closeness,
                                     branch_id=branch_id)
    return [
        dict(r) for _, r in matches
        if r["status"] in statuses and (not start or r["appt_date"] >= start) and (not end or r["appt_date"] <= end)
    ]


def upcoming_appointments_named(conn, name, today=None, min_score=0.6, closeness=0.08, branch_id=None):
    """Booked appointments from `today` on whose person sounds like `name`, best
    match first -- including walk-ins whose name is only written on the
    appointment (no patient record), and names in the other script (Devanagari
    vs Roman). Each row carries `who` (the name shown) and `score`. Only the
    best-fitting names are kept: others must score within `closeness` of the
    best. Today's earlier appointments are included, since staff do cancel those."""
    from datetime import date as _date

    today = today or _date.today().isoformat()
    matches = _appointments_matching(conn, name, today, None, ("booked", "confirmed"), min_score, closeness,
                                     branch_id=branch_id, with_patient_id=True)
    return [
        {"id": r["id"], "appt_date": r["appt_date"], "start_time": r["start_time"],
         "duration_minutes": r["duration_minutes"], "who": r["patient_name"], "score": round(score, 3),
         "patient_id": r["patient_id"], "patient_phone": r["patient_phone"],
         "branch_id": r["branch_id"], "branch": r["branch"], "branch_code": r["branch_code"],
         "doctor": r["doctor"], "status": r["status"]}
        for score, r in matches
    ]


def upcoming_appointments_by_patient_id(conn, patient_id, today=None, branch_id=None):
    """The booked / confirmed appointments from `today` on (today's earlier ones included, as in
    upcoming_appointments_named) of ONE registered patient, found by id and never by name, so two
    patients who share a name are never mixed. Same row shape as upcoming_appointments_named."""
    from datetime import date as _date

    today = today or _date.today().isoformat()
    columns, joins, default = _appointment_select(conn, with_patient_id=True)
    where, params = ["a.patient_id = ?", "a.status IN ('booked', 'confirmed')", "a.appt_date >= ?"], [patient_id, today]
    if branch_id is not None:
        where.append("COALESCE(a.branch_id, ?) = ?")
        params.extend([default, int(branch_id)])
    rows = conn.execute(
        "SELECT {} FROM appointments a {} WHERE {} ORDER BY a.appt_date, a.start_time, a.id".format(
            columns, joins, " AND ".join(where)),
        params,
    ).fetchall()
    return [
        {"id": r["id"], "appt_date": r["appt_date"], "start_time": r["start_time"],
         "duration_minutes": r["duration_minutes"], "who": r["patient_name"], "score": 1.0,
         "patient_id": r["patient_id"], "patient_phone": r["patient_phone"],
         "branch_id": r["branch_id"], "branch": r["branch"], "branch_code": r["branch_code"],
         "doctor": r["doctor"], "status": r["status"]}
        for r in rows
    ]


def appointment_patient_id(conn, appointment_id):
    """The registered patient an appointment belongs to, or None (a walk-in, or no such appointment)."""
    row = conn.execute("SELECT patient_id FROM appointments WHERE id = ?", (appointment_id,)).fetchone() \
        if appointment_id else None
    return row["patient_id"] if row else None


CALENDAR_STATUSES = ("booked", "confirmed", "completed", "no_show")


def calendar_appointments(conn, start_date, end_date, branch_id=None):
    """Appointments in [start_date, end_date] for the in-app calendar: not cancelled
    or moved, with the branch (name, code, colour) and doctor, in time order.
    `branch_id` limits it to one branch; None shows every branch."""
    columns, joins, default = _appointment_select(conn)
    where = ["a.appt_date BETWEEN ? AND ?", "a.status IN ({})".format(", ".join("?" * len(CALENDAR_STATUSES)))]
    params = [start_date, end_date] + list(CALENDAR_STATUSES)
    if branch_id is not None:
        where.append("COALESCE(a.branch_id, ?) = ?")
        params += [default, int(branch_id)]
    rows = conn.execute(
        "SELECT {}, b.color AS branch_color FROM appointments a {} WHERE {} ORDER BY a.appt_date, a.start_time, a.id".format(
            columns, joins, " AND ".join(where)),
        params,
    ).fetchall()
    return [dict(r) for r in rows]
