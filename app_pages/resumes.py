from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import streamlit as st

from candidate_profile import (
    AvailabilityPreset,
    AvailabilityWindow,
    CandidateFacts,
    CandidateProfileData,
    CommunicationPreferences,
    DefaultAvailability,
    JobPreferences,
    split_items,
)
from profile_store import load_default_profile, save_profile_version
from services.database import DB_PATH
from services.resume_service import (
    archive_resume,
    get_resume_version,
    list_resume_versions,
    save_resume_version,
    set_default_resume,
)
from ui_helpers import page_header


page_header(
    "简历库",
    "管理不可变的简历版本与统一求职档案，历史分析永远可以追溯。",
    "Library",
)
library_tab, profile_tab = st.tabs(["简历版本", "求职档案"])

with library_tab:
    with st.form("resume_upload_form"):
        st.subheader("创建简历或新版本")
        upload = st.file_uploader("选择 PDF、DOCX 或 TXT", type=["pdf", "docx", "txt"])
        name = st.text_input("简历名称", placeholder="产品实习版")
        notes = st.text_input("本版说明", placeholder="例如：强化数据分析项目")
        make_default = st.checkbox("设为默认简历")
        submitted = st.form_submit_button("保存不可变版本", type="primary")
    if submitted:
        try:
            if upload is None:
                raise ValueError("请选择简历文件。")
            version, created = save_resume_version(
                resume_name=name,
                filename=upload.name,
                mime_type=upload.type or "",
                raw=upload.getvalue(),
                notes=notes,
                set_default=make_default,
            )
            st.success(f"{version.label}{' 已创建' if created else ' 已存在，不重复保存'}。")
            st.rerun()
        except Exception as exc:
            st.error(f"保存失败：{exc}")

    versions = list_resume_versions()
    if versions.empty:
        st.info("还没有简历。第一次保存后，下次启动应用会自动找到默认简历。")
    else:
        st.subheader("全部版本")
        st.dataframe(
            versions,
            hide_index=True,
            column_config={"版本ID": None},
        )
        latest_by_name = versions.sort_values("版本", ascending=False).drop_duplicates("简历名称")
        choice_map = {
            row["完整名称"]: int(row["版本ID"]) for _, row in latest_by_name.iterrows()
        }
        selected = st.selectbox("管理某份简历", list(choice_map))
        selected_version = get_resume_version(choice_map[selected])
        with st.container(horizontal=True):
            if st.button("设为默认简历"):
                set_default_resume(selected_version.resume_id)
                st.success(f"{selected_version.resume_name} 已设为默认。")
                st.rerun()
            if st.button("归档这份简历"):
                archive_resume(selected_version.resume_id)
                st.success("已归档；历史分析、历史投递和原始文件不会删除。")
                st.rerun()

