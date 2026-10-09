"""The state card sent to the planner in model-first mode (clinic/state_card.py): what it says in the multi-turn
scenarios (Priya, the two Rahuls, Nalin), what it must NEVER say (ids, notes, diagnoses, message bodies, full phone
numbers), its size cap, and that each section can be dropped for the replay's ablation."""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic import db, state_card  # noqa: E402
from clinic.voice_context import VoiceContext  # noqa: E402

TODAY = date(2026, 10, 9)                 # a Friday; the card never reads the real date when one is given
SECRETS = ("SECRET-NOTE-7731", "SECRET-DIAGNOSIS-5520", "SECRET-MESSAGE-9904", "SECRET-TOKEN-1187")
ID_MARKERS = ("424242", "987654", "555111")


class CardCase(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.ctx = VoiceContext()
        self.ctx.set_client_branch(1, 1)
        for name, phone in (("Rahul Sharma", "9876543210"), ("Rahul Sharma", "9876546543"), ("Priya Shah", "9123499999")):
            self.conn.execute("INSERT INTO patients (name, phone, age) VALUES (?, ?, 40)", (name, phone))
        self.conn.commit()

    def card(self, **kw):
        return state_card.build(self.ctx, self.conn, TODAY, **kw)


class Scenarios(CardCase):
    def test_nothing_open_says_so_and_still_gives_the_day(self):
        text = self.card()
        self.assertIn("Today: Friday 2026-10-09.", text)
        self.assertIn("Nothing is open", text)
        self.assertLess(len(text), 400)

    def test_which_one_lists_numbered_options_by_name_and_last_four_digits(self):
        options = [{"label": "Rahul Sharma (9876543210)", "patient_name": "Rahul Sharma", "patient_id": 424242},
                   {"label": "Rahul Sharma (9876546543)", "patient_name": "Rahul Sharma", "patient_id": 555111}]
        self.ctx.ask("book_appointment", {"patient_name": "Rahul Sharma", "appt_date": "2026-10-10", "start_time": "17:00"},
                     "choose_patient", options)
        text = self.card()
        self.assertIn('Waiting for the user\'s answer to: "Which one?" (choose_patient)', text)
        self.assertIn("  1. Rahul Sharma ...3210", text)
        self.assertIn("  2. Rahul Sharma ...6543", text)
        self.assertIn("Task in progress: book_appointment. Heard: patient=Rahul Sharma, date=2026-10-10, time=17:00.", text)
        self.assertNotIn("Missing:", text)
        for hidden in ("9876543210", "9876546543", "424242", "555111"):
            self.assertNotIn(hidden, text)

    def test_the_nalin_question_shows_what_the_task_has_heard_and_what_is_missing(self):
        self.ctx.ask("book_appointment", {"patient_name": "Rahul", "appt_date": "2026-10-10", "start_time": "17:00"},
                     "choose_patient", [{"label": "Rahul Sharma (9876543210)", "patient_name": "Rahul Sharma"}])
        self.ctx.ask("book_appointment", {"patient_name": "Nalin", "appt_date": "2026-10-10", "start_time": "17:00"}, "phone")
        text = self.card()
        self.assertIn("Heard: patient=Nalin, date=2026-10-10, time=17:00.", text)
        self.assertIn("Missing: phone.", text)
        self.assertIn("(phone)", text)

    def test_priya_the_remembered_patient_is_labelled_as_a_fallback_and_carries_the_last_four_only(self):
        self.ctx.remember_patient(3, "Priya Shah")
        text = self.card()
        self.assertIn("Remembered patient", text)
        self.assertIn("Priya Shah ...9999", text)
        self.assertIn("names nobody else", text)
        self.assertNotIn("9123499999", text)

    def test_the_card_on_screen_shows_its_visible_fields(self):
        self.ctx.open_card_for("c1", "book_appointment", {"patient_name": "Kavita Rao", "patient_phone": "9988776655",
                                                          "appt_date": "2026-10-10", "start_time": "17:00", "branch_id": 1,
                                                          "patient_id": 424242, "appointment_id": 987654})
        text = self.card()
        self.assertIn("Card on screen: book_appointment (patient=Kavita Rao, phone heard ...6655, date=2026-10-10, time=17:00, branch=A)", text)
        self.assertIn("Nothing is saved until a person presses Approve", text)
        self.assertNotIn("9988776655", text)
        for marker in ID_MARKERS:
            self.assertNotIn(marker, text)

    def test_the_list_on_screen_is_numbered_with_name_date_and_time(self):
        self.ctx.remember_list([{"id": 987654, "patient_name": "Sunita Devi", "appt_date": "2026-10-10", "start_time": "10:00"},
                                {"id": 555111, "who": "Rakesh Verma", "appt_date": "2026-10-10", "start_time": "11:00",
                                 "patient_phone": "9000000001"}], "Sat 10 Oct")
        text = self.card()
        self.assertIn("List on screen (Sat 10 Oct):", text)
        self.assertIn("  1. Sunita Devi, 2026-10-10 10:00", text)
        self.assertIn("  2. Rakesh Verma, 2026-10-10 11:00", text)
        self.assertNotIn("9000000001", text)
        for marker in ID_MARKERS:
            self.assertNotIn(marker, text)

    def test_the_last_turns_are_short_and_phone_numbers_in_them_are_masked(self):
        self.ctx.remember_turn("book Kavita, phone 9988776655", "book_appointment(patient_name=Kavita, phone=9988776655)",
                               "asked the user: What is the patient's phone number? Source: x")
        self.ctx.remember_turn("98765 00301", "answer_slot(slot=phone, value=9876500301)", "showed a review card for book_appointment")
        text = self.card()
        self.assertIn("Recent turns (oldest first):", text)
        self.assertEqual(text.count("user said"), 2)
        for full in ("9988776655", "9876500301", "98765 00301", "Source:"):
            self.assertNotIn(full, text)
        self.assertIn("...6655", text)

    def test_at_most_three_turns_are_kept(self):
        for n in range(6):
            self.ctx.remember_turn("sentence number {}".format(n), "query()", "answer {}".format(n))
        text = self.card()
        self.assertEqual(text.count("user said"), 3)
        self.assertIn("sentence number 5", text)
        self.assertNotIn("sentence number 2", text)

    def test_a_clarifying_question_says_to_answer_with_the_whole_command(self):
        self.ctx.ask("clarify", {"question": "Which day and time?", "text": "book Rakesh Verma"}, "clarify")
        text = self.card()
        self.assertIn('Waiting for the user\'s answer to your question: "Which day and time?" (they first said: "book Rakesh Verma")', text)
        self.assertIn("WHOLE command", text)
        self.assertNotIn("Task in progress", text)

    def test_branches_are_shown_only_when_there_are_several(self):
        self.assertNotIn("Branch:", self.card())
        self.conn.execute("INSERT INTO branches (code, name) VALUES ('B', 'Branch B')")
        self.conn.commit()
        text = self.card()
        self.assertIn("Branch: A (this computer's branch).", text)
        self.ctx.remember_branch(2)
        self.assertIn("the user last named branch B", self.card())


class Privacy(CardCase):
    def plant(self):
        """Secrets in every place the card must not read, and ids everywhere the model must not see."""
        self.conn.execute("INSERT INTO appointments (patient_id, appt_date, start_time, status, notes) "
                          "VALUES (1, '2026-10-10', '10:00', 'booked', ?)", (SECRETS[0],))
        self.conn.execute("INSERT INTO followups (patient_id, due_date, status, diagnosis) VALUES (1, '2026-10-20', 'pending', ?)",
                          (SECRETS[1],))
        self.conn.execute("INSERT INTO wa_messages (wa_message_id, wa_id, message_type, raw_text) VALUES ('m1', '919876543210', 'text', ?)",
                          (SECRETS[2],))
        self.conn.execute("INSERT INTO app_settings (key, value) VALUES ('whatsapp_token', ?)", (SECRETS[3],))
        self.conn.commit()
        self.ctx.remember_patient(424242, "Rahul Sharma")
        self.ctx.ask("book_appointment", {"patient_name": "Rahul Sharma", "patient_id": 424242, "appointment_id": 987654,
                                          "notes": SECRETS[0], "diagnosis": SECRETS[1], "wa_id": "919876543210",
                                          "appt_date": "2026-10-10"},
                     "choose_patient", [{"label": "Rahul Sharma (9876543210)", "patient_name": "Rahul Sharma",
                                         "patient_id": 424242, "wa_id": "919876543210"}])
        self.ctx.open_card_for("c1", "book_appointment", {"patient_name": "Rahul Sharma", "patient_id": 424242, "notes": SECRETS[0],
                                                           "diagnosis": SECRETS[1], "appointment_id": 987654})
        self.ctx.remember_list([{"id": 987654, "patient_name": "Rahul Sharma", "appt_date": "2026-10-10", "start_time": "10:00",
                                 "notes": SECRETS[0], "patient_phone": "9876543210", "diagnosis": SECRETS[1]}], "Sat")
        self.ctx.remember_turn("hi", "query()", "answer")

    def test_no_secret_no_id_and_no_full_phone_number_reaches_the_model(self):
        self.plant()
        text = self.card()
        for secret in SECRETS + ID_MARKERS + ("9876543210", "919876543210", "wa_id", "patient_id", "appointment_id", "token"):
            self.assertNotIn(secret, text, secret)
        self.assertIn("...3210", text)             # but the last four digits are there, to tell two Rahuls apart

    def test_it_holds_for_every_combination_of_dropped_sections(self):
        self.plant()
        import itertools
        for count in range(len(state_card.SECTIONS) + 1):
            for dropped in itertools.combinations(state_card.SECTIONS, count):
                text = self.card(drop=dropped)
                for secret in SECRETS + ID_MARKERS + ("9876543210",):
                    self.assertNotIn(secret, text, (secret, dropped))

    def test_only_a_whitelist_of_task_slots_is_shown(self):
        self.ctx.ask("book_appointment", {"patient_name": "Kavita", "notes": SECRETS[0], "diagnosis": SECRETS[1],
                                          "internal_flag": "xyz-internal", "appt_date": "2026-10-10"}, "time")
        text = self.card()
        for hidden in (SECRETS[0], SECRETS[1], "xyz-internal"):
            self.assertNotIn(hidden, text)
        self.assertIn("patient=Kavita", text)


class Size(CardCase):
    def fill(self, n):
        self.ctx.remember_list([{"id": i, "patient_name": "Patient Number {} With A Long Name".format(i), "appt_date": "2026-10-10",
                                 "start_time": "10:00"} for i in range(n)], "a very long list " * 8)
        self.ctx.ask("cancel_appointment", {"patient_name": "Rahul"}, "choose_patient",
                     [{"label": "Rahul Sharma ({})".format(9000000000 + i), "patient_name": "Rahul Sharma"} for i in range(40)])
        for i in range(3):
            self.ctx.remember_turn("a rather long sentence " * 30, "query(entity=appointments)", "a long answer " * 60)

    def test_a_huge_state_is_cut_to_the_size_limit_and_says_so(self):
        self.fill(200)
        text = self.card()
        self.assertLessEqual(len(text), state_card.MAX_CHARS)
        self.assertLessEqual(state_card.estimate_tokens(text), 600)
        self.assertIn("more", text)                                   # "(+N more rows not shown)" or the options note

    def test_lists_are_truncated_with_a_count_of_what_is_left_out(self):
        self.fill(30)
        text = self.card(max_chars=6000)
        self.assertIn("(+22 more rows not shown)", text)
        self.assertIn("(+32 more options not shown)", text)

    def test_the_normal_card_is_small(self):
        self.ctx.remember_patient(1, "Rahul Sharma")
        self.assertLess(state_card.estimate_tokens(self.card()), 200)


class Sections(CardCase):
    def fill(self):
        self.ctx.remember_patient(3, "Priya Shah")
        self.ctx.ask("book_appointment", {"patient_name": "Rahul", "appt_date": "2026-10-10"}, "choose_patient",
                     [{"label": "Rahul Sharma (9876543210)", "patient_name": "Rahul Sharma"}])
        self.ctx.open_card_for("c1", "reschedule_appointment", {"patient_name": "Priya Shah", "appt_date": "2026-10-12"})
        self.ctx.remember_list([{"id": 1, "patient_name": "Sunita Devi", "appt_date": "2026-10-10", "start_time": "10:00"}], "Sat")
        self.ctx.remember_turn("hello there", "query()", "an answer")

    MARKER = {"task": "Task in progress", "pending": "Waiting for the user's answer", "card": "Card on screen",
              "remembered": "Remembered patient", "list": "List on screen", "last_turns": "Recent turns"}

    def test_every_section_is_present_by_default(self):
        self.fill()
        text = self.card()
        for section, marker in self.MARKER.items():
            self.assertIn(marker, text, section)

    def test_dropping_one_section_removes_only_that_one(self):
        self.fill()
        for dropped, marker in self.MARKER.items():
            with self.subTest(dropped=dropped):
                text = self.card(drop=[dropped])
                self.assertNotIn(marker, text)
                for other, other_marker in self.MARKER.items():
                    if other != dropped:
                        self.assertIn(other_marker, text)
                self.assertIn("Today: Friday", text)                  # the date is never dropped

    def test_dropping_everything_leaves_the_date_and_a_plain_statement(self):
        self.fill()
        text = self.card(drop=state_card.SECTIONS)
        self.assertIn("Today: Friday 2026-10-09.", text)
        self.assertIn("Nothing is open", text)
        for marker in self.MARKER.values():
            self.assertNotIn(marker, text)

    def test_an_unknown_section_is_refused(self):
        with self.assertRaises(ValueError):
            self.card(drop=["pending", "everything"])

    def test_the_run_wide_ablation_switch_is_honoured_and_restored(self):
        self.fill()
        token = state_card.dropped.set(frozenset({"remembered", "list"}))
        try:
            text = self.card()
        finally:
            state_card.dropped.reset(token)
        self.assertNotIn("Remembered patient", text)
        self.assertNotIn("List on screen", text)
        self.assertIn("Card on screen", text)
        self.assertIn("Remembered patient", self.card())              # back to normal afterwards

    def test_an_explicit_drop_wins_over_the_run_switch(self):
        self.fill()
        token = state_card.dropped.set(frozenset({"card"}))
        try:
            text = self.card(drop=[])
        finally:
            state_card.dropped.reset(token)
        self.assertIn("Card on screen", text)

    def test_the_helpers(self):
        self.assertEqual(state_card.tail4("98765 43210"), "...3210")
        self.assertEqual(state_card.tail4("12"), "")
        self.assertEqual(state_card.person_label("Rahul Sharma (+91 98765 43210)"), "Rahul Sharma ...3210")
        self.assertEqual(state_card.person_label("Branch A"), "Branch A")
        self.assertEqual(state_card.mask_phones("call 9876543210 or 98765 00301 at 17:00 on 2026-10-10"),
                         "call ...3210 or ...0301 at 17:00 on 2026-10-10")
        self.assertEqual(state_card.mask_phones("from 2026-10-10 2026-10-12 and 98765 43210"), "from 2026-10-10 2026-10-12 and ...3210")


if __name__ == "__main__":
    unittest.main()
