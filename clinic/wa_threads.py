"""Conversation threads for the staff dashboard (Patient messages tab):
for each WhatsApp sender, the chronological transcript -- inbound
`wa_messages` plus outbound `notifications` (agent replies, staff messages,
status replies, reminders, ...) -- with the agent/human mode and whether a
free-form reply would currently be allowed (WhatsApp's 24-hour window).

Read-only and deterministic; nothing here talks to WhatsApp.
"""

import json
import re

from clinic import notify
from clinic.entity_resolution import last10_digits, resolve_patient_by_phone

OPEN_STATUSES = ("classified", "needs_human_reply", "error")

_CONV_REPLY_KEY = re.compile(r"^conv_reply:(\d+):(\d+)$")
_LAST = 10 ** 12


def _key(wa_id):
    digits = last10_digits(wa_id or "")
    return digits if len(digits) == 10 else (wa_id or "")


def _in_clause(values):
    return "({})".format(", ".join("?" for _ in values))


def _flag(slots_json):
    try:
        return (json.loads(slots_json or "{}")).get("flag")
    except (ValueError, AttributeError):
        return None


def _options(interactive_json):
    if not interactive_json:
        return []
    try:
        spec = json.loads(interactive_json)
    except ValueError:
        return []
    return [o.get("title") for o in (spec.get("buttons") or spec.get("rows") or []) if o.get("title")]


def conversation_threads(conn, max_threads=25, per_thread=60, now_utc=None):
    """Newest-active senders first (an open emergency always first). Each
    thread: {wa_id, name, mode, language, goal, step, window_open, last_at,
    open_items, emergency, items: [...]} where items are chronological dicts
    with dir 'in' / 'out'."""
    now_utc = now_utc or notify.Now.real().utc

    groups = {}
    for row in conn.execute("SELECT wa_id, MAX(id) AS last_id FROM wa_messages GROUP BY wa_id ORDER BY last_id DESC"):
        groups.setdefault(_key(row["wa_id"]), []).append(row["wa_id"])
    keys = list(groups)[:max_threads]

    threads = []
    for key in keys:
        wa_ids = groups[key]
        primary = wa_ids[0]
        marks = _in_clause(wa_ids)

        items = []
        for r in conn.execute(
            "SELECT id, received_at, message_type, raw_text, status, slots_json, agent_handled FROM wa_messages "
            "WHERE wa_id IN {} ORDER BY id DESC LIMIT ?".format(marks), tuple(wa_ids) + (per_thread,)
        ):
            items.append({
                "dir": "in", "id": r["id"], "at": r["received_at"], "text": r["raw_text"] or "",
                "voice": r["message_type"] == "audio", "status": r["status"], "flag": _flag(r["slots_json"]),
                "handled_by_agent": bool(r["agent_handled"]), "_pos": (r["id"], 0),
            })
        for r in conn.execute(
            "SELECT id, created_at, event, body, status, error, interactive_json, dedup_key FROM notifications "
            "WHERE wa_id IN {} ORDER BY id DESC LIMIT ?".format(marks), tuple(wa_ids) + (per_thread,)
        ):
            # Timestamps have one-second resolution, so a reply and the next
            # message can tie. An agent reply sorts right after the inbound
            # message it answers (its dedup_key names it); anything else
            # sorts after the inbound messages of that second.
            m = _CONV_REPLY_KEY.match(r["dedup_key"] or "")
            pos = (int(m.group(1)), 1 + int(m.group(2))) if m else (_LAST, r["id"])
            items.append({
                "dir": "out", "id": r["id"], "at": r["created_at"], "text": r["body"], "event": r["event"],
                "status": r["status"], "error": r["error"], "options": _options(r["interactive_json"]),
                "retry": r["status"] in ("failed", "blocked_no_window"), "_pos": pos,
            })
        items.sort(key=lambda i: (i["at"], i["_pos"]))
        items = items[-per_thread:]
        for item in items:
            del item["_pos"]

        session = None
        for wa_id in wa_ids:
            session = conn.execute("SELECT * FROM wa_sessions WHERE wa_id = ?", (wa_id,)).fetchone()
            if session:
                break

        patient = resolve_patient_by_phone(conn, primary)
        name = None
        if patient is not None:
            row = conn.execute("SELECT name FROM patients WHERE id = ?", (patient.id,)).fetchone()
            name = row["name"] if row else None

        open_rows = conn.execute(
            "SELECT slots_json FROM wa_messages WHERE wa_id IN {} AND status IN ('classified', 'needs_human_reply', 'error') "
            "AND COALESCE(agent_handled, 0) = 0".format(marks), tuple(wa_ids)
        ).fetchall()

        threads.append({
            "wa_id": primary,
            "name": name,
            "mode": session["mode"] if session else "agent",
            "language": session["language"] if session else None,
            "goal": session["goal"] if session else None,
            "step": session["step"] if session else None,
            "window_open": notify.in_window(conn, primary, now_utc),
            "last_at": items[-1]["at"] if items else None,
            "open_items": len(open_rows),
            "emergency": any(_flag(r["slots_json"]) == "emergency" for r in open_rows),
            "items": items,
        })

    threads.sort(key=lambda t: 0 if t["emergency"] else 1)
    return threads


def known_sender(conn, wa_id):
    """True if this wa_id has ever messaged us -- the only numbers staff may
    reply to or take over from the thread view."""
    return conn.execute("SELECT 1 FROM wa_messages WHERE wa_id = ? LIMIT 1", (wa_id,)).fetchone() is not None
