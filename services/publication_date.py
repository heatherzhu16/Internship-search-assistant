from __future__ import annotations

import re
from datetime import date, timedelta


def normalize_posted_at(value: str, reference_date: date | None = None) -> str:
    """Normalize a visible platform date without using it in match scoring."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    today = reference_date or date.today()
    compact = re.sub(r"\s+", "", raw)

    if "今天" in compact or re.search(r"\d+小时前", compact):
        return today.isoformat()
    if "昨天" in compact:
        return (today - timedelta(days=1)).isoformat()
    if "前天" in compact:
        return (today - timedelta(days=2)).isoformat()
    relative = re.search(r"(\d{1,3})天前", compact)
    if relative:
        return (today - timedelta(days=int(relative.group(1)))).isoformat()

    full = re.search(
        r"(?<!\d)(20\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})日?",
        raw,
    )
    if full:
        try:
            return date(int(full.group(1)), int(full.group(2)), int(full.group(3))).isoformat()
        except ValueError:
            return ""

    month_day = re.search(
        r"(?<!\d)(\d{1,2})\s*(?:月|[./-])\s*(\d{1,2})日?(?!\d)", raw
    )
    if not month_day:
        return ""
    try:
        candidate = date(today.year, int(month_day.group(1)), int(month_day.group(2)))
    except ValueError:
        return ""
    if candidate > today + timedelta(days=7):
        candidate = candidate.replace(year=today.year - 1)
    return candidate.isoformat()


def publication_recency_label(value: str, reference_date: date | None = None) -> str:
    normalized = normalize_posted_at(value, reference_date)
    if not normalized:
        return "日期待确认"
    today = reference_date or date.today()
    published = date.fromisoformat(normalized)
    age = (today - published).days
    if age < 0:
        return "日期待确认"
    if age <= 3:
        return "新发布"
    if age <= 7:
        return "近一周"
    return ""
