"""Patient notifications: a transactional outbox plus fixed message templates.

How it fits together
--------------------
* Something patient-visible happens (a booking is approved, a token shifts,
  it's the morning of the visit, ...). The code that noticed calls
  `enqueue()` / `notify_appointment()`, which only *inserts a row* into the
  `notifications` table. The row's `dedup_key` is UNIQUE, so enqueueing the
  same event twice (a retried hook, two scheduler ticks, a double click) is
  harmless.
* `flush(conn, sender)` later delivers pending rows. The sender is injected:
  `sender(wa_id, text)` raises on failure. Production uses
  clinic.whatsapp.send_message (WHATSAPP_NOTIFY_MODE=live, the default) or a
  no-op recorder (WHATSAPP_NOTIFY_MODE=dry_run); tests inject a fake.

Hard rules this module enforces
-------------------------------
* Every message is a FIXED TEMPLATE filled with deterministic values (names
  from the database, tokens/positions from clinic.token_queue, dates/times
  from the appointment row). No model composes or rephrases any of it.
* A notification failure must never fail or mask a successful write:
  `post_commit()` is the single entry point the web layer calls after a
  write has committed, and it cannot raise.
* WhatsApp only permits free-form messages within 24 hours of the patient's
  last inbound message. Outside that window `flush` does NOT attempt a send;
  the row becomes `blocked_no_window` (visible in the UI, retryable with
  `retry()`). META_TEMPLATES below holds the template bodies to submit to
  Meta for approval later; template *sending* is deliberately not
  implemented.
"""

import inspect
import json
import logging
import os
import re
import threading
import time
from collections import namedtuple
from datetime import date, datetime, timedelta, timezone

from clinic import branches, token_queue, whatsapp
from clinic.entity_resolution import last10_digits

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Timing constants -- the one place to tune when things fire.
# ---------------------------------------------------------------------------
REMINDER_DAY_BEFORE_FROM_HOUR = 18   # local hour on the day before from which the day-before reminder may go out
REMINDER_MORNING_FROM_HOUR = 8       # local hour on the day itself from which the morning reminder may go out
RECENT_CONTACT_SKIP_HOURS = 6        # skip the day-before reminder if the patient was just sent a booking/reschedule message
WINDOW_HOURS = 24                    # WhatsApp's free-form messaging window after a patient's last inbound message
RETRY_MAX_ATTEMPTS = 3               # total delivery attempts for a notification that keeps failing
RETRY_MAX_AGE_MINUTES = 120          # a failed notification older than this is left failed, not retried
CONV_RETRY_MAX_AGE_MINUTES = 5       # a failed conversational reply is stale after this: a late "which day?" confuses more than it helps
CONV_FLUSH_WAIT_SECONDS = 10         # how long a conversation reply may wait for another delivery in progress

EVENTS = (
    "booking_confirmed", "appointment_rescheduled", "appointment_cancelled",
    "token_changed", "reminder_day_before", "reminder_morning",
    "queue_two_ahead", "your_turn", "status_reply", "registered",
    # WhatsApp conversation agent (clinic/conversation.py) and staff takeover:
    "conv_reply", "staff_message", "request_declined",
    # Automatic / staff-direct appointment actions and their Undo:
    "appointment_cancelled_by_clinic", "appointment_reinstated",
    # A branch closure moved or cancelled the appointment (clinic/closure_notify.py):
    "closure_moved", "closure_cancelled",
)

# Events whose text states the patient's token. Sending one updates
# appointments.last_notified_token (see _remember_token).
_TOKEN_EVENTS = frozenset((
    "booking_confirmed", "appointment_rescheduled", "token_changed",
    "reminder_day_before", "reminder_morning", "queue_two_ahead",
    "your_turn", "status_reply", "appointment_reinstated",
))

LANGUAGES = ("en", "hi", "hinglish")  # plus "bilingual" = en + hinglish together


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------

class Now(namedtuple("Now", ["local", "utc"])):
    """Both clocks in one injectable value. `local` is the clinic's wall
    clock (decides which day "today" is, and reminder hours); `utc` is what
    SQLite's datetime('now') columns (wa_messages.received_at) are in, and
    decides the 24-hour window. Both naive datetimes."""

    @property
    def today(self):
        return self.local.date()

    @classmethod
    def real(cls):
        return cls(datetime.now(), datetime.now(timezone.utc).replace(tzinfo=None))


