from __future__ import annotations

import io

import pandas as pd
import streamlit as st

from models.application import APPLICATION_STATUSES
from services.database import (
    append_application_event,
    load_applications,
    load_events,
    undo_event,
    update_application_fields,
)
from ui_helpers import clean_cell, date_to_iso, empty_state, page_header, section_header


page_header(
    "投递台账",
    "集中维护岗位状态、跟进日期与关键备注，每一次状态变化都会保留历史。",
    "Pipeline",
)
applications = load_applications()
if applications.empty:
    empty_state(
        "你的投递计划还是空的",
        "完成一次岗位评分并确认加入投递计划后，岗位会出现在这里。",
        icon=":material/table_view:",
    )
    st.page_link(
        "app_pages/job_analysis.py",
        label="分析第一个岗位",
        icon=":material/arrow_forward:",
        width="stretch",
    )
    st.stop()

with st.container(horizontal=True):
    st.metric("岗位总数", len(applications), border=True)
    st.metric(
        "待投递",
        int((applications["投递状态"] == "待投递").sum()),
        border=True,
    )
    st.metric(
        "进行中",
        int(applications["投递状态"].isin(["已投递", "笔试", "一面", "二面", "终面"]).sum()),
        border=True,
    )

section_header("01", "整理当前管线", "搜索岗位，或按行动阶段聚焦本次要处理的记录。")
filter_left, filter_right = st.columns([1.4, 1])
with filter_left:
    pipeline_view = st.segmented_control(
        "管线视图",
        ["全部", "待行动", "进行中", "已结束"],
        default="全部",
    )
with filter_right:
    ledger_query = st.text_input(
        "搜索公司或职位",
        placeholder="输入公司、职位或城市",
        icon=":material/search:",
    ).strip()

visible_applications = applications.copy()
if pipeline_view == "待行动":
    visible_applications = visible_applications[
        visible_applications["投递状态"].isin(["待投递", "已投递"])
    ]
elif pipeline_view == "进行中":
    visible_applications = visible_applications[
        visible_applications["投递状态"].isin(["笔试", "一面", "二面", "终面"])
    ]
elif pipeline_view == "已结束":
    visible_applications = visible_applications[
        visible_applications["投递状态"].isin(["Offer", "拒绝", "放弃"])
    ]
if ledger_query:
    query_mask = (
        visible_applications[["公司", "职位", "Base地"]]
        .fillna("")
        .astype(str)
        .agg(" ".join, axis=1)
        .str.contains(ledger_query, case=False, regex=False)
    )
    visible_applications = visible_applications[query_mask]

output = io.BytesIO()
with pd.ExcelWriter(output, engine="openpyxl") as writer:
    applications.drop(columns=["简历版本ID"]).to_excel(
        writer, index=False, sheet_name="投递台账"
    )

editable_columns = [
    "ID", "投递日期", "公司", "公司类型", "职位", "Base地", "薪资", "信息来源",
    "投递邮箱", "招聘编号", "投递状态", "最高进展", "简历版本", "下次跟进",
    "匹配分", "投递建议", "备注",
]
editor_input = visible_applications[editable_columns].copy()
st.caption(f"当前显示 {len(editor_input)} / {len(applications)} 个岗位 · 固定公司与职位列，横向滚动查看更多字段。")
edited = st.data_editor(
    editor_input,
    key="ledger_editor_v2",
    hide_index=True,
    disabled=["ID", "最高进展", "简历版本", "匹配分", "投递建议"],
    column_config={
        "ID": None,
        "公司": st.column_config.TextColumn("公司", pinned=True, width="medium"),
        "职位": st.column_config.TextColumn("职位", pinned=True, width="large"),
        "投递状态": st.column_config.SelectboxColumn(
            "投递状态", options=APPLICATION_STATUSES, required=True
        ),
    },
)

