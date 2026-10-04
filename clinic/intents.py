from datetime import date, timedelta

from clinic import scheduling, token_queue


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


def cancel_followup(conn, slots):
    followup_id = slots["followup_id"]
    conn.execute("UPDATE followups SET status = 'cancelled' WHERE id = ?", (followup_id,))
    return "followup", followup_id, {"status": "cancelled"}


def reschedule_followup(conn, slots):
    followup_id = slots["followup_id"]
    new_due_date = slots["new_due_date"]
    conn.execute("UPDATE followups SET due_date = ? WHERE id = ?", (new_due_date, followup_id))
    return "followup", followup_id, {"due_date": new_due_date}


def _check_slot(conn, appt_date, start_time, duration_minutes, override_block, exclude_appointment_id=None):
    """The confirm-time slot guard shared by book / reschedule / restore.
    A slot inside a staff-defined booking block is refused unless the caller
    passed the explicit override flag (a staff member who confirmed "Book
    anyway?"); a taken slot is always refused. Returns True when the block
    was overridden, so the audit payload can say so."""
    reason = scheduling.block_reason(conn, appt_date, start_time, duration_minutes)
    overridden = False
    if reason is not None:
        if not override_block:
            raise scheduling.SlotBlockedError(
                "{} at {} is inside a booking block{} -- pick another slot.".format(
                    appt_date, start_time, " ({})".format(reason) if reason else "")
            )
        overridden = True
    if not scheduling.is_slot_free(conn, appt_date, start_time, duration_minutes,
                                   exclude_appointment_id=exclude_appointment_id, ignore_blocks=True):
        raise scheduling.SlotConflictError(
            "{} at {} is no longer free -- pick another slot.".format(appt_date, start_time)
        )
    return overridden


def book_appointment(conn, slots):
    # A caller who isn't a registered patient yet can still book: patient_id
    # is None and the raw patient_name/patient_phone are kept as fallback
    # display fields (same "not found -> keep the raw text" convention
    # register_patient's own review-card flow already relies on).
    patient_id = slots.get("patient_id")
    patient_name = slots.get("patient_name") if not patient_id else None
    patient_phone = slots.get("patient_phone") if not patient_id else None
    appt_date = slots["appt_date"]
    start_time = slots["start_time"]
    duration_minutes = slots.get("duration_minutes") or scheduling.SLOT_MINUTES
    notes = slots.get("notes")

    # The double-booking guard, re-checked here at confirm-time (inside the
    # same transaction core.confirm() wraps this call in) against whatever
    # is booked *right now* -- not just what was free when the review card
    # was first shown to the human.
    overridden = _check_slot(conn, appt_date, start_time, duration_minutes, bool(slots.get("override_block")))

    cur = conn.execute(
        "INSERT INTO appointments "
        "(patient_id, patient_name, patient_phone, appt_date, start_time, duration_minutes, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (patient_id, patient_name, patient_phone, appt_date, start_time, duration_minutes, notes),
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
    }
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
        "SELECT duration_minutes FROM appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    duration_minutes = row["duration_minutes"] if row else scheduling.SLOT_MINUTES

    # Same confirm-time re-check as book_appointment, excluding this
    # appointment's own current row from the conflict check (rescheduling a
    # slot to the same time it already occupies must not count as
    # conflicting with itself).
    overridden = _check_slot(conn, new_date, new_time, duration_minutes, bool(slots.get("override_block")),
                             exclude_appointment_id=appointment_id)

    conn.execute(
        "UPDATE appointments SET appt_date = ?, start_time = ?, updated_at = datetime('now') WHERE id = ?",
        (new_date, new_time, appointment_id),
    )
    payload = {"appt_date": new_date, "start_time": new_time}
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
        "SELECT appt_date, start_time, duration_minutes, status FROM appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    if row is None:
        raise ValueError("Appointment #{} not found.".format(appointment_id))
    if row["status"] != "cancelled":
        raise ValueError("Appointment #{} is {}, not cancelled.".format(appointment_id, row["status"]))
    _check_slot(conn, row["appt_date"], row["start_time"], row["duration_minutes"], False,
                exclude_appointment_id=appointment_id)
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
