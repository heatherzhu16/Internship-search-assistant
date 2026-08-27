from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from candidate_profile import (
    AvailabilityPreset,
    AvailabilityWindow,
    CandidateProfileData,
    EligibilityAssessment,
    EligibilityCheck,
    JDConstraint,
    JobContextOverride,
)


def select_availability_preset(
    profile: CandidateProfileData,
    job_text: str,
) -> AvailabilityPreset | None:
    """Choose by matched keyword count, then user-configured priority."""
    folded = job_text.casefold()
    ranked = [
        (
            sum(
                1
                for keyword in preset.match_keywords
                if keyword.strip() and keyword.casefold() in folded
            ),
            preset.priority,
            index,
            preset,
        )
        for index, preset in enumerate(profile.availability_presets)
    ]
    matches = [item for item in ranked if item[0] > 0]
    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1], -item[2]))[3]


def suggested_job_override(
    profile: CandidateProfileData,
    job_text: str,
    *,
    location: str = "",
    reference_date: date | None = None,
    preset_name: str | None = None,
) -> JobContextOverride:
    selected = next(
        (
            preset
            for preset in profile.availability_presets
            if preset.name == preset_name
        ),
        None,
    )
    if preset_name is None:
        selected = select_availability_preset(profile, job_text)
    availability = selected or profile.default_availability
    start_within_days = availability.start_within_days
    start_date = (
        (reference_date or date.today()) + timedelta(days=start_within_days)
        if start_within_days is not None
        else profile.default_availability.start_date
    )
    duration_months = (
        availability.duration_months
        if availability.duration_months is not None
        else profile.default_availability.duration_months
    )
    days_per_week = (
        availability.days_per_week
        if availability.days_per_week is not None
        else profile.default_availability.days_per_week
    )
    onsite_available = (
        availability.onsite_available
        if availability.onsite_available is not None
        else profile.default_availability.onsite_available
    )
    cities = list(getattr(availability, "cities", []))
    if not cities and location.strip():
        cities = [location.strip()]
    return JobContextOverride(
        enabled=True,
        preset_name=selected.name if selected else "常规默认",
        start_date=start_date,
        duration_months=duration_months,
        days_per_week=days_per_week,
        onsite_available=onsite_available,
        work_modes=["现场"] if onsite_available else [],
        cities=cities,
    )


def _parse_int(value: str) -> int | None:
    digits = "".join(character for character in value if character.isdigit())
    return int(digits) if digits else None


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except (TypeError, ValueError):
        return None


def _months_between(start: date, end: date) -> float:
    return max(0, (end - start).days / 30.4375)


def effective_windows(
    profile: CandidateProfileData,
    override: JobContextOverride,
) -> tuple[list[AvailabilityWindow], str]:
    if override.enabled:
        if override.start_date and override.end_date and override.days_per_week:
            window = AvailabilityWindow(
                start_date=override.start_date,
                end_date=override.end_date,
                days_per_week=override.days_per_week,
                onsite_days_per_week=override.onsite_days_per_week,
                work_modes=override.work_modes,
                cities=override.cities,
                notes=override.notes,
            )
            return [window], "本岗位临时补充"
        if profile.availability_windows:
            merged = [
                AvailabilityWindow(
                    start_date=override.start_date or window.start_date,
                    end_date=override.end_date or window.end_date,
                    days_per_week=override.days_per_week or window.days_per_week,
                    onsite_days_per_week=(
                        override.onsite_days_per_week
                        if override.onsite_days_per_week is not None
                        else window.onsite_days_per_week
                    ),
                    max_hours_per_week=window.max_hours_per_week,
                    work_modes=override.work_modes or window.work_modes,
                    cities=override.cities or window.cities,
                    notes=override.notes or window.notes,
                )
                for window in profile.availability_windows
            ]
            return merged, "本岗位临时补充"
        if any(
            value is not None
            for value in (
                override.start_date,
                override.end_date,
                override.duration_months,
                override.days_per_week,
                override.onsite_available,
            )
        ) or override.work_modes or override.cities:
            return [], "本岗位临时补充"
    return profile.availability_windows, "求职档案"


