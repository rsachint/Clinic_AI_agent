"""This week's real bugs, each as a conversation: the seams between the rule routes and the planner."""
from tests.replay_cases.dsl import NO_CALL, approve, call, conv, idle, network, pick, say
from tests.replay_cases.fixtures import MONDAY, TOMORROW, with_setup

CASES = [
    conv("manju_move_one_word_name", "Move Manju's appointment: a one-word name the move rule used to skip", "regression", "en", [
        say("Move Manju's appointment to branch C to tomorrow at eleven am.",
            call("reschedule_appointment", patient_name="Manju", current_date=TOMORROW, new_date=TOMORROW, new_time="11:00", new_branch="C"),
            kind="card", intent="reschedule_appointment", patient="manju",
            slots={"patient_name": "Manju", "appt_date": TOMORROW, "start_time": "11:00", "branch": "C"}),
        approve(approved=True),
    ], final_db=[{"appointment": {"patient": "manju", "date": TOMORROW, "time": "11:00", "branch": "C"}},
                 {"appointment": {"patient": "manju", "date": TOMORROW, "time": "12:00", "count": 0}}]),

    conv("manju_move_devanagari", "The same move said in Devanagari (the name is a Roman one-word patient)", "regression", "hi", [
        say("मंजू का अपॉइंटमेंट कल 11 बजे शिफ्ट करो",
            call("reschedule_appointment", patient_name="मंजू", new_date=TOMORROW, new_time="11:00"),
            kind="card", intent="reschedule_appointment", patient="manju", slots={"appt_date": TOMORROW, "start_time": "11:00"}),
    ]),

    conv("priya_her_mobile_pronoun", "A pronoun that only owns a phone number must not bring back the remembered patient", "regression", "en", [
        say("show me the details of patient Rakesh Verma", call("query", entity="patients", aggregate="list", patient_name="Rakesh Verma"),
            kind="read", intent="patient_lookup", remembered_after="rakesh"),
        say("I want to book a consultation for Priya, her mobile number is 9111122233, tomorrow at 5 pm",
            call("book_appointment", patient_name="Priya", date=TOMORROW, time="17:00", phone="9111122233"),
            kind="card", intent="book_appointment", patient=None,
            slots={"patient_name": "Priya", "patient_phone": "9111122233", "appt_date": TOMORROW, "start_time": "17:00"}),
    ], setup=with_setup(without=["priya_shah", "priya_dev"])),

    conv("dotted_time_11_pm", "'11 p.m.' with dots is 23:00, not 11:00", "regression", "en", [
        say("Book Rakesh Verma coming Saturday at 11 p.m.",
            call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW, time="23:00"),
            kind="card", intent="book_appointment", patient="rakesh", slots={"appt_date": TOMORROW, "start_time": "23:00"}),
        say("actually make it 11 P.M. on Monday", call("correct_card", field="time", value="23:00"),
            kind="card_update", slots={"start_time": "23:00"}, card_after="book_appointment"),
    ]),

    conv("phone_in_words_english", "A phone number said digit by digit in English", "regression", "en", [
        say("book an appointment for Kavita Rao tomorrow at 5 pm, mobile number nine one one one two two three three four four",
            call("book_appointment", patient_name="Kavita Rao", date=TOMORROW, time="17:00", phone="91112233"),
            kind="card", intent="book_appointment", slots={"patient_name": "Kavita Rao", "patient_phone": "9111223344"}),
    ]),

    conv("phone_in_words_hinglish", "A phone number said in Hinglish digit words", "regression", "hinglish", [
        say("Kavita Rao ke liye kal shaam 5 baje appointment book karo, number nau ek ek ek do do teen teen chaar chaar",
            call("book_appointment", patient_name="Kavita Rao", date=TOMORROW, time="17:00"),
            kind="card", intent="book_appointment", slots={"patient_name": "Kavita Rao", "patient_phone": "9111223344",
                                                          "appt_date": TOMORROW, "start_time": "17:00"}),
    ]),

    conv("phone_in_devanagari_digits", "A phone number written in Devanagari digits answers the phone question", "regression", "hi", [
        say("book Kavita Rao tomorrow at 5 pm", call("book_appointment", patient_name="Kavita Rao", date=TOMORROW, time="17:00"),
            kind="ask", ask_kind="phone", pending_after="phone"),
        say("९१११२२३३४४", call("answer_slot", slot="phone", value="9111223344"),
            kind="card", intent="book_appointment", slots={"patient_phone": "9111223344"}, pending_after=None),
    ]),

    conv("nalin_new_patient_after_which_one", "'It's a new patient called Nalin' answers 'Which one?' and the booking carries on", "regression", "en", [
        say("book Rahul tomorrow at 5 pm", call("book_appointment", patient_name="Rahul", date=TOMORROW, time="17:00"),
            kind="ask", ask_kind="choose_patient", options_count=2, pending_after="choose_patient", task_after="book_appointment"),
        say("no, it's a new patient called Nalin", call("new_patient", name="Nalin"),
            kind="ask", ask_kind="phone", slots={"patient_name": "Nalin", "appt_date": TOMORROW, "start_time": "17:00"},
            pending_after="phone", task_after="book_appointment"),
        say("9988776655", call("answer_slot", slot="phone", value="9988776655"),
            kind="card", intent="book_appointment", slots={"patient_name": "Nalin", "patient_phone": "9988776655",
                                                          "appt_date": TOMORROW, "start_time": "17:00"}, pending_after=None),
        approve(approved=True),
    ], setup=with_setup(without=["nalin"]),
        final_db=[{"patient": {"name": "Nalin", "count": 1}},
                  {"appointment": {"name": "Nalin", "date": TOMORROW, "time": "17:00"}},
                  {"no_duplicate_patients": True}]),

    conv("nalin_new_patient_after_which_patient", "'It's a new patient called Nalin' answers 'Which patient?' and keeps the day and time", "regression", "en", [
        say("book an appointment tomorrow at 5 pm", call("clarify", question="Which patient?"),
            kind="ask", ask_kind="clarify", pending_after="clarify"),
        say("it's a new patient called Nalin", call("book_appointment", patient_name="Nalin", date=TOMORROW, time="17:00"),
            kind="ask", ask_kind="phone", slots={"patient_name": "Nalin", "appt_date": TOMORROW, "start_time": "17:00"}),
        say("9988776655", call("answer_slot", slot="phone", value="9988776655"),
            kind="card", slots={"patient_name": "Nalin", "patient_phone": "9988776655"}),
    ], setup=with_setup(without=["nalin"])),

    conv("two_rahuls_choose_by_phone_suffix", "Two patients named Rahul Sharma: ask which, choose by the end of the phone number", "regression", "en", [
        say("book Rahul Sharma tomorrow at 5 pm", call("book_appointment", patient_name="Rahul Sharma", date=TOMORROW, time="17:00"),
            kind="ask", ask_kind="choose_patient", options_count=2, options_contain=["3210", "6543"]),
        say("the one ending 6543", call("choose_option", index=2),
            kind="card", intent="book_appointment", patient="rahul_en", slots={"appt_date": TOMORROW, "start_time": "17:00"},
            pending_after=None),
    ]),

    conv("two_rahuls_choose_by_position", "Two patients named Rahul Sharma: choose by position", "regression", "en", [
        say("book Rahul Sharma tomorrow at 5 pm", call("book_appointment", patient_name="Rahul Sharma", date=TOMORROW, time="17:00"),
            kind="ask", ask_kind="choose_patient", options_count=2),
        say("the first one", call("choose_option", index=1),
            kind="card", intent="book_appointment", patient="rahul_dev"),
    ]),

    conv("two_amits_cancel", "Two patients called Amit each have an appointment: cancel only the one chosen", "regression", "en", [
        say("cancel Amit's appointment", call("cancel_appointment", patient_name="Amit"),
            kind="ask", ask_kind="choose_patient", options_count=2, options_contain=["Amit Dua", "Amit Anand"]),
        say("Amit Anand", call("answer_slot", slot="patient_name", value="Amit Anand"),
            kind="card", intent="cancel_appointment", patient="amit_anand"),
        approve(approved=True),
    ], final_db=[{"appointment": {"patient": "amit_anand", "status": "cancelled"}},
                 {"appointment": {"patient": "amit_dua", "date": MONDAY, "time": "11:00"}}]),

    conv("read_interrupts_open_booking", "A read in the middle of a booking leaves the booking's question open", "regression", "en", [
        say("book Rakesh Verma tomorrow", call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW),
            kind="ask", ask_kind="time", pending_after="time", task_after="book_appointment"),
        say("who is booked tomorrow", call("query", entity="appointments", aggregate="list", date=TOMORROW),
            kind="read", intent="list_appointments", note_contains="Still waiting", pending_after="time", task_after="book_appointment"),
        say("5 pm", call("answer_slot", slot="time", value="17:00"),
            kind="card", intent="book_appointment", patient="rakesh", slots={"start_time": "17:00", "appt_date": TOMORROW},
            pending_after=None),
    ]),

    conv("card_correction_make_it_7", "'Actually make it 7' edits the open card", "regression", "en", [
        say("book Rakesh Verma tomorrow at 5 pm", call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW, time="17:00"),
            kind="card", intent="book_appointment", patient="rakesh", slots={"start_time": "17:00"}),
        say("actually make it 7", call("correct_card", field="time", value="19:00"),
            kind="card_update", slots={"start_time": "19:00"}, card_after="book_appointment"),
        approve(approved=True),
    ], final_db=[{"appointment": {"patient": "rakesh", "date": TOMORROW, "time": "19:00"}}]),

    conv("branch_change_note", "Moving to another branch says so on the card", "regression", "en", [
        say("move Manju to branch B tomorrow at 11 am",
            call("reschedule_appointment", patient_name="Manju", new_date=TOMORROW, new_time="11:00", new_branch="B"),
            kind="card", intent="reschedule_appointment", patient="manju", slots={"branch": "B"},
            note_contains="Moving from Branch A to Branch B"),
    ]),

    conv("name_beats_remembered_patient", "A person named in the sentence beats the patient just discussed", "regression", "en", [
        say("show me the details of patient Rakesh Verma", call("query", entity="patients", aggregate="list", patient_name="Rakesh Verma"),
            kind="read", remembered_after="rakesh"),
        say("book Sunita Devi tomorrow at 4 pm", call("book_appointment", patient_name="Sunita Devi", date=TOMORROW, time="16:00"),
            kind="card", intent="book_appointment", patient="sunita", slots={"patient_name": "Sunita Devi"}),
    ]),

    conv("model_copies_remembered_patient", "The model wrongly fills in the remembered patient: code refuses and asks", "regression", "en", [
        say("show me the details of patient Rakesh Verma", call("query", entity="patients", aggregate="list", patient_name="Rakesh Verma"),
            kind="read", remembered_after="rakesh"),
        say("book Kamal tomorrow at 4 pm", call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW, time="16:00"),
            kind="ask", ask_kind="patient", patient=None, slots={"appt_date": TOMORROW, "start_time": "16:00"}),
    ], configs=["model_first_scripted", "model_first_live"],
        note="Injects a wrong model answer, so it only runs where the state card (which shows the remembered patient) exists."),

    conv("book_him_follows_lookup", "'Book him' after looking someone up", "regression", "en", [
        say("show me the details of patient Rakesh Verma", call("query", entity="patients", aggregate="list", patient_name="Rakesh Verma"),
            kind="read", remembered_after="rakesh"),
        say("book him tomorrow at 5", call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW, time="17:00"),
            kind="card", intent="book_appointment", patient="rakesh", slots={"appt_date": TOMORROW, "start_time": "17:00"}),
    ]),

    conv("cancel_the_second_one", "'Cancel the second one' after a list", "regression", "en", [
        say("who is booked tomorrow", call("query", entity="appointments", aggregate="list", date=TOMORROW),
            kind="read", intent="list_appointments", list_after=3),
        say("cancel the second one", call("cancel_appointment", patient_name="Rakesh Verma"),
            kind="card", intent="cancel_appointment", patient="rakesh"),
        approve(approved=True),
    ], final_db=[{"appointment": {"patient": "rakesh", "status": "cancelled"}},
                 {"appointment": {"patient": "sunita", "date": TOMORROW, "time": "10:00"}},
                 {"appointment": {"patient": "manju", "date": TOMORROW, "time": "12:00"}}]),

    conv("memory_timeout_then_bare_time", "A bare time after the 10-minute memory expired gets the 'I stopped waiting' reply", "regression", "en", [
        say("book Rakesh Verma tomorrow", call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW),
            kind="ask", ask_kind="time"),
        idle(11),
        say("5 pm", call("unsupported", reason="a bare time with nothing open"),
            kind="error", note_contains="stopped waiting", pending_after=None, no_write=True),
    ]),

    conv("network_down_falls_back_to_rules", "Planner unreachable: the turn falls back to the rules and still works", "regression", "en", [
        network("down"),
        say("book Rakesh Verma tomorrow at 5 pm", call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW, time="17:00"),
            kind="card", intent="book_appointment", patient="rakesh", slots={"appt_date": TOMORROW, "start_time": "17:00"}),
        network("up"),
        say("show me the details of patient Sunita Devi", call("query", entity="patients", aggregate="list", patient_name="Sunita Devi"),
            kind="read", intent="patient_lookup", route_contains="planner"),
    ]),

    conv("read_while_a_card_is_open", "A question about tomorrow while a card is open must not edit the card's date", "regression", "en", [
        say("book Rakesh Verma on Monday at 5 pm", call("book_appointment", patient_name="Rakesh Verma", date=MONDAY, time="17:00"),
            kind="card", intent="book_appointment", patient="rakesh", slots={"appt_date": MONDAY}),
        say("who is booked tomorrow", call("query", entity="appointments", aggregate="list", date=TOMORROW),
            kind="read", intent="list_appointments", card_after="book_appointment"),
    ]),

    conv("reschedule_with_no_appointment", "Rescheduling someone who has no appointment should say so, not open an empty card", "regression", "en", [
        say("reschedule Nalin to Monday at 5 pm", call("reschedule_appointment", patient_name="Nalin", new_date=MONDAY, new_time="17:00"),
            kind="note", note_contains="No upcoming appointment"),
    ], known_gap="there is no 'no appointment found' reply yet: the app opens a reschedule card with an empty appointment list and a small note"),

    conv("consultation_is_not_record_visit", "'Book a consultation' is a booking, not a visit record", "regression", "en", [
        say("book a consultation for Rakesh Verma tomorrow at 5 pm",
            call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW, time="17:00"),
            kind="card", intent="book_appointment", patient="rakesh", slots={"start_time": "17:00"}),
    ]),

    conv("shift_karo_is_a_move", "'Shift karo' is a move, not attendance", "regression", "hinglish", [
        say("Amit Dua ka appointment parso 3 baje shift karo",
            call("reschedule_appointment", patient_name="Amit Dua", new_date="2026-10-11", new_time="15:00"),
            kind="card", intent="reschedule_appointment", patient="amit_dua", slots={"appt_date": "2026-10-11", "start_time": "15:00"}),
    ]),

    conv("patient_count_is_a_read", "'How many patients' is a count, never a registration", "regression", "en", [
        say("how many patients are registered", call("query", entity="patients", aggregate="count"),
            kind="read", intent="patient_count", note_contains="12"),
    ]),

    conv("branch_switch_by_voice", "Switching this computer's branch is handled by a precise rule", "regression", "en", [
        say("switch to branch C", call("switch_branch", branch="C"), kind="switch_branch", intent="set_my_branch",
            slots={"branch": "C"}),
    ]),

    conv("close_branch_plan", "Closing a branch only produces a plan on a review card", "regression", "en", [
        say("close branch B tomorrow", call("close_branch", branch="B", start_date=TOMORROW),
            kind="closure", intent="close_branch"),
    ], final_db=[{"no_writes": True}]),
]
