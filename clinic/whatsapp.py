import os
import re
import time

import httpx

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"

# Fixed, non-AI-composed acknowledgment (plan §13.5) -- sent on every inbound
# message before classification runs, so a patient is never left waiting on
# transcription/classification succeeding. Never route this through the LLM
# or answer.py's composer: it's low-stakes text, but there's no reason to
# introduce a variable-output path where a fixed one works.
_ACK_TEXT = {
    "en": "Thanks, we've received your message and will get back to you shortly.",
    "hi": "धन्यवाद, आपका संदेश हमें मिल गया है। हम जल्द ही उत्तर देंगे।",
    "hinglish": "Dhanyawad, aapka message mil gaya hai. Hum jald hi reply karenge.",
    # Used when the language can't be known (voice notes are acknowledged
    # before they're transcribed) or isn't one we have a line for.
    "bilingual": "Thanks, we've received your message. Dhanyawad, hum jald hi reply karenge.",
}

# The reply matches the sender's language. Script is the reliable signal for
# Hindi; Roman-script Hinglish vs English is told apart by a small set of
# unambiguous Hinglish words, deterministic like every other classifier here.
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_HINGLISH_WORDS = frozenset((
    "haan", "han", "nahi", "nahin", "kya", "hai", "hain", "hum", "aap", "aapka",
    "aapki", "mera", "meri", "mujhe", "kal", "aaj", "chahiye", "karo", "kijiye",
    "dena", "batao", "bata", "bulao", "aaunga", "aaungi", "aayenge", "dhanyawad",
    "shukriya", "theek", "thik", "accha", "kab", "kaise", "kitne", "kaun",
    "kyun", "abhi", "baje", "karna", "karenge", "milna", "dikhana",
))
_WORD = re.compile(r"[a-z]+")

# A burst of messages gets one acknowledgment, and a message that has sat in
# Meta's retry queue for a while (e.g. while the webhook was down) gets none --
# "we've received your message" is wrong days later.
ACK_COOLDOWN_MINUTES = 10
ACK_MAX_MESSAGE_AGE_SECONDS = 3600


def phone_to_wa_id(phone):
    """Best-effort conversion of a stored phone number to a WhatsApp wa_id
    (country code + number, digits only). The same India-specific heuristic
    as clinic.entity_resolution.last10_digits, not a general E.164 parser:

      - a 10-digit number gets India's "91" prefixed:  "9876543210" -> "919876543210"
      - an 11-digit number with a trunk "0":           "09876543210" -> "919876543210"
      - anything longer is assumed to already carry its country code and is
        returned digits-only and otherwise unchanged:  "+91 98765 43210" -> "919876543210"
      - fewer than 10 digits (or no digits) is not a usable number -> None
    """
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(digits) == 10:
        return "91" + digits
    if len(digits) == 11 and digits.startswith("0"):
        return "91" + digits[1:]
    if len(digits) >= 11:
        return digits
    return None


def detect_message_language(text):
    """'hi' (Devanagari), 'hinglish' (Roman-script Hindi), 'en', or
    'bilingual' when there's no text or it's in a script we have no reply for."""
    if not text:
        return "bilingual"
    if _DEVANAGARI.search(text):
        return "hi"
    if any(ch.isalpha() and ord(ch) > 127 for ch in text):
        return "bilingual"
    if any(word in _HINGLISH_WORDS for word in _WORD.findall(text.lower())):
        return "hinglish"
    return "en"


def acknowledgment_text(language="bilingual"):
    return _ACK_TEXT.get(language, _ACK_TEXT["bilingual"])


def is_stale(message_timestamp, now=None):
    """True if the message is older than ACK_MAX_MESSAGE_AGE_SECONDS.
    `message_timestamp` is Meta's unix-seconds value, or None if absent."""
    if message_timestamp is None:
        return False
    now = time.time() if now is None else now
    return now - message_timestamp > ACK_MAX_MESSAGE_AGE_SECONDS