def build_candidate_context(
    profile: CandidateProfileData,
    override: JobContextOverride,
) -> dict[str, Any]:
    windows, source = effective_windows(profile, override)
    return {
        "facts": profile.facts.model_dump(mode="json"),
        "availability_windows": [
            {**window.model_dump(mode="json"), "source": source}
            for window in windows
        ],
        "default_availability": profile.default_availability.model_dump(mode="json"),
        "preferences": profile.preferences.model_dump(mode="json"),
        "communication_preferences": profile.communication.model_dump(mode="json"),
        "job_override": override.model_dump(mode="json"),
        "fact_rules": [
            "缺失信息必须视为未知，不得推测。",
            "经历、项目和技能只能引用简历。",
            "到岗、实习期限、每周天数和地点优先引用本岗位补充，其次引用求职档案。",
        ],
    }


def _check_days(
    constraint: JDConstraint,
    windows: list[AvailabilityWindow],
    profile: CandidateProfileData,
    override: JobContextOverride,
    source: str,
) -> EligibilityCheck:
    required = _parse_int(constraint.value)
    committed = (
        override.days_per_week
        if override.enabled and override.days_per_week is not None
        else profile.default_availability.days_per_week
    )
    available_days = (
        max(window.days_per_week for window in windows)
        if windows
        else committed
    )
    if required is None or available_days is None:
        return EligibilityCheck(
            constraint_type=constraint.constraint_type,
            requirement=constraint.quote or constraint.value,
            result="存疑",
            source=source if committed or windows else "",
            notes="JD要求或个人可用天数信息不完整。",
        )
    result = "满足" if available_days >= required else "不满足"
    return EligibilityCheck(
        constraint_type=constraint.constraint_type,
        requirement=constraint.quote or f"每周至少{required}天",
        result=result,
        candidate_evidence=f"可每周实习{available_days}天",
        source=source,
    )


def _check_duration(
    constraint: JDConstraint,
    windows: list[AvailabilityWindow],
    profile: CandidateProfileData,
    override: JobContextOverride,
    source: str,
) -> EligibilityCheck:
    required = _parse_int(constraint.value)
    committed = (
        override.duration_months
        if override.enabled and override.duration_months is not None
        else profile.default_availability.duration_months
    )
    available_months = (
        max(_months_between(window.start_date, window.end_date) for window in windows)
        if windows
        else committed
    )
    if required is None or available_months is None:
        return EligibilityCheck(
            constraint_type=constraint.constraint_type,
            requirement=constraint.quote or constraint.value,
            result="存疑",
            source=source if committed or windows else "",
            notes="JD要求或个人可实习区间信息不完整。",
        )
    result = "满足" if available_months >= required else "不满足"
    return EligibilityCheck(
        constraint_type=constraint.constraint_type,
        requirement=constraint.quote or f"连续实习至少{required}个月",
        result=result,
        candidate_evidence=f"可连续实习约{available_months:g}个月",
        source=source,
    )


def _check_arrival(
    constraint: JDConstraint,
    windows: list[AvailabilityWindow],
    profile: CandidateProfileData,
    override: JobContextOverride,
    source: str,
) -> EligibilityCheck:
    required = _parse_date(constraint.value)
    committed = (
        override.start_date
        if override.enabled and override.start_date
        else profile.default_availability.start_date
    )
    earliest = min(
        [window.start_date for window in windows]
        + ([committed] if committed else [])
    ) if windows or committed else None
    if required is None or earliest is None:
        return EligibilityCheck(
            constraint_type=constraint.constraint_type,
            requirement=constraint.quote or constraint.value,
            result="存疑",
            source=source if committed or windows else "",
            notes="到岗日期未使用 YYYY-MM-DD 提取，或个人档案尚未填写。",
        )
    operator = constraint.operator.strip()
    if operator in {"不晚于", "<=", "最晚"}:
        met = earliest <= required
    elif operator in {"不早于", ">=", "最早"}:
        met = earliest >= required
    else:
        met = earliest <= required
    return EligibilityCheck(
        constraint_type=constraint.constraint_type,
        requirement=constraint.quote or constraint.value,
        result="满足" if met else "不满足",
        candidate_evidence=f"最早可于{earliest.isoformat()}到岗",
        source=source,
    )


