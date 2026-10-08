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
    completed_at TEXT,
    -- A follow-up with a real booked slot (clinic/followups.py): the doctor advised
    -- a return visit at this date + time. appointment_id is the calendar slot that
    -- holds it; NULL = a legacy date-only recall (no slot, no reminders). Added to
    -- existing databases by clinic/db.py's ensure_columns().
    due_time TEXT,                  -- 'HH:MM'
    doctor_id INTEGER REFERENCES doctors(id),
    branch_id INTEGER REFERENCES branches(id),
    appointment_id INTEGER REFERENCES appointments(id),
    -- INTERNAL ONLY, staff-editable. Never copied into any patient-facing message.
    diagnosis TEXT,
    batch_id INTEGER                -- the "Schedule follow-ups" batch that created it
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
    last_notified_token INTEGER,
    -- Which branch and which doctor this appointment is with (multi-branch).
    -- NULL = the default branch (older rows are back-filled by db.py).
    branch_id INTEGER REFERENCES branches(id),
    doctor_id INTEGER REFERENCES doctors(id)
);

CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT,
    phone TEXT,
    branch_id INTEGER                -- where this person normally works (NULL = any)
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
    interactive_json TEXT,
    -- JSON spec of the Meta-approved template to send INSTEAD of `body` when the
    -- patient is outside WhatsApp's 24-hour window (see clinic/whatsapp.py
    -- build_template_body); NULL = no template, so such a row waits as blocked.
    template_json TEXT
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
    expires_at TEXT NOT NULL,
    branch_id INTEGER                      -- NULL = the default branch
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
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- Who the block applies to. NULL branch = every branch (a brand-wide
    -- holiday); NULL doctor = every doctor. A branch closure sets branch_id;
    -- a doctor's leave sets doctor_id.
    branch_id INTEGER,
    doctor_id INTEGER
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


-- ---------------------------------------------------------------------------
-- Multi-branch (one brand, several branches, doctors who can work at more than
-- one of them). Branch A is always present, so a single-clinic database keeps
-- behaving exactly as before; B and C (and example doctors) are created once by
-- clinic/branches.py's ensure_seed() when the app opens a real database.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS branches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,            -- short, shown on tokens: 'A'
    name TEXT NOT NULL,
    address TEXT,
    maps_url TEXT,
    phone TEXT,
    pin_code TEXT,                        -- used to rank "nearest branch"
    latitude REAL,                        -- optional, for later (not used yet)
    longitude REAL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    closed_reason TEXT,
    closed_message TEXT,                  -- what patients are told while closed
    color TEXT,                           -- calendar colour
    sort_order INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS doctors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    title TEXT,
    specialty TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

-- When a doctor is at a branch: one row per weekday window (0 = Monday ...
-- 6 = Sunday). A branch is open exactly when one of its doctors is scheduled
-- there; at most one doctor per branch at any time (checked in code).
CREATE TABLE IF NOT EXISTS doctor_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doctor_id INTEGER NOT NULL REFERENCES doctors(id),
    branch_id INTEGER NOT NULL REFERENCES branches(id),
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT
);
CREATE INDEX IF NOT EXISTS idx_doctor_schedules_branch ON doctor_schedules (branch_id, weekday);

INSERT OR IGNORE INTO branches (id, code, name, address, pin_code, color, sort_order)
VALUES (1, 'A', 'Branch A', 'Main clinic (edit the address in Settings)', '122001', '#1a6dd6', 1);

INSERT OR IGNORE INTO doctors (id, name, title, specialty) VALUES (1, 'Dr. Mehta', 'Dr.', 'General physician');

-- Branch A keeps today's hours (09:00-13:00 and 16:00-20:00, every day), staffed by doctor 1.
INSERT INTO doctor_schedules (doctor_id, branch_id, weekday, start_time, end_time)
SELECT 1, 1, w.weekday, h.start_time, h.end_time
FROM (SELECT 0 AS weekday UNION SELECT 1 UNION SELECT 2 UNION SELECT 3 UNION SELECT 4 UNION SELECT 5 UNION SELECT 6) AS w,
     (SELECT '09:00' AS start_time, '13:00' AS end_time UNION SELECT '16:00', '20:00') AS h
