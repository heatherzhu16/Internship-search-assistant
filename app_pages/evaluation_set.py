from __future__ import annotations

import pandas as pd
import streamlit as st

from models.evaluation import DIMENSION_NAMES, JDInfo, RUBRIC_VERSION
from profile_store import load_default_profile
from services.database import DB_PATH
from services.evaluation_set import (
    aggregate_case_runs,
    evaluation_report_metrics,
    failed_case_result,
    general_quality_checks,
    latest_evaluation_report,
    load_evaluation_cases,
    run_evaluation_case,
    save_evaluation_report,
)
from services.resume_service import default_resume_version
from services.scoring import get_client
from ui_helpers import api_settings, empty_state, page_header, section_header


page_header(
    "评分质量中心",
    "系统护栏保证不犯原则性错误；个人校准集保证结果符合你的实际决策。",
    "Evaluate",
)
api_key, model = api_settings()
_, _, _, profile = load_default_profile(DB_PATH)
resume = default_resume_version()
cases = load_evaluation_cases()


def _percent(value) -> str:
    return "—" if value is None else f"{value:.0%}"


def _score_status(result: dict) -> str:
    if result.get("abstained"):
        return "正确拒绝评分" if result.get("quality_pass") else "错误拒绝评分"
    if result.get("strict_score_pass", result.get("score_pass")):
        return "严格命中"
    if result.get("score_pass"):
        return "±5 分容差内"
    return "偏离"

section_header("01", "通用质量护栏", "这些规则与个人偏好无关，每次改动后都应全部通过。")
guardrails = pd.DataFrame(general_quality_checks())
st.dataframe(guardrails, hide_index=True, width="stretch")
passed_guardrails = int(guardrails["通过"].sum())
st.caption(f"当前通过 {passed_guardrails}/{len(guardrails)}。护栏失败时，不建议运行付费模型评测。")

if not cases:
    empty_state(
        "还没有个人校准案例",
        "通用护栏已经可用；之后可从带有红色“判断”段落的测评集 Word 导入个人案例。",
        icon=":material/science:",
    )
    st.stop()

section_header("02", "个人校准基准", "人工区间是带有 ±5 分容差的参照，不是模型必须拟合的唯一答案。")
case_rows = pd.DataFrame(
    [
        {
            "案例": case.case_id,
            "岗位": case.title,
            "期望区间": f"{case.score_min}-{case.score_max}",
            "输入预期": "允许评分" if case.expected_scorable else "信息不完整，应拒绝评分",
            "硬门槛期望": case.expected_gate,
            "预期缺口": "；".join(" / ".join(group) for group in case.expected_gap_groups),
            "人工判断": case.human_judgment,
        }
        for case in cases
    ]
)
st.dataframe(
    case_rows,
    hide_index=True,
    column_config={
        "案例": st.column_config.TextColumn("案例", pinned=True),
        "人工判断": st.column_config.TextColumn("人工判断", width="large"),
    },
    key="evaluation_cases_table",
)

