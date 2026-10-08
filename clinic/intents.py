from datetime import date, timedelta

from clinic import booking_phone, branches, entity_resolution, scheduling, token_queue

# A booking under a name and phone that are already registered reuses that
# patient instead of registering a second copy (a heard "Kavitha" for a
# registered "Kavita" is the same person on the same phone).
_SAME_PERSON = 0.8


def _patient_for_new_booking(conn, name, phone):
    """(patient_id, registered_now) for a booking made under a raw name and phone.
    Both a name and a 10-digit phone are needed to make a patient record;
    without them (a walk-in known by name only) there is none: (None, False).
    The same person on the same phone is reused; anyone else (a relative on a
    shared phone) is registered as a new patient."""
    name = (name or "").strip()
    phone = (phone or "").strip()
    digits = entity_resolution.last10_digits(phone)
    if not name or len(digits) != 10:
        return None, False
    for row in conn.execute("SELECT id, name, phone FROM patients").fetchall():
        if entity_resolution.last10_digits(row["phone"]) == digits \
                and entity_resolution.similarity(name, row["name"]) >= _SAME_PERSON:
            return row["id"], False
    cur = conn.execute("INSERT INTO patients (name, phone) VALUES (?, ?)", (name, phone))
    return cur.lastrowid, True


def register_patient(conn, slots):
    name = slots["name"]
    phone = slots["phone"]
    age = slots.get("age")
    cur = conn.execute(
        "INSERT INTO patients (name, phone, age) VALUES (?, ?, ?)",
        (name, phone, age),
    )
    patient_id = cur.lastrowid
    return "patient", patient_id, {"name": name, "phone": phone, "age": age}


def record_visit(conn, slots):
    patient_id = slots["patient_id"]
    visit_date = slots.get("visit_date") or date.today().isoformat()
    notes = slots.get("notes")
    fee_paise = round(slots["fee_rupees"] * 100)
    cur = conn.execute(
        "INSERT INTO visits (patient_id, visit_date, fee_paise, notes) VALUES (?, ?, ?, ?)",
        (patient_id, visit_date, fee_paise, notes),
    )
    visit_id = cur.lastrowid
    return "visit", visit_id, {
        "patient_id": patient_id,
        "visit_date": visit_date,
        "fee_paise": fee_paise,
        "notes": notes,
    }


def set_followup(conn, slots):
    patient_id = slots["patient_id"]
    visit_id = slots.get("visit_id")
    due_date = slots.get("due_date")
    if not due_date:
        due_date = (date.today() + timedelta(days=slots["days_from_now"])).isoformat()
    cur = conn.execute(
        "INSERT INTO followups (patient_id, visit_id, due_date) VALUES (?, ?, ?)",
        (patient_id, visit_id, due_date),
    )
    followup_id = cur.lastrowid
    return "followup", followup_id, {"patient_id": patient_id, "due_date": due_date}


def _refuse_slot_followup(conn, followup_id):
    """A follow-up that holds a booked slot (clinic/followups.py) is changed through
    its appointment, which keeps the calendar, the patient and the reminders in step.
    The older date-only follow-ups (no appointment) are unaffected."""
    row = conn.execute("SELECT appointment_id FROM followups WHERE id = ?", (followup_id,)).fetchone()
    if row is not None and row["appointment_id"]:
        raise ValueError("That follow-up has a booked appointment: move or cancel the appointment "
                         "(Queue tab) and the follow-up follows it.")


def cancel_followup(conn, slots):
    followup_id = slots["followup_id"]
    _refuse_slot_followup(conn, followup_id)
    conn.execute("UPDATE followups SET status = 'cancelled' WHERE id = ?", (followup_id,))
    return "followup", followup_id, {"status": "cancelled"}


def reschedule_followup(conn, slots):
    followup_id = slots["followup_id"]
    _refuse_slot_followup(conn, followup_id)
    new_due_date = slots["new_due_date"]
    conn.execute("UPDATE followups SET due_date = ? WHERE id = ?", (new_due_date, followup_id))
    return "followup", followup_id, {"due_date": new_due_date}


