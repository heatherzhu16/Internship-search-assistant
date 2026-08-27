from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from candidate_profile import CandidateProfileData


@dataclass(frozen=True)
class SearchLink:
    platform: str
    keyword: str
    url: str
    note: str


def build_search_keywords(
    profile: CandidateProfileData,
    roles: list[str] | None = None,
    cities: list[str] | None = None,
    limit: int = 8,
) -> list[str]:
    selected_roles = [item.strip() for item in (roles or profile.preferences.target_roles) if item.strip()]
    selected_cities = [item.strip() for item in (cities or profile.preferences.preferred_cities) if item.strip()]
    selected_roles = selected_roles or ["产品", "战略", "数据分析"]
    selected_cities = selected_cities or [""]
    output: list[str] = []
    for role in selected_roles:
        base = role if "实习" in role else f"{role} 实习"
        for city in selected_cities:
            phrase = " ".join(part for part in (base, city) if part)
            if phrase not in output:
                output.append(phrase)
        referral = f"{base} 内推"
        if referral not in output:
            output.append(referral)
    return output[: max(1, min(int(limit), 20))]


def build_platform_links(keywords: list[str]) -> list[SearchLink]:
    links: list[SearchLink] = []
    for keyword in keywords:
        encoded = quote(keyword)
        links.extend(
            [
                SearchLink(
                    "BOSS直聘",
                    keyword,
                    f"https://www.zhipin.com/web/geek/job?query={encoded}",
                    "使用浏览器现有登录态筛选",
                ),
                SearchLink(
                    "实习僧",
                    keyword,
                    f"https://www.shixiseng.com/interns?keyword={encoded}&type=intern",
                    "平台内确认在招状态和完整 JD",
                ),
                SearchLink(
                    "小红书",
                    keyword,
                    f"https://www.xiaohongshu.com/search_result?keyword={encoded}",
                    "若网页限制浏览，请在 App 内复制关键词搜索",
                ),
            ]
        )
    return links
