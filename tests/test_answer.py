import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic.adapters.base import Citation
from clinic.nlu.answer import compose_answer


CITATION = Citation(source="local clinic records", as_of="updated just now")


class MissedFollowupsAnswerTests(unittest.TestCase):
    def test_none_pending_hindi(self):
        text = compose_answer("missed_followups", [], CITATION, "hi-IN")
        self.assertIn("Koi follow-up baaki nahi hai.", text)
        self.assertIn("local clinic records", text)

    def test_none_pending_english(self):
        text = compose_answer("missed_followups", [], CITATION, "en-IN")
        self.assertIn("No follow-ups are pending.", text)

    def test_few_pending_names_are_read_out(self):
        data = [{"name": "Sunita Devi"}, {"name": "Ramesh Kumar"}]
        text = compose_answer("missed_followups", data, CITATION, "hi-IN")
        self.assertIn("2 follow-up baaki hain", text)
        self.assertIn("Sunita Devi", text)
        self.assertIn("Ramesh Kumar", text)

    def test_many_pending_names_are_not_read_out(self):
        data = [{"name": "P{}".format(i)} for i in range(5)]
        text = compose_answer("missed_followups", data, CITATION, "en-IN")
        self.assertIn("5 follow-ups are pending.", text)
        self.assertNotIn("P0", text)


class PatientLookupAnswerTests(unittest.TestCase):
    def test_found_hindi(self):
        text = compose_answer("patient_lookup", {"name": "Sunita Devi", "phone": "9876543210"}, CITATION, "hi-IN")
        self.assertIn("Sunita Devi ka phone number 9876543210 hai.", text)

    def test_found_english(self):
        text = compose_answer("patient_lookup", {"name": "Sunita Devi", "phone": "9876543210"}, CITATION, "en-IN")
        self.assertIn("Sunita Devi's phone number is 9876543210.", text)

    def test_not_found(self):
        text = compose_answer("patient_lookup", None, CITATION, "en-IN")
        self.assertIn("Patient not found.", text)


class CheckAvailabilityAnswerTests(unittest.TestCase):
    def test_no_slots_free(self):
        text = compose_answer("check_availability", {"date": "2026-10-01", "slots": []}, CITATION, "en-IN")
        self.assertIn("No slots are free on 2026-10-01.", text)

    def test_few_slots_are_read_out(self):
        data = {"date": "2026-10-01", "slots": ["09:00", "09:15"]}
        text = compose_answer("check_availability", data, CITATION, "en-IN")
        self.assertIn("09:00", text)
        self.assertIn("09:15", text)

    def test_many_slots_are_counted_not_read_out(self):
        data = {"date": "2026-10-01", "slots": ["{:02d}:00".format(h) for h in range(9, 20)]}
        text = compose_answer("check_availability", data, CITATION, "en-IN")
        self.assertIn("11 free slots", text)
        self.assertNotIn("09:00", text)

    def test_hindi_variant(self):
        data = {"date": "2026-10-01", "slots": ["09:00"]}
        text = compose_answer("check_availability", data, CITATION, "hi-IN")
        self.assertIn("khaali hain", text)


class ListAppointmentsAnswerTests(unittest.TestCase):
    def test_none_scheduled(self):
        text = compose_answer("list_appointments", [], CITATION, "en-IN")
        self.assertIn("No appointments are scheduled.", text)

    def test_count_scheduled(self):
        data = [{"id": 1}, {"id": 2}]
        text = compose_answer("list_appointments", data, CITATION, "en-IN")
        self.assertIn("2 appointment(s) scheduled.", text)


class NextAppointmentAnswerTests(unittest.TestCase):
    def test_none_found(self):
        text = compose_answer("next_appointment", None, CITATION, "en-IN")
        self.assertIn("No upcoming appointment found.", text)

    def test_found(self):
        data = {"patient_label": "Sunita Devi (9876543210)", "appt_date": "2026-10-05", "start_time": "11:00"}
        text = compose_answer("next_appointment", data, CITATION, "en-IN")
        self.assertIn("Sunita Devi (9876543210)", text)
        self.assertIn("2026-10-05", text)
        self.assertIn("11:00", text)


class CashbookAnswerTests(unittest.TestCase):
    def test_figures_are_byte_identical_to_source_data(self):
        data = {"date": "2026-09-25", "fees_paise": 30000, "expenses_paise": 5000, "net_paise": 25000}
        text = compose_answer("day_end_cashbook", data, CITATION, "hi-IN")
        self.assertIn("fees 300 rupaye", text)
        self.assertIn("kharch 50 rupaye", text)
        self.assertIn("net 250 rupaye", text)

    def test_english_variant(self):
        data = {"date": "2026-09-25", "fees_paise": 30000, "expenses_paise": 5000, "net_paise": 25000}
        text = compose_answer("day_end_cashbook", data, CITATION, "en-IN")
        self.assertIn("fees are 300 rupees", text)
        self.assertIn("expenses 50 rupees", text)
        self.assertIn("net 250 rupees", text)

    def test_citation_always_present(self):
        data = {"date": "2026-09-25", "fees_paise": 0, "expenses_paise": 0, "net_paise": 0}
        text = compose_answer("day_end_cashbook", data, CITATION, "en-IN")
        self.assertIn("Source: local clinic records, updated just now.", text)


if __name__ == "__main__":
    unittest.main()
