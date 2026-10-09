"""Who is meant: exact name and phone matching, new patients, walk-ins, staff and the other write cards."""
from tests.replay_cases.dsl import NO_CALL, approve, call, conv, pick, say
from tests.replay_cases.fixtures import MONDAY, TOMORROW, with_setup

WALKIN = {"key": "ramesh_walkin", "name": "Ramesh Gupta", "phone": "9899899899", "date": TOMORROW, "time": "15:00", "branch": "A"}

CASES = [
    conv("exact_phone_beats_an_ambiguous_name", "A whole phone number picks one of two same-named patients", "entities", "en", [
        say("book the patient with phone 9876546543 tomorrow at 5 pm",
            call("book_appointment", patient_name="Rahul Sharma", date=TOMORROW, time="17:00", phone="9876546543"),
            kind="card", intent="book_appointment", patient="rahul_en", slots={"appt_date": TOMORROW, "start_time": "17:00"}),
    ]),

    conv("phone_belongs_to_someone_else", "A phone number that is another patient's is flagged on the card", "entities", "en", [
        say("book Rakesh Verma tomorrow at 5 pm, phone 9000000002",
            call("book_appointment", patient_name="Rakesh Verma", date=TOMORROW, time="17:00", phone="9000000002"),
            kind="card", intent="book_appointment", note_contains="belongs to Mohan Lal"),
    ]),

    conv("new_patient_with_phone_is_registered_at_approve", "An unknown name with a phone is registered by the booking, once", "entities", "en", [
        say("book a new patient Kavita Rao tomorrow at 5 pm, phone 9988776655",
            call("book_appointment", patient_name="Kavita Rao", date=TOMORROW, time="17:00", phone="9988776655"),
            kind="card", intent="book_appointment", patient=None, slots={"patient_name": "Kavita Rao", "patient_phone": "9988776655"}),
        approve(approved=True),
    ], final_db=[{"patient": {"name": "Kavita Rao", "count": 1}}, {"appointment": {"name": "Kavita Rao", "date": TOMORROW, "time": "17:00"}},
                 {"no_duplicate_patients": True}]),

    conv("short_phone_is_asked_again", "A phone number with too few digits is never guessed at", "entities", "en", [
        say("book Kavita Rao tomorrow at 5 pm", call("book_appointment", patient_name="Kavita Rao", date=TOMORROW, time="17:00"),
            kind="ask", ask_kind="phone"),
        say("98765", call("answer_slot", slot="phone", value="98765"), kind="ask", ask_kind="phone", pending_after="phone"),
        say("9988776655", call("answer_slot", slot="phone", value="9988776655"), kind="card", slots={"patient_phone": "9988776655"}),
    ]),

    conv("two_mohans_choose_by_a_name_word", "Mohan Lal and Mohan Das: 'Mohan Das' picks the right one", "entities", "en", [
        say("book Mohan tomorrow at 5 pm", call("book_appointment", patient_name="Mohan", date=TOMORROW, time="17:00"),
            kind="ask", ask_kind="choose_patient", options_count=2),
        say("Mohan Das", call("answer_slot", slot="patient_name", value="Mohan Das"),
            kind="card", intent="book_appointment", patient="mohan_das"),
    ]),

    conv("devanagari_name_matches_both_rahuls", "A Devanagari name that two stored names equal is a question, not a guess", "entities", "hi", [
        say("राहुल शर्मा को कल शाम 5 बजे बुक करो", call("book_appointment", patient_name="राहुल शर्मा", date=TOMORROW, time="17:00"),
            kind="ask", ask_kind="choose_patient", options_count=2),
    ]),

    conv("both_amits_appointments_listed", "'Amit's appointments' lists both Amits", "entities", "en", [
        say("show Amit's appointments", call("query", entity="appointments", aggregate="list", patient_name="Amit"),
            kind="read", intent="list_appointments", rows=2),
    ]),

    conv("walk_in_without_a_patient_record_is_cancelled", "A walk-in who has no patient record can still be cancelled", "entities", "en", [
        say("cancel Ramesh Gupta's appointment", call("cancel_appointment", patient_name="Ramesh Gupta"),
            kind="card", intent="cancel_appointment", rows=1),
        approve(approved=True),
    ], setup=with_setup(appointments=[WALKIN]), final_db=[{"appointment": {"name": "Ramesh Gupta", "status": "cancelled"}}]),

    conv("register_a_new_patient", "Registering a patient is a card, and exactly one patient is added at Approve", "entities", "en", [
        say("register a new patient Kavita Rao age 33 phone 9988776655",
            call("register_patient", name="Kavita Rao", age=33, phone="9988776655"),
            kind="card", intent="register_patient", slots={"name": "Kavita Rao", "phone": "9988776655", "age": 33}),
        approve(approved=True),
    ], final_db=[{"patient": {"name": "Kavita Rao", "count": 1}}]),

    conv("record_a_visit_card", "A visit and its fee is a card", "entities", "en", [
        say("Rakesh Verma came in today, fee 300 rupees", call("record_visit", patient_name="Rakesh Verma", fee=300),
            kind="card", intent="record_visit", patient="rakesh", slots={"fee_rupees": 300}),
        say("make the fee 400", call("correct_card", field="fee", value="400"), kind="card_update", slots={"fee_rupees": 400}),
    ]),

]
