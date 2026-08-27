from __future__ import annotations

import json
import re
from dataclasses import dataclass


SUMMARY_MARKERS = (
    "信息范围：实习僧官方岗位列表摘要",
    "信息范围:实习僧官方岗位列表摘要",
)
RESPONSIBILITY_MARKERS = (
    "职位描述",
    "岗位职责",
    "工作职责",
    "主要职责",
    "职责描述",
    "主要工作",
    "工作内容",
)
REQUIREMENT_MARKERS = (
    "任职要求",
    "任职资格",
    "背景要求",
    "工作要求",
    "岗位要求",
    "岗位需求",
    "申请要求",
    "职位要求",
    "我们希望你",
)
MISSING_RESPONSIBILITY_MARKERS = (
    "只展示了岗位要求",
    "只显示了岗位要求",
    "没有看到完整的岗位职责",
    "未展示岗位职责",
    "岗位职责缺失",
)

CONTENT_LEVEL_LABELS = {
    "summary": "仅列表摘要",
    "partial": "详情不完整",
    "full": "完整 JD",
}


@dataclass(frozen=True)
class ContentQuality:
    content_level: str
    completeness_score: int
    missing_fields: tuple[str, ...]

    @property
    def scorable(self) -> bool:
        return self.content_level == "full"

    def missing_fields_json(self) -> str:
        return json.dumps(list(self.missing_fields), ensure_ascii=False)


def _clean_line(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" ：:")


KNOWN_JOB_BRANDS = (
    "小红书", "快手", "百度", "美团", "滴滴", "携程", "字节跳动",
    "腾讯", "阿里巴巴", "京东", "得物", "拼多多", "网易", "哔哩哔哩",
)
NON_COMPANY_BRACKETS = {
    "图片文字识别", "职位详情", "岗位详情", "岗位职责", "工作职责",
    "主要职责", "任职要求", "任职资格", "背景要求", "工作要求",
}


def extract_xiaohongshu_metadata(
    raw_text: str,
    fields: dict[str, str],
) -> dict[str, str]:
    """Extract job metadata from Xiaohongshu note prose.

    A note title describes the post, not necessarily the job. The Chrome extension
    therefore sends it as ``title`` but it must not silently become ``role``.
    Explicit structured fields remain authoritative when they are available.
    """
    raw = str(raw_text or "")
    output = {key: _clean_line(value) for key, value in fields.items()}
    captured_title = output.get("title", "")
    first_line = next(
        (
            _clean_line(line)
            for line in raw.splitlines()
            if 4 <= len(_clean_line(line)) <= 100
        ),
        "",
    )
    if first_line and first_line not in {
        "职位详情", "岗位详情", "主要职责", "岗位职责", "工作职责", "任职要求"
    }:
        # On Xiaohongshu, a global `.title` selector can hit a recommendation
        # card behind the open note. The first visible content line belongs to
        # the captured note and is a safer source title.
        output["title"] = first_line
    note_title = output.get("title", "")
    role_is_note_title = (
        not output.get("role")
        or output.get("role") in {captured_title, note_title}
    )

    company = output.get("company", "")
    role = "" if role_is_note_title else output.get("role", "")

    # Common format: 【快手】商业分析实习生 / 【公司】岗位名称。
    bracket_company = re.search(
        r"【\s*([^】\n]{2,20})\s*】\s*([^\n]{2,80}?(?:实习生|实习)[^\n]*)",
        raw,
    )
    if bracket_company:
        candidate_company = _clean_line(bracket_company.group(1))
        candidate_role = _clean_line(bracket_company.group(2))
        if candidate_company not in NON_COMPANY_BRACKETS:
            if not company:
                company = candidate_company
            if not role:
                role = candidate_role

    # Common format: 【小红书3C数码行业CBD实习生（商业化）】。
    if not company or not role:
        for brand in KNOWN_JOB_BRANDS:
            embedded = re.search(
                rf"【\s*{re.escape(brand)}\s*([^】\n]*?(?:实习生|实习)[^】\n]*)】",
                raw,
            )
            if embedded:
                company = company or brand
                role = role or _clean_line(embedded.group(1))
                break

    # Company suffix in prose: 橘宜集团橘朵海外产品项目开发管理急招继任。
    if not company or not role:
        prose = re.search(
            r"(?m)^\s*([\u4e00-\u9fffA-Za-z0-9·]{2,16}?(?:集团|公司))"
            r"([^\n]{2,60}?)(?:急招|急召|招聘|招继任|找继任)",
            raw,
        )
        if prose:
            company = company or _clean_line(prose.group(1))
            inferred_role = _clean_line(prose.group(2))
            if inferred_role and "实习" not in inferred_role:
                inferred_role += "实习生"
            role = role or inferred_role

    # A leading brand followed by an internship role is also reliable enough.
    if not company or not role:
        for brand in KNOWN_JOB_BRANDS:
            leading = re.search(
                rf"(?m)^\s*{re.escape(brand)}\s*([^\n]{{2,50}}?(?:实习生|实习))",
                raw,
            )
            if leading:
                company = company or brand
                inferred_role = re.sub(
                    r"^(?:急招|招聘|招募|招)", "", _clean_line(leading.group(1))
                )
                role = role or inferred_role
                break

    output["company"] = company
    output["role"] = role

    if not output.get("location"):
        base = re.search(r"(?i)\bbase\s*[：:]?\s*([^\s，,。；;]{2,20})", raw)
        bracket_city = re.search(
            r"[【（(]\s*(北京|上海|广州|深圳|杭州|成都|南京|武汉|苏州|天津|重庆)\s*[】）)]",
            raw,
        )
        tail = re.search(
            r"(?:编辑于\s*)?(?:\d{1,3}(?:分钟|小时|天)前|今天|昨天)\s+"
            r"([^\s，,。；;]{2,12})\s*$",
            raw.strip(),
        )
        if base:
            output["location"] = _clean_line(base.group(1))
        elif bracket_city:
            output["location"] = _clean_line(bracket_city.group(1))
        elif tail:
            output["location"] = _clean_line(tail.group(1))
    return output


