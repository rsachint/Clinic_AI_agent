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
    route_detail TEXT,
    -- model-first mode only (clinic/architecture.py): the state card sent to the planner with this command
    -- (clinic/state_card.py: names and last four phone digits, never ids, notes or diagnoses); NULL otherwise.
    -- route_detail is then "mf:<tool>" or "mf_fallback_classic" (the planner could not answer: classic ran)
    state_card TEXT
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

-- Internet trouble the app noticed (clinic/network_health.py): one row per FAILED network call to Sarvam speech,
-- the Sarvam planner or WhatsApp (never a success, never an HTTP error such as a refused key). It exists so a person
-- can see when patchy internet is why an answer was poor (Audit log -> Connection). `kind` is "timeout",
-- "handshake", "connect" or "dropped"; `detail` is the fixed plain wording for it ("Could not connect"); nothing
-- from the exception, no host, URL or key is ever stored. `duration_ms` is how long the call waited. `ts` is UTC
-- text like the other tables. Pruned on every write to the newest 500 rows and at most 30 days.
CREATE TABLE IF NOT EXISTS network_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    service TEXT NOT NULL CHECK (service IN ('voice', 'planner', 'whatsapp')),
    kind TEXT,
    detail TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_network_events_ts ON network_events (ts);


-- ---------------------------------------------------------------------------
-- Read-only views for the "Model does all read operations" mode (clinic/sql_read.py).
-- The Sarvam planner writes ONE SELECT over these views and nothing else: they are pre-joined, carry
-- only the columns clinic/query_tool.py already exposes (no notes, diagnoses, message bodies, tokens,
-- WhatsApp ids, raw audit payloads or payees), money in RUPEES and times in IST. Nothing reads them
-- except that mode, so the classic and New modes are unaffected. clinic/read_schema.py describes every
-- column (a test keeps the two in step). v_branches and v_roster_days use today_ist(), a function the
-- read connection registers (clinic/sql_read.py); other connections simply cannot query those two.
-- ---------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_appointments AS
SELECT a.id AS id,
       a.appt_date AS appt_date,
       a.start_time AS start_time,
       substr(time(a.start_time, '+' || a.duration_minutes || ' minutes'), 1, 5) AS end_time,
       a.duration_minutes AS minutes,
       a.status AS status,
       a.queue_state AS queue_state,
       COALESCE(p.name, a.patient_name) AS patient_name,
       COALESCE(p.phone, a.patient_phone) AS patient_phone,
       d.name AS doctor_name,
       b.code AS branch_code,
       b.name AS branch_name,
       substr(datetime(a.created_at, '+5 hours', '+30 minutes'), 1, 16) AS created_ist
FROM appointments a
LEFT JOIN patients p ON p.id = a.patient_id
LEFT JOIN branches b ON b.id = COALESCE(
    a.branch_id,
    (SELECT CAST(s.value AS INTEGER) FROM app_settings s WHERE s.key = 'default_branch_id'
        AND CAST(s.value AS INTEGER) IN (SELECT id FROM branches WHERE active = 1)),
    (SELECT id FROM branches WHERE active = 1 ORDER BY sort_order, id LIMIT 1),
    1)
LEFT JOIN doctors d ON d.id = a.doctor_id;

CREATE VIEW IF NOT EXISTS v_patients AS
SELECT p.id AS id,
       p.name AS name,
       p.phone AS phone,
       p.age AS age,
       substr(datetime(p.registered_at, '+5 hours', '+30 minutes'), 1, 16) AS registered_ist
FROM patients p;

CREATE VIEW IF NOT EXISTS v_doctors AS
SELECT d.name AS name, d.title AS title, d.specialty AS specialty
FROM doctors d
WHERE d.active = 1;

