import json

import streamlit as st

from services.history import get_analysis_detail, list_analysis_history, set_discovery_flag
from ui_helpers import empty_state, page_header


page_header(
    "分析历史",
    "回看每次评分使用的简历、原始 JD、规则版本与完整结果。",
    "Archive",
)
history = list_analysis_history()
if history.empty:
    empty_state(
        "还没有分析记录",
        "完成第一次岗位评分后，所有输入与结果都会固定保存在这里。",
        icon=":material/history:",
    )
    st.page_link(
        "app_pages/job_analysis.py",
        label="开始岗位分析",
        icon=":material/arrow_forward:",
        width="stretch",
    )
    st.stop()

st.dataframe(history, hide_index=True, column_config={"分析ID": None})
options = {
    f"#{int(row['分析ID'])} {row['分析时间']} · {row['公司']} {row['职位']}":
    int(row["分析ID"])
    for _, row in history.iterrows()
}
selected = st.selectbox("查看历史详情", list(options))
run_id = options[selected]
detail = get_analysis_detail(run_id)

with st.container(horizontal=True):
    st.metric(
        "专业匹配分",
        detail["fit_score"] if detail["fit_score"] is not None else "历史数据",
        border=True,
    )
    st.metric("资格门槛", detail["gate_result"] or "历史数据", border=True)
    st.metric("最终建议", detail["recommendation"] or "历史数据", border=True)
st.caption(
    f"使用 {detail['resume_label']} · 评分 {detail['rubric_version']} · 模型 {detail['model']}"
)

resume_tab, jd_tab, result_tab = st.tabs(["原始简历", "原始 JD", "评分快照"])
with resume_tab:
    if detail["resume_text"]:
        st.text_area("只读简历文本", detail["resume_text"], height=420, disabled=True)
    else:
        st.warning("这条历史来自简历库上线前，当时没有持久化原始简历。")
with jd_tab:
    st.text_area("只读 JD", detail["jd_text"], height=420, disabled=True)
with result_tab:
    st.json(detail["output"])

in_discovery = (
    history.loc[history["分析ID"] == run_id, "岗位发现箱"].iloc[0] == "是"
)
if st.checkbox("保存到岗位发现箱", value=in_discovery, key=f"discovery_{run_id}") != in_discovery:
    set_discovery_flag(run_id, not in_discovery)
    st.rerun()
