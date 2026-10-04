import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clinic.timefmt import utc_to_ist


class UtcToIstTests(unittest.TestCase):
    def test_adds_five_and_a_half_hours(self):
        self.assertEqual(utc_to_ist("2026-10-03 06:34:16"), "2026-10-03 12:04:16")

    def test_rolls_over_to_the_next_day(self):
        self.assertEqual(utc_to_ist("2026-10-03 20:00:00"), "2026-10-04 01:30:00")

    def test_accepts_iso_forms(self):
        self.assertEqual(utc_to_ist("2026-10-03T06:34:16Z"), "2026-10-03 12:04:16")
        self.assertEqual(utc_to_ist("2026-10-03 06:34"), "2026-10-03 12:04:00")

    def test_empty_and_unparseable_values_pass_through(self):
        self.assertIsNone(utc_to_ist(None))
        self.assertEqual(utc_to_ist(""), "")
        self.assertEqual(utc_to_ist("not a time"), "not a time")


class IstFilterRegisteredTests(unittest.TestCase):
    def test_template_filter_is_available(self):
        import app as app_module
        self.assertIn("ist", app_module.app.jinja_env.filters)
        rendered = app_module.app.jinja_env.from_string("{{ t|ist }}").render(t="2026-10-03 06:34:16")
        self.assertEqual(rendered, "2026-10-03 12:04:16")


if __name__ == "__main__":
    unittest.main()
