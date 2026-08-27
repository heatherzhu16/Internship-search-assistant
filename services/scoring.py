from __future__ import annotations

import hashlib
import json
import math
import re
from calendar import monthrange
from typing import Callable, get_args, get_origin

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from candidate_profile import EligibilityAssessment
from models.evaluation import (
    ApplicationMaterials,
    DIMENSION_GUIDES,
    DIMENSION_NAMES,
    Evaluation,
    JDInfo,
    RUBRIC_VERSION,
    SCORE_ANCHORS,
    WEIGHTS,
)
from scoring_policy import evidence_backed_score


EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])[\w.+-]+@(?:[\w-]+\.)+[A-Za-z]{2,}(?![\w.-])"
)


def _repair_model_payload(payload: object, model_type: type[BaseModel]) -> object:
    """Repair common JSON-mode shape drift before strict Pydantic validation."""
    if not isinstance(payload, dict):
        return payload
    repaired = dict(payload)
    for name, field in model_type.model_fields.items():
        if name not in repaired:
            continue
        annotation = field.annotation
        origin = get_origin(annotation)
        args = get_args(annotation)
        value = repaired[name]
        if origin is list:
            if value is None:
                value = []
            elif not isinstance(value, list):
                value = [value]
            if args and args[0] is str:
                clean_values: list[str] = []
                for item in value:
                    if isinstance(item, str):
                        clean = item.strip()
                    elif isinstance(item, dict):
                        clean = str(
                            item.get("keyword")
                            or item.get("text")
                            or item.get("name")
                            or ""
                        ).strip()
                    else:
                        clean = str(item or "").strip()
                    if clean:
                        clean_values.append(clean)
                value = clean_values
            repaired[name] = value
        elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
            repaired[name] = _repair_model_payload(value, annotation)
    return repaired


def get_client(api_key: str) -> OpenAI:
    cleaned_key = api_key.strip()
    if not cleaned_key:
        raise ValueError("请先在左侧输入 DeepSeek API Key。")
    if not cleaned_key.isascii():
        raise ValueError(
            "DeepSeek API Key 含有中文或其他非英文字符。请清空后重新粘贴以 sk- 开头的 Key。"
        )
    if not cleaned_key.startswith("sk-") or any(character.isspace() for character in cleaned_key):
        raise ValueError(
            "DeepSeek API Key 格式不正确。请重新粘贴完整、无空格且以 sk- 开头的 Key。"
        )
    return OpenAI(api_key=cleaned_key, base_url="https://api.deepseek.com")


def call_json(
    client: OpenAI,
    model: str,
    system_prompt: str,
    user_prompt: str,
    output_model: type[BaseModel],
    semantic_validator: Callable[[BaseModel], str | None] | None = None,
) -> BaseModel:
    schema = json.dumps(output_model.model_json_schema(), ensure_ascii=False)
    retry_note = ""
    last_error: ValidationError | None = None
    for attempt in range(2):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            f"{system_prompt}\n必须只输出合法 JSON 对象，不要输出 Markdown。"
                            f"\nJSON Schema：\n{schema}{retry_note}"
                        ),
                    },
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=6000,
                extra_body={"thinking": {"type": "disabled"}},
            )
        except UnicodeEncodeError as exc:
            raise ValueError(
                "模型连接配置含有不能编码的字符。请在左侧清空并重新粘贴 DeepSeek API Key。"
            ) from exc
        content = response.choices[0].message.content
        if not content:
            if attempt == 0:
                retry_note = "\n上次返回为空。请重新完整输出所有字段。"
                continue
            raise ValueError("DeepSeek 连续返回空结果，请稍后重试。")
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            if attempt == 0:
                retry_note = "\n上次不是合法 JSON。请重新完整输出，禁止解释或 Markdown。"
                continue
            raise ValueError("DeepSeek 连续返回了无法解析的 JSON，请稍后重试。")
        try:
            parsed = output_model.model_validate(
                _repair_model_payload(payload, output_model)
            )
            semantic_error = semantic_validator(parsed) if semantic_validator else None
            if semantic_error:
                if attempt == 0:
                    retry_note = (
                        f"\n上次结果虽然格式正确，但内容无效：{semantic_error}。"
                        "请重新阅读输入并完整填写所有评分维度、证据和缺口。"
                    )
                    continue
                raise ValueError(
                    f"DeepSeek 连续两次返回内容无效：{semantic_error}。"
                    "该案例已记录为失败，其余案例会继续运行。"
                )
            return parsed
        except ValidationError as exc:
            last_error = exc
            first_error = exc.errors()[0] if exc.errors() else {}
            field = ".".join(str(part) for part in first_error.get("loc", ()))
            retry_note = (
                f"\n上次字段“{field or '未知'}”类型不符合 Schema。"
                "请重新生成完整对象；列表字段必须输出 JSON 数组，未知列表输出 []。"
            )
    first_error = last_error.errors()[0] if last_error and last_error.errors() else {}
    field = ".".join(str(part) for part in first_error.get("loc", ()))
    field_hint = f"（字段：{field}）" if field else ""
    raise ValueError(
        f"DeepSeek 连续两次返回的数据格式无法解析{field_hint}。该案例已记录为失败，"
        "其余案例会继续运行。"
    ) from last_error