with st.container(horizontal=True):
    save_changes = st.button(
        "保存台账修改",
        type="primary",
        icon=":material/save:",
        disabled=editor_input.empty,
    )
    st.download_button(
        "下载 Excel",
        output.getvalue(),
        file_name="投递台账.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        icon=":material/download:",
    )

if save_changes:
    changed_fields = 0
    event_count = 0
    messages = []
    original_by_id = applications.set_index("ID")
    for _, row in edited.iterrows():
        application_id = int(row["ID"])
        original = original_by_id.loc[application_id]
        values = {
            "applied_date": date_to_iso(row["投递日期"]),
            "company": clean_cell(row["公司"]),
            "company_type": clean_cell(row["公司类型"]),
            "role": clean_cell(row["职位"]),
            "location": clean_cell(row["Base地"]),
            "salary": clean_cell(row["薪资"]),
            "source": clean_cell(row["信息来源"]),
            "application_email": clean_cell(row["投递邮箱"]),
            "application_reference": clean_cell(row["招聘编号"]),
            "next_follow_up_date": date_to_iso(row["下次跟进"]),
            "notes": clean_cell(row["备注"]),
        }
        original_values = {
            "applied_date": date_to_iso(original["投递日期"]),
            "company": clean_cell(original["公司"]),
            "company_type": clean_cell(original["公司类型"]),
            "role": clean_cell(original["职位"]),
            "location": clean_cell(original["Base地"]),
            "salary": clean_cell(original["薪资"]),
            "source": clean_cell(original["信息来源"]),
            "application_email": clean_cell(original["投递邮箱"]),
            "application_reference": clean_cell(original["招聘编号"]),
            "next_follow_up_date": date_to_iso(original["下次跟进"]),
            "notes": clean_cell(original["备注"]),
        }
        changes = {key: value for key, value in values.items() if value != original_values[key]}
        if changes:
            update_application_fields(application_id, changes)
            changed_fields += len(changes)
        new_status = clean_cell(row["投递状态"])
        if new_status != clean_cell(original["投递状态"]):
            resume_version_id = original.get("简历版本ID")
            resume_id = (
                int(resume_version_id)
                if pd.notna(resume_version_id) and str(resume_version_id).strip()
                else None
            )
            created, message = append_application_event(
                application_id,
                new_status,
                source="手动确认",
                resume_version_id=resume_id,
            )
            if created:
                event_count += 1
            else:
                messages.append(f"岗位 {application_id}：{message}")
    st.toast(
        f"已更新 {changed_fields} 个字段，追加 {event_count} 条状态事件。",
        icon=":material/check_circle:",
    )
    for message in messages:
        st.caption(message)
    st.rerun()

section_header("02", "投递事件历史", "选择岗位查看完整状态轨迹；只有错误事件才需要撤销。")
selected_application = st.selectbox(
    "查看岗位",
    applications["ID"].tolist(),
    format_func=lambda value: (
        f"#{value} {applications.loc[applications['ID'] == value, '公司'].iloc[0]} · "
        f"{applications.loc[applications['ID'] == value, '职位'].iloc[0]}"
    ),
)
events = load_events(int(selected_application))
st.dataframe(events, hide_index=True, column_config={"事件ID": None, "岗位ID": None})
effective = events[
    (events["有效性"] == "有效") & ~events["事件"].astype(str).str.startswith("迁移")
]
if not effective.empty:
    event_map = {
        f"#{int(row['事件ID'])} {row['时间']}：{row['原状态']} → {row['新状态']}（{row['来源']}）":
        int(row["事件ID"])
        for _, row in effective.iterrows()
    }
    with st.expander("撤销错误事件", icon=":material/undo:"):
        st.caption("撤销会保留原始记录，并将其标记为无效。")
        selected_event = st.selectbox("选择事件", list(event_map))
        reason = st.text_input("撤销原因", value="人工撤销错误同步")
        if st.button("确认撤销", type="secondary", icon=":material/undo:"):
            success, message = undo_event(event_map[selected_event], reason)
            (st.success if success else st.warning)(message)
            if success:
                st.rerun()
