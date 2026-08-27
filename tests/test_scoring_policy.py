import unittest

from candidate_profile import EligibilityAssessment, EligibilityCheck
from scoring_policy import (
    calibrate_decision,
    evidence_backed_score,
    recommendation_for_score,
)


def assessment(result: str | None = None) -> EligibilityAssessment:
    checks = []
    if result:
        checks.append(
            EligibilityCheck(
                constraint_type="days_per_week",
                requirement="每周至少到岗4天",
                result=result,
            )
        )
    return EligibilityAssessment(checks=checks)


class ScoringPolicyTests(unittest.TestCase):
    def test_score_bands_are_deterministic(self):
        self.assertEqual(recommendation_for_score(85), "优先投递")
        self.assertEqual(recommendation_for_score(70), "建议投递")
        self.assertEqual(recommendation_for_score(55), "谨慎投递")
        self.assertEqual(recommendation_for_score(54), "不建议投递")

    def test_unmet_required_condition_forces_no_apply(self):
        result = calibrate_decision(
            91,
            assessment("不满足"),
            model_recommendation="优先投递",
            model_gate_result="满足",
            model_gate_notes=["模型误判"],
        )
        self.assertEqual(result.gate_result, "不满足")
        self.assertEqual(result.final_recommendation, "不建议投递")
        self.assertEqual(result.original_model_gate_result, "满足")

    def test_unknown_required_condition_caps_recommendation(self):
        result = calibrate_decision(
            88,
            assessment("存疑"),
            model_recommendation="优先投递",
        )
        self.assertEqual(result.final_recommendation, "谨慎投递")

    def test_no_explicit_gate_uses_score_band(self):
        result = calibrate_decision(74, assessment())
        self.assertEqual(result.gate_result, "未识别到明确门槛")
        self.assertEqual(result.final_recommendation, "建议投递")

    def test_score_without_evidence_is_zero(self):
        self.assertEqual(evidence_backed_score(18, 20, []), 0)
        self.assertEqual(evidence_backed_score(18, 20, ["  "]), 0)
        self.assertEqual(evidence_backed_score(18, 20, ["使用过 SQL"]), 18)


if __name__ == "__main__":
    unittest.main()
