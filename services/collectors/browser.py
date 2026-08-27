from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from models.discovery import CollectedItem, CollectorResult, DiscoveryPlatform, ScanLimits
from services.database import APP_DIR


CHROME_PATH = Path(
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)
PROFILE_ROOT = APP_DIR / "data" / "browser_profiles"
SNAPSHOT_ROOT = APP_DIR / "data" / "discovery_snapshots"
NODE_WORKER = Path(__file__).with_name("browser_worker.js")
REMOTE_DEBUG_PORTS = {
    "shixiseng": 9331,
    "boss": 9332,
    "xiaohongshu": 9333,
}

PLATFORM_CONFIG: dict[str, dict[str, object]] = {
    "shixiseng": {
        "home": "https://www.shixiseng.com/",
        "search": "https://www.shixiseng.com/interns?keyword={keyword}",
        "link_patterns": [r"/intern/[^/?#]+", r"/interns/[^/?#]+"],
    },
    "boss": {
        "home": "https://www.zhipin.com/",
        "search": "https://www.zhipin.com/web/geek/job?query={keyword}",
        "link_patterns": [r"/job_detail/[^/?#]+", r"/web/geek/job\?[^#]*securityId="],
    },
    "xiaohongshu": {
        "home": "https://www.xiaohongshu.com/",
        "search": (
            "https://www.xiaohongshu.com/search_result?"
            "keyword={keyword}&source=web_search_result_notes"
        ),
        "link_patterns": [r"/explore/[^/?#]+", r"/search_result/[^/?#]+"],
    },
}

VERIFICATION_MARKERS = [
    "安全验证", "请完成验证", "滑动验证", "访问异常", "验证码",
    "当前浏览器环境存在风险", "网络环境存在风险",
    "captcha", "verify you are human",
]
LOGIN_MARKERS = ["登录后查看", "请先登录", "登录/注册", "扫码登录"]

SHIXISENG_API = "https://wap.shixiseng.com/app/interns/search/v2"


def collector_available() -> tuple[bool, str]:
    if not CHROME_PATH.exists():
        return False, "没有找到 /Applications/Google Chrome.app。"
    try:
        import playwright.sync_api  # noqa: F401
        return True, ""
    except ImportError:
        if _node_playwright_root() and shutil.which("node") and NODE_WORKER.exists():
            return True, ""
        return False, (
            "没有找到 Playwright。请在 Codex 桌面环境运行，"
            "或单独安装 Python Playwright。"
        )


def _node_playwright_root() -> Path | None:
    runtime_root = Path.home() / ".cache" / "codex-runtimes"
    for candidate in sorted(
        runtime_root.glob("*/dependencies/node/node_modules"),
        reverse=True,
    ):
        if (candidate / "playwright" / "package.json").exists():
            return candidate
    return None


def _profile_dir(platform: str) -> Path:
    path = PROFILE_ROOT / platform
    path.mkdir(parents=True, exist_ok=True)
    return path