WHERE NOT EXISTS (SELECT 1 FROM doctor_schedules WHERE branch_id = 1);

-- A closure: a branch (or one doctor at it) is unavailable for a date range and
-- its existing appointments were moved to other branches or cancelled in one
-- reviewed batch (clinic/closures.py). It wraps the booking block that stops
-- new bookings. Only an APPLIED closure is stored: the plan a person reviews
-- before applying is computed on demand. closure_moves has one row per
-- appointment the batch touched, with where it came from, where it went, and
-- what the patient answered ("Accept" / "Choose another").
CREATE TABLE IF NOT EXISTS closures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id INTEGER NOT NULL REFERENCES branches(id),
    doctor_id INTEGER,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    start_time TEXT,
    end_time TEXT,
    reason TEXT,
    message TEXT,
    block_id INTEGER,
    status TEXT NOT NULL DEFAULT 'applied' CHECK (status IN ('applied', 'undone')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    undone_at TEXT
);

CREATE TABLE IF NOT EXISTS closure_moves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    closure_id INTEGER NOT NULL REFERENCES closures(id),
    appointment_id INTEGER NOT NULL REFERENCES appointments(id),
    action TEXT NOT NULL CHECK (action IN ('move', 'cancel')),
    from_branch_id INTEGER,
    from_date TEXT NOT NULL,
    from_time TEXT NOT NULL,
    to_branch_id INTEGER,
    to_date TEXT,
    to_time TEXT,
    -- done: applied; failed: refused at apply time (see error); undone: put back; skipped: undo left it
    result TEXT NOT NULL DEFAULT 'pending' CHECK (result IN ('pending', 'done', 'failed', 'undone', 'skipped')),
    error TEXT,
    -- what the patient answered to the WhatsApp notice
    response TEXT NOT NULL DEFAULT 'none' CHECK (response IN ('none', 'accepted', 'changed')),
    responded_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_closure_moves_closure ON closure_moves (closure_id);
CREATE INDEX IF NOT EXISTS idx_closure_moves_appointment ON closure_moves (appointment_id);


-- ---------------------------------------------------------------------------
-- Follow-up visits with a booked slot and two WhatsApp reminders
-- (clinic/followups.py). All additive.
-- ---------------------------------------------------------------------------
-- One "Schedule follow-ups" batch: the rows staff reviewed and applied together.
-- Undo cancels the appointments and follow-ups it created.
CREATE TABLE IF NOT EXISTS followup_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    status TEXT NOT NULL DEFAULT 'applied' CHECK (status IN ('applied', 'undone')),
    undone_at TEXT
);

-- The two reminders of a follow-up, per SLOT: moving the slot gives the new slot
-- its own rows (and cancels the old slot's unsent ones). kind '2d' = the early
-- reminder, '4h' = the one just before the visit. state: scheduled (waiting for
-- due_at), enqueued (handed to the notifications outbox -- its real status lives
-- there), skipped (never sent, see `reason`), cancelled (slot moved / visit
-- cancelled), manual (staff sent it by hand). due_at is the clinic's local wall
-- clock, 'YYYY-MM-DD HH:MM'.
CREATE TABLE IF NOT EXISTS followup_reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    followup_id INTEGER NOT NULL REFERENCES followups(id),
    kind TEXT NOT NULL CHECK (kind IN ('2d', '4h')),
    slot_date TEXT NOT NULL,
    slot_time TEXT NOT NULL,
    due_at TEXT,
    state TEXT NOT NULL DEFAULT 'scheduled'
        CHECK (state IN ('scheduled', 'enqueued', 'skipped', 'cancelled', 'manual')),
    reason TEXT,
    notification_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT,
    UNIQUE (followup_id, kind, slot_date, slot_time)
);
CREATE INDEX IF NOT EXISTS idx_followup_reminders_state ON followup_reminders (state, due_at);

