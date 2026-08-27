from __future__ import annotations

import hashlib
from datetime import date, timedelta

import streamlit as st

from candidate_profile import JobContextOverride, split_items
from constraint_engine import (
    build_candidate_context,
    evaluate_eligibility,
    select_availability_preset,
)
from models.application import APPLICATION_STATUSES
from models.evaluation import Evaluation
from profile_store import load_default_profile, save_evaluation_run
from scoring_policy import calibrate_decision
from services.database import DB_PATH, save_application
from services.decision_support import build_decision_summary
from services.discovery_quality import REQUIREMENT_MARKERS, RESPONSIBILITY_MARKERS, SUMMARY_MARKERS
from services.professional_gate import augment_professional_eligibility
from services.resume_service import (
    get_resume_version,
    list_resume_versions,
    save_resume_version,
)
from services.scoring import (
    RUBRIC_VERSION,
    evaluate_match,
    extract_jd,
    get_client,
    jd_hash,
    total_score,
)
from ui_helpers import (
    CONSTRAINT_TYPES,
    api_settings,
    constraints_dataframe,
    dataframe_to_constraints,
    page_header,
    render_eligibility,
    render_evaluation,
    section_header,
)


page_header(
    "单份 JD 决策",
    "先确认信息完整，再分别核对门槛、能力、个人偏好与结论可信度。",
    "Analyze",
)
api_key, model = api_settings()
profile_id, profile_version_id, profile_version_no, candidate_profile = load_default_profile(DB_PATH)
profile_summary = [
    candidate_profile.facts.institution,
    "、".join(candidate_profile.facts.institution_tiers),
    (
        f"可连续实习{candidate_profile.default_availability.duration_months}个月"
        if candidate_profile.default_availability.duration_months
        else ""
    ),
    (
        f"通常投递后{candidate_profile.default_availability.start_within_days}天内到岗"
        if candidate_profile.default_availability.start_within_days is not None
        else ""
    ),
    (
        f"每周{candidate_profile.default_availability.days_per_week}天"
        if candidate_profile.default_availability.days_per_week
        else ""
    ),
    (
        "可异地实习"
        if candidate_profile.preferences.accept_any_city
        or candidate_profile.preferences.willing_to_relocate
        else ""
    ),
]
with st.container(border=True):
    with st.container(horizontal=True, vertical_alignment="center"):
        st.badge(
            f"求职档案 v{profile_version_no}",
            icon=":material/account_circle:",
            color="blue",
        )
        st.caption("用于资格判断和邮件，不会写进简历正文。")
    st.write(" · ".join(item for item in profile_summary if item))
missing_arrangements = []
if (
    not candidate_profile.default_availability.start_date
    and candidate_profile.default_availability.start_within_days is None
):
    missing_arrangements.append("最早到岗日")
if not candidate_profile.default_availability.days_per_week:
    missing_arrangements.append("每周可实习天数")
if missing_arrangements:
    st.warning(
        "还未固定：" + "、".join(missing_arrangements)
        + "。需要时可使用下方“本岗位最终安排”，或到“简历库 → 求职档案”填写一次。"
    )

section_header("01", "选择简历版本", "评分会固定关联本次选中的历史版本。")
resume_rows = list_resume_versions()
resume_version = None
if not resume_rows.empty:
    options = {
        f"{row['完整名称']} · {row['创建时间']}": int(row["版本ID"])
        for _, row in resume_rows.iterrows()
    }
    default_index = next(
        (index for index, (_, version_id) in enumerate(options.items())
         if get_resume_version(version_id).is_default),
        0,
    )
    selected_label = st.selectbox("本次分析使用", list(options), index=default_index)
    resume_version = get_resume_version(options[selected_label])
    st.badge(
        f"已选择 · {resume_version.label}",
        icon=":material/check_circle:",
        color="green",
    )
else:
    st.info("简历库还是空的。你可以在这里完成第一次上传。")