def _ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(text):
    return datetime.strptime(text.replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Formatting helpers (deterministic)
# ---------------------------------------------------------------------------

_EN_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_EN_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_HI_DAYS = ["सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"]
_HI_MONTHS = ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"]


def format_date(iso_date, lang):
    d = date.fromisoformat(iso_date)
    if lang == "hi":
        return "{} {} {}, {}".format(d.day, _HI_MONTHS[d.month - 1], d.year, _HI_DAYS[d.weekday()])
    return "{}, {} {} {}".format(_EN_DAYS[d.weekday()], d.day, _EN_MONTHS[d.month - 1], d.year)


def format_time(hhmm, lang):
    hour, minute = (int(x) for x in hhmm.split(":"))
    twelve = hour % 12 or 12
    clock = "{}:{:02d}".format(twelve, minute)
    if lang == "hi":
        if hour < 12:
            part = "सुबह"
        elif hour < 16:
            part = "दोपहर"
        elif hour < 20:
            part = "शाम"
        else:
            part = "रात"
        return "{} {}".format(part, clock)
    return "{} {}".format(clock, "AM" if hour < 12 else "PM")


def ahead_line(lang, ahead):
    if ahead is None:
        return ""
    if lang == "hi":
        return "आप अगले हैं।" if ahead == 0 else "आपसे पहले {} मरीज़ हैं।".format(ahead)
    if lang == "hinglish":
        return "Aap agle hain." if ahead == 0 else "Aapse pehle {} patient hain.".format(ahead)
    if ahead == 0:
        return "You are next."
    return "{} patient{} ahead of you.".format(ahead, " is" if ahead == 1 else "s are")


def _greet(lang, name):
    word = {"en": "Hello", "hi": "नमस्ते", "hinglish": "Namaste"}[lang]
    return "{} {},".format(word, name) if name else "{},".format(word)


# ---------------------------------------------------------------------------
# Templates. Keys are "<event>" or "<event>_<variant>". {placeholders} are the
# only variable parts; every value comes from deterministic code.
# ---------------------------------------------------------------------------

TEMPLATES = {
    "booking_confirmed_future": {
        "en": "{greet} your appointment is booked for {date} at {time}. Your token is currently {token} (provisional) - your final token is confirmed on the day.",
        "hi": "{greet} आपकी अपॉइंटमेंट {date} को {time} पर बुक हो गई है। आपका टोकन अभी {token} है (अस्थायी) - अंतिम टोकन उसी दिन कन्फर्म होगा।",
        "hinglish": "{greet} aapki appointment {date} ko {time} par book ho gayi hai. Aapka token abhi {token} hai (provisional) - final token usi din confirm hoga.",
    },
    "booking_confirmed_today": {
        "en": "{greet} your appointment is booked for today at {time}. Your token is {token}. {ahead}",
        "hi": "{greet} आपकी अपॉइंटमेंट आज {time} पर बुक हो गई है। आपका टोकन {token} है। {ahead}",
        "hinglish": "{greet} aapki appointment aaj {time} par book ho gayi hai. Aapka token {token} hai. {ahead}",
    },
    "appointment_rescheduled_future": {
        "en": "{greet} your appointment has been moved to {date} at {time}. Your token is currently {token} (provisional) - your final token is confirmed on the day.",
        "hi": "{greet} आपकी अपॉइंटमेंट अब {date} को {time} पर है। आपका टोकन अभी {token} है (अस्थायी) - अंतिम टोकन उसी दिन कन्फर्म होगा।",
        "hinglish": "{greet} aapki appointment ab {date} ko {time} par hai. Aapka token abhi {token} hai (provisional) - final token usi din confirm hoga.",
    },
    "appointment_rescheduled_today": {
        "en": "{greet} your appointment has been moved to today at {time}. Your token is {token}. {ahead}",
        "hi": "{greet} आपकी अपॉइंटमेंट अब आज {time} पर है। आपका टोकन {token} है। {ahead}",
        "hinglish": "{greet} aapki appointment ab aaj {time} par hai. Aapka token {token} hai. {ahead}",
    },
    "appointment_cancelled": {
        "en": "{greet} your appointment on {date} at {time} has been cancelled. Reply to this message if you would like to book a new one.",
        "hi": "{greet} {date} को {time} की आपकी अपॉइंटमेंट रद्द कर दी गई है। नई अपॉइंटमेंट के लिए इस संदेश का जवाब दें।",
        "hinglish": "{greet} {date} ko {time} ki aapki appointment cancel kar di gayi hai. Nayi appointment ke liye is message ka reply karein.",
    },
    # The clinic (not the patient) cancelled: an automatic booking that staff
    # had to undo. Asks for a new time instead of implying the patient asked.
    "appointment_cancelled_by_clinic": {
        "en": "{greet} sorry, the clinic had to cancel your appointment on {date} at {time}. Please send us a new preferred time and we will book it.",
        "hi": "{greet} क्षमा करें, क्लिनिक को {date} को {time} की आपकी अपॉइंटमेंट रद्द करनी पड़ी। कृपया हमें कोई नया पसंदीदा समय भेजें, हम उसे बुक कर देंगे।",
        "hinglish": "{greet} maaf kijiye, clinic ko {date} ko {time} ki aapki appointment cancel karni padi. Kripya hamein koi naya pasandeeda time bhejein, hum use book kar denge.",
    },
    # Undo of an automatic cancellation: the original slot is back.
    "appointment_reinstated_future": {
        "en": "{greet} your appointment on {date} at {time} has been reinstated. Your token is currently {token} (provisional) - your final token is confirmed on the day.",
        "hi": "{greet} {date} को {time} की आपकी अपॉइंटमेंट फिर से बहाल कर दी गई है। आपका टोकन अभी {token} है (अस्थायी) - अंतिम टोकन उसी दिन कन्फर्म होगा।",
        "hinglish": "{greet} {date} ko {time} ki aapki appointment dobara bahal kar di gayi hai. Aapka token abhi {token} hai (provisional) - final token usi din confirm hoga.",
    },
    "appointment_reinstated_today": {
        "en": "{greet} your appointment today at {time} has been reinstated. Your token is {token}. {ahead}",
        "hi": "{greet} आज {time} की आपकी अपॉइंटमेंट फिर से बहाल कर दी गई है। आपका टोकन {token} है। {ahead}",
        "hinglish": "{greet} aaj {time} ki aapki appointment dobara bahal kar di gayi hai. Aapka token {token} hai. {ahead}",
    },
    "token_changed": {
        "en": "{greet} update: your token for today is now {token} (it was {old_token}). {ahead}",
        "hi": "{greet} अपडेट: आज के लिए आपका टोकन अब {token} है (पहले {old_token} था)। {ahead}",
        "hinglish": "{greet} update: aaj ke liye aapka token ab {token} hai (pehle {old_token} tha). {ahead}",
    },
    "reminder_day_before": {
        "en": "{greet} reminder: you have an appointment tomorrow, {date} at {time}. Your token is currently {token} (provisional); your final token will be sent tomorrow morning.",
        "hi": "{greet} याद दिलाना: कल, {date} को {time} पर आपकी अपॉइंटमेंट है। आपका टोकन अभी {token} है (अस्थायी); अंतिम टोकन कल सुबह भेजा जाएगा।",
        "hinglish": "{greet} yaad dilana: kal, {date} ko {time} par aapki appointment hai. Aapka token abhi {token} hai (provisional); final token kal subah bhej diya jayega.",
    },
    "reminder_morning": {
        "en": "{greet} good morning. Your appointment is today at {time}. Your token is {token}. {ahead}",
        "hi": "{greet} सुप्रभात। आज {time} पर आपकी अपॉइंटमेंट है। आपका टोकन {token} है। {ahead}",
        "hinglish": "{greet} good morning. Aaj {time} par aapki appointment hai. Aapka token {token} hai. {ahead}",
    },
    "queue_two_ahead": {
        "en": "{greet} only 2 patients are ahead of you now (your token {token}). Please be at the clinic.",
        "hi": "{greet} अब आपसे पहले सिर्फ़ 2 मरीज़ हैं (आपका टोकन {token})। कृपया क्लिनिक पहुँचें।",
        "hinglish": "{greet} ab aapse pehle sirf 2 patient hain (aapka token {token}). Kripya clinic pahunchein.",
    },
    "your_turn": {
        "en": "{greet} it is your turn now (token {token}). Please come in to see the doctor.",
        "hi": "{greet} अब आपकी बारी है (टोकन {token})। कृपया डॉक्टर के पास अंदर आइए।",
        "hinglish": "{greet} ab aapki baari hai (token {token}). Kripya doctor ke paas andar aaiye.",
    },
    "status_reply_today": {
        "en": "{greet} your token today is {token} (slot {time}). {ahead}",
        "hi": "{greet} आज आपका टोकन {token} है (समय {time})। {ahead}",
        "hinglish": "{greet} aaj aapka token {token} hai (slot {time}). {ahead}",
    },
    "status_reply_in_consultation": {
        "en": "{greet} the doctor is seeing you now (token {token}).",
        "hi": "{greet} डॉक्टर अभी आपको देख रहे हैं (टोकन {token})।",
        "hinglish": "{greet} doctor abhi aapko dekh rahe hain (token {token}).",
    },
    "status_reply_future": {
        "en": "{greet} your next appointment is on {date} at {time}. Your token is currently {token} (provisional) - your final token is confirmed on the day.",
        "hi": "{greet} आपकी अगली अपॉइंटमेंट {date} को {time} पर है। आपका टोकन अभी {token} है (अस्थायी) - अंतिम टोकन उसी दिन कन्फर्म होगा।",
        "hinglish": "{greet} aapki agli appointment {date} ko {time} par hai. Aapka token abhi {token} hai (provisional) - final token usi din confirm hoga.",
    },
    # Deliberately says nothing about an appointment: registering a patient
    # does not book a visit.
    "registered": {
        "en": "You're registered with us{name_comma}. We'll contact you shortly to schedule your visit.",
        "hi": "आप हमारे यहाँ रजिस्टर हो गए हैं{name_comma}। हम जल्द ही आपकी विज़िट तय करने के लिए संपर्क करेंगे।",
        "hinglish": "Aap hamare yahan register ho gaye hain{name_comma}. Hum jald hi aapki visit schedule karne ke liye contact karenge.",
    },
    # Staff rejected a request the patient made through the WhatsApp
    # conversation agent: tell them, so they aren't left waiting.
    "request_declined": {
        "en": "We couldn't confirm that request. Please send another preferred time.",
        "hi": "हम इस अनुरोध की पुष्टि नहीं कर सके। कृपया कोई दूसरा पसंदीदा समय भेजें।",
        "hinglish": "Hum is request ko confirm nahi kar sake. Kripya koi doosra pasandeeda time bhejein.",
    },
}


def render(template_key, language, name=None, token=None, old_token=None,
           date=None, time=None, ahead=None, branch_code=None):
    """Fill a fixed template. `language` is en / hi / hinglish / bilingual
    (bilingual = the English text followed by the Hinglish text). `branch_code`
    puts the branch on the token ("B-T04") when there is more than one branch."""
    if language not in LANGUAGES:
        return "{}\n\n{}".format(
            render(template_key, "en", name, token, old_token, date, time, ahead, branch_code),
            render(template_key, "hinglish", name, token, old_token, date, time, ahead, branch_code),
        )
    template = TEMPLATES[template_key][language]
    values = {
        "greet": _greet(language, name),
        "name_comma": ", {}".format(name) if name else "",
        "token": token_queue.format_token(token, branch_code) if token is not None else "",
        "old_token": token_queue.format_token(old_token, branch_code) if old_token is not None else "",
        "date": format_date(date, language) if date else "",
        "time": format_time(time, language) if time else "",
        "ahead": ahead_line(language, ahead),
    }
    return template.format(**values).strip()


# --- Meta (WhatsApp Business) message templates, for later submission -------
# Outside the 24-hour window only a Meta-approved template may be sent, and
# none are approved yet. This is the same text as TEMPLATES with each
# {placeholder} turned into Meta's positional {{1}}..{{n}} (numbered by first
# appearance). NOTE for submission: Meta rejects a body that starts or ends
# with a variable and wants variables separated by text, so the leading
# {greet} and trailing {ahead} will need a fixed word added around them at
# submission time. Nothing in this module sends these.

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_META_LANGUAGE_CODE = {"en": "en", "hi": "hi", "hinglish": "en"}  # Roman-script Hinglish has no Meta code of its own


def _build_meta_templates():
    built = []
    for key, by_language in TEMPLATES.items():
        for language, body in by_language.items():
            order = []
            for match in _PLACEHOLDER.finditer(body):
                if match.group(1) not in order:
                    order.append(match.group(1))
            positional = _PLACEHOLDER.sub(lambda m: "{{{{{}}}}}".format(order.index(m.group(1)) + 1), body)
            built.append({
                "name": "clinic_{}_{}".format(key, language),
                "language": _META_LANGUAGE_CODE[language],
                "variables": order,
                "body": positional,
            })
    return built


META_TEMPLATES = _build_meta_templates()


# ---------------------------------------------------------------------------
# Recipient / language lookup
# ---------------------------------------------------------------------------

def _inbound_rows_for(conn, wa_id):
    """Inbound messages from `wa_id`, matching on the last 10 digits so
    '919876543210' and '9876543210' are the same sender (same heuristic as
    entity_resolution.last10_digits)."""
    last10 = last10_digits(wa_id or "")
    if len(last10) < 10:
        return "wa_id = ?", (wa_id,)
    return "(wa_id = ? OR wa_id LIKE ?)", (wa_id, "%" + last10)


def patient_language(conn, wa_id):
    """Language of the patient's most recent inbound message that has text
    (en / hi / hinglish), or 'bilingual' when there is none."""
    if not wa_id:
        return "bilingual"
    where, params = _inbound_rows_for(conn, wa_id)
    row = conn.execute(
        "SELECT raw_text FROM wa_messages WHERE {} AND raw_text IS NOT NULL AND raw_text != '' "
        "ORDER BY received_at DESC, id DESC LIMIT 1".format(where),
        params,
    ).fetchone()
    if row is None:
        return "bilingual"
    return whatsapp.detect_message_language(row["raw_text"])


def in_window(conn, wa_id, now_utc):
    """True iff `wa_id` sent us a message within the last WINDOW_HOURS."""
    where, params = _inbound_rows_for(conn, wa_id)
    row = conn.execute("SELECT MAX(received_at) AS last FROM wa_messages WHERE {}".format(where), params).fetchone()
    if row is None or row["last"] is None:
        return False
    return now_utc - _parse_ts(row["last"]) <= timedelta(hours=WINDOW_HOURS)


# Events that tell the patient WHEN their visit is: with several branches they
# also say WHERE (the branch name, address and map link).
_WHERE_EVENTS = frozenset((
    "booking_confirmed", "appointment_rescheduled", "appointment_reinstated", "status_reply",
    "reminder_day_before", "reminder_morning",
))


def branch_line(conn, branch_id, doctor_id=None):
    """'📍 Branch B, Sector 56 ...' (the map link on the next line, then the
    doctor's name) when there is more than one branch, else ''. Plain text, the
    same in every language."""
    if not branches.multi_branch(conn):
        return ""
    branch = branches.get_branch(conn, branches.resolve(conn, branch_id))
    if not branch:
        return ""
    line = "\U0001F4CD " + ", ".join(part for part in (branch["name"], (branch.get("address") or "").strip()) if part)
    if branch.get("maps_url"):
        line += "\n" + branch["maps_url"].strip()
    doctor = branches.doctor_label(conn, doctor_id) if doctor_id else None
    if doctor:
        line += "\n\U0001FA7A " + doctor
    return line


def _all_branch_ids(conn):
    """Every branch to sweep for reminders and token changes (retired ones too:
    they may still hold an appointment)."""
    ids = [b["id"] for b in branches.list_branches(conn, include_inactive=True)]
    return ids or [None]


def _appointment_context(conn, appointment_id):
    row = conn.execute(
        """
        SELECT a.id, a.appt_date, a.start_time, a.status, a.queue_state, a.last_notified_token, a.branch_id,
               a.doctor_id, COALESCE(p.name, a.patient_name) AS name,
               COALESCE(a.patient_phone, p.phone) AS phone
        FROM appointments a LEFT JOIN patients p ON p.id = a.patient_id
        WHERE a.id = ?
        """,
        (appointment_id,),
    ).fetchone()
    if row is None:
        return None
    ctx = dict(row)
    ctx["wa_id"] = whatsapp.phone_to_wa_id(row["phone"])
    return ctx


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------

def enqueue(conn, *, event, dedup_key, body, wa_id, appointment_id=None, language=None, now=None,
            interactive=None):
    """Insert one outbox row, idempotently. Returns the new row id, or None
    if a row with this dedup_key already exists (nothing is changed then).
    A missing wa_id is recorded as `skipped_no_phone` rather than dropped, so
    staff can see the patient could not be reached. `interactive` is the
    JSON-able button/list spec (see whatsapp.build_interactive_body)."""
    now = now or Now.real()
    status = "pending" if wa_id else "skipped_no_phone"
    cur = conn.execute(
        "INSERT OR IGNORE INTO notifications "
        "(appointment_id, wa_id, event, dedup_key, language, body, status, created_at, interactive_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (appointment_id, wa_id or None, event, dedup_key, language, body, status, _ts(now.utc),
         json.dumps(interactive, ensure_ascii=False) if interactive else None),
    )
    conn.commit()
    return cur.lastrowid if cur.rowcount else None


