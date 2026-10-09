"""Multi-turn dialogue: questions the assistant asks and the answers that carry the task on."""
from tests.replay_cases.dsl import NO_CALL, approve, call, conv, idle, network, pick, say
from tests.replay_cases.fixtures import MONDAY, SUNDAY, TOMORROW, with_setup

CASES = [
    conv("clarify_chain_then_book", "A booking with the day and time missing is clarified twice, then booked", "dialogue", "en", [
        say("book Rakesh Verma", call("clarify", question="Which day and time?"),
            kind="ask", ask_kind="clarify", pending_after="clarify"),
        say("tomorrow", call("clarify", question="What time tomorrow?"), kind="ask", ask_kind="clarify"),
        say("5 pm", call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW, time="17:00"),
            kind="card", intent="book_appointment", patient="rakesh", slots={"appt_date": TOMORROW, "start_time": "17:00"},
            pending_after=None),
        approve(approved=True),
    ], final_db=[{"appointment": {"patient": "rakesh", "date": TOMORROW, "time": "17:00"}}]),

    conv("move_asks_day_then_time", "A move with no day or time asks for the day, then the time", "dialogue", "en", [
        say("move Manju's appointment", call("reschedule_appointment", patient_name="Manju"),
            kind="ask", ask_kind="date", pending_after="date", task_after="reschedule_appointment"),
        say("Monday", call("answer_slot", slot="date", value=MONDAY),
            kind="ask", ask_kind="time", slots={"appt_date": MONDAY}, pending_after="time"),
        say("12 pm", call("answer_slot", slot="time", value="12:00"),
            kind="card", intent="reschedule_appointment", patient="manju", slots={"appt_date": MONDAY, "start_time": "12:00"},
            pending_after=None),
        approve(approved=True),
    ], final_db=[{"appointment": {"patient": "manju", "date": MONDAY, "time": "12:00"}}]),

    conv("day_answer_carries_a_time", "A day answer that also says the time fills both", "dialogue", "en", [
        say("move Manju's appointment", call("reschedule_appointment", patient_name="Manju"), kind="ask", ask_kind="date"),
        say("Monday at 12 pm", call("answer_slot", slot="date", value=MONDAY),
            kind="card", intent="reschedule_appointment", patient="manju", slots={"appt_date": MONDAY, "start_time": "12:00"},
            pending_after=None),
    ]),

    conv("patient_question_then_name", "No name heard: the assistant asks which patient and takes the answer", "dialogue", "en", [
        say("move the appointment to Monday at 12 pm", call("reschedule_appointment", new_date=MONDAY, new_time="12:00", patient_name="Someone"),
            kind="ask", ask_kind="patient", pending_after="patient"),
        say("Manju", call("answer_slot", slot="patient_name", value="Manju"),
            kind="card", intent="reschedule_appointment", patient="manju", slots={"appt_date": MONDAY, "start_time": "12:00"}),
    ]),

    conv("never_mind_drops_the_question", "'Never mind' drops the open question and nothing is written", "dialogue", "en", [
        say("move Manju's appointment", call("reschedule_appointment", patient_name="Manju"), kind="ask", ask_kind="date", pending_after="date"),
        say("never mind", call("cancel_task"), kind="note", note_contains="dropped", pending_after=None, task_after=None),
    ], final_db=[{"no_writes": True}]),

    conv("skip_the_time", "'Skip' for the time is something the model has no tool for: the rules take that turn", "dialogue", "en", [
        say("move Manju to Monday", call("reschedule_appointment", patient_name="Manju", new_date=MONDAY),
            kind="ask", ask_kind="time"),
        say("skip", NO_CALL, kind="card", intent="reschedule_appointment", patient="manju", pending_after=None),
    ]),

    conv("card_edits_time_and_branch", "Two voice edits to the open card (time, then branch), then approve", "dialogue", "en", [
        say("book Rakesh Verma tomorrow at 5 pm", call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW, time="17:00"),
            kind="card", patient="rakesh"),
        say("make it 11 am", call("correct_card", field="time", value="11:00"), kind="card_update", slots={"start_time": "11:00"}),
        say("make it Branch B", call("correct_card", field="branch", value="B"), kind="card_update", slots={"branch": "B"}),
        approve(approved=True),
    ], final_db=[{"appointment": {"patient": "rakesh", "date": TOMORROW, "time": "11:00", "branch": "B"}}]),

    conv("card_edit_the_date", "'Change the date to Monday' edits the card's date", "dialogue", "en", [
        say("book Rakesh Verma tomorrow at 5 pm", call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW, time="17:00"),
            kind="card", patient="rakesh"),
        say("change the date to Monday", call("correct_card", field="date", value=MONDAY),
            kind="card_update", slots={"appt_date": MONDAY}, card_after="book_appointment"),
        approve(approved=True),
    ], final_db=[{"appointment": {"patient": "rakesh", "date": MONDAY, "time": "17:00"}}]),

    conv("card_phone_correction", "A new phone number said for a booking card replaces the phone on the card", "dialogue", "en", [
        say("book Kavita Rao tomorrow at 5 pm", call("book_appointment", patient_name="Kavita Rao", date=TOMORROW, time="17:00"),
            kind="ask", ask_kind="phone"),
        say("9111223344", call("answer_slot", slot="phone", value="9111223344"), kind="card", slots={"patient_phone": "9111223344"}),
        say("no the phone is 9111223355", call("correct_card", field="phone", value="9111223355"),
            kind="card_update", slots={"patient_phone": "9111223355"}),
        approve(approved=True),
    ], final_db=[{"patient": {"name": "Kavita Rao", "count": 1}}, {"appointment": {"name": "Kavita Rao", "date": TOMORROW, "time": "17:00"}}]),

    conv("card_name_correction_clears_the_old_patient", "Changing the patient's name on a card must not keep the old patient", "dialogue", "en", [
        say("book Rakesh Verma tomorrow at 5 pm", call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW, time="17:00"),
            kind="card", patient="rakesh"),
        say("no it's for Sunita Devi", call("correct_card", field="patient_name", value="Sunita Devi"),
            kind="card_update", slots={"patient_name": "Sunita Devi"}, remembered_after="sunita"),
        approve(approved=True),
    ], final_db=[{"appointment": {"patient": "sunita", "date": TOMORROW, "time": "17:00"}},
                 {"appointment": {"patient": "rakesh", "date": TOMORROW, "time": "17:00", "count": 0}}],
        note="The old patient's id leaves the card with the name; the new patient is selected and remembered.",
        configs=["model_first_scripted", "model_first_live"]),

    conv("branch_named_in_booking", "A branch said in the booking sentence goes on the card", "dialogue", "en", [
        say("book Rakesh Verma tomorrow at 11 am at Branch B", call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW,
                                                                      time="11:00", branch="B"),
            kind="card", patient="rakesh", slots={"branch": "B", "start_time": "11:00"}, note_contains="Branch B"),
    ]),

    conv("two_bookings_in_a_row", "A second booking after the first card was approved", "dialogue", "en", [
        say("book Rakesh Verma tomorrow at 5 pm", call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW, time="17:00"),
            kind="card", patient="rakesh"),
        approve(approved=True),
        say("book Sunita Devi tomorrow at 6 pm", call("book_appointment", patient_name="Sunita Devi", date=TOMORROW, time="18:00"),
            kind="card", patient="sunita", slots={"start_time": "18:00"}),
        approve(approved=True),
    ], final_db=[{"appointment": {"patient": "rakesh", "date": TOMORROW, "time": "17:00"}},
                 {"appointment": {"patient": "sunita", "date": TOMORROW, "time": "18:00"}}]),

    conv("booking_a_taken_slot_is_refused_at_approve", "A slot that is already booked is refused when Approve is pressed", "dialogue", "en", [
        say("book Rakesh Verma tomorrow at 10 am", call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW, time="10:00"),
            kind="card", patient="rakesh", note_contains="already booked"),
        approve(approved=False),
    ], final_db=[{"appointment": {"patient": "rakesh", "date": TOMORROW, "time": "10:00", "count": 0}}]),

    conv("memory_forgets_after_idle", "After 11 idle minutes the remembered patient is gone: 'book him' must ask", "dialogue", "en", [
        say("show me the details of patient Rakesh Verma", call("query", entity="patients", aggregate="list", patient_name="Rakesh Verma"),
            kind="read", remembered_after="rakesh"),
        idle(11),
        say("book him tomorrow at 5", call("clarify", question="Which patient?"),
            kind="ask", ask_kind="clarify", remembered_after=None),
    ]),

    conv("answering_with_a_new_command", "A real command while a question is open replaces the question", "dialogue", "en", [
        say("move Manju to Monday", call("reschedule_appointment", patient_name="Manju", new_date=MONDAY), kind="ask", ask_kind="time", pending_after="time"),
        say("cancel Sunita Devi's appointment", call("cancel_appointment", patient_name="Sunita Devi"),
            kind="card", intent="cancel_appointment", patient="sunita", pending_after=None),
    ]),

]
