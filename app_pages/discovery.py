from __future__ import annotations

import json
import re
from datetime import date

import pandas as pd
import streamlit as st

from constraint_engine import suggested_job_override
from models.discovery import PLATFORM_LABELS
from candidate_profile import EligibilityAssessment
from models.evaluation import DIMENSION_NAMES, Evaluation, WEIGHTS
from profile_store import load_default_profile
from services.browser_capture import get_capture_token
from services.browser_launcher import chrome_available, open_in_chrome
from services.database import APP_DIR, DB_PATH
from services.discovery_scoring import score_discovery_item
from services.discovery_service import (
    STATUS_LABELS,
    apply_capture_quality_flags,
    company_preference,
    confirm_as_recruitment,
    filter_discovery_items_by_text,
    get_discovery_item,
    import_to_application,
    load_decision_feedback,
    load_discovery_items,
    update_discovery_content,
    update_discovery_status,
    update_manual_decision,
)
from services.decision_support import build_decision_summary
from services.resume_service import default_resume_version
from services.publication_date import publication_recency_label
from services.scoring import RUBRIC_VERSION, get_client
from services.search_assistant import build_platform_links, build_search_keywords
from ui_helpers import api_settings, empty_state, page_header, section_header


def _job_number(item_id: int) -> str:
    return f"#{int(item_id):04d}"


def _score_discovery_id(
    item_id: int,
    *,
    scoring_client,
    model_name: str,
    resume_version_id: int,
    profile_version_id: int,
    candidate_profile,
) -> dict:
    detail = get_discovery_item(item_id)
    if detail.get("availability_status") == "expired":
        raise ValueError("岗位已标记为失效，不能生成正式匹配分。")
    if detail.get("capture_kind") == "image":
        raise ValueError("图片 JD 缺少可核验文字，请先补全完整 JD。")
    allowed, reason = company_preference(
        str(detail.get("company") or ""), candidate_profile
    )
    if not allowed:
        raise ValueError(f"公司偏好为“{reason}”，已停止自动评分。")
    raw_text = str(detail.get("raw_text") or "")
    override = suggested_job_override(
        candidate_profile,
        raw_text,
        location=str(detail.get("location") or ""),
        reference_date=date.today(),
    )
    return score_discovery_item(
        item_id=item_id,
        client=scoring_client,
        model=model_name,
        resume_version_id=resume_version_id,
        profile_version_id=profile_version_id,
        profile=candidate_profile,
        job_override=override,
    )


def _handle_primary_action(item_ids: list[int]) -> None:
    click = st.session_state.get("decision_primary_action")
    if not click:
        return
    row_index = int(click.get("row", -1))
    if row_index < 0 or row_index >= len(item_ids):
        return
    item_id = item_ids[row_index]
    label = str(click.get("label") or "")
    st.session_state["decision_selected_id"] = item_id
    st.session_state["decision_action_selected_id"] = item_id
    if "评分" in label or "重评" in label:
        st.session_state["pending_discovery_score_id"] = item_id
    elif "补全" in label:
        st.session_state["expand_discovery_content_id"] = item_id


def _primary_action_label(row: pd.Series) -> str:
    if row["状态代码"] == "dismissed":
        return ":material/visibility: 查看"
    if row["有效性代码"] == "expired":
        return ":material/event_busy: 查看"
    if row["内容级别代码"] != "full" or row["内容形态代码"] == "image":
        return ":material/edit_note: 补全"
    if row.get("评分版本") == RUBRIC_VERSION and pd.notna(row.get("匹配分")):
        return ":material/refresh: 重评"
    return ":material/query_stats: 评分"


