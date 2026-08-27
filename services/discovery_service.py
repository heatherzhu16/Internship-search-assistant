from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pandas as pd

from candidate_profile import CandidateProfileData
from models.discovery import (
    CollectedItem,
    CollectorResult,
    DiscoverySaveResult,
    PLATFORM_LABELS,
)
from models.evaluation import RUBRIC_VERSION
from services.database import APP_DIR, DB_PATH, save_application
from services.discovery_quality import (
    CONTENT_LEVEL_LABELS,
    assess_content_quality,
    extract_platform_metadata,
    extract_xiaohongshu_metadata,
)
from services.publication_date import normalize_posted_at, publication_recency_label


RECRUITMENT_TERMS = [
    "招聘", "招募", "内推", "急招", "岗位", "职位", "实习生", "校招",
    "社招", "投递", "简历", "任职要求", "岗位职责", "job description",
]
JOB_CONTEXT_TERMS = [
    "产品", "运营", "设计", "开发", "算法", "数据", "市场", "商务",
    "人力", "hr", "薪资", "base", "到岗", "每周", "实习",
]
NON_RECRUITMENT_TERMS = [
    "面试复盘", "求职经验", "避雷", "上岸经验", "简历修改经验",
    "面经分享", "职场吐槽",
]

STATUS_LABELS = {
    "new": "新发现",
    "needs_review": "待确认",
    "needs_details": "待补全",
    "not_recruitment": "非招聘内容",
    "hard_gate_failed": "硬性条件不符",
    "scored": "已评分",
    "shortlisted": "已收藏",
    "dismissed": "已移出",
    "imported": "已加入投递计划",
    "collection_failed": "采集失败",
    "expired": "已失效",
}

PRIVATE_GLYPH_PATTERN = re.compile(
    r"[\uE000-\uF8FF\uFFFD\U000F0000-\U000FFFFD\U00100000-\U0010FFFD]"
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sanitize_platform_text(value: str, *, preserve_newlines: bool = True) -> str:
    """Hide private web-font glyphs that cannot be rendered outside the source site."""
    text = unicodedata.normalize("NFC", str(value or ""))
    text = PRIVATE_GLYPH_PATTERN.sub("", text)
    text = re.sub(r"[\u200B-\u200D\u2060\uFEFF]", "", text)
    text = re.sub(r"[（(]\s*[）)]", "", text)
    if preserve_newlines:
        lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line).strip()
    return re.sub(r"\s+", " ", text).strip()


def _normalized_text(value: str) -> str:
    clean = sanitize_platform_text(value, preserve_newlines=False)
    return re.sub(r"[\W_]+", "", clean.casefold(), flags=re.UNICODE)


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    safe_query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=False)
        if key.casefold() in {"id", "securityid"}
    ]
    return urlunsplit(
        (
            parts.scheme.casefold(),
            parts.netloc.casefold(),
            parts.path.rstrip("/"),
            urlencode(safe_query),
            "",
        )
    )


def classify_recruitment(item: CollectedItem) -> tuple[bool, float, list[str]]:
    if item.platform in {"boss", "shixiseng"}:
        return True, 0.99, ["structured_job_platform"]
    text = sanitize_platform_text(
        f"{item.title}\n{item.raw_text}"
    ).casefold()
    strong = [term for term in RECRUITMENT_TERMS if term.casefold() in text]
    context = [term for term in JOB_CONTEXT_TERMS if term.casefold() in text]
    negative = [term for term in NON_RECRUITMENT_TERMS if term.casefold() in text]
    score = min(0.98, 0.18 + 0.16 * len(strong) + 0.06 * len(context))
    if negative and len(strong) < 2:
        score = max(0.05, score - 0.35)
    is_recruitment = bool(
        score >= 0.58
        or any(term in text for term in ["招聘", "内推", "急招", "岗位职责"])
    )
    reasons = [f"招聘词:{term}" for term in strong[:5]]
    reasons.extend(f"岗位语境:{term}" for term in context[:3])
    reasons.extend(f"非招聘语境:{term}" for term in negative[:2])
    if item.snapshot_path and not item.raw_text.strip():
        return False, 0.35, ["image_only_needs_review"]
    return is_recruitment, score, reasons or ["no_recruitment_signal"]


def generate_keywords(profile: CandidateProfileData) -> list[str]:
    roles = profile.preferences.target_roles or ["产品实习"]
    cities = profile.preferences.preferred_cities or [""]
    output: list[str] = []
    for role in roles[:5]:
        clean_role = role.strip()
        if not clean_role:
            continue
        internship_role = clean_role if "实习" in clean_role else f"{clean_role} 实习"
        for city in cities[:3]:
            phrase = " ".join(part for part in [internship_role, city.strip()] if part)
            if phrase not in output:
                output.append(phrase)
        referral = f"{internship_role} 内推"
        if referral not in output:
            output.append(referral)
    return output[:12]


