import json
import logging
import os
import threading
from datetime import date, datetime, timedelta

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO

from clinic import (auto_actions, booking_blocks, conv_runtime, conversation, core, gcal_client, gcal_config,
                    gcal_sync, notify, patient_activity, scheduler, scheduling, settings, token_queue, whatsapp as wa)
from clinic.adapters.registry import build_write_handlers, get_adapters
from clinic.asr import transcribe
from clinic.timefmt import utc_to_ist
from clinic.db import connect
from clinic.entity_resolution import last10_digits
from clinic.realtime_voice import register_realtime_voice
from clinic import wa_threads
from clinic.whatsapp_pipeline import classify_text_message, sender_appointments

load_dotenv()

_logger = logging.getLogger(__name__)

# CLINIC_DB_PATH lets a scratch instance (screenshots, experiments) run on its
# own database; unset, the app uses clinic.db exactly as before.
DB_PATH = os.environ.get("CLINIC_DB_PATH", "clinic.db")

# Test seam: a callable sender(wa_id, text) that replaces the real WhatsApp
# send for patient notifications. None means "use WHATSAPP_NOTIFY_MODE"
# (live by default, or dry_run). See clinic/notify.py. A sender that also
# accepts an `interactive=` keyword is given button / list specs.
NOTIFY_SENDER = None

# Test seams for the WhatsApp conversation agent (clinic/conversation.py):
#   BACKGROUND(fn, *args) runs inbound processing off the webhook thread
#     (a daemon thread in production; tests replace it with an inline call);
#   CLOCK() is the clinic's local wall clock (None = datetime.now);
#   AGENT_PICKER(text) is the closed-enum LLM intent picker (None = the local
#     Ollama model via clinic/nlu/intent_llm.py).
BACKGROUND = None
CLOCK = None
AGENT_PICKER = None

# Test seams for the Google Calendar sync (clinic/gcal_sync.py):
#   GCAL_CLIENT is the calendar client (a fake in tests; None = the real
#     service-account client, built lazily and only when configured);
#   GCAL_BACKGROUND(fn) runs the queue drain off the request thread (a daemon
#     thread in production; tests replace it with an inline call or a no-op).
GCAL_CLIENT = None
GCAL_BACKGROUND = None


def _run_in_background(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def _clinic_now():
    return CLOCK() if CLOCK is not None else datetime.now()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "clinic-copilot-dev-only")
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.add_template_filter(utc_to_ist, "ist")

# threading async_mode: this appliance runs as a single process on a
# clinic's mini-PC with a hardware watchdog (see plan) -- no eventlet/gevent
# monkeypatching, just plain OS threads, matching clinic/realtime_voice.py's
# own threading model and the WhatsApp audio path's existing
# threading.Thread usage below.
socketio = SocketIO(app, async_mode="threading")

CLINICAL_ADAPTER, OPS_ADAPTER = get_adapters()
HANDLERS = build_write_handlers(CLINICAL_ADAPTER, OPS_ADAPTER)

# Every write intent is deferred to the dashboard's structured review card:
# a voice command never writes to the database by itself. It only produces
# the parsed (and best-effort resolved) fields for a human to see, edit if
# needed, and explicitly approve -- the approval itself is what proposes
# and commits, in one request (see /approve).
DEFERRED_INTENTS = frozenset({
    "register_patient", "register_staff", "record_visit",
    "set_followup", "log_attendance", "log_expense",
    "cancel_followup", "reschedule_followup",
    "book_appointment", "cancel_appointment", "reschedule_appointment",
    # Voice queue commands are writes too (speech can be misheard: the wrong
    # "token 5"), so they get a review card. queue_status is read-only.
    "queue_check_in", "queue_call_next", "queue_mark_done", "queue_mark_no_show",
})

# Queue-tab button action -> the write intent it runs. The button click on a
# specific visible row is the explicit human action, so /queue/<id>/<action>
# proposes and confirms in one request (still audited), with no review card.
QUEUE_BUTTON_INTENTS = {
    "check_in": "queue_check_in",
    "call": "queue_call_next",
    "done": "queue_mark_done",
    "no_show": "queue_mark_no_show",
}


def get_conn():
    return connect(DB_PATH)


def _json_for_script(value):
    """JSON safe to embed inside a <script> block: patient-controlled text
    (WhatsApp messages, names) must never be able to close the tag."""
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


# Wires the live-voice Socket.IO events (see clinic/realtime_voice.py) into
# this same Flask app/process. This only changes how audio gets in and out
# -- it calls back into the exact transcript_to_response() contract below,
# and every write still lands on the same /approve route, untouched.
register_realtime_voice(socketio, os.environ.get("SARVAM_API_KEY", ""), get_conn, CLINICAL_ADAPTER, OPS_ADAPTER, DEFERRED_INTENTS)


def _token_suffix(conn, appointment_id):
    """' Token T-04' (or ' Token T-07 (provisional)' for a future date) for an
    appointment, or '' if it has none. Read-only, deterministic."""
    entry = token_queue.queue_entry(conn, appointment_id) if appointment_id else None
    if entry is None:
        return ""
    provisional = " (provisional)" if entry["appt_date"] != date.today().isoformat() else ""
    return ". Token {}{}".format(entry["token_label"], provisional)