-- Numbers that asked to stop follow-up reminders ("STOP"), by their last 10 digits.
CREATE TABLE IF NOT EXISTS reminder_opt_outs (
    phone10 TEXT PRIMARY KEY,
    opted_out_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A local record of every staff command that reached the tool-calling planner
-- (clinic/nlu/planner.py): what was said, what the planner called, which route
-- decided the outcome and how long it took. It exists to be reviewed by a person
-- (scripts/export_planner_log.py turns it into labelled test cases) and never
-- leaves this database. Transcripts contain patient names. Turn it off with the
-- app setting planner_log_enabled = 0. `outcome` is filled in later, when the
-- review card is approved or rejected.
CREATE TABLE IF NOT EXISTS planner_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    source TEXT NOT NULL DEFAULT 'voice' CHECK (source IN ('voice', 'wa_staff')),
    transcript TEXT NOT NULL,
    previous_turn TEXT,
    planner_tool TEXT,
    planner_args_json TEXT,
    -- rules: the keyword rules decided; planner: the tool call was used;
    -- label_fallback: the planner failed and the one-word label picker decided;
    -- rephrase: nothing could place the command
    route_taken TEXT NOT NULL CHECK (route_taken IN ('rules', 'planner', 'label_fallback', 'rephrase')),
    final_intent TEXT,
    latency_ms INTEGER,
    override_notes TEXT,
    outcome TEXT CHECK (outcome IS NULL OR outcome IN ('approved', 'rejected', 'edited')),
    -- which model planned it ('local' or 'sarvam'; NULL before this was recorded) and, for a Sarvam call that
    -- answered, its token counts and cost in whole paise at Sarvam's published prices (0 when nothing was billed)
    backend TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_paise INTEGER,
    -- for a 'rules' row, which precise rule decided it ("rule:move", "rule:context", "rule:count", "rule:branch",
    -- "rule:closure", "rule:keywords"; "+name_fill" when the small hosted name read ran); NULL for a planner row.
    -- planner_args_json then lists which slots were found / missing (names only, never values)
    route_detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_planner_log_ts ON planner_log (ts);


-- Questions the assistant could not answer (clinic/unanswered.py): a staff command that was
-- plainly a request to see, count or find information, but nothing the app can read covers it.
-- Kept so a PERSON can review them (scripts/export_unanswered.py), add a whitelist entry to
-- clinic/query_tool.py and mark the question resolved; the user is then told once that it
-- works. The app never turns this text into SQL or a whitelist entry. `key` is the normalised
-- question (lowercase, punctuation stripped, spaces collapsed): one row per key, `times_asked`
-- counts the repeats. Transcripts contain patient names: the table is as private as `patients`
-- and is written only while the planner_log_enabled setting is on. Times are the clinic's
-- local wall clock, as text.
CREATE TABLE IF NOT EXISTS unanswered_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    first_asked_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    last_asked_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    times_asked INTEGER NOT NULL DEFAULT 1,
    key TEXT NOT NULL UNIQUE,
    example_transcript TEXT NOT NULL,
    wanted TEXT,                           -- what the planner said they wanted, in a few words
    rejected_spec_json TEXT,               -- the planner's rejected query spec: a HINT for the developer only
    source TEXT NOT NULL DEFAULT 'voice' CHECK (source IN ('voice', 'typed')),
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'building', 'resolved', 'dismissed')),
    resolved_note TEXT,                    -- what the user can now ask ("Ask: who is on duty now")
    resolved_at TEXT,
    notified_at TEXT                       -- when the user was told it works (shown once)
);
CREATE INDEX IF NOT EXISTS idx_unanswered_status ON unanswered_questions (status, times_asked);
