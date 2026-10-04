import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic.nlu import llm_slots
from clinic.nlu.llm_slots import (
    _clean_name, clear_prefetch, extract_name, match_known_name, ollama_options,
    prefetch_name, reset_known_names, set_known_names,
)


def _reply(content):
    response = Mock()
    response.raise_for_status = Mock()
    response.json.return_value = {"message": {"content": content}}
    return response


class MatchKnownNameTests(unittest.TestCase):
    NAMES = ["Rakesh Verma", "Sunita Devi", "Mohan Lal", "Mohan Das", "Seema"]

    def test_full_registered_name_is_matched_case_insensitively(self):
        self.assertEqual(match_known_name("what is rakesh verma's phone number", self.NAMES), "Rakesh Verma")
        self.assertEqual(match_known_name("what is Rakesh Verma ka phone number", self.NAMES), "Rakesh Verma")
        self.assertEqual(match_known_name("SUNITA DEVI ka appointment cancel", self.NAMES), "Sunita Devi")

    def test_a_single_word_staff_name_matches(self):
        self.assertEqual(match_known_name("Seema aaj absent hai", self.NAMES), "Seema")

    def test_partial_name_does_not_match(self):
        self.assertIsNone(match_known_name("Rakesh ka number batao", self.NAMES))

    def test_two_different_names_tying_is_ambiguous(self):
        self.assertIsNone(match_known_name("Mohan Lal aur Mohan Das", self.NAMES))

    def test_longest_name_wins(self):
        self.assertEqual(match_known_name("Seema Rakesh Verma", self.NAMES), "Rakesh Verma")

    def test_devanagari_command_for_a_roman_name_does_not_match(self):
        self.assertIsNone(match_known_name("राकेश वर्मा का नंबर बताओ", self.NAMES))

    def test_nothing_known_or_empty_text(self):
        self.assertIsNone(match_known_name("Rakesh Verma", []))
        self.assertIsNone(match_known_name("", self.NAMES))


class CleanNameTests(unittest.TestCase):
    def test_plain_and_decorated_answers(self):
        for raw, expected in (("Ravi Kumar", "Ravi Kumar"), ('"Ravi Kumar".', "Ravi Kumar"),
                              ("Name: Ravi Kumar", "Ravi Kumar"), ("अमित दुआ", "अमित दुआ"),
                              ('{"name": "Ravi Kumar"}', "Ravi Kumar"), ("Ravi Kumar\nextra line", "Ravi Kumar")):
            with self.subTest(raw=raw):
                self.assertEqual(_clean_name(raw), expected)

    def test_none_empty_and_sentences_are_no_name(self):
        for raw in ("NONE", "none", "", "  ", "null", "I could not find any person in this command at all"):
            with self.subTest(raw=raw):
                self.assertIsNone(_clean_name(raw))


class ExtractNameTests(unittest.TestCase):
    def tearDown(self):
        clear_prefetch()

    @patch("clinic.nlu.llm_slots.httpx.post")
    def test_a_registered_name_makes_no_model_call(self, post):
        token = set_known_names(["Rakesh Verma"])
        try:
            self.assertEqual(extract_name("Rakesh Verma ka number"), "Rakesh Verma")
        finally:
            reset_known_names(token)
        post.assert_not_called()

    @patch("clinic.nlu.llm_slots.httpx.post", return_value=_reply("Ravi Kumar"))
    def test_unknown_name_asks_for_bare_text_not_json(self, post):
        self.assertEqual(extract_name("register Ravi Kumar 9876543210"), "Ravi Kumar")
        body = post.call_args.kwargs["json"]
        self.assertNotIn("format", body)
        self.assertEqual(body["options"]["temperature"], 0)

    @patch("clinic.nlu.llm_slots.httpx.post", return_value=_reply("Ravi Kumar"))
    def test_prefetched_answer_is_reused_without_a_second_call(self, post):
        prefetch_name("register Ravi Kumar 9876543210")
        self.assertEqual(extract_name("register Ravi Kumar 9876543210"), "Ravi Kumar")
        self.assertEqual(post.call_count, 1)

    @patch("clinic.nlu.llm_slots.httpx.post", return_value=_reply("Ravi Kumar"))
    def test_prefetch_for_a_different_text_is_not_reused(self, post):
        prefetch_name("register Ravi Kumar 9876543210")
        llm_slots._prefetched.get()[1].result()
        extract_name("something else Ravi Kumar")
        self.assertEqual(post.call_count, 2)

    @patch("clinic.nlu.llm_slots.httpx.post", return_value=_reply("Ravi Kumar"))
    def test_prefetch_is_skipped_when_a_registered_name_matches(self, post):
        token = set_known_names(["Rakesh Verma"])
        try:
            prefetch_name("Rakesh Verma ka number")
        finally:
            reset_known_names(token)
        post.assert_not_called()


class OptionsTests(unittest.TestCase):
    def test_runs_on_cpu_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLINIC_LLM_NUM_GPU", None)
            self.assertEqual(ollama_options()["num_gpu"], 0)

    def test_auto_leaves_the_choice_to_ollama(self):
        with patch.dict(os.environ, {"CLINIC_LLM_NUM_GPU": "auto"}):
            self.assertNotIn("num_gpu", ollama_options())

    def test_garbage_falls_back_to_cpu_and_extra_options_merge(self):
        with patch.dict(os.environ, {"CLINIC_LLM_NUM_GPU": "banana"}):
            options = ollama_options(num_predict=16)
        self.assertEqual((options["num_gpu"], options["num_predict"]), (0, 16))


class PipelineKnownNamesTests(unittest.TestCase):
    def test_registered_names_filters_single_word_patients_but_keeps_staff(self):
        import sqlite3
        from clinic.queries import registered_names
        conn = sqlite3.connect(":memory:")
        conn.executescript(Path("clinic/schema.sql").read_text())
        conn.execute("INSERT INTO patients (name, phone, age) VALUES ('Rakesh Verma', '9000000001', 40)")
        conn.execute("INSERT INTO patients (name, phone, age) VALUES ('Anil', '9000000002', 30)")
        conn.execute("INSERT INTO staff (name, role) VALUES ('Seema', 'nurse')")
        self.assertEqual(sorted(registered_names(conn)), ["Rakesh Verma", "Seema"])


if __name__ == "__main__":
    unittest.main()


class ModelUnavailableTests(unittest.TestCase):
    def tearDown(self):
        clear_prefetch()

    @patch("clinic.nlu.llm_slots.httpx.post", side_effect=OSError("connection refused"))
    def test_a_missing_model_leaves_the_name_blank_instead_of_failing(self, _post):
        self.assertIsNone(extract_name("book an appointment for Anurag Sharma"))

    @patch("clinic.nlu.llm_slots.httpx.post", side_effect=OSError("connection refused"))
    def test_a_failed_background_lookup_also_leaves_the_name_blank(self, _post):
        prefetch_name("book an appointment for Anurag Sharma")
        self.assertIsNone(extract_name("book an appointment for Anurag Sharma"))

    @patch("clinic.nlu.llm_slots.httpx.post", side_effect=OSError("connection refused"))
    def test_a_registered_name_still_works_without_the_model(self, _post):
        token = set_known_names(["Rakesh Verma"])
        try:
            self.assertEqual(extract_name("Rakesh Verma ka number"), "Rakesh Verma")
        finally:
            reset_known_names(token)
