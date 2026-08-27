from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from candidate_profile import CandidateProfileData


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _canonical_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def init_profile_tables(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS candidate_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                current_version_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS candidate_profile_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                version_no INTEGER NOT NULL,
                schema_version INTEGER NOT NULL,
                data_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                confirmed_at TEXT,
                created_at TEXT NOT NULL,
                notes TEXT,
                FOREIGN KEY (profile_id) REFERENCES candidate_profiles(id),
                UNIQUE(profile_id, version_no)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS evaluation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                jd_hash TEXT NOT NULL,
                resume_hash TEXT,
                profile_version_id INTEGER,
                job_override_json TEXT NOT NULL DEFAULT '{}',
                input_snapshot_json TEXT NOT NULL,
                rubric_version TEXT NOT NULL,
                model TEXT NOT NULL,
                output_json TEXT NOT NULL,
                resume_version_id INTEGER,
                company TEXT,
                role TEXT,
                jd_text TEXT,
                fit_score INTEGER,
                gate_result TEXT,
                recommendation TEXT,
                saved_to_discovery INTEGER DEFAULT 0,
                entered_application_plan INTEGER DEFAULT 0,
                application_id INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (profile_version_id)
                    REFERENCES candidate_profile_versions(id)
            )
            """
        )
        evaluation_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(evaluation_runs)")
        }
        for column, definition in {
            "resume_version_id": "INTEGER",
            "company": "TEXT",
            "role": "TEXT",
            "jd_text": "TEXT",
            "fit_score": "INTEGER",
            "gate_result": "TEXT",
            "recommendation": "TEXT",
            "saved_to_discovery": "INTEGER DEFAULT 0",
            "entered_application_plan": "INTEGER DEFAULT 0",
            "application_id": "INTEGER",
        }.items():
            if column not in evaluation_columns:
                conn.execute(
                    f"ALTER TABLE evaluation_runs ADD COLUMN {column} {definition}"
                )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_profile_versions_profile
            ON candidate_profile_versions(profile_id, version_no DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_evaluation_runs_jd
            ON evaluation_runs(jd_hash, created_at DESC)
            """
        )


def ensure_default_profile(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM candidate_profiles WHERE is_default = 1 LIMIT 1"
        ).fetchone()
        if row:
            return int(row[0])

        now = _now()
        cursor = conn.execute(
            """
            INSERT INTO candidate_profiles (name, is_default, created_at, updated_at)
            VALUES (?, 1, ?, ?)
            """,
            ("默认求职档案", now, now),
        )
        profile_id = int(cursor.lastrowid)
        empty_profile = CandidateProfileData()
        data_json = _canonical_json(empty_profile.model_dump(mode="json"))
        content_hash = hashlib.sha256(data_json.encode("utf-8")).hexdigest()
        version_cursor = conn.execute(
            """
            INSERT INTO candidate_profile_versions (
                profile_id, version_no, schema_version, data_json,
                content_hash, confirmed_at, created_at, notes
            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile_id,
                empty_profile.schema_version,
                data_json,
                content_hash,
                None,
                now,
                "系统创建的空白档案",
            ),
        )
        version_id = int(version_cursor.lastrowid)
        conn.execute(
            """
            UPDATE candidate_profiles
            SET current_version_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (version_id, now, profile_id),
        )
        return profile_id


def load_default_profile(
    db_path: Path,
) -> tuple[int, int, int, CandidateProfileData]:
    profile_id = ensure_default_profile(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT p.current_version_id, v.version_no, v.data_json
            FROM candidate_profiles p
            JOIN candidate_profile_versions v ON v.id = p.current_version_id
            WHERE p.id = ?
            """,
            (profile_id,),
        ).fetchone()
    if not row:
        raise RuntimeError("默认求职档案缺少有效版本。")
    version_id, version_no, data_json = row
    return (
        profile_id,
        int(version_id),
        int(version_no),
        CandidateProfileData.model_validate_json(data_json),
    )


def save_profile_version(
    db_path: Path,
    profile_id: int,
    profile: CandidateProfileData,
    notes: str = "",
) -> tuple[int, int, bool]:
    data_json = _canonical_json(profile.model_dump(mode="json"))
    content_hash = hashlib.sha256(data_json.encode("utf-8")).hexdigest()
    now = _now()
    with sqlite3.connect(db_path) as conn:
        current = conn.execute(
            """
            SELECT v.id, v.version_no, v.content_hash
            FROM candidate_profiles p
            JOIN candidate_profile_versions v ON v.id = p.current_version_id
            WHERE p.id = ?
            """,
            (profile_id,),
        ).fetchone()
        if current and current[2] == content_hash:
            conn.execute(
                """
                UPDATE candidate_profile_versions
                SET confirmed_at = ?
                WHERE id = ?
                """,
                (now, int(current[0])),
            )
            return int(current[0]), int(current[1]), False

        next_version = int(current[1]) + 1 if current else 1
        cursor = conn.execute(
            """
            INSERT INTO candidate_profile_versions (
                profile_id, version_no, schema_version, data_json,
                content_hash, confirmed_at, created_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile_id,
                next_version,
                profile.schema_version,
                data_json,
                content_hash,
                now,
                now,
                notes.strip(),
            ),
        )
        version_id = int(cursor.lastrowid)
        conn.execute(
            """
            UPDATE candidate_profiles
            SET current_version_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (version_id, now, profile_id),
        )
    return version_id, next_version, True


def save_evaluation_run(
    db_path: Path,
    *,
    jd_hash: str,
    resume_hash: str,
    profile_version_id: int,
    job_override: dict[str, Any],
    input_snapshot: dict[str, Any],
    rubric_version: str,
    model: str,
    output: dict[str, Any],
    resume_version_id: int | None = None,
    company: str = "",
    role: str = "",
    jd_text: str = "",
    fit_score: int | None = None,
    gate_result: str = "",
    recommendation: str = "",
) -> int:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO evaluation_runs (
                jd_hash, resume_hash, profile_version_id, job_override_json,
                input_snapshot_json, rubric_version, model, output_json, created_at,
                resume_version_id, company, role, jd_text, fit_score,
                gate_result, recommendation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                jd_hash,
                resume_hash,
                profile_version_id,
                _canonical_json(job_override),
                _canonical_json(input_snapshot),
                rubric_version,
                model,
                _canonical_json(output),
                _now(),
                resume_version_id,
                company,
                role,
                jd_text,
                fit_score,
                gate_result,
                recommendation,
            ),
        )
        return int(cursor.lastrowid)
