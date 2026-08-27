from __future__ import annotations

import hashlib
from pathlib import Path

import streamlit as st

from models.email import EmailConnectionConfig
from profile_store import load_default_profile
from services.credential_store import load_email_credentials
from services.database import APP_DIR, DB_PATH
from services.email_sync import (
    create_email_draft,
    load_account_settings,
)
from services.scoring import generate_materials, get_client
from ui_helpers import api_settings, empty_state, page_header, render_copyable


page_header(
    "求职材料",
    "根据最近一次岗位评分，生成可核验、可直接修改的定向材料。",
    "Create",
)

required = ["evaluation", "jd_info", "candidate_context", "analysis_resume_version"]
if not all(key in st.session_state for key in required):
    empty_state(
        "还差一次岗位评分",
        "选择简历并完成岗位分析后，这里会生成邮件、私信和简历改写建议。",
        icon=":material/edit_note:",
    )
    st.page_link(
        "app_pages/job_analysis.py",
        label="前往岗位分析",
        icon=":material/arrow_forward:",
        width="stretch",
    )
    st.stop()

_, current_profile_version_id, _, _ = load_default_profile(DB_PATH)
if (
    st.session_state.get("analysis_profile_version_id")
    != current_profile_version_id
):
    st.warning(
        "求职档案已经更新。请回到“单份 JD”重新生成一次四层判断，"
        "再生成材料，避免继续使用旧的空档案。"
    )
    st.stop()

evaluation = st.session_state.evaluation
jd = st.session_state.jd_info
resume = st.session_state.analysis_resume_version
api_key, model = api_settings()

with st.container(border=True):
    st.badge("当前生成上下文", icon=":material/link:", color="blue")
    st.markdown(f"**{jd.company} · {jd.role}**")
    st.caption(f"简历版本：{resume.label}")
if st.button(
    "生成定向求职材料",
    type="primary",
    width="stretch",
    icon=":material/auto_awesome:",
):
    try:
        with st.spinner("正在生成真实、可核验的材料……"):
            st.session_state.application_materials = generate_materials(
                get_client(api_key),
                model,
                resume.extracted_text,
                jd,
                evaluation,
                st.session_state.candidate_context,
            )
        st.success("材料已生成。")
    except Exception as exc:
        st.error(f"生成失败：{exc}")

if "application_materials" in st.session_state:
    materials = st.session_state.application_materials
    st.subheader("策略摘要")
    st.write(materials.strategy_summary)
    for item in materials.priority_actions:
        st.write(f"- {item}")

    st.subheader("简历改写建议")
    if not materials.resume_rewrites:
        st.info(
            "本次没有通过真实性与有效性检查的简历改写。"
            "这比展示只改标题或没有实际变化的建议更可靠。"
        )
    else:
        for item in materials.resume_rewrites:
            with st.expander(item.location or "简历改写"):
                st.markdown("**原文**")
                st.write(item.original)
                st.markdown("**建议版本**")
                st.write(item.suggested)
                st.caption(item.rationale)

    chinese, english, boss = st.tabs(["中文邮件", "英文邮件", "BOSS 私信"])
    with chinese:
        st.markdown(f"**主题：{materials.chinese_email_subject}**")
        render_copyable(materials.chinese_email_body)
    with english:
        st.markdown(f"**Subject: {materials.english_email_subject}**")
        render_copyable(materials.english_email_body)
    with boss:
        render_copyable(materials.boss_message)
    if materials.truthfulness_notes:
        st.warning("发送前请确认：" + "；".join(materials.truthfulness_notes))

    st.space("medium")
    st.subheader("写入 163 草稿箱")
    st.caption(
        "先在这里检查收件人、主题、正文和简历版本；点击后只创建草稿，"
        "不会自动发送。你仍需进入 163 邮箱做最终审核并手动发送。"
    )
    stored_credentials = load_email_credentials()
    email_address = (
        st.session_state.get("email_sync_address", "").strip()
        or (stored_credentials[0] if stored_credentials else "")
    )
    authorization_code = (
        st.session_state.get("email_sync_auth_code", "").strip()
        or (stored_credentials[1] if stored_credentials else "")
    )
    saved_email_settings = (
        load_account_settings(email_address) if email_address else None
    )
    if not email_address or not authorization_code:
        st.warning(
            "请先到“同步中心”填写固定 163 邮箱和客户端授权码，并勾选保存到 "
            "macOS 钥匙串。之后无需每次重复输入。"
        )
    else:
        language = st.segmented_control(
            "草稿语言",
            ["中文", "英文"],
            default="中文",
            key="draft_language",
        )
        default_subject = (
            materials.chinese_email_subject
            if language == "中文"
            else materials.english_email_subject
        )
        default_body = (
            materials.chinese_email_body
            if language == "中文"
            else materials.english_email_body
        )
        draft_nonce = hashlib.sha256(
            f"{language}|{default_subject}|{default_body}|{jd.application_email}".encode(
                "utf-8"
            )
        ).hexdigest()[:10]
        recipient = st.text_input(
            "收件人",
            value=jd.application_email,
            placeholder="JD 中的投递邮箱",
            key=f"draft_to_{draft_nonce}",
        ).strip()
        draft_subject = st.text_input(
            "主题",
            value=default_subject,
            key=f"draft_subject_{draft_nonce}",
        )
        draft_body = st.text_area(
            "正文",
            value=default_body,
            height=300,
            key=f"draft_body_{draft_nonce}",
        )

        resume_path = Path(resume.storage_path)
        if not resume_path.is_absolute():
            resume_path = APP_DIR / resume_path
        attach_resume = st.checkbox(
            f"附上本次评分使用的简历：{resume.original_filename}",
            value=resume_path.exists(),
            disabled=not resume_path.exists(),
            key=f"draft_attachment_{draft_nonce}",
        )
        if not resume_path.exists():
            st.warning("找不到该历史简历文件，本次只能创建无附件草稿。")

        config = EmailConnectionConfig(
            address=email_address,
            authorization_code=authorization_code,
            host=(saved_email_settings or {}).get("host", "imap.163.com"),
            port=int((saved_email_settings or {}).get("port", 993)),
            sent_folder=(saved_email_settings or {}).get(
                "sent_folder", "已发送"
            ),
            incoming_folder=(saved_email_settings or {}).get(
                "incoming_folder", "求职同步"
            ),
            draft_folder=(saved_email_settings or {}).get(
                "draft_folder", "草稿箱"
            ),
            max_messages_per_folder=int(
                (saved_email_settings or {}).get("max_messages", 100)
            ),
            auto_apply_high_confidence=bool(
                (saved_email_settings or {}).get("auto_apply", False)
            ),
        )
        if st.button(
            "写入 163 草稿箱（不会发送）",
            type="primary",
            width="stretch",
            disabled=not recipient or not draft_subject.strip() or not draft_body.strip(),
        ):
            try:
                with st.spinner("正在创建草稿……"):
                    created, message = create_email_draft(
                        config,
                        to_address=recipient,
                        subject=draft_subject,
                        body=draft_body,
                        attachment_bytes=(
                            resume_path.read_bytes()
                            if attach_resume and resume_path.exists()
                            else None
                        ),
                        attachment_filename=(
                            resume.original_filename if attach_resume else ""
                        ),
                        attachment_mime_type=(
                            resume.mime_type if attach_resume else ""
                        ),
                    )
                (st.success if created else st.warning)(message)
            except Exception as exc:
                st.error(f"写入草稿失败：{exc}")