def company_preference(
    company: str,
    profile: CandidateProfileData,
) -> tuple[bool, str]:
    name = _normalized_text(company)
    preferences = profile.preferences
    excluded = [
        entry for entry in preferences.excluded_companies if _normalized_text(entry)
    ]
    if any(
        _normalized_text(entry) in name or name in _normalized_text(entry)
        for entry in excluded
        if name
    ):
        return False, "已排除公司"

    targets = [entry for entry in preferences.target_companies if _normalized_text(entry)]
    if preferences.company_filter_mode == "仅目标公司":
        if not name:
            return False, "公司待确认"
        if not any(
            _normalized_text(entry) in name or name in _normalized_text(entry)
            for entry in targets
        ):
            return False, "不在目标公司名单"
    return True, "符合"


def save_search_preset(
    name: str,
    keywords: list[str],
    cities: list[str],
    platforms: list[str],
) -> None:
    now = _now()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO discovery_search_presets(
                name, keywords_json, cities_json, platforms_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                keywords_json = excluded.keywords_json,
                cities_json = excluded.cities_json,
                platforms_json = excluded.platforms_json,
                updated_at = excluded.updated_at
            """,
            (
                name.strip() or "默认搜索方案",
                json.dumps(keywords, ensure_ascii=False),
                json.dumps(cities, ensure_ascii=False),
                json.dumps(platforms, ensure_ascii=False),
                now,
                now,
            ),
        )


def load_search_presets() -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query(
            """
            SELECT id AS ID, name AS 名称, keywords_json AS 关键词,
                   cities_json AS 城市, platforms_json AS 平台,
                   updated_at AS 更新时间
            FROM discovery_search_presets ORDER BY updated_at DESC
            """,
            conn,
        )


def start_discovery_run(platform: str, keyword: str) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO discovery_runs(platform, keyword, started_at, status)
            VALUES (?, ?, ?, 'running')
            """,
            (platform, keyword, _now()),
        )
        return int(cursor.lastrowid)


def _fingerprints(item: CollectedItem) -> tuple[str, str, str, str]:
    canonical_url = canonicalize_url(item.url)
    url_hash = hashlib.sha256(canonical_url.encode()).hexdigest()
    content_basis = _normalized_text(
        f"{item.title}\n{item.company}\n{item.role}\n{item.raw_text[:8000]}"
    )
    content_hash = hashlib.sha256(content_basis.encode()).hexdigest()
    cross_basis = _normalized_text(
        f"{item.company}|{item.role}|{item.location}"
    )
    if len(cross_basis) < 6:
        cross_basis = _normalized_text(f"{item.title}|{item.raw_text[:500]}")
    cross_key = hashlib.sha256(cross_basis.encode()).hexdigest()
    return canonical_url, url_hash, content_hash, cross_key


def _enrich_collected_item(item: CollectedItem) -> CollectedItem:
    fields = {
        "title": item.title,
        "company": item.company,
        "role": item.role,
        "location": item.location,
        "salary": item.salary,
    }
    if item.platform == "xiaohongshu":
        fields = extract_xiaohongshu_metadata(item.raw_text, fields)
    metadata = extract_platform_metadata(
        item.raw_text,
        fields,
    )
    return item.model_copy(
        update={
            "title": metadata.get("title", item.title),
            "company": metadata.get("company", ""),
            "role": metadata.get("role", ""),
            "location": metadata.get("location", ""),
            "salary": metadata.get("salary", ""),
            "posted_at": normalize_posted_at(item.posted_at),
        }
    )


