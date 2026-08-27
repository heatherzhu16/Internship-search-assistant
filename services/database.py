from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from models.application import APPLICATION_STATUSES, STAGE_RANK, status_to_stage
from profile_store import init_profile_tables


APP_DIR = Path(__file__).resolve().parents[1]
DB_PATH = APP_DIR / "job_search.db"
BACKUP_DIR = APP_DIR / "data" / "backups"
SCHEMA_VERSION = 11


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(
    conn: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _backup_database() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = BACKUP_DIR / f"job_search_before_schema_v{SCHEMA_VERSION}_{stamp}.db"
    if DB_PATH.exists():
        shutil.copy2(DB_PATH, destination)
    return destination


def init_database() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                applied_date TEXT,
                company TEXT,
                company_type TEXT,
                role TEXT,
                location TEXT,
                salary TEXT,
                source TEXT,
                status TEXT,
                score INTEGER,
                recommendation TEXT,
                jd_hash TEXT UNIQUE,
                jd_text TEXT,
                evaluation_json TEXT,
                notes TEXT,
                resume_version TEXT DEFAULT '默认版',
                highest_stage TEXT DEFAULT '待投递',
                next_follow_up_date TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        current = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]

    if int(current) < SCHEMA_VERSION and DB_PATH.exists():
        _backup_database()

    init_profile_tables(DB_PATH)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _add_column(conn, "applications", "resume_version", "TEXT DEFAULT '默认版'")
        _add_column(conn, "applications", "highest_stage", "TEXT DEFAULT '待投递'")
        _add_column(conn, "applications", "next_follow_up_date", "TEXT")
        _add_column(conn, "applications", "resume_version_id", "INTEGER")
        _add_column(conn, "applications", "analysis_run_id", "INTEGER")
        _add_column(conn, "applications", "application_email", "TEXT")
        _add_column(conn, "applications", "application_reference", "TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resumes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                archived_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resume_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resume_id INTEGER NOT NULL REFERENCES resumes(id),
                version_no INTEGER NOT NULL,
                label TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                mime_type TEXT,
                sha256 TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                extracted_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                notes TEXT,
                UNIQUE(resume_id, version_no),
                UNIQUE(resume_id, sha256)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS application_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL REFERENCES applications(id),
                event_type TEXT NOT NULL,
                old_status TEXT,
                new_status TEXT NOT NULL,
                source TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                resume_version_id INTEGER REFERENCES resume_versions(id),
                external_id TEXT,
                dedupe_key TEXT NOT NULL UNIQUE,
                is_voided INTEGER NOT NULL DEFAULT 0,
                voided_at TEXT,
                void_reason TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_sync_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                address_hash TEXT NOT NULL,
                address_masked TEXT NOT NULL,
                sent_folder TEXT NOT NULL,
                incoming_folder TEXT NOT NULL,
                draft_folder TEXT NOT NULL DEFAULT '草稿箱',
                host TEXT NOT NULL DEFAULT 'imap.163.com',
                port INTEGER NOT NULL DEFAULT 993,
                max_messages INTEGER NOT NULL DEFAULT 100,
                auto_apply INTEGER NOT NULL DEFAULT 0,
                last_success_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(provider, address_hash)
            )
            """
        )
        for column, definition in {
            "host": "TEXT NOT NULL DEFAULT 'imap.163.com'",
            "port": "INTEGER NOT NULL DEFAULT 993",
            "max_messages": "INTEGER NOT NULL DEFAULT 100",
            "auto_apply": "INTEGER NOT NULL DEFAULT 0",
            "draft_folder": "TEXT NOT NULL DEFAULT '草稿箱'",
        }.items():
            _add_column(conn, "email_sync_accounts", column, definition)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES email_sync_accounts(id),
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                scanned_count INTEGER NOT NULL DEFAULT 0,
                new_count INTEGER NOT NULL DEFAULT 0,
                duplicate_count INTEGER NOT NULL DEFAULT 0,
                pending_count INTEGER NOT NULL DEFAULT 0,
                unmatched_count INTEGER NOT NULL DEFAULT 0,
                auto_event_count INTEGER NOT NULL DEFAULT 0,
                error_text TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES email_sync_accounts(id),
                folder_name TEXT NOT NULL,
                direction TEXT NOT NULL,
                uid_validity INTEGER NOT NULL,
                imap_uid INTEGER NOT NULL,
                message_id TEXT,
                received_at TEXT NOT NULL,
                from_address TEXT,
                to_addresses_json TEXT NOT NULL DEFAULT '[]',
                subject TEXT,
                body_excerpt TEXT,
                body_hash TEXT NOT NULL,
                attachment_names_json TEXT NOT NULL DEFAULT '[]',
                classification TEXT NOT NULL,
                suggested_status TEXT,
                classification_confidence REAL NOT NULL,
                matched_application_id INTEGER REFERENCES applications(id),
                match_confidence REAL NOT NULL DEFAULT 0,
                processing_state TEXT NOT NULL,
                reason_codes_json TEXT NOT NULL DEFAULT '[]',
                event_id INTEGER REFERENCES application_events(id),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(account_id, folder_name, uid_validity, imap_uid)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_match_candidates (
                email_message_id INTEGER NOT NULL REFERENCES email_messages(id),
                application_id INTEGER NOT NULL REFERENCES applications(id),
                match_score REAL NOT NULL,
                reason_codes_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                PRIMARY KEY(email_message_id, application_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_draft_exports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES email_sync_accounts(id),
                content_hash TEXT NOT NULL,
                to_address TEXT NOT NULL,
                subject TEXT NOT NULL,
                draft_folder TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(account_id, content_hash)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discovery_search_presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                keywords_json TEXT NOT NULL DEFAULT '[]',
                cities_json TEXT NOT NULL DEFAULT '[]',
                platforms_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discovery_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                keyword TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                scanned_count INTEGER NOT NULL DEFAULT 0,
                inserted_count INTEGER NOT NULL DEFAULT 0,
                duplicate_count INTEGER NOT NULL DEFAULT 0,
                filtered_count INTEGER NOT NULL DEFAULT 0,
                error_text TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discovery_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                external_id TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                canonical_url_hash TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                cross_platform_key TEXT NOT NULL,
                title TEXT,
                company TEXT,
                role TEXT,
                location TEXT,
                salary TEXT,
                posted_at TEXT,
                author TEXT,
                raw_text TEXT NOT NULL,
                source_keyword TEXT,
                snapshot_path TEXT,
                is_recruitment INTEGER NOT NULL DEFAULT 0,
                recruitment_confidence REAL NOT NULL DEFAULT 0,
                filter_reasons_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'new',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                scan_run_id INTEGER REFERENCES discovery_runs(id),
                latest_evaluation_id INTEGER,
                application_id INTEGER REFERENCES applications(id),
                UNIQUE(platform, external_id)
            )
            """
        )
        _add_column(conn, "discovery_items", "snapshot_path", "TEXT")
        _add_column(
            conn, "discovery_items", "content_level", "TEXT NOT NULL DEFAULT 'summary'"
        )
        _add_column(
            conn, "discovery_items", "completeness_score", "INTEGER NOT NULL DEFAULT 0"
        )
        _add_column(
            conn, "discovery_items", "missing_fields_json", "TEXT NOT NULL DEFAULT '[]'"
        )
        _add_column(
            conn, "discovery_items", "content_source", "TEXT NOT NULL DEFAULT 'platform'"
        )
        _add_column(conn, "discovery_items", "content_updated_at", "TEXT")
        _add_column(conn, "discovery_items", "access_url", "TEXT")
        _add_column(
            conn,
            "discovery_items",
            "capture_method",
            "TEXT NOT NULL DEFAULT 'platform_scan'",
        )
        _add_column(
            conn,
            "discovery_items",
            "manual_decision",
            "TEXT NOT NULL DEFAULT '待定'",
        )
        _add_column(
            conn,
            "discovery_items",
            "availability_status",
            "TEXT NOT NULL DEFAULT 'unknown'",
        )
        _add_column(
            conn,
            "discovery_items",
            "capture_kind",
            "TEXT NOT NULL DEFAULT 'text'",
        )
        _add_column(conn, "discovery_items", "manual_decision_reason", "TEXT DEFAULT ''")
        _add_column(conn, "discovery_items", "decision_confirmed_at", "TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discovery_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discovery_item_id INTEGER NOT NULL REFERENCES discovery_items(id),
                evaluation_run_id INTEGER NOT NULL REFERENCES evaluation_runs(id),
                resume_version_id INTEGER NOT NULL REFERENCES resume_versions(id),
                rubric_version TEXT NOT NULL,
                fit_score INTEGER NOT NULL,
                gate_result TEXT NOT NULL,
                recommendation TEXT NOT NULL,
                jd_json TEXT NOT NULL,
                eligibility_json TEXT NOT NULL,
                evaluation_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS decision_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discovery_item_id INTEGER NOT NULL REFERENCES discovery_items(id),
                evaluation_id INTEGER REFERENCES discovery_evaluations(id),
                user_decision TEXT NOT NULL,
                reason_code TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                ai_recommendation TEXT NOT NULL DEFAULT '',
                ai_score INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        evaluation_columns = _columns(conn, "evaluation_runs")
        if evaluation_columns:
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
                _add_column(conn, "evaluation_runs", column, definition)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_resume_versions_resume ON resume_versions(resume_id, version_no)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_application ON application_events(application_id, occurred_at, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_email_messages_state ON email_messages(processing_state, received_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_email_messages_hash ON email_messages(account_id, direction, body_hash)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_email_drafts_account ON email_draft_exports(account_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_evaluation_resume ON evaluation_runs(resume_version_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_discovery_items_status ON discovery_items(status, last_seen_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_discovery_items_url ON discovery_items(platform, canonical_url_hash)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_discovery_items_cross ON discovery_items(cross_platform_key)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_discovery_eval_item ON discovery_evaluations(discovery_item_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_decision_feedback_item ON decision_feedback(discovery_item_id, created_at DESC)"
        )
        conn.execute(
            """
            UPDATE discovery_items SET manual_decision = CASE manual_decision
                WHEN '想投' THEN '准备投递'
                WHEN '待定' THEN '继续了解'
                WHEN '不投' THEN '暂不投递'
                ELSE manual_decision END
            WHERE manual_decision IN ('想投', '待定', '不投')
            """
        )
        _backfill_discovery_quality(conn)
        _backfill_events(conn)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, datetime.now().isoformat(timespec="seconds")),
        )


def _backfill_discovery_quality(conn: sqlite3.Connection) -> None:
    from services.discovery_quality import assess_content_quality, extract_platform_metadata

    rows = conn.execute(
        """
        SELECT id, title, company, role, location, salary, raw_text, status
        FROM discovery_items
        """
    ).fetchall()
    for item_id, title, company, role, location, salary, raw_text, status in rows:
        fields = {
            "title": str(title or ""),
            "company": str(company or ""),
            "role": str(role or ""),
            "location": str(location or ""),
            "salary": str(salary or ""),
        }
        metadata = extract_platform_metadata(str(raw_text or ""), fields)
        quality = assess_content_quality(str(raw_text or ""), metadata)
        next_status = str(status)
        if quality.content_level != "full" and next_status in {
            "new", "scored", "hard_gate_failed"
        }:
            next_status = "needs_details"
        elif quality.content_level == "full" and next_status == "needs_details":
            next_status = "new"
        conn.execute(
            """
            UPDATE discovery_items
            SET company = COALESCE(NULLIF(?, ''), company),
                role = COALESCE(NULLIF(?, ''), role),
                location = COALESCE(NULLIF(?, ''), location),
                salary = COALESCE(NULLIF(?, ''), salary),
                content_level = ?, completeness_score = ?,
                missing_fields_json = ?, status = ?,
                content_source = COALESCE(NULLIF(content_source, ''), 'platform'),
                content_updated_at = COALESCE(content_updated_at, last_seen_at)
            WHERE id = ?
            """,
            (
                metadata.get("company", ""), metadata.get("role", ""),
                metadata.get("location", ""), metadata.get("salary", ""),
                quality.content_level, quality.completeness_score,
                quality.missing_fields_json(), next_status, int(item_id),
            ),
        )


def _backfill_events(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, created_at, COALESCE(status, '待投递'),
               COALESCE(highest_stage, '待投递')
        FROM applications
        WHERE NOT EXISTS (
            SELECT 1 FROM application_events e WHERE e.application_id = applications.id
        )
        """
    ).fetchall()
    now = datetime.now().isoformat(timespec="seconds")
    for application_id, created_at, status, highest_stage in rows:
        occurred = created_at or now
        first_status = highest_stage if highest_stage in STAGE_RANK else status
        conn.execute(
            """
            INSERT OR IGNORE INTO application_events(
                application_id, event_type, old_status, new_status, source,
                occurred_at, dedupe_key, created_at
            ) VALUES (?, '迁移初始状态', '', ?, '历史数据迁移', ?, ?, ?)
            """,
            (application_id, first_status, occurred, f"legacy:{application_id}:stage", now),
        )
        if status != first_status:
            conn.execute(
                """
                INSERT OR IGNORE INTO application_events(
                    application_id, event_type, old_status, new_status, source,
                    occurred_at, dedupe_key, created_at
                ) VALUES (?, '迁移当前状态', ?, ?, '历史数据迁移', ?, ?, ?)
                """,
                (
                    application_id,
                    first_status,
                    status,
                    occurred,
                    f"legacy:{application_id}:current",
                    now,
                ),
            )


def current_status(conn: sqlite3.Connection, application_id: int) -> str:
    row = conn.execute(
        """
        SELECT new_status FROM application_events
        WHERE application_id = ? AND is_voided = 0
        ORDER BY occurred_at DESC, id DESC LIMIT 1
        """,
        (application_id,),
    ).fetchone()
    return str(row[0]) if row else "待投递"


def highest_stage(conn: sqlite3.Connection, application_id: int) -> str:
    statuses = [
        str(row[0])
        for row in conn.execute(
            """
            SELECT new_status FROM application_events
            WHERE application_id = ? AND is_voided = 0
            """,
            (application_id,),
        )
    ]
    stages = [status_to_stage(status) for status in statuses]
    return max(stages, key=lambda item: STAGE_RANK.get(item, 0), default="待投递")


def append_application_event(
    application_id: int,
    new_status: str,
    *,
    source: str = "手动确认",
    occurred_at: str | None = None,
    event_type: str = "状态变更",
    resume_version_id: int | None = None,
    external_id: str = "",
) -> tuple[bool, str]:
    if new_status not in APPLICATION_STATUSES:
        raise ValueError("未知的投递状态。")
    occurred = occurred_at or datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        old_status = current_status(conn, application_id)
        if old_status == new_status:
            return False, "当前已经是这个状态，没有重复写入事件。"
        dedupe_source = external_id or f"{application_id}|{old_status}|{new_status}|{source}|{occurred}"
        dedupe_key = hashlib.sha256(dedupe_source.encode("utf-8")).hexdigest()
        try:
            conn.execute(
                """
                INSERT INTO application_events(
                    application_id, event_type, old_status, new_status, source,
                    occurred_at, resume_version_id, external_id, dedupe_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    application_id,
                    event_type,
                    old_status,
                    new_status,
                    source,
                    occurred,
                    resume_version_id,
                    external_id,
                    dedupe_key,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
        except sqlite3.IntegrityError:
            return False, "这条外部事件已经同步过。"
        status = current_status(conn, application_id)
        stage = highest_stage(conn, application_id)
        conn.execute(
            "UPDATE applications SET status = ?, highest_stage = ? WHERE id = ?",
            (status, stage, application_id),
        )
    return True, "状态事件已追加。"


def undo_event(event_id: int, reason: str = "人工撤销错误事件") -> tuple[bool, str]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT application_id, event_type, is_voided
            FROM application_events WHERE id = ?
            """,
            (event_id,),
        ).fetchone()
        if not row:
            return False, "事件不存在。"
        application_id, event_type, is_voided = row
        if is_voided:
            return False, "该事件已经撤销。"
        if str(event_type).startswith("迁移"):
            return False, "迁移初始事件不能撤销。"
        conn.execute(
            """
            UPDATE application_events
            SET is_voided = 1, voided_at = ?, void_reason = ?
            WHERE id = ?
            """,
            (datetime.now().isoformat(timespec="seconds"), reason, event_id),
        )
        conn.execute(
            "UPDATE applications SET status = ?, highest_stage = ? WHERE id = ?",
            (current_status(conn, application_id), highest_stage(conn, application_id), application_id),
        )
    return True, "事件已撤销，当前状态已重新计算。"


def load_applications() -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.applied_date, a.company, a.company_type, a.role,
                   a.location, a.salary, a.source, a.next_follow_up_date,
                   a.score, a.recommendation, a.notes, a.resume_version,
                   a.resume_version_id, a.application_email,
                   a.application_reference
            FROM applications a ORDER BY a.id DESC
            """
        ).fetchall()
        columns = [
            "ID", "投递日期", "公司", "公司类型", "职位", "Base地", "薪资",
            "信息来源", "下次跟进", "匹配分", "投递建议", "备注", "简历版本",
            "简历版本ID", "投递邮箱", "招聘编号",
        ]
        data = pd.DataFrame(rows, columns=columns)
        if not data.empty:
            data["投递状态"] = [
                current_status(conn, int(application_id)) for application_id in data["ID"]
            ]
            data["最高进展"] = [
                highest_stage(conn, int(application_id)) for application_id in data["ID"]
            ]
        else:
            data["投递状态"] = pd.Series(dtype="string")
            data["最高进展"] = pd.Series(dtype="string")
    ordered = [
        "ID", "投递日期", "公司", "公司类型", "职位", "Base地", "薪资",
        "信息来源", "投递邮箱", "招聘编号", "投递状态", "最高进展",
        "简历版本", "简历版本ID",
        "下次跟进", "匹配分", "投递建议", "备注",
    ]
    return data[ordered]


def update_application_fields(application_id: int, values: dict[str, Any]) -> None:
    allowed = {
        "applied_date", "company", "company_type", "role", "location", "salary",
        "source", "next_follow_up_date", "notes", "application_email",
        "application_reference",
    }
    items = [(key, value) for key, value in values.items() if key in allowed]
    if not items:
        return
    assignments = ", ".join(f"{key} = ?" for key, _ in items)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            f"UPDATE applications SET {assignments} WHERE id = ?",
            [value for _, value in items] + [application_id],
        )


def load_events(application_id: int | None = None) -> pd.DataFrame:
    query = """
        SELECT e.id AS 事件ID, e.application_id AS 岗位ID,
               a.company AS 公司, a.role AS 职位, e.event_type AS 事件,
               e.old_status AS 原状态, e.new_status AS 新状态,
               e.source AS 来源, e.occurred_at AS 时间,
               COALESCE(r.name || ' v' || rv.version_no, a.resume_version, '') AS 使用简历,
               CASE e.is_voided WHEN 1 THEN '已撤销' ELSE '有效' END AS 有效性,
               e.void_reason AS 撤销原因
        FROM application_events e
        JOIN applications a ON a.id = e.application_id
        LEFT JOIN resume_versions rv ON rv.id = e.resume_version_id
        LEFT JOIN resumes r ON r.id = rv.resume_id
    """
    params: tuple[Any, ...] = ()
    if application_id is not None:
        query += " WHERE e.application_id = ?"
        params = (application_id,)
    query += " ORDER BY e.occurred_at DESC, e.id DESC"
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query(query, conn, params=params)


def save_application(
    *,
    fields: dict[str, Any],
    jd_hash: str,
    jd_text: str,
    evaluation_json: str,
    score: int,
    recommendation: str,
    resume_version_id: int | None,
    analysis_run_id: int | None,
) -> tuple[bool, int | None]:
    now = datetime.now().isoformat(timespec="seconds")
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.execute(
                """
                INSERT INTO applications(
                    created_at, applied_date, company, company_type, role,
                    location, salary, source, status, score, recommendation,
                    jd_hash, jd_text, evaluation_json, notes, resume_version,
                    highest_stage, next_follow_up_date, resume_version_id,
                    analysis_run_id, application_email, application_reference
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now, fields.get("applied_date"), fields.get("company"),
                    fields.get("company_type"), fields.get("role"), fields.get("location"),
                    fields.get("salary"), fields.get("source"), fields.get("status", "待投递"),
                    score, recommendation, jd_hash, jd_text, evaluation_json,
                    fields.get("notes"), fields.get("resume_version", "默认版"),
                    status_to_stage(fields.get("status", "待投递")),
                    fields.get("next_follow_up_date"), resume_version_id, analysis_run_id,
                    fields.get("application_email"), fields.get("application_reference"),
                ),
            )
            application_id = int(cursor.lastrowid)
            initial_status = fields.get("status", "待投递")
            dedupe = hashlib.sha256(f"created:{application_id}".encode()).hexdigest()
            conn.execute(
                """
                INSERT INTO application_events(
                    application_id, event_type, old_status, new_status, source,
                    occurred_at, resume_version_id, dedupe_key, created_at
                ) VALUES (?, '创建投递计划', '', ?, '手动确认', ?, ?, ?, ?)
                """,
                (application_id, initial_status, now, resume_version_id, dedupe, now),
            )
            if analysis_run_id:
                conn.execute(
                    """
                    UPDATE evaluation_runs
                    SET entered_application_plan = 1, application_id = ?
                    WHERE id = ?
                    """,
                    (application_id, analysis_run_id),
                )
        return True, application_id
    except sqlite3.IntegrityError:
        return False, None


def database_counts() -> dict[str, int]:
    with sqlite3.connect(DB_PATH) as conn:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ["applications", "application_events", "resumes", "resume_versions"]
        }
