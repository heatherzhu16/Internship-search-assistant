from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


WorkMode = Literal["现场", "混合", "远程"]
ConstraintImportance = Literal["必须", "优先", "加分"]

KNOWN_CONSTRAINT_TYPES = {
    "arrival_date",
    "days_per_week",
    "duration_months",
    "location",
    "work_mode",
    "graduation_year",
    "education",
    "institution_tier",
    "student_status",
    "major",
    "language",
    "work_authorization",
    "other",
}
CONSTRAINT_TYPE_ALIASES = {
    "arrival": "arrival_date",
    "start_date": "arrival_date",
    "available_date": "arrival_date",
    "days": "days_per_week",
    "weekly_days": "days_per_week",
    "work_days": "days_per_week",
    "duration": "duration_months",
    "internship_duration": "duration_months",
    "city": "location",
    "base": "location",
    "onsite": "work_mode",
    "remote": "work_mode",
    "graduation": "graduation_year",
    "degree": "education",
    "school_tier": "institution_tier",
    "school": "institution_tier",
    "status": "student_status",
    "field_of_study": "major",
    "english": "language",
    "visa": "work_authorization",
}


class CandidateFacts(BaseModel):
    institution: str = ""
    institution_tiers: list[str] = Field(default_factory=list)
    graduation_date: date | None = None
    student_status: str = ""
    highest_education: str = ""
    major: str = ""
    languages: list[str] = Field(default_factory=list)
    work_authorization: str = ""
    requires_sponsorship: bool | None = None


class AvailabilityWindow(BaseModel):
    start_date: date
    end_date: date
    days_per_week: int = Field(ge=1, le=7)
    onsite_days_per_week: int | None = Field(default=None, ge=0, le=7)
    max_hours_per_week: int | None = Field(default=None, ge=1, le=168)
    work_modes: list[WorkMode] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def validate_window(self) -> "AvailabilityWindow":
        if self.end_date < self.start_date:
            raise ValueError("可用时间的结束日期不能早于开始日期。")
        if (
            self.onsite_days_per_week is not None
            and self.onsite_days_per_week > self.days_per_week
        ):
            raise ValueError("现场到岗天数不能超过每周总可工作天数。")
        return self


class DefaultAvailability(BaseModel):
    start_date: date | None = None
    start_within_days: int | None = Field(default=None, ge=0, le=60)
    duration_months: int | None = Field(default=None, ge=1, le=24)
    days_per_week: int | None = Field(default=None, ge=1, le=7)
    onsite_available: bool | None = None
    notes: str = ""


class AvailabilityPreset(BaseModel):
    name: str
    priority: int = Field(default=0, ge=0, le=100)
    match_keywords: list[str] = Field(default_factory=list)
    start_within_days: int | None = Field(default=None, ge=0, le=60)
    duration_months: int | None = Field(default=None, ge=1, le=24)
    days_per_week: int | None = Field(default=None, ge=1, le=7)
    onsite_available: bool | None = None
    cities: list[str] = Field(default_factory=list)
    notes: str = ""


def default_availability_presets() -> list[AvailabilityPreset]:
    return [
        AvailabilityPreset(
            name="互联网",
            priority=10,
            match_keywords=["互联网", "科技", "电商", "平台", "产品", "运营", "用户"],
            start_within_days=7,
            duration_months=6,
            onsite_available=True,
        ),
        AvailabilityPreset(
            name="券商",
            priority=20,
            match_keywords=["券商", "证券", "投行", "研究所", "IPO"],
            start_within_days=7,
            duration_months=3,
            onsite_available=True,
        ),
    ]


class JobPreferences(BaseModel):
    target_roles: list[str] = Field(default_factory=list)
    preferred_cities: list[str] = Field(default_factory=list)
    target_companies: list[str] = Field(default_factory=list)
    excluded_companies: list[str] = Field(default_factory=list)
    company_filter_mode: Literal["不限", "仅目标公司"] = "不限"
    acceptable_work_modes: list[WorkMode] = Field(default_factory=list)
    willing_to_relocate: bool | None = None
    accept_any_city: bool | None = None
    internship_types: list[str] = Field(default_factory=list)


class CommunicationPreferences(BaseModel):
    include_start_date_by_default: bool = True
    include_days_per_week_by_default: bool = True
    include_duration_by_default: bool = True


class CandidateProfileData(BaseModel):
    schema_version: int = 4
    facts: CandidateFacts = Field(default_factory=CandidateFacts)
    default_availability: DefaultAvailability = Field(
        default_factory=DefaultAvailability
    )
    availability_presets: list[AvailabilityPreset] = Field(
        default_factory=default_availability_presets
    )
    availability_windows: list[AvailabilityWindow] = Field(default_factory=list)
    preferences: JobPreferences = Field(default_factory=JobPreferences)
    communication: CommunicationPreferences = Field(
        default_factory=CommunicationPreferences
    )
    last_confirmed_at: datetime | None = None


class JobContextOverride(BaseModel):
    enabled: bool = False
    preset_name: str = ""
    start_date: date | None = None
    end_date: date | None = None
    duration_months: int | None = Field(default=None, ge=1, le=24)
    days_per_week: int | None = Field(default=None, ge=1, le=7)
    onsite_days_per_week: int | None = Field(default=None, ge=0, le=7)
    onsite_available: bool | None = None
    work_modes: list[WorkMode] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def validate_override(self) -> "JobContextOverride":
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("本岗位特殊安排的结束日期不能早于开始日期。")
        if (
            self.days_per_week is not None
            and self.onsite_days_per_week is not None
            and self.onsite_days_per_week > self.days_per_week
        ):
            raise ValueError("本岗位现场到岗天数不能超过每周总可工作天数。")
        return self


class JDConstraint(BaseModel):
    constraint_type: Literal[
        "arrival_date",
        "days_per_week",
        "duration_months",
        "location",
        "work_mode",
        "graduation_year",
        "education",
        "institution_tier",
        "student_status",
        "major",
        "language",
        "work_authorization",
        "other",
    ] = "other"
    operator: str = ""
    value: str = ""
    unit: str = ""
    importance: ConstraintImportance = "必须"
    quote: str = ""

    @field_validator("constraint_type", mode="before")
    @classmethod
    def normalize_constraint_type(cls, value) -> str:
        """Keep occasional model-invented enum values from breaking JD extraction."""
        normalized = (
            str(value or "other")
            .strip()
            .casefold()
            .replace("-", "_")
            .replace(" ", "_")
        )
        normalized = CONSTRAINT_TYPE_ALIASES.get(normalized, normalized)
        return normalized if normalized in KNOWN_CONSTRAINT_TYPES else "other"

    @field_validator("importance", mode="before")
    @classmethod
    def normalize_importance(cls, value) -> str:
        """Normalize common English and Chinese variants returned by the model."""
        normalized = str(value or "").strip().casefold()
        if any(marker in normalized for marker in ("优先", "preferred", "preference")):
            return "优先"
        if any(marker in normalized for marker in ("加分", "bonus", "plus")):
            return "加分"
        return "必须"


class EligibilityCheck(BaseModel):
    constraint_type: str
    requirement: str
    result: Literal["满足", "不满足", "存疑", "可能协商"]
    candidate_evidence: str = ""
    source: str = ""
    notes: str = ""


class EligibilityAssessment(BaseModel):
    checks: list[EligibilityCheck] = Field(default_factory=list)
    met_count: int = 0
    unmet_count: int = 0
    unknown_count: int = 0
    negotiable_count: int = 0
    summary: str = ""


def split_items(value: str) -> list[str]:
    normalized = value.replace("，", ",").replace("、", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]