def _remember_token(conn, appointment_id, token, is_today):
    """Record what the patient was last told. Only a same-day token is a
    *final* one worth notifying about when it changes; a provisional
    (future-date) token clears the marker, so no token_changed fires until
    the morning-of message has stated the real number."""
    conn.execute(
        "UPDATE appointments SET last_notified_token = ? WHERE id = ?",
        (token if is_today else None, appointment_id),
    )
    conn.commit()


def notify_appointment(conn, event, appointment_id, discriminator, now=None, *,
                       wa_id_override=None, old_token=None, record_skip=True, language=None):
    """Compose and enqueue `event` for the patient of `appointment_id`.
    Returns the notification row id, or None (duplicate, no such
    appointment, no token for a token event, or no phone with
    record_skip=False)."""
    now = now or Now.real()
    ctx = _appointment_context(conn, appointment_id)
    if ctx is None:
        return None

    wa_id = wa_id_override or ctx["wa_id"]
    if wa_id_override and last10_digits(wa_id_override) != last10_digits(ctx["phone"] or ""):
        # status_reply must only ever go to the number the appointment is
        # actually booked under.
        return None
    if not wa_id and not record_skip:
        return None

    entry = token_queue.queue_entry(conn, appointment_id) if event in _TOKEN_EVENTS else None
    if event in _TOKEN_EVENTS and entry is None:
        return None  # cancelled/rescheduled: there is no token to tell anyone

    is_today = ctx["appt_date"] == now.today.isoformat()
    key = event
    if event in ("booking_confirmed", "appointment_rescheduled", "status_reply", "appointment_reinstated"):
        key = "{}_{}".format(event, "today" if is_today else "future")
        if event == "status_reply" and is_today and ctx["queue_state"] == "in_consultation":
            key = "status_reply_in_consultation"

    language = language or patient_language(conn, wa_id)
    code = token_queue._label_code(conn, branches.resolve(conn, ctx["branch_id"]))
    body = render(
        key, language, name=ctx["name"],
        token=entry["token"] if entry else None, old_token=old_token,
        date=ctx["appt_date"], time=ctx["start_time"],
        ahead=entry["ahead"] if entry and is_today else None,
        branch_code=code,
    )
    where = branch_line(conn, ctx["branch_id"], ctx["doctor_id"]) if event in _WHERE_EVENTS else ""
    if where:
        body = "{}\n{}".format(body, where)
    row_id = enqueue(
        conn, event=event, dedup_key="{}:{}:{}".format(event, appointment_id, discriminator),
        body=body, wa_id=wa_id, appointment_id=appointment_id, language=language, now=now,
    )
    if row_id and event in _TOKEN_EVENTS and wa_id:
        _remember_token(conn, appointment_id, entry["token"], is_today)
    return row_id


