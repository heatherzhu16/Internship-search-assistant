from __future__ import annotations

import re

from candidate_profile import CandidateProfileData, JobContextOverride
from constraint_engine import (
    build_candidate_context,
    evaluate_eligibility,
    suggested_job_override,
)
from profile_store import save_evaluation_run
from scoring_policy import calibrate_decision
from services.database import DB_PATH
from services.discovery_service import (
    get_discovery_item,
    sanitize_platform_text,
    save_discovery_evaluation,
)
from services.professional_gate import augment_professional_eligibility
from services.discovery_quality import CONTENT_LEVEL_LABELS
from services.resume_service import get_resume_version
from services.scoring import (
    RUBRIC_VERSION,
    evaluate_match,
    extract_jd,
    jd_hash,
    total_score,
)


def prepare_discovery_jd_text(raw_text: str, item: dict) -> str:
    """Remove platform navigation/footer while retaining structured job metadata."""
    compact = sanitize_platform_text(raw_text).strip()
    if "职位描述" not in compact:
        return compact
    body_match = re.search(
        r"职位描述\s*[：:]?(.*?)(?:投递要求\s*[：:]|公司简介|产品服务)",
        compact,
        flags=re.DOTALL,
    )
    if not body_match:
        return compact
    body = body_match.group(1).strip()
    header = compact[: body_match.start()]
    metadata_patterns = [
        r"\d+(?:-\d+)?/天",
        r"[1-7]\s*天\s*[／/]\s*周",
        r"实习\s*\d+\s*个月",
        r"(?:本科|硕士|博士)(?:及以上)?",
    ]
    metadata = []
    for pattern in metadata_patterns:
        match = re.search(pattern, header, flags=re.IGNORECASE)
        if match and match.group(0) not in metadata:
            metadata.append(match.group(0))
    structured_metadata = [
        ("公司", item.get("company")),
        ("职位", item.get("role") or item.get("title")),
        ("工作地点", item.get("location")),
        ("薪资", item.get("salary")),
    ]
    for label, value in structured_metadata:
        clean_value = str(value or "").strip()
        entry = f"{label}：{clean_value}" if clean_value else ""
        if entry and entry not in metadata:
            metadata.append(entry)
    return "\n".join(["；".join(metadata), body]).strip()


def score_discovery_item(
    *,
    item_id: int,
    client,
    model: str,
    resume_version_id: int,
    profile_version_id: int,
    profile: CandidateProfileData,
    job_override: JobContextOverride | None = None,
) -> dict:
    item = get_discovery_item(item_id)
    if item.get("content_level") != "full":
        label = CONTENT_LEVEL_LABELS.get(
            str(item.get("content_level") or ""), "详情不完整"
        )
        raise ValueError(
            f"该岗位目前是“{label}”，缺少完整职责或任职要求，不能生成正式匹配分。"
        )
    resume = get_resume_version(resume_version_id)
    raw_text = str(item.get("raw_text") or "").strip()
    if len(raw_text) < 40:
        raise ValueError("该线索提取到的岗位文字过少，请先打开原链接人工确认。")
    scoring_text = prepare_discovery_jd_text(raw_text, item)
    jd = extract_jd(client, model, scoring_text)
    jd.company = jd.company or str(item.get("company") or "")
    jd.role = jd.role or str(item.get("role") or item.get("title") or "")
    jd.location = jd.location or str(item.get("location") or "")
    jd.salary = jd.salary or str(item.get("salary") or "")
    jd.source = jd.source or str(item.get("platform") or "")
    jd.full_text = scoring_text

    override = job_override or suggested_job_override(
        profile,
        scoring_text,
        location=jd.location,
    )
    context = build_candidate_context(profile, override)
    eligibility = evaluate_eligibility(jd.constraints, profile, override)
    eligibility = augment_professional_eligibility(
        eligibility,
        jd_text=jd.full_text,
        resume_text=resume.extracted_text,
    )
    evaluation = evaluate_match(
        client,
        model,
        resume.extracted_text,
        jd,
        context,
        eligibility,
    )
    decision = calibrate_decision(
        total_score(evaluation),
        eligibility,
        model_recommendation=evaluation.recommendation,
        model_reason=evaluation.recommendation_reason,
        model_gate_result=evaluation.hard_gate_result,
        model_gate_notes=evaluation.hard_gate_notes,
    )
    evaluation.hard_gate_result = decision.gate_result
    evaluation.recommendation = decision.final_recommendation
    evaluation.recommendation_reason = decision.final_reason
    run_id = save_evaluation_run(
        DB_PATH,
        jd_hash=jd_hash(jd),
        resume_hash=resume.sha256,
        profile_version_id=profile_version_id,
        job_override=override.model_dump(mode="json"),
        input_snapshot={
            "jd": jd.model_dump(mode="json"),
            "candidate_context": context,
            "discovery_item_id": item_id,
            "source_url": item.get("canonical_url", ""),
        },
        rubric_version=RUBRIC_VERSION,
        model=model,
        output={
            "evaluation": evaluation.model_dump(mode="json"),
            "eligibility": eligibility.model_dump(mode="json"),
            "decision": decision.model_dump(mode="json"),
        },
        resume_version_id=resume.id,
        company=jd.company,
        role=jd.role,
        jd_text=jd.full_text,
        fit_score=total_score(evaluation),
        gate_result=decision.gate_result,
        recommendation=decision.final_recommendation,
    )
    evaluation_id = save_discovery_evaluation(
        item_id=item_id,
        evaluation_run_id=run_id,
        resume_version_id=resume.id,
        rubric_version=RUBRIC_VERSION,
        fit_score=total_score(evaluation),
        gate_result=decision.gate_result,
        recommendation=decision.final_recommendation,
        jd_json=jd.model_dump(mode="json"),
        eligibility_json=eligibility.model_dump(mode="json"),
        evaluation_json=evaluation.model_dump(mode="json"),
    )
    return {
        "evaluation_id": evaluation_id,
        "evaluation_run_id": run_id,
        "fit_score": total_score(evaluation),
        "gate_result": decision.gate_result,
        "recommendation": decision.final_recommendation,
        "reason": decision.final_reason,
    }