def describe_proposal(conn, intent, slots, entity_id=None):
    if intent == "register_patient":
        # slots.get(...), not **slots -- the WhatsApp registration path
        # (clinic/whatsapp_pipeline.py) never sets "age" at all, unlike the
        # voice path which always includes it (as None if unheard). A
        # KeyError here would fire *after* core.confirm already committed
        # the write, wrongly reporting a successful save as a failure.
        return "register {} ({}), age {}".format(
            slots.get("name"), slots.get("phone"), slots.get("age") or "unknown"
        )
    if intent == "register_staff":
        return "register staff {} ({})".format(slots.get("name"), slots.get("role") or "no role given")
    if intent in ("record_visit", "set_followup"):
        patient = conn.execute("SELECT name, phone FROM patients WHERE id = ?", (slots["patient_id"],)).fetchone()
        label = "{} ({})".format(patient["name"], patient["phone"]) if patient else "patient #{}".format(slots["patient_id"])
        if intent == "record_visit":
            return "visit for {}: Rs {}".format(label, slots.get("fee_rupees"))
        return "follow-up for {} in {} days".format(label, slots.get("days_from_now"))
    if intent in ("cancel_followup", "reschedule_followup"):
        followup = conn.execute(
            "SELECT p.name, p.phone FROM followups f JOIN patients p ON p.id = f.patient_id WHERE f.id = ?",
            (slots["followup_id"],),
        ).fetchone()
        label = "{} ({})".format(followup["name"], followup["phone"]) if followup else "follow-up #{}".format(slots["followup_id"])
        if intent == "cancel_followup":
            return "cancelled follow-up for {}".format(label)
        return "rescheduled follow-up for {} to {}".format(label, slots.get("new_due_date"))
    if intent == "book_appointment":
        if slots.get("patient_id"):
            patient = conn.execute("SELECT name, phone FROM patients WHERE id = ?", (slots["patient_id"],)).fetchone()
            label = "{} ({})".format(patient["name"], patient["phone"]) if patient else "patient #{}".format(slots["patient_id"])
        else:
            label = slots.get("patient_name") or slots.get("patient_phone") or "unregistered caller"
        return "appointment for {} on {} at {}{}".format(
            label, slots.get("appt_date"), slots.get("start_time"), _token_suffix(conn, entity_id)
        )
    if intent in ("cancel_appointment", "reschedule_appointment"):
        appointment = conn.execute(
            "SELECT COALESCE(p.name, a.patient_name) AS name, COALESCE(p.phone, a.patient_phone) AS phone "
            "FROM appointments a LEFT JOIN patients p ON p.id = a.patient_id WHERE a.id = ?",
            (slots["appointment_id"],),
        ).fetchone()
        label = "{} ({})".format(appointment["name"], appointment["phone"]) if appointment and appointment["name"] \
            else "appointment #{}".format(slots["appointment_id"])
        if intent == "cancel_appointment":
            return "cancelled appointment for {}".format(label)
        return "rescheduled appointment for {} to {} at {}{}".format(
            label, slots.get("appt_date"), slots.get("start_time"), _token_suffix(conn, slots["appointment_id"])
        )
    if intent in QUEUE_BUTTON_INTENTS.values():
        entry = token_queue.queue_entry(conn, slots.get("appointment_id"))
        label = "{} {}".format(entry["token_label"], entry["name"] or "").strip() if entry else "appointment #{}".format(slots.get("appointment_id"))
        verb = {
            "queue_check_in": "checked in",
            "queue_call_next": "called in -- now in consultation",
            "queue_mark_done": "marked done",
            "queue_mark_no_show": "marked no-show",
        }[intent]
        return "{} {}".format(label, verb)
    if intent == "log_attendance":
        staff = conn.execute("SELECT name FROM staff WHERE id = ?", (slots["staff_id"],)).fetchone()
        label = staff["name"] if staff else "staff #{}".format(slots["staff_id"])
        return "attendance for {}: {}".format(label, slots.get("status"))
    if intent == "log_expense":
        return "expense: Rs {} -- {}".format(slots.get("amount_rupees"), slots.get("description"))
    return str(slots)


def safe_describe(conn, intent, slots, entity_id=None):
    """describe_proposal() runs AFTER core.confirm() has committed, so it must
    never be allowed to turn a successful write into a reported failure (a
    past bug: a formatting error here was shown to staff as 'could not
    save'). Falls back to a plain description."""
    try:
        return describe_proposal(conn, intent, slots, entity_id)
    except Exception:
        _logger.exception("describe_proposal failed after a committed %s write", intent)
        return "{} saved".format(intent.replace("_", " "))


def notify_after_write(conn, intent, slots, entity_id, wa_id=None, language=None):
    """Post-commit patient notifications (outbox + flush). Cannot raise --
    see notify.post_commit -- so the caller's response is unaffected."""
    try:
        notify.post_commit(conn, intent, slots, entity_id, sender=NOTIFY_SENDER, wa_id=wa_id, language=language)
    except Exception:  # post_commit already swallows; this is belt and braces
        _logger.exception("notification hook raised after a committed %s write", intent)


def _gcal_client():
    return GCAL_CLIENT if GCAL_CLIENT is not None else gcal_client.get_client()


def _gcal_drain_job():
    """Drain the calendar sync queue on its own thread / connection. Cannot
    raise: it runs after (and independently of) a response."""
    conn = None
    try:
        client = _gcal_client()
        if client is None:
            return
        conn = get_conn()
        gcal_sync.drain(conn, client)
    except Exception:
        _logger.exception("calendar sync worker failed")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _gcal_kick():
    (GCAL_BACKGROUND or _run_in_background)(_gcal_drain_job)


def post_write_hooks(conn, intent, slots, entity_id, wa_id=None, language=None):
    """Everything that follows a committed write, in one place: patient
    notification (+ token-change fan-out for other patients) and the Google
    Calendar resync enqueue. Used by the staff approval routes, the Queue-tab
    buttons, the staff direct-edit endpoints, Undo and the automatic WhatsApp
    path, so there is exactly one copy of this logic. Cannot raise."""
    notify_after_write(conn, intent, slots, entity_id, wa_id=wa_id, language=language)
    calendar_after_write(conn, intent, slots, entity_id)


def _auto_runner(conn, **kwargs):
    """The conversation agent's automatic-commit hook (see
    clinic/conversation.py): decide with the guard policy and, if every
    check passes, commit through propose/confirm and the shared hooks."""
    return auto_actions.handle_request(
        conn, handlers=HANDLERS, after_commit=post_write_hooks, **kwargs)


def calendar_after_write(conn, intent, slots, entity_id):
    """Post-commit Google Calendar sync: queues the affected day for
    reconciliation (one INSERT) and wakes the worker thread. Cannot raise, and
    never talks to Google on the request thread, so the caller's response is
    unaffected -- see gcal_sync.post_commit."""
    try:
        gcal_sync.post_commit(conn, intent, slots, entity_id, kick=_gcal_kick)
    except Exception:  # post_commit already swallows; this is belt and braces
        _logger.exception("calendar sync hook raised after a committed %s write", intent)