def notify_registered(conn, wa_id, name, patient_id, now=None):
    """Sent to the WhatsApp number that asked to register, after staff
    approved the registration. Never mentions an appointment."""
    now = now or Now.real()
    language = patient_language(conn, wa_id)
    return enqueue(
        conn, event="registered", dedup_key="registered:{}".format(patient_id),
        body=render("registered", language, name=name), wa_id=wa_id, language=language, now=now,
    )


def notify_status_reply(conn, wa_id, appointment_id, wa_message_id, now=None, language=None):
    """Automatic answer to a patient asking where they are in the queue.
    One reply per inbound message; only to the asker's own number."""
    return notify_appointment(
        conn, "status_reply", appointment_id, discriminator="msg{}".format(wa_message_id),
        now=now, wa_id_override=wa_id, language=language,
    )


def notify_request_declined(conn, wa_id, wa_message_id, language=None, now=None):
    """Tell a patient that staff could not confirm a request they made through
    the WhatsApp conversation. Fixed template; one per inbox item."""
    now = now or Now.real()
    language = language or patient_language(conn, wa_id)
    return enqueue(
        conn, event="request_declined", dedup_key="request_declined:{}".format(wa_message_id),
        body=render("request_declined", language), wa_id=wa_id, language=language, now=now,
    )


