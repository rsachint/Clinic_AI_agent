"""The Settings switch between Classic and New (model first), flipped in the middle of a conversation."""
from tests.replay_cases.dsl import NO_CALL, approve, architecture, call, conv, say
from tests.replay_cases.fixtures import MONDAY, TOMORROW

CASES = [
    conv("flip_back_and_forth_mid_conversation", "The setting is read at every command: the next sentence uses the other path", "architecture", "en", [
        say("move Manju's appointment", call("reschedule_appointment", patient_name="Manju"),
            kind="ask", ask_kind="date", route_contains="planner", pending_after="date"),
        architecture("classic"),
        say("Monday", NO_CALL, kind="ask", ask_kind="time", route_contains="rules", slots={"appt_date": MONDAY}),
        architecture("model_first"),
        say("12 pm", call("answer_slot", slot="time", value="12:00"),
            kind="card", intent="reschedule_appointment", patient="manju", route_contains="planner",
            slots={"appt_date": MONDAY, "start_time": "12:00"}),
    ], configs=["model_first_scripted"]),
]