def _render_decision_summary(detail: dict, candidate_profile) -> None:
    evaluation = Evaluation.model_validate_json(detail["evaluation_json"])
    eligibility = EligibilityAssessment.model_validate_json(detail["eligibility_json"])
    summary = build_decision_summary(
        evaluation=evaluation,
        eligibility=eligibility,
        profile=candidate_profile,
        score=int(detail["fit_score"]),
        company=str(detail.get("company") or ""),
        role=str(detail.get("role") or detail.get("title") or ""),
        location=str(detail.get("location") or ""),
        content_level=str(detail.get("content_level") or "summary"),
        completeness_score=int(detail.get("completeness_score") or 0),
        capture_kind=str(detail.get("capture_kind") or "text"),
    )
    layers = [summary.eligibility, summary.capability, summary.preference, summary.information]
    columns = st.columns(4)
    metrics = [
        ("匹配分", f"{int(detail['fit_score'])} 分"),
        ("投递建议", str(detail.get("recommendation") or "待判断")),
        ("硬性门槛", summary.eligibility.result),
        ("可信度", summary.confidence),
    ]
    for column, (label, value) in zip(columns, metrics, strict=True):
        column.metric(label, value, border=True)
    st.caption(
        f"建议动作：{summary.suggested_action}。参考分用于比较岗位，不代替你的最终决定。"
    )
    with st.expander("查看四层判断与注意事项"):
        for layer in layers:
            st.markdown(f"**{layer.label}：{layer.result}**")
            st.caption(layer.reason)
        for caveat in summary.caveats:
            st.caption(f"• {caveat}")


page_header(
    "岗位收录与判断",
    "浏览岗位时一键收录；系统负责完整度检查、可信评分和投递台账衔接。",
    "Capture",
)
api_key, model = api_settings()
_, profile_version_id, _, candidate_profile = load_default_profile(DB_PATH)
default_resume = default_resume_version()

all_items = load_discovery_items()
active_items = all_items.loc[all_items["状态代码"].ne("dismissed")].copy()
if st.session_state.pop("capture_refresh_completed", False):
    st.toast(f"已刷新，目前保留 {len(active_items)} 个岗位。")
with st.container(horizontal=True):
    st.metric("已收录", len(active_items), border=True, help="不包含已移出的岗位")
    st.metric(
        "完整 JD",
        int(active_items.get("内容级别代码", pd.Series(dtype="string")).eq("full").sum()),
        border=True,
    )
    st.metric(
        "待我决定",
        int(
            active_items.get("我的判断", pd.Series(dtype="string"))
            .isin(["继续了解", "待定", "信息待补全"])
            .sum()
        ),
        border=True,
    )
    st.metric(
        "已进台账",
        int(active_items.get("状态代码", pd.Series(dtype="string")).eq("imported").sum()),
        border=True,
    )

section_header(
    "01",
    "搜索辅助",
    "生成平台内搜索入口；只帮助发现岗位，不把搜索摘要当作完整 JD 评分。",
)
with st.container(border=True):
    default_roles = candidate_profile.preferences.target_roles[:4]
    default_cities = candidate_profile.preferences.preferred_cities[:3]
    search_columns = st.columns(2)
    with search_columns[0]:
        search_roles = st.multiselect(
            "岗位方向",
            options=default_roles or ["产品", "战略", "数据分析", "运营"],
            default=default_roles,
            accept_new_options=True,
            placeholder="选择或输入岗位方向",
        )
    with search_columns[1]:
        search_cities = st.multiselect(
            "城市",
            options=default_cities or ["北京", "上海", "深圳", "杭州", "广州"],
            default=default_cities,
            accept_new_options=True,
            placeholder="选择或输入城市",
        )
    keywords = build_search_keywords(
        candidate_profile,
        roles=search_roles or None,
        cities=search_cities or None,
        limit=6,
    )
    selected_keyword = st.selectbox("本次搜索词", keywords)
    st.caption(
        "下方按钮会直接调用本机 Chrome，不受 Safari 默认浏览器设置影响；"
        "感兴趣后打开详情并使用扩展收录。"
    )
    links = [link for link in build_platform_links([selected_keyword])]
    has_chrome = chrome_available()
    if not has_chrome:
        st.warning("未在“应用程序”目录检测到 Google Chrome，暂时无法使用扩展收录流程。")
    link_columns = st.columns(3)
    for index, (column, link) in enumerate(zip(link_columns, links, strict=True)):
        with column:
            if st.button(
                f"用 Chrome 搜索 {link.platform}",
                icon=":material/open_in_new:",
                width="stretch",
                disabled=not has_chrome,
                key=f"chrome_search_{index}_{selected_keyword}",
            ):
                try:
                    open_in_chrome(link.url)
                    st.toast(f"已在 Chrome 打开 {link.platform}。")
                except (ValueError, RuntimeError) as exc:
                    st.error(str(exc))
            st.caption(link.note)

