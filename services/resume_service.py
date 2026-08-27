from __future__ import annotations

import hashlib
import io
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd
from docx import Document
from pypdf import PdfReader

from models.resume import ResumeVersion
from services.database import APP_DIR, DB_PATH


RESUME_DIR = APP_DIR / "data" / "resumes"


def extract_resume_text(filename: str, raw: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if suffix == ".docx":
        document = Document(io.BytesIO(raw))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)
    if suffix == ".txt":
        return raw.decode("utf-8", errors="ignore")
    raise ValueError("只支持 PDF、DOCX 或 TXT 简历。")


def save_resume_version(
    *,
    resume_name: str,
    filename: str,
    mime_type: str,
    raw: bytes,
    notes: str = "",
    set_default: bool = False,
) -> tuple[ResumeVersion, bool]:
    name = resume_name.strip()
    if not name:
        raise ValueError("请填写简历名称，例如“产品实习版”。")
    text = extract_resume_text(filename, raw)
    if len(text.strip()) < 30:
        raise ValueError("简历提取到的文字过少，请确认文件不是纯扫描图片。")
    digest = hashlib.sha256(raw).hexdigest()
    now = datetime.now().isoformat(timespec="seconds")
    RESUME_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix.lower()
    storage_path = RESUME_DIR / f"{digest}{suffix}"
    try:
        stored_path = str(storage_path.relative_to(APP_DIR))
    except ValueError:
        stored_path = str(storage_path)

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT id FROM resumes WHERE name = ? AND archived_at IS NULL",
            (name,),
        ).fetchone()
        if row:
            resume_id = int(row[0])
        else:
            cursor = conn.execute(
                """
                INSERT INTO resumes(name, is_default, created_at, updated_at)
                VALUES (?, 0, ?, ?)
                """,
                (name, now, now),
            )
            resume_id = int(cursor.lastrowid)
        duplicate = conn.execute(
            "SELECT id FROM resume_versions WHERE resume_id = ? AND sha256 = ?",
            (resume_id, digest),
        ).fetchone()
        if duplicate:
            version_id = int(duplicate[0])
            created = False
        else:
            version_no = int(
                conn.execute(
                    "SELECT COALESCE(MAX(version_no), 0) + 1 FROM resume_versions WHERE resume_id = ?",
                    (resume_id,),
                ).fetchone()[0]
            )
            if not storage_path.exists():
                storage_path.write_bytes(raw)
            cursor = conn.execute(
                """
                INSERT INTO resume_versions(
                    resume_id, version_no, label, original_filename, mime_type,
                    sha256, storage_path, extracted_text, created_at, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resume_id, version_no, f"{name} v{version_no}", filename,
                    mime_type, digest, stored_path, text,
                    now, notes.strip(),
                ),
            )
            version_id = int(cursor.lastrowid)
            conn.execute(
                "UPDATE resumes SET updated_at = ? WHERE id = ?",
                (now, resume_id),
            )
            created = True
        if set_default or not conn.execute(
            "SELECT 1 FROM resumes WHERE is_default = 1 AND archived_at IS NULL"
        ).fetchone():
            conn.execute("UPDATE resumes SET is_default = 0")
            conn.execute("UPDATE resumes SET is_default = 1 WHERE id = ?", (resume_id,))
    return get_resume_version(version_id), created


def get_resume_version(version_id: int) -> ResumeVersion:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT rv.id, rv.resume_id, r.name, rv.version_no, rv.label,
                   rv.original_filename, COALESCE(rv.mime_type, ''), rv.sha256,
                   rv.storage_path, rv.extracted_text, rv.created_at,
                   COALESCE(rv.notes, ''), r.is_default
            FROM resume_versions rv
            JOIN resumes r ON r.id = rv.resume_id
            WHERE rv.id = ?
            """,
            (version_id,),
        ).fetchone()
    if not row:
        raise ValueError("找不到这个简历版本。")
    return ResumeVersion(
        id=int(row[0]), resume_id=int(row[1]), resume_name=str(row[2]),
        version_no=int(row[3]), label=str(row[4]), original_filename=str(row[5]),
        mime_type=str(row[6]), sha256=str(row[7]), storage_path=str(row[8]),
        extracted_text=str(row[9]), created_at=str(row[10]), notes=str(row[11]),
        is_default=bool(row[12]),
    )


def list_resume_versions(include_archived: bool = False) -> pd.DataFrame:
    where = "" if include_archived else "WHERE r.archived_at IS NULL"
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query(
            f"""
            SELECT rv.id AS 版本ID, r.name AS 简历名称, rv.version_no AS 版本,
                   rv.label AS 完整名称, rv.original_filename AS 原文件,
                   rv.created_at AS 创建时间, rv.notes AS 备注,
                   CASE r.is_default WHEN 1 THEN '是' ELSE '否' END AS 默认简历
            FROM resume_versions rv JOIN resumes r ON r.id = rv.resume_id
            {where}
            ORDER BY r.is_default DESC, r.updated_at DESC, rv.version_no DESC
            """,
            conn,
        )


def latest_versions() -> list[ResumeVersion]:
    with sqlite3.connect(DB_PATH) as conn:
        ids = [
            int(row[0])
            for row in conn.execute(
                """
                SELECT rv.id
                FROM resume_versions rv JOIN resumes r ON r.id = rv.resume_id
                WHERE r.archived_at IS NULL
                  AND rv.version_no = (
                    SELECT MAX(v2.version_no) FROM resume_versions v2
                    WHERE v2.resume_id = rv.resume_id
                  )
                ORDER BY r.is_default DESC, r.updated_at DESC
                """
            )
        ]
    return [get_resume_version(version_id) for version_id in ids]


def default_resume_version() -> ResumeVersion | None:
    versions = latest_versions()
    return next((item for item in versions if item.is_default), versions[0] if versions else None)


def set_default_resume(resume_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE resumes SET is_default = 0")
        conn.execute(
            "UPDATE resumes SET is_default = 1, updated_at = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), resume_id),
        )


def archive_resume(resume_id: int) -> None:
    """Hide a resume without deleting versions or breaking historical references."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE resumes SET archived_at = ?, is_default = 0 WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), resume_id),
        )