def extract_jd(client: OpenAI, model: str, jd_text: str) -> JDInfo:
    system = (
        "忠实提取 JD；缺失字段用空值。hard_requirements 只写可能一票否决的条件。"
        "constraints 只放入可用候选人事实客观核对的结构化资格条件，如学历、"
        "专业、语言、到岗日期、每周天数、实习月数、城市和工作方式。"
        "产品设计、逻辑思考、自驱、行业理解、文档编写等专业能力只留在"
        "requirements/hard_requirements 中参与专业评分，绝不得作为 other constraint。"
        "application_email 专门提取简历投递邮箱；source 只填写信息发布渠道或平台，"
        "例如小红书、BOSS直聘、实习僧、官网、公众号或邮件，绝不能填写邮箱地址。"
        "application_reference 提取明确出现的招聘编号、职位编号或 requisition ID。"
        "constraints 的日期 value 使用 YYYY-MM-DD，天数/月数/毕业年份只填数字，"
        "quote 保留原文；无法确定用 other，禁止补写原文没有的信息。"
        "出现“优先、加分、更佳”必须标为优先或加分，不得标为必须；"
        "“不确定是否有转正HC、介意勿投”等岗位说明不是候选人资格条件，不放入constraints。"
        "211/985/QS学校层级使用institution_tier；本科/硕士在校状态可分别使用"
        "education和student_status；不接受远程、必须现场使用work_mode。"
    )
    result = call_json(client, model, system, f"结构化以下 JD：\n\n{jd_text}", JDInfo)
    result = normalize_jd_contact_fields(result, jd_text)
    cleaned = []
    seen_quotes: set[str] = set()
    for constraint in result.constraints:
        quote = constraint.quote.strip()
        folded = quote.casefold()
        if not quote or not constraint.value.strip():
            continue
        if constraint.constraint_type == "other":
            # Unknown professional qualities cannot be checked against the
            # structured candidate profile. Keeping them here converts normal
            # fit gaps into false "存疑" eligibility gates. They remain in
            # requirements/hard_requirements and are scored by the fit rubric.
            continue
        if constraint.constraint_type == "arrival_date":
            month_range = re.search(
                r"(20\d{2})\s*年\s*(\d{1,2})\s*[—–\-~～至]\s*(\d{1,2})\s*月",
                quote,
            )
            if month_range:
                year = int(month_range.group(1))
                end_month = int(month_range.group(3))
                end_day = monthrange(year, end_month)[1]
                constraint.value = f"{year:04d}-{end_month:02d}-{end_day:02d}"
                constraint.operator = "不晚于"
        if (
            constraint.importance == "必须"
            and any(marker in folded for marker in ["优先", "加分", "更佳"])
            and not any(marker in folded for marker in ["必须", "不接受", "至少", "非诚勿扰"])
        ):
            constraint.importance = "优先"
        dedupe = re.sub(r"\s+", "", quote or constraint.value).casefold()
        if dedupe and dedupe in seen_quotes:
            continue
        if dedupe:
            seen_quotes.add(dedupe)
        cleaned.append(constraint)
    result.constraints = cleaned
    return result