CREATE VIEW IF NOT EXISTS v_branches AS
SELECT b.code AS code,
       b.name AS name,
       b.address AS address,
       b.pin_code AS pin_code,
       b.phone AS phone,
       b.status AS status,
       substr(b.closed_reason, 1, 80) AS closed_reason,
       CASE
         WHEN b.status = 'closed' THEN 'no'
         WHEN EXISTS (SELECT 1 FROM booking_blocks k
                      WHERE k.active = 1 AND k.doctor_id IS NULL AND k.start_time IS NULL
                        AND (k.branch_id IS NULL OR k.branch_id = b.id)
                        AND k.start_date <= today_ist() AND k.end_date >= today_ist()) THEN 'no'
         WHEN EXISTS (SELECT 1 FROM doctor_schedules s JOIN doctors d ON d.id = s.doctor_id
                      WHERE s.branch_id = b.id
                        AND s.weekday = ((CAST(strftime('%w', today_ist()) AS INTEGER) + 6) % 7)
                        AND d.active = 1
                        AND (s.valid_from IS NULL OR s.valid_from <= today_ist())
                        AND (s.valid_to IS NULL OR s.valid_to >= today_ist())) THEN 'yes'
         ELSE 'no'
       END AS open_today
FROM branches b
WHERE b.active = 1;

CREATE VIEW IF NOT EXISTS v_staff AS
SELECT s.name AS name,
       s.role AS role,
       s.phone AS phone,
       b.code AS branch_code,
       COALESCE(b.name, 'Any') AS branch_name
FROM staff s
LEFT JOIN branches b ON b.id = s.branch_id;

CREATE VIEW IF NOT EXISTS v_attendance AS
SELECT att.attendance_date AS attendance_date,
       s.name AS staff_name,
       s.role AS role,
       att.status AS status,
       COALESCE(b.name, 'Any') AS branch_name
FROM attendance att
JOIN staff s ON s.id = att.staff_id
LEFT JOIN branches b ON b.id = s.branch_id;

CREATE VIEW IF NOT EXISTS v_followups AS
SELECT p.name AS patient_name,
       p.phone AS patient_phone,
       f.due_date AS due_date,
       f.due_time AS due_time,
       f.status AS status,
       d.name AS doctor_name,
       b.code AS branch_code,
       b.name AS branch_name,
       CASE WHEN f.appointment_id IS NOT NULL THEN 'yes' ELSE 'no' END AS has_slot
FROM followups f
JOIN patients p ON p.id = f.patient_id
LEFT JOIN doctors d ON d.id = f.doctor_id
LEFT JOIN branches b ON b.id = f.branch_id;

CREATE VIEW IF NOT EXISTS v_visits AS
SELECT p.name AS patient_name,
       v.visit_date AS visit_date,
       ROUND(v.fee_paise / 100.0, 2) AS fee_rupees
FROM visits v
JOIN patients p ON p.id = v.patient_id;

CREATE VIEW IF NOT EXISTS v_expenses AS
SELECT e.expense_date AS expense_date,
       e.description AS description,
       ROUND(e.amount_paise / 100.0, 2) AS amount_rupees
FROM expenses e;

CREATE VIEW IF NOT EXISTS v_cashbook AS
SELECT v.visit_date AS entry_date,
       'fee' AS kind,
       COALESCE(p.name, '') AS description,
       ROUND(v.fee_paise / 100.0, 2) AS amount_rupees
FROM visits v
LEFT JOIN patients p ON p.id = v.patient_id
UNION ALL
SELECT e.expense_date, 'expense', e.description, ROUND(e.amount_paise / 100.0, 2)
FROM expenses e;

CREATE VIEW IF NOT EXISTS v_reminders AS
SELECT COALESCE(p.name, a.patient_name,
                (SELECT p2.name FROM patients p2
                 WHERE n.wa_id IS NOT NULL AND substr(p2.phone, -10) = substr(n.wa_id, -10)
                 ORDER BY p2.id LIMIT 1), '') AS patient_name,
       n.event AS kind,
       n.status AS status,
       substr(datetime(COALESCE(n.sent_at, n.created_at), '+5 hours', '+30 minutes'), 1, 16) AS sent_ist
FROM notifications n
LEFT JOIN appointments a ON a.id = n.appointment_id
LEFT JOIN patients p ON p.id = a.patient_id
WHERE n.event <> 'conv_reply';

CREATE VIEW IF NOT EXISTS v_closures AS
SELECT b.code AS branch_code,
       b.name AS branch_name,
       d.name AS doctor_name,
       c.start_date AS start_date,
       c.end_date AS end_date,
       substr(c.reason, 1, 80) AS reason,
       c.status AS status,
       COALESCE(m.moved, 0) AS patients_moved,
       COALESCE(m.cancelled, 0) AS appointments_cancelled