def claim_acknowledgment(conn, wa_id, now=None):
    """Atomically reserve the right to acknowledge `wa_id`. Returns the new
    wa_acks row id, or None if this sender was already acknowledged within
    ACK_COOLDOWN_MINUTES. A single INSERT ... WHERE NOT EXISTS, so two
    messages arriving at once can't both pass the check."""
    now = time.time() if now is None else now
    cur = conn.execute(
        "INSERT INTO wa_acks (wa_id, sent_at) "
        "SELECT ?, datetime(?, 'unixepoch') "
        "WHERE NOT EXISTS (SELECT 1 FROM wa_acks WHERE wa_id = ? "
        "AND sent_at >= datetime(?, 'unixepoch', ?))",
        (wa_id, now, wa_id, now, "-{} minutes".format(ACK_COOLDOWN_MINUTES)),
    )
    conn.commit()
    return cur.lastrowid if cur.rowcount else None


def release_acknowledgment(conn, ack_id):
    """Undo a claim when the send itself failed, so the next message can retry."""
    conn.execute("DELETE FROM wa_acks WHERE id = ?", (ack_id,))
    conn.commit()


def verify_webhook(args):
    """Meta's GET verification handshake. `args` is request.args (or any
    dict-like of query params). Returns the hub.challenge string to echo
    back on success, or None if verification fails."""
    verify_token = os.environ["WHATSAPP_VERIFY_TOKEN"]
    if args.get("hub.mode") == "subscribe" and args.get("hub.verify_token") == verify_token:
        return args.get("hub.challenge")
    return None


def parse_webhook_payload(payload):
    """Extract the first inbound message from a Meta webhook POST body.
    Returns None if this isn't a message event (Meta also sends status
    updates -- delivered/read receipts -- to the same URL)."""
    try:
        value = payload["entry"][0]["changes"][0]["value"]
        message = value["messages"][0]
    except (KeyError, IndexError, TypeError):
        return None

    wa_id = message.get("from")
    wa_message_id = message.get("id")
    msg_type = message.get("type")
    try:
        timestamp = int(message["timestamp"])
    except (KeyError, TypeError, ValueError):
        timestamp = None

    if msg_type == "text":
        return {
            "wa_message_id": wa_message_id,
            "wa_id": wa_id,
            "message_type": "text",
            "text": message["text"]["body"],
            "media_id": None,
            "timestamp": timestamp,
        }
    if msg_type == "audio":
        return {
            "wa_message_id": wa_message_id,
            "wa_id": wa_id,
            "message_type": "audio",
            "text": None,
            "media_id": message["audio"]["id"],
            "timestamp": timestamp,
        }
    if msg_type == "interactive":
        # A tap on a reply button or a list row we sent. Stored like text
        # (message_type 'text': the wa_messages CHECK constraint is unchanged)
        # with the human-readable title as the text, plus the choice id that
        # clinic/conversation.py understands.
        interactive = message.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply")
        if not isinstance(reply, dict) or not reply.get("id"):
            return None
        return {
            "wa_message_id": wa_message_id,
            "wa_id": wa_id,
            "message_type": "text",
            "text": reply.get("title") or "",
            "media_id": None,
            "timestamp": timestamp,
            "choice_id": str(reply["id"]),
        }
    return None  # other message types (image, location, sticker, ...) not handled yet


def download_media(media_id):
    """Two-step Graph API fetch: resolve a temporary CDN URL, then download
    the bytes. Returns raw audio bytes."""
    token = os.environ["WHATSAPP_ACCESS_TOKEN"]
    headers = {"Authorization": "Bearer {}".format(token)}
    meta = httpx.get("{}/{}".format(GRAPH_API_BASE, media_id), headers=headers, timeout=15)
    meta.raise_for_status()
    media_url = meta.json()["url"]
    media = httpx.get(media_url, headers=headers, timeout=30)
    media.raise_for_status()
    return media.content


# --- Interactive messages (reply buttons and lists) -------------------------
# WhatsApp Cloud API limits. The builders below are PURE functions that return
# the request body (unit-tested for shape); only send_interactive() touches the
# network, and only when called with real credentials.
BODY_MAX = 1024
BUTTON_MAX = 3
BUTTON_TITLE_MAX = 20
ROW_MAX = 10
ROW_TITLE_MAX = 24
ROW_DESCRIPTION_MAX = 72
LIST_BUTTON_MAX = 20
SECTION_TITLE_MAX = 24
CHOICE_ID_MAX = 256