def _check_location(
    constraint: JDConstraint,
    windows: list[AvailabilityWindow],
    profile: CandidateProfileData,
    source: str,
) -> EligibilityCheck:
    required = constraint.value.strip()
    if (
        profile.preferences.accept_any_city is True
        or profile.preferences.willing_to_relocate is True
    ):
        return EligibilityCheck(
            constraint_type=constraint.constraint_type,
            requirement=constraint.quote or required,
            result="满足",
            candidate_evidence="已确认可以异地实习并为岗位城市搬迁",
            source="求职档案",
        )
    cities = {
        city.casefold()
        for window in windows
        for city in window.cities
        if city.strip()
    }
    cities.update(
        city.casefold()
        for city in profile.preferences.preferred_cities
        if city.strip()
    )
    if not required or not cities:
        return EligibilityCheck(
            constraint_type=constraint.constraint_type,
            requirement=constraint.quote or required,
            result="存疑",
            source=source if cities else "",
            notes="个人档案尚未确认可接受城市。",
        )
    met = any(city in required.casefold() or required.casefold() in city for city in cities)
    return EligibilityCheck(
        constraint_type=constraint.constraint_type,
        requirement=constraint.quote or required,
        result="满足" if met else "不满足",
        candidate_evidence="可接受城市：" + "、".join(sorted(cities)),
        source=source,
    )


def _check_work_mode(
    constraint: JDConstraint,
    windows: list[AvailabilityWindow],
    profile: CandidateProfileData,
    override: JobContextOverride,
) -> EligibilityCheck:
    requirement = (constraint.quote or constraint.value).casefold()
    modes = {
        mode
        for window in windows
        for mode in window.work_modes
    }
    modes.update(profile.preferences.acceptable_work_modes)
    modes.update(override.work_modes if override.enabled else [])
    onsite = (
        profile.default_availability.onsite_available is True
        or (
            override.enabled
            and override.onsite_available is True
        )
        or "现场" in modes
        or profile.preferences.willing_to_relocate is True
    )
    if "最多" in requirement and "远程" in requirement and onsite:
        return EligibilityCheck(
            constraint_type="work_mode",
            requirement=constraint.quote or constraint.value,
            result="满足",
            candidate_evidence="已确认可全程现场实习，不会超过远程天数上限",
            source="本岗位临时补充" if override.enabled else "求职档案",
        )
    if any(term in requirement for term in ["不接受远程", "现场", "坐班", "线下"]):
        if onsite:
            return EligibilityCheck(
                constraint_type="work_mode",
                requirement=constraint.quote or constraint.value,
                result="满足",
                candidate_evidence="已确认可以现场实习",
                source="求职档案",
            )
        return EligibilityCheck(
            constraint_type="work_mode",
            requirement=constraint.quote or constraint.value,
            result="存疑",
            notes="尚未确认是否可以现场实习。",
        )
    required = constraint.value.strip()
    if required and required in modes:
        return EligibilityCheck(
            constraint_type="work_mode",
            requirement=constraint.quote or required,
            result="满足",
            candidate_evidence="可接受工作方式：" + "、".join(sorted(modes)),
            source="求职档案",
        )
    return EligibilityCheck(
        constraint_type="work_mode",
        requirement=constraint.quote or required,
        result="存疑",
        notes="尚未确认可接受的工作方式。",
    )


def _check_education(
    constraint: JDConstraint,
    profile: CandidateProfileData,
) -> EligibilityCheck:
    requirement = constraint.quote or constraint.value
    folded = requirement.casefold()
    required_tiers = [
        tier
        for tier in ["985", "211", "双一流", "QS前50", "QS前100"]
        if tier.casefold() in folded
    ]
    candidate_tiers = profile.facts.institution_tiers
    tier_met = not required_tiers or bool(set(required_tiers) & set(candidate_tiers))
    required_degrees = [
        degree for degree in ["本科", "硕士", "博士"] if degree in requirement
    ]
    degree_met = (
        not required_degrees
        or any(degree in profile.facts.highest_education for degree in required_degrees)
    )
    requires_student = "在校" in requirement
    student_met = not requires_student or "在校" in profile.facts.student_status
    known = (
        (not required_tiers or bool(candidate_tiers))
        and (not required_degrees or bool(profile.facts.highest_education))
        and (not requires_student or bool(profile.facts.student_status))
    )
    evidence_parts = [
        item
        for item in [
            profile.facts.institution,
            "、".join(candidate_tiers),
            profile.facts.highest_education,
            profile.facts.student_status,
        ]
        if item
    ]
    if known and tier_met and degree_met and student_met:
        result = "满足"
        notes = ""
    elif known and (not tier_met or not degree_met or not student_met):
        result = "不满足"
        notes = "已确认的学校层级、学历或学生状态不符合该必须条件。"
    else:
        result = "存疑"
        notes = "学校层级、学历或学生状态尚未完整确认。"
    return EligibilityCheck(
        constraint_type=constraint.constraint_type,
        requirement=requirement,
        result=result,
        candidate_evidence="；".join(evidence_parts),
        source="求职档案" if evidence_parts else "",
        notes=notes,
    )


