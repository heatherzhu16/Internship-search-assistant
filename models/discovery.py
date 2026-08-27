from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


DiscoveryPlatform = Literal["shixiseng", "boss", "xiaohongshu"]
DiscoveryStatus = Literal[
    "new",
    "needs_review",
    "needs_details",
    "not_recruitment",
    "hard_gate_failed",
    "scored",
    "shortlisted",
    "dismissed",
    "imported",
    "collection_failed",
    "expired",
]

PLATFORM_LABELS: dict[str, str] = {
    "shixiseng": "实习僧",
    "boss": "BOSS 直聘",
    "xiaohongshu": "小红书",
}


class CollectedItem(BaseModel):
    platform: DiscoveryPlatform
    external_id: str
    url: str
    access_url: str = ""
    title: str = ""
    company: str = ""
    role: str = ""
    location: str = ""
    salary: str = ""
    posted_at: str = ""
    author: str = ""
    raw_text: str = ""
    source_keyword: str = ""
    snapshot_path: str = ""
    capture_method: str = "platform_scan"
    manual_decision: str = ""
    availability_status: str = ""
    capture_kind: str = ""


class ScanLimits(BaseModel):
    max_items_per_keyword: int = Field(default=8, ge=1, le=20)
    max_details: int = Field(default=20, ge=1, le=50)
    timeout_seconds: int = Field(default=300, ge=30, le=600)
    target_companies: list[str] = Field(default_factory=list)
    excluded_companies: list[str] = Field(default_factory=list)
    company_filter_mode: str = "不限"


class CollectorResult(BaseModel):
    platform: DiscoveryPlatform
    keyword: str
    items: list[CollectedItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: str = ""
    login_required: bool = False
    verification_required: bool = False
    prefiltered_count: int = 0


class DiscoverySaveResult(BaseModel):
    run_id: int
    scanned: int = 0
    inserted: int = 0
    duplicates: int = 0
    recruitment_items: int = 0
    filtered_items: int = 0
    incomplete_items: int = 0
    new_item_ids: list[int] = Field(default_factory=list)
    score_candidate_ids: list[int] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
