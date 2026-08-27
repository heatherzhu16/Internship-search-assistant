from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from candidate_profile import CandidateProfileData, EligibilityAssessment
from models.evaluation import Evaluation


Level = Literal["高", "中", "低", "待确认"]


class DecisionLayer(BaseModel):
    label: str
    result: str
    level: Level
    reason: str


class DecisionSummary(BaseModel):
    eligibility: DecisionLayer
    capability: DecisionLayer
    preference: DecisionLayer
    information: DecisionLayer
    reference_score: int = Field(ge=0, le=100)
    confidence: Level
    suggested_action: str
    caveats: list[str] = Field(default_factory=list)


def _contains_any(value: str, choices: list[str]) -> bool:
    clean = value.casefold().replace(" ", "")
    return any(
        choice.casefold().replace(" ", "") in clean
        or clean in choice.casefold().replace(" ", "")
        for choice in choices
        if choice.strip() and clean
    )


def _preference_layer(
    profile: CandidateProfileData,
    *,
    company: str,
    role: str,
    location: str,
) -> DecisionLayer:
    preferences = profile.preferences
    issues: list[str] = []
    positives: list[str] = []

    if _contains_any(company, preferences.excluded_companies):
        issues.append("公司在排除名单中")
    elif preferences.company_filter_mode == "仅目标公司":
        if _contains_any(company, preferences.target_companies):
            positives.append("属于目标公司")
        else:
            issues.append("不在目标公司名单")
    elif _contains_any(company, preferences.target_companies):
        positives.append("属于目标公司")

    if preferences.target_roles:
        (positives if _contains_any(role, preferences.target_roles) else issues).append(
            "岗位方向匹配" if _contains_any(role, preferences.target_roles) else "岗位方向需确认"
        )
    if preferences.preferred_cities and not preferences.accept_any_city:
        (positives if _contains_any(location, preferences.preferred_cities) else issues).append(
            "城市匹配" if _contains_any(location, preferences.preferred_cities) else "城市不在偏好范围"
        )

    if not positives and not issues:
        return DecisionLayer(
            label="个人偏好", result="待确认", level="待确认", reason="尚未设置可核对的岗位偏好。"
        )
    if any("排除" in item or "不在目标公司" in item for item in issues):
        result, level = "不符合", "低"
    elif issues:
        result, level = "部分符合", "中"
    else:
        result, level = "符合", "高"
    return DecisionLayer(
        label="个人偏好",
        result=result,
        level=level,
        reason="；".join([*positives, *issues]),
    )


def _direct_evidence_count(evaluation: Evaluation) -> int:
    count = 0
    for name in (
        "hard_requirements",
        "experience_match",
        "skills_tools",
        "motivation",
        "education",
        "keyword_evidence",
    ):
        for evidence in getattr(evaluation, name).evidence:
            normalized = str(evidence).strip()
            if normalized and not normalized.startswith(("可迁移", "间接", "弱相关")):
                count += 1
    return count


def build_decision_summary(
    *,
    evaluation: Evaluation,
    eligibility: EligibilityAssessment,
    profile: CandidateProfileData,
    score: int,
    company: str = "",
    role: str = "",
    location: str = "",
    content_level: str = "full",
    completeness_score: int = 100,
    capture_kind: str = "text",
) -> DecisionSummary:
    bounded_score = max(0, min(int(score), 100))
    if eligibility.unmet_count:
        gate_result, gate_level = "不满足", "低"
    elif eligibility.unknown_count or eligibility.negotiable_count:
        gate_result, gate_level = "待确认", "中"
    elif eligibility.checks:
        gate_result, gate_level = "满足", "高"
    else:
        gate_result, gate_level = "未识别到明确门槛", "待确认"
    gate = DecisionLayer(
        label="硬性门槛",
        result=gate_result,
        level=gate_level,
        reason=eligibility.summary or "根据结构化 JD 与个人档案核对。",
    )

    direct_count = _direct_evidence_count(evaluation)
    if bounded_score >= 75 and direct_count >= 4:
        capability_result, capability_level = "较强", "高"
    elif bounded_score >= 55 and direct_count >= 2:
        capability_result, capability_level = "中等", "中"
    else:
        capability_result, capability_level = "较弱", "低"
    capability = DecisionLayer(
        label="能力匹配",
        result=capability_result,
        level=capability_level,
        reason=f"个人参考分 {bounded_score}/100；识别到 {direct_count} 条较直接证据。",
    )

    preference = _preference_layer(
        profile, company=company, role=role, location=location
    )
    scorable = (
        content_level == "full"
        and completeness_score >= 80
        and capture_kind != "image"
    )
    if scorable and completeness_score >= 95:
        info_result, info_level = "完整可核验", "高"
    elif scorable:
        info_result, info_level = "基本完整", "中"
    else:
        info_result, info_level = "不足以正式评分", "低"
    information = DecisionLayer(
        label="信息可信度",
        result=info_result,
        level=info_level,
        reason=f"内容级别 {content_level}，字段完整度 {int(completeness_score)}%，形态 {capture_kind}。",
    )

    caveats: list[str] = []
    if information.level == "低":
        caveats.append("先补全岗位职责与任职要求，再使用参考分。")
    if gate.level in {"低", "待确认"}:
        caveats.append("硬性条件未通过或未确认，不应只看总分。")
    if direct_count < 3:
        caveats.append("简历中的直接证据较少，能力分置信度有限。")

    if information.level == "低":
        confidence, action = "低", "信息待补全"
    elif gate_result == "不满足":
        confidence, action = "高", "暂不投递"
    elif preference.result == "不符合":
        confidence, action = "中", "暂不投递"
    elif gate_result == "满足" and capability_level == "高" and preference.level == "高":
        confidence, action = "高", "准备投递"
    else:
        confidence, action = "中", "继续了解"

    return DecisionSummary(
        eligibility=gate,
        capability=capability,
        preference=preference,
        information=information,
        reference_score=bounded_score,
        confidence=confidence,
        suggested_action=action,
        caveats=caveats,
    )