with profile_tab:
    profile_id, _, version_no, profile = load_default_profile(DB_PATH)
    st.caption(f"当前求职档案 v{version_no}。保存会创建新版本。")
    with st.form("candidate_profile_form"):
        c1, c2, c3 = st.columns(3)
        with c1:
            institution = st.text_input("学校", value=profile.facts.institution)
            graduation_date = st.date_input("预计毕业日期", value=profile.facts.graduation_date)
            student_status = st.text_input("学生状态", value=profile.facts.student_status)
        with c2:
            institution_tiers = st.multiselect(
                "学校层级（仅选择已确认的）",
                ["985", "211", "双一流", "QS前50", "QS前100", "海外院校"],
                default=profile.facts.institution_tiers,
            )
            education = st.text_input("最高学历", value=profile.facts.highest_education)
            major = st.text_input("专业", value=profile.facts.major)
        with c3:
            languages = st.text_input("语言", value="、".join(profile.facts.languages))
            work_authorization = st.text_input("工作许可", value=profile.facts.work_authorization)

        st.markdown("#### 常规实习承诺")
        st.caption(
            "这些信息用于评分硬性条件和生成邮件，不会被硬塞进简历正文。"
        )
        a1, a2, a3, a4, a5 = st.columns(5)
        with a1:
            default_start_within_days = st.number_input(
                "投递后几天内到岗",
                min_value=0,
                max_value=60,
                value=profile.default_availability.start_within_days,
                help="例如填 7：每个岗位会按本次计划/投递日期自动计算到岗日。",
            )
        with a2:
            default_start = st.date_input(
                "固定最早到岗（可空）",
                value=profile.default_availability.start_date,
            )
        with a3:
            default_duration = st.number_input(
                "可连续实习月数",
                min_value=1,
                max_value=24,
                value=profile.default_availability.duration_months,
            )
        with a4:
            default_days = st.number_input(
                "每周可实习天数",
                min_value=1,
                max_value=7,
                value=profile.default_availability.days_per_week,
            )
        with a5:
            onsite_available = st.checkbox(
                "可现场实习",
                value=bool(profile.default_availability.onsite_available),
            )
        relocate = st.checkbox(
            "可以异地实习/为岗位城市搬迁",
            value=bool(
                profile.preferences.accept_any_city
                or profile.preferences.willing_to_relocate
            ),
        )

        st.markdown("#### 岗位类型安排")
        st.caption(
            "系统按 JD 关键词自动选择预设；岗位分析页会展示最终采用值，并允许本次单独修改。"
        )
        preset_rows = pd.DataFrame(
            [
                {
                    "名称": preset.name,
                    "优先级": preset.priority,
                    "匹配关键词": "、".join(preset.match_keywords),
                    "投递后到岗天数": preset.start_within_days,
                    "实习月数": preset.duration_months,
                    "每周天数": preset.days_per_week,
                    "可现场": preset.onsite_available,
                    "城市": "、".join(preset.cities),
                    "备注": preset.notes,
                }
                for preset in profile.availability_presets
            ],
            columns=[
                "名称", "优先级", "匹配关键词", "投递后到岗天数", "实习月数",
                "每周天数", "可现场", "城市", "备注",
            ],
        )
        edited_presets = st.data_editor(
            preset_rows,
            num_rows="dynamic",
            hide_index=True,
            column_config={
                "名称": st.column_config.TextColumn("名称", required=True),
                "优先级": st.column_config.NumberColumn(
                    "优先级", min_value=0, max_value=100,
                    help="多个预设命中数相同时，数值更高的优先。",
                ),
                "投递后到岗天数": st.column_config.NumberColumn(
                    "投递后到岗天数", min_value=0, max_value=60
                ),
                "实习月数": st.column_config.NumberColumn(
                    "实习月数", min_value=1, max_value=24
                ),
                "每周天数": st.column_config.NumberColumn(
                    "每周天数", min_value=1, max_value=7
                ),
                "可现场": st.column_config.CheckboxColumn("可现场"),
            },
        )

        st.markdown("#### 可实习时间")
        st.caption("如有多段不同安排可在此补充；常规承诺已经足够时可以不填。")
        rows = pd.DataFrame(
            [
                {
                    "开始日期": window.start_date, "结束日期": window.end_date,
                    "每周天数": window.days_per_week,
                    "现场天数": window.onsite_days_per_week,
                    "工作方式": "、".join(window.work_modes),
                    "城市": "、".join(window.cities), "备注": window.notes,
                }
                for window in profile.availability_windows
            ],
            columns=["开始日期", "结束日期", "每周天数", "现场天数", "工作方式", "城市", "备注"],
        )
        edited = st.data_editor(
            rows,
            num_rows="dynamic",
            hide_index=True,
            column_config={
                "开始日期": st.column_config.DateColumn("开始日期", required=True),
                "结束日期": st.column_config.DateColumn("结束日期", required=True),
                "每周天数": st.column_config.NumberColumn("每周天数", min_value=1, max_value=7, required=True),
                "现场天数": st.column_config.NumberColumn("现场天数", min_value=0, max_value=7),
            },
        )
        target_roles = st.text_input("目标岗位", value="、".join(profile.preferences.target_roles))
        cities = st.text_input("偏好城市", value="、".join(profile.preferences.preferred_cities))
        company_filter_mode = st.selectbox(
            "公司筛选方式",
            ["不限", "仅目标公司"],
            index=0 if profile.preferences.company_filter_mode == "不限" else 1,
            help="选择“仅目标公司”后，名单外岗位仍可收录，但页面会提示偏好不符合。",
        )
        target_companies = st.text_input(
            "目标公司",
            value="、".join(profile.preferences.target_companies),
            placeholder="例如：示例科技、示例咨询",
        )
        excluded_companies = st.text_input(
            "排除公司",
            value="、".join(profile.preferences.excluded_companies),
            placeholder="例如：示例外包公司、示例中介公司",
        )
        profile_notes = st.text_input("版本说明")
        saved = st.form_submit_button("保存求职档案新版本", type="primary")

    if saved:
        try:
            presets = []
            for _, row in edited_presets.iterrows():
                name_value = str(row["名称"]).strip() if pd.notna(row["名称"]) else ""
                if not name_value:
                    continue
                presets.append(
                    AvailabilityPreset(
                        name=name_value,
                        priority=(
                            int(row["优先级"])
                            if pd.notna(row["优先级"])
                            else 0
                        ),
                        match_keywords=split_items(
                            str(row["匹配关键词"])
                            if pd.notna(row["匹配关键词"])
                            else ""
                        ),
                        start_within_days=(
                            int(row["投递后到岗天数"])
                            if pd.notna(row["投递后到岗天数"])
                            else None
                        ),
                        duration_months=(
                            int(row["实习月数"])
                            if pd.notna(row["实习月数"])
                            else None
                        ),
                        days_per_week=(
                            int(row["每周天数"])
                            if pd.notna(row["每周天数"])
                            else None
                        ),
                        onsite_available=(
                            bool(row["可现场"])
                            if pd.notna(row["可现场"])
                            else None
                        ),
                        cities=split_items(
                            str(row["城市"]) if pd.notna(row["城市"]) else ""
                        ),
                        notes=(
                            str(row["备注"]) if pd.notna(row["备注"]) else ""
                        ),
                    )
                )
            windows = []
            for _, row in edited.iterrows():
                if pd.isna(row["开始日期"]) or pd.isna(row["结束日期"]) or pd.isna(row["每周天数"]):
                    continue
                onsite = None if pd.isna(row["现场天数"]) else int(row["现场天数"])
                windows.append(
                    AvailabilityWindow(
                        start_date=pd.Timestamp(row["开始日期"]).date(),
                        end_date=pd.Timestamp(row["结束日期"]).date(),
                        days_per_week=int(row["每周天数"]),
                        onsite_days_per_week=onsite,
                        work_modes=split_items(str(row["工作方式"]) if pd.notna(row["工作方式"]) else ""),
                        cities=split_items(str(row["城市"]) if pd.notna(row["城市"]) else ""),
                        notes=str(row["备注"]) if pd.notna(row["备注"]) else "",
                    )
                )
            updated = CandidateProfileData(
                facts=CandidateFacts(
                    institution=institution.strip(),
                    institution_tiers=institution_tiers,
                    graduation_date=graduation_date,
                    student_status=student_status.strip(),
                    highest_education=education.strip(),
                    major=major.strip(),
                    languages=split_items(languages),
                    work_authorization=work_authorization.strip(),
                    requires_sponsorship=profile.facts.requires_sponsorship,
                ),
                default_availability=DefaultAvailability(
                    start_date=default_start,
                    start_within_days=(
                        int(default_start_within_days)
                        if default_start_within_days is not None
                        else None
                    ),
                    duration_months=(
                        int(default_duration) if default_duration is not None else None
                    ),
                    days_per_week=(
                        int(default_days) if default_days is not None else None
                    ),
                    onsite_available=onsite_available,
                ),
                availability_presets=presets,
                availability_windows=windows,
                preferences=JobPreferences(
                    target_roles=split_items(target_roles),
                    preferred_cities=split_items(cities),
                    target_companies=split_items(target_companies),
                    excluded_companies=split_items(excluded_companies),
                    company_filter_mode=company_filter_mode,
                    acceptable_work_modes=profile.preferences.acceptable_work_modes,
                    willing_to_relocate=relocate,
                    accept_any_city=relocate,
                    internship_types=profile.preferences.internship_types,
                ),
                communication=profile.communication,
                last_confirmed_at=datetime.now(),
            )
            _, new_version, created = save_profile_version(
                DB_PATH, profile_id, updated, profile_notes
            )
            st.success(f"求职档案 v{new_version}{' 已创建' if created else ' 内容未变化'}。")
            st.rerun()
        except Exception as exc:
            st.error(f"档案保存失败：{exc}")
