"""The live command "Find the next available appointment with Doctor Mehta and book it for a patient named Neha Gupta."

The model read it right (a free-slots query with the doctor, branch A and limit 1) but the read tool refused the doctor and the
app fell back to "free slots today". Now: the NEXT free slot with that doctor is searched forward (the clock is Friday 10:00, so the
first slot is 10:30), the answer is short, and because the same sentence says "and book it for Neha Gupta" ONE question follows:
"Book Neha Gupta on Fri 9 Oct at 10:30 with Dr. Mehta?". A yes only builds the normal booking card (a new patient's phone is still
asked for); nothing is written until a person presses Approve. In both architectures: the classic one reads the same words with
plain rules (and, with a planner, takes its query), the model-first one takes the model's query and the same follow-up.

Branch A: Dr. Mehta 09:00-13:00 and 16:00-20:00; Branch B: Dr. Rao 10:00-14:00; Branch C: Dr. Iyer 09:00-12:00 (every day).
"""
from tests.replay_cases.dsl import NO_CALL, approve, call, conv, pick, say
from tests.replay_cases.fixtures import BRANCHES, TODAY, TOMORROW, with_setup

SENTENCE = "Find the next available appointment with Doctor Mehta and book it for a patient named Neha Gupta."
LIVE_CALL = call("query", entity="availability", doctor="Dr. Mehta", branch="A", limit=1)      # what Sarvam really returned
PHONE = "9988776655"
OFFER = "Book Neha Gupta on Fri 9 Oct at 10:30 with Dr. Mehta?"

