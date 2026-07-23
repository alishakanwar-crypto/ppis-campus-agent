import unittest
from datetime import datetime

from gate_counter import IST, is_cpplus_after_hours_time, is_monitoring_time


class CPPlusIntelligenceScheduleTests(unittest.TestCase):
    def test_school_hours_are_official_only(self):
        current = datetime(2026, 7, 23, 9, 0, tzinfo=IST)
        self.assertTrue(is_monitoring_time(current))
        self.assertFalse(is_cpplus_after_hours_time(current))

    def test_limited_morning_and_evening_after_hours_windows(self):
        self.assertTrue(
            is_cpplus_after_hours_time(datetime(2026, 7, 23, 5, 30, tzinfo=IST))
        )
        self.assertTrue(
            is_cpplus_after_hours_time(datetime(2026, 7, 23, 19, 0, tzinfo=IST))
        )
        self.assertFalse(
            is_cpplus_after_hours_time(datetime(2026, 7, 23, 23, 0, tzinfo=IST))
        )


if __name__ == "__main__":
    unittest.main()
