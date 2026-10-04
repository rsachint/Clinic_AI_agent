# Template-based, deliberately not LLM-phrased: nlu/extract.py already treats
# "a number must never come from the LLM" as a hard rule on the way IN; an
# LLM asked to "phrase this cash total naturally" on the way OUT is the same
# risk in the other direction -- a rupee figure the clinic owner is about to
# trust could get silently reworded wrong. Keep this dumb and exact instead.

_MAX_NAMES_SPOKEN = 3


def _missed_followups_text(data, lang):
    if not data:
        return "Koi follow-up baaki nahi hai." if lang == "hi-IN" else "No follow-ups are pending."
    count = len(data)
    if count <= _MAX_NAMES_SPOKEN:
        names = ", ".join(row["name"] for row in data)
        if lang == "hi-IN":
            return "{} follow-up baaki hain: {}.".format(count, names)
        return "{} follow-up(s) pending: {}.".format(count, names)
    if lang == "hi-IN":
        return "{} follow-up baaki hain.".format(count)
    return "{} follow-ups are pending.".format(count)


def _day_end_cashbook_text(data, lang):
    fees = data["fees_paise"] // 100
    expenses = data["expenses_paise"] // 100
    net = data["net_paise"] // 100
    if lang == "hi-IN":
        return "Aaj ki fees {} rupaye, kharch {} rupaye, net {} rupaye.".format(fees, expenses, net)
    return "Today's fees are {} rupees, expenses {} rupees, net {} rupees.".format(fees, expenses, net)


def _patient_lookup_text(data, lang):
    if not data:
        return "Patient nahi mila." if lang == "hi-IN" else "Patient not found."
    if lang == "hi-IN":
        return "{} ka phone number {} hai.".format(data["name"], data["phone"])
    return "{}'s phone number is {}.".format(data["name"], data["phone"])


def _check_availability_text(data, lang):
    date_str = data["date"]
    slots = data["slots"]
    if not slots:
        return ("{} ko koi slot khaali nahi hai.".format(date_str) if lang == "hi-IN"
                else "No slots are free on {}.".format(date_str))
    count = len(slots)
    if count <= _MAX_NAMES_SPOKEN:
        times = ", ".join(slots)
        if lang == "hi-IN":
            return "{} ko {} slot khaali hain: {}.".format(date_str, count, times)
        return "{} free slot(s) on {}: {}.".format(count, date_str, times)
    if lang == "hi-IN":
        return "{} ko {} slot khaali hain.".format(date_str, count)
    return "{} free slots on {}.".format(count, date_str)


def _list_appointments_text(data, lang, scope=None, name=None):
    """`scope` names the day or range that was listed (e.g. "Wed 7 Oct"), so a
    reader can never mistake today's list for the day they asked about. `name`
    is set when one person's appointments were asked for: nothing found then
    says so, instead of quietly showing someone else's or another day's."""
    if name:
        if not data:
            if lang == "hi-IN":
                return ("{} ko {} ke koi appointments nahi mile.".format(scope, name) if scope
                        else "{} ke koi appointments nahi mile.".format(name))
            return ("No appointments found for {} on {}.".format(name, scope) if scope
                    else "No appointments found for {}.".format(name))
        count = len(data)
        if lang == "hi-IN":
            return ("{} ko {} ke {} appointment hain.".format(scope, name, count) if scope
                    else "{} ke {} appointment hain.".format(name, count))
        return ("{} appointment(s) for {} on {}.".format(count, name, scope) if scope
                else "{} appointment(s) for {}.".format(count, name))
    if not data:
        if lang == "hi-IN":
            return "{} ko koi appointment nahi hai.".format(scope) if scope else "Koi appointment schedule nahi hai."
        return "No appointments on {}.".format(scope) if scope else "No appointments are scheduled."
    count = len(data)
    if lang == "hi-IN":
        return "{} ko {} appointment hain.".format(scope, count) if scope else "{} appointment hain.".format(count)
    return "{} appointment(s) on {}.".format(count, scope) if scope else "{} appointment(s) scheduled.".format(count)


def _next_appointment_text(data, lang):
    if not data:
        return "Koi upcoming appointment nahi hai." if lang == "hi-IN" else "No upcoming appointment found."
    if lang == "hi-IN":
        return "{} ki agli appointment {} ko {} baje hai.".format(
            data["patient_label"], data["appt_date"], data["start_time"]
        )
    return "{}'s next appointment is on {} at {}.".format(
        data["patient_label"], data["appt_date"], data["start_time"]
    )


def _queue_status_text(data, lang):
    if not data["total_today"]:
        return "Aaj koi appointment nahi hai." if lang == "hi-IN" else "No appointments today."
    serving, nxt, waiting = data["now_serving"], data["next_up"], data["waiting"]
    if lang == "hi-IN":
        parts = ["Abhi {} doctor ke paas hain.".format(serving) if serving else "Abhi doctor ke paas koi nahi hai."]
        if nxt:
            parts.append("Agla: {}.".format(nxt))
        parts.append("{} intezaar mein hain.".format(waiting))
        return " ".join(parts)
    parts = ["Now with the doctor: {}.".format(serving) if serving else "Nobody is with the doctor right now."]
    if nxt:
        parts.append("Next: {}.".format(nxt))
    parts.append("{} waiting.".format(waiting))
    return " ".join(parts)


_COMPOSERS = {
    "queue_status": _queue_status_text,
    "missed_followups": _missed_followups_text,
    "day_end_cashbook": _day_end_cashbook_text,
    "patient_lookup": _patient_lookup_text,
    "check_availability": _check_availability_text,
    "list_appointments": _list_appointments_text,
    "next_appointment": _next_appointment_text,
}


_MODE_LABELS = {"week": "Week", "month": "Month", "agenda": "Agenda"}


def compose_navigation(intent, mode, language_code):
    """Fixed acknowledgement for a navigation command (open_calendar). Pure
    navigation has no data to cite, so no Source tail."""
    view = " ({} view)".format(_MODE_LABELS[mode]) if mode in _MODE_LABELS else ""
    if language_code == "hi-IN":
        return "Appointments calendar khol raha hoon{}.".format(view)
    return "Opening the Appointments calendar{}.".format(view)


def compose_answer(intent, data, citation, language_code, scope=None, name=None):
    # Binary for now: Hindi or English body text. Bulbul itself only speaks
    # 11 languages (clinic/tts.py); this keeps the two in sync rather than
    # trying to support every Saaras-detected code here.
    lang = "hi-IN" if language_code == "hi-IN" else "en-IN"
    if intent == "list_appointments":
        body = _list_appointments_text(data, lang, scope, name)
    else:
        body = _COMPOSERS[intent](data, lang)
    tail = " Source: {}, {}.".format(citation.source, citation.as_of)
    return body + tail