def complete_discovery_run(
    run_id: int,
    result: CollectorResult,
) -> DiscoverySaveResult:
    summary = DiscoverySaveResult(
        run_id=run_id,
        scanned=result.prefiltered_count,
        filtered_items=result.prefiltered_count,
        errors=[entry for entry in [result.error, *result.warnings] if entry],
    )
    now = _now()
    with sqlite3.connect(DB_PATH) as conn:
        for collected_item in result.items:
            summary.scanned += 1
            item = _enrich_collected_item(collected_item)
            quality = assess_content_quality(
                item.raw_text,
                {
                    "title": item.title,
                    "company": item.company,
                    "role": item.role,
                    "location": item.location,
                    "salary": item.salary,
                },
            )
            canonical_url, url_hash, content_hash, cross_key = _fingerprints(item)
            duplicate = conn.execute(
                """
                SELECT id, latest_evaluation_id, status, is_recruitment,
                       COALESCE(raw_text, ''), COALESCE(completeness_score, 0),
                       COALESCE(content_level, 'summary')
                FROM discovery_items
                WHERE (platform = ? AND external_id = ?)
                   OR (platform = ? AND canonical_url_hash = ?)
                LIMIT 1
                """,
                (item.platform, item.external_id, item.platform, url_hash),
            ).fetchone()
            if duplicate:
                old_raw = str(duplicate[4] or "")
                richer = (
                    quality.completeness_score > int(duplicate[5] or 0)
                    or (
                        quality.content_level == "full"
                        and str(duplicate[6]) != "full"
                    )
                    or len(item.raw_text) > len(old_raw) + 100
                )
                invalidated = richer and _normalized_text(item.raw_text) != _normalized_text(old_raw)
                if richer:
                    next_status = str(duplicate[2])
                    if next_status not in {"dismissed", "imported", "shortlisted"}:
                        next_status = "new" if quality.scorable else "needs_details"
                    conn.execute(
                        """
                        UPDATE discovery_items
                        SET canonical_url = ?, canonical_url_hash = ?, content_hash = ?,
                            cross_platform_key = ?,
                            access_url = COALESCE(NULLIF(?, ''), access_url),
                            availability_status = COALESCE(NULLIF(?, ''), availability_status),
                            capture_kind = COALESCE(NULLIF(?, ''), capture_kind),
                            title = COALESCE(NULLIF(?, ''), title),
                            company = COALESCE(NULLIF(?, ''), company),
                            role = COALESCE(NULLIF(?, ''), role),
                            location = COALESCE(NULLIF(?, ''), location),
                            salary = COALESCE(NULLIF(?, ''), salary),
                            posted_at = COALESCE(NULLIF(?, ''), posted_at),
                            raw_text = ?, source_keyword = COALESCE(NULLIF(?, ''), source_keyword),
                            snapshot_path = COALESCE(NULLIF(?, ''), snapshot_path),
                            content_level = ?, completeness_score = ?,
                            missing_fields_json = ?, content_source = ?,
                            capture_method = COALESCE(NULLIF(?, ''), capture_method),
                            manual_decision = CASE
                                WHEN ? IN ('想投', '待定', '不投') THEN ?
                                ELSE manual_decision
                            END,
                            content_updated_at = ?, status = ?,
                            latest_evaluation_id = CASE WHEN ? THEN NULL ELSE latest_evaluation_id END,
                            last_seen_at = ?, scan_run_id = ?
                        WHERE id = ?
                        """,
                        (
                            canonical_url, url_hash, content_hash, cross_key,
                            item.access_url or item.url,
                            item.availability_status, item.capture_kind,
                            item.title, item.company, item.role, item.location,
                            item.salary, item.posted_at, item.raw_text, item.source_keyword,
                            item.snapshot_path, quality.content_level,
                            quality.completeness_score, quality.missing_fields_json(),
                            item.capture_method, item.capture_method,
                            item.manual_decision, item.manual_decision,
                            now, next_status, int(invalidated), now, run_id,
                            int(duplicate[0]),
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE discovery_items
                        SET last_seen_at = ?,
                            source_keyword = COALESCE(NULLIF(source_keyword, ''), ?),
                            access_url = COALESCE(NULLIF(?, ''), access_url),
                            capture_method = COALESCE(NULLIF(?, ''), capture_method),
                            availability_status = COALESCE(NULLIF(?, ''), availability_status),
                            capture_kind = COALESCE(NULLIF(?, ''), capture_kind),
                            posted_at = COALESCE(NULLIF(?, ''), posted_at),
                            manual_decision = CASE
                                WHEN ? IN ('想投', '待定', '不投') THEN ?
                                ELSE manual_decision
                            END,
                            scan_run_id = ?
                        WHERE id = ?
                        """,
                        (
                            now, item.source_keyword, item.access_url or item.url,
                            item.capture_method, item.availability_status,
                            item.capture_kind, item.posted_at, item.manual_decision,
                            item.manual_decision, run_id, int(duplicate[0]),
                        ),
                    )
                summary.duplicates += 1
                effective_scorable = (
                    quality.scorable if richer else str(duplicate[6]) == "full"
                )
                if (
                    (duplicate[1] is None or invalidated)
                    and str(duplicate[2]) not in {"dismissed", "imported"}
                    and bool(duplicate[3])
                    and effective_scorable
                ):
                    summary.score_candidate_ids.append(int(duplicate[0]))
                continue
            is_recruitment, confidence, reasons = classify_recruitment(item)
            status = "new" if is_recruitment else "not_recruitment"
            if item.snapshot_path and confidence < 0.58:
                status = "needs_review"
            elif is_recruitment and not quality.scorable:
                status = "needs_details"
            cursor = conn.execute(
                """
                INSERT INTO discovery_items(
                    platform, external_id, canonical_url, canonical_url_hash,
                    content_hash, cross_platform_key, access_url, title, company, role,
                    location, salary, posted_at, author, raw_text,
                    source_keyword, snapshot_path, is_recruitment,
                    recruitment_confidence, filter_reasons_json, status,
                    content_level, completeness_score, missing_fields_json,
                    content_source, content_updated_at, capture_method, manual_decision,
                    availability_status, capture_kind,
                    first_seen_at, last_seen_at, scan_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.platform, item.external_id, canonical_url, url_hash,
                    content_hash, cross_key, item.access_url or item.url,
                    item.title, item.company, item.role,
                    item.location, item.salary, item.posted_at, item.author,
                    item.raw_text, item.source_keyword, item.snapshot_path,
                    int(is_recruitment), confidence,
                    json.dumps(reasons, ensure_ascii=False), status,
                    quality.content_level, quality.completeness_score,
                    quality.missing_fields_json(), item.capture_method, now,
                    item.capture_method, item.manual_decision or "待定",
                    item.availability_status or "unknown",
                    item.capture_kind or "text",
                    now, now, run_id,
                ),
            )
            item_id = int(cursor.lastrowid)
            summary.inserted += 1
            summary.new_item_ids.append(item_id)
            if is_recruitment:
                summary.recruitment_items += 1
                if quality.scorable:
                    summary.score_candidate_ids.append(item_id)
                else:
                    summary.incomplete_items += 1
            else:
                summary.filtered_items += 1
        status = "failed" if result.error and not result.items else (
            "completed_with_warnings" if result.error or result.warnings else "completed"
        )
        error_text = "\n".join(
            [entry for entry in [result.error, *result.warnings] if entry]
        )
        conn.execute(
            """
            UPDATE discovery_runs
            SET finished_at = ?, status = ?, scanned_count = ?,
                inserted_count = ?, duplicate_count = ?, filtered_count = ?,
                error_text = ?
            WHERE id = ?
            """,
            (
                now, status, summary.scanned, summary.inserted,
                summary.duplicates, summary.filtered_items, error_text, run_id,
            ),
        )
    return summary


def load_discovery_items(
    statuses: list[str] | None = None,
    platforms: list[str] | None = None,
) -> pd.DataFrame:
    query = """
        SELECT d.id AS 线索ID,
               COALESCE(NULLIF(d.company, ''), '待提取') AS 公司,
               COALESCE(NULLIF(d.role, ''), NULLIF(d.title, ''), '待提取') AS 职位,
               d.location AS 城市, d.salary AS 薪资,
               d.posted_at AS 发布日期,
               d.platform AS 平台代码,
               COALESCE(NULLIF(d.access_url, ''), d.canonical_url) AS 原始链接,
               d.capture_method AS 收录方式, d.manual_decision AS 我的判断,
               d.availability_status AS 有效性代码,
               d.capture_kind AS 内容形态代码,
               d.source_keyword AS 搜索关键词,
               d.recruitment_confidence AS 招聘置信度,
               d.content_level AS 内容级别代码,
               d.completeness_score AS 信息完整度,
               d.missing_fields_json AS 缺失字段,
               d.status AS 状态代码, d.last_seen_at AS 发现时间,
               CASE WHEN d.content_level = 'full' AND e.rubric_version = ?
                    THEN e.fit_score END AS 匹配分,
               CASE WHEN d.content_level = 'full' AND e.rubric_version = ?
                    THEN e.gate_result END AS 硬性条件,
               CASE WHEN d.content_level = 'full' AND e.rubric_version = ?
                    THEN e.recommendation END AS 投递建议,
               e.rubric_version AS 评分版本,
               d.cross_platform_key AS 跨平台组,
               (SELECT COUNT(*) FROM discovery_items peer
                WHERE peer.cross_platform_key = d.cross_platform_key) AS 同岗位来源数,
               d.application_id AS 台账岗位ID
        FROM discovery_items d
        LEFT JOIN discovery_evaluations e ON e.id = d.latest_evaluation_id
    """
    clauses: list[str] = []
    params: list[str] = [RUBRIC_VERSION, RUBRIC_VERSION, RUBRIC_VERSION]
    if statuses:
        clauses.append("d.status IN (" + ",".join("?" for _ in statuses) + ")")
        params.extend(statuses)
    if platforms:
        clauses.append("d.platform IN (" + ",".join("?" for _ in platforms) + ")")
        params.extend(platforms)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += """
        ORDER BY
          CASE d.status
            WHEN 'new' THEN 0
            WHEN 'needs_review' THEN 0
            WHEN 'needs_details' THEN 0
            ELSE 1
          END,
          CASE WHEN d.status IN ('new', 'needs_review', 'needs_details')
               THEN d.last_seen_at END DESC,
          CASE COALESCE(e.gate_result, '')
            WHEN '满足' THEN 0
            WHEN '未识别到明确门槛' THEN 1
            WHEN '可能协商' THEN 2
            WHEN '存疑' THEN 3
            WHEN '不满足' THEN 5
            ELSE 4
          END,
          CASE WHEN COALESCE(d.posted_at, '') = '' THEN 1 ELSE 0 END,
          d.posted_at DESC,
          COALESCE(e.fit_score, -1) DESC,
          d.last_seen_at DESC
    """
    with sqlite3.connect(DB_PATH) as conn:
        data = pd.read_sql_query(query, conn, params=params)
    if data.empty:
        data["平台"] = pd.Series(dtype="string")
        data["状态"] = pd.Series(dtype="string")
        return data
    data["平台"] = data["平台代码"].map(PLATFORM_LABELS).fillna(data["平台代码"])
    data["状态"] = data["状态代码"].map(STATUS_LABELS).fillna(data["状态代码"])
    stale = (
        data["内容级别代码"].eq("full")
        & data["评分版本"].fillna("").ne(RUBRIC_VERSION)
        & data["状态代码"].isin(["scored", "hard_gate_failed", "shortlisted"])
    )
    data.loc[stale, "状态"] = "待重新评分"
    data["数据状态"] = (
        data["内容级别代码"].map(CONTENT_LEVEL_LABELS).fillna("详情不完整")
    )
    data["岗位有效性"] = data["有效性代码"].map(
        {"active": "招聘中", "expired": "已失效", "unknown": "待确认"}
    ).fillna("待确认")
    data["内容形态"] = data["内容形态代码"].map(
        {"text": "文字 JD", "mixed": "图文 JD", "image": "图片 JD"}
    ).fillna("文字 JD")
    data["发布提示"] = data["发布日期"].map(publication_recency_label)
    for column in ("公司", "职位", "城市", "薪资"):
        data[column] = data[column].map(
            lambda value: sanitize_platform_text(value, preserve_newlines=False)
        )
    data["公司"] = data["公司"].replace("", "待提取")
    data["职位"] = data["职位"].replace("", "待提取")
    return data


def filter_discovery_items_by_text(
    items: pd.DataFrame,
    query: str,
) -> pd.DataFrame:
    """Filter the discovery landing table without failing on an empty result set."""
    clean_query = str(query or "").strip()
    if items.empty or not clean_query:
        return items.copy()
    searchable = (
        items.reindex(columns=["公司", "职位", "城市", "搜索关键词"], fill_value="")
        .fillna("")
        .astype(str)
        .agg(" ".join, axis=1)
    )
    return items[searchable.str.contains(clean_query, case=False, regex=False)].copy()


def get_discovery_item(item_id: int) -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT d.*, e.jd_json, e.eligibility_json, e.evaluation_json,
                   e.fit_score, e.gate_result, e.recommendation,
                   e.resume_version_id, e.evaluation_run_id, e.rubric_version
            FROM discovery_items d
            LEFT JOIN discovery_evaluations e ON e.id = d.latest_evaluation_id
            WHERE d.id = ?
            """,
            (item_id,),
        ).fetchone()
    if not row:
        raise ValueError("找不到这条岗位线索。")
    result = dict(row)
    raw_fields = ("title", "company", "role", "location", "salary", "raw_text")
    result["private_glyph_count"] = sum(
        len(PRIVATE_GLYPH_PATTERN.findall(str(result.get(field) or "")))
        for field in raw_fields
    )
    for field in raw_fields:
        result[field] = sanitize_platform_text(
            str(result.get(field) or ""),
            preserve_newlines=field == "raw_text",
        )
    return result


def find_discovery_item_id(platform: str, external_id: str) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT id FROM discovery_items WHERE platform = ? AND external_id = ?",
            (platform, external_id),
        ).fetchone()
    if not row:
        raise ValueError("收录完成后未找到对应岗位。")
    return int(row[0])


def list_unscored_discovery_item_ids(limit: int = 50) -> list[int]:
    safe_limit = max(1, min(int(limit), 100))
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT id
            FROM discovery_items
            WHERE is_recruitment = 1
              AND content_level = 'full'
              AND COALESCE(availability_status, 'unknown') != 'expired'
              AND COALESCE(capture_kind, 'text') != 'image'
              AND status NOT IN ('dismissed', 'imported')
              AND (
                  latest_evaluation_id IS NULL
                  OR NOT EXISTS (
                      SELECT 1 FROM discovery_evaluations latest
                      WHERE latest.id = discovery_items.latest_evaluation_id
                        AND latest.rubric_version = ?
                  )
              )
            ORDER BY last_seen_at DESC, id DESC
            LIMIT ?
            """,
            (RUBRIC_VERSION, safe_limit),
        ).fetchall()
    return [int(row[0]) for row in rows]