def _check_simple_fact(
    constraint: JDConstraint,
    candidate_value: str,
    source: str,
) -> EligibilityCheck:
    required = constraint.value.strip()
    if not required or not candidate_value.strip():
        return EligibilityCheck(
            constraint_type=constraint.constraint_type,
            requirement=constraint.quote or required,
            result="存疑",
            notes="个人档案尚未确认该项信息。",
        )
    normalized_required = required.casefold()
    normalized_candidate = candidate_value.casefold()
    met = (
        normalized_required in normalized_candidate
        or normalized_candidate in normalized_required
    )
    return EligibilityCheck(
        constraint_type=constraint.constraint_type,
        requirement=constraint.quote or required,
        result="满足" if met else "存疑",
        candidate_evidence=candidate_value,
        source=source,
        notes="" if met else "需要人工确认该表述是否等价。",
    )


def _check_major(
    constraint: JDConstraint,
    profile: CandidateProfileData,
) -> EligibilityCheck:
    requirement = constraint.quote or constraint.value
    candidate = profile.facts.major.strip()
    if not requirement.strip() or not candidate:
        return EligibilityCheck(
            constraint_type="major",
            requirement=requirement,
            result="存疑",
            notes="个人档案尚未确认专业信息。",
        )
    cleaned = re.sub(
        r"(等)?(相关|相近)?专业|专业背景|方向",
        "",
        constraint.value,
        flags=re.IGNORECASE,
    )
    alternatives = [
        item.strip()
        for item in re.split(r"[、,，/]|(?:or)|或者|或", cleaned, flags=re.IGNORECASE)
        if item.strip()
    ]
    folded_candidate = candidate.casefold()
    met = any(
        item.casefold() in folded_candidate
        or folded_candidate in item.casefold()
        for item in alternatives
    )
    return EligibilityCheck(
        constraint_type="major",
        requirement=requirement,
        result="满足" if met else "存疑",
        candidate_evidence=candidate,
        source="求职档案",
        notes="" if met else "专业名称未直接命中，建议人工确认是否属于相关专业。",
    )


def _check_language(
    constraint: JDConstraint,
    profile: CandidateProfileData,
) -> EligibilityCheck:
    requirement = constraint.quote or constraint.value
    candidate = "、".join(profile.facts.languages)
    if not requirement.strip() or not candidate:
        return EligibilityCheck(
            constraint_type="language",
            requirement=requirement,
            result="存疑",
            notes="个人档案尚未确认语言能力。",
        )
    required = requirement.casefold()
    known = candidate.casefold()
    asks_english = any(
        token in required for token in ["英语", "英文", "english", "cet", "雅思", "ielts"]
    )
    has_english = any(
        token in known for token in ["英语", "英文", "english", "cet", "雅思", "ielts"]
    )
    asks_fluent = any(token in required for token in ["流利", "工作语言", "fluent"])
    has_working_level = any(
        token in known
        for token in ["工作语言", "流利", "雅思7", "ielts 7", "cet-6 6"]
    )
    met = (asks_english and has_english and (not asks_fluent or has_working_level))
    return EligibilityCheck(
        constraint_type="language",
        requirement=requirement,
        result="满足" if met else "存疑",
        candidate_evidence=candidate,
        source="求职档案",
        notes="" if met else "语言要求与已确认成绩/能力表述不能自动等价。",
    )


