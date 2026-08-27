from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from models.discovery import ScanLimits
from services.collectors import browser


class _FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class CollectorTests(unittest.TestCase):
    def test_xiaohongshu_navigation_does_not_wait_for_domcontentloaded(self):
        self.assertEqual(browser.REMOTE_DEBUG_PORTS["xiaohongshu"], 9333)
        self.assertEqual(browser._navigation_wait_until("xiaohongshu"), "commit")
        self.assertEqual(browser._page_settle_delay("xiaohongshu"), 4_000)
        self.assertEqual(browser._navigation_timeout("xiaohongshu"), 20_000)
        self.assertEqual(browser._navigation_wait_until("boss"), "domcontentloaded")

    def test_xiaohongshu_browser_risk_page_is_treated_as_verification(self):
        login, verification = browser._page_state(
            "当前浏览器环境存在风险，请稍后再试"
        )
        self.assertFalse(login)
        self.assertTrue(verification)

    def test_xiaohongshu_app_only_note_is_not_collected(self):
        self.assertTrue(
            browser._xiaohongshu_note_unavailable(
                "https://www.xiaohongshu.com/404?source=test",
                "小红书 - 你访问的页面不见了",
                "当前笔记暂时无法浏览，请打开小红书App扫码查看",
            )
        )

    def test_boss_list_prefilter_uses_explicit_company_preferences(self):
        limits = ScanLimits(
            target_companies=["百度", "腾讯"],
            excluded_companies=["久邦"],
            company_filter_mode="仅目标公司",
        )
        self.assertTrue(
            browser._boss_list_candidate_allowed(
                "百度｜AI 产品实习生｜北京", limits
            )
        )
        self.assertFalse(
            browser._boss_list_candidate_allowed(
                "久邦 GOMO｜产品实习生｜广州", limits
            )
        )
        self.assertFalse(
            browser._boss_list_candidate_allowed(
                "示例创业公司｜产品实习生｜上海", limits
            )
        )
        self.assertFalse(
            browser._xiaohongshu_note_unavailable(
                "https://www.xiaohongshu.com/explore/note-1",
                "产品实习招聘",
                "岗位职责与任职要求",
            )
        )

    def test_shixiseng_official_api_builds_structured_items(self):
        payload = {
            "code": 100,
            "msg": {
                "data": [
                    {
                        "uuid": "inn-example",
                        "name": "&#xed64;&#xf313;产品经理（实习）",
                        "cname": "示例科技",
                        "city": "上海",
                        "degree": "本科",
                        "industry": "互联网",
                        "minsalary": 150,
                        "maxsalary": 250,
                        "i_tags": ["可转正实习"],
                        "skill": [],
                    }
                ]
            },
        }
        with patch.object(browser, "urlopen", return_value=_FakeResponse(payload)):
            result = browser._collect_shixiseng_api(
                "产品实习",
                ScanLimits(max_items_per_keyword=6, max_details=18, timeout_seconds=240),
            )

        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].external_id, "inn-example")
        self.assertEqual(result.items[0].role, "产品经理（实习）")
        self.assertEqual(result.items[0].company, "示例科技")
        self.assertEqual(result.items[0].salary, "150-250元/天")
        self.assertNotIn("&#", result.items[0].title)
        self.assertIn("官方移动搜索接口", result.warnings[0])


if __name__ == "__main__":
    unittest.main()
