import unittest
from datetime import date

from pydantic import ValidationError

from candidate_profile import AvailabilityWindow, split_items


class CandidateProfileModelTests(unittest.TestCase):
    def test_rejects_end_before_start(self):
        with self.assertRaises(ValidationError):
            AvailabilityWindow(
                start_date=date(2026, 9, 1),
                end_date=date(2026, 8, 1),
                days_per_week=4,
            )

    def test_rejects_onsite_days_above_total_days(self):
        with self.assertRaises(ValidationError):
            AvailabilityWindow(
                start_date=date(2026, 8, 1),
                end_date=date(2026, 12, 1),
                days_per_week=3,
                onsite_days_per_week=4,
            )

    def test_split_items_supports_chinese_separators(self):
        self.assertEqual(
            split_items("上海、杭州，北京"),
            ["上海", "杭州", "北京"],
        )


if __name__ == "__main__":
    unittest.main()

