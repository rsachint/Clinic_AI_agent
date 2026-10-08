"""The two WhatsApp reminders of a follow-up visit, as FIXED templates.

Same rule as clinic/notify.py: only deterministic code fills the blanks (patient
name, doctor, branch, date, time -- all read from the database), no model writes
any of it. And one hard rule of its own: **a reminder never carries a diagnosis,
treatment or any other clinical text.** compose() is the only place a reminder
is built and it does not read the follow-up's `diagnosis` column at all.

Each reminder exists in three languages (English, Devanagari Hindi, Roman-script
Hinglish; a patient with no language on record gets English). The same text is
used two ways:

  * inside WhatsApp's 24-hour window: a normal message with three reply buttons
    (Reschedule / Already visited / Cancel);
  * outside it: the Meta-approved TEMPLATE of the same text, body variables
    {{1}}..{{5}} = patient name, doctor, branch, date, time, and the same three
    buttons as quick replies. Nothing is sent as a template until staff mark it
    approved (Settings); see docs/meta_templates/ for the texts to submit.

The button ids are read by clinic/conversation.py:
followup:reschedule:<id>, followup:visited:<id>, followup:cancel:<id>.

`python -m clinic.followup_notify` prints the Meta template definitions as JSON
(docs/meta_templates/followup_templates.json is generated from it).
"""

import json
import re

from clinic import branches, notify

KINDS = ("2d", "4h")
LANGUAGES = ("en", "hi", "hinglish")
EVENT = {"2d": "followup_reminder_2d", "4h": "followup_reminder_4h"}
ACTIONS = ("reschedule", "visited", "cancel")

# Body variables, in Meta's positional order.
VARIABLES = ("name", "doctor", "branch", "date", "time")

BODY = {
    "2d": {
        "en": ("Hello {name}, this is a reminder from the clinic about your follow-up visit with {doctor} at {branch} "
               "on {date} at {time}. If your plans have changed, please tap a button below."),
        "hi": ("नमस्ते {name}, क्लिनिक की ओर से याद दिलाया जा रहा है कि {doctor} के साथ {branch} में आपका फ़ॉलो-अप विज़िट "
               "{date} को {time} पर है। यदि आपकी योजना बदल गई है, तो कृपया नीचे दिए बटन दबाएँ।"),
        "hinglish": ("Namaste {name}, clinic ki taraf se yaad dilana: {doctor} ke saath {branch} mein aapka follow-up visit "
                     "{date} ko {time} par hai. Agar aapka plan badal gaya hai, to kripya neeche diye button dabayein."),
    },
    "4h": {
        "en": ("Hello {name}, a reminder that your follow-up visit with {doctor} at {branch} is today, {date}, at {time}. "
               "If you cannot make it, please tap a button below."),
        "hi": ("नमस्ते {name}, याद दिलाना: {doctor} के साथ {branch} में आपका फ़ॉलो-अप विज़िट आज, {date} को {time} पर है। "
               "यदि आप नहीं आ सकते, तो कृपया नीचे दिए बटन दबाएँ।"),
        "hinglish": ("Namaste {name}, yaad dilana: {doctor} ke saath {branch} mein aapka follow-up visit aaj, {date} ko {time} par hai. "
                     "Agar aap nahi aa sakte, to kripya neeche diye button dabayein."),
    },
}

FOOTER = {
    "en": "Reply STOP to stop these reminders",
    "hi": "ये रिमाइंडर रोकने के लिए STOP लिखें",
    "hinglish": "Reminders rokne ke liye STOP likhein",
}

# Reply-button / quick-reply titles. WhatsApp: <= 20 chars on a reply button
# (Meta: <= 25 on a template quick reply). Roman-script Hindi keeps the English
# words, which are what patients already know these buttons by.
BUTTONS = {
    "reschedule": {"en": "Reschedule", "hi": "समय बदलें", "hinglish": "Reschedule"},
    "visited": {"en": "Already visited", "hi": "विज़िट हो चुकी", "hinglish": "Already visited"},
    "cancel": {"en": "Cancel", "hi": "रद्द करें", "hinglish": "Cancel"},
}

# What the template language code is: Roman-script Hinglish has no Meta code of
# its own, so it is an English-language template written in Hinglish.
META_LANGUAGE_CODE = {"en": "en", "hi": "hi", "hinglish": "en"}

# Sample values Meta wants when a template with variables is submitted.
SAMPLES = {
    "en": {"name": "Sunita Devi", "doctor": "Dr. Mehta", "branch": "Branch A",
           "date": "Monday, 12 Oct 2026", "time": "10:30 AM"},
    "hi": {"name": "सुनीता देवी", "doctor": "डॉ. मेहता", "branch": "ब्रांच ए",
           "date": "12 अक्टूबर 2026, सोमवार", "time": "सुबह 10:30"},
    "hinglish": {"name": "Sunita Devi", "doctor": "Dr. Mehta", "branch": "Branch A",
                 "date": "Monday, 12 Oct 2026", "time": "10:30 AM"},
}

FALLBACK_DOCTOR = {"en": "your doctor", "hi": "आपके डॉक्टर", "hinglish": "aapke doctor"}

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def template_name(kind, language):
    return "followup_reminder_{}_{}".format(kind, language)


def all_template_names():
    return [template_name(k, lang) for k in KINDS for lang in LANGUAGES]


def choice_id(action, followup_id):
    return "followup:{}:{}".format(action, followup_id)


