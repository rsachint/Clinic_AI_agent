from clinic import entity_resolution, intents, queries, scheduling, token_queue
from clinic.adapters.base import Citation


class LocalSQLiteAdapter:
    """Default/local implementation of both ClinicalAdapter and OpsAdapter.
    Delegates to the existing, already-tested functions rather than
    reimplementing them -- this file is an adapter seam, not new logic."""

    def register_patient(self, conn, slots):
        return intents.register_patient(conn, slots)

    def record_visit(self, conn, slots):
        return intents.record_visit(conn, slots)

    def set_followup(self, conn, slots):
        return intents.set_followup(conn, slots)

    def cancel_followup(self, conn, slots):
        return intents.cancel_followup(conn, slots)

    def reschedule_followup(self, conn, slots):
        return intents.reschedule_followup(conn, slots)

    def nearest_pending_followup(self, conn, patient_id):
        return queries.nearest_pending_followup(conn, patient_id)

    def pending_followups_for_patient(self, conn, patient_id):
        return queries.pending_followups_for_patient(conn, patient_id)

    def book_appointment(self, conn, slots):
        return intents.book_appointment(conn, slots)

    def cancel_appointment(self, conn, slots):
        return intents.cancel_appointment(conn, slots)

    def reschedule_appointment(self, conn, slots):
        return intents.reschedule_appointment(conn, slots)

    def restore_appointment(self, conn, slots):
        return intents.restore_appointment(conn, slots)

    def check_in(self, conn, slots):
        return intents.queue_check_in(conn, slots)

    def start_consultation(self, conn, slots):
        return intents.queue_call_next(conn, slots)

    def complete(self, conn, slots):
        return intents.queue_mark_done(conn, slots)

    def mark_no_show(self, conn, slots):
        return intents.queue_mark_no_show(conn, slots)

    def queue_for_date(self, conn, appt_date, branch_id=None):
        return token_queue.day_queue(conn, appt_date, branch_id)

    def queue_entry(self, conn, appointment_id):
        return token_queue.queue_entry(conn, appointment_id)

    def queue_options(self, conn, appt_date, branch_id=None):
        return token_queue.queue_options(conn, appt_date, branch_id)

    def find_by_token(self, conn, appt_date, number, branch_id=None):
        return token_queue.find_by_token(conn, appt_date, number, branch_id)

    def next_to_call(self, conn, appt_date, branch_id=None):
        return token_queue.next_to_call(conn, appt_date, branch_id)

    def queue_snapshot(self, conn, appt_date, branch_id=None):
        return token_queue.queue_snapshot(conn, appt_date, branch_id)

    def available_slots(self, conn, appt_date, branch_id=None):
        return scheduling.generate_slots(conn, appt_date, branch_id=branch_id)

    def upcoming_appointments_for_patient(self, conn, patient_id):
        return queries.upcoming_appointments_for_patient(conn, patient_id)

    def upcoming_appointments_named(self, conn, name, branch_id=None):
        return queries.upcoming_appointments_named(conn, name, branch_id=branch_id)

    def appointments_named(self, conn, name, start_date=None, end_date=None, branch_id=None):
        return queries.appointments_named(conn, name, start_date, end_date, branch_id=branch_id)

    def appointment_option(self, conn, appointment_id):
        return queries.appointment_option(conn, appointment_id)

    def next_appointment_for_patient(self, conn, patient_id):
        return queries.next_appointment_for_patient(conn, patient_id)

    def calendar_appointments(self, conn, start_date, end_date, branch_id=None):
        return queries.calendar_appointments(conn, start_date, end_date, branch_id)

    def scheduled_appointments(self, conn, start_date, end_date=None, branch_id=None):
        return queries.scheduled_appointments(conn, start_date, end_date, branch_id)

    def resolve_patient(self, conn, query, top_n=3):
        return entity_resolution.resolve_patient(conn, query, top_n)

    def resolve_patient_by_phone(self, conn, wa_id):
        return entity_resolution.resolve_patient_by_phone(conn, wa_id)

    def missed_followups(self, conn, as_of=None):
        return queries.missed_followups(conn, as_of)

    def patient_counts(self, conn):
        return queries.patient_counts(conn)

    def patient_lookup(self, conn, patient_id):
        row = conn.execute(
            "SELECT id, name, phone, age FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()
        return dict(row) if row else None

    def register_staff(self, conn, slots):
        return intents.register_staff(conn, slots)

    def log_attendance(self, conn, slots):
        return intents.log_attendance(conn, slots)

    def log_expense(self, conn, slots):
        return intents.log_expense(conn, slots)

    def resolve_staff(self, conn, query, top_n=3):
        return entity_resolution.resolve_staff(conn, query, top_n)

    def day_end_cashbook(self, conn, on_date=None):
        return queries.day_end_cashbook(conn, on_date)

    def attendance_register(self, conn, on_date=None):
        return queries.attendance_register(conn, on_date)

    def citation(self):
        return Citation(source="local clinic records", as_of="updated just now")
