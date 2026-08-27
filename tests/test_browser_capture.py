from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import services.database as database
import services.discovery_service as discovery
import services.resume_service as resumes
from services.browser_capture import ingest_browser_capture


def full_job_text() -> str:
    return (
        "百度AI战略实习找继任。\n"
        "岗位职责：\n"
        "1. 研究生成式 AI 行业和产品趋势；\n"
        "2. 通过用户调研和数据分析形成专项报告。\n"
        "任职要求：\n"
        "1. 本科及以上学历，具备结构化思维和数据分析能力；\n"
        "2. 每周到岗5天，可连续实习4个月；\n"
        "3. 对人工智能产品有热情，能够独立完成行业研究、竞品分析和报告撰写。\n"
        "工作地点：\n北京\n"
        "公司简介\n百度\n"
        "公司专注于搜索、人工智能和互联网产品。"
    )


class BrowserCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DB_PATH
        self.original_backup_dir = database.BACKUP_DIR
        self.original_discovery_path = discovery.DB_PATH
        self.original_resume_path = resumes.DB_PATH
        self.original_resume_dir = resumes.RESUME_DIR
        database.DB_PATH = Path(self.temp_dir.name) / "job_search.db"
        database.BACKUP_DIR = Path(self.temp_dir.name) / "backups"
        discovery.DB_PATH = database.DB_PATH
        resumes.DB_PATH = database.DB_PATH
        resumes.RESUME_DIR = Path(self.temp_dir.name) / "resumes"
        database.init_database()

    def tearDown(self):
        database.DB_PATH = self.original_database_path
        database.BACKUP_DIR = self.original_backup_dir
        discovery.DB_PATH = self.original_discovery_path
        resumes.DB_PATH = self.original_resume_path
        resumes.RESUME_DIR = self.original_resume_dir
        self.temp_dir.cleanup()

    def test_xhs_capture_preserves_access_url_and_manual_decision(self):
        url = (
            "https://www.xiaohongshu.com/explore/note-100"
            "?xsec_token=temporary-token&xsec_source=pc_search"
        )
        first = ingest_browser_capture(
            {
                "url": url,
                "title": "百度AI战略实习找继任",
                "raw_text": full_job_text(),
                "company": "百度",
                "role": "AI战略实习生",
                "location": "北京",
                "posted_at": "08-10 浙江",
                "manual_decision": "想投",
            }
        )
        self.assertTrue(first.created)
        detail = discovery.get_discovery_item(first.item_id)
        self.assertEqual(detail["canonical_url"], "https://www.xiaohongshu.com/explore/note-100")
        self.assertEqual(detail["access_url"], url)
        self.assertEqual(detail["capture_method"], "browser_extension")
        self.assertEqual(detail["manual_decision"], "想投")
        self.assertEqual(detail["content_level"], "full")
        self.assertEqual(detail["posted_at"], "2026-08-10")

        second = ingest_browser_capture(
            {
                "url": url,
                "title": "百度AI战略实习找继任",
                "raw_text": full_job_text(),
                "manual_decision": "不投",
            }
        )
        self.assertFalse(second.created)
        self.assertEqual(second.item_id, first.item_id)
        self.assertEqual(
            discovery.get_discovery_item(first.item_id)["manual_decision"], "不投"
        )

    def test_capture_rejects_unrelated_sites(self):
        with self.assertRaisesRegex(ValueError, "只支持"):
            ingest_browser_capture(
                {
                    "url": "https://example.com/job",
                    "title": "示例岗位",
                    "raw_text": "岗位职责和任职要求，这是一段足够长的示例岗位文字。",
                }
            )

    def test_shixiseng_capture_is_supported(self):
        receipt = ingest_browser_capture(
            {
                "url": "https://www.shixiseng.com/intern/inn-demo-100",
                "title": "战略产品实习生",
                "company": "示例科技",
                "role": "战略产品实习生",
                "location": "上海",
                "raw_text": full_job_text(),
            }
        )
        self.assertEqual(receipt.platform, "shixiseng")
        self.assertEqual(receipt.capture_kind, "text")
        self.assertEqual(receipt.availability_status, "active")

    def test_image_jd_is_saved_but_not_treated_as_scorable(self):
        receipt = ingest_browser_capture(
            {
                "url": "https://www.xiaohongshu.com/explore/image-note-1",
                "title": "实习招聘，详情见图片",
                "raw_text": "实习招聘，岗位职责和要求请查看笔记中的多张图片。",
                "image_count": 5,
            }
        )
        detail = discovery.get_discovery_item(receipt.item_id)
        self.assertEqual(receipt.capture_kind, "image")
        self.assertEqual(detail["status"], "needs_details")
        self.assertNotIn(receipt.item_id, discovery.list_unscored_discovery_item_ids())

    def test_xhs_note_title_is_not_used_as_job_role(self):
        receipt = ingest_browser_capture(
            {
                "url": "https://www.xiaohongshu.com/explore/note-metadata-1",
                "title": "招趣丸日常实习继任🧩",
                "role": "招趣丸日常实习继任🧩",
                "raw_text": (
                    "小红书商业化实习生继任\n"
                    "【小红书3C数码行业CBD实习生（商业化）】\n"
                    "base：广州\n岗位职责\n支持商业化项目和数据分析。\n"
                    "岗位要求\n本科及以上学历，每周到岗5天，可实习6个月。"
                ),
            }
        )
        detail = discovery.get_discovery_item(receipt.item_id)
        self.assertEqual(detail["company"], "小红书")
        self.assertEqual(detail["role"], "3C数码行业CBD实习生（商业化）")
        self.assertEqual(detail["location"], "广州")

    def test_xhs_group_prose_recovers_company_and_role(self):
        receipt = ingest_browser_capture(
            {
                "url": "https://www.xiaohongshu.com/explore/note-metadata-2",
                "title": "【急招继任】含泪出珀莱雅实习",
                "role": "【急招继任】含泪出珀莱雅实习",
                "raw_text": (
                    "橘宜实习招继任啦～\n橘宜海外产品急招继任\n"
                    "橘宜集团橘朵海外产品项目开发管理急召继任\n"
                    "base上海 博荟广场A座\nJD见P2"
                ),
                "image_count": 2,
            }
        )
        detail = discovery.get_discovery_item(receipt.item_id)
        self.assertEqual(detail["company"], "橘宜集团")
        self.assertEqual(detail["role"], "橘朵海外产品项目开发管理实习生")

    def test_main_responsibilities_heading_is_full_jd(self):
        receipt = ingest_browser_capture(
            {
                "url": "https://www.xiaohongshu.com/explore/note-main-duties",
                "title": "橘宜海外产品实习",
                "company": "橘宜集团",
                "role": "海外产品项目开发管理实习生",
                "location": "上海",
                "raw_text": (
                    "主要职责\n1. 协助项目经理跟进海外新品开发进度并整理文档。\n"
                    "2. 参与跨部门协调，推动项目节点按时达成，并支持海外市场趋势、竞品信息和消费者洞察分析。\n"
                    "任职要求\n1. 本科或硕士在校生，英语沟通能力优秀。\n"
                    "2. 每周到岗4天，可连续实习4个月，熟练使用办公软件并具备基础数据分析能力。\n"
                    "3. 对美妆行业和海外市场有兴趣，工作细致负责，具备良好的沟通协作和多任务处理能力。"
                ),
            }
        )
        detail = discovery.get_discovery_item(receipt.item_id)
        self.assertEqual(detail["content_level"], "full")
        self.assertEqual(detail["completeness_score"], 100)

    def test_xhs_misleading_card_title_is_replaced_by_visible_note(self):
        receipt = ingest_browser_capture(
            {
                "url": "https://www.xiaohongshu.com/explore/note-baidu-strategy",
                "title": "拼多多战略分析实习生面试",
                "role": "拼多多战略分析实习生面试",
                "raw_text": (
                    "百度招聘商业与战略分析实习生（北京）\n"
                    "【工作职责】\n1. 开展人工智能和出行等赛道研究，独立产出观点。\n"
                    "2. 进行竞品追踪和经营数据分析，提出商业分析建议。\n"
                    "【背景要求】\n1. 本科及以上学历，结构化思维和沟通能力优秀。\n"
                    "2. 熟练使用PPT和Excel，会Python或SQL加分，可连续实习6个月并保持稳定到岗。\n"
                    "工作地点：北京"
                ),
            }
        )
        detail = discovery.get_discovery_item(receipt.item_id)
        self.assertEqual(detail["title"], "百度招聘商业与战略分析实习生（北京）")
        self.assertEqual(detail["company"], "百度")
        self.assertEqual(detail["role"], "商业与战略分析实习生")
        self.assertEqual(detail["content_level"], "full")

    def test_historical_image_text_marker_is_not_used_as_company(self):
        receipt = ingest_browser_capture(
            {
                "url": "https://www.xiaohongshu.com/explore/note-kuaishou-overseas",
                "title": "拼多多战略分析实习生面试",
                "raw_text": (
                    "北京实习丨快手 海外战略分析实习生\n"
                    "【图片文字识别】\n快手 海外战略分析实习生\n【北京】\n"
                    "【任职要求】\n1. 每周线下实习4天及以上。\n"
                    "2. 有互联网战略或咨询实习经历者优先。"
                ),
                "image_count": 1,
            }
        )
        detail = discovery.get_discovery_item(receipt.item_id)
        self.assertEqual(detail["company"], "快手")
        self.assertEqual(detail["role"], "海外战略分析实习生")
        self.assertEqual(detail["location"], "北京")

    def test_expired_job_is_marked_and_excluded_from_scoring(self):
        receipt = ingest_browser_capture(
            {
                "url": "https://www.zhipin.com/job_detail/expired-1",
                "title": "产品实习生｜职位已下线",
                "company": "示例科技",
                "role": "产品实习生",
                "location": "北京",
                "raw_text": full_job_text() + "\n该职位已下线，停止招聘。",
            }
        )
        detail = discovery.get_discovery_item(receipt.item_id)
        self.assertEqual(receipt.availability_status, "expired")
        self.assertEqual(detail["status"], "expired")
        self.assertNotIn(receipt.item_id, discovery.list_unscored_discovery_item_ids())


if __name__ == "__main__":
    unittest.main()
