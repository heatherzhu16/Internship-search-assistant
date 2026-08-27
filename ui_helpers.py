from __future__ import annotations

import os
from datetime import date

import pandas as pd
import streamlit as st

from candidate_profile import JDConstraint
from models.evaluation import DIMENSION_NAMES, Evaluation, WEIGHTS
from scoring_policy import DecisionOutcome


CONSTRAINT_TYPES = [
    "arrival_date", "days_per_week", "duration_months", "location", "work_mode",
    "graduation_year", "education", "institution_tier", "student_status",
    "major", "language", "work_authorization", "other",
]


def apply_global_styles() -> None:
    """Apply the editorial layout details that Streamlit theming cannot express."""
    st.html(
        """
        <style>
        :root {
            --career-paper: #f4f1ea;
            --career-surface: #faf8f3;
            --career-ink: #191916;
            --career-muted: #716f68;
            --career-line: #d8d3c8;
            --career-accent: #c84b31;
        }

        html, body, [class*="css"] {
            font-family: Inter, -apple-system, BlinkMacSystemFont, "SF Pro Text",
                "PingFang SC", "Microsoft YaHei", sans-serif;
        }

        [data-testid="stMainBlockContainer"] {
            max-width: 1280px;
            padding-top: 0.75rem;
            padding-bottom: 6rem;
        }

        [data-testid="stSidebar"] [data-testid="stSidebarContent"] {
            padding-top: 1rem;
        }

        .st-key-page_heading {
            max-width: 980px;
            margin-bottom: 3.4rem;
        }

        .st-key-page_heading h1 {
            max-width: 900px;
            margin: 0.38rem 0 0.9rem;
            font-size: clamp(2.8rem, 5vw, 4.8rem);
            font-weight: 620;
            line-height: 0.98;
            letter-spacing: -0.065em;
        }

        .st-key-page_heading [data-testid="stCaptionContainer"]:first-child {
            color: var(--career-accent);
            font-family: "SFMono-Regular", Consolas, monospace;
            font-size: 0.72rem;
            font-weight: 650;
            letter-spacing: 0.12em;
        }

        .st-key-page_heading [data-testid="stCaptionContainer"]:last-child {
            max-width: 660px;
            color: #55534e;
            font-size: 1rem;
            line-height: 1.75;
        }

        [class*="st-key-section_"] {
            margin-top: 3.8rem;
            padding-top: 0.8rem;
            border-top: 1px solid var(--career-ink);
        }

        [class*="st-key-section_"] h3 {
            margin: 0;
            font-size: 1.45rem;
            font-weight: 620;
            letter-spacing: -0.035em;
        }

        .st-key-toolbox_navigation [data-testid="stPageLink"] a {
            min-height: 40px;
            justify-content: flex-start;
            border-color: transparent;
            background: transparent;
            color: #57534d;
        }

        .st-key-toolbox_navigation [data-testid="stPageLink"] a span {
            font-variation-settings: "FILL" 0, "wght" 350, "GRAD" 0, "opsz" 20;
        }

        [class*="st-key-featured_job_card_"] {
            position: relative;
            min-height: 300px;
            border-color: #d4cfc4 !important;
            background: #faf8f3 !important;
            transition: transform 180ms cubic-bezier(.22,.8,.28,1),
                border-color 180ms ease, background-color 180ms ease;
        }

        [class*="st-key-featured_job_card_"]:hover {
            transform: translateY(-2px);
            border-color: #aaa398 !important;
            background: #fdfbf7 !important;
        }

        [class*="st-key-featured_job_card_"] h3 {
            min-height: 3.1rem;
            margin: 0.3rem 0 0.1rem;
            font-size: 1.25rem;
            line-height: 1.25;
        }

        [class*="st-key-featured_job_card_"] [data-testid="stCaptionContainer"] {
            line-height: 1.55;
        }

        .st-key-featured_filters {
            margin-bottom: 1rem;
            padding: 0.85rem 0 0.15rem;
            border-top: 1px solid var(--career-line);
        }

        .st-key-featured_detail {
            margin: 1.25rem 0 2rem;
            border-left: 3px solid var(--career-accent) !important;
        }

        .st-key-toolbox_navigation [data-testid="stPageLink"] a:hover,
        .st-key-toolbox_navigation [data-testid="stPageLink"] a[aria-current="page"] {
            border-color: #d2cdc2;
            background: #e2ded5;
            color: var(--career-ink);
        }

        h1, h2, h3 {
            color: var(--career-ink);
            letter-spacing: -0.035em;
        }

        h1 {
            line-height: 1.04;
            margin-bottom: 0.35rem;
        }

        p, label, [data-testid="stCaptionContainer"] {
            letter-spacing: -0.008em;
        }

        [data-testid="stMetricValue"] {
            letter-spacing: -0.045em;
        }

        [data-testid="stMetric"] {
            min-height: 118px;
            padding: 1rem 1.05rem;
            background: var(--career-surface);
        }

        [data-testid="stMetricLabel"] {
            color: var(--career-muted);
            font-size: 0.76rem;
            letter-spacing: 0.045em;
        }

        [data-testid="stMetricValue"] {
            font-size: 2rem;
            font-weight: 620;
        }

        [data-testid="stDataFrame"], [data-testid="stDataEditor"] {
            border-radius: 6px;
            overflow: hidden;
        }

        [data-testid="stForm"], [data-testid="stVerticalBlockBorderWrapper"] {
            background: var(--career-surface);
        }

        [data-testid="stAlert"] {
            border-radius: 6px;
            box-shadow: none;
        }

        [data-baseweb="input"], [data-baseweb="select"], textarea {
            transition: border-color 150ms ease, background-color 150ms ease,
                box-shadow 150ms ease;
        }

        button, [role="button"] {
            transition: transform 120ms cubic-bezier(.2,.8,.2,1),
                background-color 160ms ease, border-color 160ms ease,
                color 160ms ease;
        }

        button:active, [role="button"]:active {
            transform: scale(0.985);
        }

        [data-testid="stMainBlockContainer"] > div {
            animation: career-enter 240ms cubic-bezier(.22,.8,.28,1) both;
        }

        @keyframes career-enter {
            from { opacity: 0; transform: translateY(7px); }
            to { opacity: 1; transform: translateY(0); }
        }

        @media (prefers-reduced-motion: reduce) {
            *, *::before, *::after {
                animation-duration: 0.01ms !important;
                animation-iteration-count: 1 !important;
                transition-duration: 0.01ms !important;
            }
        }

        @media (max-width: 900px) {
            [data-testid="stMainBlockContainer"] {
                padding-top: 0.75rem;
            }

        }
        </style>
        """
    )