def enqueue_conv_reply(conn, wa_id, body, msg_key, seq, interactive=None, language=None, now=None):
    """One reply of the conversation agent. Deduplicated per inbound message
    (`msg_key` = the wa_messages id) and position in its reply list, so a
    retried webhook can never send the same reply twice. Goes through the
    same outbox as every other notification: logged, 24h-window-checked, and
    visible in the patient's thread."""
    return enqueue(
        conn, event="conv_reply", dedup_key="conv_reply:{}:{}".format(msg_key, seq), body=body,
        wa_id=wa_id, language=language, now=now, interactive=interactive,
    )


def enqueue_staff_message(conn, wa_id, text, now=None):
    """A message a staff member typed in the dashboard, sent through the
    outbox (so the 24h window is enforced and the result is visible). Never
    deduplicated: two different messages with the same words are both sent."""
    return enqueue(
        conn, event="staff_message", dedup_key="staff_message:{}:{}".format(wa_id, time.time_ns()),
        body=text, wa_id=wa_id, language=None, now=now,
    )


# ---------------------------------------------------------------------------
# Fan-out after anything that can change today's queue
# ---------------------------------------------------------------------------

def _queue_has_started(entries):
    return any(
        e["status"] in ("completed", "no_show") or e["queue_state"] == "in_consultation"
        for e in entries
    )