def update_discovery_status(item_id: int, status: str) -> None:
    allowed = set(STATUS_LABELS)
    if status not in allowed:
        raise ValueError("未知的岗位发现状态。")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE discovery_items SET status = ?, last_seen_at = ? WHERE id = ?",
            (status, _now(), item_id),
        )


def update_manual_decision(
    item_id: int,
    decision: str,
    *,
    reason: str = "",
    notes: str = "",
) -> None:
    aliases = {"想投": "准备投递", "待定": "继续了解", "不投": "暂不投递"}
    normalized = aliases.get(decision, decision)
    allowed = {"准备投递", "继续了解", "暂不投递", "信息待补全"}
    if normalized not in allowed:
        raise ValueError("请选择准备投递、继续了解、暂不投递或信息待补全。")
    now = _now()
    with sqlite3.connect(DB_PATH) as conn:
        current = conn.execute(
            """
            SELECT latest_evaluation_id, COALESCE(e.recommendation, ''), e.fit_score
            FROM discovery_items d
            LEFT JOIN discovery_evaluations e ON e.id = d.latest_evaluation_id
            WHERE d.id = ?
            """,
            (item_id,),
        ).fetchone()
        if not current:
            raise ValueError("找不到这条岗位线索。")
        conn.execute(
            """
            UPDATE discovery_items
            SET manual_decision = ?, manual_decision_reason = ?,
                decision_confirmed_at = ?, last_seen_at = ?
            WHERE id = ?
            """,
            (normalized, reason.strip(), now, now, item_id),
        )
        conn.execute(
            """
            INSERT INTO decision_feedback(
                discovery_item_id, evaluation_id, user_decision, reason_code,
                notes, ai_recommendation, ai_score, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id, current[0], normalized, reason.strip(), notes.strip(),
                str(current[1] or ""), current[2], now,
            ),
        )


def load_decision_feedback(item_id: int) -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query(
            """
            SELECT created_at AS 时间, user_decision AS 我的决定,
                   reason_code AS 原因, notes AS 备注,
                   ai_recommendation AS 当时AI建议, ai_score AS 当时参考分
            FROM decision_feedback
            WHERE discovery_item_id = ?
            ORDER BY id DESC
            """,
            conn,
            params=(item_id,),
        )


def apply_capture_quality_flags(
    item_id: int,
    *,
    availability_status: str,
    capture_kind: str,
) -> None:
    if availability_status not in {"active", "expired", "unknown"}:
        raise ValueError("未知的岗位有效性状态。")
    if capture_kind not in {"text", "mixed", "image"}:
        raise ValueError("未知的岗位内容形态。")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE discovery_items
            SET availability_status = ?, capture_kind = ?,
                status = CASE
                    WHEN ? = 'expired' AND status != 'imported' THEN 'expired'
                    WHEN ? != 'expired' AND status = 'expired'
                        THEN CASE
                            WHEN content_level = 'full' THEN 'new'
                            ELSE 'needs_details'
                        END
                    WHEN ? = 'image'
                         AND status NOT IN ('imported', 'shortlisted', 'dismissed')
                        THEN 'needs_details'
                    ELSE status
                END,
                last_seen_at = ?
            WHERE id = ?
            """,
            (
                availability_status, capture_kind, availability_status,
                availability_status, capture_kind, _now(), item_id,
            ),
        )