def normalize_jd_contact_fields(result: JDInfo, jd_text: str) -> JDInfo:
    """Keep contact addresses out of the source field even when the LLM misfiles them."""
    emails = [match.group(0).casefold() for match in EMAIL_PATTERN.finditer(jd_text)]
    if not result.application_email.strip() and emails:
        result.application_email = emails[0]
    else:
        result.application_email = result.application_email.strip().casefold()

    source = result.source.strip()
    if "@" in source or (
        result.application_email
        and result.application_email.casefold() in source.casefold()
    ):
        result.source = ""
    else:
        result.source = source
    return result


def evaluate_match(
    client: OpenAI,
    model: str,
    resume_text: str,
    jd: JDInfo,
    candidate_context: dict,
    eligibility: EligibilityAssessment | None = None,
) -> Evaluation:
    rubric = "\n".join(
        f"- {DIMENSION_NAMES[key]}（{points}分）：{DIMENSION_GUIDES[key]}"
        for key, points in WEIGHTS.items()
    )
    prompt = f"""
只根据简历、用户确认的求职档案和 JD 评分，不得虚构。

评分维度：
{rubric}

规则：
1. max_score 严格等于给定分值，score 在合法区间。
2. {SCORE_ANCHORS}
3. 每个维度都必须填写 score_reason，用一至两句话解释为什么得到这个分数。
   evidence 必须逐条标注证据直接性：
   - 同类任务、分析对象或交付物能对应 JD，写“直接证据：简历事实 → JD 要求”；
   - 只有通用能力或需跨职能/跨行业迁移，写“可迁移证据：简历事实 → JD 要求”。
   无证据必须 0 分并写 gaps；不得把同一事实拆成多条凑数。
4. 不按关键词词频抬分，不凭专业名称推测动机，不先给印象分再补理由。
5. 到岗、实习期限、每周天数、地点、远程/现场方式只属于资格门槛，绝不能写入
   六个专业匹配维度的 evidence 或 gaps，也绝不能因此降低专业匹配分。
6. 求职上下文不能当作项目、经历或技能证据。
7. recommendation 只是模型初判，程序会用本地资格门槛校准。
8. 学校名称、学校层级、学历和毕业时间属于已确认教育事实，可以同时引用简历与求职档案。
9. “优先、加分、更佳”不是硬性门槛；缺少加分项只能记为差距，不能判定硬性不符合。
10. 到岗安排只影响资格门槛，不得在专业匹配各维度重复扣分。
11. “核心专业必备项”评估岗位工作的核心能力，例如行业研究、数据分析、英语等；
    即使 JD 没有使用“必须”二字，也不能把该维度直接记为 0。
12. 求职档案中的毕业日期是已核验事实。不得自行改写届别或声称与该日期矛盾。
13. “行业研究/商业尽调/竞品分析/财报及经营数据分析”与战略、商业分析岗属于
    直接任务对应，不能因所在行业不同就降为弱相关。
14. 对产品设计、功能方案、需求对接、协同研发上线、CRM/营销系统等职能专项要求，
    只有研究、办公软件、沟通或数据分析经历时只能算可迁移证据，不得按直接经验高分。
15. 行业兴趣不等于职能经验；例如学习过 AI 案例不等于做过 AI 产品，使用 Excel
    不等于使用过 CRM。
16. gaps 只能记录 JD 职责或任职要求明确要求、但简历没有支持的内容。
    不得自行增加 JD 未要求的 CRM、行业专用工具、软件或课程作为缺口。
17. gaps 必须区分性质：JD 明写“优先/加分/更佳”的内容缺少时，以
    “加分项缺口：”开头；岗位核心工作无直接证据时，以“核心缺口：”开头。
    JD 明确欢迎首段相关实习时，不得再把“没有该行业/平台实习”写为核心缺口。
18. JD 职责中列出的未来研究赛道，不等于要求候选人入职前已有该赛道经验；
    只有任职要求明确要求相关经验时才可记为缺口。
19. 简历求职意向和已确认目标职能可以仅在 motivation 维度作为直接动机证据；
    不得把它们用作经历、技能或业绩证据。

简历：
{resume_text}

JD：
{json.dumps(jd.model_dump(mode="json"), ensure_ascii=False)}

求职上下文：
{json.dumps(candidate_context, ensure_ascii=False)}
"""
    result = call_json(
        client,
        model,
        "你是证据优先的实习岗位匹配评估员。",
        prompt,
        Evaluation,
        semantic_validator=_evaluation_semantic_error,
    )
    for field, maximum in WEIGHTS.items():
        item = getattr(result, field)
        item.max_score = maximum
        item.score = evidence_backed_score(item.score, maximum, item.evidence)
    result = calibrate_evaluation_dimensions(
        result,
        jd,
        eligibility,
        candidate_context,
    )
    return result