section_header(
    "03",
    "运行当前评分规则",
    "每个案例只提取一次 JD；重复 3 次复用同一份结构化输入，只检查评分波动。",
)
labels = {f"{case.case_id} · {case.title}": case for case in cases}
selected_labels = st.multiselect(
    "选择案例",
    list(labels),
    placeholder="建议先选择 1–3 个曾经误判的案例",
)
repeat_count = st.segmented_control(
    "重复次数",
    [1, 3],
    default=1,
    format_func=lambda value: "运行一次" if value == 1 else "重复三次（稳定性）",
)
run_disabled = not selected_labels or not api_key or not resume
if st.button(
    "运行评测",
    icon=":material/play_arrow:",
    type="primary",
    disabled=run_disabled,
):
    client = get_client(api_key)
    results: list[dict] = []
    report_path = None
    with st.status("正在运行个人评测集……", expanded=True) as status:
        for label in selected_labels:
            case = labels[label]
            status.write(f"正在评估 {case.case_id} · {case.title}")
            runs: list[dict] = []
            errors: list[str] = []
            prepared_jd = None
            for _ in range(int(repeat_count or 1)):
                try:
                    run_result = run_evaluation_case(
                        case,
                        client=client,
                        model=model,
                        resume_text=resume.extracted_text,
                        profile=profile,
                        prepared_jd=prepared_jd,
                    )
                    runs.append(run_result)
                    if prepared_jd is None and run_result.get("jd_snapshot"):
                        prepared_jd = JDInfo.model_validate(run_result["jd_snapshot"])
                except Exception as exc:
                    errors.append(str(exc))
            if runs:
                result = aggregate_case_runs(case, runs)
                if errors:
                    result["stable"] = False
                    result["passed"] = False
                    result["error"] = "；".join(errors)[:300]
            else:
                result = failed_case_result(
                    case,
                    error="；".join(errors) or "模型未返回结果",
                    model=model,
                )
            results.append(result)
            report_path = save_evaluation_report(
                results,
                model=model,
                target=report_path,
            )
            if errors:
                status.write(f"{case.case_id} 有 {len(errors)} 次失败，已记录并继续。")
        status.update(
            label=f"评测完成：{sum(result['passed'] for result in results)}/{len(results)} 通过",
            state="complete",
            expanded=False,
        )
    st.session_state["latest_evaluation_report_path"] = str(report_path)
    st.rerun()

if not api_key:
    st.caption("请先在侧边栏配置 DeepSeek API Key。")
elif not resume:
    st.caption("请先在简历库设置默认简历；评测始终使用当前默认简历。")

