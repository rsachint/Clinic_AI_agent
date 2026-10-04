CREATE TABLE IF NOT EXISTS patients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    age INTEGER,
    registered_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- fees stored in paise (integer) to avoid float rounding on money
CREATE TABLE IF NOT EXISTS visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    visit_date TEXT NOT NULL,
    fee_paise INTEGER NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS followups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    visit_id INTEGER REFERENCES visits(id),
    due_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'missed', 'cancelled')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);

-- Real appointment/time-slot scheduling, separate from `followups` (which is
-- a bare-date post-visit recall, no time-of-day, and is not a calendar).
-- patient_id is nullable: a caller who isn't a registered patient yet can
-- still book a slot, using the fallback patient_name/patient_phone columns
-- (same "not found -> raw text fallback" convention register_patient uses,
-- rather than blocking the booking on registration happening first).
CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER REFERENCES patients(id),
    patient_name TEXT,
    patient_phone TEXT,
    appt_date TEXT NOT NULL,       -- 'YYYY-MM-DD', matching due_date/visit_date's plain-TEXT convention
    start_time TEXT NOT NULL,      -- 'HH:MM', 24-hour
    duration_minutes INTEGER NOT NULL DEFAULT 30,
    status TEXT NOT NULL DEFAULT 'booked'
        CHECK (status IN ('booked', 'confirmed', 'cancelled', 'rescheduled', 'completed', 'no_show')),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT,
    -- Live-queue state for the day-of flow: NULL (not arrived) / 'checked_in'
    -- / 'in_consultation'. `status` still owns the lifecycle (completed,
    -- no_show, ...). Validated in code, not by a CHECK, so a fresh DB and one
    -- upgraded by clinic/db.py's ensure_columns() have the identical shape.
    queue_state TEXT,
    -- The token number last communicated to this patient, so a token_changed
    -- notification fires only when the number actually changed for them.
    -- (Tokens themselves are NOT stored: see clinic/token_queue.py.)
    last_notified_token INTEGER
);

CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT,
    phone TEXT
);

CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id INTEGER NOT NULL REFERENCES staff(id),
    attendance_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('present', 'half_day', 'absent', 'leave')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (staff_id, attendance_date)
);

CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expense_date TEXT NOT NULL,
    description TEXT NOT NULL,
    amount_paise INTEGER NOT NULL,
    paid_to TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- a proposal is the model's parsed reading of a command; it has no effect on
-- patients/visits/etc until confirm() runs its handler inside a transaction
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    intent TEXT NOT NULL,
    slots_json TEXT NOT NULL,
    source_text TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'rejected')),
    resolved_at TEXT,
    result_entity_type TEXT,
    result_entity_id INTEGER
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at TEXT NOT NULL DEFAULT (datetime('now')),
    proposal_id INTEGER REFERENCES proposals(id),
    intent TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);

-- an inbound WhatsApp message, from receipt through to (optionally) becoming
-- a proposal. This sits *before* core.propose/core.confirm -- it never
-- writes to patients/followups/etc itself; approving a row just calls
-- propose+confirm the same way the dashboard's voice flow already does.
CREATE TABLE IF NOT EXISTS wa_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_message_id TEXT NOT NULL UNIQUE,
    wa_id TEXT NOT NULL,
    patient_id INTEGER REFERENCES patients(id),
    received_at TEXT NOT NULL DEFAULT (datetime('now')),
    message_type TEXT NOT NULL CHECK (message_type IN ('text', 'audio')),
    raw_text TEXT,
    media_id TEXT,
    intent TEXT,
    slots_json TEXT,
    status TEXT NOT NULL DEFAULT 'received'
        CHECK (status IN ('received', 'classified', 'needs_human_reply', 'approved', 'rejected', 'dismissed', 'error')),
    proposal_id INTEGER REFERENCES proposals(id),
    error_text TEXT,
    resolved_at TEXT,
    -- 1 when the WhatsApp conversation agent answered this message with
    -- replies only (no proposal for staff): it is excluded from the actionable
    -- inbox. Added to existing databases by clinic/db.py's ensure_columns().
    agent_handled INTEGER DEFAULT 0
);

-- One row per receipt acknowledgment actually sent, so a burst of messages
-- from one sender gets a single reply instead of one per message. Not an
-- audit record -- rows can be deleted (e.g. if the send itself failed).
CREATE TABLE IF NOT EXISTS wa_acks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_id TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Outbox of patient notifications (see clinic/notify.py). A row is created
-- (idempotently, via dedup_key) when something patient-visible happens, and
-- sent later by flush(). Not an audit record; status/attempts are updated
-- in place.
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER REFERENCES appointments(id),
    wa_id TEXT,
    event TEXT NOT NULL,
    dedup_key TEXT NOT NULL UNIQUE,
    language TEXT,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sent', 'dry_run', 'blocked_no_window', 'failed', 'skipped_no_phone')),
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at TEXT,
    -- JSON spec of reply buttons / a list message to send with `body` (see
    -- clinic/whatsapp.py build_interactive_body); NULL for plain text. Added to
    -- existing databases by clinic/db.py's ensure_columns().
    interactive_json TEXT
);

