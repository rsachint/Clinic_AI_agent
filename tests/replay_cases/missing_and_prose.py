"""The live command "I want to book a consultation for Priya, her mobile number is nine eight seven six five four three
two one zero." (no day, no time) in both architectures, and the cases around it:

  * a booking tool call that lacks the day, the time or the patient is NOT thrown away: the app asks for what is
    missing ("Which day?", "What time?", "Which patient?") and keeps what was said (clinic/nlu/tools.py `askable`);
  * a model that answers in plain words ("What date and time would you like?") has its short question shown as the
    assistant's own (clinic/nlu/prose.py); a reply that claims an action, runs long or looks like code is not shown.

The phone number in the sentence belongs to nobody on file, so (exact matching) it is a NEW patient called Priya and no
"Which one?" is asked; without a number, "Priya" fits two patients and "Which one?" is. Both are here."""
from tests.replay_cases.dsl import approve, call, conv, pick, prose, say
from tests.replay_cases.fixtures import TOMORROW, with_setup

LIVE = "I want to book a consultation for Priya, her mobile number is nine eight seven six five four three two one zero."
PHONE = "9876543210"

CASES = [
    conv("live_priya_tool_call_without_day_or_time", "The live sentence: a correct booking call with no day or time is asked for, not rejected", "regression", "en", [
        say(LIVE, call("book_appointment", patient_name="Priya", phone=PHONE),
            kind="ask", ask_kind="date", intent="book_appointment", pending_after="date", task_after="book_appointment",
            slots={"patient_name": "Priya", "patient_phone": PHONE}, route_contains="planner"),
        say("tomorrow", call("answer_slot", slot="date", value=TOMORROW),
            kind="ask", ask_kind="time", slots={"appt_date": TOMORROW, "patient_phone": PHONE}, pending_after="time"),
        say("5 pm", call("answer_slot", slot="time", value="17:00"),
            kind="card", intent="book_appointment", pending_after=None, patient="rahul_dev",
            note_contains="you said Priya",
            slots={"patient_name": "Priya", "patient_phone": PHONE, "appt_date": TOMORROW, "start_time": "17:00"}),
    ], final_db=[{"no_writes": True}],
        note="In this clinic 9876543210 is Rahul Sharma's number (Devanagari), and a full number is a hard identifier: the card is "
             "his, with the warning 'this phone number belongs to ...; you said Priya'. Nothing is written until a person decides."),

    conv("live_priya_new_patient_with_that_number", "The live sentence where nobody has the number: a new patient Priya, booked after the day and time", "regression", "en", [
        say(LIVE, call("book_appointment", patient_name="Priya", phone=PHONE),
            kind="ask", ask_kind="date", pending_after="date", slots={"patient_name": "Priya", "patient_phone": PHONE}),
        say("tomorrow", call("answer_slot", slot="date", value=TOMORROW), kind="ask", ask_kind="time"),
        say("5 pm", call("answer_slot", slot="time", value="17:00"),
            kind="card", intent="book_appointment", pending_after=None, patient=None,
            slots={"patient_name": "Priya", "patient_phone": PHONE, "appt_date": TOMORROW, "start_time": "17:00"}),
        approve(approved=True),
    ], setup=with_setup(without=["rahul_dev"]),
        final_db=[{"patient": {"name": "Priya", "count": 1}}, {"appointment": {"name": "Priya", "date": TOMORROW, "time": "17:00"}},
                  {"no_duplicate_patients": True}]),

    conv("live_priya_prose_question_then_answer", "The live sentence answered in words: the question is shown, the answer books", "regression", "en", [
        say(LIVE, prose("What date and time would you like?"),
            kind="ask", ask_kind="clarify", note_contains="What date and time would you like?", pending_after="clarify",
            route_contains="planner", no_write=True),
        say("tomorrow at 5 pm", call("book_appointment", patient_name="Priya", date=TOMORROW, time="17:00", phone=PHONE),
            kind="card", intent="book_appointment", pending_after=None, card_has="What date and time would you like?",
            slots={"patient_name": "Priya", "patient_phone": PHONE, "appt_date": TOMORROW, "start_time": "17:00"}),
    ]),

    conv("priya_without_a_number_asks_which_one_then_day_then_time", "No number said: 'Which one?' for the two Priyas, then the day, then the time", "dialogue", "en", [
        say("I want to book a consultation for Priya", call("book_appointment", patient_name="Priya"),
            kind="ask", ask_kind="choose_patient", options_count=2, pending_after="choose_patient", route_contains="planner"),
        pick(0, kind="ask", ask_kind="date", pending_after="date"),
        say("tomorrow", call("answer_slot", slot="date", value=TOMORROW), kind="ask", ask_kind="time", pending_after="time"),
        say("5 pm", call("answer_slot", slot="time", value="17:00"),
            kind="card", intent="book_appointment", patient="priya_shah", slots={"appt_date": TOMORROW, "start_time": "17:00"}),
    ]),

    conv("booking_call_without_a_time_asks_what_time", "A booking call with the day but no time asks 'What time?'", "dialogue", "en", [
        say("book Rakesh Verma tomorrow", call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW),
            kind="ask", ask_kind="time", slots={"appt_date": TOMORROW}, pending_after="time", route_contains="planner"),
        say("5 pm", call("answer_slot", slot="time", value="17:00"),
            kind="card", patient="rakesh", slots={"appt_date": TOMORROW, "start_time": "17:00"}),
    ]),

    conv("booking_call_without_a_day_asks_which_day", "A booking call with the time but no day asks 'Which day?'", "dialogue", "en", [
        say("book Rakesh Verma at 5 pm", call("book_appointment", patient_name="Rakesh Verma", time="17:00"),
            kind="ask", ask_kind="date", slots={"start_time": "17:00"}, pending_after="date", route_contains="planner"),
        say("tomorrow", call("answer_slot", slot="date", value=TOMORROW),
            kind="card", patient="rakesh", slots={"appt_date": TOMORROW, "start_time": "17:00"}),
    ]),

    conv("booking_call_without_a_patient_asks_which_patient", "A booking call with no patient asks 'Which patient?' and keeps the day and time", "dialogue", "en", [
        say("book an appointment tomorrow at 5 pm", call("book_appointment", date=TOMORROW, time="17:00"),
            kind="ask", ask_kind="patient", slots={"appt_date": TOMORROW, "start_time": "17:00"}, pending_after="patient",
            route_contains="planner"),
        say("Rakesh Verma", call("answer_slot", slot="patient_name", value="Rakesh Verma"),
            kind="card", patient="rakesh", slots={"appt_date": TOMORROW, "start_time": "17:00"}),
    ]),

    conv("booking_call_with_nothing_in_it_is_still_rejected", "A booking call with no detail at all is no booking: the rules decide", "safety", "en", [
        say("hello there", call("book_appointment"), kind=("error", "note"), note_lacks="Which"),
    ], final_db=[{"no_writes": True}]),

    conv("hinglish_booking_call_without_a_day", "Hinglish: a booking call with no day asks 'Kis din?'", "multilingual", "hinglish", [
        say("Rakesh Verma ke liye appointment book karo", call("book_appointment", patient_name="Rakesh Verma"),
            kind="ask", ask_kind="date", note_contains="Kis din", pending_after="date"),
        say("kal", call("answer_slot", slot="date", value=TOMORROW), kind="ask", ask_kind="time", note_contains="Kitne baje"),
        say("shaam paanch baje", call("answer_slot", slot="time", value="17:00"),
            kind="card", patient="rakesh", slots={"appt_date": TOMORROW, "start_time": "17:00"}),
    ]),

    conv("devanagari_booking_call_without_a_day", "Devanagari: the registered patient is found and the day is asked", "multilingual", "hi", [
        say("प्रिया शर्मा के लिए अपॉइंटमेंट बुक करो", call("book_appointment", patient_name="प्रिया शर्मा"),
            kind="ask", ask_kind="date", pending_after="date"),
        say("कल", call("answer_slot", slot="date", value=TOMORROW), kind="ask", ask_kind="time"),
        say("शाम पाँच बजे", call("answer_slot", slot="time", value="17:00"),
            kind="card", patient="priya_dev", slots={"appt_date": TOMORROW, "start_time": "17:00"}),
    ]),

    conv("prose_that_claims_a_booking_is_not_shown", "The model says it booked it: that is never shown as the assistant's words", "safety", "en", [
        say(LIVE, prose("Sure, I've booked it for you."), kind=("ask", "error", "note"), note_lacks=["booked", "Sure"]),
    ], final_db=[{"no_writes": True}]),

    conv("prose_that_claims_in_hinglish_is_not_shown", "Hinglish claim 'book kar di': not shown, nothing written", "safety", "hinglish", [
        say(LIVE, prose("Priya ke liye appointment book kar di gayi hai."), kind=("ask", "error", "note"),
            note_lacks=["book kar di", "gayi"]),
    ], final_db=[{"no_writes": True}]),

    conv("prose_that_is_long_is_ignored", "A long plain-words reply is ignored and the rules take the sentence", "safety", "en", [
        say(LIVE, prose("Thank you for contacting the clinic assistant. " * 6 + "What date and time would you like?"),
            kind=("ask", "error", "note"), note_lacks=["Thank you for contacting"]),
    ], final_db=[{"no_writes": True}]),

    conv("prose_with_a_link_is_ignored", "A reply with a link is ignored", "safety", "en", [
        say(LIVE, prose("Please open https://example.com/book to choose a time?"), kind=("ask", "error", "note"),
            note_lacks=["example.com"]),
    ], final_db=[{"no_writes": True}]),

    conv("prose_question_in_hindi", "A short Hindi question in plain words is shown as the question", "multilingual", "hi", [
        say("प्रिया शर्मा के लिए अपॉइंटमेंट बुक करो", prose("आप किस दिन और कितने बजे आना चाहेंगे?"),
            kind="ask", ask_kind="clarify", note_contains="किस दिन", pending_after="clarify"),
        say("कल शाम पाँच बजे", call("book_appointment", patient_name="प्रिया शर्मा", date=TOMORROW, time="17:00"),
            kind="card", patient="priya_dev", slots={"appt_date": TOMORROW, "start_time": "17:00"}),
    ]),

    conv("prose_asks_for_the_patient", "'Which patient is this for?' in plain words is shown, and the name answers it", "dialogue", "en", [
        say("book an appointment tomorrow at 5 pm", prose("Which patient is this appointment for?"),
            kind="ask", ask_kind="clarify", note_contains="Which patient", pending_after="clarify"),
        say("Rakesh Verma", call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW, time="17:00"),
            kind="card", patient="rakesh", slots={"appt_date": TOMORROW, "start_time": "17:00"}),
    ]),
]