def fanout_queue_changes(conn, now=None, include_two_ahead=True):
    """For TODAY's outstanding appointments only (future dates would be
    spam): send token_changed to anyone whose token differs from what they
    were last told, and -- once the day's queue has actually started moving
    (someone has been seen, skipped, or is with the doctor) -- queue_two_ahead
    to anyone for whom "N ahead" is now exactly 2. New bookings pass
    include_two_ahead=False: a booking can only push people back, and "you're
    nearly up" at the moment of booking would be wrong. Idempotent --
    last_notified_token and the dedup_key stop repeats."""
    now = now or Now.real()
    today = now.today.isoformat()
    for branch_id in _all_branch_ids(conn):          # every branch has its own queue
        entries = token_queue.day_queue(conn, today, branch_id)
        started = _queue_has_started(entries)
        for entry in entries:
            if not entry["outstanding"] or entry["queue_state"] == "in_consultation":
                continue
            told = entry["last_notified_token"]
            if told is not None and told != entry["token"]:
                seq = conn.execute(
                    "SELECT COUNT(*) AS n FROM notifications WHERE appointment_id = ? AND event = 'token_changed'",
                    (entry["id"],),
                ).fetchone()["n"]
                notify_appointment(
                    conn, "token_changed", entry["id"], "{}>{}#{}".format(told, entry["token"], seq),
                    now, old_token=told, record_skip=False,
                )
            if include_two_ahead and started and entry["ahead"] == 2:
                notify_appointment(conn, "queue_two_ahead", entry["id"], today, now, record_skip=False)


# ---------------------------------------------------------------------------
# Post-commit hook
# ---------------------------------------------------------------------------

QUEUE_INTENTS = frozenset(("queue_check_in", "queue_call_next", "queue_mark_done", "queue_mark_no_show"))
NOTIFYING_INTENTS = frozenset((
    "book_appointment", "cancel_appointment", "reschedule_appointment", "restore_appointment", "register_patient",
)) | QUEUE_INTENTS


def after_write(conn, intent, slots, entity_id, now=None, wa_id=None, language=None):
    """Enqueue (but do not send) whatever a just-committed write should tell
    patients. `entity_id` is core.confirm's result id. `wa_id` is the sender
    of the originating WhatsApp message, if any (used for 'registered').
    `language`, when known (the WhatsApp conversation's language), is used for
    the message to the patient whose request this was; otherwise it is
    inferred from their last message."""
    now = now or Now.real()

    if slots.get("quiet"):
        # A closure moved / cancelled this appointment and sends its own notice
        # (clinic/closure_notify.py): no "moved" / "cancelled" message here, but
        # the queue tokens of everyone else still get refreshed.
        fanout_queue_changes(conn, now, include_two_ahead=False)
        return

    if intent == "book_appointment":
        notify_appointment(conn, "booking_confirmed", entity_id, slots.get("appt_date"), now, language=language)
        fanout_queue_changes(conn, now, include_two_ahead=False)

    elif intent == "cancel_appointment":
        appointment_id = slots.get("appointment_id")
        status = conn.execute("SELECT status, appt_date FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
        if status is not None and status["status"] == "cancelled":
            # "by_clinic": staff undid an automatic booking -- a different
            # fixed message that asks for a new time. The discriminator gets
            # a counter only from the second cancellation of the same
            # appointment on (an appointment can be reinstated and cancelled
            # again), so the first key is unchanged.
            event = "appointment_cancelled_by_clinic" if slots.get("by_clinic") else "appointment_cancelled"
            seq = conn.execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE appointment_id = ? AND event = ?",
                (appointment_id, event),
            ).fetchone()["n"]
            discriminator = status["appt_date"] if not seq else "{}#{}".format(status["appt_date"], seq)
            notify_appointment(conn, event, appointment_id, discriminator, now, language=language)
        fanout_queue_changes(conn, now)

    elif intent == "reschedule_appointment":
        appointment_id = slots.get("appointment_id")
        seq = conn.execute(
            "SELECT COUNT(*) AS n FROM notifications WHERE appointment_id = ? AND event = 'appointment_rescheduled'",
            (appointment_id,),
        ).fetchone()["n"]
        notify_appointment(
            conn, "appointment_rescheduled", appointment_id,
            "{}@{}#{}".format(slots.get("appt_date"), slots.get("start_time"), seq), now, language=language,
        )
        fanout_queue_changes(conn, now, include_two_ahead=False)

    elif intent == "restore_appointment":
        appointment_id = slots.get("appointment_id")
        seq = conn.execute(
            "SELECT COUNT(*) AS n FROM notifications WHERE appointment_id = ? AND event = 'appointment_reinstated'",
            (appointment_id,),
        ).fetchone()["n"]
        notify_appointment(conn, "appointment_reinstated", appointment_id, "#{}".format(seq), now, language=language)
        fanout_queue_changes(conn, now, include_two_ahead=False)

    elif intent == "register_patient":
        if wa_id:
            notify_registered(conn, wa_id, slots.get("name"), entity_id, now)

    elif intent in QUEUE_INTENTS:
        appointment_id = slots.get("appointment_id")
        if intent == "queue_call_next":
            notify_appointment(conn, "your_turn", appointment_id, "called", now)
        fanout_queue_changes(conn, now)


