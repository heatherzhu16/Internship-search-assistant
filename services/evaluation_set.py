from __future__ import annotations

import json
import re
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from candidate_profile import CandidateProfileData, EligibilityAssessment, EligibilityCheck
from constraint_engine import build_candidate_context, evaluate_eligibility, suggested_job_override
from models.evaluation import JDInfo, RUBRIC_VERSION, WEIGHTS
from scoring_policy import calibrate_decision, evidence_backed_score
from services.discovery_quality import assess_content_quality
from services.professional_gate import augment_professional_eligibility
from services.scoring import evaluate_match, extract_jd, total_score


CASE_ROOT = Path(__file__).resolve().parents[1] / "data" / "evaluation_cases"
REPORT_ROOT = Path(__file__).resolve().parents[1] / "data" / "evaluation_reports"
CASE_FILE = CASE_ROOT / "cases.json"
DEFAULT_SCORE_TOLERANCE = 5
INCOMPLETE_CASE_MARKERS = (
    "只展示了岗位要求",
    "只显示了岗位要求",
    "没有看到完整的岗位职责",
    "未展示岗位职责",
    "岗位职责缺失",
)


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    title: str
    company: str
    role: str
    jd_text: str
    human_judgment: str
    score_min: int
    score_max: int
    expected_gate: str
    expected_gap_groups: list[list[str]]
    published_at: str = ""
    score_tolerance: int = DEFAULT_SCORE_TOLERANCE
    expected_scorable: bool = True


EXPECTED_GAPS: dict[int, list[list[str]]] = {
    2: [["计算机", "数学", "医学", "专业"], ["医疗", "智能体"]],
    3: [["CRM", "营销系统"], ["营销", "增长"]],
    4: [["互联网", "电商"]],
    5: [["互联网"]],
    6: [["采购", "商品运营", "供应链"]],
    7: [["SQL"], ["Tableau"], ["A/B", "AB测试"]],
    8: [["营销", "MKT"], ["4A", "酒旅"]],
    9: [["SQL"]],
}

# These cases contain explicit, verifiable must-have conditions in the user's
# judgment. "非不满足" is intentionally kept for cases where the human label only
# says that no rejection gate should fire.
EXPECTED_GATES: dict[int, str] = {
    7: "不满足",
    9: "不满足",
}


def _case_number(title: str) -> int:
    match = re.match(r"\s*(\d+)\.", title)
    if not match:
        raise ValueError(f"无法识别案例编号：{title}")
    return int(match.group(1))


def _score_range(judgment: str) -> tuple[int, int]:
    match = re.search(r"判断\s*[：:]\s*(\d{1,3})\s*[-—–~～至]\s*(\d{1,3})", judgment)
    if not match:
        raise ValueError(f"人工判断缺少分数区间：{judgment}")
    return int(match.group(1)), int(match.group(2))