with st.expander(
    "上传新简历或创建新版本",
    expanded=resume_version is None,
    icon=":material/upload_file:",
):
    upload = st.file_uploader("简历文件", type=["pdf", "docx", "txt"], key="analysis_resume_upload")
    upload_name = st.text_input("简历名称", placeholder="例如：产品实习版")
    upload_notes = st.text_input("版本说明", placeholder="例如：强化用户研究项目")
    upload_default = st.checkbox("设为默认简历", value=resume_version is None)
    if st.button("保存到简历库", type="primary", disabled=upload is None):
        try:
            saved, created = save_resume_version(
                resume_name=upload_name,
                filename=upload.name,
                mime_type=upload.type or "",
                raw=upload.getvalue(),
                notes=upload_notes,
                set_default=upload_default,
            )
            st.success(f"{saved.label}{' 已创建' if created else ' 已存在'}。")
            st.rerun()
        except Exception as exc:
            st.error(f"保存失败：{exc}")

section_header("02", "输入岗位 JD", "粘贴岗位职责、任职要求和到岗条件。")
jd_text_input = st.text_area(
    "粘贴完整 JD",
    height=240,
    placeholder="粘贴岗位职责、任职要求、到岗要求等完整文字……",
)
if st.button(
    "提取并整理 JD",
    type="primary",
    width="stretch",
    icon=":material/auto_awesome:",
):
    try:
        if not jd_text_input.strip():
            raise ValueError("请先粘贴 JD。")
        if any(marker in jd_text_input for marker in SUMMARY_MARKERS):
            raise ValueError("这是一段平台列表摘要，不能正式评分。请打开岗位详情并粘贴完整 JD。")
        if len(jd_text_input.strip()) < 160 or not any(
            marker in jd_text_input for marker in RESPONSIBILITY_MARKERS
        ) or not any(marker in jd_text_input for marker in REQUIREMENT_MARKERS):
            raise ValueError("JD 信息不完整：至少需要岗位职责、任职要求和足够的正文内容。")
        with st.spinner("正在读取 JD……"):
            st.session_state.jd_info = extract_jd(get_client(api_key), model, jd_text_input)
            st.session_state.jd_nonce = st.session_state.get("jd_nonce", 0) + 1
            for key in ["evaluation", "eligibility", "decision", "application_materials"]:
                st.session_state.pop(key, None)
        st.success("JD 已提取，请检查下面的信息。")
    except Exception as exc:
        st.error(f"JD 提取失败：{exc}")

