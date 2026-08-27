from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from models.application import STAGE_RANK
from services.database import APP_DIR


DEMO_DATA_PATH = APP_DIR / "demo_data.csv"


def load_demo_applications() -> pd.DataFrame:
    if not DEMO_DATA_PATH.exists():
        return pd.DataFrame()
    data = pd.read_csv(DEMO_DATA_PATH)
    today = date.today()
    data["投递日期"] = data["投递日期偏移"].apply(
        lambda days: (today + timedelta(days=int(days))).isoformat()
    )
    data["下次跟进"] = data["跟进日期偏移"].apply(
        lambda days: (
            (today + timedelta(days=int(days))).isoformat()
            if pd.notna(days) else ""
        )
    )
    return data.drop(columns=["投递日期偏移", "跟进日期偏移"])


def prepare_dashboard_data(real: pd.DataFrame, scope: str) -> pd.DataFrame:
    demo = load_demo_applications()
    if scope == "仅真实数据":
        data = real.copy()
    elif scope == "仅演示数据":
        data = demo.copy()
    else:
        data = pd.concat([real, demo], ignore_index=True)
    if data.empty:
        return data
    data["阶段序号"] = data["最高进展"].map(STAGE_RANK).fillna(0)
    data["已投递标记"] = data["阶段序号"] >= STAGE_RANK["已投递"]
    data["面试标记"] = data["阶段序号"] >= STAGE_RANK["一面"]
    data["Offer标记"] = data["阶段序号"] >= STAGE_RANK["Offer"]
    return data


def funnel_summary(data: pd.DataFrame) -> pd.DataFrame:
    stages = ["待投递", "已投递", "笔试", "一面", "二面", "终面", "Offer"]
    return pd.DataFrame(
        {
            "阶段": stages,
            "岗位数": [
                int((data["阶段序号"] >= STAGE_RANK[stage]).sum()) for stage in stages
            ],
        }
    )


def resume_summary(data: pd.DataFrame) -> pd.DataFrame:
    if data.empty:
        return pd.DataFrame()
    grouped = data.groupby("简历版本", dropna=False).agg(
        投递数=("已投递标记", "sum"),
        面试数=("面试标记", "sum"),
        Offer数=("Offer标记", "sum"),
    ).reset_index()
    grouped["面试转化率"] = grouped.apply(
        lambda row: row["面试数"] / row["投递数"] if row["投递数"] else 0, axis=1
    )
    grouped["Offer转化率"] = grouped.apply(
        lambda row: row["Offer数"] / row["投递数"] if row["投递数"] else 0, axis=1
    )
    return grouped