def calendar_panel_context(conn):
    """Everything the Appointments tab (templates/_calendar_*.html) renders."""
    try:
        status = gcal_sync.sync_status(conn)
    except Exception:
        _logger.exception("could not read calendar sync status")
        status = {
            "configured": gcal_config.is_configured(), "calendar_id": gcal_config.calendar_id(),
            "last_sync_at": None, "last_sync_local": None, "pending": 0, "failed": 0,
            "last_error": "Could not read the sync status.",
        }
    return {
        "cal": {
            "status": status,
            "calendar_id": status["calendar_id"],
            "embed_urls": {mode.lower(): gcal_config.embed_url(mode) for mode in gcal_config.EMBED_MODES},
            "default_mode": gcal_config.DEFAULT_EMBED_MODE.lower(),
            "open_url": gcal_config.open_url(),
            "timezone": gcal_config.TIMEZONE,
        },
    }


def _valid_day(text):
    try:
        return date.fromisoformat(text).isoformat() if text else None
    except (TypeError, ValueError):
        return None


def queue_panel_context(conn, day=None):
    """Everything the Queue tab (templates/_queue_panel.html) renders, for
    `day` (YYYY-MM-DD; today when omitted or invalid)."""
    today = date.today().isoformat()
    day = _valid_day(day) or today
    return {
        "queue_today": today,
        "queue_date": day,
        "queue_is_today": day == today,
        "queue_is_past": day < today,
        "queue_entries": CLINICAL_ADAPTER.queue_for_date(conn, day),
        "notifications": notify.recent_notifications(conn, 15),
    }


@app.route("/")
def dashboard():
    conn = get_conn()
    patients = conn.execute(
        "SELECT id, name, phone, age, registered_at FROM patients ORDER BY id DESC LIMIT 10"
    ).fetchall()
    staff = conn.execute("SELECT id, name FROM staff ORDER BY name").fetchall()
    audit_log = conn.execute(
        "SELECT logged_at, intent, entity_type, entity_id, payload_json FROM audit_log ORDER BY id DESC LIMIT 10"
    ).fetchall()
    # agent_handled=1: the conversation agent answered with replies only, so
    # there is nothing for staff to approve (the thread view still shows it).
    wa_inbox = conn.execute(
        "SELECT * FROM wa_messages WHERE status IN ('classified', 'needs_human_reply', 'error') "
        "AND COALESCE(agent_handled, 0) = 0 ORDER BY received_at"
    ).fetchall()
    wa_inbox_rows = []
    for row in wa_inbox:
        item = dict(row)
        item["slots"] = json.loads(row["slots_json"]) if row["slots_json"] else {}
        item["pending_followups"] = []
        if row["patient_id"] and row["intent"] in ("confirm_followup", "cancel_followup", "reschedule_followup"):
            item["pending_followups"] = [
                dict(f) for f in conn.execute(
                    "SELECT id, due_date FROM followups WHERE patient_id = ? AND status = 'pending' ORDER BY due_date",
                    (row["patient_id"],),
                ).fetchall()
            ]
        item["appointments"] = []
        if row["intent"] in ("cancel_appointment", "reschedule_appointment"):
            # The sender's own upcoming appointments (by patient or by phone,
            # so an unregistered WhatsApp booker is covered too) for the
            # review card's appointment dropdown.
            item["appointments"] = sender_appointments(conn, row["wa_id"], row["patient_id"])
        wa_inbox_rows.append(item)
    # Emergencies first, then oldest first (sort is stable).
    wa_inbox_rows.sort(key=lambda item: 0 if item["slots"].get("flag") == "emergency" else 1)

    return render_template(
        "dashboard.html",
        today=date.today().isoformat(),
        missed=CLINICAL_ADAPTER.missed_followups(conn),
        cashbook=OPS_ADAPTER.day_end_cashbook(conn),
        attendance=OPS_ADAPTER.attendance_register(conn),
        clinical_citation=CLINICAL_ADAPTER.citation(),
        ops_citation=OPS_ADAPTER.citation(),
        patients=patients,
        staff=staff,
        audit_log=audit_log,
        wa_inbox=wa_inbox_rows,
        all_patients=conn.execute("SELECT id, name, phone FROM patients ORDER BY name").fetchall(),
        automation=automation_settings_view(conn),
        patients_json=_json_for_script([dict(p) for p in patients]),
        staff_json=_json_for_script([dict(s) for s in staff]),
        wa_inbox_json=_json_for_script(wa_inbox_rows),
        wa_threads_json=_json_for_script(wa_threads.conversation_threads(conn)),
        **queue_panel_context(conn),
        **calendar_panel_context(conn),
        # Connectors tab: a plain, deterministic boolean env check -- no
        # NLU/pipeline code involved. Gmail/Calendar/Printer have no
        # backend integration yet, so their cards are hardcoded "not
        # configured" directly in the template.
        whatsapp_configured=bool(os.environ.get("WHATSAPP_ACCESS_TOKEN")),
    )


def _apply_block_override(intent, slots, payload):
    """A review card that was refused because the slot is inside a booking
    block can be re-submitted with override_block after staff confirm "Book
    anyway?". The flag only exists on this explicit second submit, is passed
    to the write handler, and ends up in the audit payload."""
    if payload.get("override_block") and intent in ("book_appointment", "reschedule_appointment") and isinstance(slots, dict):
        slots["override_block"] = True


def _approve_failure(exc):
    """A failed approval. A booking-block refusal is flagged so the card can
    offer "Book anyway?"; everything else is the plain error it always was."""
    if isinstance(exc, scheduling.SlotBlockedError):
        return jsonify(ok=False, error=str(exc), blocked=True)
    return jsonify(ok=False, error=str(exc))


@app.route("/approve", methods=["POST"])
def approve():
    """The structured review card's Approve button. This is the human
    tap-to-confirm the whole architecture is built around (§3): propose and
    confirm happen together here, in one request, only after a human has
    seen and optionally edited every field -- never from the voice command
    alone."""
    payload = request.get_json(force=True)
    intent = payload["intent"]
    slots = payload["slots"]
    transcript = payload.get("transcript", "")
    lang = payload.get("language") or "hi-IN"

    conn = get_conn()
    _apply_block_override(intent, slots, payload)
    proposal_id = core.propose(conn, intent, slots, source_text=transcript)
    try:
        entity_type, entity_id = core.confirm(conn, proposal_id, HANDLERS)
    except Exception as e:
        return _approve_failure(e)

    # The write is committed. Everything from here on is best-effort and
    # must not change the answer: describing it, and notifying patients.
    description = safe_describe(conn, intent, slots, entity_id)
    post_write_hooks(conn, intent, slots, entity_id)
    message = ("Save ho gaya: {}".format(description) if lang == "hi-IN" else "Saved: {}".format(description))
    return jsonify(
        ok=True,
        message=message,
        entity_type=entity_type,
        entity_id=entity_id,
    )


