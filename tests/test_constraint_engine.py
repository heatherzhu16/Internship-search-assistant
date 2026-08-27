import unittest
from datetime import date

from candidate_profile import (
    AvailabilityPreset,
    AvailabilityWindow,
    CandidateFacts,
    CandidateProfileData,
    DefaultAvailability,
    JDConstraint,
    JobPreferences,
    JobContextOverride,
)
from constraint_engine import (
    evaluate_eligibility,
    select_availability_preset,
    suggested_job_override,
)


class ConstraintEngineTests(unittest.TestCase):
    def test_missing_profile_information_is_unknown(self):
        result = evaluate_eligibility(
            [
                JDConstraint(
                    constraint_type="days_per_week",
                    operator=">=",
                    value="4",
                    quote="每周至少到岗4天",
                )
            ],
            CandidateProfileData(),
            JobContextOverride(),
        )
        self.assertEqual(result.checks[0].result, "存疑")

    def test_job_override_takes_precedence(self):
        profile = CandidateProfileData(
            availability_windows=[
                AvailabilityWindow(
                    start_date=date(2026, 8, 1),
                    end_date=date(2026, 12, 31),
                    days_per_week=3,
                )
            ]
        )
        override = JobContextOverride(
            enabled=True,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 12, 31),
            days_per_week=4,
        )
        result = evaluate_eligibility(
            [
                JDConstraint(
                    constraint_type="days_per_week",
                    operator=">=",
                    value="4",
                    quote="每周至少到岗4天",
                )
            ],
            profile,
            override,
        )
        self.assertEqual(result.checks[0].result, "满足")
        self.assertEqual(result.checks[0].source, "本岗位临时补充")

    def test_partial_override_merges_with_profile_window(self):
        profile = CandidateProfileData(
            availability_windows=[
                AvailabilityWindow(
                    start_date=date(2026, 8, 1),
                    end_date=date(2026, 12, 31),
                    days_per_week=3,
                    cities=["上海"],
                )
            ]
        )
        result = evaluate_eligibility(
            [
                JDConstraint(
                    constraint_type="days_per_week",
                    operator=">=",
                    value="4",
                    quote="每周至少到岗4天",
                )
            ],
            profile,
            JobContextOverride(enabled=True, days_per_week=4),
        )
        self.assertEqual(result.checks[0].result, "满足")
        self.assertEqual(result.checks[0].source, "本岗位临时补充")

    def test_separate_windows_do_not_fake_combined_availability(self):
        profile = CandidateProfileData(
            availability_windows=[
                AvailabilityWindow(
                    start_date=date(2026, 8, 1),
                    end_date=date(2026, 9, 15),
                    days_per_week=5,
                ),
                AvailabilityWindow(
                    start_date=date(2026, 9, 16),
                    end_date=date(2026, 12, 31),
                    days_per_week=3,
                ),
            ]
        )
        result = evaluate_eligibility(
            [
                JDConstraint(
                    constraint_type="days_per_week",
                    operator=">=",
                    value="4",
                    quote="每周至少到岗4天",
                ),
                JDConstraint(
                    constraint_type="duration_months",
                    operator=">=",
                    value="3",
                    quote="连续实习至少3个月",
                ),
            ],
            profile,
            JobContextOverride(),
        )
        combined = next(
            item
            for item in result.checks
            if item.constraint_type == "availability_combined"
        )
        self.assertEqual(combined.result, "不满足")

    def test_confirmed_default_commitment_satisfies_duration_and_mobility(self):
        profile = CandidateProfileData(
            default_availability=DefaultAvailability(
                duration_months=6,
                onsite_available=True,
            ),
            preferences=JobPreferences(
                willing_to_relocate=True,
                accept_any_city=True,
            ),
        )
        result = evaluate_eligibility(
            [
                JDConstraint(
                    constraint_type="duration_months",
                    value="3",
                    quote="连续实习至少3个月",
                ),
                JDConstraint(
                    constraint_type="location",
                    value="上海",
                    quote="实习地点：上海",
                ),
                JDConstraint(
                    constraint_type="work_mode",
                    value="现场",
                    quote="不接受远程实习",
                ),
            ],
            profile,
            JobContextOverride(),
        )
        self.assertTrue(all(item.result == "满足" for item in result.checks))

    def test_school_tier_degree_and_student_status_are_checked_together(self):
        profile = CandidateProfileData(
            facts=CandidateFacts(
                institution="示例财经大学",
                institution_tiers=["211"],
                highest_education="本科",
                student_status="本科在校生",
            )
        )
        result = evaluate_eligibility(
            [
                JDConstraint(
                    constraint_type="education",
                    value="本科,硕士",
                    quote="211/985或QS前100的本科/硕士在校生",
                )
            ],
            profile,
            JobContextOverride(),
        )
        self.assertEqual(result.checks[0].result, "满足")
        self.assertIn("211", result.checks[0].candidate_evidence)

    def test_job_type_preset_selection_and_duration_override(self):
        profile = CandidateProfileData(
            availability_presets=[
                AvailabilityPreset(
                    name="互联网",
                    priority=10,
                    match_keywords=["产品", "互联网"],
                    duration_months=6,
                ),
                AvailabilityPreset(
                    name="券商",
                    priority=20,
                    match_keywords=["券商", "证券", "IPO"],
                    duration_months=3,
                ),
            ]
        )
        selected = select_availability_preset(
            profile, "某证券研究所 IPO 项目实习生"
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected.name, "券商")
        result = evaluate_eligibility(
            [
                JDConstraint(
                    constraint_type="duration_months",
                    value="3",
                    quote="至少实习3个月",
                )
            ],
            profile,
            JobContextOverride(
                enabled=True,
                preset_name=selected.name,
                duration_months=selected.duration_months,
            ),
        )
        self.assertEqual(result.checks[0].result, "满足")
        self.assertEqual(result.checks[0].source, "本岗位临时补充")

        tied = select_availability_preset(profile, "证券公司产品实习")
        self.assertEqual(tied.name, "券商")

    def test_major_language_and_remote_limit_use_confirmed_facts(self):
        profile = CandidateProfileData(
            facts=CandidateFacts(
                major="金融学（双语实验班）",
                languages=["英语（雅思7.5、CET-6 640、可作为工作语言）"],
            ),
            default_availability=DefaultAvailability(
                days_per_week=5,
                onsite_available=True,
            ),
        )
        result = evaluate_eligibility(
            [
                JDConstraint(
                    constraint_type="major",
                    value="金融、经济等相关专业",
                    quote="金融、经济等相关专业",
                ),
                JDConstraint(
                    constraint_type="language",
                    value="英语流利",
                    quote="英语流利（must）",
                ),
                JDConstraint(
                    constraint_type="work_mode",
                    value="最多可2天远程",
                    quote="最多可2天远程",
                ),
            ],
            profile,
            JobContextOverride(enabled=True, onsite_available=True),
        )
        self.assertTrue(all(check.result == "满足" for check in result.checks))

    def test_suggested_internet_override_inherits_default_days(self):
        profile = CandidateProfileData(
            default_availability=DefaultAvailability(days_per_week=5),
        )
        override = suggested_job_override(
            profile,
            "京东互联网战略实习生",
            location="北京",
            reference_date=date(2026, 7, 30),
        )
        self.assertEqual(override.preset_name, "互联网")
        self.assertEqual(override.start_date, date(2026, 8, 6))
        self.assertEqual(override.duration_months, 6)
        self.assertEqual(override.days_per_week, 5)
        self.assertEqual(override.cities, ["北京"])


if __name__ == "__main__":
    unittest.main()
