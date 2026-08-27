from __future__ import annotations

import os

import pandas as pd
import streamlit as st

from models.application import APPLICATION_STATUSES
from models.email import EmailConnectionConfig
from services.credential_store import (
    delete_email_credentials,
    keychain_available,
    load_email_credentials,
    save_email_credentials,
)
from services.database import load_applications, load_events
from services.email_sync import (
    confirm_email_message,
    ignore_email_message,
    load_account_settings,
    load_email_messages,
    load_sync_runs,
    save_account_settings,
    scan_mailbox,
    test_connection,
    undo_email_message,
)
from ui_helpers import page_header


def _application_options(applications: pd.DataFrame) -> dict[str, int]:
    return {
        f"#{int(row['ID'])} {row['公司'] or '未命名公司'} · {row['职位'] or '未命名职位'}":
        int(row["ID"])
        for _, row in applications.iterrows()
    }


def _render_review(
    *,
    states: list[str],
    key_prefix: str,
    empty_message: str,
) -> None:
    messages = load_email_messages(states)
    if messages.empty:
        st.info(empty_message)
        return

    display_columns = [
        "邮件ID", "时间", "方向", "主题", "识别类型", "建议状态",
        "分类置信度", "匹配公司", "匹配职位", "匹配置信度",
    ]
    display = messages[display_columns].copy()
    display["分类置信度"] = display["分类置信度"] * 100
    display["匹配置信度"] = display["匹配置信度"] * 100
    st.dataframe(
        display,
        hide_index=True,
        width="stretch",
        column_config={
            "邮件ID": None,
            "分类置信度": st.column_config.ProgressColumn(
                "分类置信度", min_value=0.0, max_value=100.0, format="%.0f%%"
            ),
            "匹配置信度": st.column_config.ProgressColumn(
                "岗位匹配度", min_value=0.0, max_value=100.0, format="%.0f%%"
            ),
        },
    )
    message_options = {
        (
            f"#{int(row['邮件ID'])} {row['方向']}｜{row['主题'] or '（无主题）'}"
            f"｜{row['识别类型']}"
        ): int(row["邮件ID"])
        for _, row in messages.iterrows()
    }
    selected_label = st.selectbox(
        "选择一封邮件处理",
        list(message_options),
        key=f"{key_prefix}_message",
    )
    email_id = message_options[selected_label]
    selected = messages.loc[messages["邮件ID"] == email_id].iloc[0]
    with st.expander("查看邮件摘要与识别结果", expanded=True):
        st.write(selected["正文摘要"] or "（没有可显示的正文）")
        st.caption(
            f"文件夹：{selected['文件夹']}　发件人：{selected['发件人'] or '未知'}　"
            f"分类：{selected['识别类型']}"
        )

    applications = load_applications()
    if applications.empty:
        st.warning("台账里还没有岗位，暂时不能把邮件关联到投递事件。")
        if st.button("忽略这封邮件", key=f"{key_prefix}_ignore_empty"):
            ignore_email_message(email_id)
            st.rerun()
        return

    app_options = _application_options(applications)
    app_ids = list(app_options.values())
    matched_id = selected["匹配岗位ID"]
    default_app_index = (
        app_ids.index(int(matched_id))
        if pd.notna(matched_id) and int(matched_id) in app_ids
        else 0
    )
    c1, c2 = st.columns(2)
    with c1:
        application_label = st.selectbox(
            "关联到哪个岗位",
            list(app_options),
            index=default_app_index,
            key=f"{key_prefix}_application_{email_id}",
        )
    with c2:
        suggested_status = str(selected["建议状态"] or "")
        default_status_index = (
            APPLICATION_STATUSES.index(suggested_status)
            if suggested_status in APPLICATION_STATUSES
            else 0
        )
        new_status = st.selectbox(
            "确认后的状态",
            APPLICATION_STATUSES,
            index=default_status_index,
            key=f"{key_prefix}_status_{email_id}",
        )
    b1, b2 = st.columns(2)
    if b1.button(
        "确认并追加投递事件",
        type="primary",
        width="stretch",
        key=f"{key_prefix}_confirm_{email_id}",
    ):
        created, message = confirm_email_message(
            email_id,
            app_options[application_label],
            new_status,
        )
        (st.success if created else st.warning)(message)
        if created:
            st.rerun()
    if b2.button(
        "忽略，不修改台账",
        width="stretch",
        key=f"{key_prefix}_ignore_{email_id}",
    ):
        ignore_email_message(email_id)
        st.rerun()