FROM closures c
JOIN branches b ON b.id = c.branch_id
LEFT JOIN doctors d ON d.id = c.doctor_id
LEFT JOIN (SELECT closure_id,
                  SUM(CASE WHEN action = 'move' AND result = 'done' THEN 1 ELSE 0 END) AS moved,
                  SUM(CASE WHEN action = 'cancel' AND result = 'done' THEN 1 ELSE 0 END) AS cancelled
           FROM closure_moves GROUP BY closure_id) m ON m.closure_id = c.id;

CREATE VIEW IF NOT EXISTS v_blocks AS
SELECT k.start_date AS start_date,
       k.end_date AS end_date,
       k.start_time AS start_time,
       k.end_time AS end_time,
       COALESCE(b.name, 'All branches') AS branch_name,
       COALESCE(d.name, 'All doctors') AS doctor_name,
       substr(k.reason, 1, 80) AS reason
FROM booking_blocks k
LEFT JOIN branches b ON b.id = k.branch_id
LEFT JOIN doctors d ON d.id = k.doctor_id
WHERE k.active = 1;

CREATE VIEW IF NOT EXISTS v_activity AS
SELECT substr(pa.created_at, 1, 16) AS activity_time,
       pa.event AS event,
       COALESCE(p.name, pa.patient_name) AS patient_name,
       pa.source AS source
FROM patient_activity pa
LEFT JOIN patients p ON p.id = pa.patient_id;

CREATE VIEW IF NOT EXISTS v_audit AS
SELECT substr(datetime(a.logged_at, '+5 hours', '+30 minutes'), 1, 16) AS logged_ist,
       a.intent AS action,
       a.entity_type AS record_type
FROM audit_log a;

CREATE VIEW IF NOT EXISTS v_schedules AS
SELECT d.name AS doctor_name,
       b.code AS branch_code,
       b.name AS branch_name,
       CASE s.weekday WHEN 0 THEN 'Monday' WHEN 1 THEN 'Tuesday' WHEN 2 THEN 'Wednesday' WHEN 3 THEN 'Thursday'
                      WHEN 4 THEN 'Friday' WHEN 5 THEN 'Saturday' ELSE 'Sunday' END AS weekday,
       s.start_time AS start_time,
       s.end_time AS end_time,
       s.valid_from AS valid_from,
       s.valid_to AS valid_to
FROM doctor_schedules s
JOIN doctors d ON d.id = s.doctor_id AND d.active = 1
JOIN branches b ON b.id = s.branch_id AND b.active = 1;

-- One row per day x branch x doctor for the next 60 days (today included): who is at which branch on a date,
-- worked out from the weekday windows, their valid_from / valid_to dates and the branch / doctor being active
-- and open, exactly as clinic/branches.py doctor_windows() does.
CREATE VIEW IF NOT EXISTS v_roster_days AS
WITH RECURSIVE roster_series(day, n) AS (
    SELECT today_ist(), 0
    UNION ALL
    SELECT date(day, '+1 day'), n + 1 FROM roster_series WHERE n < 59
)
SELECT r.day AS roster_date,
       CASE CAST(strftime('%w', r.day) AS INTEGER) WHEN 0 THEN 'Sunday' WHEN 1 THEN 'Monday' WHEN 2 THEN 'Tuesday'
            WHEN 3 THEN 'Wednesday' WHEN 4 THEN 'Thursday' WHEN 5 THEN 'Friday' ELSE 'Saturday' END AS weekday,
       b.code AS branch_code,
       b.name AS branch_name,
       d.name AS doctor_name,
       group_concat(s.start_time || '-' || s.end_time, ', ' ORDER BY s.start_time) AS hours,
       MIN(s.start_time) AS first_start,
       MAX(s.end_time) AS last_end
FROM roster_series r
JOIN doctor_schedules s ON s.weekday = ((CAST(strftime('%w', r.day) AS INTEGER) + 6) % 7)
                       AND (s.valid_from IS NULL OR s.valid_from <= r.day)
                       AND (s.valid_to IS NULL OR s.valid_to >= r.day)
JOIN doctors d ON d.id = s.doctor_id AND d.active = 1
JOIN branches b ON b.id = s.branch_id AND b.active = 1 AND b.status <> 'closed'
GROUP BY r.day, b.id, d.id;
