import unicodedata

# Deliberately a separate module from clinic/nlu/classify.py, not merged in.
# That file's own comments document two real keyword-collision bugs already
# found this session, both within one speaker population (clinic staff)
# sharing one vocabulary register. Patient WhatsApp replies are a
# structurally different register -- short confirm/cancel/reschedule
# messages, plus clinical questions that must route to a human, never get
# auto-answered -- and are never invoked from the same code path as voice.
# Merging would reimport the fragility already patched, for no reuse benefit.
_RULES = [
    ("cancel_followup", [
        "cancel", "कैंसिल", "नहीं आ सकता", "नहीं आऊंगा", "nahi aa sakta",
        "nahi aaunga", "won't be able to come", "can't come",
    ]),
    ("reschedule_followup", [
        "reschedule", "रीशेड्यूल", "postpone", "date badlo", "दूसरे दिन",
        "किसी और दिन", "another day", "change the date",
    ]),
    # Status questions ("my token", "where am I", "how many people before
    # me"). Checked before confirm_followup so "theek hai, mera number kab
    # aayega" is a question, not a confirmation. Read-only: see
    # clinic/whatsapp_pipeline.py's my_status handling.
    ("my_status", [
        "my token", "mera token", "मेरा टोकन", "token number", "टोकन नंबर", "टोकन",
        "where am i", "mera number kab", "number kab aayega", "number kab aayegi", "मेरा नंबर कब",
        "meri baari", "मेरी बारी", "mera turn", "my turn", "kitne log", "kitne patient", "kitne mareez",
        "कितने लोग", "कितने मरीज", "कितने पेशेंट", "how many people", "how many patients", "queue",
    ]),
    # A patient asking for an appointment is a booking request (a proposed
    # book_appointment for staff to approve), whether or not they are
    # registered yet. Checked before confirm_followup so "haan, appointment
    # chahiye" is not read as a follow-up confirmation.
    ("book_appointment", [
        "appointment chahiye", "अपॉइंटमेंट चाहिए", "book appointment", "book an appointment",
        "new appointment", "want an appointment", "need an appointment", "appointment book",
        "अपॉइंटमेंट बुक", "appointment lena", "अपॉइंटमेंट लेना", "appointment lene", "appointment le lo",
        "doctor se milna", "doctor ko dikhana", "dikhana hai", "दिखाना है", "first visit", "consult karna",
        "appointment fix", "get an appointment",
    ]),
    ("confirm_followup", [
        "haan", "हाँ", "हां", "confirm", "कन्फर्म", "aaunga", "आऊंगा",
        "aa jaunga", "ठीक है", "yes i will come", "i will be there",
    ]),
    # Explicit registration only; an appointment request is book_appointment above.
    ("register_patient", [
        "naya patient", "नया मरीज", "i want to register", "register",
    ]),
]


def _normalize(text):
    return unicodedata.normalize("NFC", text).lower()


def classify(text):
    normalized = _normalize(text)
    for intent, keywords in _RULES:
        for kw in keywords:
            if _normalize(kw) in normalized:
                return intent
    return None