# Dr. Rao's every slot for the next 14 days is booked (Branch B 10:00-14:00 is 8 slots a day), except the ones in `free`.
_DAYS = ["2026-10-{:02d}".format(d) for d in range(9, 23)]
_TIMES = ["10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "13:00", "13:30"]


def _rao_booked(free=()):
    return [{"patient": "nalin", "date": day, "time": at, "branch": "B"} for day in _DAYS for at in _TIMES if (day, at) not in free]


SHARMAS = [{"code": "D", "name": "Branch D", "address": "Sector 14", "pin_code": "122001", "doctor": "Dr. Anil Sharma", "hours": ("09:00", "13:00")},
           {"code": "E", "name": "Branch E", "address": "Sector 21", "pin_code": "122002", "doctor": "Dr. Sunil Sharma", "hours": ("14:00", "18:00")}]

CASES = [
    conv("next_available_find_and_book", "The live sentence: next free slot with Dr. Mehta, then ONE booking question; yes builds the card", "reads", "en", [
        say(SENTENCE, LIVE_CALL, kind="read", intent="check_availability", rows=1, pending_after="book_slot", task_after="book_appointment",
            note_contains=["Next available with Dr. Mehta: Fri 9 Oct, 10:30 (Branch A)", OFFER], note_lacks="15 free slots"),
        say("yes", call("choose_option", index=1), kind="ask", ask_kind="phone", pending_after="phone", task_after="book_appointment",
            slots={"patient_name": "Neha Gupta", "appt_date": TODAY, "start_time": "10:30", "branch": "A"}),
        say(PHONE, call("answer_slot", slot="phone", value=PHONE), kind="card", intent="book_appointment", patient=None,
            slots={"patient_name": "Neha Gupta", "patient_phone": PHONE, "appt_date": TODAY, "start_time": "10:30", "branch": "A"},
            note_contains="Dr. Mehta", pending_after=None, card_after="book_appointment"),
        approve(approved=True),
    ], final_db=[{"patient": {"name": "Neha Gupta", "count": 1}},
                 {"appointment": {"name": "Neha Gupta", "date": TODAY, "time": "10:30", "branch": "A"}}]),

    conv("next_available_registered_patient_gets_the_card_at_once", "Booking it for a registered patient: yes goes straight to the card", "reads", "en", [
        say("Find the next available appointment with Dr. Mehta and book it for Rakesh Verma.", LIVE_CALL, kind="read",
            pending_after="book_slot", note_contains="Book Rakesh Verma on Fri 9 Oct at 10:30 with Dr. Mehta?"),
        say("ok book it", call("choose_option", index=1), kind="card", intent="book_appointment", patient="rakesh",
            slots={"appt_date": TODAY, "start_time": "10:30", "branch": "A"}, pending_after=None),
        approve(approved=True),
    ], final_db=[{"appointment": {"patient": "rakesh", "date": TODAY, "time": "10:30", "branch": "A"}}]),

    conv("next_available_the_option_chip_is_the_yes", "The 'Yes, book it' chip answers the question exactly as saying yes", "reads", "en", [
        say(SENTENCE, LIVE_CALL, kind="read", pending_after="book_slot"),
        pick(0, kind="ask", ask_kind="phone", slots={"patient_name": "Neha Gupta", "start_time": "10:30"}),
    ], final_db=[{"no_writes": True}]),

    conv("next_available_no_the_next_one", "'No, the next one' offers the next free slot and asks again; yes then books THAT one", "reads", "en", [
        say(SENTENCE, LIVE_CALL, kind="read", pending_after="book_slot", note_contains="Fri 9 Oct, 10:30"),
        say("no, the next one", call("choose_option", index=2), kind="read", intent="check_availability", pending_after="book_slot",
            note_contains=["Fri 9 Oct, 11:00", "Book Neha Gupta on Fri 9 Oct at 11:00 with Dr. Mehta?"]),
        say("another time", call("choose_option", index=2), kind="read", pending_after="book_slot",
            note_contains="Book Neha Gupta on Fri 9 Oct at 11:30 with Dr. Mehta?"),
        say("yes", call("choose_option", index=1), kind="ask", ask_kind="phone", slots={"start_time": "11:30", "appt_date": TODAY}),
        say(PHONE, call("answer_slot", slot="phone", value=PHONE), kind="card", intent="book_appointment",
            slots={"start_time": "11:30", "appt_date": TODAY, "patient_name": "Neha Gupta"}),
        approve(approved=True),
    ], final_db=[{"appointment": {"name": "Neha Gupta", "date": TODAY, "time": "11:30", "branch": "A"}},
                 {"appointments_total": 6}]),

    conv("next_available_plain_no_drops_it", "A plain 'no' drops the offer: nothing is booked, nothing remains open", "reads", "en", [
        say(SENTENCE, LIVE_CALL, kind="read", pending_after="book_slot"),
        say("no thanks", NO_CALL, kind="note", note_contains="Okay", pending_after=None, task_after=None),
    ], final_db=[{"no_writes": True}]),

    conv("next_available_a_different_command_replaces_the_offer", "A real new command is not swallowed as an answer to the offer", "reads", "en", [
        say(SENTENCE, LIVE_CALL, kind="read", pending_after="book_slot"),
        say("how many patients are registered", call("query", entity="patients", aggregate="count"), kind="read", intent="patient_count"),
    ], final_db=[{"no_writes": True}]),

    conv("next_available_none_in_14_days", "Nothing free with Dr. Rao for 14 days: a plain sentence, no question, no card", "reads", "en", [
        say("Find the next available appointment with Dr. Rao and book it for Neha Gupta.",
            call("query", entity="availability", doctor="Dr. Rao", branch="B", limit=1),
            kind="read", intent="check_availability", pending_after=None, rows=None,
            note_contains="No free slot with Dr. Rao in the next 14 days (Branch B).", note_lacks="Book Neha"),
        say("yes", NO_CALL, kind=("error", "note"), pending_after=None),
    ], setup=with_setup(appointments=_rao_booked()), final_db=[{"no_writes": True}]),

    conv("next_available_another_time_runs_out", "'Another time' when nothing else is free in the window says so and books nothing", "reads", "en", [
        say("Find the next available appointment with Dr. Rao and book it for Neha Gupta.",
            call("query", entity="availability", doctor="Dr. Rao", limit=1), kind="read", pending_after="book_slot",
            note_contains=["Next available with Dr. Rao: Mon 12 Oct, 10:00 (Branch B)", "Book Neha Gupta on Mon 12 Oct at 10:00 with Dr. Rao?"]),
        say("another time", call("choose_option", index=2), kind="note", pending_after=None,
            note_contains="No other free slot with Dr. Rao in the next 14 days."),
    ], setup=with_setup(appointments=_rao_booked(free=[("2026-10-12", "10:00")])), final_db=[{"no_writes": True}]),

    conv("next_available_unknown_doctor_is_asked", "A doctor nobody has is a question with every doctor as an option, never a guess", "reads", "en", [
        say("Find the next available appointment with Dr. Gupta.", call("query", entity="availability", doctor="Dr. Gupta", next=True),
            kind="ask", ask_kind="doctor", options_count=3, options_contain=["Dr. Mehta", "Dr. Rao", "Dr. Iyer"],
            note_contains="Gupta", pending_after="doctor"),
        pick(2, kind="read", intent="check_availability", note_contains="Next available with Dr. Iyer: Fri 9 Oct, 10:30 (Branch C)"),
    ], final_db=[{"no_writes": True}]),

    conv("next_available_two_doctors_one_name_is_asked", "'Dr. Sharma' with two Sharmas asks which one, then carries on", "reads", "en", [
        say("What is the next available appointment with Dr. Sharma?", call("query", entity="availability", doctor="Dr. Sharma", next=True),
            kind="ask", ask_kind="doctor", options_count=2, options_contain=["Dr. Anil Sharma", "Dr. Sunil Sharma"], pending_after="doctor"),
        say("the second one", call("choose_option", index=2), kind="read", intent="check_availability",
            note_contains="Next available with Dr. Sunil Sharma: Fri 9 Oct, 14:00 (Branch E)", pending_after=None),
    ], setup=with_setup(branches=BRANCHES + SHARMAS), final_db=[{"no_writes": True}]),

    conv("next_available_doctor_named_in_full_is_not_ambiguous", "The full name 'Dr. Anil Sharma' picks that doctor without a question", "reads", "en", [
        say("next available appointment with Dr. Anil Sharma", call("query", entity="availability", doctor="Dr. Anil Sharma", next=True),
            kind="read", intent="check_availability", note_contains="Next available with Dr. Anil Sharma: Fri 9 Oct, 10:30 (Branch D)"),
    ], setup=with_setup(branches=BRANCHES + SHARMAS)),

    conv("next_available_plain_no_booking_tail", "Just 'next available appointment': answers, asks nothing", "reads", "en", [
        say("What is the next available appointment?", call("query", entity="availability", next=True),
            kind="read", intent="check_availability", pending_after=None, task_after=None, rows=1,
            note_contains="Next available: Fri 9 Oct, 10:30 (Branch A)."),
    ], final_db=[{"no_writes": True}]),

    conv("next_available_plain_with_doctor", "'Next available with Dr. Mehta' (no booking tail) just answers", "reads", "en", [
        say("next available with Dr. Mehta", call("query", entity="availability", doctor="Dr. Mehta", next=True),
            kind="read", intent="check_availability", pending_after=None, note_contains="Next available with Dr. Mehta: Fri 9 Oct, 10:30 (Branch A)."),
    ], final_db=[{"no_writes": True}]),

    conv("next_available_a_few", "'Next three available slots with Dr. Mehta' lists three, grouped by day", "reads", "en", [
        say("next three available slots with Dr. Mehta", call("query", entity="availability", doctor="Dr. Mehta", next=True, limit=3),
            kind="read", rows=1, pending_after=None,
            note_contains="Next available with Dr. Mehta: Fri 9 Oct 10:30, 11:00, 11:30 (Branch A)."),
    ], final_db=[{"no_writes": True}]),

    conv("next_available_from_a_later_day", "'Next available with Dr. Mehta from Monday' starts the search on Monday", "reads", "en", [
        say("next available with Dr. Mehta from Monday", call("query", entity="availability", doctor="Dr. Mehta", next=True, date="2026-10-12"),
            kind="read", note_contains="Next available with Dr. Mehta: Mon 12 Oct, 09:00 (Branch A)."),
    ], final_db=[{"no_writes": True}]),

    conv("next_available_iyer_at_branch_c", "'Next available with Dr. Iyer at Branch C' (the 8 Oct failure's doctor) is Dr. Iyer's own schedule", "reads", "en", [
        say("next available with Dr. Iyer at Branch C", call("query", entity="availability", doctor="Dr. Iyer", branch="C", next=True),
            kind="read", note_contains="Next available with Dr. Iyer: Fri 9 Oct, 10:30 (Branch C)."),
    ], final_db=[{"no_writes": True}]),

    conv("next_available_doctor_not_at_that_branch", "Dr. Iyer does not work at Branch B: it says so instead of showing another doctor's slots", "reads", "en", [
        say("next available with Dr. Iyer at Branch B", call("query", entity="availability", doctor="Dr. Iyer", branch="B", next=True),
            kind="read", note_contains="Dr. Iyer does not work at Branch B.", note_lacks="Next available"),
    ], final_db=[{"no_writes": True}]),

    conv("next_available_hinglish", "Hinglish: 'agla available appointment Dr. Mehta ke saath'", "reads", "hinglish", [
        say("agla available appointment Dr. Mehta ke saath", call("query", entity="availability", doctor="Dr. Mehta", next=True),
            kind="read", intent="check_availability", pending_after=None,
            note_contains="Agla khaali slot Dr. Mehta ke saath: Fri 9 Oct, 10:30 (Branch A)."),
    ], final_db=[{"no_writes": True}]),

    conv("next_available_hinglish_find_and_book", "Hinglish with the booking tail: the question and the phone question are Hinglish too", "reads", "hinglish", [
        say("agla available appointment Dr. Mehta ke saath aur Neha Gupta ke liye book karo",
            call("query", entity="availability", doctor="Dr. Mehta", next=True), kind="read", pending_after="book_slot",
            note_contains="Neha Gupta ko Fri 9 Oct 10:30 baje Dr. Mehta ke saath book karun?"),
        say("haan", call("choose_option", index=1), kind="ask", ask_kind="phone", note_contains="phone number kya hai"),
    ], final_db=[{"no_writes": True}]),

    conv("next_available_devanagari_find_and_book", "Devanagari: the doctor's Devanagari name is matched, the question is asked in Devanagari", "reads", "hi", [
        say("डॉक्टर मेहता के साथ अगला उपलब्ध अपॉइंटमेंट और नेहा गुप्ता के लिए बुक करो",
            call("query", entity="availability", doctor="डॉक्टर मेहता", next=True), kind="read", intent="check_availability",
            pending_after="book_slot", note_contains=["Dr. Mehta", "नेहा गुप्ता को Fri 9 Oct 10:30 बजे Dr. Mehta के साथ"]),
        say("हाँ", call("choose_option", index=1), kind="ask", ask_kind="phone", note_contains="फ़ोन नंबर"),
    ], final_db=[{"no_writes": True}]),

    conv("next_available_the_word_book_elsewhere_is_not_a_booking_request", "'Book' and a name in the sentence but no booking tail: only the answer", "reads", "en", [
        say("What is the next available appointment with Dr. Mehta, Neha Gupta called to book one",
            call("query", entity="availability", doctor="Dr. Mehta", next=True), kind="read", pending_after=None,
            note_lacks="Book Neha"),
    ], final_db=[{"no_writes": True}]),

    conv("next_available_yes_with_a_card_on_screen_answers_the_question_not_the_card", "A yes while a card is open answers the OFFER (a new card); the open card is never approved", "safety", "en", [
        say("book Rakesh Verma tomorrow at 5 pm", call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW, time="17:00"),
            kind="card", intent="book_appointment", patient="rakesh"),
        say(SENTENCE, LIVE_CALL, kind="read", pending_after="book_slot"),
        say("yes", call("choose_option", index=1), kind="ask", ask_kind="phone", slots={"patient_name": "Neha Gupta", "start_time": "10:30"}),
    ], final_db=[{"no_writes": True}]),

    conv("next_available_yes_never_approves_the_card_it_builds", "After the yes built the card, another 'yes' does not approve it: only a person pressing Approve does", "safety", "en", [
        say("Find the next available appointment with Dr. Mehta and book it for Rakesh Verma.", LIVE_CALL, kind="read", pending_after="book_slot"),
        say("yes", call("choose_option", index=1), kind="card", intent="book_appointment", patient="rakesh", pending_after=None),
        say("yes", NO_CALL, kind=("note", "error")),
        say("ok approve it", NO_CALL, kind=("note", "error")),
    ], final_db=[{"no_writes": True}]),

    conv("next_available_yes_without_an_offer_does_nothing", "A lone 'yes' when no question is open: nothing is booked, nothing approved", "safety", "en", [
        say("yes", NO_CALL, kind=("note", "error"), pending_after=None, card_after=None),
        say("haan book kar do", NO_CALL, kind=("note", "error", "ask", "card")),
    ], final_db=[{"appointments_total": 5}]),
]
