"""Reads: look-ups and lists answered from the whitelist; they never write and never need Approve."""
from tests.replay_cases.dsl import NO_CALL, call, conv, say
from tests.replay_cases.fixtures import TOMORROW

CASES = [
    conv("list_tomorrows_appointments", "Tomorrow's list is remembered for 'the second one'", "reads", "en", [
        say("who is booked tomorrow", call("query", entity="appointments", aggregate="list", date=TOMORROW),
            kind="read", intent="list_appointments", rows=3, list_after=3),
    ]),

    conv("free_slots_tomorrow", "Free slots", "reads", "en", [
        say("what slots are free tomorrow", call("query", entity="availability", date=TOMORROW), kind="read", intent="check_availability"),
    ]),

    conv("next_appointment_read", "A patient's next appointment", "reads", "en", [
        say("when is Manju's next appointment", call("query", entity="appointments", patient_name="Manju", limit=1),
            kind="read", intent="next_appointment", remembered_after="manju"),
    ]),

    conv("unanswerable_question_is_saved", "A question no record type can answer is saved for a person, not guessed", "reads", "en", [
        say("what is our profit this month", call("unsupported", reason="not available", wanted="profit this month"),
            kind="note", note_contains="can't answer that yet"),
    ], final_db=[{"no_writes": True}]),

    conv("open_the_calendar", "Opening the calendar is navigation, nothing else", "reads", "en", [
        say("open the calendar", call("open_calendar", view="week"), kind="navigate", intent="open_calendar"),
    ], final_db=[{"no_writes": True}]),
]
