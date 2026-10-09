"""A small clinic with a SECRET planted in every column the read views must never expose, for the model-reads tests.
Not a test module itself.

`build(conn)` fills a database made by clinic.db.connect with: Branch A / B / C, three doctors (a rotating roster at
B: a permanent pattern plus dated rows), patients with Roman and Devanagari names (two Amits, two Rahuls), staff,
appointments (with notes), follow-ups (with a diagnosis), visits (with notes), expenses (with a payee), WhatsApp
messages and notifications (with bodies, ids and an error), a closure (with its message), audit and activity rows with
payload secrets, and a settings row holding a token. Fixed dates: "today" is Friday 2026-10-09 (`TODAY`).
"""
from datetime import date

from clinic import branches

TODAY = date(2026, 10, 9)
NOW_HHMM = "10:00"

# One marker per excluded column. The read views must never carry any of them, however the query is written.
SECRETS = {
    "appointments.notes": "SECRETAPPTNOTE",
    "visits.notes": "SECRETVISITNOTE",
    "followups.diagnosis": "SECRETDIAGNOSIS",
    "expenses.paid_to": "SECRETPAYEE",
    "notifications.body": "SECRETBODY",
    "notifications.wa_id": "919000000099",
    "notifications.error": "SECRETERROR",
    "notifications.interactive_json": "SECRETINTERACTIVE",
    "notifications.template_json": "SECRETTEMPLATE",
    "wa_messages.raw_text": "SECRETRAWTEXT",
    "wa_sessions.slots_json": "SECRETSESSION",
    "branches.maps_url": "SECRETMAPSURL",
    "branches.closed_message": "SECRETCLOSEDMESSAGE",
    "closures.message": "SECRETCLOSUREMESSAGE",
    "closure_moves.error": "SECRETMOVEERROR",
    "audit_log.payload_json": "SECRETAUDITPAYLOAD",
    "patient_activity.wa_id": "919000000088",
    "patient_activity.detail": "SECRETACTIVITYDETAIL",
    "patient_activity.meta_json": "SECRETACTIVITYMETA",
    "app_settings.value": "SECRETSETTINGTOKEN",
    "proposals.slots_json": "SECRETPROPOSAL",
}