def import_cases_from_docx(source: Path, destination: Path = CASE_FILE) -> list[EvaluationCase]:
    """Convert numbered JD sections and red judgments into local private cases."""
    from docx import Document

    document = Document(source)
    sections: list[tuple[str, list[str], str]] = []
    title = ""
    body: list[str] = []
    judgment = ""
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        if re.match(r"^\d+\.\s*", text) and paragraph.style.name.startswith("Heading"):
            if title:
                sections.append((title, body, judgment))
            title, body, judgment = text, [], ""
        elif text.startswith("判断："):
            red_runs = [
                str(run.font.color.rgb or "").upper()
                for run in paragraph.runs
                if run.text.strip()
            ]
            if any(color in {"C00000", "FF0000"} for color in red_runs):
                judgment = text
        elif title:
            body.append(text)
    if title:
        sections.append((title, body, judgment))

    cases: list[EvaluationCase] = []
    for heading, paragraphs, human_judgment in sections:
        number = _case_number(heading)
        score_min, score_max = _score_range(human_judgment)
        clean_title = re.sub(r"^\d+\.\s*", "", heading).strip()
        company, _, role = clean_title.partition("｜")
        explicitly_unmet = bool(
            re.search(r"(?:不匹配硬性条件|硬性条件.*(?:不满足|必要条件不满足))", human_judgment)
        )
        published_at = "2026-08-10" if number == 9 else ""
        cases.append(
            EvaluationCase(
                case_id=f"case_{number:03d}",
                title=clean_title,
                company=company.strip(),
                role=(role or clean_title).strip(),
                jd_text="\n".join([clean_title, *paragraphs]),
                human_judgment=human_judgment.removeprefix("判断：").strip(),
                score_min=score_min,
                score_max=score_max,
                expected_gate=EXPECTED_GATES.get(
                    number,
                    "不满足" if explicitly_unmet else "非不满足",
                ),
                expected_gap_groups=EXPECTED_GAPS.get(number, []),
                published_at=published_at,
                expected_scorable=not any(
                    marker in "\n".join(paragraphs)
                    for marker in INCOMPLETE_CASE_MARKERS
                ),
            )
        )
    if len(cases) != 10:
        raise ValueError(f"预期导入 10 个案例，实际得到 {len(cases)} 个。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps([asdict(case) for case in cases], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return cases


def load_evaluation_cases(path: Path = CASE_FILE) -> list[EvaluationCase]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    for entry in payload:
        entry.setdefault("score_tolerance", DEFAULT_SCORE_TOLERANCE)
        entry.setdefault(
            "expected_scorable",
            not any(
                marker in str(entry.get("jd_text") or "")
                for marker in INCOMPLETE_CASE_MARKERS
            ),
        )
    cases = [EvaluationCase(**entry) for entry in payload]
    return [
        EvaluationCase(
            **{
                **asdict(case),
                "expected_gate": EXPECTED_GATES.get(
                    int(case.case_id.rsplit("_", 1)[-1]),
                    case.expected_gate,
                ),
            }
        )
        for case in cases
    ]


def _all_gap_text(
    evaluation,
    eligibility: EligibilityAssessment | None = None,
) -> str:
    values = [*evaluation.missing_keywords, *evaluation.risks]
    for field in WEIGHTS:
        values.extend(getattr(evaluation, field).gaps)
    if eligibility:
        for check in eligibility.checks:
            if check.result in {"不满足", "存疑", "可能协商"}:
                values.extend(
                    [check.requirement, check.candidate_evidence, check.notes]
                )
    return "\n".join(values).casefold()


def gate_expectation_passes(expected: str, actual: str) -> bool:
    if expected == "不满足":
        return actual == "不满足"
    if expected == "满足":
        return actual == "满足"
    return actual != "不满足"


def _dimension_diagnostics(evaluation) -> dict[str, dict]:
    return {
        field: {
            "score": getattr(evaluation, field).score,
            "max_score": getattr(evaluation, field).max_score,
            "raw_score": evaluation.raw_dimension_scores.get(field),
            "reason": getattr(evaluation, field).score_reason,
            "evidence": list(getattr(evaluation, field).evidence),
            "gaps": list(getattr(evaluation, field).gaps),
        }
        for field in WEIGHTS
    }


def run_evaluation_case(
    case: EvaluationCase,
    *,
    client,
    model: str,
    resume_text: str,
    profile: CandidateProfileData,
    prepared_jd: JDInfo | None = None,
) -> dict:
    quality = assess_content_quality(
        case.jd_text,
        {"company": case.company, "role": case.role, "location": "评测集已确认"},
    )
    if not quality.scorable:
        quality_pass = not case.expected_scorable
        return {
            "case_id": case.case_id,
            "title": case.title,
            "expected_score": f"{case.score_min}-{case.score_max}",
            "actual_score": None,
            "strict_score_pass": None,
            "score_pass": None,
            "score_tolerance": case.score_tolerance,
            "expected_gate": case.expected_gate,
            "actual_gate": "未评估",
            "gate_pass": None,
            "gap_checks": [],
            "gap_pass": None,
            "recommendation": "信息待补全",
            "score_deviation": None,
            "tolerance_deviation": None,
            "input_quality": quality.content_level,
            "input_scorable": False,
            "expected_scorable": case.expected_scorable,
            "quality_pass": quality_pass,
            "abstained": True,
            "repeat_count": 1,
            "score_spread": 0,
            "stable": True,
            "passed": quality_pass,
            "error": "",
            "rubric_version": RUBRIC_VERSION,
            "model": model,
        }
    jd: JDInfo = (
        prepared_jd.model_copy(deep=True)
        if prepared_jd
        else extract_jd(client, model, case.jd_text)
    )
    jd.company = jd.company or case.company
    jd.role = jd.role or case.role
    jd.full_text = case.jd_text
    override = suggested_job_override(profile, case.jd_text, location=jd.location)
    context = build_candidate_context(profile, override)
    eligibility = evaluate_eligibility(jd.constraints, profile, override)
    eligibility = augment_professional_eligibility(
        eligibility,
        jd_text=case.jd_text,
        resume_text=resume_text,
    )
    evaluation = evaluate_match(client, model, resume_text, jd, context, eligibility)
    score = total_score(evaluation)
    decision = calibrate_decision(
        score,
        eligibility,
        model_recommendation=evaluation.recommendation,
        model_reason=evaluation.recommendation_reason,
        model_gate_result=evaluation.hard_gate_result,
        model_gate_notes=evaluation.hard_gate_notes,
    )
    gate_pass = gate_expectation_passes(case.expected_gate, decision.gate_result)
    gap_text = _all_gap_text(evaluation, eligibility)
    gap_checks = [
        {
            "expected": " / ".join(group),
            "matched": any(keyword.casefold() in gap_text for keyword in group),
        }
        for group in case.expected_gap_groups
    ]
    strict_score_pass = case.score_min <= score <= case.score_max
    tolerant_score_pass = (
        case.score_min - case.score_tolerance
        <= score
        <= case.score_max + case.score_tolerance
    )
    return {
        "case_id": case.case_id,
        "title": case.title,
        "expected_score": f"{case.score_min}-{case.score_max}",
        "actual_score": score,
        "strict_score_pass": strict_score_pass,
        "score_pass": tolerant_score_pass,
        "score_tolerance": case.score_tolerance,
        "expected_gate": case.expected_gate,
        "actual_gate": decision.gate_result,
        "gate_pass": gate_pass,
        "gap_checks": gap_checks,
        "gap_pass": all(check["matched"] for check in gap_checks),
        "recommendation": decision.final_recommendation,
        "score_deviation": (
            score - case.score_max
            if score > case.score_max
            else score - case.score_min
            if score < case.score_min
            else 0
        ),
        "tolerance_deviation": (
            score - (case.score_max + case.score_tolerance)
            if score > case.score_max + case.score_tolerance
            else score - (case.score_min - case.score_tolerance)
            if score < case.score_min - case.score_tolerance
            else 0
        ),
        "input_quality": quality.content_level,
        "input_scorable": True,
        "expected_scorable": case.expected_scorable,
        "quality_pass": case.expected_scorable,
        "abstained": False,
        "dimensions": _dimension_diagnostics(evaluation),
        "calibration_notes": list(evaluation.calibration_notes),
        "matched_keywords": list(evaluation.matched_keywords),
        "missing_keywords": list(evaluation.missing_keywords),
        "strengths": list(evaluation.strengths),
        "risks": list(evaluation.risks),
        "eligibility_checks": [
            check.model_dump(mode="json")
            for check in eligibility.checks
        ],
        "jd_snapshot": jd.model_dump(mode="json"),
        "decision_reason": decision.final_reason,
        "passed": bool(
            tolerant_score_pass
            and gate_pass
            and all(check["matched"] for check in gap_checks)
        ),
        "rubric_version": RUBRIC_VERSION,
        "model": model,
    }


def save_evaluation_report(
    results: list[dict], *, model: str, target: Path | None = None
) -> Path:
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = target or REPORT_ROOT / f"evaluation_{stamp}.json"
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "rubric_version": RUBRIC_VERSION,
        "model": model,
        "case_count": len(results),
        "passed_count": sum(bool(result.get("passed")) for result in results),
        "metrics": evaluation_report_metrics(results),
        "general_guardrails": general_quality_checks(),
        "results": results,
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def failed_case_result(
    case: EvaluationCase,
    *,
    error: str,
    model: str,
    completed_repeats: int = 0,
) -> dict:
    return {
        "case_id": case.case_id,
        "title": case.title,
        "expected_score": f"{case.score_min}-{case.score_max}",
        "actual_score": None,
        "strict_score_pass": False,
        "score_pass": False,
        "score_tolerance": case.score_tolerance,
        "expected_gate": case.expected_gate,
        "actual_gate": "未完成",
        "gate_pass": False,
        "gap_checks": [],
        "gap_pass": False,
        "recommendation": "未完成",
        "input_quality": "error",
        "input_scorable": None,
        "expected_scorable": case.expected_scorable,
        "quality_pass": False,
        "abstained": False,
        "repeat_count": completed_repeats,
        "score_spread": None,
        "stable": False,
        "passed": False,
        "error": str(error)[:300],
        "rubric_version": RUBRIC_VERSION,
        "model": model,
    }


def aggregate_case_runs(case: EvaluationCase, runs: list[dict]) -> dict:
    if not runs:
        raise ValueError("案例至少需要一次评分结果。")
    abstained = [run for run in runs if run.get("abstained")]
    if len(abstained) == len(runs):
        representative = abstained[0]
        stable = all(bool(run.get("quality_pass")) for run in abstained)
        return {
            **representative,
            "repeat_count": len(runs),
            "stable": stable,
            "passed": stable,
        }
    if abstained:
        representative = next(run for run in runs if not run.get("abstained"))
        return {
            **representative,
            "repeat_count": len(runs),
            "stable": False,
            "passed": False,
            "error": "同一案例在重复运行中出现评分/拒绝评分不一致。",
        }
    scores = [int(run["actual_score"]) for run in runs]
    median_score = int(statistics.median(scores))
    gates = [str(run["actual_gate"]) for run in runs]
    strict_score_pass = case.score_min <= median_score <= case.score_max
    score_pass = (
        case.score_min - case.score_tolerance
        <= median_score
        <= case.score_max + case.score_tolerance
    )
    gate_pass = all(bool(run["gate_pass"]) for run in runs)
    gap_pass = all(bool(run["gap_pass"]) for run in runs)
    stable = len(set(gates)) == 1 and max(scores) - min(scores) <= 5
    representative = min(runs, key=lambda run: abs(int(run["actual_score"]) - median_score))
    return {
        **representative,
        "actual_score": median_score,
        "strict_score_pass": strict_score_pass,
        "score_pass": score_pass,
        "score_deviation": (
            median_score - case.score_max
            if median_score > case.score_max
            else median_score - case.score_min
            if median_score < case.score_min
            else 0
        ),
        "tolerance_deviation": (
            median_score - (case.score_max + case.score_tolerance)
            if median_score > case.score_max + case.score_tolerance
            else median_score - (case.score_min - case.score_tolerance)
            if median_score < case.score_min - case.score_tolerance
            else 0
        ),
        "gate_pass": gate_pass,
        "gap_pass": gap_pass,
        "repeat_count": len(runs),
        "score_spread": max(scores) - min(scores),
        "run_scores": scores,
        "run_gates": gates,
        "run_recommendations": [
            str(run.get("recommendation", "")) for run in runs
        ],
        "stable": stable,
        "passed": bool(score_pass and gate_pass and gap_pass and stable),
    }


def latest_evaluation_report() -> dict | None:
    if not REPORT_ROOT.exists():
        return None
    files = sorted(REPORT_ROOT.glob("evaluation_*.json"), reverse=True)
    if not files:
        return None
    return json.loads(files[0].read_text(encoding="utf-8"))


def evaluation_report_metrics(results: list[dict]) -> dict[str, float | int | None]:
    completed = [result for result in results if result.get("actual_score") is not None]
    quality_labeled = [
        result for result in results if result.get("quality_pass") is not None
    ]
    if not completed:
        return {
            "case_count": len(results), "interval_hit_rate": None,
            "strict_interval_hit_rate": None,
            "gate_accuracy": None, "gap_recall": None,
            "mean_interval_deviation": None, "rank_consistency": None,
            "stability_rate": None,
            "input_guardrail_accuracy": (
                sum(bool(result.get("quality_pass")) for result in quality_labeled)
                / len(quality_labeled)
                if quality_labeled else None
            ),
        }
    expected_gap_count = sum(len(result.get("gap_checks", [])) for result in completed)
    matched_gap_count = sum(
        sum(bool(check.get("matched")) for check in result.get("gap_checks", []))
        for result in completed
    )
    comparable_pairs = 0
    concordant_pairs = 0
    for index, left in enumerate(completed):
        left_low, left_high = (int(value) for value in left["expected_score"].split("-"))
        left_mid = (left_low + left_high) / 2
        for right in completed[index + 1:]:
            right_low, right_high = (int(value) for value in right["expected_score"].split("-"))
            right_mid = (right_low + right_high) / 2
            if left_mid == right_mid:
                continue
            comparable_pairs += 1
            expected_direction = left_mid > right_mid
            actual_direction = int(left["actual_score"]) > int(right["actual_score"])
            if expected_direction == actual_direction:
                concordant_pairs += 1
    repeated = [result for result in completed if int(result.get("repeat_count") or 0) > 1]
    gate_results = [
        result for result in completed if result.get("gate_pass") is not None
    ]
    return {
        "case_count": len(completed),
        "interval_hit_rate": sum(bool(result.get("score_pass")) for result in completed) / len(completed),
        "strict_interval_hit_rate": sum(
            bool(result.get("strict_score_pass", result.get("score_pass")))
            for result in completed
        ) / len(completed),
        "gate_accuracy": (
            sum(bool(result.get("gate_pass")) for result in gate_results) / len(gate_results)
            if gate_results else None
        ),
        "gap_recall": matched_gap_count / expected_gap_count if expected_gap_count else 1.0,
        "mean_interval_deviation": sum(abs(int(result.get("score_deviation") or 0)) for result in completed) / len(completed),
        "mean_tolerance_deviation": sum(
            abs(int(result.get("tolerance_deviation") or 0)) for result in completed
        ) / len(completed),
        "rank_consistency": concordant_pairs / comparable_pairs if comparable_pairs else None,
        "stability_rate": (
            sum(bool(result.get("stable")) for result in repeated) / len(repeated)
            if repeated else None
        ),
        "input_guardrail_accuracy": (
            sum(bool(result.get("quality_pass")) for result in quality_labeled)
            / len(quality_labeled)
            if quality_labeled else None
        ),
    }


def general_quality_checks() -> list[dict[str, str | bool]]:
    summary_text = "信息范围：实习僧官方岗位列表摘要\n产品实习\n北京"
    full_text = (
        "岗位职责\n负责用户研究、需求分析和产品迭代，跟进数据并推动跨团队协作。\n"
        "任职要求\n本科及以上，具备数据分析能力，每周到岗五天，连续实习四个月。"
        "候选人需要能够独立整理反馈、验证方案并形成完整分析报告。"
        "工作中需要持续跟踪核心指标，设计调研问卷与访谈提纲，结合用户反馈定位问题，"
        "与设计、研发和运营团队共同制定迭代计划，并对上线后的实际效果进行复盘。"
        "有互联网产品、策略分析或数据分析相关项目经历者优先，能够清晰表达分析结论。"
    )
    summary_quality = assess_content_quality(
        summary_text, {"company": "示例公司", "role": "产品实习", "location": "北京"}
    )
    full_quality = assess_content_quality(
        full_text, {"company": "示例公司", "role": "产品实习", "location": "北京"}
    )
    unmet = EligibilityAssessment(
        checks=[EligibilityCheck(constraint_type="days_per_week", requirement="每周到岗5天", result="不满足")],
        unmet_count=1,
    )
    blocked = calibrate_decision(92, unmet)
    incomplete_detail = assess_content_quality(
        "这张截图只展示了岗位要求，没有看到完整的岗位职责。\n任职要求\n本科以上，熟练Excel。",
        {"company": "示例公司", "role": "采购实习", "location": "北京"},
    )
    skill_gate = augment_professional_eligibility(
        EligibilityAssessment(),
        jd_text="任职要求\n熟练使用 SQL 取数为必要条件。",
        resume_text="技能：SQL 基础查询。",
    )
    return [
        {"检查": "摘要输入拒绝评分", "通过": not summary_quality.scorable, "说明": summary_quality.content_level},
        {"检查": "完整 JD 允许评分", "通过": full_quality.scorable, "说明": full_quality.content_level},
        {"检查": "无证据不得分", "通过": evidence_backed_score(9, 10, []) == 0, "说明": "证据为空时归零"},
        {"检查": "硬门槛优先于高分", "通过": blocked.final_recommendation == "不建议投递", "说明": blocked.gate_result},
        {"检查": "缺少职责的详情拒绝评分", "通过": not incomplete_detail.scorable, "说明": incomplete_detail.content_level},
        {"检查": "基础技能不冒充熟练门槛", "通过": skill_gate.unmet_count == 1, "说明": "SQL 基础查询 ≠ 熟练取数"},
    ]