if "jd_info" in st.session_state:
    jd = st.session_state.jd_info
    nonce = st.session_state.get("jd_nonce", 0)
    section_header(
        "03",
        "确认岗位与资格条件",
        "校对结构化信息后，再开始正式评分。",
    )
    col1, col2, col3 = st.columns(3)
    with col1:
        company = st.text_input("公司", value=jd.company, key=f"company_{nonce}")
        company_type = st.text_input("公司类型", value=jd.company_type, key=f"company_type_{nonce}")
        source = st.text_input("信息来源", value=jd.source, key=f"source_{nonce}")
    with col2:
        role = st.text_input("职位", value=jd.role, key=f"role_{nonce}")
        location = st.text_input("Base 地", value=jd.location, key=f"location_{nonce}")
        salary = st.text_input("薪资", value=jd.salary, key=f"salary_{nonce}")
    with col3:
        applied_date = st.date_input("计划/投递日期", value=date.today(), key=f"date_{nonce}")
        status = st.selectbox("初始状态", APPLICATION_STATUSES, key=f"status_{nonce}")
        next_follow_up = st.date_input("下次跟进", value=None, key=f"follow_{nonce}")
        notes = st.text_input("台账备注", key=f"notes_{nonce}")
    contact1, contact2 = st.columns(2)
    with contact1:
        application_email = st.text_input(
            "投递邮箱（用于匹配已发送邮件）",
            value=jd.application_email,
            placeholder="例如：jobs@example.com",
            key=f"application_email_{nonce}",
        )
    with contact2:
        application_reference = st.text_input(
            "招聘编号（可选）",
            value=jd.application_reference,
            placeholder="例如：REQ-2026-001",
            key=f"application_reference_{nonce}",
        )

    jd.company, jd.company_type, jd.source = company.strip(), company_type.strip(), source.strip()
    jd.role, jd.location, jd.salary = role.strip(), location.strip(), salary.strip()
    jd.application_email = application_email.strip().casefold()
    jd.application_reference = application_reference.strip()
    jd.full_text = st.text_area(
        "整理后的完整 JD", value=jd.full_text, height=220, key=f"jd_full_{nonce}"
    ).strip()

    with st.expander("确认资格条件", expanded=bool(jd.constraints)):
        st.caption("这些条件会直接影响最终建议。日期用 YYYY-MM-DD，天数/月数只填数字。")
        edited = st.data_editor(
            constraints_dataframe(jd.constraints),
            key=f"constraints_{nonce}",
            num_rows="dynamic",
            hide_index=True,
            column_config={
                "类型": st.column_config.SelectboxColumn("类型", options=CONSTRAINT_TYPES, required=True),
                "重要性": st.column_config.SelectboxColumn(
                    "重要性", options=["必须", "优先", "加分"], required=True
                ),
            },
        )
        jd.constraints = dataframe_to_constraints(edited)

    auto_preset = select_availability_preset(
        candidate_profile,
        "\n".join([jd.company, jd.company_type, jd.role, jd.full_text]),
    )
    arrangement_options = ["常规默认"] + [
        preset.name for preset in candidate_profile.availability_presets
    ]
    auto_label = auto_preset.name if auto_preset else "常规默认"
    with st.expander("本岗位最终安排（评分与邮件会使用）", expanded=True):
        st.caption(
            f"系统根据 JD 推荐“{auto_label}”。下方是本岗位最终值，可以随时修改；"
            "不会改动简历库中的默认配置。"
        )
        selected_arrangement = st.selectbox(
            "采用哪套安排",
            arrangement_options,
            index=arrangement_options.index(auto_label),
            key=f"availability_preset_{nonce}",
        )
        selected_preset = next(
            (
                preset
                for preset in candidate_profile.availability_presets
                if preset.name == selected_arrangement
            ),
            None,
        )
        availability = (
            selected_preset
            if selected_preset is not None
            else candidate_profile.default_availability
        )
        start_within_days = availability.start_within_days
        calculated_start = (
            applied_date + timedelta(days=int(start_within_days))
            if start_within_days is not None
            else (
                candidate_profile.default_availability.start_date
                if selected_preset is None
                else None
            )
        )
        preset_key = hashlib.sha256(
            selected_arrangement.encode("utf-8")
        ).hexdigest()[:8]
        applied_date_key = applied_date.isoformat().replace("-", "")
        location_key = hashlib.sha256(
            jd.location.encode("utf-8")
        ).hexdigest()[:8]
        o1, o2, o3 = st.columns(3)
        with o1:
            override_start = st.date_input(
                "预计最晚到岗日",
                value=calculated_start,
                key=f"os_{nonce}_{preset_key}_{applied_date_key}",
                help="按计划/投递日期和预设天数自动计算，可为本岗位单独修改。",
            )
            override_end = st.date_input(
                "明确可实习至（可空）",
                value=None,
                key=f"oe_{nonce}_{preset_key}",
            )
        with o2:
            override_duration = st.number_input(
                "可连续实习月数",
                min_value=1,
                max_value=24,
                value=availability.duration_months,
                key=f"duration_{nonce}_{preset_key}",
            )
            override_days = st.number_input(
                "每周可实习天数（可空）",
                min_value=1,
                max_value=7,
                value=availability.days_per_week,
                key=f"od_{nonce}_{preset_key}",
            )
        with o3:
            override_onsite_available = st.checkbox(
                "可现场实习",
                value=bool(availability.onsite_available),
                key=f"onsite_{nonce}_{preset_key}",
            )
            default_cities = getattr(availability, "cities", []) or (
                [jd.location] if jd.location else []
            )
            override_cities = st.text_input(
                "本岗位可接受城市",
                value="、".join(default_cities),
                key=f"oc_{nonce}_{preset_key}_{location_key}",
            )

    job_override = JobContextOverride(
        enabled=True,
        preset_name=selected_arrangement,
        start_date=override_start,
        end_date=override_end,
        duration_months=(
            int(override_duration) if override_duration is not None else None
        ),
        days_per_week=int(override_days) if override_days else None,
        onsite_days_per_week=None,
        onsite_available=override_onsite_available,
        work_modes=["现场"] if override_onsite_available else [],
        cities=split_items(override_cities),
    )

    if st.button(
        "生成四层判断",
        type="primary",
        width="stretch",
        disabled=resume_version is None,
        icon=":material/query_stats:",
    ):
        try:
            if resume_version is None:
                raise ValueError("请先保存并选择一个简历版本。")
            if not jd.full_text:
                raise ValueError("JD 不能为空。")
            context = build_candidate_context(candidate_profile, job_override)
            eligibility = evaluate_eligibility(jd.constraints, candidate_profile, job_override)
            eligibility = augment_professional_eligibility(
                eligibility,
                jd_text=jd.full_text,
                resume_text=resume_version.extracted_text,
            )
            with st.spinner("正在逐项寻找简历证据……"):
                evaluation = evaluate_match(
                    get_client(api_key),
                    model,
                    resume_version.extracted_text,
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
                    resume_hash=resume_version.sha256,
                    profile_version_id=profile_version_id,
                    job_override=job_override.model_dump(mode="json"),
                    input_snapshot={"jd": jd.model_dump(mode="json"), "candidate_context": context},
                    rubric_version=RUBRIC_VERSION,
                    model=model,
                    output={
                        "evaluation": evaluation.model_dump(mode="json"),
                        "eligibility": eligibility.model_dump(mode="json"),
                        "decision": decision.model_dump(mode="json"),
                    },
                    resume_version_id=resume_version.id,
                    company=jd.company,
                    role=jd.role,
                    jd_text=jd.full_text,
                    fit_score=total_score(evaluation),
                    gate_result=decision.gate_result,
                    recommendation=decision.final_recommendation,
                )
            st.session_state.evaluation = evaluation
            st.session_state.eligibility = eligibility
            st.session_state.decision = decision
            st.session_state.candidate_context = context
            st.session_state.analysis_resume_version = resume_version
            st.session_state.analysis_profile_version_id = profile_version_id
            st.session_state.evaluation_run_id = run_id
            st.session_state.pop("application_materials", None)
            st.success(f"评分完成，已写入分析历史 #{run_id}。")
        except Exception as exc:
            st.error(f"评分失败：{exc}")

    if "evaluation" in st.session_state:
        result: Evaluation = st.session_state.evaluation
        st.space("medium")
        section_header("04", "四层判断", "参考分不是录取概率；最终决定仍由你确认。")
        summary = build_decision_summary(
            evaluation=result,
            eligibility=st.session_state.eligibility,
            profile=candidate_profile,
            score=total_score(result),
            company=jd.company,
            role=jd.role,
            location=jd.location,
            content_level="full",
            completeness_score=100,
            capture_kind="text",
        )
        layers = [summary.eligibility, summary.capability, summary.preference, summary.information]
        for column, layer in zip(st.columns(4), layers, strict=True):
            with column:
                st.metric(layer.label, layer.result, border=True)
                st.caption(layer.reason)
        st.info(
            f"系统建议动作：{summary.suggested_action}｜结论可信度：{summary.confidence}｜"
            f"个人参考分：{summary.reference_score}/100。"
        )
        for caveat in summary.caveats:
            st.caption(f"• {caveat}")
        render_eligibility(
            st.session_state.eligibility, st.session_state.get("decision")
        )
        render_evaluation(result)

        if st.button(
            "保存到投递计划",
            type="primary",
            width="stretch",
            icon=":material/playlist_add:",
        ):
            selected_resume = st.session_state.analysis_resume_version
            fields = {
                "applied_date": applied_date.isoformat(),
                "company": company.strip(), "company_type": company_type.strip(),
                "role": role.strip(), "location": location.strip(),
                "salary": salary.strip(), "source": source.strip(), "status": status,
                "resume_version": selected_resume.label,
                "next_follow_up_date": next_follow_up.isoformat() if next_follow_up else "",
                "notes": notes.strip(),
                "application_email": application_email.strip().casefold(),
                "application_reference": application_reference.strip(),
            }
            created, application_id = save_application(
                fields=fields,
                jd_hash=jd_hash(jd),
                jd_text=jd.full_text,
                evaluation_json=result.model_dump_json(),
                score=total_score(result),
                recommendation=result.recommendation,
                resume_version_id=selected_resume.id,
                analysis_run_id=st.session_state.get("evaluation_run_id"),
            )
            if created:
                st.success(f"已进入投递计划，岗位 ID {application_id}。")
            else:
                st.warning("该岗位已经存在，没有重复保存。")