def extract_platform_metadata(raw_text: str, fields: dict[str, str]) -> dict[str, str]:
    """Recover stable metadata from a visible job-detail page.

    Platform adapters should still populate structured fields whenever possible.
    This fallback prevents useful company/location text already present in a detail
    page from being discarded before scoring.
    """
    raw = str(raw_text or "")
    output = {key: _clean_line(value) for key, value in fields.items()}

    if not output.get("company"):
        match = re.search(
            r"公司简介\s*[：:]?\s*(?:\r?\n)+\s*([^\r\n]{2,100})",
            raw,
            flags=re.IGNORECASE,
        )
        if match:
            candidate = _clean_line(match.group(1)).strip('“”"')
            if candidate and candidate not in {"职位百科", "产品服务"}:
                output["company"] = candidate

    if not output.get("location"):
        match = re.search(
            r"工作地点\s*[：:]?\s*(?:\r?\n)?\s*([^\r\n]{2,160})",
            raw,
            flags=re.IGNORECASE,
        )
        if match:
            candidate = _clean_line(match.group(1))
            if candidate and "举报" not in candidate:
                output["location"] = candidate

    if not output.get("salary"):
        match = re.search(
            r"(?<!\d)(\d{2,5}(?:\s*-\s*\d{2,5})?\s*(?:元)?\s*[／/]\s*(?:天|日|小时|月))",
            raw,
        )
        if match:
            output["salary"] = _clean_line(match.group(1))

    if not output.get("role"):
        output["role"] = output.get("title", "")

    return output


def assess_content_quality(raw_text: str, fields: dict[str, str]) -> ContentQuality:
    raw = str(raw_text or "").strip()
    metadata = extract_platform_metadata(raw, fields)
    responsibilities_explicitly_missing = any(
        marker in raw for marker in MISSING_RESPONSIBILITY_MARKERS
    )
    has_responsibilities = (
        any(marker in raw for marker in RESPONSIBILITY_MARKERS)
        and not responsibilities_explicitly_missing
    )
    has_requirements = any(marker in raw for marker in REQUIREMENT_MARKERS)
    is_explicit_summary = any(marker in raw for marker in SUMMARY_MARKERS)

    checks = {
        "company": bool(metadata.get("company")),
        "role": bool(metadata.get("role") or metadata.get("title")),
        "location": bool(metadata.get("location")),
        "responsibilities": has_responsibilities,
        "requirements": has_requirements,
    }
    weights = {
        "company": 15,
        "role": 10,
        "location": 10,
        "responsibilities": 30,
        "requirements": 35,
    }
    score = sum(weights[key] for key, present in checks.items() if present)
    missing = tuple(key for key, present in checks.items() if not present)

    if is_explicit_summary:
        level = "summary"
    elif not missing and len(raw) >= 160:
        level = "full"
    else:
        level = "partial" if has_responsibilities or has_requirements else "summary"
    return ContentQuality(level, score, missing)