LOGISTICS_MARKERS = (
    "到岗", "每周", "一周", "实习期", "实习时长", "期限", "工作地点",
    "地点", "base", "远程", "现场", "坐班", "搬迁",
)


def _invalid_professional_gap(text: str, candidate_context: dict) -> bool:
    folded = text.casefold()
    if any(marker in folded for marker in LOGISTICS_MARKERS):
        return True
    graduation = (
        candidate_context.get("facts", {}).get("graduation_date")
        if candidate_context
        else None
    )
    if graduation:
        expected_cohort = str(graduation)[:4][-2:]
        mentioned = re.findall(r"(?<!\d)(\d{2})届", text)
        if any(cohort != expected_cohort for cohort in mentioned):
            return True
    return False


OPTIONAL_EXPERIENCE_TERMS = (
    "硕士", "博士", "python", "sql", "互联网战略", "商分", "数分",
    "投资", "券商", "基金", "咨询", "汽车", "ai应用", "ai",
)


def _normalize_professional_gap(text: str, jd: JDInfo) -> str | None:
    """Remove contradicted gaps and label clearly optional experience gaps."""
    cleaned = text.strip()
    folded = cleaned.casefold()
    full_text = jd.full_text.casefold()
    if (
        "欢迎希望积累首段互联网实习" in full_text
        and "互联网" in folded
        and any(marker in folded for marker in ("缺少", "没有", "无"))
    ):
        return None
    if "简历虽提及" in cleaned and any(
        marker in cleaned for marker in ("未展示", "未体现")
    ):
        return None
    optional_clauses = [
        clause
        for clause in re.split(r"[\n。；;]", full_text)
        if any(marker in clause for marker in ("优先", "加分", "更佳"))
    ]
    if not cleaned.startswith("加分项缺口：") and any(
        term in folded
        and any(term in clause for clause in optional_clauses)
        for term in OPTIONAL_EXPERIENCE_TERMS
    ):
        cleaned = re.sub(r"^(?:核心缺口：)?", "加分项缺口：", cleaned)
    return cleaned


def _is_indirect_evidence(text: str) -> bool:
    folded = text.casefold()
    return any(
        marker.casefold() in folded
        for marker in INDIRECT_EVIDENCE_MARKERS
    )


ELIGIBILITY_ONLY_EVIDENCE_MARKERS = (
    "本科", "硕士", "博士", "学历", "在校", "毕业", "211", "985",
    "雅思", "cet", "英语", "工作语言", "到岗", "每周", "实习期限",
    "实习时长", "地点", "城市", "搬迁", "专业不限", "有实习经历者优先",
)


def _is_eligibility_only_evidence(text: str) -> bool:
    folded = text.casefold()
    return any(marker.casefold() in folded for marker in ELIGIBILITY_ONLY_EVIDENCE_MARKERS)


def _direct_evidence_floor(maximum: int, evidence: list[str]) -> int:
    """Set a bounded floor when several independently stated facts are direct."""
    count = sum(not _is_indirect_evidence(item) for item in evidence)
    if count >= 3:
        rate = 0.85
    elif count >= 2:
        rate = 0.80
    else:
        return 0
    return math.ceil(maximum * rate)


INDIRECT_EVIDENCE_MARKERS = (
    "推断", "可能", "可迁移", "间接", "部分相关", "一定关联",
    "任务不同", "相似度有限", "缺乏直接", "非直接", "无直接",
    "部分满足", "有一定差距", "相关课程", "包含数学",
)


