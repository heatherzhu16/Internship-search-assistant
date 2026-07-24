import hashlib
import io
import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from docx import Document
from openai import OpenAI
from pydantic import BaseModel, Field
from pypdf import PdfReader


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "job_search.db"
DEMO_DATA_PATH = APP_DIR / "demo_data.csv"
load_dotenv(APP_DIR / ".env")


class JDInfo(BaseModel):
    company: str = ""
    role: str = ""
    company_type: str = ""
    location: str = ""
    salary: str = ""
    source: str = ""
    responsibilities: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    hard_requirements: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    full_text: str = ""


class DimensionScore(BaseModel):
    score: int
    max_score: int
    evidence: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class Evaluation(BaseModel):
    hard_requirements: DimensionScore
    experience_match: DimensionScore
    skills_tools: DimensionScore
    motivation: DimensionScore
    education: DimensionScore
    keyword_evidence: DimensionScore
    hard_gate_result: str
    hard_gate_notes: list[str] = Field(default_factory=list)
    matched_keywords: list[str] = Field(default_factory=list)
    missing_keywords: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    resume_suggestions: list[str] = Field(default_factory=list)
    recommendation: str
    recommendation_reason: str


class ResumeRewrite(BaseModel):
    location: str = ""
    original: str = ""
    suggested: str = ""
    rationale: str = ""
    supported_keywords: list[str] = Field(default_factory=list)


class ApplicationMaterials(BaseModel):
    strategy_summary: str = ""
    priority_actions: list[str] = Field(default_factory=list)
    resume_rewrites: list[ResumeRewrite] = Field(default_factory=list)
    chinese_email_subject: str = ""
    chinese_email_body: str = ""
    english_email_subject: str = ""
    english_email_body: str = ""
    boss_message: str = ""
    truthfulness_notes: list[str] = Field(default_factory=list)


WEIGHTS = {
    "hard_requirements": 25,
    "experience_match": 25,
    "skills_tools": 20,
    "motivation": 10,
    "education": 10,
    "keyword_evidence": 10,
}

DIMENSION_NAMES = {
    "hard_requirements": "硬性要求",
    "experience_match": "实习、项目与工作内容匹配",
    "skills_tools": "专业技能和工具",
    "motivation": "行业与岗位动机",
    "education": "教育背景",
    "keyword_evidence": "简历关键词及证据充分程度",
}