def build(conn):
    """Populate `conn` (from clinic.db.connect). Returns {"A": id, "B": id, "C": id, "mehta": id, "rao": id, "iyer": id}."""
    ids = {"A": 1, "mehta": 1}
    ids["B"] = branches.add_branch(conn, "B", "Branch B", "Sector 56", maps_url=SECRETS["branches.maps_url"], phone="0124111222",
                                   pin_code="122011")
    ids["C"] = branches.add_branch(conn, "C", "Branch C", "MG Road", pin_code="122018")
    ids["rao"] = branches.add_doctor(conn, "Dr. Rao", "Dr.", "Paediatrics")
    ids["iyer"] = branches.add_doctor(conn, "Dr. Iyer", "Dr.", "General physician")
    # Branch A: Dr. Mehta 09-13 and 16-20 every day (schema.sql). Branch B has a ROTATING roster like the live clinic:
    # Dr. Rao Mon-Fri until Sun 11 Oct, Dr. Iyer Mon-Fri for the week 12-18 Oct, Dr. Rao again from Mon 19 Oct, and Dr. Iyer on
    # Saturdays all year. Branch C is switched off.
    for weekday in range(5):
        branches.add_schedule(conn, ids["rao"], ids["B"], weekday, "10:00", "14:00", valid_to="2026-10-11")
        branches.add_schedule(conn, ids["iyer"], ids["B"], weekday, "10:00", "14:00", valid_from="2026-10-12", valid_to="2026-10-18")
        branches.add_schedule(conn, ids["rao"], ids["B"], weekday, "10:00", "14:00", valid_from="2026-10-19")
    branches.add_schedule(conn, ids["iyer"], ids["B"], 5, "09:00", "12:00")
    branches.add_schedule(conn, ids["iyer"], ids["C"], 6, "09:00", "12:00")      # Sundays at C, which is closed below
    conn.execute("UPDATE branches SET status = 'closed', closed_reason = 'Renovation', closed_message = ? WHERE id = ?",
                 (SECRETS["branches.closed_message"], ids["C"]))

    patients = [("Amit Dua", "9811122233", 45), ("Amit Anand", "9811144455", 38), ("राहुल शर्मा", "9876543210", 41),
                ("Rahul Sharma", "9876546543", 29), ("Priya Shah", "9123499999", 34), ("Sunita Devi", "919000000004", 60)]
    for name, phone, age in patients:
        conn.execute("INSERT INTO patients (name, phone, age, registered_at) VALUES (?, ?, ?, '2026-10-01 04:30:00')", (name, phone, age))
    conn.execute("INSERT INTO staff (name, role, phone, branch_id) VALUES ('Seema', 'nurse', '9000000100', ?)", (ids["A"],))
    conn.execute("INSERT INTO staff (name, role, phone) VALUES ('Ravi', 'receptionist', '9000000101')")
    conn.execute("INSERT INTO attendance (staff_id, attendance_date, status) VALUES (1, '2026-10-09', 'present'), (2, '2026-10-09', 'leave')")

    note = SECRETS["appointments.notes"]
    rows = [  # patient_id, name, phone, date, time, status, branch, doctor, created_at (UTC text)
        (1, None, None, "2026-10-12", "11:00", "booked", ids["A"], 1, "2026-10-09 04:30:00"),
        (2, None, None, "2026-10-12", "16:00", "booked", ids["A"], 1, "2026-10-09 05:00:00"),
        (3, None, None, "2026-10-10", "10:00", "booked", None, 1, "2026-10-08 18:29:00"),          # NULL branch = the default branch
        (4, None, None, "2026-10-10", "11:00", "confirmed", ids["A"], 1, "2026-10-08 18:29:00"),
        (5, None, None, "2026-09-29", "11:00", "cancelled", ids["A"], 1, "2026-09-25 04:30:00"),
        (1, None, None, "2026-10-01", "10:30", "cancelled", ids["B"], ids["rao"], "2026-09-25 04:30:00"),
        (None, "Walk In Wali", "9555500000", "2026-10-09", "12:00", "booked", ids["A"], 1, "2026-10-09 04:00:00"),
        (6, None, None, "2026-10-13", "10:00", "rescheduled", ids["B"], ids["rao"], "2026-10-05 04:30:00"),
    ]
    for patient_id, name, phone, day, time, status, branch, doctor, created in rows:
        conn.execute("INSERT INTO appointments (patient_id, patient_name, patient_phone, appt_date, start_time, duration_minutes, status, "
                     "notes, created_at, branch_id, doctor_id) VALUES (?, ?, ?, ?, ?, 30, ?, ?, ?, ?, ?)",
                     (patient_id, name, phone, day, time, status, note, created, branch, doctor))
    conn.execute("INSERT INTO followups (patient_id, due_date, due_time, status, doctor_id, branch_id, appointment_id, diagnosis) "
                 "VALUES (1, '2026-10-20', '10:00', 'pending', 1, ?, 1, ?)", (ids["A"], SECRETS["followups.diagnosis"]))
    conn.execute("INSERT INTO followups (patient_id, due_date, status, diagnosis) VALUES (2, '2026-10-05', 'missed', ?)",
                 (SECRETS["followups.diagnosis"],))
    for patient_id, day, paise in ((1, "2026-10-05", 50000), (2, "2026-10-06", 30000), (5, "2026-10-08", 45050), (4, "2026-09-20", 100000)):
        conn.execute("INSERT INTO visits (patient_id, visit_date, fee_paise, notes) VALUES (?, ?, ?, ?)",
                     (patient_id, day, paise, SECRETS["visits.notes"]))
    for day, text, paise in (("2026-10-01", "Clinic rent", 5000000), ("2026-10-02", "electricity", 120000)):
        conn.execute("INSERT INTO expenses (expense_date, description, amount_paise, paid_to) VALUES (?, ?, ?, ?)",
                     (day, text, paise, SECRETS["expenses.paid_to"]))
    conn.execute("INSERT INTO notifications (appointment_id, wa_id, event, dedup_key, body, status, error, created_at, sent_at, "
                 "interactive_json, template_json) VALUES (1, ?, 'booking_confirmed', 'k1', ?, 'sent', ?, '2026-10-08 04:00:00', "
                 "'2026-10-08 04:30:00', ?, ?)",
                 (SECRETS["notifications.wa_id"], SECRETS["notifications.body"], SECRETS["notifications.error"],
                  SECRETS["notifications.interactive_json"], SECRETS["notifications.template_json"]))
    conn.execute("INSERT INTO notifications (appointment_id, wa_id, event, dedup_key, body, status) "
                 "VALUES (NULL, ?, 'conv_reply', 'k2', ?, 'sent')", (SECRETS["notifications.wa_id"], SECRETS["notifications.body"]))
    conn.execute("INSERT INTO wa_messages (wa_message_id, wa_id, message_type, raw_text) VALUES ('w1', ?, 'text', ?)",
                 (SECRETS["notifications.wa_id"], SECRETS["wa_messages.raw_text"]))
    conn.execute("INSERT INTO wa_sessions (wa_id, slots_json) VALUES (?, ?)", (SECRETS["notifications.wa_id"], SECRETS["wa_sessions.slots_json"]))
    conn.execute("INSERT INTO closures (branch_id, start_date, end_date, reason, message, status) VALUES (?, '2026-10-12', '2026-10-14', "
                 "'Doctor on leave', ?, 'applied')", (ids["B"], SECRETS["closures.message"]))
    conn.execute("INSERT INTO closure_moves (closure_id, appointment_id, action, from_date, from_time, result, error) "
                 "VALUES (1, 2, 'move', '2026-10-12', '16:00', 'done', ?)", (SECRETS["closure_moves.error"],))
    conn.execute("INSERT INTO closure_moves (closure_id, appointment_id, action, from_date, from_time, result) "
                 "VALUES (1, 6, 'cancel', '2026-10-12', '10:30', 'done')")
    conn.execute("INSERT INTO booking_blocks (start_date, end_date, reason, active, branch_id) VALUES ('2026-10-12', '2026-10-14', 'Doctor on leave', 1, ?)",
                 (ids["B"],))
    conn.execute("INSERT INTO proposals (intent, slots_json) VALUES ('book_appointment', ?)", (SECRETS["proposals.slots_json"],))
    conn.execute("INSERT INTO audit_log (logged_at, intent, entity_type, entity_id, payload_json) VALUES ('2026-10-08 04:30:00', "
                 "'book_appointment', 'appointment', 1, ?)", (SECRETS["audit_log.payload_json"],))
    conn.execute("INSERT INTO patient_activity (patient_id, wa_id, patient_name, event, source, detail, created_at, meta_json) "
                 "VALUES (1, ?, 'Amit Dua', 'auto_booked', 'whatsapp-agent', ?, '2026-10-08 10:00:00', ?)",
                 (SECRETS["patient_activity.wa_id"], SECRETS["patient_activity.detail"], SECRETS["patient_activity.meta_json"]))
    conn.execute("INSERT INTO app_settings (key, value) VALUES ('whatsapp_access_token', ?)", (SECRETS["app_settings.value"],))
    conn.commit()
    return ids