def _evaluation_semantic_error(value: BaseModel) -> str | None:
    """Reject schema-valid placeholder evaluations before they become 0 scores."""
    if not isinstance(value, Evaluation):
        return None
    dimensions = [getattr(value, field) for field in WEIGHTS]
    placeholder_markers = ("尚未定义评估标准", "缺少评估标准", "无法确定得分")
    placeholder_count = sum(
        any(
            marker in item.score_reason or marker in " ".join(item.gaps)
            for marker in placeholder_markers
        )
        for item in dimensions
    )
    evidence_count = sum(
        len([evidence for evidence in item.evidence if evidence.strip()])
        for item in dimensions
    )
    score_total = sum(int(item.score) for item in dimensions)
    if placeholder_count >= 3:
        return "多个维度只是占位说明，没有实际评估"
    if score_total == 0 and evidence_count == 0:
        return "所有维度均为 0 且没有任何简历证据"
    return None


def _consistency_cap(item, maximum: int) -> tuple[int, str]:
    """Cap optimistic scores when the model's own explanation admits gaps."""
    combined_reason = " ".join(
        [item.score_reason, *item.evidence]
    ).casefold()
    indirect = any(marker.casefold() in combined_reason for marker in INDIRECT_EVIDENCE_MARKERS)
    usable_evidence = [item for item in item.evidence if item.strip()]
    indirect_evidence = [item for item in usable_evidence if _is_indirect_evidence(item)]
    if usable_evidence and len(indirect_evidence) == len(usable_evidence):
        return math.floor(maximum * 0.55), "证据全部属于间接或可迁移能力"
    if not item.gaps:
        return maximum, ""
    if all(gap.strip().startswith("加分项缺口：") for gap in item.gaps):
        return math.floor(maximum * 0.95), "只缺少优先或加分项"
    indirect_share = (
        len(indirect_evidence) / len(usable_evidence)
        if usable_evidence
        else 0
    )
    if indirect and indirect_share >= 0.5:
        return math.floor(maximum * 0.60), "存在明确缺口且证据主要为间接或可迁移"
    return math.floor(maximum * 0.80), "存在明确缺口"