report = latest_evaluation_report()
if report:
    section_header("04", "最近一次结果", "分别观察区间、门槛、缺口、排序和稳定性，不用单一通过率概括。")
    results = report.get("results", [])
    metrics = evaluation_report_metrics(results)
    if report.get("rubric_version") != RUBRIC_VERSION:
        st.warning(
            f"这份报告使用旧规则 {report.get('rubric_version', '未知')}。"
            f"当前规则是 {RUBRIC_VERSION}，请重新运行后再判断是否命中。"
        )
    st.caption(
        f"评分规则：{report.get('rubric_version', '未知')}｜"
        "同时展示严格区间与 ±5 分宽容区间；失败用于定位系统性偏差，不代表人工标签必然正确。"
    )
    with st.container(horizontal=True):
        st.metric("运行案例", len(results), border=True)
        st.metric(
            "±5 分宽容命中",
            _percent(metrics["interval_hit_rate"]),
            border=True,
        )
        st.metric(
            "严格区间命中",
            _percent(metrics.get("strict_interval_hit_rate")),
            border=True,
        )
        st.metric(
            "输入门控正确",
            _percent(metrics.get("input_guardrail_accuracy")),
            border=True,
        )
    with st.container(horizontal=True):
        st.metric(
            "硬门槛正确",
            _percent(metrics["gate_accuracy"]),
            border=True,
        )
        st.metric(
            "预期缺口召回",
            _percent(metrics["gap_recall"]),
            border=True,
        )
        st.metric(
            "平均容差外偏差",
            "—" if metrics.get("mean_tolerance_deviation") is None else f"{metrics['mean_tolerance_deviation']:.1f} 分",
            border=True,
        )
        st.metric(
            "人工排序一致率",
            "—" if metrics["rank_consistency"] is None else f"{metrics['rank_consistency']:.0%}",
            border=True,
        )
        st.metric(
            "重复运行稳定率",
            "未测" if metrics["stability_rate"] is None else f"{metrics['stability_rate']:.0%}",
            border=True,
        )
    result_rows = pd.DataFrame(
        [
            {
                "案例": result["case_id"],
                "岗位": result["title"],
                "期望分": result["expected_score"],
                "实际分": (
                    "拒绝评分"
                    if result.get("abstained")
                    else str(result.get("actual_score", "未完成"))
                ),
                "偏差": (
                    "输入不完整"
                    if result.get("abstained")
                    else "严格命中"
                    if result.get("strict_score_pass", result.get("score_pass"))
                    else f"容差内（高 {result.get('score_deviation')} 分）"
                    if result.get("score_pass") and (result.get("score_deviation") or 0) > 0
                    else f"容差内（低 {abs(result.get('score_deviation') or 0)} 分）"
                    if result.get("score_pass") and (result.get("score_deviation") or 0) < 0
                    else
                    f"高估 {result.get('score_deviation')} 分"
                    if (result.get("score_deviation") or 0) > 0
                    else f"低估 {abs(result.get('score_deviation') or 0)} 分"
                    if (result.get("score_deviation") or 0) < 0
                    else "命中"
                ),
                "区间": _score_status(result),
                "输入门控": "通过" if result.get("quality_pass") else "错误",
                "硬门槛": (
                    "未评估" if result.get("gate_pass") is None
                    else f"{result['actual_gate']} · {'通过' if result['gate_pass'] else '错误'}"
                ),
                "缺口": (
                    "未评估" if result.get("gap_pass") is None
                    else "通过" if result["gap_pass"] else "有遗漏"
                ),
                "波动": (
                    f"{result.get('score_spread')} 分"
                    + (
                        f"（{' / '.join(str(score) for score in result.get('run_scores', []))}）"
                        if result.get("run_scores")
                        else ""
                    )
                    if result.get("score_spread") is not None else "未完成"
                ),
                "错误": result.get("error", ""),
                "总体": "通过" if result["passed"] else "需调整",
            }
            for result in results
        ]
    )
    st.dataframe(result_rows, hide_index=True, key="latest_evaluation_results")

    failed_results = [result for result in results if not result.get("passed")]
    if failed_results:
        section_header(
            "05",
            "偏差诊断",
            "先看是哪个维度高估或低估，再调规则；不会把人工分数当成答案硬套。",
        )
        for result in failed_results:
            expander = st.expander(
                f"{result['case_id']} · {result['title']} — "
                f"期望 {result['expected_score']}，实际 "
                f"{'拒绝评分' if result.get('abstained') else result.get('actual_score', '未完成')}",
            )
            with expander:
                if result.get("error"):
                    st.error(result["error"])
                    continue
                dimensions = result.get("dimensions", {})
                if not dimensions:
                    if result.get("abstained"):
                        st.info("该案例缺少完整岗位职责，系统正确停止评分并要求补全信息。")
                        continue
                    st.info("这是旧版报告，重新运行后会显示维度证据与校准详情。")
                    continue
                dimension_rows = pd.DataFrame(
                    [
                        {
                            "维度": DIMENSION_NAMES.get(field, field),
                            "原始分": detail.get("raw_score"),
                            "校准分": detail.get("score"),
                            "满分": detail.get("max_score"),
                            "理由": detail.get("reason", ""),
                            "证据": "\n".join(detail.get("evidence", [])),
                            "缺口": "\n".join(detail.get("gaps", [])),
                        }
                        for field, detail in dimensions.items()
                    ]
                )
                st.dataframe(
                    dimension_rows,
                    hide_index=True,
                    column_config={
                        "维度": st.column_config.TextColumn("维度", pinned=True),
                        "理由": st.column_config.TextColumn("理由", width="large"),
                        "证据": st.column_config.TextColumn("证据", width="large"),
                        "缺口": st.column_config.TextColumn("缺口", width="large"),
                    },
                    key=f"diagnostics_{result['case_id']}",
                )
                if result.get("calibration_notes"):
                    st.markdown("**程序校准**")
                    for note in result["calibration_notes"]:
                        st.write(f"- {note}")
                if result.get("eligibility_checks"):
                    st.markdown("**硬门槛核对**")
                    st.dataframe(
                        pd.DataFrame(result["eligibility_checks"]),
                        hide_index=True,
                        key=f"eligibility_{result['case_id']}",
                    )
