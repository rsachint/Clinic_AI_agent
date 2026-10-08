"""Glue between the conversation agent (clinic/conversation.py) and the rest
of the app: the wa_messages inbox row, and the notifications outbox.

Kept out of app.py so it can be tested without Flask, and out of
conversation.py so the dialog manager stays free of delivery concerns.

Rules this module enforces
--------------------------
* Replies go through the outbox (clinic/notify.py): logged, 24h-window
  checked, deduplicated per inbound wa_message, visible in the patient's
  thread. They are by construction inside the window (the patient just wrote).
* A hand-off becomes an ordinary `classified` inbox row -- the same shape the
  dashboard already approves. Nothing here writes an appointment; when the
  dialog's `auto` callable committed one automatically (clinic/auto_actions.py)
  the inbox row is recorded as handled by the agent, linked to its proposal.
* Delivery trouble never propagates: flush_replies() cannot raise.
"""

import json
import logging
import threading
from collections import defaultdict

from clinic import conv_templates as ct
from clinic import conversation, notify, patient_activity

_logger = logging.getLogger(__name__)

# One message at a time per sender, so two quick taps can't race on the same
# session row. (A single-process appliance: an in-process lock is enough.)
_LOCKS = defaultdict(threading.Lock)
_LOCKS_GUARD = threading.Lock()


def sender_lock(wa_id):
    with _LOCKS_GUARD:
        return _LOCKS[wa_id]


def enqueue_replies(conn, wa_id, msg_id, result):
    """Put the agent's replies in the outbox. Returns the (possibly amended)
    result: if a status answer could not be produced, the patient is told a
    person will reply and the message is flagged for staff."""
    language = result.language
    for seq, reply in enumerate(result.replies):
        if reply.kind == "status":
            queued = notify.notify_status_reply(conn, wa_id, reply.appointment_id, msg_id, language=language)
            if queued is None and not _already_queued(conn, wa_id, msg_id):
                result.escalate = result.escalate or "status_unavailable"
                notify.enqueue_conv_reply(
                    conn, wa_id, ct.text("escalate", language), msg_id, seq, language=language)
            continue
        notify.enqueue_conv_reply(
            conn, wa_id, reply.text, msg_id, seq, interactive=reply.interactive(), language=language)
    return result


def _already_queued(conn, wa_id, msg_id):
    """True if a status_reply for this inbound message is already in the outbox
    (a retried webhook): then 'None' means duplicate, not failure."""
    row = conn.execute(
        "SELECT 1 FROM notifications WHERE event = 'status_reply' AND dedup_key LIKE ? AND wa_id = ?",
        ("status_reply:%:msg{}".format(msg_id), wa_id),
    ).fetchone()
    return row is not None


def record_outcome(conn, msg_id, text, result):
    """Update the inbox row for this inbound message.

    * committed automatically -> `dismissed`, agent_handled=1, linked to the
                       [auto:...] proposal: nothing for staff to approve
    * hand-off      -> `classified` proposal staff approve (intent + prefilled slots)
    * needs a human -> `needs_human_reply`, with slots_json.flag saying why
                       (emergency / clinical / escalation / human_mode)
    * replies only  -> `dismissed` + agent_handled=1: nothing for staff to do,
                       and excluded from the actionable inbox.
    """
    auto = result.auto
    if auto is not None and auto.kind == "committed" and result.flag:
        # Committed automatically, but the same message also needs a person
        # (e.g. it mentioned a symptom): the booking is done, the flag is not
        # lost -- staff still see the item and can dismiss it after calling.
        conn.execute(
            "UPDATE wa_messages SET patient_id = ?, raw_text = ?, intent = ?, slots_json = ?, proposal_id = ?, "
            "status = 'needs_human_reply', agent_handled = 0 WHERE id = ?",
            (result.patient_id, text, auto.intent,
             json.dumps({"flag": result.flag, "auto": True, "appointment_id": auto.appointment_id}),
             auto.proposal_id, msg_id),
        )
    elif auto is not None and auto.kind == "committed":
        conn.execute(
            "UPDATE wa_messages SET patient_id = ?, raw_text = ?, intent = ?, slots_json = ?, proposal_id = ?, "
            "status = 'dismissed', agent_handled = 1, resolved_at = datetime('now') WHERE id = ?",
            (result.patient_id, text, auto.intent,
             json.dumps({"auto": True, "appointment_id": auto.appointment_id}), auto.proposal_id, msg_id),
        )
    elif result.handoff is not None:
        handoff = result.handoff
        conn.execute(
            "UPDATE wa_messages SET patient_id = ?, raw_text = ?, intent = ?, slots_json = ?, "
            "status = 'classified', agent_handled = 0 WHERE id = ?",
            (result.patient_id, text, handoff.intent, json.dumps(handoff.slots, ensure_ascii=False), msg_id),
        )
    elif result.flag:
        slots = {"flag": result.flag}
        if result.escalate:
            slots["reason"] = result.escalate
        conn.execute(
            "UPDATE wa_messages SET patient_id = ?, raw_text = ?, intent = NULL, slots_json = ?, "
            "status = 'needs_human_reply', agent_handled = 0 WHERE id = ?",
            (result.patient_id, text, json.dumps(slots), msg_id),
        )
    else:
        conn.execute(
            "UPDATE wa_messages SET patient_id = ?, raw_text = ?, intent = NULL, slots_json = NULL, "
            "status = 'dismissed', agent_handled = 1, resolved_at = datetime('now') WHERE id = ?",
            (result.patient_id, text, msg_id),
        )
    conn.commit()