def open_login_browser(platform: DiscoveryPlatform) -> int:
    config = PLATFORM_CONFIG[platform]
    debug_port = REMOTE_DEBUG_PORTS[platform]
    process = subprocess.Popen(
        [
            str(CHROME_PATH),
            f"--user-data-dir={_profile_dir(platform)}",
            f"--remote-debugging-port={debug_port}",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-quic",
            str(config["home"]),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return int(process.pid)


def _connect_logged_in_browser(playwright, platform: DiscoveryPlatform):
    """Reuse a user-opened dedicated Chrome window when its local endpoint exists."""
    endpoint = f"http://127.0.0.1:{REMOTE_DEBUG_PORTS[platform]}"
    try:
        with urlopen(f"{endpoint}/json/version", timeout=1) as response:
            if int(getattr(response, "status", 200)) != 200:
                return None
        browser = playwright.chromium.connect_over_cdp(endpoint, timeout=3_000)
        if not browser.contexts:
            return None
        return browser, browser.contexts[0]
    except Exception:
        return None


def _canonical_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if "/job_detail/" in path or "/explore/" in path or "/intern/" in path:
        query = ""
    else:
        allowed = [
            item
            for item in parts.query.split("&")
            if item.startswith(("securityId=", "id="))
        ]
        query = "&".join(allowed)
    return urlunsplit((parts.scheme, parts.netloc.casefold(), path, query, ""))


def _external_id(platform: str, url: str) -> str:
    patterns = {
        "boss": [r"/job_detail/([^/?#]+)", r"[?&]securityId=([^&#]+)"],
        "shixiseng": [r"/intern/([^/?#]+)", r"/interns/([^/?#]+)"],
        "xiaohongshu": [r"/explore/([^/?#]+)", r"/search_result/([^/?#]+)"],
    }
    for pattern in patterns[platform]:
        if match := re.search(pattern, url):
            return match.group(1)
    return hashlib.sha256(_canonical_url(url).encode()).hexdigest()[:24]


def _matching_links(page, platform: str, limit: int) -> list[dict[str, str]]:
    config = PLATFORM_CONFIG[platform]
    raw_links = page.locator("a[href]").evaluate_all(
        """
        elements => elements.map(element => {
          const container = element.closest('li, article, section, [class*="card"], [class*="item"]');
          return {
            href: element.href || '',
            title: (element.innerText || element.textContent || '').trim(),
            context: (container?.innerText || '').trim()
          };
        })
        """
    )
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in raw_links:
        href = urljoin(page.url, str(row.get("href", "")))
        if not any(re.search(pattern, href) for pattern in config["link_patterns"]):
            continue
        canonical = _canonical_url(href)
        if canonical in seen:
            continue
        seen.add(canonical)
        output.append(
            {
                "url": href,
                "title": re.sub(r"\s+", " ", str(row.get("title", ""))).strip()[:300],
                "context": str(row.get("context", "")).strip()[:4000],
            }
        )
        if len(output) >= limit:
            break
    return output


def _boss_list_candidate_allowed(context: str, limits: ScanLimits) -> bool:
    """Apply explicit company preferences before opening a BOSS detail page."""
    compact = re.sub(r"\s+", "", str(context or "")).casefold()
    excluded = [
        re.sub(r"\s+", "", name).casefold()
        for name in limits.excluded_companies
        if str(name).strip()
    ]
    if any(name in compact for name in excluded):
        return False
    targets = [
        re.sub(r"\s+", "", name).casefold()
        for name in limits.target_companies
        if str(name).strip()
    ]
    if limits.company_filter_mode == "仅目标公司" and targets:
        return any(name in compact for name in targets)
    return True


def _page_state(text: str) -> tuple[bool, bool]:
    folded = text.casefold()
    verification = any(marker.casefold() in folded for marker in VERIFICATION_MARKERS)
    login = any(marker.casefold() in folded for marker in LOGIN_MARKERS)
    return login, verification


def _xiaohongshu_note_unavailable(url: str, title: str, text: str) -> bool:
    combined = f"{title}\n{text}".casefold()
    return bool(
        "/404" in str(url)
        or "当前笔记暂时无法浏览" in combined
        or "你访问的页面不见了" in combined
        or "请打开小红书app扫码查看" in combined
    )


def _navigation_wait_until(platform: str) -> str:
    """Xiaohongshu keeps loading long-lived resources after the HTML responds."""
    return "commit" if platform == "xiaohongshu" else "domcontentloaded"


def _page_settle_delay(platform: str) -> int:
    return 4_000 if platform == "xiaohongshu" else 2_500


def _navigation_timeout(platform: str) -> int:
    return 20_000 if platform == "xiaohongshu" else 30_000


def _clean_shixiseng_text(value: object) -> str:
    """Remove the site's encrypted private-font entities from readable labels."""
    text = re.sub(r"&#(?:x[0-9a-f]+|\d+);?", "", str(value or ""), flags=re.I)
    text = html.unescape(text)
    text = re.sub(r"[（(]\s*[）)]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _collect_shixiseng_api(keyword: str, limits: ScanLimits) -> CollectorResult:
    """Use the official mobile listing API when the desktop site is challenged."""
    result = CollectorResult(platform="shixiseng", keyword=keyword)
    query = urlencode(
        {
            "page": 1,
            "keyword": keyword,
            "category": "",
            "city": "",
            "area": "",
            "salary": "-",
            "degree": "",
            "days": "",
            "months": "",
        }
    )
    request = Request(
        f"{SHIXISENG_API}?{query}",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/138 Safari/537.36"
            ),
            "Referer": "https://wap.shixiseng.com/interns/list/search",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    try:
        with urlopen(request, timeout=min(limits.timeout_seconds, 30)) as response:
            payload = json.loads(response.read().decode("utf-8"))
        rows = payload.get("msg", {}).get("data", []) if payload.get("code") == 100 else []
        for row in rows[: limits.max_items_per_keyword]:
            external_id = str(row.get("uuid") or "").strip()
            if not external_id:
                continue
            role = _clean_shixiseng_text(row.get("name")) or "实习岗位"
            company = _clean_shixiseng_text(row.get("cname"))
            location = _clean_shixiseng_text(row.get("city"))
            degree = _clean_shixiseng_text(row.get("degree"))
            industry = _clean_shixiseng_text(row.get("industry"))
            minimum = int(row.get("minsalary") or 0)
            maximum = int(row.get("maxsalary") or 0)
            salary = (
                f"{minimum}-{maximum}元/天"
                if minimum and maximum
                else (f"{minimum or maximum}元/天" if minimum or maximum else "薪资面议")
            )
            tags = [
                _clean_shixiseng_text(tag)
                for tag in [*(row.get("i_tags") or []), *(row.get("skill") or [])]
            ]
            summary_lines = [
                f"职位：{role}",
                f"公司：{company}" if company else "",
                f"城市：{location}" if location else "",
                f"薪资：{salary}",
                f"学历：{degree}" if degree else "",
                f"行业：{industry}" if industry else "",
                f"岗位标签：{'、'.join(tag for tag in tags if tag)}" if any(tags) else "",
                "信息范围：实习僧官方岗位列表摘要；打开原始页面可查看完整职责。",
            ]
            result.items.append(
                CollectedItem(
                    platform="shixiseng",
                    external_id=external_id,
                    url=f"https://www.shixiseng.com/intern/{external_id}",
                    title=role,
                    company=company,
                    role=role,
                    location=location,
                    salary=salary,
                    raw_text="\n".join(line for line in summary_lines if line),
                    source_keyword=keyword,
                )
            )
        if result.items:
            result.warnings.append(
                "实习僧桌面页当前触发安全校验，已自动改用官方移动搜索接口；"
                "发现箱可正常收录，评分依据暂为岗位列表摘要。"
            )
        else:
            result.warnings.append("实习僧官方搜索接口未返回匹配岗位。")
    except Exception as exc:
        result.error = f"实习僧官方搜索接口读取失败：{type(exc).__name__}: {exc}"
    return result


def _safe_snapshot_name(platform: str, external_id: str) -> Path:
    folder = SNAPSHOT_ROOT / platform
    folder.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", external_id)[:80]
    return folder / f"{safe_id}.png"


def _collect_with_node(
    platform: DiscoveryPlatform,
    keyword: str,
    limits: ScanLimits,
) -> CollectorResult:
    module_root = _node_playwright_root()
    node_path = shutil.which("node")
    if module_root is None or node_path is None:
        return CollectorResult(
            platform=platform,
            keyword=keyword,
            error="没有可用的 Node Playwright 运行时。",
        )
    payload = {
        "platform": platform,
        "keyword": keyword,
        "limits": limits.model_dump(),
        "chrome_path": str(CHROME_PATH),
        "profile_dir": str(_profile_dir(platform)),
        "snapshot_dir": str(SNAPSHOT_ROOT / platform),
        "app_dir": str(APP_DIR),
        "remote_debug_port": REMOTE_DEBUG_PORTS[platform],
    }
    environment = os.environ.copy()
    existing_node_path = environment.get("NODE_PATH", "")
    environment["NODE_PATH"] = (
        str(module_root)
        if not existing_node_path
        else f"{module_root}{os.pathsep}{existing_node_path}"
    )
    process = subprocess.Popen(
        [node_path, str(NODE_WORKER), json.dumps(payload, ensure_ascii=False)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=limits.timeout_seconds + 60)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        process.communicate()
        return CollectorResult(
            platform=platform,
            keyword=keyword,
            error="浏览器采集超过安全时间上限，已停止。",
        )
    if not stdout.strip():
        return CollectorResult(
            platform=platform,
            keyword=keyword,
            error=f"浏览器采集没有返回结果：{stderr.strip()[:300]}",
        )
    try:
        return CollectorResult.model_validate_json(stdout)
    except Exception as exc:
        return CollectorResult(
            platform=platform,
            keyword=keyword,
            error=f"无法解析浏览器结果：{exc}；{stderr.strip()[:200]}",
        )


def collect_platform(
    platform: DiscoveryPlatform,
    keyword: str,
    limits: ScanLimits,
) -> CollectorResult:
    if platform == "shixiseng":
        api_result = _collect_shixiseng_api(keyword, limits)
        if api_result.items or not api_result.error:
            return api_result

    available, reason = collector_available()
    if not available:
        return CollectorResult(platform=platform, keyword=keyword, error=reason)
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        return _collect_with_node(platform, keyword, limits)

    result = CollectorResult(platform=platform, keyword=keyword)
    search_url = str(PLATFORM_CONFIG[platform]["search"]).format(
        keyword=quote_plus(keyword)
    )
    deadline = time.monotonic() + limits.timeout_seconds
    try:
        with sync_playwright() as playwright:
            connected = _connect_logged_in_browser(playwright, platform)
            owns_context = connected is None
            connected_pages = []
            if connected:
                _, context = connected
                page = context.new_page()
                connected_pages.append(page)
            else:
                context = playwright.chromium.launch_persistent_context(
                    str(_profile_dir(platform)),
                    executable_path=str(CHROME_PATH),
                    headless=False,
                    viewport=None,
                    args=[
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-quic",
                    ],
                )
                page = context.pages[0] if context.pages else context.new_page()
            context.set_default_timeout(12_000)
            try:
                try:
                    page.goto(
                        search_url,
                        wait_until=_navigation_wait_until(platform),
                        timeout=_navigation_timeout(platform),
                    )
                except PlaywrightTimeout:
                    if platform != "xiaohongshu":
                        raise
                    result.login_required = True
                    result.error = (
                        "小红书网页入口未响应。请先点击“打开小红书登录窗口”，"
                        "人工登录并完成可能出现的安全验证，保持窗口开启后再扫描；"
                        "如果登录窗口也无法打开，请稍后再试。"
                    )
                    return result
                page.wait_for_timeout(_page_settle_delay(platform))
                search_text = page.locator("body").inner_text(timeout=10_000)[:30_000]
                login, verification = _page_state(search_text)
                if verification:
                    result.verification_required = True
                    result.error = "平台触发了安全验证，扫描已停止，请在登录窗口人工完成验证。"
                    return result
                if login and len(search_text) < 1200:
                    result.login_required = True
                    result.error = "登录状态可能已失效，请先点击“打开登录窗口”。"
                    return result
                links = _matching_links(
                    page,
                    platform,
                    min(limits.max_items_per_keyword, limits.max_details),
                )
                if not links:
                    result.warnings.append(
                        "页面没有提取到岗位链接；可能没有结果、需要登录，或平台页面结构已变化。"
                    )
                detail_page = context.new_page()
                if not owns_context:
                    connected_pages.append(detail_page)
                for row in links[: limits.max_details]:
                    if time.monotonic() >= deadline:
                        result.warnings.append("已达到本次扫描时间上限。")
                        break
                    if platform == "boss" and not _boss_list_candidate_allowed(
                        row["context"], limits
                    ):
                        result.prefiltered_count += 1
                        continue
                    external_id = _external_id(platform, row["url"])
                    body_text = row["context"]
                    final_url = row["url"]
                    title = row["title"]
                    snapshot_path = ""
                    try:
                        detail_page.goto(
                            row["url"],
                            wait_until=_navigation_wait_until(platform),
                            timeout=25_000,
                        )
                        detail_page.wait_for_timeout(1000)
                        final_url = detail_page.url
                        page_title = detail_page.title().strip()
                        if page_title:
                            title = title or page_title
                        body_text = detail_page.locator("body").inner_text(
                            timeout=10_000
                        )[:25_000]
                        if platform == "xiaohongshu" and _xiaohongshu_note_unavailable(
                            final_url, title, body_text
                        ):
                            result.warnings.append(
                                f"笔记 {external_id} 当前仅允许在 App 内查看，已跳过。"
                            )
                            continue
                        login, verification = _page_state(body_text)
                        if verification:
                            result.verification_required = True
                            result.warnings.append(
                                "读取详情时触发安全验证，已停止后续详情采集。"
                            )
                            break
                        if platform == "xiaohongshu":
                            snapshot = _safe_snapshot_name(platform, external_id)
                            detail_page.screenshot(path=str(snapshot), full_page=False)
                            snapshot_path = str(snapshot.relative_to(APP_DIR))
                    except (PlaywrightTimeout, PlaywrightError) as exc:
                        result.warnings.append(
                            f"详情 {external_id} 读取不完整：{str(exc)[:120]}"
                        )
                    result.items.append(
                        CollectedItem(
                            platform=platform,
                            external_id=external_id,
                            url=final_url,
                            access_url=final_url,
                            title=title[:300],
                            raw_text=body_text[:25_000],
                            source_keyword=keyword,
                            snapshot_path=snapshot_path,
                        )
                    )
            finally:
                if owns_context:
                    context.close()
                else:
                    for connected_page in connected_pages:
                        connected_page.close()
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    return result