@app.route("/webhook/whatsapp", methods=["GET"])
def whatsapp_verify():
    challenge = wa.verify_webhook(request.args)
    if challenge is not None:
        return challenge, 200
    return "Forbidden", 403


@app.route("/webhook/whatsapp", methods=["POST"])
def whatsapp_receive():
    """Meta needs a fast 2xx or it retries (and eventually disables the
    webhook), so this ALWAYS answers 200 -- any trouble is logged -- and does
    the slow work (the conversation agent, transcription) off this thread."""
    try:
        payload = request.get_json(force=True, silent=True) or {}
        parsed = wa.parse_webhook_payload(payload)
        if parsed is not None:
            _receive_message(parsed)
    except Exception:
        _logger.exception("WhatsApp webhook handling failed (answering 200 anyway)")
    return jsonify({}), 200


def _receive_message(parsed):
    # parsed is None for non-message events (delivered/read receipts) --
    # handled by the caller.
    conn = get_conn()
    existing = conn.execute(
        "SELECT id FROM wa_messages WHERE wa_message_id = ?", (parsed["wa_message_id"],)
    ).fetchone()
    if existing:
        # Meta's delivery is at-least-once; without this we'd show staff the
        # same message twice on a retry.
        return

    cur = conn.execute(
        "INSERT INTO wa_messages (wa_message_id, wa_id, message_type, raw_text, media_id, status) "
        "VALUES (?, ?, ?, ?, ?, 'received')",
        (parsed["wa_message_id"], parsed["wa_id"], parsed["message_type"], parsed["text"], parsed["media_id"]),
    )
    conn.commit()
    msg_id = cur.lastrowid
    # A message that sat in Meta's retry queue (e.g. while the webhook was
    # down) is not answered conversationally or acknowledged: "we've received
    # your message" is wrong days later, and a stale "yes" must not confirm
    # anything. It goes through the old staff-review path instead.
    stale = wa.is_stale(parsed.get("timestamp"))
    run = BACKGROUND or _run_in_background

    if conversation.agent_enabled():
        # No generic acknowledgment here: the agent's own reply is the
        # response (voice notes are acknowledged only if transcription fails).
        if parsed["message_type"] == "text":
            run(_process_text, msg_id, parsed["wa_id"], parsed["text"], parsed.get("choice_id"), stale)
        else:
            run(_process_wa_audio, msg_id, parsed["wa_id"], parsed["media_id"], stale)
        return

    # WHATSAPP_AGENT_ENABLED=0: the previous single-message behaviour.
    if not stale:
        _acknowledge(conn, parsed["wa_id"], parsed["text"])
    if parsed["message_type"] == "text":
        _classify_and_store(msg_id, parsed["wa_id"], parsed["text"])
    else:
        run(_process_wa_audio, msg_id, parsed["wa_id"], parsed["media_id"], stale)


def _acknowledge(conn, wa_id, text):
    """The fixed "we've received your message" reply, in the sender's
    language and at most once per ACK_COOLDOWN. Goes through the injectable
    sender, so WHATSAPP_NOTIFY_MODE=dry_run (and tests) never reach WhatsApp.
    Cannot raise: an ack failure must never block classification, and
    releasing the claim lets the next message retry the ack."""
    ack_id = None
    try:
        ack_id = wa.claim_acknowledgment(conn, wa_id)
        if ack_id is None:
            return
        sender, dry_run = notify.resolve_sender(NOTIFY_SENDER)
        if dry_run:
            wa.release_acknowledgment(conn, ack_id)
            return
        sender(wa_id, wa.acknowledgment_text(wa.detect_message_language(text)))
    except Exception:
        _logger.exception("acknowledgment to %s failed", wa_id)
        if ack_id is not None:
            try:
                wa.release_acknowledgment(conn, ack_id)
            except Exception:
                pass


def _process_text(msg_id, wa_id, text, choice_id=None, stale=False):
    """One inbound message, already stored: the conversation agent answers
    it, or (agent off / stale message) the old classify-and-store path."""
    if stale or not conversation.agent_enabled():
        _classify_and_store(msg_id, wa_id, text)
        return
    conn = get_conn()
    try:
        conv_runtime.process_inbound(
            conn, msg_id, wa_id, text, choice_id, now=_clinic_now(), picker=AGENT_PICKER, auto=_auto_runner)
    except Exception:
        _logger.exception("conversation agent failed for wa_message %s; handing it to staff", msg_id)
        try:
            conn.rollback()
        except Exception:
            pass
        if auto_actions.committed_for_message(conn, msg_id):
            # Something failed AFTER the automatic write committed: the
            # request is done, so it must not also be handed to staff.
            conn.execute(
                "UPDATE wa_messages SET status = 'dismissed', agent_handled = 1, resolved_at = datetime('now') "
                "WHERE id = ?", (msg_id,))
            conn.commit()
            conv_runtime.flush_replies(conn, NOTIFY_SENDER)
            return
        _acknowledge(conn, wa_id, text)
        _classify_and_store(msg_id, wa_id, text)
        return
    conv_runtime.flush_replies(conn, NOTIFY_SENDER)


def _answer_status_question(conn, msg_id, wa_id, result):
    """A patient asked where they are in the queue. Answer automatically with
    the fixed status_reply template -- read-only, no approval needed -- but
    ONLY to the sender's own number (notify enforces the number matches the
    appointment). Returns True if answered; False means the caller should
    leave it for staff."""
    try:
        appointment_id = result["slots"].get("appointment_id")
        reply_id = notify.notify_status_reply(conn, wa_id, appointment_id, msg_id)
        if reply_id is None:
            return False
        sender, dry_run = notify.resolve_sender(NOTIFY_SENDER)
        notify.flush(conn, sender, dry_run=dry_run)
        return True
    except Exception:
        _logger.exception("automatic status reply failed for wa_message %s", msg_id)
        return False