def evaluate_eligibility(
    constraints: list[JDConstraint],
    profile: CandidateProfileData,
    override: JobContextOverride,
) -> EligibilityAssessment:
    windows, availability_source = effective_windows(profile, override)
    checks: list[EligibilityCheck] = []
    for constraint in constraints:
        if constraint.importance != "必须":
            continue
        if constraint.constraint_type == "days_per_week":
            check = _check_days(
                constraint, windows, profile, override, availability_source
            )
        elif constraint.constraint_type == "duration_months":
            check = _check_duration(
                constraint, windows, profile, override, availability_source
            )
        elif constraint.constraint_type == "arrival_date":
            check = _check_arrival(
                constraint, windows, profile, override, availability_source
            )
        elif constraint.constraint_type == "location":
            check = _check_location(
                constraint, windows, profile, availability_source
            )
        elif constraint.constraint_type == "work_mode":
            check = _check_work_mode(constraint, windows, profile, override)
        elif constraint.constraint_type == "graduation_year":
            graduation = (
                str(profile.facts.graduation_date.year)
                if profile.facts.graduation_date
                else ""
            )
            check = _check_simple_fact(constraint, graduation, "求职档案")
        elif constraint.constraint_type in {
            "education", "institution_tier", "student_status"
        }:
            check = _check_education(constraint, profile)
        elif constraint.constraint_type == "major":
            check = _check_major(constraint, profile)
        elif constraint.constraint_type == "language":
            check = _check_language(constraint, profile)
        elif constraint.constraint_type == "work_authorization":
            check = _check_simple_fact(
                constraint, profile.facts.work_authorization, "求职档案"
            )
        else:
            check = EligibilityCheck(
                constraint_type=constraint.constraint_type,
                requirement=constraint.quote or constraint.value,
                result="存疑",
                notes="该要求暂不支持本地自动判断，需要人工确认。",
            )
        checks.append(check)

    required_days_constraint = next(
        (
            item
            for item in constraints
            if item.importance == "必须"
            and item.constraint_type == "days_per_week"
            and _parse_int(item.value) is not None
        ),
        None,
    )
    required_duration_constraint = next(
        (
            item
            for item in constraints
            if item.importance == "必须"
            and item.constraint_type == "duration_months"
            and _parse_int(item.value) is not None
        ),
        None,
    )
    required_arrival_constraint = next(
        (
            item
            for item in constraints
            if item.importance == "必须"
            and item.constraint_type == "arrival_date"
            and _parse_date(item.value) is not None
        ),
        None,
    )
    combined_constraints = [
        item
        for item in (
            required_days_constraint,
            required_duration_constraint,
            required_arrival_constraint,
        )
        if item is not None
    ]
    if len(combined_constraints) >= 2 and windows:
        required_days = (
            _parse_int(required_days_constraint.value)
            if required_days_constraint
            else None
        )
        required_duration = (
            _parse_int(required_duration_constraint.value)
            if required_duration_constraint
            else None
        )
        required_arrival = (
            _parse_date(required_arrival_constraint.value)
            if required_arrival_constraint
            else None
        )

        def window_meets_all(window: AvailabilityWindow) -> bool:
            if required_days is not None and window.days_per_week < required_days:
                return False
            if (
                required_duration is not None
                and _months_between(window.start_date, window.end_date)
                < required_duration
            ):
                return False
            if required_arrival is not None:
                operator = required_arrival_constraint.operator.strip()
                if operator in {"不早于", ">=", "最早"}:
                    if window.start_date < required_arrival:
                        return False
                elif window.start_date > required_arrival:
                    return False
            return True

        combined_met = any(window_meets_all(window) for window in windows)
        requirement_parts = []
        if required_arrival is not None:
            requirement_parts.append(f"{required_arrival.isoformat()}前后到岗")
        if required_duration is not None:
            requirement_parts.append(f"连续至少{required_duration}个月")
        if required_days is not None:
            requirement_parts.append(f"全程每周至少{required_days}天")
        evidence = "；".join(
            f"{window.start_date.isoformat()}至{window.end_date.isoformat()}，"
            f"每周{window.days_per_week}天"
            for window in windows
        )
        checks = [
            check
            for check in checks
            if check.constraint_type
            not in {"arrival_date", "duration_months", "days_per_week"}
        ]
        checks.append(
            EligibilityCheck(
                constraint_type="availability_combined",
                requirement="，".join(requirement_parts),
                result=(
                    "满足"
                    if combined_met
                    else ("不满足" if windows else "存疑")
                ),
                candidate_evidence=evidence,
                source=availability_source if windows else "",
                notes=(
                    ""
                    if combined_met
                    else "没有一个连续时间段能同时满足这些到岗条件。"
                ),
            )
        )

    met_count = sum(check.result == "满足" for check in checks)
    unmet_count = sum(check.result == "不满足" for check in checks)
    unknown_count = sum(check.result == "存疑" for check in checks)
    negotiable_count = sum(check.result == "可能协商" for check in checks)
    if not checks:
        summary = "JD 未提取到可自动核对的必须条件。"
    else:
        summary = (
            f"必须条件共{len(checks)}项：满足{met_count}项，"
            f"不满足{unmet_count}项，存疑{unknown_count}项。"
        )
    return EligibilityAssessment(
        checks=checks,
        met_count=met_count,
        unmet_count=unmet_count,
        unknown_count=unknown_count,
        negotiable_count=negotiable_count,
        summary=summary,
    )
