"""Labelled staff voice commands for choosing and regression-testing the
intent router. Each case is (transcript, expected_intent); expected None means
the router must NOT pick any action ("unclear").

Used by scripts/eval_intent.py (live, against Ollama, to pick the smallest model
that passes) and by tests/test_intent_eval_rules.py (offline, for the rules
backup). English, Hindi (Devanagari) and Hinglish are all represented, and the
"misroute" group holds phrasings that used to be mistaken for a write.
"""

CASES = [
    # --- the original misroute: reads about appointments mistaken for booking
    ("Can you get all the appointments for tomorrow?", "list_appointments"),
    ("fetch all the appointments for tomorrow", "list_appointments"),
    ("show me tomorrow's appointments", "list_appointments"),
    ("what appointments do we have on Monday", "list_appointments"),
    ("list all appointments for this week", "list_appointments"),
    ("pull up the appointment list for today", "list_appointments"),
    ("who all are coming in tomorrow", "list_appointments"),
    ("kal ke saare appointments dikhao", "list_appointments"),
    ("aaj kitne appointments hain", "list_appointments"),
    ("आज की अपॉइंटमेंट लिस्ट दिखाओ", "list_appointments"),
    ("what's scheduled today", "list_appointments"),
    ("this week's schedule", "list_appointments"),
    # --- booking
    ("book an appointment for Ramesh tomorrow at 5 pm", "book_appointment"),
    ("Sunita Devi ke liye kal subah 10 baje appointment book karo", "book_appointment"),
    ("fix an appointment for Mohan on Friday at 11", "book_appointment"),
    ("अपॉइंटमेंट बुक करो रीना शर्मा के लिए कल शाम 6 बजे", "book_appointment"),
    ("I need an appointment for my father on 15 October", "book_appointment"),
    ("appointment chahiye Anil Kumar ke liye parso", "book_appointment"),
    ("schedule appointment for Priya at 4:30 pm", "book_appointment"),
    # --- cancel / reschedule
    ("cancel Ramesh's appointment", "cancel_appointment"),
    ("Sunita ka appointment cancel kar do", "cancel_appointment"),
    ("रीना की अपॉइंटमेंट कैंसिल करो", "cancel_appointment"),
    ("cancel the 5 pm appointment tomorrow", "cancel_appointment"),
    ("reschedule Mohan's appointment to Friday", "reschedule_appointment"),
    ("Anil ka appointment kal se parso kar do", "reschedule_appointment"),
    ("move Priya's appointment to 6 pm", "reschedule_appointment"),
    ("postpone Rakesh's appointment by a day", "reschedule_appointment"),
    # --- availability / next appointment
    ("are there any free slots tomorrow", "check_availability"),
    ("kal kaunse slot khaali hain", "check_availability"),
    ("is 5 pm available on Saturday", "check_availability"),
    ("when is Ramesh's next appointment", "next_appointment"),
    ("Sunita ka agla appointment kab hai", "next_appointment"),
    # --- patients
    ("register a new patient Ravi Kumar phone 9876543210 age 42", "register_patient"),
    ("naya patient Geeta Sharma 9123456780 umar 35", "register_patient"),
    ("add patient Arjun Mehta 9988776655 age 28", "register_patient"),
    ("what is Rakesh Verma's phone number", "patient_lookup"),
    ("retrieve the patient details for Rakesh Verma", "patient_lookup"),
    ("Sunita Devi ka number batao", "patient_lookup"),
    ("find the patient Anita Rao", "patient_lookup"),
    # --- visits / fees
    ("Ramesh came in today fee 500 rupees", "record_visit"),
    ("record visit for Sunita fee 300", "record_visit"),
    ("Mohan ki visit log karo fees 400", "record_visit"),
    # --- follow-ups
    ("set a follow-up for Priya in 7 days", "set_followup"),
    ("Anil ko 10 din baad follow up par bulao", "set_followup"),
    ("cancel Ramesh's follow-up", "cancel_followup"),
    ("move Sunita's follow-up to 5 days from now", "reschedule_followup"),
    ("who has missed their follow-up", "missed_followups"),
    ("which patients are overdue for a follow-up", "missed_followups"),
    ("kiska follow up chhoot gaya", "missed_followups"),
    # --- staff and money
    ("register a new staff member Seema as a nurse", "register_staff"),
    ("naya staff Rahul compounder", "register_staff"),
    ("mark Seema present today", "log_attendance"),
    ("Rahul aaj absent hai", "log_attendance"),
    ("Seema is on leave today", "log_attendance"),
    ("log an expense of 1200 rupees for electricity", "log_expense"),
    ("bijli ka kharcha 1500 rupaye", "log_expense"),
    ("what is today's cash book", "day_end_cashbook"),
    ("aaj ka hisaab batao", "day_end_cashbook"),
    ("how much did we collect today", "day_end_cashbook"),
    # --- queue
    ("check in token 5", "queue_check_in"),
    ("Ramesh has arrived", "queue_check_in"),
    ("Sunita aa gayi hai check in kar do", "queue_check_in"),
    ("call the next patient", "queue_call_next"),
    ("agla patient bhejo", "queue_call_next"),
    ("send in token 7", "queue_call_next"),
    ("token 4 is done", "queue_mark_done"),
    ("Mohan ka consultation ho gaya", "queue_mark_done"),
    ("mark token 6 as no show", "queue_mark_no_show"),
    ("Priya didn't turn up", "queue_mark_no_show"),
    ("who is with the doctor right now", "queue_status"),
    ("how many patients are waiting", "queue_status"),
    ("queue ka status batao", "queue_status"),
    # --- calendar navigation
    ("open the calendar", "open_calendar"),
    ("show me the month view", "open_calendar"),
    ("calendar kholo", "open_calendar"),
    # --- must NOT act
    ("hello", None),
    ("what's the weather today", None),
    ("play some music", None),
    ("thank you", None),
    ("tell me a joke", None),
    ("mmm okay", None),
    ("kya haal hai", None),
]
