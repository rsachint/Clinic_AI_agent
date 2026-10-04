import unicodedata
from datetime import date, timedelta

from clinic.nlu.classify_patient import classify
from clinic.nlu.datetime_extract import extract_appt_date, extract_appt_time
from clinic.nlu.llm_slots import extract_name


class UnrecognizedPatientMessage(Exception):
    pass


# Only "kal"/"aaj" -- single-token, zero ambiguity. Everything else ("agle
# hafte", a weekday name) is real relative-date NLU that's easy to get
# silently wrong; per this codebase's established pattern for gaps (a
# cut-off phone number, register_staff's unparsed role), leave it blank for
# a human to fill in on the review card rather than guess.
def _extract_relative_date(text):
    normalized = unicodedata.normalize("NFC", text).lower()
    if "kal" in normalized or "कल" in normalized:
        return (date.today() + timedelta(days=1)).isoformat()
    if "aaj" in normalized or "आज" in normalized:
        return date.today().isoformat()
    return None


def parse_patient_message(text, today=None):
    """Turn a patient's WhatsApp message into (intent, slots), or raise
    UnrecognizedPatientMessage -- the caller treats that as 'needs_human_reply',
    never as license to guess at an action. Unlike clinic/nlu/parser.py, this
    never resolves an entity id or fills in a phone number -- the caller
    (clinic/whatsapp.py) already knows the sender's phone and is responsible
    for resolving which of the patient's follow-ups an intent refers to."""
    intent = classify(text)
    if intent is None:
        raise UnrecognizedPatientMessage(text)

    if intent == "register_patient":
        return intent, {"name": extract_name(text)}
    if intent == "reschedule_followup":
        return intent, {"new_due_date": _extract_relative_date(text)}
    if intent in ("confirm_followup", "cancel_followup", "my_status"):
        return intent, {}
    if intent == "book_appointment":
        # Date/time by deterministic regex only (clinic/nlu/datetime_extract.py).
        # Either may be None: clinic/whatsapp_pipeline.py then pre-fills a
        # *suggestion* and flags it as such for staff.
        return intent, {"appt_date": extract_appt_date(text, today=today), "start_time": extract_appt_time(text)}

    raise UnrecognizedPatientMessage(text)