def update_discovery_content(
    item_id: int,
    *,
    raw_text: str,
    title: str = "",
    company: str = "",
    role: str = "",
    location: str = "",
    salary: str = "",
    posted_at: str = "",
    content_source: str = "manual",
) -> dict:
    """Replace an incomplete discovery record with user-confirmed detail text."""
    detail = get_discovery_item(item_id)
    if detail.get("status") == "imported":
        raise ValueError("已进入投递计划的岗位不能在发现箱中改写 JD。")
    clean_raw = sanitize_platform_text(raw_text).strip()
    if len(clean_raw) < 80:
        raise ValueError("完整 JD 文字过少，请粘贴岗位职责和任职要求。")

    fields = {
        "title": title.strip() or str(detail.get("title") or ""),
        "company": company.strip() or str(detail.get("company") or ""),
        "role": role.strip() or str(detail.get("role") or ""),
        "location": location.strip() or str(detail.get("location") or ""),
        "salary": salary.strip() or str(detail.get("salary") or ""),
    }
    metadata = extract_platform_metadata(clean_raw, fields)
    quality = assess_content_quality(clean_raw, metadata)
    item = CollectedItem(
        platform=detail["platform"],
        external_id=detail["external_id"],
        url=detail["canonical_url"],
        title=fields["title"],
        company=metadata.get("company", ""),
        role=metadata.get("role", ""),
        location=metadata.get("location", ""),
        salary=metadata.get("salary", ""),
        posted_at=normalize_posted_at(posted_at) or str(detail.get("posted_at") or ""),
        raw_text=clean_raw,
        source_keyword=str(detail.get("source_keyword") or ""),
        snapshot_path=str(detail.get("snapshot_path") or ""),
    )
    canonical_url, url_hash, content_hash, cross_key = _fingerprints(item)
    old_basis = _normalized_text(
        "|".join(
            [
                str(detail.get("raw_text") or ""),
                str(detail.get("company") or ""),
                str(detail.get("role") or ""),
                str(detail.get("location") or ""),
            ]
        )
    )
    new_basis = _normalized_text(
        "|".join([clean_raw, item.company, item.role, item.location])
    )
    content_changed = old_basis != new_basis
    current_status = str(detail.get("status") or "new")
    if current_status in {"dismissed", "shortlisted"}:
        next_status = current_status
    else:
        next_status = "new" if quality.scorable else "needs_details"
    now = _now()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE discovery_items
            SET canonical_url = ?, canonical_url_hash = ?, content_hash = ?,
                cross_platform_key = ?, title = ?, company = ?, role = ?, location = ?, salary = ?,
                posted_at = ?, raw_text = ?, content_level = ?, completeness_score = ?,
                missing_fields_json = ?, content_source = ?,
                content_updated_at = ?, is_recruitment = 1,
                recruitment_confidence = 1, status = ?,
                latest_evaluation_id = CASE WHEN ? THEN NULL ELSE latest_evaluation_id END,
                last_seen_at = ?
            WHERE id = ?
            """,
            (
                canonical_url, url_hash, content_hash, cross_key,
                item.title, item.company, item.role, item.location, item.salary,
                item.posted_at, clean_raw,
                quality.content_level, quality.completeness_score,
                quality.missing_fields_json(), content_source, now, next_status,
                int(content_changed), now, item_id,
            ),
        )
    return {
        "content_level": quality.content_level,
        "completeness_score": quality.completeness_score,
        "missing_fields": list(quality.missing_fields),
        "scorable": quality.scorable,
        "content_changed": content_changed,
    }


def confirm_as_recruitment(item_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE discovery_items
            SET is_recruitment = 1, recruitment_confidence = 1,
                status = CASE
                    WHEN content_level = 'full' THEN 'new'
                    ELSE 'needs_details'
                END,
                last_seen_at = ?
            WHERE id = ?
            """,
            (_now(), item_id),
        )