def calibrate_evaluation_dimensions(
    result: Evaluation,
    jd: JDInfo,
    eligibility: EligibilityAssessment | None,
    candidate_context: dict,
) -> Evaluation:
    """Calibrate evidence directness and keep logistics out of fit scoring."""
    raw_scores = {
        field: int(getattr(result, field).score)
        for field in WEIGHTS
    }
    notes: list[str] = []
    for field, maximum in WEIGHTS.items():
        item = getattr(result, field)
        item.max_score = maximum
        old_evidence_count = len(item.evidence)
        old_gap_count = len(item.gaps)
        item.evidence = [
            evidence
            for evidence in item.evidence
            if not _invalid_professional_gap(evidence, candidate_context)
        ]
        normalized_gaps = [
            _normalize_professional_gap(gap, jd)
            for gap in item.gaps
            if not _invalid_professional_gap(gap, candidate_context)
        ]
        item.gaps = [gap for gap in normalized_gaps if gap]
        calibrated_score = max(
            evidence_backed_score(item.score, maximum, item.evidence),
            _direct_evidence_floor(maximum, item.evidence),
        )
        consistency_cap, cap_reason = _consistency_cap(item, maximum)
        if calibrated_score > consistency_cap:
            notes.append(
                f"{DIMENSION_NAMES[field]}：{cap_reason}，"
                f"由{calibrated_score}分校准为{consistency_cap}分。"
            )
            calibrated_score = consistency_cap
        if field == "experience_match":
            core_gap_count = sum(
                gap.strip().startswith("核心缺口：") for gap in item.gaps
            )
            if core_gap_count >= 2:
                core_gap_cap = math.floor(
                    maximum * (0.55 if core_gap_count >= 3 else 0.60)
                )
                if calibrated_score > core_gap_cap:
                    notes.append(
                        f"{DIMENSION_NAMES[field]}：存在{core_gap_count}项相互独立的核心职责缺口，"
                        f"由{calibrated_score}分校准为{core_gap_cap}分。"
                    )
                    calibrated_score = core_gap_cap
        if len(item.evidence) != old_evidence_count or len(item.gaps) != old_gap_count:
            notes.append(f"{DIMENSION_NAMES[field]}：移除资格条件或错误届别造成的专业扣分。")
        if calibrated_score > item.score:
            direct_count = sum(
                not _is_indirect_evidence(evidence)
                for evidence in item.evidence
            )
            notes.append(
                f"{DIMENSION_NAMES[field]}：依据{direct_count}条直接且可核验证据，"
                f"由{item.score}分校准为{calibrated_score}分。"
            )
        item.score = calibrated_score

    checks = eligibility.checks if eligibility else []
    hard = result.hard_requirements
    experience = result.experience_match
    skills = result.skills_tools
    professional_hard_evidence = [
        evidence
        for evidence in hard.evidence
        if not _is_eligibility_only_evidence(evidence)
    ]
    if not professional_hard_evidence and experience.evidence and skills.evidence:
        proxy_rate = (
            experience.score / experience.max_score
            + skills.score / skills.max_score
        ) / 2
        proxy_score = round(hard.max_score * proxy_rate)
        if hard.score > proxy_score:
            notes.append(
                f"{DIMENSION_NAMES['hard_requirements']}：现有证据仅证明学历、语言或到岗资格，"
                f"不能重复抬高专业匹配分，由{hard.score}分校准为{proxy_score}分。"
            )
            hard.score = proxy_score
        elif not hard.evidence and proxy_score > hard.score:
            hard.evidence = [
                f"核心工作证据：{evidence}"
                for evidence in (experience.evidence[:1] + skills.evidence[:1])
            ]
            notes.append(
                f"{DIMENSION_NAMES['hard_requirements']}：JD未列独立专业必备项，"
                f"按核心经历与技能证据由{hard.score}分校准为{proxy_score}分。"
            )
            hard.score = proxy_score

    education_checks = [
        check
        for check in checks
        if check.constraint_type
        in {"education", "institution_tier", "student_status", "graduation_year"}
    ]
    if education_checks and all(
        check.result == "满足" for check in education_checks
    ):
        education = result.education
        # A verified degree/student status only creates a high floor when the
        # education dimension itself has no professional-background gap. A
        # candidate can satisfy "本科及以上" while still missing a preferred
        # computer-science/medical/quantitative major.
        calibrated_education = (
            max(education.score, math.ceil(education.max_score * 0.85))
            if not education.gaps
            else education.score
        )
        if calibrated_education > education.score:
            notes.append(
                f"{DIMENSION_NAMES['education']}：已核验学历/在校条件均满足，"
                f"由{education.score}分校准为{calibrated_education}分。"
            )
        education.score = calibrated_education

    # Target-role preferences belong to the separate preference layer. They may
    # explain interest, but must not create a deterministic score floor here.
    experience_has_direct = any(
        not _is_indirect_evidence(evidence)
        for evidence in experience.evidence
    )
    core_experience_gaps = [
        gap for gap in experience.gaps if gap.strip().startswith("核心缺口：")
    ]
    core_skill_gaps = [
        gap for gap in skills.gaps if gap.strip().startswith("核心缺口：")
    ]
    direct_experience_count = sum(
        not _is_indirect_evidence(evidence)
        for evidence in experience.evidence
    )
    weak_core_evidence = (
        (not experience_has_direct and bool(experience.gaps) and bool(skills.gaps))
        or (len(core_experience_gaps) >= 2 and bool(core_skill_gaps))
        or (
            direct_experience_count < 2
            and bool(core_experience_gaps)
            and bool(core_skill_gaps)
        )
    )
    if (
        weak_core_evidence
        and hard.score > math.floor(hard.max_score * 0.40)
    ):
        capped_hard = math.floor(hard.max_score * 0.40)
        notes.append(
            f"{DIMENSION_NAMES['hard_requirements']}：岗位核心职能没有直接经历，"
            f"且经历与技能都有明确缺口，由{hard.score}分校准为{capped_hard}分。"
        )
        hard.score = capped_hard
    for field, maximum in WEIGHTS.items():
        item = getattr(result, field)
        consistency_cap, cap_reason = _consistency_cap(item, maximum)
        if item.score > consistency_cap:
            notes.append(
                f"{DIMENSION_NAMES[field]}：{cap_reason}，"
                f"最终分由{item.score}分限制为{consistency_cap}分。"
            )
            item.score = consistency_cap
    result.raw_dimension_scores = raw_scores
    result.calibration_notes = list(dict.fromkeys(notes))
    return result