def post_commit(conn, intent, slots, entity_id, *, sender=None, wa_id=None, now=None, language=None):
    """The ONE call the web layer makes after a write has committed: enqueue
    the right notifications and try to deliver them. It can never raise --
    any failure is logged and swallowed, because a notification problem must
    not fail or mask a write that already succeeded."""
    try:
        if intent not in NOTIFYING_INTENTS:
            return
        now = now or Now.real()
        after_write(conn, intent, slots, entity_id, now=now, wa_id=wa_id, language=language)
        actual_sender, dry_run = resolve_sender(sender)
        flush(conn, actual_sender, now=now, dry_run=dry_run)
    except Exception:
        _logger.exception("post-commit notification work failed for intent=%s (the write itself succeeded)", intent)
        try:
            conn.rollback()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

_FLUSH_LOCK = threading.Lock()


def notify_mode():
    # A scratch-database copy of the app never messages real patients, whatever
    # WHATSAPP_NOTIFY_MODE says (it would use the real WhatsApp credentials).
    scratch = (os.environ.get("CLINIC_DB_PATH") or "").strip()
    if scratch and os.path.basename(scratch) != "clinic.db" and os.environ.get("WHATSAPP_ALLOW_SCRATCH_DB") != "1":
        return "dry_run"
    mode = os.environ.get("WHATSAPP_NOTIFY_MODE", "").strip().lower()
    if mode:
        return mode
    # Unset: send for real only when WhatsApp credentials exist. A fresh copy
    # of the app with no credentials just records what it would have sent.
    return "live" if os.environ.get("WHATSAPP_ACCESS_TOKEN") else "dry_run"


def live_sender(wa_id, text, interactive=None):
    # Looked up at call time (not imported by name) so a test can patch it.
    if interactive:
        return whatsapp.send_interactive(wa_id, text, interactive)
    return whatsapp.send_message(wa_id, text)


def interactive_fallback_text(body, interactive):
    """Plain-text rendering of a button/list message, for a sender that can't
    send interactive messages: the body followed by the option titles."""
    options = (interactive or {}).get("buttons") or (interactive or {}).get("rows") or []
    if not options:
        return body
    return "{}\n\n{}".format(body, "\n".join("- {}".format(o["title"]) for o in options))


def _sender_takes_interactive(sender):
    try:
        params = inspect.signature(sender).parameters
    except (TypeError, ValueError):
        return False
    return "interactive" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def resolve_sender(override=None):
    """(sender, dry_run). An explicit `override` (tests) always wins.
    WHATSAPP_NOTIFY_MODE: 'live' (default) sends through the WhatsApp Cloud
    API; 'dry_run' sends nothing. An unrecognised value is treated as
    dry_run -- the failure mode that cannot message a patient."""
    if override is not None:
        return override, False
    mode = notify_mode()
    if mode == "live":
        return live_sender, False
    if mode != "dry_run":
        _logger.warning("Unknown WHATSAPP_NOTIFY_MODE=%r; treating as dry_run", mode)
    return None, True


def _set_status(conn, notification_id, status, error=None, attempts_delta=0, sent_at=None):
    conn.execute(
        "UPDATE notifications SET status = ?, error = ?, attempts = attempts + ?, sent_at = ? WHERE id = ?",
        (status, error, attempts_delta, sent_at, notification_id),
    )
    conn.commit()


def flush(conn, sender, now=None, dry_run=False, lock_timeout=0):
    """Deliver pending notifications (and recently failed ones still under
    RETRY_MAX_ATTEMPTS). `sender(wa_id, text)` must raise on failure (a
    sender that accepts an `interactive=` keyword also gets button/list
    specs; others get the options as plain text). Returns {status: count}
    for the rows touched. Never raises for a per-notification problem.

    `lock_timeout` is how long to wait if another delivery is in progress:
    0 (the default, and what the web layer's request handlers use) never
    waits; the conversation agent's background thread waits a few seconds so
    a reply doesn't sit until the next scheduler tick."""
    if sender is None and not dry_run:
        raise ValueError("flush() needs a sender unless dry_run=True")
    now = now or Now.real()
    cutoff = _ts(now.utc - timedelta(minutes=RETRY_MAX_AGE_MINUTES))
    conv_cutoff = _ts(now.utc - timedelta(minutes=CONV_RETRY_MAX_AGE_MINUTES))
    counts = {}
    # Non-blocking by default: if the scheduler (or another request) is
    # already delivering, do not queue up behind a possibly slow network
    # call -- whatever is still pending is picked up by that flush or the
    # next tick. The lock is what stops two threads sending the same row twice.
    acquired = _FLUSH_LOCK.acquire(timeout=lock_timeout) if lock_timeout else _FLUSH_LOCK.acquire(blocking=False)
    if not acquired:
        return counts
    try:
        rows = conn.execute(
            "SELECT * FROM notifications WHERE status = 'pending' "
            "OR (status = 'failed' AND attempts < ? AND created_at >= ? "
            "    AND (event != 'conv_reply' OR created_at >= ?)) ORDER BY id",
            (RETRY_MAX_ATTEMPTS, cutoff, conv_cutoff),
        ).fetchall()
        for row in rows:
            outcome = _deliver(conn, row, sender, now, dry_run)
            counts[outcome] = counts.get(outcome, 0) + 1
    finally:
        _FLUSH_LOCK.release()
    return counts


