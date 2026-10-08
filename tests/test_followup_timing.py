"""When the two follow-up reminders go out: 2 days before at 10:00, and 4 hours
before the slot but never earlier than 07:00 that day; the clinic can change
all four numbers; a reminder is never due at or after the slot itself."""
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.followup_fixtures import D2, FollowupCase, at, make_db  # noqa: E402

from clinic import followups, settings  # noqa: E402


def t(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


class ReminderTimeTests(unittest.TestCase):
    def test_defaults_are_two_days_before_at_ten_and_four_hours_before(self):
        times = followups.reminder_times("2026-10-07", "16:00")
        self.assertEqual(times["2d"], t("2026-10-05 10:00"))          # 2 days before the DATE, at 10:00
        self.assertEqual(times["4h"], t("2026-10-07 12:00"))          # 4 hours before the slot

    def test_the_early_reminder_ignores_the_slot_time_of_day(self):
        for slot in ("09:00", "12:30", "19:30"):
            self.assertEqual(followups.reminder_times("2026-10-07", slot)["2d"], t("2026-10-05 10:00"))

    def test_the_second_reminder_is_never_earlier_than_seven_that_day(self):
        self.assertEqual(followups.reminder_times("2026-10-07", "09:00")["4h"], t("2026-10-07 07:00"))   # 05:00 -> 07:00
        self.assertEqual(followups.reminder_times("2026-10-07", "10:00")["4h"], t("2026-10-07 07:00"))   # 06:00 -> 07:00
        self.assertEqual(followups.reminder_times("2026-10-07", "11:00")["4h"], t("2026-10-07 07:00"))   # exactly 07:00
        self.assertEqual(followups.reminder_times("2026-10-07", "11:30")["4h"], t("2026-10-07 07:30"))   # later than 07:00: not clamped

    def test_it_is_skipped_when_seven_oclock_is_not_before_the_slot(self):
        self.assertIsNone(followups.reminder_times("2026-10-07", "07:00")["4h"])    # the slot itself
        self.assertIsNone(followups.reminder_times("2026-10-07", "06:30")["4h"])    # already past 07:00
        self.assertEqual(followups.reminder_times("2026-10-07", "07:30")["4h"], t("2026-10-07 07:00"))

    def test_the_clinic_settings_change_every_number(self):
        cfg = {"days_before": 3, "send_time": "09:30", "hours_before": 2, "earliest_send": "08:00"}
        times = followups.reminder_times("2026-10-07", "16:00", cfg)
        self.assertEqual(times["2d"], t("2026-10-04 09:30"))
        self.assertEqual(times["4h"], t("2026-10-07 14:00"))
        self.assertEqual(followups.reminder_times("2026-10-07", "09:00", cfg)["4h"], t("2026-10-07 08:00"))   # 07:00 -> clamp 08:00
        self.assertIsNone(followups.reminder_times("2026-10-07", "08:00", cfg)["4h"])

    def test_the_second_reminder_is_never_at_or_after_the_slot_whatever_the_settings(self):
        for hours in (1, 4, 12):
            for earliest in ("05:00", "07:00", "12:00", "22:00"):
                cfg = {"days_before": 2, "send_time": "10:00", "hours_before": hours, "earliest_send": earliest}
                for hour in range(0, 24):
                    for minute in (0, 30):
                        slot = "{:02d}:{:02d}".format(hour, minute)
                        late = followups.reminder_times("2026-10-07", slot, cfg)["4h"]
                        if late is not None:
                            self.assertLess(late, followups.slot_start("2026-10-07", slot), (cfg, slot))
                            self.assertGreaterEqual(late.strftime("%H:%M"), earliest, (cfg, slot))
                            self.assertEqual(late.date().isoformat(), "2026-10-07")        # the slot's own day

    def test_month_and_year_boundaries(self):
        self.assertEqual(followups.reminder_times("2026-11-01", "10:00")["2d"], t("2026-10-30 10:00"))
        self.assertEqual(followups.reminder_times("2027-01-01", "10:00")["2d"], t("2026-12-30 10:00"))


class ReminderSettingTests(FollowupCase):
    def test_defaults(self):
        self.assertEqual(settings.followup_reminder_settings(self.conn),
                         {"days_before": 2, "send_time": "10:00", "hours_before": 4, "earliest_send": "07:00"})

    def test_saving_and_reading_back(self):
        settings.set_followup_reminder_settings(self.conn, "3", "09:30", 2, "08:00")
        self.assertEqual(settings.followup_reminder_settings(self.conn),
                         {"days_before": 3, "send_time": "09:30", "hours_before": 2, "earliest_send": "08:00"})

    def test_bad_values_are_refused_with_a_message_and_nothing_is_saved(self):
        for args, word in (((0, "10:00", 4, "07:00"), "Days before"), ((15, "10:00", 4, "07:00"), "Days before"),
                           (("x", "10:00", 4, "07:00"), "Days before"), ((True, "10:00", 4, "07:00"), "Days before"),
                           ((2, "25:00", 4, "07:00"), "Send time"), ((2, "3:00", 4, "07:00"), "Send time"),
                           ((2, "02:00", 4, "07:00"), "Send time"), ((2, "10:00", 0, "07:00"), "Hours before"),
                           ((2, "10:00", 13, "07:00"), "Hours before"), ((2, "10:00", 4, "late"), "Earliest send time"),
                           ((2, "10:00", 4, "23:30"), "Earliest send time")):
            with self.subTest(args=args):
                with self.assertRaises(ValueError) as caught:
                    settings.set_followup_reminder_settings(self.conn, *args)
                self.assertIn(word, str(caught.exception))
        self.assertEqual(settings.followup_reminder_settings(self.conn)["days_before"], 2)

    def test_a_corrupt_stored_value_falls_back_to_its_default(self):
        settings.set_value(self.conn, settings.FU_DAYS_BEFORE, "banana")
        settings.set_value(self.conn, settings.FU_SEND_TIME, "27:99")
        cfg = settings.followup_reminder_settings(self.conn)
        self.assertEqual((cfg["days_before"], cfg["send_time"]), (2, "10:00"))

    def test_a_new_followup_uses_the_clinic_settings(self):
        settings.set_followup_reminder_settings(self.conn, 1, "08:00", 3, "09:00")
        fid = self.make(due_time="16:00")
        due = {r["kind"]: r["due_at"] for r in self.reminder_rows(fid)}
        self.assertEqual(due, {"2d": "2026-10-06 08:00", "4h": "2026-10-07 13:00"})

    def test_changing_the_settings_moves_reminders_that_have_not_gone_out(self):
        fid = self.make(due_time="16:00")
        self.assertEqual({r["kind"]: r["due_at"] for r in self.reminder_rows(fid)}, {"2d": "2026-10-05 10:00", "4h": "2026-10-07 12:00"})
        settings.set_followup_reminder_settings(self.conn, 1, "08:00", 3, "09:00")
        followups.refresh_scheduled(self.conn)
        self.assertEqual({r["kind"]: r["due_at"] for r in self.reminder_rows(fid)}, {"2d": "2026-10-06 08:00", "4h": "2026-10-07 13:00"})

    def test_a_reminder_already_sent_keeps_its_state_when_settings_change(self):
        fid = self.make(due_time="16:00")
        followups.process_due(self.conn, at(5, 12))                 # the early reminder is out
        settings.set_followup_reminder_settings(self.conn, 1, "08:00", 3, "09:00")
        followups.refresh_scheduled(self.conn)
        states = {r["kind"]: r["state"] for r in self.reminder_rows(fid)}
        self.assertEqual(states, {"2d": "enqueued", "4h": "scheduled"})


class NoRealDateTests(unittest.TestCase):
    def test_nothing_here_reads_the_real_clock(self):
        conn = make_db()
        # a far-future date with the fixed fake clock: due times depend only on the arguments
        self.assertEqual(followups.reminder_times("2031-03-15", "10:00")["2d"], t("2031-03-13 10:00"))
        self.assertEqual(at(5, 10).local, datetime(2026, 10, 5, 10, 0))
        self.assertEqual(D2, "2026-10-07")
        conn.close()


if __name__ == "__main__":
    unittest.main()
