from __future__ import annotations

import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from services.credential_store import (
    delete_deepseek_credentials,
    keychain_available,
    load_deepseek_credentials,
    save_deepseek_credentials,
)
from services.browser_capture import start_capture_server
from services.database import APP_DIR, init_database
from ui_helpers import apply_global_styles


load_dotenv(APP_DIR / ".env")
init_database()


@st.cache_resource
def _browser_capture_service():
    try:
        return start_capture_server(), ""
    except OSError as exc:
        return None, f"浏览器收录服务无法启动：{exc}"


_, browser_capture_error = _browser_capture_service()
st.session_state["browser_capture_error"] = browser_capture_error

if "deepseek_keychain_loaded" not in st.session_state:
    stored_deepseek = load_deepseek_credentials()
    st.session_state.deepseek_keychain_loaded = True
    st.session_state.deepseek_keychain_has_credentials = bool(stored_deepseek)
    st.session_state.setdefault(
        "api_key",
        os.getenv("DEEPSEEK_API_KEY", "")
        or (stored_deepseek[0] if stored_deepseek else ""),
    )
    st.session_state.setdefault(
        "model",
        os.getenv("DEEPSEEK_MODEL", "")
        or (stored_deepseek[1] if stored_deepseek else "deepseek-chat"),
    )

st.set_page_config(
    page_title="实习求职助手",
    page_icon=":material/explore:",
    layout="wide",
)
apply_global_styles()
st.logo(
    "static/career_os_logo.svg",
    size="large",
    icon_image="static/career_os_icon.svg",
)

discovery_page = st.Page(
    "app_pages/discovery.py",
    title="岗位决策",
    default=True,
)
analysis_page = st.Page(
    "app_pages/job_analysis.py",
    title="单份 JD",
)
materials_page = st.Page(
    "app_pages/materials.py",
    title="材料生成",
)
ledger_page = st.Page(
    "app_pages/ledger.py",
    title="投递台账",
)
dashboard_page = st.Page(
    "app_pages/dashboard.py",
    title="复盘看板",
)
rubric_page = st.Page(
    "app_pages/rubric.py",
    title="评分规则",
    icon=":material/tune:",
)
resumes_page = st.Page(
    "app_pages/resumes.py",
    title="简历库",
    icon=":material/folder_open:",
)
history_page = st.Page(
    "app_pages/history.py",
    title="分析历史",
    icon=":material/schedule:",
)
sync_page = st.Page(
    "app_pages/sync_center.py",
    title="同步中心",
    icon=":material/sync_alt:",
)
evaluation_set_page = st.Page(
    "app_pages/evaluation_set.py",
    title="评分质量",
    icon=":material/science:",
)

navigation = st.navigation(
    {
        "": [
            discovery_page,
            analysis_page,
            materials_page,
            ledger_page,
            dashboard_page,
        ],
        "系统工具": [
            rubric_page,
            resumes_page,
            history_page,
            sync_page,
            evaluation_set_page,
        ],
    },
    position="top",
)

with st.sidebar:
    st.caption("SYSTEM / 06—10")
    with st.container(key="toolbox_navigation", gap=None):
        st.page_link(rubric_page, label="06  评分规则", icon=":material/tune:", width="stretch")
        st.page_link(resumes_page, label="07  简历库", icon=":material/folder_open:", width="stretch")
        st.page_link(history_page, label="08  分析历史", icon=":material/schedule:", width="stretch")
        st.page_link(sync_page, label="09  同步中心", icon=":material/sync_alt:", width="stretch")
        st.page_link(evaluation_set_page, label="10  评分质量", icon=":material/science:", width="stretch")

    st.space("medium")
    st.caption("AI / CONNECTION")
    current_api_key = str(st.session_state.get("api_key", "")).strip()
    current_model = str(st.session_state.get("model", "")).strip() or "deepseek-chat"
    valid_api_key = bool(
        current_api_key
        and current_api_key.isascii()
        and current_api_key.startswith("sk-")
        and not any(character.isspace() for character in current_api_key)
    )
    if valid_api_key:
        st.badge("模型已连接", icon=":material/check:", color="green")
    else:
        st.badge("模型待配置", icon=":material/radio_button_unchecked:", color="orange")
    st.caption(f"当前模型 · {current_model}")
    with st.expander("模型与隐私设置", icon=":material/settings:"):
        st.text_input(
            "DeepSeek API Key",
            type="password",
            key="api_key",
            persist_state="session",
            help="建议保存到 macOS 钥匙串；不会写入数据库或代码仓库。",
        )
        st.text_input(
            "模型名称",
            key="model",
            persist_state="session",
        )
        current_api_key = str(st.session_state.get("api_key", "")).strip()
        current_model = str(st.session_state.get("model", "")).strip() or "deepseek-chat"
        valid_api_key = bool(
            current_api_key
            and current_api_key.isascii()
            and current_api_key.startswith("sk-")
            and not any(character.isspace() for character in current_api_key)
        )
        if current_api_key and not valid_api_key:
            st.error("API Key 含中文、空格或格式不完整。")
        elif st.session_state.get("deepseek_keychain_has_credentials"):
            st.caption("已从 macOS 钥匙串安全读取。")
        if st.button(
            "保存模型配置",
            icon=":material/key:",
            type="primary",
            disabled=not valid_api_key or not keychain_available(),
            width="stretch",
        ):
            try:
                save_deepseek_credentials(current_api_key, current_model)
                st.session_state.deepseek_keychain_has_credentials = True
                st.toast("模型配置已安全保存。", icon=":material/check_circle:")
                st.rerun()
            except Exception as exc:
                st.error(f"保存失败：{exc}")
        if (
            st.session_state.get("deepseek_keychain_has_credentials")
            and st.button("忘记模型配置", type="tertiary", width="stretch")
        ):
            if delete_deepseek_credentials():
                st.session_state.deepseek_keychain_has_credentials = False
                st.toast("已删除钥匙串中的模型配置。")
                st.rerun()
        st.caption("数据默认仅保存在当前电脑。")

navigation.run()
