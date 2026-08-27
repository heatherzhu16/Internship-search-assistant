from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


EmailDirection = Literal["outgoing", "incoming"]
EmailClassification = Literal[
    "application_sent",
    "application_received",
    "assessment",
    "interview_1",
    "interview_2",
    "interview_final",
    "interview_unknown",
    "rejection",
    "offer",
    "other",
]


class EmailConnectionConfig(BaseModel):
    address: str
    authorization_code: str
    host: str = "imap.163.com"
    port: int = 993
    sent_folder: str = "已发送"
    incoming_folder: str = "求职同步"
    draft_folder: str = "草稿箱"
    max_messages_per_folder: int = Field(default=100, ge=1, le=500)
    auto_apply_high_confidence: bool = False


class MailboxInfo(BaseModel):
    display_name: str
    wire_name: str
    flags: list[str] = Field(default_factory=list)
    is_sent: bool = False
    is_draft: bool = False


class NormalizedEmail(BaseModel):
    folder_name: str
    direction: EmailDirection
    uid_validity: int
    imap_uid: int
    message_id: str = ""
    received_at: datetime
    from_address: str = ""
    to_addresses: list[str] = Field(default_factory=list)
    subject: str = ""
    body_text: str = ""
    body_excerpt: str = ""
    body_hash: str
    attachment_names: list[str] = Field(default_factory=list)


class ClassificationResult(BaseModel):
    classification: EmailClassification
    suggested_status: str = ""
    confidence: float = Field(ge=0, le=1)
    reason_codes: list[str] = Field(default_factory=list)


class MatchResult(BaseModel):
    application_id: int | None = None
    confidence: float = Field(default=0, ge=0, le=1)
    reason_codes: list[str] = Field(default_factory=list)
    candidate_ids: list[int] = Field(default_factory=list)


class ScanSummary(BaseModel):
    run_id: int
    scanned: int = 0
    new_messages: int = 0
    duplicates: int = 0
    pending: int = 0
    unmatched: int = 0
    auto_applied: int = 0
    ignored_non_job: int = 0
    errors: list[str] = Field(default_factory=list)
