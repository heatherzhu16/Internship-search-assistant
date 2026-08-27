from __future__ import annotations

import json
import sqlite3

import pandas as pd

from services.database import DB_PATH


def list_analysis_history() -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query(
            """
            SELECT er.id AS 分析ID, er.created_at AS 分析时间,
                   COALESCE(er.company, '') AS 公司,
                   COALESCE(er.role, '') AS 职位,
                   COALESCE(r.name || ' v' || rv.version_no, '历史会话上传') AS 使用简历,
                   er.rubric_version AS 评分版本,
                   er.model AS 模型,
                   er.fit_score AS 得分,
                   er.gate_result AS 资格门槛,
                   er.recommendation AS 最终建议,
                   CASE COALESCE(er.saved_to_discovery, 0) WHEN 1 THEN '是' ELSE '否' END AS 岗位发现箱,
                   CASE COALESCE(er.entered_application_plan, 0) WHEN 1 THEN '是' ELSE '否' END AS 投递计划
            FROM evaluation_runs er
            LEFT JOIN resume_versions rv ON rv.id = er.resume_version_id
            LEFT JOIN resumes r ON r.id = rv.resume_id
            ORDER BY er.created_at DESC, er.id DESC
            """,
            conn,
        )


def get_analysis_detail(run_id: int) -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT er.created_at, er.company, er.role, er.jd_text,
                   er.rubric_version, er.model, er.fit_score, er.gate_result,
                   er.recommendation, er.input_snapshot_json, er.output_json,
                   rv.extracted_text, COALESCE(r.name || ' v' || rv.version_no, '历史会话上传')
            FROM evaluation_runs er
            LEFT JOIN resume_versions rv ON rv.id = er.resume_version_id
            LEFT JOIN resumes r ON r.id = rv.resume_id
            WHERE er.id = ?
            """,
            (run_id,),
        ).fetchone()
    if not row:
        raise ValueError("找不到这次分析。")
    return {
        "created_at": row[0],
        "company": row[1] or "",
        "role": row[2] or "",
        "jd_text": row[3] or "",
        "rubric_version": row[4],
        "model": row[5],
        "fit_score": row[6],
        "gate_result": row[7] or "",
        "recommendation": row[8] or "",
        "input_snapshot": json.loads(row[9]),
        "output": json.loads(row[10]),
        "resume_text": row[11] or "",
        "resume_label": row[12],
    }


def set_discovery_flag(run_id: int, enabled: bool) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE evaluation_runs SET saved_to_discovery = ? WHERE id = ?",
            (int(enabled), run_id),
        )
