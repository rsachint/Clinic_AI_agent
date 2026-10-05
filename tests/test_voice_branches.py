"""The staff voice assistant with several branches: naming a branch in a
command, defaulting to this computer's My branch, remembering a named branch,
branch-aware reads, moving an appointment to another branch, and switching
My branch by voice. Nothing here writes: every write is still a review card.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_voice_dialog import DEFER, DialogTestCase  # noqa: E402
from tests.test_conversation_branches import add_branches  # noqa: E402

from clinic import branches, voice_branch  # noqa: E402
from clinic.pipeline import ParsedResult, PipelineError, ReadResult, SwitchBranchResult  # noqa: E402
from clinic.voice_context import AskResult, CardUpdate, Note  # noqa: E402
from clinic.voice_turns import handle_pick  # noqa: E402

A, B, C = 1, 2, 3


class BranchVoiceCase(DialogTestCase):
    def setUp(self):
        super().setUp()
        add_branches(self.conn)
        self.ctx.set_client_branch(A, A)
        self.tomorrow_iso = self.tomorrow.isoformat()

    def book_at(self, branch_id, start, patient_id=None, name=None, phone=None, day=None):
        self.conn.execute(
            "INSERT INTO appointments (patient_id, patient_name, patient_phone, appt_date, start_time, duration_minutes, "
            "status, branch_id) VALUES (?, ?, ?, ?, ?, 30, 'booked', ?)",
            (patient_id, name, phone, day or self.tomorrow_iso, start, branch_id))
        self.conn.commit()

    def clear_appointments(self):
        self.conn.execute("DELETE FROM appointments")
        self.conn.commit()


class NamingABranch(unittest.TestCase):
    def setUp(self):
        from tests.test_conversation import make_db
        self.conn = make_db()
        add_branches(self.conn)

    def named(self, text, **kw):
        m = voice_branch.find(self.conn, text, **kw)
        return m.branch["code"] if m.branch else None

    def test_english_hindi_and_hinglish_ways_of_saying_it(self):
        for text, code in (
            ("book Amit tomorrow at Branch B", "B"), ("book Amit at branch c", "C"),
            ("ब्रांच बी में अमित की अपॉइंटमेंट बुक करो", "B"), ("बी ब्रांच", "B"), ("ब्रांच सी पर", "C"),
            ("B mein kal 4 baje appointment", "B"), ("branch bee", "B"), ("bee branch", "B"),
            ("shift Amit to branch B", "B"),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.named(text), code)

    def test_the_last_branch_named_is_the_destination(self):
        self.assertEqual(self.named("move Amit from branch A to branch B at 3 pm"), "B")

    def test_the_branch_words_are_cut_out_of_the_text(self):
        self.assertEqual(voice_branch.find(self.conn, "book Amit tomorrow at Branch B at 3 pm").text,
                         "book Amit tomorrow at 3 pm")
        self.assertEqual(voice_branch.find(self.conn, "ब्रांच बी में अमित बुक करो").text, "अमित बुक करो")

    def test_ordinary_sentences_name_no_branch(self):
        for text in ("book a patient", "I want a branch", "show tomorrow's appointments", "Amit ko sector 5 mein dikhao"):
            with self.subTest(text=text):
                self.assertIsNone(self.named(text))

    def test_all_branches_and_my_branch(self):
        for text in ("show appointments for all branches", "list across branches", "सभी ब्रांच के अपॉइंटमेंट", "saari branches"):
            with self.subTest(text=text):
                self.assertTrue(voice_branch.find(self.conn, text).every)
        self.assertTrue(voice_branch.find(self.conn, "my branch ke appointments").mine)

    def test_a_lone_letter_only_counts_as_the_answer_to_which_branch(self):
        self.assertIsNone(self.named("B"))
        for reply in ("B", "बी", "bee", "branch C", "सी"):
            self.assertIsNotNone(self.named(reply, bare=True), reply)
        self.assertIsNone(self.named("tomorrow", bare=True))

    def test_switch_commands(self):
        for text in ("switch to branch C", "I am at branch B", "change my branch to b", "मैं ब्रांच सी में हूँ",
                     "branch c par switch karo", "set my branch to C"):
            with self.subTest(text=text):
                self.assertTrue(voice_branch.is_switch_command(text))
        for text in ("shift Amit to branch B", "show Branch B queue", "branch b ke saare appointments",
                     "book Amit at branch C", "switch off the lights"):
            with self.subTest(text=text):
                self.assertFalse(voice_branch.is_switch_command(text))

    def test_a_one_branch_clinic_hears_nothing_about_branches(self):
        self.conn.execute("UPDATE branches SET active = 0 WHERE id IN (2, 3)")
        self.conn.commit()
        m = voice_branch.find(self.conn, "book Amit at branch B")
        self.assertEqual((m.branch, m.text), (None, "book Amit at branch B"))
        self.assertEqual(voice_branch.default_branch(self.conn, "book_appointment", {"branch_id": 2, "all_branches": True}, None), {})


class BookingByVoice(BranchVoiceCase):
    def test_a_booking_goes_to_my_branch_when_none_is_named(self):
        self.ctx.set_client_branch(B, B)
        card = self.say("book Rakesh Verma tomorrow at 11 am")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.slots["branch_id"], B)
        self.assertIn("Branch B · Dr. Rao.", card.resolved["notes"])

    def test_a_named_branch_wins_and_is_kept_for_the_next_command(self):
        card = self.say("book Rakesh Verma tomorrow at 11 am at Branch C")
        self.assertEqual(card.slots["branch_id"], C)
        self.assertEqual(self.ctx.branch, C)
        later = self.say("what slots are free tomorrow")
        self.assertEqual(later.data["branch"], "Branch C")

    def test_my_branch_forgets_a_named_one(self):
        self.say("book Rakesh Verma tomorrow at 11 am at Branch C")
        self.say("show appointments for tomorrow in my branch")
        self.assertIsNone(self.ctx.branch)
        self.assertEqual(self.say("what slots are free tomorrow").data["branch"], "Branch A")

    def test_the_branch_words_never_reach_the_name_extractor(self):
        seen = []
        real = __import__("clinic.nlu.parser", fromlist=["parse"])._parse

        def spy(text, context=None):
            seen.append(text)
            return real(text, context)

        with patch("clinic.nlu.parser._parse", side_effect=spy):
            self.say("book Rakesh Verma tomorrow at 11 am at Branch B")
        self.assertNotIn("Branch", seen[0])

    def test_the_card_says_when_the_branch_has_no_doctor_at_that_time(self):
        card = self.say("book Rakesh Verma tomorrow at 5 pm at Branch B")        # B: 10:00-14:00
        notes = card.resolved["notes"]
        self.assertIn("Branch B has no doctor on duty at 17:00", notes[0])
        self.assertIn("10:00-14:00", notes[0])
        self.assertTrue(notes[1].startswith("Free at Branch B that day: 10:00"))

    def test_the_card_says_when_the_slot_is_taken_and_offers_others(self):
        self.book_at(B, "11:00", name="Someone", phone="9000099999")
        card = self.say("book Rakesh Verma tomorrow at 11 am at Branch B")
        self.assertIn("11:00 is already booked or blocked at Branch B.", card.resolved["notes"])
        self.assertNotIn("11:00", card.resolved["notes"][1])

    def test_the_same_time_is_free_at_another_branch(self):
        self.book_at(B, "11:00", name="Someone", phone="9000099999")
        card = self.say("book Rakesh Verma tomorrow at 11 am at Branch C")           # C: 09:00-12:00
        self.assertEqual(card.resolved["notes"], ["Branch C · Dr. Iyer."])

    def test_a_question_for_a_missing_time_keeps_the_branch(self):
        ask = self.say("book Rakesh Verma tomorrow at Branch B")
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.kind, "time")
        card = self.say("11 am")
        self.assertEqual(card.slots["branch_id"], B)
        self.assertEqual(card.slots["start_time"], "11:00")

    def test_a_one_branch_clinic_card_has_no_branch(self):
        self.conn.execute("UPDATE branches SET active = 0 WHERE id IN (2, 3)")
        self.conn.commit()
        card = self.say("book Rakesh Verma tomorrow at 11 am")
        self.assertNotIn("branch_id", card.slots)
        self.assertNotIn("notes", card.resolved)

    def test_editing_the_open_card_can_change_the_branch(self):
        card = self.say("book Rakesh Verma tomorrow at 11 am")
        self.assertEqual(card.slots["branch_id"], A)
        update = self.say("make it Branch B instead")
        self.assertIsInstance(update, CardUpdate)
        self.assertEqual(update.changes, {"branch_id": B})
        self.assertEqual(update.summary, "Branch -> Branch B")
        self.assertEqual(self.ctx.open_card["slots"]["branch_id"], B)


class ReadsByBranch(BranchVoiceCase):
    def setUp(self):
        super().setUp()
        self.clear_appointments()
        self.book_at(A, "09:00", name="Pt At A", phone="9000011111")
        self.book_at(B, "10:00", name="Pt At B", phone="9000022222")
        self.book_at(B, "10:30", name="Pt At B Two", phone="9000033333")
        self.book_at(C, "09:30", name="Pt At C", phone="9000044444")

    def names(self, result):
        return sorted(row["patient_name"] for row in result.data)

    def test_a_day_list_is_my_branchs_by_default_and_says_so(self):
        self.ctx.set_client_branch(B, B)
        result = self.say("show appointments for tomorrow")
        self.assertEqual(self.names(result), ["Pt At B", "Pt At B Two"])
        self.assertIn("(Branch B)", result.answer_text)
        self.assertIn("Branch B", result.scope_caption)

    def test_a_named_branch_overrides_it(self):
        self.assertEqual(self.names(self.say("show appointments for tomorrow at Branch C")), ["Pt At C"])

    def test_all_branches_lists_everyone(self):
        result = self.say("show appointments for tomorrow for all branches")
        self.assertEqual(len(result.data), 4)
        self.assertNotIn("(Branch", result.answer_text.split("Source")[0])
        self.assertEqual({row["branch"] for row in result.data}, {"Branch A", "Branch B", "Branch C"})

    def test_a_persons_appointments_are_found_at_any_branch(self):
        with patch("clinic.nlu.parser._spoken_name_or_none", return_value="Pt At C"):
            result = self.say("show Pt At C's appointments")
        self.assertEqual([row["patient_name"] for row in result.data], ["Pt At C"])

    def test_free_slots_are_that_branchs_only(self):
        self.ctx.set_client_branch(C, C)
        result = self.say("what slots are free tomorrow")
        self.assertEqual(result.data["branch"], "Branch C")
        self.assertTrue(all("09:00" <= t < "12:00" for t in result.data["slots"]))
        self.assertNotIn("09:30", result.data["slots"])          # taken at C
        self.assertIn("(Branch C)", result.answer_text)

    def test_free_slots_for_all_branches_is_one_row_each(self):
        result = self.say("what slots are free tomorrow across all branches")
        self.assertEqual([row["branch"] for row in result.data], ["Branch A", "Branch B", "Branch C"])
        self.assertIn("Branch B:", result.answer_text)
        self.assertTrue(all(row["free_slots"] > 0 for row in result.data))

    def test_the_queue_is_that_branchs(self):
        self.conn.execute("UPDATE appointments SET appt_date = ?", (self.today.isoformat(),))
        self.conn.commit()
        self.ctx.set_client_branch(B, B)
        result = self.say("what is the queue status")
        self.assertEqual((result.data["branch"], result.data["total_today"]), ("Branch B", 2))
        self.assertIn("(Branch B)", result.answer_text)
        every = self.say("queue status for all branches")
        self.assertEqual({row["branch"]: row["total_today"] for row in every.data},
                         {"Branch A": 1, "Branch B": 2, "Branch C": 1})

    def test_a_one_branch_clinic_reads_are_unchanged(self):
        self.conn.execute("UPDATE branches SET active = 0 WHERE id IN (2, 3)")
        self.conn.commit()
        result = self.say("show appointments for tomorrow")
        self.assertEqual(len(result.data), 4)
        self.assertNotIn("Branch", result.answer_text)


class QueueCommandsByBranch(BranchVoiceCase):
    def say(self, text, language="en-IN"):
        from clinic.voice_turns import handle_turn
        return handle_turn(self.ctx, self.conn, text, self.adapter, self.adapter, language,
                           DEFER | voice_branch.QUEUE_INTENTS)

    def test_a_token_means_the_token_at_my_branch(self):
        self.clear_appointments()
        today = self.today.isoformat()
        self.book_at(A, "09:00", name="Pt At A", phone="9000011111", day=today)
        self.book_at(B, "10:00", name="Pt At B", phone="9000022222", day=today)
        ids = {r["patient_name"]: r["id"] for r in self.conn.execute("SELECT id, patient_name FROM appointments")}
        self.ctx.set_client_branch(B, B)
        card = self.say("check in token 1")
        self.assertEqual(card.slots["appointment_id"], ids["Pt At B"])
        self.ctx.set_client_branch(A, A)
        self.ctx.clear()
        self.assertEqual(self.say("check in token 1").slots["appointment_id"], ids["Pt At A"])


class MovingBetweenBranches(BranchVoiceCase):
    def setUp(self):
        super().setUp()
        self.clear_appointments()
        self.book_at(A, "10:00", patient_id=1)            # Rakesh Verma at Branch A

    def test_move_to_another_branch_makes_a_reschedule_card_with_the_branch(self):
        card = self.say("move Rakesh Verma's appointment to Branch B tomorrow at 11 am")
        self.assertIsInstance(card, ParsedResult)
        self.assertEqual(card.intent, "reschedule_appointment")
        self.assertEqual(card.slots["branch_id"], B)
        self.assertEqual(card.slots["start_time"], "11:00")
        self.assertIn("Moving from Branch A to Branch B.", card.resolved["notes"])
        self.assertIn("Branch B · Dr. Rao.", card.resolved["notes"])

    def test_a_reschedule_without_a_branch_keeps_the_appointments_own(self):
        self.ctx.set_client_branch(C, C)                    # My branch is NOT applied to a move
        card = self.say("reschedule Rakesh Verma's appointment to tomorrow at 11 am")
        self.assertEqual(card.intent, "reschedule_appointment")
        self.assertNotIn("branch_id", card.slots)
        self.assertTrue(all("Moving" not in n for n in card.resolved["notes"]))
        self.assertTrue(any(n.startswith("Branch A") for n in card.resolved["notes"]))

    def test_a_move_to_a_time_the_other_branch_cannot_take_says_so(self):
        card = self.say("move Rakesh Verma's appointment to Branch C tomorrow at 3 pm")      # C: 09:00-12:00
        self.assertIn("Branch C has no doctor on duty at 15:00", " ".join(card.resolved["notes"]))


class SwitchingMyBranch(BranchVoiceCase):
    def test_switch_to_a_named_branch(self):
        result = self.say("switch to Branch C")
        self.assertIsInstance(result, SwitchBranchResult)
        self.assertEqual((result.branch_id, result.branch_name), (C, "Branch C"))
        self.assertEqual(self.ctx.my_branch, C)
        self.assertIn("Branch C", result.answer_text)

    def test_hindi_switch(self):
        result = self.say("मैं ब्रांच बी में हूँ", language="hi-IN")
        self.assertIsInstance(result, SwitchBranchResult)
        self.assertEqual(result.branch_id, B)
        self.assertIn("Branch B", result.answer_text)

    def test_without_a_branch_it_asks_which_and_a_short_answer_will_do(self):
        ask = self.say("switch my branch")
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.kind, "branch")
        self.assertEqual([o["label"] for o in ask.options], ["Branch A", "Branch B", "Branch C"])
        result = self.say("bee")
        self.assertIsInstance(result, SwitchBranchResult)
        self.assertEqual(result.branch_id, B)

    def test_the_options_can_be_tapped(self):
        self.say("switch my branch")
        label, result = handle_pick(self.ctx, self.conn, 2, self.adapter, self.adapter, "en-IN", DEFER)
        self.assertEqual(label, "Branch C")
        self.assertIsInstance(result, SwitchBranchResult)
        self.assertEqual(result.branch_id, C)

    def test_a_wrong_answer_is_asked_again_then_dropped(self):
        self.say("switch my branch")
        self.assertIsInstance(self.say("hmm"), AskResult)
        with self.assertRaises(PipelineError):
            self.say("hmm hmm")

    def test_a_booking_after_switching_lands_in_the_new_branch(self):
        self.say("switch to Branch B")
        card = self.say("book Rakesh Verma tomorrow at 11 am")
        self.assertEqual(card.slots["branch_id"], B)

    def test_a_one_branch_clinic_has_nothing_to_switch(self):
        self.conn.execute("UPDATE branches SET active = 0 WHERE id IN (2, 3)")
        self.conn.commit()
        result = self.say("switch to Branch B")
        self.assertIsInstance(result, Note)
        self.assertIn("only one branch", result.message)


class MoveWordsRouting(unittest.TestCase):
    """"move / shift ... appointment" is a reschedule by rule, so the model's
    habit of reading it as a booking (or attendance) cannot misroute it."""

    def test_explicit_move_phrases(self):
        from clinic.nlu.classify import classify, is_move_command
        for text in ("move Amit's appointment tomorrow at 11 am", "Amit ko shift karo kal 11 baje",
                     "transfer Rakesh Verma's appointment to 3 pm", "अमित की अपॉइंटमेंट शिफ्ट करो कल 11 बजे"):
            with self.subTest(text=text):
                self.assertEqual(classify(text), "reschedule_appointment")
                self.assertTrue(is_move_command(text))

    def test_other_sentences_are_not_moves(self):
        from clinic.nlu.classify import is_move_command
        for text in ("remove the appointment", "book Amit tomorrow at 11 am", "cancel Amit's appointment",
                     "move Asha's follow-up to 3 days from now", "show tomorrow's appointments"):
            with self.subTest(text=text):
                self.assertFalse(is_move_command(text))

    def test_the_parser_skips_the_model_for_a_move(self):
        from clinic.nlu.parser import _parse
        with patch("clinic.nlu.parser.pick_intent", side_effect=AssertionError("model asked")), \
                patch("clinic.nlu.parser.extract_name", return_value="Amit"):
            intent, slots = _parse("Amit ko shift karo kal 11 baje")
        self.assertEqual(intent, "reschedule_appointment")


class VoiceSessionWiring(unittest.TestCase):
    def setUp(self):
        import os
        os.environ.setdefault("SARVAM_API_KEY", "test-not-real")
        from tests.test_conversation import make_db
        from clinic.adapters.local_sqlite import LocalSQLiteAdapter
        from clinic.realtime_voice import VoiceSession
        self.conn = make_db()
        add_branches(self.conn)
        self.emitted = []
        adapter = LocalSQLiteAdapter()
        self.session = VoiceSession("sid", "key", lambda e, d: self.emitted.append((e, d)), lambda: self.conn,
                                    adapter, adapter, DEFER)

    def events(self, name):
        return [d for e, d in self.emitted if e == name]

    def test_the_page_tells_the_session_its_branch_and_bad_values_are_ignored(self):
        self.session.set_branch(B, "all")
        self.assertEqual((self.session.context.my_branch, self.session.context.view_branch), (B, "all"))
        self.session.set_branch(999, "nope")
        self.assertEqual((self.session.context.my_branch, self.session.context.view_branch), (None, None))
        self.session.set_branch("2", None)                      # a string id is not trusted either
        self.assertIsNone(self.session.context.my_branch)

    def test_a_switch_command_is_sent_to_the_page(self):
        with patch("clinic.nlu.parser.pick_intent", return_value=None):
            self.session._handle_final_transcript("switch to Branch C")
        event = self.events("switch_branch")[-1]
        self.assertEqual((event["branch_id"], event["branch_name"]), (C, "Branch C"))
        self.assertEqual(self.session.context.my_branch, C)


class PageWiring(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_the_page_syncs_its_branch_and_handles_a_switch(self):
        js = (self.ROOT / "static" / "live_voice.js").read_text()
        self.assertIn('socket.emit("set_branch"', js)
        self.assertIn('socket.on("switch_branch"', js)
        self.assertIn('document.addEventListener("branchchange", syncBranch)', js)

    def test_the_move_card_can_keep_or_change_the_branch(self):
        card = (self.ROOT / "static" / "review_card.js").read_text()
        self.assertIn('keep: true', card)
        self.assertIn("Same branch as now", card)


if __name__ == "__main__":
    unittest.main()
