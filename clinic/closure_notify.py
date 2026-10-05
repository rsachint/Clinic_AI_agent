"""The WhatsApp notice a patient gets when a closure moves or cancels their
appointment. Fixed templates in English, Hindi and Hinglish -- the same rule as
clinic/notify.py: only deterministic code fills the blanks, and the reason and
the extra line are the staff's own words, inserted verbatim.

A moved patient gets two buttons: Accept (keep the new slot) and Choose another
(pick a different branch, day or time in the normal chat). A cancelled patient
gets a Book again button. The button ids are read by clinic/conversation.py.
"""

import logging
from datetime import timedelta

from clinic import branches, notify
from clinic.whatsapp import phone_to_wa_id

_logger = logging.getLogger(__name__)

TEMPLATES = {
    "closure_moved": {
        "en": ("{greet} we're sorry: we cannot see patients at {from_branch} on {old_date}{reason}.\n"
               "Your {old_time} appointment has been moved to:\n{new_date}, {new_time}\n{where}{message}\n"
               "Tap Accept to keep it, or Choose another to pick a different branch or time."),
        "hi": ("{greet} क्षमा करें: {old_date} को {from_branch} में हम मरीज़ों को नहीं देख पाएँगे{reason}।\n"
               "आपकी {old_time} की अपॉइंटमेंट अब यहाँ कर दी गई है:\n{new_date}, {new_time}\n{where}{message}\n"
               "इसे रखने के लिए \"मंज़ूर\" दबाएँ, या दूसरी ब्रांच/समय के लिए \"दूसरा चुनें\"।"),
        "hinglish": ("{greet} maaf kijiye: {old_date} ko {from_branch} mein hum mareezon ko nahi dekh paayenge{reason}.\n"
                     "Aapki {old_time} ki appointment ab yahan kar di gayi hai:\n{new_date}, {new_time}\n{where}{message}\n"
                     "Ise rakhne ke liye Accept dabayein, ya doosri branch/time ke liye Choose another."),
    },
    "closure_cancelled": {
        "en": ("{greet} we're sorry: we cannot see patients at {from_branch} on {old_date}{reason}, so your "
               "{old_time} appointment has been cancelled.{message}\nTap Book again to pick a new time."),
        "hi": ("{greet} क्षमा करें: {old_date} को {from_branch} में हम मरीज़ों को नहीं देख पाएँगे{reason}, इसलिए आपकी "
               "{old_time} की अपॉइंटमेंट रद्द कर दी गई है।{message}\nनया समय चुनने के लिए \"फिर से बुक करें\" दबाएँ।"),
        "hinglish": ("{greet} maaf kijiye: {old_date} ko {from_branch} mein hum mareezon ko nahi dekh paayenge{reason}, "
                     "isliye aapki {old_time} ki appointment cancel kar di gayi hai.{message}\nNaya time chunne ke liye Book again dabayein."),
    },
}

BUTTONS = {
    "accept": {"en": "Accept", "hi": "मंज़ूर", "hinglish": "Accept"},
    "change": {"en": "Choose another", "hi": "दूसरा चुनें", "hinglish": "Choose another"},
    "book": {"en": "Book again", "hi": "फिर से बुक करें", "hinglish": "Book again"},
}

_REASON_WORD = {"en": " ({})", "hi": " ({})", "hinglish": " ({})"}


def _as_now(now):
    """notify wants its two-clock Now; accept a plain local datetime too."""
    if now is None:
        return notify.Now.real()
    if isinstance(now, notify.Now):
        return now
    return notify.Now(now, now - timedelta(hours=5, minutes=30))     # the clinic's clock is IST


def accept_choice(move_id):
    return "closure:accept:{}".format(move_id)


def change_choice(move_id):
    return "closure:change:{}".format(move_id)


def _lang(language):
    return language if language in ("en", "hi", "hinglish") else "en"


def compose(conn, move, closure, appointment, language):
    """(text, interactive) for one closure_moves row."""
    lang = _lang(language)
    name = appointment["name"]
    values = {
        "greet": notify._greet(lang, name),
        "from_branch": branches.branch_label(conn, move["from_branch_id"]),
        "old_date": notify.format_date(move["from_date"], lang),
        "old_time": notify.format_time(move["from_time"], lang),
        "reason": _REASON_WORD[lang].format(closure["reason"]) if closure["reason"] else "",
        "message": "\n{}".format(closure["message"]) if closure["message"] else "",
        "new_date": "", "new_time": "", "where": "",
    }
    if move["action"] == "move":
        values["new_date"] = notify.format_date(move["to_date"], lang)
        values["new_time"] = notify.format_time(move["to_time"], lang)
        doctor_id = branches.doctor_at(conn, move["to_branch_id"], move["to_date"], move["to_time"])
        where = notify.branch_line(conn, move["to_branch_id"], doctor_id) or branches.branch_label(conn, move["to_branch_id"])
        values["where"] = where
        text = TEMPLATES["closure_moved"][lang].format(**values)
        # `where` and `message` end without a newline of their own; the template supplies it.
        buttons = [{"id": accept_choice(move["id"]), "title": BUTTONS["accept"][lang]},
                   {"id": change_choice(move["id"]), "title": BUTTONS["change"][lang]}]
    else:
        text = TEMPLATES["closure_cancelled"][lang].format(**values)
        buttons = [{"id": "menu:book", "title": BUTTONS["book"][lang]}]
    return text.strip(), {"type": "button", "buttons": buttons}


def notify_move(conn, move_id, now=None):
    """Queue the notice for one move (does not send; the caller flushes). Returns
    the notification row id, or None (already queued, or no such move)."""
    move = conn.execute("SELECT * FROM closure_moves WHERE id = ?", (move_id,)).fetchone()
    if move is None or move["result"] != "done":
        return None
    closure = conn.execute("SELECT * FROM closures WHERE id = ?", (move["closure_id"],)).fetchone()
    appointment = notify._appointment_context(conn, move["appointment_id"])
    if closure is None or appointment is None:
        return None
    wa_id = phone_to_wa_id(appointment["phone"]) if appointment["phone"] else None
    language = notify.patient_language(conn, wa_id)
    text, interactive = compose(conn, move, closure, appointment, language if language != "bilingual" else "en")
    event = "closure_moved" if move["action"] == "move" else "closure_cancelled"
    return notify.enqueue(
        conn, event=event, dedup_key="{}:{}".format(event, move_id), body=text, wa_id=wa_id,
        appointment_id=move["appointment_id"], language=language, now=_as_now(now), interactive=interactive)