section_header(
    "02",
    "浏览时一键收录",
    "支持小红书、BOSS 直聘和实习僧；不再依赖批量爬取或平台搜索结果。",
)
with st.container(border=True):
    capture_error = str(st.session_state.get("browser_capture_error") or "")
    if capture_error:
        st.error(capture_error)
    else:
        st.badge("本地收录服务已启动", icon=":material/check_circle:", color="green")
    st.write(
        "在平台上正常搜索并打开感兴趣的岗位，点击 Chrome 扩展完成收录。"
        "系统会保存当前文字、来源链接和页面截图；完整 JD 等你逐条确认后评分，图片 JD 会等待补全文字。"
    )
    with st.container(horizontal=True):
        st.page_link(
            "app_pages/job_analysis.py",
            label="直接粘贴 JD 分析",
            icon=":material/content_paste:",
        )
        if st.button("刷新收录结果", icon=":material/refresh:"):
            st.session_state["capture_refresh_completed"] = True
            st.rerun()
    recent_captures = (
        all_items.loc[all_items["收录方式"].eq("browser_extension")]
        .sort_values("发现时间", ascending=False)
        .head(3)
    )
    if not recent_captures.empty:
        recent_labels = [
            f"{_job_number(int(row['线索ID']))} {row['平台']}｜"
            f"{row['公司']} · {row['职位']}"
            for _, row in recent_captures.iterrows()
        ]
        st.caption("最近通过扩展收录：" + "；".join(recent_labels))
    with st.expander("安装扩展与首次配对", icon=":material/extension:"):
        st.markdown(
            "1. 在 Chrome 打开 `chrome://extensions` 并启用开发者模式。\n"
            "2. 点击“加载已解压的扩展程序”，选择下面的扩展目录。\n"
            "3. 修改扩展代码后，回到扩展管理页点击刷新按钮。\n"
            "4. 点击扩展图标，把配对码保存一次。"
        )
        st.caption("扩展目录")
        st.code(str(APP_DIR / "browser_extension"), language=None)
        st.caption("本机配对码（不要提交到 GitHub）")
        st.code(get_capture_token(), language=None)
        st.caption(
            "扩展仅连接当前电脑的 127.0.0.1，不读取平台密码，也不会自动投递、发送消息或操作账号。"
        )

section_header(
    "03",
    "岗位决策箱",
    "先确认信息，再逐条评分；最终是否投递始终由你确认。",
)
recommendation_values = sorted(
    {
        str(value).strip()
        for value in all_items.get("投递建议", pd.Series(dtype="string")).dropna()
        if str(value).strip()
    }
)
filter_row = st.columns([2, 1])
with filter_row[0]:
    query = st.text_input(
        "搜索岗位",
        placeholder="搜索公司、职位或城市",
        icon=":material/search:",
    )
with filter_row[1]:
    status_filter = st.selectbox(
        "状态",
        ["__active__", "__all__", *STATUS_LABELS],
        format_func=lambda value: {
            "__active__": "全部（不含已移出）",
            "__all__": "全部状态",
        }.get(value, STATUS_LABELS.get(value, value)),
    )
secondary_filters = st.columns(2)
with secondary_filters[0]:
    platform_filter = st.selectbox(
        "平台",
        ["__all__", *PLATFORM_LABELS],
        format_func=lambda value: "全部平台" if value == "__all__" else PLATFORM_LABELS[value],
    )
with secondary_filters[1]:
    recommendation_filter = st.selectbox(
        "投递建议",
        ["__all__", "__unscored__", *recommendation_values],
        format_func=lambda value: {
            "__all__": "全部建议",
            "__unscored__": "尚未评分",
        }.get(value, value),
    )
sort_mode = st.segmented_control(
    "排序",
    ["id_desc", "score_desc", "id_asc"],
    default="id_desc",
    format_func=lambda value: {
        "id_desc": "编号：新到旧",
        "score_desc": "分数：高到低",
        "id_asc": "编号：旧到新",
    }[value],
)