-- Per-sender state of the WhatsApp conversation agent (clinic/conversation.py).
-- The agent only COLLECTS details here; it never writes appointments. `goal`
-- and `slots_json` reset after 30 idle minutes (expires_at); `mode` persists
-- ('human' = staff took over, the agent stays silent). All times are the
-- clinic's local wall clock, as text.
CREATE TABLE IF NOT EXISTS wa_sessions (
    wa_id TEXT PRIMARY KEY,
    goal TEXT CHECK (goal IS NULL OR goal IN ('book', 'reschedule', 'cancel')),
    step TEXT,
    slots_json TEXT NOT NULL DEFAULT '{}',
    language TEXT,
    mode TEXT NOT NULL DEFAULT 'agent' CHECK (mode IN ('agent', 'human')),
    confusion_count INTEGER NOT NULL DEFAULT 0,
    turn_count INTEGER NOT NULL DEFAULT 0,
    rate_window_start TEXT,
    updated_at TEXT,
    expires_at TEXT
);

-- A slot reserved for the patient who just confirmed a request, until staff
-- resolve it or `expires_at`. Only affects what is OFFERED to other senders;
-- the confirm-time double-booking guard (clinic/scheduling.py) is untouched.
CREATE TABLE IF NOT EXISTS slot_holds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_id TEXT NOT NULL,
    wa_message_id INTEGER,
    appt_date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- Google Calendar one-way sync (clinic/gcal_sync.py). All additive; none of
-- this is source-of-truth data -- it can be dropped and rebuilt from
-- `appointments` by a full reconcile.
--
-- appointment -> the Google event that mirrors it.
CREATE TABLE IF NOT EXISTS calendar_events (
    appointment_id INTEGER PRIMARY KEY,
    gcal_event_id TEXT NOT NULL,
    calendar_id TEXT NOT NULL,
    appt_date TEXT NOT NULL,       -- the day the event is on in Google
    last_synced_at TEXT,
    last_error TEXT,
    status TEXT NOT NULL DEFAULT 'synced' CHECK (status IN ('synced', 'error'))
);

-- Pending sync work, queued by the post-write hook / "Sync now" / the
-- scheduler and drained by gcal_sync.drain(). kind: 'date' (target =
-- YYYY-MM-DD), 'appointment' (target = appointment id), 'full' (today..+30d).
CREATE TABLE IF NOT EXISTS calendar_sync_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('date', 'appointment', 'full')),
    target TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'done', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT
);

-- A few facts that must outlive pruned queue rows: last success, last error,
-- when the periodic full reconcile was last queued.
CREATE TABLE IF NOT EXISTS calendar_sync_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

-- Automatic WhatsApp appointment actions (clinic/auto_policy.py,
-- clinic/auto_actions.py). All additive. None of these is the audit trail --
-- audit_log (below) stays the immutable record of every write.
--
-- Small key/value settings: auto_appointments_enabled ('1'/'0', default on)
-- and auto_daily_cap (default 40). See clinic/settings.py.
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

-- Staff-defined "no new appointments" windows. NULL start_time/end_time =
-- the whole day(s); with times = that range on EACH date from start_date to
-- end_date. Removing a block just sets active = 0.
CREATE TABLE IF NOT EXISTS booking_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    start_time TEXT,
    end_time TEXT,
    reason TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Every time a patient initiates an appointment event (and every staff direct
-- edit / undo), one row. event: requested, auto_booked, auto_cancelled,
-- auto_rescheduled, escalated, blocked, conflict, undone, staff_booked,
-- staff_cancelled, staff_rescheduled. source: whatsapp-agent | staff. Validated
-- in code (no CHECK) so a new event never needs a table rebuild. created_at is
-- the clinic's local wall clock. meta_json carries what Undo needs.
CREATE TABLE IF NOT EXISTS patient_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER REFERENCES patients(id),
    wa_id TEXT,
    patient_name TEXT,
    appointment_id INTEGER,
    event TEXT NOT NULL,
    source TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    proposal_id INTEGER,
    meta_json TEXT,
    undone INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_patient_activity_patient ON patient_activity (patient_id);
CREATE INDEX IF NOT EXISTS idx_patient_activity_created ON patient_activity (created_at);

-- audit_log is append-only: no code path should ever update or delete a row,
-- and these triggers make that true even if a bug tries to
CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is immutable');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is immutable');
END;