page_header(
    "同步中心",
    "扫描始终保持只读；只有你主动确认时才写入草稿或更新台账。",
    "Sync",
)

if "email_keychain_loaded" not in st.session_state:
    keychain_credentials = load_email_credentials()
    st.session_state.email_keychain_loaded = True
    st.session_state.email_keychain_has_credentials = bool(keychain_credentials)
    st.session_state.setdefault(
        "email_sync_address",
        os.getenv("EMAIL_SYNC_ADDRESS", "")
        or (keychain_credentials[0] if keychain_credentials else ""),
    )
    st.session_state.setdefault(
        "email_sync_auth_code",
        os.getenv("EMAIL_SYNC_AUTH_CODE", "")
        or (keychain_credentials[1] if keychain_credentials else ""),
    )

with st.expander(
    "固定 163 邮箱连接",
    expanded=not st.session_state.get("email_keychain_has_credentials", False),
):
    st.info(
        "请先在 163 网页邮箱启用 IMAP/SMTP，并生成“客户端授权码”；"
        "授权码不是邮箱登录密码。建议创建文件夹“求职同步”，只把招聘回信移入该文件夹。",
        icon=":material/lock:",
    )
    e1, e2 = st.columns(2)
    with e1:
        address = st.text_input(
            "163 邮箱地址",
            placeholder="your_name@163.com",
            key="email_sync_address",
            persist_state="session",
        ).strip()
    with e2:
        authorization_code = st.text_input(
            "客户端授权码",
            type="password",
            help="可加密保存到 macOS 钥匙串；不会写入项目数据库或代码仓库。",
            key="email_sync_auth_code",
            persist_state="session",
        ).strip()
    if st.session_state.get("email_keychain_has_credentials"):
        st.success("固定邮箱和授权码已从 macOS 钥匙串读取，无需重复输入。")

    saved = load_account_settings(address) if address else None
    with st.expander("高级连接设置"):
        a1, a2, a3 = st.columns(3)
        with a1:
            host = st.text_input(
                "IMAP 地址",
                value=(saved or {}).get(
                    "host", os.getenv("EMAIL_SYNC_IMAP_HOST", "imap.163.com")
                ),
            ).strip()
        with a2:
            port = st.number_input(
                "IMAP 端口",
                min_value=1,
                max_value=65535,
                value=int(
                    (saved or {}).get(
                        "port", os.getenv("EMAIL_SYNC_IMAP_PORT", "993")
                    )
                ),
            )
        with a3:
            max_messages = st.number_input(
                "每个文件夹最多扫描",
                min_value=1,
                max_value=500,
                value=int(
                    (saved or {}).get(
                        "max_messages", os.getenv("EMAIL_SYNC_MAX_MESSAGES", "100")
                    )
                ),
            )

    base_config = EmailConnectionConfig(
        address=address,
        authorization_code=authorization_code,
        host=host,
        port=int(port),
        sent_folder=(saved or {}).get(
            "sent_folder", os.getenv("EMAIL_SYNC_SENT_FOLDER", "已发送")
        ),
        incoming_folder=(saved or {}).get(
            "incoming_folder", os.getenv("EMAIL_SYNC_INCOMING_FOLDER", "求职同步")
        ),
        draft_folder=(saved or {}).get(
            "draft_folder", os.getenv("EMAIL_SYNC_DRAFT_FOLDER", "草稿箱")
        ),
        max_messages_per_folder=int(max_messages),
        auto_apply_high_confidence=False,
    )
    if st.button(
        "测试连接并读取文件夹",
        disabled=not address or not authorization_code,
    ):
        try:
            with st.spinner("正在连接 163 邮箱……"):
                st.session_state.email_mailboxes = [
                    item.model_dump() for item in test_connection(base_config)
                ]
            st.success("连接成功。下面请选择真实的已发送和招聘回信文件夹。")
        except Exception as exc:
            st.error(
                f"连接失败：{exc}。请检查是否已开启 IMAP、是否使用客户端授权码，"
                "以及 163 是否触发了登录安全验证。"
            )

    mailbox_rows = st.session_state.get("email_mailboxes", [])
    mailbox_names = [row["display_name"] for row in mailbox_rows]
    configured_sent = base_config.sent_folder
    configured_incoming = base_config.incoming_folder
    configured_draft = base_config.draft_folder
    if mailbox_names:
        detected_sent = next(
            (
                row["display_name"]
                for row in mailbox_rows
                if row.get("is_sent")
            ),
            configured_sent,
        )
        if configured_sent not in mailbox_names:
            configured_sent = (
                detected_sent if detected_sent in mailbox_names else mailbox_names[0]
            )
        if configured_incoming not in mailbox_names:
            configured_incoming = (
                "求职同步" if "求职同步" in mailbox_names else mailbox_names[0]
            )
        detected_draft = next(
            (
                row["display_name"]
                for row in mailbox_rows
                if row.get("is_draft")
            ),
            configured_draft,
        )
        if configured_draft not in mailbox_names:
            configured_draft = (
                detected_draft if detected_draft in mailbox_names else mailbox_names[0]
            )
        f1, f2, f3 = st.columns(3)
        with f1:
            sent_folder = st.selectbox(
                "已发送文件夹",
                mailbox_names,
                index=mailbox_names.index(configured_sent),
            )
        with f2:
            incoming_folder = st.selectbox(
                "招聘回信文件夹",
                mailbox_names,
                index=mailbox_names.index(configured_incoming),
            )
        with f3:
            draft_folder = st.selectbox(
                "草稿文件夹",
                mailbox_names,
                index=mailbox_names.index(configured_draft),
            )
    else:
        f1, f2, f3 = st.columns(3)
        with f1:
            sent_folder = st.text_input("已发送文件夹", value=configured_sent)
        with f2:
            incoming_folder = st.text_input("招聘回信文件夹", value=configured_incoming)
        with f3:
            draft_folder = st.text_input("草稿文件夹", value=configured_draft)

    env_auto_apply = os.getenv("EMAIL_SYNC_AUTO_APPLY", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }
    auto_apply = st.toggle(
        "高置信度结果自动修改台账（测试稳定后再开启）",
        value=bool((saved or {}).get("auto_apply", env_auto_apply)),
        help="默认关闭。Offer 和无法确定轮次的面试即使开启也必须人工确认。",
    )
    remember_credentials = st.checkbox(
        "将固定邮箱和授权码保存到 macOS 钥匙串",
        value=st.session_state.get("email_keychain_has_credentials", False),
        disabled=not keychain_available(),
        help="由 macOS 钥匙串加密保存，不进入 job_search.db。",
    )
    config = EmailConnectionConfig(
        address=address,
        authorization_code=authorization_code,
        host=host,
        port=int(port),
        sent_folder=sent_folder,
        incoming_folder=incoming_folder,
        draft_folder=draft_folder,
        max_messages_per_folder=int(max_messages),
        auto_apply_high_confidence=auto_apply,
    )
    s1, s2 = st.columns(2)
    if s1.button(
        "保存设置",
        width="stretch",
        disabled=not address,
    ):
        try:
            save_account_settings(config)
            if remember_credentials:
                save_email_credentials(address, authorization_code)
                st.session_state.email_keychain_has_credentials = True
                st.success(
                    "设置已保存；固定邮箱和授权码已加密保存到 macOS 钥匙串。"
                )
            else:
                st.success("连接设置已保存；授权码只保留在当前应用会话。")
        except Exception as exc:
            st.error(f"保存失败：{exc}")
    if s2.button(
        "立即手动扫描",
        type="primary",
        width="stretch",
        disabled=not address or not authorization_code,
    ):
        with st.spinner("正在只读扫描已发送和求职同步文件夹……"):
            summary = scan_mailbox(config)
        st.session_state.email_scan_summary = summary.model_dump()
    if st.session_state.get("email_keychain_has_credentials"):
        if st.button("忘记钥匙串中的固定邮箱", type="tertiary"):
            if delete_email_credentials():
                st.session_state.email_keychain_has_credentials = False
                st.session_state.email_sync_address = ""
                st.session_state.email_sync_auth_code = ""
                st.success("已从 macOS 钥匙串删除固定邮箱凭据。")
                st.rerun()
            else:
                st.warning("没有找到可删除的固定邮箱凭据。")

