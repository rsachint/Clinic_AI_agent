"""The same jobs said in Hindi (Devanagari), Hinglish and mixed speech."""
from tests.replay_cases.dsl import NO_CALL, approve, call, conv, say
from tests.replay_cases.fixtures import MONDAY, TOMORROW

CASES = [
    conv("hinglish_booking", "Hinglish booking sentence", "multilingual", "hinglish", [
        say("Rakesh Verma ke liye kal shaam 5 baje appointment book karo",
            call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW, time="17:00"),
            kind="card", intent="book_appointment", patient="rakesh", slots={"appt_date": TOMORROW, "start_time": "17:00"}),
    ]),

    conv("devanagari_booking", "Devanagari booking sentence, name in Devanagari", "multilingual", "hi", [
        say("सुनीता देवी का अपॉइंटमेंट कल शाम 5 बजे बुक करो",
            call("book_appointment", patient_name="सुनीता देवी", date=TOMORROW, time="17:00"),
            kind="card", intent="book_appointment", patient="sunita", slots={"appt_date": TOMORROW, "start_time": "17:00"}),
    ]),

    conv("devanagari_cancel", "Devanagari cancel", "multilingual", "hi", [
        say("सुनीता देवी की अपॉइंटमेंट रद्द करो", call("cancel_appointment", patient_name="सुनीता देवी"),
            kind="card", intent="cancel_appointment", patient="sunita"),
    ]),

    conv("hinglish_move_asks_time", "Hinglish move, then the time answered in Hinglish", "multilingual", "hinglish", [
        say("Manju ka appointment kal ke liye shift karo", call("reschedule_appointment", patient_name="Manju", new_date=TOMORROW),
            kind="ask", ask_kind="time"),
        say("shaam 5 baje", call("answer_slot", slot="time", value="17:00"),
            kind="card", intent="reschedule_appointment", patient="manju", slots={"appt_date": TOMORROW, "start_time": "17:00"}),
    ]),

    conv("hinglish_phone_digits_answer", "A phone number answered in Hinglish digit words", "multilingual", "hinglish", [
        say("Kavita Rao ko kal shaam 5 baje book karo", call("book_appointment", patient_name="Kavita Rao", date=TOMORROW, time="17:00"),
            kind="ask", ask_kind="phone"),
        say("nau ek ek ek do do teen teen chaar chaar", call("answer_slot", slot="phone", value="9111223344"),
            kind="card", slots={"patient_phone": "9111223344"}),
    ]),

    conv("hinglish_second_one_from_a_list", "A Hinglish 'the second one' after a list", "multilingual", "hinglish", [
        say("kal ke appointments dikhao", call("query", entity="appointments", aggregate="list", date=TOMORROW),
            kind="read", intent="list_appointments", list_after=3),
        say("doosra wala cancel karo", call("cancel_appointment", patient_name="Rakesh Verma"),
            kind="card", intent="cancel_appointment", patient="rakesh"),
    ]),

    conv("devanagari_branch_switch", "Switching branch in Devanagari", "multilingual", "hi", [
        say("मैं ब्रांच सी में हूँ", call("switch_branch", branch="C"), kind="switch_branch", intent="set_my_branch",
            slots={"branch": "C"}),
    ]),

]
