from __future__ import annotations

from pydantic import BaseModel


APPLICATION_STATUSES = [
    "待投递", "已投递", "笔试", "一面", "二面", "终面", "Offer", "拒绝", "放弃"
]
PROGRESS_STAGES = ["待投递", "已投递", "笔试", "一面", "二面", "终面", "Offer"]
STAGE_RANK = {stage: index for index, stage in enumerate(PROGRESS_STAGES)}
TERMINAL_STATUSES = {"Offer", "拒绝", "放弃"}


class ApplicationEventInput(BaseModel):
    application_id: int
    event_type: str = "状态变更"
    new_status: str
    source: str = "手动确认"
    occurred_at: str
    resume_version_id: int | None = None
    external_id: str = ""


def status_to_stage(status: str) -> str:
    if status in PROGRESS_STAGES:
        return status
    if status in {"拒绝", "放弃"}:
        return "已投递"
    return "待投递"