if summary_data := st.session_state.get("email_scan_summary"):
    st.subheader(f"最近一次扫描 #{summary_data['run_id']}")
    metrics = st.columns(6)
    for column, (label, key) in zip(
        metrics,
        [
            ("扫描", "scanned"),
            ("新识别", "new_messages"),
            ("重复跳过", "duplicates"),
            ("待确认", "pending"),
            ("未匹配", "unmatched"),
            ("自动事件", "auto_applied"),
        ],
    ):
        column.metric(label, summary_data[key])
    if summary_data["ignored_non_job"]:
        st.caption(
            f"另有 {summary_data['ignored_non_job']} 封普通已发送邮件被本地规则忽略，"
            "未保存正文。"
        )
    for error in summary_data["errors"]:
        st.warning(error)

pending_tab, unmatched_tab, processed_tab, runs_tab, events_tab = st.tabs(
    ["待确认", "未匹配邮件", "已处理", "扫描历史", "投递事件"]
)
with pending_tab:
    _render_review(
        states=["pending"],
        key_prefix="pending",
        empty_message="没有等待确认的邮件。",
    )
with unmatched_tab:
    st.caption("低置信度或找不到唯一岗位的邮件留在这里，不会修改台账。")
    _render_review(
        states=["unmatched"],
        key_prefix="unmatched",
        empty_message="没有未匹配邮件。",
    )