def generate_materials(
    client: OpenAI,
    model: str,
    resume_text: str,
    jd: JDInfo,
    evaluation: Evaluation,
    candidate_context: dict,
) -> ApplicationMaterials:
    prompt = f"""
基于简历、JD、评分和已确认求职上下文生成求职材料。
不得虚构或夸大。简历改写只能调整真实原文的结构与措辞。
到岗信息只能写进邮件或私信，不能塞进简历改写；缺失信息用占位符并写入
truthfulness_notes。

简历改写规则：
1. 生成 3–5 条真正有信息增益的改写；如果没有足够真实证据，宁可少写。
2. original 必须逐字引用简历中的一条完整职责/项目 bullet，不能只引用公司、
   岗位、章节标题或日期。
3. suggested 必须仍是可直接粘贴进简历的 bullet，突出动作、分析对象、方法和
   已有结果；不得只是把原文复述一遍，也不得添加“与JD高度相关”等评价句。
4. 不得新造数字、工具、职责、行业、项目成果或因果关系。
5. JD 没有对应真实证据时写进 priority_actions，不要伪造一条简历改写。
6. rationale 具体说明改了什么以及对应哪条 JD，不得使用“突出相关性”之类空话。

邮件和私信应在用户已确认时自然写入到岗日、每周天数、实习期限和异地/现场意愿；
这些信息不进入简历改写。生成中英文邮件和 180 字以内 BOSS 私信。

简历：{resume_text}
JD：{json.dumps(jd.model_dump(mode="json"), ensure_ascii=False)}
评分：{evaluation.model_dump_json()}
求职上下文：{json.dumps(candidate_context, ensure_ascii=False)}
"""
    materials = call_json(
        client, model, "你是诚实、具体的求职材料助手。", prompt, ApplicationMaterials
    )
    materials.resume_rewrites = _valid_resume_rewrites(
        materials.resume_rewrites, resume_text
    )
    if len(materials.resume_rewrites) < 2:
        retry_prompt = (
            prompt
            + "\n\n上一次生成的简历改写因只改标题、原文不在简历中或建议没有实际变化"
            "而被程序拒绝。请重新生成；每条 original 必须是上述简历中至少25字的完整"
            "职责或项目 bullet，suggested 必须有实质改写。"
        )
        retry = call_json(
            client,
            model,
            "你是严谨的简历编辑，只改写可核验的完整经历 bullet。",
            retry_prompt,
            ApplicationMaterials,
        )
        retry.resume_rewrites = _valid_resume_rewrites(
            retry.resume_rewrites, resume_text
        )
        if len(retry.resume_rewrites) >= len(materials.resume_rewrites):
            materials = retry
    return materials


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _valid_resume_rewrites(items: list, resume_text: str) -> list:
    compact_resume = _compact_text(resume_text)
    valid = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        original = item.original.strip()
        suggested = item.suggested.strip()
        compact_original = _compact_text(original)
        compact_suggested = _compact_text(suggested)
        if len(compact_original) < 25 or len(compact_suggested) < 25:
            continue
        if compact_original not in compact_resume:
            continue
        if compact_original == compact_suggested:
            continue
        pair = (compact_original, compact_suggested)
        if pair in seen:
            continue
        seen.add(pair)
        valid.append(item)
    return valid


def total_score(result: Evaluation) -> int:
    return sum(getattr(result, field).score for field in WEIGHTS)


def jd_hash(jd: JDInfo) -> str:
    normalized = " ".join(
        [jd.company, jd.role, jd.full_text] + jd.responsibilities + jd.requirements
    ).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