def page_header(title: str, description: str, eyebrow: str) -> None:
    """Render a consistent editorial page heading."""
    page_numbers = {
        "Discover": "01", "Analyze": "02", "Create": "03",
        "Pipeline": "04", "Insights": "05", "Method": "06",
        "Library": "07", "Archive": "08", "Sync": "09",
    }
    with st.container(key="page_heading", gap=None):
        number = page_numbers.get(eyebrow, "—")
        st.caption(f"{number} / {eyebrow.upper()} · CAREER OS")
        st.title(title)
        st.caption(description)


def section_header(number: str, title: str, description: str = "") -> None:
    """Render a numbered workflow section without heavy dividers."""
    with st.container(key=f"section_{number}", gap=None):
        left, right = st.columns([1, 5], vertical_alignment="top")
        with left:
            st.caption(f"STEP / {number}")
        with right:
            st.subheader(title)
            if description:
                st.caption(description)


def empty_state(
    title: str,
    description: str,
    *,
    icon: str = ":material/inbox:",
) -> None:
    """Render an actionable, calm empty state container."""
    with st.container(border=True, horizontal_alignment="center"):
        st.markdown(icon, text_alignment="center")
        st.subheader(title, text_alignment="center")
        st.caption(description, text_alignment="center")


def api_settings() -> tuple[str, str]:
    return (
        str(st.session_state.get("api_key", os.getenv("DEEPSEEK_API_KEY", ""))).strip(),
        str(
            st.session_state.get(
                "model", os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
            )
        ).strip() or "deepseek-chat",
    )


def clean_cell(value) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def date_to_iso(value) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def constraints_dataframe(constraints: list[JDConstraint]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "类型": item.constraint_type, "运算符": item.operator,
                "值": item.value, "单位": item.unit,
                "重要性": item.importance, "JD原文": item.quote,
            }
            for item in constraints
        ],
        columns=["类型", "运算符", "值", "单位", "重要性", "JD原文"],
    )


def dataframe_to_constraints(data: pd.DataFrame) -> list[JDConstraint]:
    result = []
    for _, row in data.iterrows():
        values = {column: clean_cell(row.get(column)) for column in data.columns}
        if not any(values.values()):
            continue
        result.append(
            JDConstraint(
                constraint_type=values["类型"] if values["类型"] in CONSTRAINT_TYPES else "other",
                operator=values["运算符"], value=values["值"], unit=values["单位"],
                importance=values["重要性"] if values["重要性"] in {"必须", "优先", "加分"} else "必须",
                quote=values["JD原文"],
            )
        )
    return result


def render_evaluation(result: Evaluation) -> None:
    if result.calibration_notes:
        st.info(
            "v2.2 本地校准：" + "；".join(result.calibration_notes),
            icon=":material/tune:",
        )
    for field in WEIGHTS:
        item = getattr(result, field)
        with st.expander(
            f"{DIMENSION_NAMES[field]}：{item.score}/{item.max_score}", expanded=True
        ):
            if item.score_reason:
                st.markdown("**评分原因**")
                st.write(item.score_reason)
            if item.evidence:
                st.markdown("**得分证据**")
                for evidence in item.evidence:
                    st.write(f"✅ {evidence}")
            if item.gaps:
                st.markdown("**缺口或扣分点**")
                for gap in item.gaps:
                    st.write(f"⚠️ {gap}")


def render_eligibility(assessment, decision: DecisionOutcome | None = None) -> None:
    st.markdown("#### 资格门槛")
    st.caption("根据已确认求职档案本地核对，并正式参与最终投递建议。")
    st.info(assessment.summary, icon=":material/fact_check:")
    if decision and decision.recommendation_adjusted:
        st.caption(
            f"模型原始建议：{decision.original_model_recommendation or '未给出'}；"
            f"本地校准后：{decision.final_recommendation}。"
        )
    if assessment.checks:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "JD要求": check.requirement, "结果": check.result,
                        "你的信息": check.candidate_evidence, "来源": check.source,
                        "说明": check.notes,
                    }
                    for check in assessment.checks
                ]
            ),
            hide_index=True,
        )


def render_copyable(text: str) -> None:
    st.code(text.strip(), language=None, wrap_lines=True, height="content")
