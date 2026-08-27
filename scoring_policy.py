from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from candidate_profile import EligibilityAssessment


Recommendation = Literal["优先投递", "建议投递", "谨慎投递", "不建议投递"]


class DecisionOutcome(BaseModel):
    fit_score: int = Field(ge=0, le=100)
    gate_result: str
    base_recommendation: Recommendation
    final_recommendation: Recommendation
    final_reason: str
    original_model_recommendation: str = ""
    original_model_reason: str = ""
    original_model_gate_result: str = ""
    original_model_gate_notes: list[str] = Field(default_factory=list)
    recommendation_adjusted: bool = False


def evidence_backed_score(
    score: int,
    maximum: int,
    evidence: list[str],
) -> int:
    """Prevent a model from awarding points when it returned no usable evidence."""
    bounded = max(0, min(int(score), int(maximum)))
    if not any(item.strip() for item in evidence):
        return 0
    return bounded


def recommendation_for_score(score: int) -> Recommendation:
    if score >= 85:
        return "优先投递"
    if score >= 70:
        return "建议投递"
    if score >= 55:
        return "谨慎投递"
    return "不建议投递"


def _requirements(assessment: EligibilityAssessment, result: str) -> list[str]:
    return [
        check.requirement
        for check in assessment.checks
        if check.result == result and check.requirement.strip()
    ]


def _short_list(items: list[str], limit: int = 2) -> str:
    visible = items[:limit]
    suffix = f"等{len(items)}项" if len(items) > limit else ""
    return "；".join(visible) + suffix


def calibrate_decision(
    fit_score: int,
    assessment: EligibilityAssessment,
    *,
    model_recommendation: str = "",
    model_reason: str = "",
    model_gate_result: str = "",
    model_gate_notes: list[str] | None = None,
) -> DecisionOutcome:
    """Apply deterministic eligibility gates after the model scores job fit.

    The numerical fit score is intentionally kept separate from eligibility:
    a candidate can be professionally well matched while still failing an
    availability requirement. The final recommendation combines both signals.
    """
    bounded_score = max(0, min(int(fit_score), 100))
    base = recommendation_for_score(bounded_score)
    final: Recommendation = base

    unmet = _requirements(assessment, "不满足")
    unknown = _requirements(assessment, "存疑")
    negotiable = _requirements(assessment, "可能协商")

    if unmet:
        gate_result = "不满足"
        final = "不建议投递"
        reason = (
            f"专业匹配分为 {bounded_score}/100，但存在明确不满足的必须条件："
            f"{_short_list(unmet)}。除非该条件可与招聘方协商，否则不建议投入投递时间。"
        )
    elif unknown:
        gate_result = "存疑"
        if final in {"优先投递", "建议投递"}:
            final = "谨慎投递"
        reason = (
            f"专业匹配分为 {bounded_score}/100；以下必须条件尚未确认："
            f"{_short_list(unknown)}。补全个人档案或向招聘方确认后，再决定是否升级优先级。"
        )
    elif negotiable:
        gate_result = "可能协商"
        if final in {"优先投递", "建议投递"}:
            final = "谨慎投递"
        reason = (
            f"专业匹配分为 {bounded_score}/100；"
            f"{_short_list(negotiable)}需要协商确认，因此暂按谨慎投递处理。"
        )
    elif assessment.checks:
        gate_result = "满足"
        reason = (
            f"已核对的必须条件均满足；专业匹配分为 {bounded_score}/100，"
            f"按统一分数档位给出“{final}”。"
        )
    else:
        gate_result = "未识别到明确门槛"
        reason = (
            f"JD 中未识别到可核对的明确必须条件；专业匹配分为 "
            f"{bounded_score}/100，按统一分数档位给出“{final}”。"
        )

    return DecisionOutcome(
        fit_score=bounded_score,
        gate_result=gate_result,
        base_recommendation=base,
        final_recommendation=final,
        final_reason=reason,
        original_model_recommendation=model_recommendation,
        original_model_reason=model_reason,
        original_model_gate_result=model_gate_result,
        original_model_gate_notes=model_gate_notes or [],
        recommendation_adjusted=(
            final != model_recommendation or gate_result not in {"满足", "未识别到明确门槛"}
        ),
    )
