from datetime import date

import altair as alt
import pandas as pd
import streamlit as st

from services.dashboard import funnel_summary, prepare_dashboard_data, resume_summary
from services.database import load_applications
from ui_helpers import empty_state, page_header, section_header


page_header(
    "复盘看板",
    "从投递节奏和转化结果中找到下一步行动，而不只是浏览数字。",
    "Insights",
)
scope = st.segmented_control(
    "数据范围",
    ["仅真实数据", "真实 + 演示", "仅演示数据"],
    default="仅真实数据",
)
data = prepare_dashboard_data(load_applications(), scope or "仅真实数据")
if data.empty:
    empty_state(
        "还没有可复盘的数据",
        "把完成评分的岗位加入投递计划后，这里会自动生成漏斗和转化洞察。",
        icon=":material/monitoring:",
    )
    st.page_link(
        "app_pages/job_analysis.py",
        label="前往岗位分析",
        icon=":material/arrow_forward:",
        width="stretch",
    )
    st.stop()

applied = int(data["已投递标记"].sum())
interviews = int(data["面试标记"].sum())
offers = int(data["Offer标记"].sum())
follow = data.copy()
follow["下次跟进日期"] = pd.to_datetime(follow["下次跟进"], errors="coerce")
follow = follow[
    follow["下次跟进日期"].notna()
    & (follow["下次跟进日期"] <= pd.Timestamp(date.today()))
    & ~follow["投递状态"].isin(["Offer", "拒绝", "放弃"])
]

with st.container(horizontal=True):
    st.metric("岗位总数", len(data), border=True)
    st.metric("已投递", applied, border=True)
    st.metric(
        "面试转化率",
        f"{interviews / applied:.1%}" if applied else "—",
        border=True,
    )
    st.metric(
        "Offer 转化率",
        f"{offers / applied:.1%}" if applied else "—",
        border=True,
    )

section_header("01", "转化与行动", "用最高进展观察转化，用到期事项决定今天先做什么。")
left, right = st.columns([1.55, 1])
with left:
    with st.container(border=True, height="stretch"):
        st.subheader("投递漏斗")
        st.caption("按事件历史中的最高进展统计")
        funnel = funnel_summary(data)
        funnel_chart = (
            alt.Chart(funnel)
            .mark_bar(size=24, cornerRadiusEnd=2, color="#C84B31")
            .encode(
                x=alt.X("岗位数:Q", title=None, axis=alt.Axis(tickCount=5, grid=True)),
                y=alt.Y("阶段:N", title=None, sort=None),
                tooltip=["阶段:N", "岗位数:Q"],
            )
            .properties(height=260)
        )
        st.altair_chart(funnel_chart)
with right:
    with st.container(border=True, height="stretch"):
        st.subheader("今日行动")
        st.caption("只显示跟进日期已到、且尚未结束的岗位")
        if follow.empty:
            st.badge(
                "今天没有到期事项",
                icon=":material/check_circle:",
                color="green",
            )
            st.caption("保持当前节奏；新增跟进日期后，这里会自动提醒。")
        else:
            st.badge(
                f"{len(follow)} 个岗位待跟进",
                icon=":material/notifications_active:",
                color="orange",
            )
            st.dataframe(
                follow[["公司", "职位", "投递状态", "下次跟进"]],
                hide_index=True,
                height=235,
            )

section_header("02", "组合质量", "比较岗位来源结构与简历版本带来的真实结果。")
lower_left, lower_right = st.columns([1, 1.45])
with lower_left:
    with st.container(border=True, height="stretch"):
        st.subheader("公司类型")
        st.caption("当前求职组合的行业分布")
        company_types = (
            data["公司类型"].fillna("未填写").replace("", "未填写").value_counts()
            .rename_axis("公司类型").reset_index(name="岗位数")
        )
        company_chart = (
            alt.Chart(company_types)
            .mark_bar(size=18, cornerRadiusEnd=2, color="#1F4C43")
            .encode(
                x=alt.X("岗位数:Q", title=None, axis=alt.Axis(tickCount=4)),
                y=alt.Y("公司类型:N", title=None, sort="-x"),
                tooltip=["公司类型:N", "岗位数:Q"],
            )
            .properties(height=230)
        )
        st.altair_chart(company_chart)

with lower_right:
    with st.container(border=True, height="stretch"):
        st.subheader("简历版本效果")
        st.caption("比较不同简历版本带来的实际转化")
        summary = resume_summary(data)
        if not summary.empty:
            st.dataframe(
                summary,
                hide_index=True,
                column_config={
                    "面试转化率": st.column_config.NumberColumn(format="percent"),
                    "Offer转化率": st.column_config.NumberColumn(format="percent"),
                },
            )