def _deliver(conn, row, sender, now, dry_run):
    nid = row["id"]
    try:
        if not row["wa_id"]:
            _set_status(conn, nid, "skipped_no_phone")
            return "skipped_no_phone"
        if not in_window(conn, row["wa_id"], now.utc):
            _set_status(
                conn, nid, "blocked_no_window",
                error="No message from this number in the last {}h; WhatsApp needs an approved template.".format(WINDOW_HOURS),
            )
            return "blocked_no_window"
        if dry_run:
            _set_status(conn, nid, "dry_run")
            return "dry_run"
        try:
            _send_row(sender, row)
        except Exception as exc:
            _logger.warning("notification %s failed: %s", nid, exc)
            _set_status(conn, nid, "failed", error=str(exc)[:500], attempts_delta=1)
            return "failed"
        _set_status(conn, nid, "sent", attempts_delta=1, sent_at=_ts(now.utc))
        return "sent"
    except Exception:
        _logger.exception("could not record the outcome of notification %s", nid)
        try:
            conn.rollback()
        except Exception:
            pass
        return "error"


def _send_row(sender, row):
    interactive = None
    if "interactive_json" in row.keys() and row["interactive_json"]:
        interactive = json.loads(row["interactive_json"])
    if interactive is None:
        return sender(row["wa_id"], row["body"])
    if _sender_takes_interactive(sender):
        return sender(row["wa_id"], row["body"], interactive=interactive)
    return sender(row["wa_id"], interactive_fallback_text(row["body"], interactive))


def retry(conn, notification_id):
    """Put a blocked/failed notification back to pending (a human clicked
    Retry). Returns True if it was eligible. flush() still re-checks the
    24-hour window, so a retry outside the window just blocks again."""
    cur = conn.execute(
        "UPDATE notifications SET status = 'pending', error = NULL, attempts = 0 "
        "WHERE id = ? AND status IN ('blocked_no_window', 'failed')",
        (notification_id,),
    )
    conn.commit()
    return bool(cur.rowcount)


def recent_notifications(conn, limit=20):
    """Newest first, with the patient's name where the notification belongs
    to an appointment -- for the Queue tab's "Recent notifications" list."""
    return [
        dict(r) for r in conn.execute(
            "SELECT n.id, n.appointment_id, n.wa_id, n.event, n.language, n.status, n.error, "
            "       n.attempts, n.created_at, n.sent_at, COALESCE(p.name, a.patient_name) AS patient_name "
            "FROM notifications n "
            "LEFT JOIN appointments a ON a.id = n.appointment_id "
            "LEFT JOIN patients p ON p.id = a.patient_id "
            # Conversation replies and staff chat messages live in the
            # Patient messages tab's threads; here they would bury the queue.
            "WHERE n.event NOT IN ('conv_reply', 'staff_message') "
            "ORDER BY n.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# Reminders (called by the scheduler)
# ---------------------------------------------------------------------------

def generate_reminders(conn, now=None):
    """Enqueue due reminders. Safe to call every tick: dedup_keys (event +
    appointment + date) mean a second call adds nothing.

    - reminder_day_before: from REMINDER_DAY_BEFORE_FROM_HOUR on the evening
      before. Carries a PROVISIONAL token. Skipped if the patient was sent a
      booking/reschedule message within RECENT_CONTACT_SKIP_HOURS (they were
      just told).
    - reminder_morning: from REMINDER_MORNING_FROM_HOUR on the day, for
      patients who haven't arrived yet and whose slot hasn't passed. Carries
      the FINAL token and position, and arms token_changed.
    Returns the number of notifications enqueued.
    """
    now = now or Now.real()
    created = 0

    if now.local.hour >= REMINDER_MORNING_FROM_HOUR:
        today = now.today.isoformat()
        hhmm = now.local.strftime("%H:%M")
        for branch_id in _all_branch_ids(conn):
            for entry in token_queue.day_queue(conn, today, branch_id):
                if entry["outstanding"] and entry["queue_state"] is None and entry["start_time"] >= hhmm:
                    if notify_appointment(conn, "reminder_morning", entry["id"], today, now, record_skip=False):
                        created += 1

    if now.local.hour >= REMINDER_DAY_BEFORE_FROM_HOUR:
        tomorrow = (now.today + timedelta(days=1)).isoformat()
        recent_cutoff = _ts(now.utc - timedelta(hours=RECENT_CONTACT_SKIP_HOURS))
        tomorrow_entries = [e for branch_id in _all_branch_ids(conn) for e in token_queue.day_queue(conn, tomorrow, branch_id)]
        for entry in tomorrow_entries:
            if not entry["outstanding"]:
                continue
            just_told = conn.execute(
                "SELECT 1 FROM notifications WHERE appointment_id = ? "
                "AND event IN ('booking_confirmed', 'appointment_rescheduled') AND created_at >= ?",
                (entry["id"], recent_cutoff),
            ).fetchone()
            if just_told:
                continue
            if notify_appointment(conn, "reminder_day_before", entry["id"], tomorrow, now, record_skip=False):
                created += 1

    return created