with processed_tab:
    processed = load_email_messages(
        ["confirmed", "confirmed_no_change", "auto_applied", "ignored", "voided"]
    )
    if processed.empty:
        st.info("还没有处理记录。")
    else:
        st.dataframe(
            processed[
                [
                    "邮件ID", "时间", "方向", "主题", "识别类型",
                    "匹配公司", "匹配职位", "建议状态", "处理状态",
                ]
            ],
            hide_index=True,
            width="stretch",
        )
        undoable = processed[
            processed["处理状态"].isin(["confirmed", "auto_applied"])
            & processed["事件ID"].notna()
        ]
        if not undoable.empty:
            undo_map = {
                f"邮件 #{int(row['邮件ID'])}｜{row['主题'] or '（无主题）'}":
                int(row["邮件ID"])
                for _, row in undoable.iterrows()
            }
            undo_label = st.selectbox("撤销一次错误邮件同步", list(undo_map))
            if st.button("撤销所选邮件事件"):
                success, message = undo_email_message(undo_map[undo_label])
                (st.success if success else st.warning)(message)
                if success:
                    st.rerun()
with runs_tab:
    runs = load_sync_runs()
    if runs.empty:
        st.info("尚未扫描。")
    else:
        st.dataframe(runs, hide_index=True, width="stretch")
with events_tab:
    events = load_events()
    if events.empty:
        st.info("还没有投递事件。")
    else:
        email_events = events[events["来源"].astype(str).str.contains("163邮件")]
        st.dataframe(
            email_events,
            hide_index=True,
            width="stretch",
            column_config={"事件ID": None, "岗位ID": None},
        )