APPLICATION_STATUSES = [
    "待投递",
    "已投递",
    "笔试",
    "一面",
    "二面",
    "终面",
    "Offer",
    "拒绝",
    "放弃",
]
PROGRESS_STAGES = ["待投递", "已投递", "笔试", "一面", "二面", "终面", "Offer"]
STAGE_RANK = {stage: rank for rank, stage in enumerate(PROGRESS_STAGES)}
TERMINAL_STATUSES = {"Offer", "拒绝", "放弃"}


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                applied_date TEXT,
                company TEXT,
                company_type TEXT,
                role TEXT,
                location TEXT,
                salary TEXT,
                source TEXT,
                status TEXT,
                score INTEGER,
                recommendation TEXT,
                jd_hash TEXT UNIQUE,
                jd_text TEXT,
                evaluation_json TEXT,
                notes TEXT
            )
            """
        )
        existing_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(applications)")
        }
        migrations = {
            "resume_version": "TEXT DEFAULT '默认版'",
            "highest_stage": "TEXT DEFAULT '待投递'",
            "next_follow_up_date": "TEXT",
        }
        added_highest_stage = "highest_stage" not in existing_columns
        for column, definition in migrations.items():
            if column not in existing_columns:
                conn.execute(
                    f"ALTER TABLE applications ADD COLUMN {column} {definition}"
                )
        conn.execute(
            """
            UPDATE applications
            SET resume_version = '默认版'
            WHERE resume_version IS NULL OR TRIM(resume_version) = ''
            """
        )
        if added_highest_stage:
            conn.execute(
                """
                UPDATE applications
                SET highest_stage = CASE status
                    WHEN 'Offer' THEN 'Offer'
                    WHEN '终面' THEN '终面'
                    WHEN '二面' THEN '二面'
                    WHEN '一面' THEN '一面'
                    WHEN '笔试' THEN '笔试'
                    WHEN '已投递' THEN '已投递'
                    WHEN '拒绝' THEN '已投递'
                    WHEN '放弃' THEN '已投递'
                    ELSE '待投递'
                END
                """
            )


def status_to_stage(status: str) -> str:
    if status in PROGRESS_STAGES:
        return status
    if status in {"拒绝", "放弃"}:
        return "已投递"
    return "待投递"


def file_to_text(uploaded_file) -> str:
    suffix = Path(uploaded_file.name).suffix.lower()
    raw = uploaded_file.getvalue()
    if suffix == ".pdf":
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if suffix == ".docx":
        document = Document(io.BytesIO(raw))
        return "\n".join(p.text for p in document.paragraphs)
    if suffix == ".txt":
        return raw.decode("utf-8", errors="ignore")
    raise ValueError("只支持 PDF、DOCX 或 TXT 简历。")


def get_client(api_key: str) -> OpenAI:
    if not api_key:
        raise ValueError("请先在左侧输入 DeepSeek API Key。")
    return OpenAI(api_key=api_key, base_url="https://api.deepseek.com")


def call_deepseek_json(
    client: OpenAI,
    model: str,
    system_prompt: str,
    user_prompt: str,
    output_model: type[BaseModel],
) -> BaseModel:
    schema = json.dumps(output_model.model_json_schema(), ensure_ascii=False)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    f"{system_prompt}\n"
                    "必须只输出一个合法 JSON 对象，不要输出 Markdown 代码块或解释文字。"
                    f"\nJSON Schema：\n{schema}"
                ),
            },
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        max_tokens=6000,
        extra_body={"thinking": {"type": "disabled"}},
    )
    content = response.choices[0].message.content
    if not content:
        raise ValueError("DeepSeek 返回了空结果，请重新点击一次。")
    return output_model.model_validate_json(content)


def extract_jd(client: OpenAI, model: str, jd_text: str) -> JDInfo:
    system_prompt = (
        "你是职位信息结构化助手。忠实提取用户粘贴的 JD，不猜测文本中不存在的"
        "公司、地点和薪资，缺失字段使用空字符串或空数组。full_text 应整理为完整、"
        "可读的 JD。hard_requirements 只包含学历、毕业年份、专业、语言、到岗天数、"
        "实习期限等可能一票否决的条件。keywords 去重。"
    )
    return call_deepseek_json(
        client,
        model,
        system_prompt,
        f"请将以下岗位信息提取为 JSON：\n\n{jd_text}",
        JDInfo,
    )


def evaluate_match(
    client: OpenAI, model: str, resume_text: str, jd: JDInfo
) -> Evaluation:
    rubric = "\n".join(
        f"- {DIMENSION_NAMES[key]}：{value}分" for key, value in WEIGHTS.items()
    )
    prompt = f"""
你是严谨的实习求职匹配评估员。只根据下方简历和 JD 评分，不得替候选人虚构经历。

评分维度：
{rubric}

评分规则：
1. 每个维度的 max_score 必须严格等于给定分值，score 必须在 0 到 max_score 之间。
2. evidence 必须引用或紧贴简历中的事实；没有证据就扣分并写进 gaps。
3. 将硬性条件单独判断：满足、存疑、不满足。存疑不能当作满足。
4. recommendation 只能是“优先投递”“建议投递”“谨慎投递”“不建议投递”之一。
5. 行业动机如果简历没有直接证据，不要仅凭专业名称给满分。
6. 简历关键词只在有经历、成果、课程或工具证据时计分。
7. 修改建议只能优化表达和排序，不能让候选人添加不存在的经历或技能。

简历：
{resume_text}

JD（结构化信息）：
{json.dumps(jd.model_dump(), ensure_ascii=False)}
"""
    result = call_deepseek_json(
        client,
        model,
        "你是严谨的实习求职匹配评估员，只能依据用户提供的简历和 JD 作出判断。",
        prompt,
        Evaluation,
    )
    for field, maximum in WEIGHTS.items():
        item = getattr(result, field)
        item.max_score = maximum
        item.score = max(0, min(item.score, maximum))
    return result


def generate_application_materials(
    client: OpenAI,
    model: str,
    resume_text: str,
    jd: JDInfo,
    evaluation: Evaluation,
) -> ApplicationMaterials:
    prompt = f"""
请基于简历、JD 和匹配评估，生成一套可直接使用的求职材料。

严格要求：
1. 不得虚构、夸大或补写简历中不存在的经历、成果、技能、数字、公司或身份。
2. resume_rewrites 生成 3 到 6 项最值得修改的内容，按重要程度排序。
3. 每项 original 必须是简历中的原文片段；suggested 只能调整结构、措辞、顺序和
   与 JD 相关的重点，不能增加原文没有的事实。