def write_activity(conn, wa_id, result, now):
    """The dialog's own patient_activity rows (a blocked time, an escalation
    mid-flow). Best effort: log() cannot raise."""
    patient_name = None
    if result.patient_id is not None:
        row = conn.execute("SELECT name FROM patients WHERE id = ?", (result.patient_id,)).fetchone()
        patient_name = row["name"] if row else None
    for item in result.activity:
        patient_activity.log(
            conn, event=item["event"], source="whatsapp-agent", wa_id=wa_id, patient_id=result.patient_id,
            patient_name=patient_name, appointment_id=item.get("appointment_id"), detail=item.get("detail"),
            meta=item.get("meta"), now=now)


def process_inbound(conn, msg_id, wa_id, text, choice_id=None, *, now=None, picker=None, auto=None, followup=None):
    """Run the dialog manager on one stored inbound message, queue its
    replies and record the outcome on the inbox row. Does NOT send: the
    caller flushes (see flush_replies). Raises only if the agent itself
    breaks, in which case the caller falls back to staff handling. `auto`
    is the automatic-commit callable and `followup` the follow-up button runner
    (see conversation.handle_inbound)."""
    with sender_lock(wa_id):
        result = conversation.handle_inbound(
            conn, wa_id, text, choice_id=choice_id, now=now, msg_id=msg_id, intent_picker=picker, auto=auto,
            followup=followup)
        enqueue_replies(conn, wa_id, msg_id, result)
        record_outcome(conn, msg_id, text, result)
        write_activity(conn, wa_id, result, now)
    return result


def flush_replies(conn, sender_override=None):
    """Deliver whatever is waiting in the outbox (the replies just queued).
    Waits a few seconds for another delivery in progress, then gives up --
    the scheduler's next tick sends the rest. Never raises."""
    try:
        sender, dry_run = notify.resolve_sender(sender_override)
        return notify.flush(conn, sender, dry_run=dry_run, lock_timeout=notify.CONV_FLUSH_WAIT_SECONDS)
    except Exception:
        _logger.exception("flushing conversation replies failed")
        return {}


def session_language(conn, wa_id):
    row = conn.execute("SELECT language FROM wa_sessions WHERE wa_id = ?", (wa_id,)).fetchone()
    return row["language"] if row and row["language"] else None


def is_conversation_request(row):
    """Was this inbox row produced by the conversation agent's hand-off?"""
    if row is None or row["status"] != "classified":
        return False
    if row["intent"] not in ("book_appointment", "reschedule_appointment", "cancel_appointment"):
        return False
    try:
        return (json.loads(row["slots_json"] or "{}")).get("via") == "conversation"
    except ValueError:
        return False


def after_resolution(conn, row, declined=False, sender_override=None):
    """Staff approved / rejected / dismissed an inbox item: release the slot
    held for it, and -- on a reject of a request made through the
    conversation -- tell the patient with the fixed `request_declined`
    notice. Best effort: never raises, never affects the resolution."""
    try:
        conversation.release_holds(conn, row["id"])
    except Exception:
        _logger.exception("could not release the slot hold for wa_message %s", row["id"])
    if not declined:
        return
    try:
        notify.notify_request_declined(
            conn, row["wa_id"], row["id"], language=session_language(conn, row["wa_id"]))
        flush_replies(conn, sender_override)
    except Exception:
        _logger.exception("could not notify the patient that wa_message %s was declined", row["id"])
        try:
            conn.rollback()
        except Exception:
            pass