def _check_body(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("interactive message needs body text")
    if len(text) > BODY_MAX:
        raise ValueError("interactive body is {} chars (max {})".format(len(text), BODY_MAX))


def _check_choice(choice_id, title, title_max, what):
    if not choice_id or len(str(choice_id)) > CHOICE_ID_MAX:
        raise ValueError("{} id must be 1-{} chars".format(what, CHOICE_ID_MAX))
    if not isinstance(title, str) or not title.strip():
        raise ValueError("{} {!r} needs a title".format(what, choice_id))
    if len(title) > title_max:
        raise ValueError("{} title {!r} is {} chars (max {})".format(what, title, len(title), title_max))


def _pairs(items):
    """Accept [(id, title), ...] or [{"id":..,"title":..,"description":..}, ...]."""
    out = []
    for item in items:
        if isinstance(item, dict):
            out.append((item.get("id"), item.get("title"), item.get("description")))
        else:
            out.append((item[0], item[1], item[2] if len(item) > 2 else None))
    return out


def build_text_body(to, text):
    return {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}


def build_reply_buttons_body(to, body_text, buttons):
    """Cloud API body for up to 3 quick-reply buttons (title <= 20 chars)."""
    _check_body(body_text)
    items = _pairs(buttons)
    if not 1 <= len(items) <= BUTTON_MAX:
        raise ValueError("reply buttons: 1-{} allowed, got {}".format(BUTTON_MAX, len(items)))
    if len({i for i, _, _ in items}) != len(items):
        raise ValueError("reply button ids must be unique")
    for choice_id, title, _ in items:
        _check_choice(choice_id, title, BUTTON_TITLE_MAX, "button")
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": str(i), "title": t}} for i, t, _ in items
            ]},
        },
    }


def build_list_body(to, body_text, button_label, rows, section_title=None):
    """Cloud API body for a list message: up to 10 rows (title <= 24 chars,
    optional description <= 72) in one section, opened by a button whose
    label is <= 20 chars."""
    _check_body(body_text)
    if not isinstance(button_label, str) or not button_label.strip() or len(button_label) > LIST_BUTTON_MAX:
        raise ValueError("list button label must be 1-{} chars".format(LIST_BUTTON_MAX))
    items = _pairs(rows)
    if not 1 <= len(items) <= ROW_MAX:
        raise ValueError("list rows: 1-{} allowed, got {}".format(ROW_MAX, len(items)))
    if len({i for i, _, _ in items}) != len(items):
        raise ValueError("list row ids must be unique")
    built = []
    for choice_id, title, description in items:
        _check_choice(choice_id, title, ROW_TITLE_MAX, "row")
        row = {"id": str(choice_id), "title": title}
        if description:
            if len(description) > ROW_DESCRIPTION_MAX:
                raise ValueError("row description is {} chars (max {})".format(len(description), ROW_DESCRIPTION_MAX))
            row["description"] = description
        built.append(row)
    section = {"rows": built}
    if section_title:
        if len(section_title) > SECTION_TITLE_MAX:
            raise ValueError("section title is {} chars (max {})".format(len(section_title), SECTION_TITLE_MAX))
        section["title"] = section_title
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body_text},
            "action": {"button": button_label, "sections": [section]},
        },
    }


def build_interactive_body(to, text, spec):
    """From the JSON spec stored in notifications.interactive_json:
    {"type": "button", "buttons": [{"id", "title"}]} or
    {"type": "list", "button": "Choose time", "rows": [{"id", "title"}]}."""
    kind = (spec or {}).get("type")
    if kind == "button":
        return build_reply_buttons_body(to, text, spec.get("buttons") or [])
    if kind == "list":
        return build_list_body(to, text, spec.get("button") or "", spec.get("rows") or [], spec.get("section"))
    raise ValueError("unknown interactive type: {!r}".format(kind))


def _post_message(body):
    token = os.environ["WHATSAPP_ACCESS_TOKEN"]
    phone_number_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
    url = "{}/{}/messages".format(GRAPH_API_BASE, phone_number_id)
    headers = {"Authorization": "Bearer {}".format(token)}
    response = httpx.post(url, headers=headers, json=body, timeout=15)
    response.raise_for_status()
    return response.json()


def send_message(to, text):
    """Plain freeform text reply. Used for the fixed receipt acknowledgment
    now, and for outbound recall later (§10 step 6) -- never for AI-generated
    content."""
    return _post_message(build_text_body(to, text))


def send_interactive(to, text, spec):
    """Reply buttons / a list message. The body is validated (and so can raise
    ValueError) BEFORE any credential is read or any request is made."""
    return _post_message(build_interactive_body(to, text, spec))