def _classify_and_store(msg_id, wa_id, text):
    """The pre-agent path: classify one message and either answer a status
    question or leave a proposal / needs_human_reply item for staff."""
    conn = get_conn()
    result = classify_text_message(conn, wa_id, text, CLINICAL_ADAPTER)
    if result["intent"] == "my_status":
        if _answer_status_question(conn, msg_id, wa_id, result):
            # Handled end to end; nothing for staff to approve. (The reply
            # and its delivery status are visible in the Queue tab.)
            conn.execute(
                "UPDATE wa_messages SET patient_id=?, raw_text=?, intent=?, slots_json=?, "
                "status='dismissed', resolved_at=datetime('now') WHERE id=?",
                (result["patient_id"], text, result["intent"], json.dumps(result["slots"]), msg_id),
            )
            conn.commit()
            return
        # Could not answer (e.g. the reply was refused): hand to a human.
        result = {"patient_id": result["patient_id"], "intent": None, "slots": {}}
    status = "classified" if result["intent"] else "needs_human_reply"
    conn.execute(
        "UPDATE wa_messages SET patient_id=?, raw_text=?, intent=?, slots_json=?, status=? WHERE id=?",
        (result["patient_id"], text, result["intent"], json.dumps(result["slots"]), status, msg_id),
    )
    conn.commit()
    if result["intent"] in ("book_appointment", "cancel_appointment", "reschedule_appointment"):
        # A patient-initiated appointment event on the path that never
        # auto-commits (assistant off / stale message / assistant failed):
        # still log it, as sent to staff.
        _log_legacy_request(conn, wa_id, result)


def _log_legacy_request(conn, wa_id, result):
    patient_name = None
    if result.get("patient_id") is not None:
        row = conn.execute("SELECT name FROM patients WHERE id = ?", (result["patient_id"],)).fetchone()
        patient_name = row["name"] if row else (result["slots"].get("patient_name"))
    else:
        patient_name = result["slots"].get("patient_name")
    now = _clinic_now()
    appointment_id = result["slots"].get("appointment_id")
    for event, detail in (("requested", "Asked about an appointment ({})".format(result["intent"].replace("_", " "))),
                          ("escalated", "Sent to staff: the assistant did not handle this message")):
        patient_activity.log(conn, event=event, source="whatsapp-agent", wa_id=wa_id, patient_id=result.get("patient_id"),
                             patient_name=patient_name, appointment_id=appointment_id, detail=detail, now=now)


def _process_wa_audio(msg_id, wa_id, media_id, stale=False):
    conn = get_conn()
    try:
        audio_bytes = wa.download_media(media_id)
        tmp_path = "wa_audio_{}.ogg".format(msg_id)
        with open(tmp_path, "wb") as f:
            f.write(audio_bytes)
        heard = transcribe(tmp_path, language_code="unknown", input_audio_codec="opus")
        heard_text = heard.text
    except Exception as e:
        conn.execute("UPDATE wa_messages SET status='error', error_text=? WHERE id=?", (str(e), msg_id))
        conn.commit()
        if conversation.agent_enabled() and not stale:
            # Voice notes aren't acknowledged up front when the agent is on,
            # so this is the one case that still gets the generic receipt.
            _acknowledge(conn, wa_id, None)
        return
    try:
        # The transcript goes through exactly the same path as typed text.
        _process_text(msg_id, wa_id, heard_text, None, stale)
    except Exception as e:
        conn.execute("UPDATE wa_messages SET status='error', error_text=? WHERE id=?", (str(e), msg_id))
        conn.commit()


@app.route("/wa/<int:msg_id>/approve", methods=["POST"])
def wa_approve(msg_id):
    payload = request.get_json(force=True)
    slots = payload.get("slots", {})

    conn = get_conn()
    row = conn.execute("SELECT * FROM wa_messages WHERE id = ?", (msg_id,)).fetchone()
    if row is None:
        return jsonify(ok=False, error="Message not found")
    intent = row["intent"]

    if intent == "confirm_followup":
        # Open question in the plan, not yet decided: does a patient's
        # confirmation write anything to followups, or just clear the inbox
        # item? Defaulting to no write until the PM decides -- see plan §13.1.
        conn.execute(
            "UPDATE wa_messages SET status='approved', resolved_at=datetime('now') WHERE id=?", (msg_id,)
        )
        conn.commit()
        return jsonify(ok=True, message="Acknowledged -- no record was changed.")

    _apply_block_override(intent, slots, payload)
    proposal_id = core.propose(conn, intent, slots, source_text=row["raw_text"])
    try:
        entity_type, entity_id = core.confirm(conn, proposal_id, HANDLERS)
    except Exception as e:
        return _approve_failure(e)

    conn.execute(
        "UPDATE wa_messages SET status='approved', proposal_id=?, resolved_at=datetime('now') WHERE id=?",
        (proposal_id, msg_id),
    )
    conn.commit()
    description = safe_describe(conn, intent, slots, entity_id)
    post_write_hooks(conn, intent, slots, entity_id, wa_id=row["wa_id"])
    conv_runtime.after_resolution(conn, row, sender_override=NOTIFY_SENDER)   # release the slot hold
    return jsonify(ok=True, message="Saved: {}".format(description), entity_type=entity_type, entity_id=entity_id)


