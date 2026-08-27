from __future__ import annotations

import re

from candidate_profile import EligibilityAssessment, EligibilityCheck


SKILL_PATTERNS: dict[str, tuple[str, ...]] = {
    "SQL": ("sql",),
    "Python": ("python",),
    "Tableau": ("tableau",),
    "Power BI": ("power bi", "powerbi"),
    "Excel": ("excel",),
    "SPSS": ("spss",),
    "Stata": ("stata",),
    "R": ("r语言", "r language"),
    "Axure": ("axure",),
    "Figma": ("figma",),
    "CRM": ("crm",),
    "ERP": ("erp",),
}
HARD_MARKERS = ("必须", "必要条件", "硬性条件", "务必", "一票否决")
PROFICIENCY_MARKERS = ("熟练使用", "熟练应用", "熟练掌握", "精通")
OPTIONAL_MARKERS = ("优先", "加分", "更佳", "有则更好")
WEAK_LEVEL_MARKERS = ("基础", "基本", "入门", "了解", "初步", "简单查询")
STRONG_LEVEL_MARKERS = ("熟练", "精通", "独立使用", "实际使用", "项目中使用", "工作中使用")


def _clauses(text: str) -> list[tuple[str, str]]:
    output: list[tuple[str, str]] = []
    section = ""
    for line in str(text or "").splitlines():
        clean = line.strip()
        if not clean:
            continue
        if clean in {"任职要求", "任职资格", "岗位要求", "职位要求"}:
            section = "requirements"
            continue
        if clean in {"工作职责", "岗位职责", "职位描述", "工作内容"}:
            section = "responsibilities"
            continue
        for prefix in ("任职要求", "任职资格", "岗位要求", "职位要求"):
            if clean.startswith(prefix):
                section = "requirements"
                clean = clean.removeprefix(prefix).lstrip("：:")
                break
        for clause in re.split(r"[。；;]", clean):
            if clause.strip():
                output.append((section, clause.strip()))
    return output


def _resume_contexts(resume_text: str, aliases: tuple[str, ...]) -> list[str]:
    folded = resume_text.casefold()
    contexts: list[str] = []
    for alias in aliases:
        start = 0
        while True:
            index = folded.find(alias.casefold(), start)
            if index < 0:
                break
            contexts.append(folded[max(0, index - 45): index + len(alias) + 45])
            start = index + len(alias)
    return contexts


def _level_claim(
    resume_text: str,
    aliases: tuple[str, ...],
    markers: tuple[str, ...],
    *,
    marker_after_skill_distance: int = 10,
) -> bool:
    folded = resume_text.casefold()
    for alias in aliases:
        escaped_alias = re.escape(alias.casefold())
        for marker in markers:
            escaped_marker = re.escape(marker.casefold())
            if re.search(
                rf"(?:{escaped_alias}.{{0,{marker_after_skill_distance}}}{escaped_marker}|"
                rf"{escaped_marker}.{{0,10}}{escaped_alias})",
                folded,
            ):
                return True
    return False


def _professional_skill_checks(jd_text: str, resume_text: str) -> list[EligibilityCheck]:
    checks: list[EligibilityCheck] = []
    for section, clause in _clauses(jd_text):
        folded_clause = clause.casefold()
        explicit_gate = any(marker in folded_clause for marker in HARD_MARKERS)
        requirement_proficiency = (
            section == "requirements"
            and any(marker in folded_clause for marker in PROFICIENCY_MARKERS)
        )
        if not explicit_gate and not requirement_proficiency:
            continue
        if any(marker in folded_clause for marker in OPTIONAL_MARKERS):
            continue
        for skill, aliases in SKILL_PATTERNS.items():
            if not any(alias.casefold() in folded_clause for alias in aliases):
                continue
            contexts = _resume_contexts(resume_text, aliases)
            weak_claim = _level_claim(resume_text, aliases, WEAK_LEVEL_MARKERS)
            strong_claim = _level_claim(
                resume_text,
                aliases,
                STRONG_LEVEL_MARKERS,
                marker_after_skill_distance=4,
            )
            requires_proficiency = any(
                marker in folded_clause for marker in ("熟练", "精通", "必要条件", "硬性条件")
            )
            if not contexts:
                result = "不满足"
                evidence = f"简历未找到 {skill} 证据"
                notes = "JD 将该技能写为明确必须条件。"
            elif requires_proficiency and weak_claim and not strong_claim:
                result = "不满足"
                evidence = f"简历仅体现 {skill} 基础或入门水平"
                notes = "JD 要求熟练/必要水平，候选人证据明确低于要求。"
            elif requires_proficiency and not strong_claim:
                result = "存疑"
                evidence = f"简历提及 {skill}，但熟练程度缺少可核验场景"
                notes = "需要补充项目或工作中的实际使用证据。"
            else:
                result = "满足"
                evidence = f"简历包含与 {skill} 要求对应的使用证据"
                notes = ""
            checks.append(
                EligibilityCheck(
                    constraint_type="professional_skill",
                    requirement=clause,
                    result=result,
                    candidate_evidence=evidence,
                    source="简历证据",
                    notes=notes,
                )
            )
    return checks


def augment_professional_eligibility(
    assessment: EligibilityAssessment,
    *,
    jd_text: str,
    resume_text: str,
) -> EligibilityAssessment:
    existing = list(assessment.checks)
    signatures = {
        (check.constraint_type, check.requirement.casefold().strip())
        for check in existing
    }
    for check in _professional_skill_checks(jd_text, resume_text):
        signature = (check.constraint_type, check.requirement.casefold().strip())
        if signature not in signatures:
            existing.append(check)
            signatures.add(signature)
    counts = {
        result: sum(check.result == result for check in existing)
        for result in ("满足", "不满足", "存疑", "可能协商")
    }
    professional_count = sum(check.constraint_type == "professional_skill" for check in existing)
    summary = assessment.summary
    if professional_count:
        summary = "；".join(
            part for part in [summary, f"另核对 {professional_count} 项明确专业技能门槛"] if part
        )
    return EligibilityAssessment(
        checks=existing,
        met_count=counts["满足"],
        unmet_count=counts["不满足"],
        unknown_count=counts["存疑"],
        negotiable_count=counts["可能协商"],
        summary=summary,
    )
