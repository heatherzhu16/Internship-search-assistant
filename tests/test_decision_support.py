from __future__ import annotations

import unittest

from candidate_profile import CandidateProfileData, EligibilityAssessment, EligibilityCheck
from models.evaluation import DimensionScore, Evaluation
from services.decision_support import build_decision_summary
from services.evaluation_set import evaluation_report_metrics, general_quality_checks
from services.search_assistant import build_platform_links, build_search_keywords


def _evaluation(score_each: int = 8) -> Evaluation:
    maximums = [25, 25, 20, 10, 10, 10]
    fields = ["hard_requirements", "experience_match", "skills_tools", "motivation", "education", "keyword_evidence"]
    values = {
        field: DimensionScore(
            score=min(score_each, maximum),
            max_score=maximum,
            evidence=[f"直接证据：{field}"],
        )
        for field, maximum in zip(fields, maximums, strict=True)
    }
    return Evaluation(
        **values,
        hard_gate_result="满足",
        recommendation="建议投递",
        recommendation_reason="测试",
    )


class DecisionSupportTests(unittest.TestCase):
    def test_incomplete_information_forces_completion_action(self):
        summary = build_decision_summary(
            evaluation=_evaluation(),
            eligibility=EligibilityAssessment(),
            profile=CandidateProfileData(),
            score=80,
            content_level="summary",
            completeness_score=35,
        )
        self.assertEqual(summary.suggested_action, "信息待补全")
        self.assertEqual(summary.information.level, "低")

    def test_unmet_gate_beats_high_reference_score(self):
        eligibility = EligibilityAssessment(
            checks=[EligibilityCheck(constraint_type="major", requirement="计算机专业", result="不满足")],
            unmet_count=1,
        )
        summary = build_decision_summary(
            evaluation=_evaluation(), eligibility=eligibility,
            profile=CandidateProfileData(), score=96,
        )
        self.assertEqual(summary.eligibility.result, "不满足")
        self.assertEqual(summary.suggested_action, "暂不投递")

    def test_search_assistant_builds_platform_links_without_fetching(self):
        keywords = build_search_keywords(CandidateProfileData(), roles=["战略"], cities=["北京"])
        self.assertIn("战略 实习 北京", keywords)
        links = build_platform_links([keywords[0]])
        self.assertEqual({link.platform for link in links}, {"BOSS直聘", "实习僧", "小红书"})

    def test_general_guardrails_all_pass(self):
        self.assertTrue(all(check["通过"] for check in general_quality_checks()))

    def test_report_metrics_include_ordering(self):
        metrics = evaluation_report_metrics([
            {"actual_score": 85, "expected_score": "80-90", "score_pass": True, "gate_pass": True, "gap_checks": [], "score_deviation": 0, "repeat_count": 1},
            {"actual_score": 45, "expected_score": "40-50", "score_pass": True, "gate_pass": True, "gap_checks": [], "score_deviation": 0, "repeat_count": 1},
        ])
        self.assertEqual(metrics["rank_consistency"], 1.0)


if __name__ == "__main__":
    unittest.main()