items = all_items.copy()
if platform_filter != "__all__":
    items = items.loc[items["平台代码"].eq(platform_filter)]
if status_filter == "__active__":
    items = items.loc[items["状态代码"].ne("dismissed")]
elif status_filter != "__all__":
    items = items.loc[items["状态代码"].eq(status_filter)]
if recommendation_filter == "__unscored__":
    items = items.loc[items["投递建议"].fillna("").eq("")]
elif recommendation_filter != "__all__":
    items = items.loc[items["投递建议"].fillna("").eq(recommendation_filter)]
items = filter_discovery_items_by_text(items, query)
if sort_mode == "score_desc":
    items = items.sort_values(["匹配分", "线索ID"], ascending=[False, False], na_position="last")
elif sort_mode == "id_asc":
    items = items.sort_values("线索ID", ascending=True)
else:
    items = items.sort_values("线索ID", ascending=False)
if not items.empty:
    items["公司偏好"] = items["公司"].map(
        lambda company: company_preference(str(company), candidate_profile)[1]
    )

if items.empty:
    empty_state(
        "还没有符合条件的岗位",
        "先在小红书、BOSS 或实习僧打开岗位，再使用扩展一键收录。",
        icon=":material/inbox:",
    )
else:
    display = items.copy()
    display.insert(0, "岗位编号", display["线索ID"].map(_job_number))
    display["岗位"] = (
        display["公司"] + " · " + display["职位"]
        + display["城市"].fillna("").map(lambda value: f"（{value}）" if str(value).strip() else "")
    )
    display["分数"] = pd.to_numeric(display["匹配分"], errors="coerce")
    display["建议"] = display["投递建议"].fillna("").replace("", "待评分")
    display["状态与建议"] = display["状态"] + " · " + display["建议"]
    display["主操作"] = display.apply(_primary_action_label, axis=1)
    visible_columns = [
        "岗位编号", "岗位", "分数", "状态与建议", "主操作",
    ]
    item_ids = [int(value) for value in display["线索ID"].tolist()]
    table_event = st.dataframe(
        display[visible_columns],
        hide_index=True,
        width="stretch",
        height=min(540, max(220, 38 * (len(display) + 1))),
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "岗位编号": st.column_config.TextColumn("编号", pinned=True, width="small"),
            "岗位": st.column_config.TextColumn("公司 · 职位", width="large"),
            "分数": st.column_config.NumberColumn(
                "分数", min_value=0, max_value=100, format="%d", width="small"
            ),
            "状态与建议": st.column_config.TextColumn("状态 · 投递建议", width="medium"),
            "主操作": st.column_config.ButtonColumn(
                "操作",
                on_click=_handle_primary_action,
                args=(item_ids,),
                key="decision_primary_action",
                type="secondary",
                alignment="center",
                width="small",
            ),
        },
        key="decision_box_table_v2",
    )
    action_selected_id = st.session_state.pop("decision_action_selected_id", None)
    selected_rows = list(table_event.selection.rows)
    if action_selected_id in item_ids:
        selected_id = int(action_selected_id)
    elif selected_rows and 0 <= selected_rows[0] < len(item_ids):
        selected_id = item_ids[selected_rows[0]]
        st.session_state["decision_selected_id"] = selected_id
    elif st.session_state.get("decision_selected_id") in item_ids:
        selected_id = int(st.session_state["decision_selected_id"])
    else:
        selected_id = item_ids[0]
        st.session_state["decision_selected_id"] = selected_id
    detail = get_discovery_item(selected_id)

    pending_score_id = st.session_state.pop("pending_discovery_score_id", None)
    if pending_score_id == selected_id:
        company_allowed, company_reason = company_preference(
            str(detail.get("company") or ""), candidate_profile
        )
        score_error = ""
        if detail.get("content_level") != "full" or detail.get("capture_kind") == "image":
            score_error = "岗位文字尚不完整，请先补全完整 JD。"
        elif detail.get("availability_status") == "expired":
            score_error = "岗位已失效，不能评分。"
        elif detail.get("status") == "dismissed":
            score_error = "岗位已移出，请先恢复后再评分。"
        elif not company_allowed:
            score_error = f"当前公司偏好不允许评分：{company_reason}。"
        elif not api_key:
            score_error = "请先配置 DeepSeek API Key。"
        elif not default_resume:
            score_error = "请先在简历库设置默认简历。"
        if score_error:
            st.warning(score_error)
        else:
            try:
                with st.status("正在使用默认简历评估岗位……", expanded=True):
                    _score_discovery_id(
                        selected_id,
                        scoring_client=get_client(api_key),
                        model_name=model,
                        resume_version_id=default_resume.id,
                        profile_version_id=profile_version_id,
                        candidate_profile=candidate_profile,
                    )
                st.toast("评分已更新。", icon=":material/check_circle:")
                st.rerun()
            except Exception as exc:
                st.error(f"评分失败：{exc}")

    with st.container(border=True):
        st.subheader(
            f"{_job_number(selected_id)} · "
            f"{detail.get('company') or '待提取公司'} · "
            f"{detail.get('role') or detail.get('title') or '待提取岗位'}"
        )
        with st.container(horizontal=True):
            original_url = detail.get("access_url") or detail["canonical_url"]
            if st.button(
                "用 Chrome 打开原始页面",
                icon=":material/open_in_new:",
                disabled=not chrome_available(),
                key=f"chrome_original_{selected_id}",
            ):
                try:
                    open_in_chrome(original_url)
                    st.toast("已在 Chrome 打开原始页面。")
                except (ValueError, RuntimeError) as exc:
                    st.error(str(exc))
            if detail.get("status") != "imported":
                is_dismissed = detail.get("status") == "dismissed"
                if st.button(
                    "恢复岗位" if is_dismissed else "移出岗位",
                    icon=(
                        ":material/restore_from_trash:"
                        if is_dismissed
                        else ":material/delete:"
                    ),
                    type="tertiary",
                    key=f"toggle_dismissed_{selected_id}",
                ):
                    update_discovery_status(
                        selected_id,
                        "new" if is_dismissed else "dismissed",
                    )
                    st.toast("岗位已恢复。" if is_dismissed else "岗位已移出。")
                    st.rerun()
            st.badge(
                {"active": "招聘中", "expired": "已失效"}.get(
                    str(detail.get("availability_status")), "有效性待确认"
                ),
                color="red" if detail.get("availability_status") == "expired" else "green",
            )
            if detail.get("posted_at"):
                recency = publication_recency_label(str(detail.get("posted_at")))
                st.badge(
                    " · ".join(part for part in [str(detail.get("posted_at")), recency] if part),
                    icon=":material/schedule:",
                    color="green" if recency == "新发布" else "blue",
                )
            st.badge(
                {"text": "文字 JD", "mixed": "图文 JD", "image": "图片 JD"}.get(
                    str(detail.get("capture_kind")), "文字 JD"
                ),
                color="orange" if detail.get("capture_kind") == "image" else "blue",
            )
            st.badge(
                {
                    "full": "完整 JD",
                    "partial": "详情不完整",
                    "summary": "仅摘要",
                }.get(str(detail.get("content_level")), "详情不完整"),
                color="green" if detail.get("content_level") == "full" else "orange",
            )

        if detail.get("availability_status") == "expired":
            st.warning(
                "页面包含“停止招聘 / 已下线 / 已过期”等提示，系统已停止自动评分。",
                icon=":material/event_busy:",
            )
            if st.button("我已确认仍在招聘", icon=":material/event_available:"):
                apply_capture_quality_flags(
                    selected_id,
                    availability_status="active",
                    capture_kind=str(detail.get("capture_kind") or "text"),
                )
                st.rerun()
        if detail.get("capture_kind") == "image":
            st.warning(
                "该岗位主要通过图片展示。系统不会自动识别图片文字，"
                "请在下方补全完整 JD 后再评分。",
                icon=":material/image:",
            )
        elif detail.get("content_level") != "full":
            missing_labels = {
                "company": "公司", "role": "职位", "location": "地点",
                "responsibilities": "岗位职责", "requirements": "任职要求",
            }
            missing = json.loads(detail.get("missing_fields_json") or "[]")
            st.warning(
                "信息尚不完整，缺少："
                + "、".join(missing_labels.get(key, key) for key in missing),
                icon=":material/info:",
            )
            if re.search(r"(?<!\d)1\s*/\s*2(?!\d)", str(detail.get("raw_text") or "")):
                st.info(
                    "当前页面显示为轮播第 1/2 张。请从平台复制完整文字，"
                    "或自行转成文字后粘贴到下方的“完整 JD”。",
                    icon=":material/filter_2:",
                )

        with st.container(border=True):
            st.markdown("#### 我的判断")
            decisions = ["准备投递", "继续了解", "暂不投递", "信息待补全"]
            aliases = {"想投": "准备投递", "待定": "继续了解", "不投": "暂不投递"}
            current = aliases.get(
                str(detail.get("manual_decision") or "继续了解"),
                str(detail.get("manual_decision") or "继续了解"),
            )
            decision_columns = st.columns([1, 1.25, 2, 0.7], vertical_alignment="bottom")
            with decision_columns[0]:
                decision = st.selectbox(
                    "结论",
                    decisions,
                    index=decisions.index(current) if current in decisions else 1,
                    key=f"manual_decision_{selected_id}",
                )
            reason_options = {
                "准备投递": ["方向与公司合适", "岗位内容高度匹配", "值得尝试", "其他"],
                "继续了解": ["需要确认硬门槛", "需要确认岗位内容", "需要确认公司或团队", "其他"],
                "暂不投递": ["岗位方向不合适", "公司不符合偏好", "城市或时间不合适", "硬门槛不满足", "岗位已过期", "其他"],
                "信息待补全": ["只有摘要", "图片 JD 待补全文字", "公司/地点缺失", "职责或要求缺失", "其他"],
            }
            with decision_columns[1]:
                reason = st.selectbox(
                    "主要原因",
                    reason_options[decision],
                    key=f"decision_reason_{selected_id}_{decision}",
                )
            with decision_columns[2]:
                notes = st.text_input(
                    "备注（可选）",
                    key=f"decision_notes_{selected_id}",
                    placeholder="例如：先向 HR 确认每周到岗天数",
                )
            with decision_columns[3]:
                if st.button(
                    "保存",
                    icon=":material/save:",
                    key=f"save_decision_{selected_id}",
                    width="stretch",
                ):
                    update_manual_decision(selected_id, decision, reason=reason, notes=notes)
                    st.toast("个人判断已保存。", icon=":material/check_circle:")
                    st.rerun()
            feedback = load_decision_feedback(selected_id)
            if not feedback.empty:
                with st.expander("查看判断记录"):
                    st.dataframe(feedback, hide_index=True, width="stretch")

        snapshot_path = str(detail.get("snapshot_path") or "")
        if snapshot_path:
            resolved = APP_DIR / snapshot_path
            if resolved.exists():
                with st.expander("查看收录截图"):
                    st.image(str(resolved), caption="收录时的可见页面截图", width="stretch")

        with st.expander("查看收录到的原始文字"):
            st.text_area(
                "原始文字",
                value=str(detail.get("raw_text") or ""),
                height=280,
                disabled=True,
                key=f"raw_text_{selected_id}",
            )

        if detail.get("status") != "imported":
            with st.expander(
                "补全或修正 JD",
                expanded=(
                    detail.get("capture_kind") == "image"
                    or st.session_state.pop("expand_discovery_content_id", None) == selected_id
                ),
            ):
                st.caption("粘贴完整岗位职责和任职要求；保存后会重新检查完整度。")
                with st.form(f"enrich_{selected_id}"):
                    metadata = st.columns(2)
                    with metadata[0]:
                        company = st.text_input("公司", value=str(detail.get("company") or ""))
                        location = st.text_input("地点", value=str(detail.get("location") or ""))
                    with metadata[1]:
                        role = st.text_input(
                            "职位", value=str(detail.get("role") or detail.get("title") or "")
                        )
                        salary = st.text_input("薪资", value=str(detail.get("salary") or ""))
                    posted_at = st.text_input(
                        "发布时间",
                        value=str(detail.get("posted_at") or ""),
                        placeholder="例如 08-10 或 2026-08-10",
                        help="只用于发现箱的新鲜度提示，不参与匹配评分。",
                    )
                    jd_text = st.text_area(
                        "完整 JD",
                        height=300,
                        placeholder="请手动粘贴完整岗位职责和任职要求。图片 JD 不会自动识别。",
                    )
                    save_content = st.form_submit_button(
                        "保存并检查完整度",
                        type="primary",
                        icon=":material/fact_check:",
                    )
                if save_content:
                    try:
                        result = update_discovery_content(
                            selected_id,
                            raw_text=jd_text,
                            company=company,
                            role=role,
                            location=location,
                            salary=salary,
                            posted_at=posted_at,
                        )
                        if result["scorable"]:
                            apply_capture_quality_flags(
                                selected_id,
                                availability_status=str(
                                    detail.get("availability_status") or "active"
                                ),
                                capture_kind="text",
                            )
                            st.success("JD 已补全，现在可以正式评分。")
                        else:
                            missing_labels = {
                                "company": "公司",
                                "role": "职位",
                                "location": "地点",
                                "responsibilities": "岗位职责",
                                "requirements": "任职要求",
                            }
                            missing_text = "、".join(
                                missing_labels.get(field, field)
                                for field in result["missing_fields"]
                            )
                            st.warning(
                                f"信息完整度 {result['completeness_score']}%，"
                                f"仍缺少：{missing_text or '可识别的完整 JD 字段'}。"
                            )
                        st.rerun()
                    except ValueError as exc:
                        st.warning(str(exc))

        if detail.get("status") in {"not_recruitment", "needs_review"}:
            if st.button("确认这是招聘信息", icon=":material/check:"):
                confirm_as_recruitment(selected_id)
                st.rerun()

    detail = get_discovery_item(selected_id)
    if detail.get("latest_evaluation_id"):
        section_header("04", "评分结果", "先看结论，需要时再展开四层判断和详细依据。")
        if detail.get("rubric_version") != RUBRIC_VERSION:
            st.info("这条岗位使用旧版评分规则，请重新评分。", icon=":material/update:")
        else:
            _render_decision_summary(detail, candidate_profile)

            evaluation = Evaluation.model_validate_json(detail["evaluation_json"])
            score_rows = pd.DataFrame(
                [
                    {
                        "评分维度": DIMENSION_NAMES[field],
                        "得分": getattr(evaluation, field).score,
                        "满分": WEIGHTS[field],
                        "评分原因": getattr(evaluation, field).score_reason,
                    }
                    for field in WEIGHTS
                ]
            )
            with st.expander("查看详细评分依据"):
                st.dataframe(score_rows, hide_index=True, width="stretch")
                if evaluation.strengths:
                    st.markdown("**主要优势**")
                    for strength in evaluation.strengths:
                        st.write(f"- {strength}")
                if evaluation.risks:
                    st.markdown("**主要风险**")
                    for risk in evaluation.risks:
                        st.write(f"- {risk}")
                if evaluation.missing_keywords:
                    st.markdown("**缺失关键词**")
                    st.write("、".join(evaluation.missing_keywords))

            manual_decision = {
                "想投": "准备投递", "待定": "继续了解", "不投": "暂不投递"
            }.get(str(detail.get("manual_decision") or ""), str(detail.get("manual_decision") or ""))
            if detail["status"] not in {"shortlisted", "imported"}:
                if st.button(
                    "确认加入候选清单",
                    icon=":material/bookmark_add:",
                    type="primary",
                    disabled=manual_decision != "准备投递",
                    help="请先把“我的判断”保存为“准备投递”。",
                ):
                    update_discovery_status(selected_id, "shortlisted")
                    st.rerun()
            elif detail["status"] == "shortlisted":
                if st.button(
                    "加入投递计划",
                    icon=":material/playlist_add:",
                    type="primary",
                ):
                    success, message = import_to_application(selected_id)
                    (st.success if success else st.warning)(message)
                    if success:
                        st.rerun()