def save_discovery_evaluation(
    *,
    item_id: int,
    evaluation_run_id: int,
    resume_version_id: int,
    rubric_version: str,
    fit_score: int,
    gate_result: str,
    recommendation: str,
    jd_json: dict,
    eligibility_json: dict,
    evaluation_json: dict,
) -> int:
    now = _now()
    status = "hard_gate_failed" if gate_result == "不满足" else "scored"
    normalized_cross = _normalized_text(
        f"{jd_json.get('company', '')}|{jd_json.get('role', '')}|"
        f"{jd_json.get('location', '')}"
    )
    scored_cross_key = (
        hashlib.sha256(normalized_cross.encode()).hexdigest()
        if len(normalized_cross) >= 6
        else ""
    )
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO discovery_evaluations(
                discovery_item_id, evaluation_run_id, resume_version_id,
                rubric_version, fit_score, gate_result, recommendation,
                jd_json, eligibility_json, evaluation_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id, evaluation_run_id, resume_version_id, rubric_version,
                fit_score, gate_result, recommendation,
                json.dumps(jd_json, ensure_ascii=False),
                json.dumps(eligibility_json, ensure_ascii=False),
                json.dumps(evaluation_json, ensure_ascii=False),
                now,
            ),
        )
        evaluation_id = int(cursor.lastrowid)
        conn.execute(
            """
            UPDATE discovery_items
            SET latest_evaluation_id = ?,
                status = CASE WHEN application_id IS NOT NULL THEN 'imported' ELSE ? END,
                company = COALESCE(NULLIF(?, ''), company),
                role = COALESCE(NULLIF(?, ''), role),
                location = COALESCE(NULLIF(?, ''), location),
                salary = COALESCE(NULLIF(?, ''), salary),
                cross_platform_key = COALESCE(NULLIF(?, ''), cross_platform_key),
                last_seen_at = ?
            WHERE id = ?
            """,
            (
                evaluation_id, status, jd_json.get("company", ""),
                jd_json.get("role", ""), jd_json.get("location", ""),
                jd_json.get("salary", ""), scored_cross_key, now, item_id,
            ),
        )
        application_row = conn.execute(
            "SELECT application_id FROM discovery_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        application_id = int(application_row[0]) if application_row and application_row[0] else None
        if application_id is not None:
            conn.execute(
                """
                UPDATE applications
                SET score = ?, recommendation = ?, jd_text = ?, evaluation_json = ?,
                    resume_version_id = ?, analysis_run_id = ?
                WHERE id = ?
                """,
                (
                    fit_score,
                    recommendation,
                    str(jd_json.get("full_text") or ""),
                    json.dumps(evaluation_json, ensure_ascii=False),
                    resume_version_id,
                    evaluation_run_id,
                    application_id,
                ),
            )
            conn.execute(
                """
                UPDATE evaluation_runs
                SET entered_application_plan = 1, application_id = ?
                WHERE id = ?
                """,
                (application_id, evaluation_run_id),
            )
        conn.execute(
            "UPDATE evaluation_runs SET saved_to_discovery = 1 WHERE id = ?",
            (evaluation_run_id,),
        )
    return evaluation_id


def import_to_application(item_id: int) -> tuple[bool, str]:
    item = get_discovery_item(item_id)
    if item.get("content_level") != "full":
        return False, "岗位详情尚不完整，请补全完整 JD 并重新评分后再加入投递计划。"
    if item.get("application_id"):
        return False, f"已经加入投递计划，岗位 ID {item['application_id']}。"
    if not item.get("latest_evaluation_id"):
        return False, "请先使用当前评分规则评估这条岗位。"
    if item.get("status") != "shortlisted":
        return False, "请先人工点击“加入候选清单”，再进入投递计划。"
    jd = json.loads(item["jd_json"])
    evaluation = json.loads(item["evaluation_json"])
    fields = {
        "company": jd.get("company") or item.get("company", ""),
        "company_type": jd.get("company_type", ""),
        "role": jd.get("role") or item.get("role", ""),
        "location": jd.get("location") or item.get("location", ""),
        "salary": jd.get("salary") or item.get("salary", ""),
        "source": PLATFORM_LABELS.get(item["platform"], item["platform"]),
        "application_email": jd.get("application_email", ""),
        "application_reference": jd.get("application_reference", ""),
        "status": "待投递",
        "resume_version": "",
        "notes": f"岗位发现来源：{item['canonical_url']}",
    }
    created, application_id = save_application(
        fields=fields,
        jd_hash=item["content_hash"],
        jd_text=jd.get("full_text") or item["raw_text"],
        evaluation_json=json.dumps(evaluation, ensure_ascii=False),
        score=int(item["fit_score"]),
        recommendation=str(item["recommendation"]),
        resume_version_id=int(item["resume_version_id"]),
        analysis_run_id=int(item["evaluation_run_id"]),
    )
    if not created or application_id is None:
        return False, "该岗位已经存在于投递台账，没有重复创建。"
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE discovery_items
            SET status = 'imported', application_id = ?, last_seen_at = ?
            WHERE id = ?
            """,
            (application_id, _now(), item_id),
        )
    return True, f"已加入投递计划，岗位 ID {application_id}。"


def load_discovery_runs() -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        data = pd.read_sql_query(
            """
            SELECT id AS 扫描ID, platform AS 平台代码, keyword AS 关键词,
                   started_at AS 开始时间, finished_at AS 结束时间,
                   status AS 状态, scanned_count AS 扫描数,
                   inserted_count AS 新增, duplicate_count AS 重复,
                   filtered_count AS 过滤, error_text AS 提示与错误
            FROM discovery_runs ORDER BY id DESC
            """,
            conn,
        )
    if not data.empty:
        data["平台"] = data["平台代码"].map(PLATFORM_LABELS).fillna(data["平台代码"])
    return data
