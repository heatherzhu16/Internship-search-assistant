from __future__ import annotations

import unittest
from datetime import date

from services.publication_date import normalize_posted_at, publication_recency_label


class PublicationDateTests(unittest.TestCase):
    def test_normalizes_xhs_month_day_with_location_suffix(self):
        self.assertEqual(
            normalize_posted_at("08-10 浙江", date(2026, 8, 25)),
            "2026-08-10",
        )

    def test_infers_previous_year_for_future_month_day(self):
        self.assertEqual(
            normalize_posted_at("12-30", date(2026, 1, 5)),
            "2025-12-30",
        )

    def test_recent_posts_are_labeled_without_changing_score(self):
        reference = date(2026, 8, 25)
        self.assertEqual(publication_recency_label("2026-08-23", reference), "新发布")
        self.assertEqual(publication_recency_label("2026-08-20", reference), "近一周")
        self.assertEqual(publication_recency_label("2026-08-10", reference), "")


if __name__ == "__main__":
    unittest.main()
