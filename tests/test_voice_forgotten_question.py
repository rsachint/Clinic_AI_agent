"""A bare answer ("8th of October") arriving after the question it answers is gone must get a clear message,
not "Could not classify this into a known command."."""

import unittest

from clinic.pipeline import NOT_CLASSIFIED, PipelineError, ReadResult
from clinic.voice_context import AskResult
from clinic.voice_turns import FORGOTTEN_REPLIES
from tests import test_voice_dialog as dialog

GARBLED = "Book a follow up appointment for the patient Rakesh Verma on aathth of October at 11 am"


class ForgottenQuestionTests(dialog.DialogTestCase):
    def ask_which_day(self):
        ask = self.say(GARBLED)
        self.assertIsInstance(ask, AskResult)
        self.assertEqual(ask.question, "Which day?")

    def refused(self, text, language="en-IN"):
        with self.assertRaises(PipelineError) as caught:
            self.say(text, language)
        return str(caught.exception)

    def test_an_answer_inside_ten_minutes_is_still_taken_as_the_day(self):
        self.ask_which_day()
        self.clock.now += 9 * 60
        result = self.say("8th of October.")
        self.assertNotIsInstance(result, PipelineError)          # it became a card or the next question

    def test_an_answer_after_ten_minutes_says_the_question_timed_out(self):
        self.ask_which_day()
        self.clock.now += 11 * 60
        message = self.refused("8th of October.")
        self.assertEqual(message, FORGOTTEN_REPLIES["timed out"]["en"])
        self.assertNotEqual(message, NOT_CLASSIFIED)
        self.assertIn("10 minutes", message)

    def test_only_the_first_turn_after_the_timeout_says_so(self):
        self.ask_which_day()
        self.clock.now += 11 * 60
        self.refused("8th of October.")
        self.assertEqual(self.refused("blah blah"), NOT_CLASSIFIED)

    def test_a_timeout_does_not_get_in_the_way_of_a_real_command(self):
        self.ask_which_day()
        self.clock.now += 11 * 60
        self.assertIsInstance(self.say("show appointments for tomorrow"), ReadResult)

    def test_any_unclear_reply_after_a_timeout_gets_the_timeout_message(self):
        self.ask_which_day()
        self.clock.now += 11 * 60
        self.assertEqual(self.refused("Rakesh"), FORGOTTEN_REPLIES["timed out"]["en"])

    def test_a_bare_day_with_no_question_open_says_there_is_no_question(self):
        self.assertEqual(self.refused("8th of October."), FORGOTTEN_REPLIES["no question"]["en"])
        self.assertEqual(self.refused("at 4 pm"), FORGOTTEN_REPLIES["no question"]["en"])

    def test_a_sentence_that_merely_mentions_a_day_keeps_the_usual_message(self):
        for text in ("the weather is nice today", "I will think about it tomorrow", "what a lovely day it is today"):
            self.assertEqual(self.refused(text), NOT_CLASSIFIED, text)

    def test_short_day_and_time_fragments_still_get_the_message(self):
        for text in ("8th of October", "on the 8th of October at 11 a m", "tomorrow at five", "next Monday at 4 pm"):
            self.assertEqual(self.refused(text), FORGOTTEN_REPLIES["no question"]["en"], text)

    def test_gibberish_with_no_question_keeps_the_usual_message(self):
        self.assertEqual(self.refused("blah blah"), NOT_CLASSIFIED)

    def test_the_reply_follows_the_language_of_the_session(self):
        self.assertEqual(self.refused("8th of October.", "hi-IN"), FORGOTTEN_REPLIES["no question"]["hinglish"])
        self.assertEqual(self.refused("आठ अक्टूबर", "hi-IN"), FORGOTTEN_REPLIES["no question"]["hi"])
        self.assertEqual(self.refused("8th of October.", "en-IN"), FORGOTTEN_REPLIES["no question"]["en"])

    def test_every_message_exists_in_three_languages_and_is_fixed_text(self):
        for kind in ("timed out", "no question"):
            self.assertEqual(set(FORGOTTEN_REPLIES[kind]), {"en", "hi", "hinglish"})


if __name__ == "__main__":
    unittest.main()