def _check_slot(conn, appt_date, start_time, duration_minutes, override_block, exclude_appointment_id=None,
                branch_id=None, check_hours=False):
    """The confirm-time slot guard shared by book / reschedule / restore.
    A slot inside a staff-defined booking block is refused unless the caller
    passed the explicit override flag (a staff member who confirmed "Book
    anyway?"); a taken slot is always refused. Returns True when the block
    was overridden, so the audit payload can say so.

    With several branches the guard runs against ONE branch's bookings, blocks
    and doctor schedule. `check_hours` (set when the caller named a branch)
    also refuses a time at which the branch has no doctor on duty -- an
    overridable refusal, like a block."""
    overridden = False
    if check_hours and scheduling.within_doctor_hours(conn, appt_date, start_time, duration_minutes, branch_id) is None:
        if not override_block:
            raise scheduling.SlotBlockedError(
                "{} has no doctor on duty on {} at {} -- pick another slot.".format(
                    branches.branch_label(conn, branch_id), appt_date, start_time))
        overridden = True
    reason = scheduling.block_reason(conn, appt_date, start_time, duration_minutes, branch_id)
    if reason is not None:
        if not override_block:
            raise scheduling.SlotBlockedError(
                "{} at {} is inside a booking block{} -- pick another slot.".format(
                    appt_date, start_time, " ({})".format(reason) if reason else "")
            )
        overridden = True
    if not scheduling.is_slot_free(conn, appt_date, start_time, duration_minutes,
                                   exclude_appointment_id=exclude_appointment_id, ignore_blocks=True,
                                   branch_id=branch_id):
        raise scheduling.SlotConflictError(
            "{} at {} is no longer free -- pick another slot.".format(appt_date, start_time)
        )
    return overridden


def book_appointment(conn, slots):
    # A caller who isn't a registered patient yet can still book. With a name
    # AND a phone they are registered as a patient by this booking (or matched
    # to the patient already on that phone), unless nobody reviewed it (the
    # automatic WhatsApp path passes `unattended`). With a name only (a walk-in),
    # patient_id stays None and the raw patient_name/patient_phone are kept as
    # display fields.
    #
    # Every NEW booking needs a valid phone (clinic/booking_phone.py): the
    # registered patient's own, or the one given here. Checked first, before
    # anything is registered or the slot is looked at; rescheduling and
    # cancelling never come through here.
    booking_phone.check(conn, slots)
    patient_id = slots.get("patient_id")
    registered_now = False
    if not patient_id and not slots.get("unattended"):
        patient_id, registered_now = _patient_for_new_booking(conn, slots.get("patient_name"), slots.get("patient_phone"))
    patient_name = slots.get("patient_name") if not patient_id else None
    patient_phone = slots.get("patient_phone") if not patient_id else None
    if patient_id and not booking_phone.patient_phone_on_file(conn, patient_id)[1]:
        # A registered patient whose own number is unusable: the phone given on this booking is the
        # one it is made under (kept on the appointment only; patients.phone is never rewritten here).
        patient_phone = booking_phone.valid_phone(slots.get("patient_phone")) or None
    appt_date = slots["appt_date"]
    start_time = slots["start_time"]
    duration_minutes = slots.get("duration_minutes") or scheduling.SLOT_MINUTES
    notes = slots.get("notes")

    # The double-booking guard, re-checked here at confirm-time (inside the
    # same transaction core.confirm() wraps this call in) against whatever
    # is booked *right now* -- not just what was free when the review card
    # was first shown to the human.
    named_branch = slots.get("branch_id")
    branch_id = branches.resolve(conn, named_branch)
    overridden = _check_slot(conn, appt_date, start_time, duration_minutes, bool(slots.get("override_block")),
                             branch_id=branch_id, check_hours=named_branch not in (None, ""))
    doctor_id = branches.doctor_at(conn, branch_id, appt_date, start_time)

    cur = conn.execute(
        "INSERT INTO appointments "
        "(patient_id, patient_name, patient_phone, appt_date, start_time, duration_minutes, notes, branch_id, doctor_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (patient_id, patient_name, patient_phone, appt_date, start_time, duration_minutes, notes, branch_id, doctor_id),
    )
    appointment_id = cur.lastrowid
    payload = {
        "patient_id": patient_id,
        "patient_name": patient_name,
        "patient_phone": patient_phone,
        "appt_date": appt_date,
        "start_time": start_time,
        "duration_minutes": duration_minutes,
        "notes": notes,
        "branch_id": branch_id,
        "doctor_id": doctor_id,
    }
    if registered_now:
        payload["registered_patient_id"] = patient_id        # the audit trail shows this booking created the patient
    if overridden:
        payload["override_block"] = True
    return "appointment", appointment_id, payload