4. location 写明原文所在位置，例如“个人简介”“某段实习经历”“项目经历”。
5. supported_keywords 只填写改写后有真实简历证据支撑的 JD 关键词。
6. 中英文求职邮件都要简洁、具体，包含明确主题；不知道招聘者姓名时使用通用称呼。
7. 英文邮件必须是自然的英文表达，不要逐字翻译中文邮件。
8. BOSS 私信控制在 180 个中文字符以内，语气自然，适合首次联系招聘者。
9. 如果简历缺少姓名、联系方式等信息，不要编造；邮件末尾使用“[你的姓名]”等占位符。
10. truthfulness_notes 记录仍需用户确认的表述；没有则返回空数组。

简历：
{resume_text}

JD：
{json.dumps(jd.model_dump(), ensure_ascii=False)}

匹配评估：
{evaluation.model_dump_json()}
"""
    return call_deepseek_json(
        client,
        model,
        "你是严谨的求职材料编辑，只能优化用户已有的真实经历，绝不能虚构事实。",
        prompt,
        ApplicationMaterials,
    )


def total_score(result: Evaluation) -> int:
    return sum(getattr(result, field).score for field in WEIGHTS)


def jd_hash(jd: JDInfo) -> str:
    normalized = " ".join(
        [jd.company, jd.role, jd.full_text] + jd.responsibilities + jd.requirements
    ).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def save_application(jd: JDInfo, result: Evaluation, fields: dict) -> bool:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO applications (
                    created_at, applied_date, company, company_type, role,
                    location, salary, source, status, score, recommendation,
                    jd_hash, jd_text, evaluation_json, notes, resume_version,
                    highest_stage, next_follow_up_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now().isoformat(timespec="seconds"),
                    fields["applied_date"],
                    fields["company"],
                    fields["company_type"],
                    fields["role"],
                    fields["location"],
                    fields["salary"],
                    fields["source"],
                    fields["status"],
                    total_score(result),
                    result.recommendation,
                    jd_hash(jd),
                    jd.full_text,
                    result.model_dump_json(),
                    fields["notes"],
                    fields["resume_version"],
                    fields["highest_stage"],
                    fields["next_follow_up_date"],
                ),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def load_applications() -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query(
            """
            SELECT id AS ID, applied_date AS 投递日期, company AS 公司,
                   company_type AS 公司类型, role AS 职位, location AS Base地,
                   salary AS 薪资, source AS 信息来源, status AS 投递状态,
                   highest_stage AS 最高进展, resume_version AS 简历版本,
                   next_follow_up_date AS 下次跟进, score AS 匹配分,
                   recommendation AS 投递建议, notes AS 备注
            FROM applications ORDER BY id DESC
            """,
            conn,
        )


def load_demo_applications() -> pd.DataFrame:
    if not DEMO_DATA_PATH.exists():
        return pd.DataFrame()
    demo = pd.read_csv(DEMO_DATA_PATH)
    today = date.today()
    demo["投递日期"] = demo["投递日期偏移"].apply(
        lambda days: (today + timedelta(days=int(days))).isoformat()
    )
    demo["下次跟进"] = demo["跟进日期偏移"].apply(
        lambda days: (
            (today + timedelta(days=int(days))).isoformat()
            if pd.notna(days)
            else ""
        )
    )
    return demo.drop(columns=["投递日期偏移", "跟进日期偏移"])


def clean_cell(value) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def date_cell_to_iso(value) -> str:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    parsed = pd.to_datetime(value, errors="coerce")
    return "" if pd.isna(parsed) else parsed.date().isoformat()


def save_ledger_updates(edited: pd.DataFrame) -> int:
    updated = 0
    with sqlite3.connect(DB_PATH) as conn:
        for _, row in edited.iterrows():
            status = clean_cell(row["投递状态"])
            highest_stage = clean_cell(row["最高进展"])
            if status not in APPLICATION_STATUSES:
                continue
            if highest_stage not in PROGRESS_STAGES:
                highest_stage = status_to_stage(status)
            conn.execute(
                """
                UPDATE applications
                SET status = ?, highest_stage = ?, resume_version = ?,
                    next_follow_up_date = ?, notes = ?
                WHERE id = ?
                """,
                (
                    status,
                    highest_stage,
                    clean_cell(row["简历版本"]) or "默认版",
                    date_cell_to_iso(row["下次跟进"]),
                    clean_cell(row["备注"]),
                    int(row["ID"]),
                ),
            )
            updated += 1
    return updated


def stage_rank(stage: str) -> int:
    return STAGE_RANK.get(clean_cell(stage), 0)


def prepare_dashboard_data(real_data: pd.DataFrame, scope: str) -> pd.DataFrame:
    demo_data = load_demo_applications()
    if scope == "仅演示数据":
        data = demo_data.copy()
    elif scope == "真实 + 演示":
        data = pd.concat([real_data, demo_data], ignore_index=True)
    else:
        data = real_data.copy()
    if data.empty:
        return data
    data["投递日期"] = pd.to_datetime(data["投递日期"], errors="coerce").dt.date
    data["下次跟进"] = pd.to_datetime(data["下次跟进"], errors="coerce").dt.date
    data["阶段序号"] = data["最高进展"].map(stage_rank)
    data["已投递标记"] = data["阶段序号"] >= STAGE_RANK["已投递"]
    data["面试标记"] = data["阶段序号"] >= STAGE_RANK["一面"]
    data["Offer标记"] = data["阶段序号"] >= STAGE_RANK["Offer"]
    return data


def funnel_summary(data: pd.DataFrame) -> pd.DataFrame:
    stages = [
        ("已投递", "已投递"),
        ("进入笔试", "笔试"),
        ("进入面试", "一面"),
        ("进入终面", "终面"),
        ("获得 Offer", "Offer"),
    ]
    return pd.DataFrame(
        {
            "阶段": [label for label, _ in stages],
            "数量": [
                int((data["阶段序号"] >= STAGE_RANK[stage]).sum())
                for _, stage in stages
            ],
        }
    )


def resume_version_summary(data: pd.DataFrame) -> pd.DataFrame:
    applied = data[data["已投递标记"]].copy()
    if applied.empty:
        return pd.DataFrame()
    applied["简历版本"] = applied["简历版本"].fillna("").replace("", "默认版")
    summary = (
        applied.groupby("简历版本", as_index=False)
        .agg(
            投递数=("ID", "count"),
            面试数=("面试标记", "sum"),
            Offer数=("Offer标记", "sum"),
        )
        .sort_values(["面试数", "投递数"], ascending=False)
    )
    summary["面试转化率"] = summary["面试数"] / summary["投递数"] * 100
    summary["Offer转化率"] = summary["Offer数"] / summary["投递数"] * 100
    return summary


def follow_up_summary(data: pd.DataFrame) -> pd.DataFrame:
    if data.empty:
        return data
    today = date.today()
    pending = data[
        data["下次跟进"].notna()
        & ~data["投递状态"].isin(TERMINAL_STATUSES)
    ].copy()
    if pending.empty:
        return pending
    pending["距跟进"] = pending["下次跟进"].apply(
        lambda target: (
            f"逾期 {(today - target).days} 天"
            if target < today
            else ("今天" if target == today else f"{(target - today).days} 天后")
        )
    )
    return pending.sort_values("下次跟进")


def dataframe_to_excel(df: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="投递台账", index=False)
        sheet = writer.book["投递台账"]
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        widths = {
            "A": 8, "B": 14, "C": 20, "D": 14, "E": 24, "F": 14,
            "G": 14, "H": 16, "I": 14, "J": 14, "K": 16, "L": 14,
            "M": 10, "N": 14, "O": 28,
        }
        for column, width in widths.items():
            sheet.column_dimensions[column].width = width
    return output.getvalue()


def render_copyable_text(text: str, height: int | str = "content") -> None:
    st.code(
        text.strip(),
        language=None,
        wrap_lines=True,
        height=height,
        width="stretch",
    )


def render_dimension(result: Evaluation, field: str) -> None:
    item = getattr(result, field)
    with st.expander(
        f"{DIMENSION_NAMES[field]}：{item.score}/{item.max_score}",
        expanded=True,
    ):
        if item.evidence:
            st.markdown("**得分证据**")
            for evidence in item.evidence:
                st.write(f"✅ {evidence}")
        if item.gaps:
            st.markdown("**缺口或扣分点**")
            for gap in item.gaps:
                st.write(f"⚠️ {gap}")


init_db()
st.set_page_config(
    page_title="实习求职助手",
    page_icon=":material/explore:",
    layout="wide",
)
st.title("实习求职助手 · 第三阶段")
st.caption("岗位分析、定向求职材料、投递管理与求职复盘")

with st.sidebar:
    st.subheader("模型设置")
    api_key = st.text_input(
        "DeepSeek API Key",
        value=os.getenv("DEEPSEEK_API_KEY", ""),
        type="password",
        help="只保存在本次浏览器会话；也可写入项目目录的 .env。",
    )
    model = st.text_input(
        "模型名称",
        value=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        help="推荐保持 deepseek-v4-flash，速度快、价格低。",
    )
    st.caption("服务商：DeepSeek · 文本模式")

analysis_tab, materials_tab, review_tab, ledger_tab, rubric_tab = st.tabs(
    ["① 岗位分析", "② 求职材料", "③ 复盘看板", "④ 投递台账", "⑤ 评分规则"]
)

with analysis_tab:
    st.subheader("1. 上传简历")
    resume_file = st.file_uploader(
        "支持 PDF、DOCX、TXT", type=["pdf", "docx", "txt"], key="resume"
    )
    resume_text = ""
    if resume_file:
        try:
            resume_text = file_to_text(resume_file)
            current_resume_hash = hashlib.sha256(
                resume_text.encode("utf-8")
            ).hexdigest()
            previous_resume_hash = st.session_state.get("resume_hash")
            if previous_resume_hash and previous_resume_hash != current_resume_hash:
                st.session_state.pop("evaluation", None)
                st.session_state.pop("application_materials", None)
            st.session_state.resume_hash = current_resume_hash
            if len(resume_text.strip()) < 100:
                st.warning("提取到的简历文字很少；如果是扫描版 PDF，请先转成可复制文字的 PDF。")
            else:
                st.success(f"简历读取成功，共 {len(resume_text)} 个字符。")
        except Exception as exc:
            st.error(f"简历读取失败：{exc}")
    elif st.session_state.pop("resume_hash", None):
        st.session_state.pop("evaluation", None)
        st.session_state.pop("application_materials", None)

    st.subheader("2. 输入岗位 JD")
    jd_text_input = st.text_area(
        "粘贴 JD 文字",
        height=260,
        placeholder="将岗位职责和任职要求完整粘贴到这里……",
    )
    st.info(
        "DeepSeek API 暂不直接识别图片。Mac 用户可以在 JD 截图中长按或拖动选中文字，"
        "复制后粘贴到上方文本框。"
    )

    if st.button("提取并整理 JD", type="primary", width="stretch"):
        try:
            if not jd_text_input.strip():
                raise ValueError("请先粘贴 JD 文字。")
            with st.spinner("正在读取 JD……"):
                st.session_state.jd_info = extract_jd(
                    get_client(api_key), model, jd_text_input
                )
                st.session_state.pop("evaluation", None)
                st.session_state.pop("application_materials", None)
                st.session_state.jd_editor_nonce = (
                    st.session_state.get("jd_editor_nonce", 0) + 1
                )
            st.success("JD 已提取。请检查并修改下方内容，再开始评分。")
        except Exception as exc:
            st.error(f"JD 提取失败：{exc}")

    if "jd_info" in st.session_state:
        jd = st.session_state.jd_info
        nonce = st.session_state.get("jd_editor_nonce", 0)
        st.subheader("3. 确认 JD 信息")
        col1, col2, col3 = st.columns(3)
        with col1:
            company = st.text_input("公司", value=jd.company, key=f"company_{nonce}")
            company_type = st.text_input(
                "公司类型", value=jd.company_type, key=f"company_type_{nonce}"
            )
            source = st.text_input(
                "信息来源", value=jd.source, key=f"source_{nonce}"
            )
        with col2:
            role = st.text_input("职位", value=jd.role, key=f"role_{nonce}")
            location = st.text_input(
                "Base 地", value=jd.location, key=f"location_{nonce}"
            )
            salary = st.text_input("薪资", value=jd.salary, key=f"salary_{nonce}")
        with col3:
            applied_date = st.date_input(
                "投递日期", value=date.today(), key=f"applied_date_{nonce}"
            )
            status = st.selectbox(
                "投递状态",
                APPLICATION_STATUSES,
                key=f"status_{nonce}",
            )
            resume_version = st.text_input(
                "简历版本",
                value="默认版",
                key=f"resume_version_{nonce}",
                help="例如：产品版、数据分析版、咨询版。",
            )
            next_follow_up_date = st.date_input(
                "下次跟进日期",
                value=None,
                key=f"next_follow_up_{nonce}",
                help="可以留空；填写后会出现在复盘看板的待跟进提醒中。",
            )
            notes = st.text_input("备注", key=f"notes_{nonce}")

        confirmed_text = st.text_area(
            "整理后的完整 JD（可修改）",
            value=jd.full_text,
            height=260,
            key=f"confirmed_text_{nonce}",
        )
        jd.company = company.strip()
        jd.company_type = company_type.strip()
        jd.source = source.strip()
        jd.role = role.strip()
        jd.location = location.strip()
        jd.salary = salary.strip()
        jd.full_text = confirmed_text.strip()

        if st.button("开始六维评分", type="primary", width="stretch"):
            try:
                if not resume_text.strip():
                    raise ValueError("请先上传并成功读取简历。")
                if not jd.full_text.strip():
                    raise ValueError("整理后的 JD 不能为空。")
                with st.spinner("正在逐项寻找简历证据并评分……"):
                    st.session_state.evaluation = evaluate_match(
                        get_client(api_key), model, resume_text, jd
                    )
                    st.session_state.pop("application_materials", None)
                st.success("评分完成。")
            except Exception as exc:
                st.error(f"评分失败：{exc}")

        if "evaluation" in st.session_state:
            result = st.session_state.evaluation
            st.divider()
            st.subheader("4. 评分结果")
            metric1, metric2, metric3 = st.columns(3)
            metric1.metric("综合匹配分", f"{total_score(result)}/100")
            metric2.metric("硬性条件", result.hard_gate_result)
            metric3.metric("投递建议", result.recommendation)
            st.info(result.recommendation_reason)

            for field in WEIGHTS:
                render_dimension(result, field)

            left, right = st.columns(2)
            with left:
                st.markdown("**匹配关键词**")
                st.write("、".join(result.matched_keywords) or "暂无")
                st.markdown("**主要优势**")
                for item in result.strengths:
                    st.write(f"✅ {item}")
            with right:
                st.markdown("**缺失关键词**")
                st.write("、".join(result.missing_keywords) or "暂无")
                st.markdown("**主要风险**")
                for item in result.risks:
                    st.write(f"⚠️ {item}")

            st.markdown("**针对该 JD 的简历修改建议**")
            for index, suggestion in enumerate(result.resume_suggestions, 1):
                st.write(f"{index}. {suggestion}")

            fields = {
                "applied_date": applied_date.isoformat(),
                "company": company.strip(),
                "company_type": company_type.strip(),
                "role": role.strip(),
                "location": location.strip(),
                "salary": salary.strip(),
                "source": source.strip(),
                "status": status,
                "resume_version": resume_version.strip() or "默认版",
                "highest_stage": status_to_stage(status),
                "next_follow_up_date": (
                    next_follow_up_date.isoformat()
                    if next_follow_up_date
                    else ""
                ),
                "notes": notes.strip(),
            }
            if st.button("保存到投递台账", width="stretch"):
                if save_application(jd, result, fields):
                    st.success("已保存到投递台账。")
                else:
                    st.warning("这个岗位已经保存过，为避免重复，本次没有再次写入。")

with materials_tab:
    st.subheader("针对岗位的求职材料")
    st.caption("先完成岗位分析和六维评分，再生成与该岗位对应的材料。")

    if "evaluation" not in st.session_state or "jd_info" not in st.session_state:
        st.info(
            "还没有可用的评分结果。请先在“岗位分析”中上传简历、整理 JD 并完成评分。",
            icon=":material/info:",
        )
    elif not resume_text.strip():
        st.warning(
            "当前没有读取到简历。请回到“岗位分析”重新上传简历。",
            icon=":material/warning:",
        )
    else:
        materials_jd = st.session_state.jd_info
        materials_evaluation = st.session_state.evaluation
        target_name = " · ".join(
            item for item in [materials_jd.company, materials_jd.role] if item
        )
        if target_name:
            st.badge(target_name, icon=":material/work:", color="blue")

        if st.button(
            "生成第二阶段求职材料",
            type="primary",
            icon=":material/auto_awesome:",
            width="stretch",
        ):
            try:
                with st.spinner("正在生成简历改写对比和沟通文案……"):
                    st.session_state.application_materials = (
                        generate_application_materials(
                            get_client(api_key),
                            model,
                            resume_text,
                            materials_jd,
                            materials_evaluation,
                        )
                    )
                st.success("求职材料已生成。")
            except Exception as exc:
                st.error(f"求职材料生成失败：{exc}")

        if "application_materials" in st.session_state:
            materials = st.session_state.application_materials
            st.info(materials.strategy_summary, icon=":material/lightbulb:")

            if materials.priority_actions:
                st.markdown("#### 优先修改建议")
                for index, action in enumerate(materials.priority_actions, 1):
                    st.write(f"{index}. {action}")

            st.markdown("#### 简历原文 / 建议版本")
            st.caption("每个文本框右上角都有复制按钮；修改前请再次确认内容符合真实经历。")
            if not materials.resume_rewrites:
                st.info("本次没有生成可安全改写的简历片段。")
            for index, rewrite in enumerate(materials.resume_rewrites, 1):
                with st.container(border=True):
                    st.markdown(
                        f"**{index}. {rewrite.location or '简历内容'}**"
                    )
                    original_col, suggested_col = st.columns(2)
                    with original_col:
                        st.caption("原文")
                        render_copyable_text(rewrite.original)
                    with suggested_col:
                        st.caption("建议版本")
                        render_copyable_text(rewrite.suggested)
                    if rewrite.rationale:
                        st.caption(f"修改理由：{rewrite.rationale}")
                    if rewrite.supported_keywords:
                        st.caption(
                            "对应关键词："
                            + "、".join(rewrite.supported_keywords)
                        )

            st.markdown("#### 沟通文案")
            chinese_tab, english_tab, boss_tab = st.tabs(
                ["中文求职邮件", "英文求职邮件", "BOSS 私信短版"]
            )
            with chinese_tab:
                chinese_email = (
                    f"主题：{materials.chinese_email_subject}\n\n"
                    f"{materials.chinese_email_body}"
                )
                render_copyable_text(chinese_email)
            with english_tab:
                english_email = (
                    f"Subject: {materials.english_email_subject}\n\n"
                    f"{materials.english_email_body}"
                )
                render_copyable_text(english_email)
            with boss_tab:
                render_copyable_text(materials.boss_message)
                st.caption(f"当前长度：{len(materials.boss_message)} 个字符")

            if materials.truthfulness_notes:
                with st.expander(
                    "发送前需要确认",
                    icon=":material/fact_check:",
                ):
                    for note in materials.truthfulness_notes:
                        st.write(f"- {note}")

with review_tab:
    st.subheader("求职复盘看板")
    data_scope = st.segmented_control(
        "数据范围",
        ["真实数据", "真实 + 演示", "仅演示数据"],
        default="真实数据",
        required=True,
        key="review_data_scope",
    )
    if data_scope != "真实数据":
        st.caption("演示数据按今天动态计算日期，不会写入你的投递台账。")

    dashboard_data = prepare_dashboard_data(load_applications(), data_scope)
    if dashboard_data.empty:
        st.info(
            "当前范围还没有数据。保存投递记录，或切换到“仅演示数据”查看完整看板。",
            icon=":material/info:",
        )
    else:
        applied_count = int(dashboard_data["已投递标记"].sum())
        interview_count = int(dashboard_data["面试标记"].sum())
        offer_count = int(dashboard_data["Offer标记"].sum())
        interview_rate = (
            interview_count / applied_count * 100 if applied_count else 0
        )
        offer_rate = offer_count / applied_count * 100 if applied_count else 0
        pending_follow_ups = follow_up_summary(dashboard_data)

        with st.container(horizontal=True):
            st.metric("已投递", applied_count, border=True)
            st.metric("进入面试", interview_count, border=True)
            st.metric(
                "面试转化率",
                f"{interview_rate:.1f}%",
                border=True,
                help="进入过一面及以上的岗位数 ÷ 已投递岗位数。",
            )
            st.metric(
                "Offer 转化率",
                f"{offer_rate:.1f}%",
                border=True,
                help="获得 Offer 的岗位数 ÷ 已投递岗位数。",
            )
            st.metric("待跟进", len(pending_follow_ups), border=True)

        funnel_col, company_col = st.columns(2)
        with funnel_col:
            with st.container(border=True):
                st.markdown("#### 投递漏斗")
                funnel = funnel_summary(dashboard_data)
                st.bar_chart(
                    funnel,
                    x="阶段",
                    y="数量",
                    sort=False,
                    color="primary",
                )
                st.caption("按每条记录的“最高进展”统计，因此拒绝后仍保留曾到达的阶段。")

        with company_col:
            with st.container(border=True):
                st.markdown("#### 不同公司类型的投递数量")
                applied_data = dashboard_data[
                    dashboard_data["已投递标记"]
                ].copy()
                if applied_data.empty:
                    st.info("还没有已投递记录。")
                else:
                    applied_data["公司类型"] = applied_data["公司类型"].apply(
                        lambda value: clean_cell(value) or "未标注"
                    )
                    company_counts = (
                        applied_data["公司类型"]
                        .value_counts()
                        .rename_axis("公司类型")
                        .reset_index(name="投递数")
                    )
                    st.bar_chart(
                        company_counts,
                        x="公司类型",
                        y="投递数",
                        horizontal=True,
                        sort="-投递数",
                    )

        with st.container(border=True):
            st.markdown("#### 待跟进提醒")
            if pending_follow_ups.empty:
                st.success(
                    "目前没有待跟进事项。",
                    icon=":material/check_circle:",
                )
            else:
                st.dataframe(
                    pending_follow_ups[
                        [
                            "下次跟进",
                            "距跟进",
                            "公司",
                            "职位",
                            "投递状态",
                            "简历版本",
                        ]
                    ],
                    column_config={
                        "下次跟进": st.column_config.DateColumn(
                            "下次跟进", format="YYYY-MM-DD"
                        )
                    },
                    hide_index=True,
                )

        with st.container(border=True):
            st.markdown("#### 不同简历版本的效果比较")
            version_summary = resume_version_summary(dashboard_data)
            if version_summary.empty:
                st.info("还没有可比较的已投递记录。")
            else:
                st.dataframe(
                    version_summary,
                    column_config={
                        "投递数": st.column_config.NumberColumn(format="%d"),
                        "面试数": st.column_config.NumberColumn(format="%d"),
                        "Offer数": st.column_config.NumberColumn(format="%d"),
                        "面试转化率": st.column_config.NumberColumn(
                            format="%.1f%%"
                        ),
                        "Offer转化率": st.column_config.NumberColumn(
                            format="%.1f%%"
                        ),
                    },
                    hide_index=True,
                )
                if (version_summary["投递数"] < 5).any():
                    st.caption("样本少于 5 次投递的版本波动较大，建议积累更多数据后再判断。")

with ledger_tab:
    st.subheader("投递台账")
    applications = load_applications()
    if applications.empty:
        st.info("台账还是空的。完成一次岗位评分后，点击“保存到投递台账”。")
    else:
        st.caption("可编辑投递状态、最高进展、简历版本、下次跟进日期和备注。")
        ledger_view = applications.copy()
        ledger_view["投递日期"] = pd.to_datetime(
            ledger_view["投递日期"], errors="coerce"
        ).dt.date
        ledger_view["下次跟进"] = pd.to_datetime(
            ledger_view["下次跟进"], errors="coerce"
        ).dt.date
        with st.form("ledger_update_form"):
            edited_applications = st.data_editor(
                ledger_view,
                key="ledger_editor",
                column_config={
                    "ID": st.column_config.NumberColumn("ID", pinned=True),
                    "投递日期": st.column_config.DateColumn(
                        "投递日期", format="YYYY-MM-DD"
                    ),
                    "投递状态": st.column_config.SelectboxColumn(
                        "投递状态",
                        options=APPLICATION_STATUSES,
                        required=True,
                    ),
                    "最高进展": st.column_config.SelectboxColumn(
                        "最高进展",
                        options=PROGRESS_STAGES,
                        required=True,
                        help="记录这次投递曾到达的最高阶段，用于准确计算漏斗。",
                    ),
                    "简历版本": st.column_config.TextColumn(
                        "简历版本",
                        help="例如：产品版、数据分析版、咨询版。",
                    ),
                    "下次跟进": st.column_config.DateColumn(
                        "下次跟进",
                        format="YYYY-MM-DD",
                        required=False,
                    ),
                },
                disabled=[
                    "ID",
                    "投递日期",
                    "公司",
                    "公司类型",
                    "职位",
                    "Base地",
                    "薪资",
                    "信息来源",
                    "匹配分",
                    "投递建议",
                ],
                hide_index=True,
                num_rows="fixed",
            )
            save_updates = st.form_submit_button(
                "保存台账修改",
                type="primary",
                icon=":material/save:",
                width="stretch",
            )
        if save_updates:
            updated_count = save_ledger_updates(edited_applications)
            st.success(f"已更新 {updated_count} 条投递记录。")

        st.download_button(
            "下载 Excel 台账",
            data=dataframe_to_excel(applications),
            file_name=f"投递台账_{date.today().isoformat()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )

with rubric_tab:
    st.subheader("当前评分维度")
    rubric_df = pd.DataFrame(
        {
            "维度": [DIMENSION_NAMES[key] for key in WEIGHTS],
            "分值": list(WEIGHTS.values()),
        }
    )
    st.table(rubric_df)
    st.markdown(
        """
        评分之外还会单独给出：

        - 硬性条件判断：满足 / 存疑 / 不满足
        - 匹配关键词与缺失关键词
        - 投递优先级和证据充分程度
        - 只能基于真实经历执行的简历修改建议
        """
    )