def positional_body(kind, language):
    """The body with each {name} turned into Meta's {{1}}..{{5}}."""
    order = {name: i + 1 for i, name in enumerate(VARIABLES)}
    return _PLACEHOLDER.sub(lambda m: "{{{{{}}}}}".format(order[m.group(1)]), BODY[kind][language])


def context(conn, followup_id):
    """What a reminder says, read from the database. Deliberately selects NO
    clinical column: the follow-up's diagnosis is not fetched here."""
    row = conn.execute(
        "SELECT f.id, f.due_date, f.due_time, f.appointment_id, f.branch_id, f.doctor_id, p.name AS name, p.phone AS phone "
        "FROM followups f JOIN patients p ON p.id = f.patient_id WHERE f.id = ?", (followup_id,)).fetchone()
    return dict(row) if row else None


def _values(conn, ctx, lang):
    return {
        "name": (ctx.get("name") or "").strip() or {"en": "there", "hi": "जी", "hinglish": "ji"}[lang],
        "doctor": branches.doctor_label(conn, ctx.get("doctor_id")) or FALLBACK_DOCTOR[lang],
        "branch": branches.branch_label(conn, ctx.get("branch_id")),
        "date": notify.format_date(ctx["due_date"], lang),
        "time": notify.format_time(ctx["due_time"], lang),
    }


def compose(conn, ctx, kind, language):
    """(text, interactive, template) for one reminder.

    text         the free-form message (sent inside the 24-hour window): the template
                 text filled in, the branch address / map link when there are several
                 branches, and the STOP footer. A patient whose language is not known
                 ('bilingual', like every other message) gets English then Hinglish;
    interactive  the three reply buttons;
    template     the spec of the approved template to send outside the window (the
                 English one for a 'bilingual' patient)."""
    bilingual = language not in LANGUAGES
    lang = "en" if bilingual else language
    values = _values(conn, ctx, lang)
    text = BODY[kind][lang].format(**values)
    if bilingual:
        text = "{}\n\n{}".format(text, BODY[kind]["hinglish"].format(**_values(conn, ctx, "hinglish")))
    where = notify.branch_line(conn, ctx.get("branch_id"), ctx.get("doctor_id"))
    if where:
        text = "{}\n{}".format(text, where)
    text = "{}\n\n{}".format(text, FOOTER[lang])
    interactive = {"type": "button", "buttons": [
        {"id": choice_id(action, ctx["id"]), "title": BUTTONS[action][lang]} for action in ACTIONS]}
    template = {
        "name": template_name(kind, lang), "language": META_LANGUAGE_CODE[lang],
        "params": [values[v] for v in VARIABLES],
        "buttons": [choice_id(action, ctx["id"]) for action in ACTIONS],
    }
    return text, interactive, template


# ---------------------------------------------------------------------------
# Meta template definitions (what to submit in WhatsApp Manager)
# ---------------------------------------------------------------------------

def meta_templates():
    """The six templates, in the shape docs/meta_templates/followup_templates.json uses."""
    out = []
    for kind in KINDS:
        for lang in LANGUAGES:
            samples = [SAMPLES[lang][v] for v in VARIABLES]
            out.append({
                "name": template_name(kind, lang),
                "category": "UTILITY",
                "language": META_LANGUAGE_CODE[lang],
                "written_in": {"en": "English", "hi": "Hindi (Devanagari script)", "hinglish": "Hindi in Roman script (Hinglish)"}[lang],
                "reminder": {"2d": "2 days before the follow-up date", "4h": "4 hours before the visit"}[kind],
                "components": {
                    "body": {
                        "text": positional_body(kind, lang),
                        "variables": ["{{%d}} = %s" % (i + 1, label) for i, label in enumerate(
                            ("patient name", "doctor name", "branch", "follow-up date", "follow-up time"))],
                        "example": samples,
                    },
                    "footer": {"text": FOOTER[lang]},
                    "buttons": [{"type": "QUICK_REPLY", "text": BUTTONS[a][lang], "payload_sent_with_each_message": "followup:%s:<follow-up id>" % a}
                                for a in ACTIONS],
                },
            })
    return out


def meta_rule_problems(definition):
    """Meta's template rules we can check offline; [] when the definition is fine."""
    problems = []
    body = definition["components"]["body"]["text"]
    marks = list(re.finditer(r"\{\{(\d+)\}\}", body))
    numbers = [int(m.group(1)) for m in marks]
    if numbers != sorted(numbers) or sorted(set(numbers)) != list(range(1, len(set(numbers)) + 1)):
        problems.append("variables must be numbered {{1}}, {{2}}, ... without gaps")
    if len(body) > 1024:
        problems.append("body is over 1024 characters")
    if body.startswith("{{") or body.endswith("}}"):
        problems.append("the body starts or ends with a variable")
    if re.search(r"\}\}\s*\{\{", body):
        problems.append("two variables are next to each other")
    if len(definition["components"]["footer"]["text"]) > 60:
        problems.append("footer is over 60 characters")
    if any(len(b["text"]) > 25 for b in definition["components"]["buttons"]):
        problems.append("a button label is over 25 characters")
    if len(definition["components"]["body"]["example"]) != len(numbers):
        problems.append("one sample value is needed per variable")
    text_only = re.sub(r"\{\{\d+\}\}", "", body)
    if len(text_only.strip()) < 20 * len(marks):     # a comfortable amount of fixed text around the variables
        problems.append("too little fixed text for the number of variables")
    return problems


if __name__ == "__main__":
    print(json.dumps({"templates": meta_templates()}, ensure_ascii=False, indent=2))