def _require_active(conn, appointment_id):
    """Guard for the automatic / staff-button / undo paths (they pass
    slots['require_active']): the appointment must still be booked or
    confirmed at the moment of the write, whatever it was when the request
    was made. Voice and review-card flows don't pass the flag, so their
    behaviour is unchanged."""
    row = conn.execute("SELECT status, queue_state FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
    if row is None:
        raise ValueError("Appointment #{} not found.".format(appointment_id))
    if row["status"] not in ("booked", "confirmed"):
        raise ValueError("The appointment is already {}.".format(row["status"]))
    return row


def cancel_appointment(conn, slots):
    appointment_id = slots["appointment_id"]
    if slots.get("require_active"):
        _require_active(conn, appointment_id)
    conn.execute(
        "UPDATE appointments SET status = 'cancelled', updated_at = datetime('now') WHERE id = ?",
        (appointment_id,),
    )
    return "appointment", appointment_id, {"status": "cancelled"}


def reschedule_appointment(conn, slots):
    appointment_id = slots["appointment_id"]
    new_date = slots["appt_date"]
    new_time = slots["start_time"]
    if slots.get("require_active"):
        _require_active(conn, appointment_id)

    row = conn.execute(
        "SELECT duration_minutes, branch_id FROM appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    duration_minutes = row["duration_minutes"] if row else scheduling.SLOT_MINUTES
    # Moving to another branch is just a reschedule that names a different one;
    # otherwise the appointment keeps its own branch.
    named_branch = slots.get("branch_id")
    branch_id = branches.resolve(conn, named_branch if named_branch not in (None, "") else (row["branch_id"] if row else None))

    # Same confirm-time re-check as book_appointment, excluding this
    # appointment's own current row from the conflict check (rescheduling a
    # slot to the same time it already occupies must not count as
    # conflicting with itself).
    overridden = _check_slot(conn, new_date, new_time, duration_minutes, bool(slots.get("override_block")),
                             exclude_appointment_id=appointment_id, branch_id=branch_id,
                             # `restore` (an Undo): putting an appointment back where it was is never refused
                             # for the doctor's hours; it may have been booked outside them in the first place.
                             check_hours=named_branch not in (None, "") and not slots.get("restore"))
    doctor_id = branches.doctor_at(conn, branch_id, new_date, new_time)

    conn.execute(
        "UPDATE appointments SET appt_date = ?, start_time = ?, branch_id = ?, doctor_id = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (new_date, new_time, branch_id, doctor_id, appointment_id),
    )
    payload = {"appt_date": new_date, "start_time": new_time, "branch_id": branch_id, "doctor_id": doctor_id}
    if overridden:
        payload["override_block"] = True
    return "appointment", appointment_id, payload


def restore_appointment(conn, slots):
    """Reinstate a CANCELLED appointment in its original slot (Undo of an
    automatic cancellation). Refused -- nothing changes -- if the appointment
    is not cancelled any more or the slot has been taken / blocked since."""
    appointment_id = slots["appointment_id"]
    status = slots.get("status") or "booked"
    if status not in ("booked", "confirmed"):
        raise ValueError("An appointment can only be restored as booked or confirmed.")
    row = conn.execute(
        "SELECT appt_date, start_time, duration_minutes, status, branch_id FROM appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    if row is None:
        raise ValueError("Appointment #{} not found.".format(appointment_id))
    if row["status"] != "cancelled":
        raise ValueError("Appointment #{} is {}, not cancelled.".format(appointment_id, row["status"]))
    _check_slot(conn, row["appt_date"], row["start_time"], row["duration_minutes"], False,
                exclude_appointment_id=appointment_id, branch_id=row["branch_id"])
    conn.execute(
        "UPDATE appointments SET status = ?, queue_state = NULL, updated_at = datetime('now') WHERE id = ?",
        (status, appointment_id),
    )
    return "appointment", appointment_id, {"status": status, "restored": True}


# -- Day-of queue actions -------------------------------------------------
# Same (conn, slots) -> (entity_type, entity_id, audit payload) shape as every
# other write handler, so each goes through core.propose/core.confirm and
# lands in audit_log. The state-machine rules live in clinic/token_queue.py;
# a QueueActionError raised there rolls back core.confirm's transaction and is
# surfaced to staff as a normal failed approve.

def _queue_action(action, fn, conn, slots):
    appointment_id = slots.get("appointment_id")
    payload = fn(conn, appointment_id)
    payload["action"] = action
    return "appointment", appointment_id, payload


def queue_check_in(conn, slots):
    return _queue_action("check_in", token_queue.check_in, conn, slots)


def queue_call_next(conn, slots):
    return _queue_action("call", token_queue.start_consultation, conn, slots)


def queue_mark_done(conn, slots):
    return _queue_action("done", token_queue.complete, conn, slots)


def queue_mark_no_show(conn, slots):
    return _queue_action("no_show", token_queue.mark_no_show, conn, slots)


def register_staff(conn, slots):
    name = slots["name"]
    role = slots.get("role")
    phone = slots.get("phone")
    cur = conn.execute(
        "INSERT INTO staff (name, role, phone) VALUES (?, ?, ?)",
        (name, role, phone),
    )
    staff_id = cur.lastrowid
    return "staff", staff_id, {"name": name, "role": role, "phone": phone}


def log_attendance(conn, slots):
    staff_id = slots["staff_id"]
    attendance_date = slots.get("attendance_date") or date.today().isoformat()
    status = slots["status"]
    conn.execute(
        "INSERT INTO attendance (staff_id, attendance_date, status) VALUES (?, ?, ?) "
        "ON CONFLICT(staff_id, attendance_date) DO UPDATE SET status = excluded.status",
        (staff_id, attendance_date, status),
    )
    row = conn.execute(
        "SELECT id FROM attendance WHERE staff_id = ? AND attendance_date = ?",
        (staff_id, attendance_date),
    ).fetchone()
    return "attendance", row["id"], {
        "staff_id": staff_id,
        "attendance_date": attendance_date,
        "status": status,
    }


def log_expense(conn, slots):
    expense_date = slots.get("expense_date") or date.today().isoformat()
    description = slots["description"]
    paid_to = slots.get("paid_to")
    amount_paise = round(slots["amount_rupees"] * 100)
    cur = conn.execute(
        "INSERT INTO expenses (expense_date, description, amount_paise, paid_to) VALUES (?, ?, ?, ?)",
        (expense_date, description, amount_paise, paid_to),
    )
    expense_id = cur.lastrowid
    return "expense", expense_id, {
        "expense_date": expense_date,
        "description": description,
        "amount_paise": amount_paise,
        "paid_to": paid_to,
    }


HANDLERS = {
    "register_patient": register_patient,
    "register_staff": register_staff,
    "record_visit": record_visit,
    "set_followup": set_followup,
    "cancel_followup": cancel_followup,
    "reschedule_followup": reschedule_followup,
    "book_appointment": book_appointment,
    "cancel_appointment": cancel_appointment,
    "reschedule_appointment": reschedule_appointment,
    "restore_appointment": restore_appointment,
    "queue_check_in": queue_check_in,
    "queue_call_next": queue_call_next,
    "queue_mark_done": queue_mark_done,
    "queue_mark_no_show": queue_mark_no_show,
    "log_attendance": log_attendance,
    "log_expense": log_expense,
}