@app.route("/wa/<int:msg_id>/reject", methods=["POST"])
def wa_reject(msg_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM wa_messages WHERE id = ?", (msg_id,)).fetchone()
    conn.execute("UPDATE wa_messages SET status='rejected', resolved_at=datetime('now') WHERE id=?", (msg_id,))
    conn.commit()
    if row is not None:
        # A request the patient made through the WhatsApp conversation gets a
        # fixed "we couldn't confirm that" notice so they aren't left
        # hanging; the held slot is released either way. Best effort.
        conv_runtime.after_resolution(
            conn, row, declined=conv_runtime.is_conversation_request(row), sender_override=NOTIFY_SENDER)
    return jsonify(ok=True)


@app.route("/wa/<int:msg_id>/dismiss", methods=["POST"])
def wa_dismiss(msg_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM wa_messages WHERE id = ?", (msg_id,)).fetchone()
    conn.execute("UPDATE wa_messages SET status='dismissed', resolved_at=datetime('now') WHERE id=?", (msg_id,))
    conn.commit()
    if row is not None:
        conv_runtime.after_resolution(conn, row, sender_override=NOTIFY_SENDER)
    return jsonify(ok=True)


@app.route("/wa/threads")
def wa_threads_json():
    """The Patient messages tab's conversation threads (polled by
    static/wa_threads.js)."""
    return jsonify(wa_threads.conversation_threads(get_conn()))


@app.route("/wa/threads/<wa_id>/mode", methods=["POST"])
def wa_thread_mode(wa_id):
    """Staff Take over / Resume agent for one sender. In 'human' mode the
    conversation agent stays silent and every inbound message lands in the
    inbox as needs_human_reply."""
    mode = (request.get_json(force=True, silent=True) or {}).get("mode")
    if mode not in ("agent", "human"):
        return jsonify(ok=False, error="mode must be 'agent' or 'human'"), 400
    conn = get_conn()
    if not wa_threads.known_sender(conn, wa_id):
        return jsonify(ok=False, error="Unknown sender"), 404
    conversation.set_mode(conn, wa_id, mode, now=_clinic_now())
    return jsonify(ok=True, mode=mode)


@app.route("/wa/threads/<wa_id>/reply", methods=["POST"])
def wa_thread_reply(wa_id):
    """A plain message a staff member typed, sent to the patient through the
    notifications outbox (event 'staff_message'): logged, visible in the
    thread, and sent only inside WhatsApp's 24-hour window -- otherwise the
    row is 'blocked_no_window' and the thread says so."""
    text = ((request.get_json(force=True, silent=True) or {}).get("text") or "").strip()
    if not text:
        return jsonify(ok=False, error="Type a message first."), 400
    if len(text) > 1000:
        return jsonify(ok=False, error="Messages are limited to 1000 characters."), 400
    conn = get_conn()
    if not wa_threads.known_sender(conn, wa_id):
        return jsonify(ok=False, error="Unknown sender"), 404
    notification_id = notify.enqueue_staff_message(conn, wa_id, text)
    try:
        sender, dry_run = notify.resolve_sender(NOTIFY_SENDER)
        notify.flush(conn, sender, dry_run=dry_run, lock_timeout=3)
    except Exception:
        _logger.exception("flush after a staff message failed")
    row = conn.execute("SELECT status, error FROM notifications WHERE id = ?", (notification_id,)).fetchone()
    return jsonify(ok=True, status=row["status"], error=row["error"], notification_id=notification_id,
                   window_open=notify.in_window(conn, wa_id, notify.Now.real().utc))


# ---------------------------------------------------------------------------
# Staff direct edits (Queue tab): book / move / cancel an appointment.
#
# The click or form submit IS the explicit human action, so these run propose +
# confirm immediately (audited like any write, proposal source "[staff:...]")
# with no review card, then the same hooks as the approval route: the patient
# is notified with the existing templates, other patients' tokens are
# re-announced, and the Google Calendar resync is queued. Each is also logged
# in the patient activity log (source "staff").
# ---------------------------------------------------------------------------

def _json_body():
    body = request.get_json(force=True, silent=True)
    return body if isinstance(body, dict) else {}


def _staff_slot(payload):
    """(appt_date, start_time, None) from a request body, or (None, None, error)."""
    appt_date, start_time = payload.get("appt_date"), payload.get("start_time")
    day = _valid_day(appt_date) if isinstance(appt_date, str) else None
    if day is None:
        return None, None, "Pick a valid date."
    if day < _clinic_now().date().isoformat():
        return None, None, "That date has already passed."
    if start_time not in scheduling.slot_grid():
        return None, None, "Pick a time from the list of clinic slots."
    return day, start_time, None


def _staff_failure(exc):
    if exc.blocked:
        return jsonify(ok=False, blocked=True, error="{} Book anyway?".format(str(exc).rstrip(".") + "."))
    return jsonify(ok=False, error=str(exc))


@app.route("/appointments/slots")
def appointment_slots():
    """Free slots for one date, for the New / Move forms. `free` excludes
    booked and blocked slots; `blocked` lists the not-booked slots that a
    booking block is holding back (staff can still pick one, with a confirm)."""
    day = _valid_day(request.args.get("date"))
    if day is None:
        return jsonify(ok=False, error="Pick a valid date."), 400
    conn = get_conn()
    free = scheduling.generate_slots(conn, day)
    blocked = scheduling.blocked_only_slots(conn, day)
    if day == _clinic_now().date().isoformat():
        hhmm = _clinic_now().strftime("%H:%M")
        free = [t for t in free if t >= hhmm]
        blocked = [t for t in blocked if t >= hhmm]
    return jsonify(ok=True, date=day, free=free, blocked=blocked)


@app.route("/appointments/new", methods=["POST"])
def appointment_new():
    payload = _json_body()
    appt_date, start_time, error = _staff_slot(payload)
    if error:
        return jsonify(ok=False, error=error)
    conn = get_conn()
    patient_id = payload.get("patient_id") or None
    name = phone = None
    if patient_id:
        patient = conn.execute("SELECT id, name, phone FROM patients WHERE id = ?", (patient_id,)).fetchone()
        if patient is None:
            return jsonify(ok=False, error="That patient was not found.")
        patient_id, name, phone = patient["id"], patient["name"], patient["phone"]
        slots_name = slots_phone = None
    else:
        name = (payload.get("patient_name") or "").strip()
        phone = last10_digits(payload.get("patient_phone") or "")
        if not name or len(name) > 60:
            return jsonify(ok=False, error="Enter the patient's name (or pick an existing patient).")
        if len(phone) != 10:
            return jsonify(ok=False, error="Enter a 10-digit phone number so the patient can be notified.")
        slots_name, slots_phone = name, phone
    slots = {
        "patient_id": patient_id, "patient_name": slots_name, "patient_phone": slots_phone,
        "appt_date": appt_date, "start_time": start_time, "duration_minutes": None,
        "notes": "Booked by staff from the dashboard",
    }
    override = bool(payload.get("override_block"))
    if override:
        slots["override_block"] = True
    try:
        appointment_id, _ = auto_actions.staff_action(
            conn, "book_appointment", slots, HANDLERS, post_write_hooks, _clinic_now(),
            note="New appointment form", event="staff_booked", patient_id=patient_id, patient_name=name,
            wa_id=wa.phone_to_wa_id(phone), detail="Booked {} {}{}".format(
                appt_date, start_time, " (inside a booking block -- staff override)" if override else ""),
            meta={"appt_date": appt_date, "start_time": start_time, "override_block": override})
    except auto_actions.ActionError as exc:
        return _staff_failure(exc)
    return jsonify(ok=True, message="Booked {} on {} at {}. The patient has been notified.".format(
        name, appt_date, start_time), appointment_id=appointment_id)


def _editable_appointment(conn, appointment_id):
    """(row, None) for an appointment staff may move / cancel, else (None, error)."""
    row = conn.execute(
        "SELECT a.id, a.patient_id, a.appt_date, a.start_time, a.status, a.queue_state, "
        "COALESCE(p.name, a.patient_name) AS name, COALESCE(a.patient_phone, p.phone) AS phone "
        "FROM appointments a LEFT JOIN patients p ON p.id = a.patient_id WHERE a.id = ?", (appointment_id,)).fetchone()
    if row is None:
        return None, "Appointment not found."
    if row["status"] not in ("booked", "confirmed"):
        return None, "That appointment is already {}.".format(row["status"].replace("_", " "))
    if row["queue_state"] == "in_consultation":
        return None, "That patient is with the doctor now."
    return row, None


@app.route("/appointments/<int:appointment_id>/move", methods=["POST"])
def appointment_move(appointment_id):
    payload = _json_body()
    conn = get_conn()
    row, error = _editable_appointment(conn, appointment_id)
    if error:
        return jsonify(ok=False, error=error)
    appt_date, start_time, error = _staff_slot(payload)
    if error:
        return jsonify(ok=False, error=error)
    slots = {"appointment_id": appointment_id, "appt_date": appt_date, "start_time": start_time, "require_active": True}
    override = bool(payload.get("override_block"))
    if override:
        slots["override_block"] = True
    try:
        auto_actions.staff_action(
            conn, "reschedule_appointment", slots, HANDLERS, post_write_hooks, _clinic_now(),
            note="Queue tab Move", event="staff_rescheduled", patient_id=row["patient_id"], patient_name=row["name"],
            wa_id=wa.phone_to_wa_id(row["phone"]), appointment_id=appointment_id,
            detail="Moved {} {} to {} {}{}".format(
                row["appt_date"], row["start_time"], appt_date, start_time,
                " (inside a booking block -- staff override)" if override else ""),
            meta={"old_date": row["appt_date"], "old_time": row["start_time"], "appt_date": appt_date,
                  "start_time": start_time, "override_block": override})
    except auto_actions.ActionError as exc:
        return _staff_failure(exc)
    return jsonify(ok=True, message="Moved {} to {} at {}. The patient has been notified.".format(
        row["name"] or "the appointment", appt_date, start_time))


@app.route("/appointments/<int:appointment_id>/cancel", methods=["POST"])
def appointment_cancel(appointment_id):
    conn = get_conn()
    row, error = _editable_appointment(conn, appointment_id)
    if error:
        return jsonify(ok=False, error=error)
    slots = {"appointment_id": appointment_id, "require_active": True}
    try:
        auto_actions.staff_action(
            conn, "cancel_appointment", slots, HANDLERS, post_write_hooks, _clinic_now(),
            note="Queue tab Cancel", event="staff_cancelled", patient_id=row["patient_id"], patient_name=row["name"],
            wa_id=wa.phone_to_wa_id(row["phone"]), appointment_id=appointment_id,
            detail="Cancelled {} {}".format(row["appt_date"], row["start_time"]),
            meta={"old_date": row["appt_date"], "old_time": row["start_time"]})
    except auto_actions.ActionError as exc:
        return _staff_failure(exc)
    return jsonify(ok=True, message="Cancelled {}'s appointment. The patient has been notified.".format(
        row["name"] or "the"))


# ---------------------------------------------------------------------------
# Automation tab: the on/off switch, the daily cap, booking blocks, the
# automated-actions feed (with Undo) and a patient's appointment timeline.
# ---------------------------------------------------------------------------

def automation_settings_view(conn):
    today = _clinic_now().date().isoformat()
    return {
        "enabled": settings.auto_enabled(conn),
        "daily_cap": settings.auto_daily_cap(conn),
        "used_today": patient_activity.automated_bookings_on(conn, today),
        "today": today,
    }


@app.route("/automation/data")
def automation_data():
    conn = get_conn()
    return jsonify(
        ok=True,
        settings=automation_settings_view(conn),
        blocks=booking_blocks.list_blocks(conn, today=date.today().isoformat()),
        feed=patient_activity.feed(conn, request.args.get("q")),
    )


@app.route("/automation/settings", methods=["POST"])
def automation_settings():
    payload = _json_body()
    conn = get_conn()
    try:
        if "enabled" in payload:
            if not isinstance(payload["enabled"], bool):
                raise ValueError("enabled must be true or false.")
            settings.set_auto_enabled(conn, payload["enabled"])
            _logger.info("automatic appointments switched %s from the dashboard", "ON" if payload["enabled"] else "OFF")
        if "daily_cap" in payload:
            settings.set_auto_daily_cap(conn, payload["daily_cap"])
            _logger.info("automatic booking daily cap set to %s from the dashboard", payload["daily_cap"])
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    return jsonify(ok=True, settings=automation_settings_view(conn))


def _block_args(payload):
    return (payload.get("start_date"), payload.get("end_date"), payload.get("start_time"), payload.get("end_time"))


@app.route("/automation/blocks/preview", methods=["POST"])
def automation_block_preview():
    """What a block would sit on top of, BEFORE it is created: the existing
    appointments inside the window (they are never moved automatically)."""
    payload = _json_body()
    try:
        start_date, end_date, start_time, end_time, _ = booking_blocks.normalize(*_block_args(payload), reason=None)
    except booking_blocks.BlockError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    affected = booking_blocks.appointments_in_window(get_conn(), start_date, end_date, start_time, end_time)
    return jsonify(ok=True, affected=affected, count=len(affected))


@app.route("/automation/blocks", methods=["POST"])
def automation_block_add():
    payload = _json_body()
    conn = get_conn()
    try:
        block = booking_blocks.add_block(conn, *_block_args(payload), reason=payload.get("reason"), now=_clinic_now())
    except booking_blocks.BlockError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    return jsonify(ok=True, block=block, count=len(block["affected"]))


@app.route("/automation/blocks/<int:block_id>/remove", methods=["POST"])
def automation_block_remove(block_id):
    if not booking_blocks.remove_block(get_conn(), block_id):
        return jsonify(ok=False, error="That block was not found (or is already removed)."), 404
    return jsonify(ok=True)


@app.route("/automation/undo/<int:activity_id>", methods=["POST"])
def automation_undo(activity_id):
    result = auto_actions.undo(get_conn(), activity_id, HANDLERS, post_write_hooks, _clinic_now())
    return jsonify(result)


@app.route("/patients/<int:patient_id>/activity")
def patient_activity_timeline(patient_id):
    conn = get_conn()
    patient = conn.execute("SELECT id, name, phone FROM patients WHERE id = ?", (patient_id,)).fetchone()
    if patient is None:
        return jsonify(ok=False, error="Patient not found."), 404
    return jsonify(ok=True, patient=dict(patient), activity=patient_activity.timeline_for_patient(conn, patient_id))


@app.route("/queue/<int:appointment_id>/<action>", methods=["POST"])
def queue_action(appointment_id, action):
    """Queue-tab buttons (Check in / Call / Done / No-show). The click on a
    specific visible row is the explicit human action, so this proposes and
    confirms in one request with no review card -- but it still goes through
    core.propose/core.confirm and the adapter, so it is audited like any
    other write."""
    intent = QUEUE_BUTTON_INTENTS.get(action)
    if intent is None:
        return jsonify(ok=False, error="Unknown queue action: {}".format(action)), 400

    conn = get_conn()
    row = conn.execute("SELECT appt_date FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
    if row is None:
        return jsonify(ok=False, error="Appointment not found"), 404
    if row["appt_date"] != date.today().isoformat():
        return jsonify(ok=False, error="Queue actions only apply to today's appointments.")

    slots = {"appointment_id": appointment_id}
    proposal_id = core.propose(conn, intent, slots, source_text="Queue tab button: {}".format(action))
    try:
        entity_type, entity_id = core.confirm(conn, proposal_id, HANDLERS)
    except Exception as e:
        try:
            core.reject(conn, proposal_id)  # don't leave a dead 'pending' proposal behind
        except Exception:
            pass
        return jsonify(ok=False, error=str(e))

    description = safe_describe(conn, intent, slots, entity_id)
    post_write_hooks(conn, intent, slots, entity_id)
    return jsonify(ok=True, message=description, entity_type=entity_type, entity_id=entity_id)


@app.route("/queue/partial")
def queue_partial():
    """Just the Queue tab's contents, for the 15-second auto-refresh (see
    static/dashboard_refresh.js) -- much lighter than re-fetching the whole
    dashboard, and it leaves other tabs' in-progress edits alone."""
    conn = get_conn()
    return render_template("_queue_panel.html", **queue_panel_context(conn, request.args.get("date")))


@app.route("/calendar/status/partial")
def calendar_status_partial():
    """Just the Appointments tab's status strip, polled while the tab is open
    (see static/calendar.js). The iframe is never re-rendered: a reload would
    reset the person's view."""
    return render_template("_calendar_status.html", **calendar_panel_context(get_conn()))


@app.route("/calendar/sync", methods=["POST"])
def calendar_sync_now():
    """The "Sync now" button: queue a full reconcile (today..+30 days) and
    return immediately -- the work happens on the sync worker thread."""
    if not gcal_config.is_configured():
        return jsonify(ok=False, error="Google Calendar sync is not set up yet."), 409
    conn = get_conn()
    try:
        gcal_sync.request_full_sync(conn, kick=_gcal_kick)
        pending = gcal_sync.sync_status(conn)["pending"]
    except Exception:
        _logger.exception("could not queue a calendar sync")
        return jsonify(ok=False, error="Could not queue the sync -- see the app log."), 500
    return jsonify(ok=True, pending=pending)


@app.route("/notifications/<int:notification_id>/retry", methods=["POST"])
def notification_retry(notification_id):
    """Staff clicked Retry on a blocked/failed notification. The 24-hour
    window is re-checked at send time, so this can block again."""
    conn = get_conn()
    if not notify.retry(conn, notification_id):
        return jsonify(ok=False, error="Only blocked or failed notifications can be retried.")
    try:
        sender, dry_run = notify.resolve_sender(NOTIFY_SENDER)
        notify.flush(conn, sender, dry_run=dry_run)
    except Exception:
        _logger.exception("flush after retry failed")
    status = conn.execute("SELECT status FROM notifications WHERE id = ?", (notification_id,)).fetchone()["status"]
    return jsonify(ok=True, status=status)


if __name__ == "__main__":
    # socketio.run (not app.run): Flask-SocketIO needs to own the dev
    # server's request loop so the /socket.io WebSocket transport (see
    # clinic/realtime_voice.py) is served alongside the regular HTTP
    # routes, in this same single process. use_reloader=False -- the
    # reloader's child-process restart would tear down the module-level
    # SarvamAI realtime sessions/threads uncleanly.
    #
    # threaded=True is no longer needed to keep the WhatsApp audio path
    # concurrent: SocketIO's own dev server (via simple-websocket) already
    # serves each connection on its own thread. WAL mode (clinic/db.py)
    # already makes concurrent writes safe.
    #
    # The notification scheduler (flush outbox + day-before/morning
    # reminders) is started here, never at import time, so importing app
    # (e.g. from tests) can never start a background thread.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    scheduler.start(get_conn, sender_override=lambda: NOTIFY_SENDER, calendar_client=_gcal_client)
    socketio.run(app, debug=False, port=int(os.environ.get("PORT", "5050")), use_reloader=False,
                 allow_unsafe_werkzeug=True)
