import tempfile
import unittest
import sqlite3
from pathlib import Path

import pandas as pd

import services.database as database
import services.discovery_service as discovery
import services.resume_service as resumes
from profile_store import load_default_profile, save_evaluation_run
from candidate_profile import CandidateProfileData, JobPreferences
from models.discovery import CollectedItem, CollectorResult


def full_job_text(company: str = "示例科技", city: str = "上海") -> str:
    return (
        "职位描述：\n"
        "1. 负责产品需求分析、用户调研和方案设计；\n"
        "2. 与研发、设计和运营协作，跟进产品上线并分析效果数据。\n"
        "任职要求：\n"
        "1. 本科及以上学历，具备良好的数据分析和沟通能力；\n"
        "2. 熟练使用 Excel 或 SQL，每周至少到岗4天，连续实习3个月。\n"
        f"工作地点：\n{city}\n"
        f"公司简介\n{company}\n"
        "公司专注于用技术改善用户体验，并提供完善的实习生培养机制。"
    )


class DiscoveryTests(unittest.TestCase):
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

    def test_keywords_use_confirmed_roles_and_cities(self):
        profile = CandidateProfileData(
            preferences=JobPreferences(
                target_roles=["AI 产品经理"],
                preferred_cities=["上海"],
            )
        )
        keywords = discovery.generate_keywords(profile)
        self.assertIn("AI 产品经理 实习 上海", keywords)
        self.assertIn("AI 产品经理 实习 内推", keywords)

    def test_keywords_do_not_repeat_internship_word(self):
        profile = CandidateProfileData()
        profile.preferences.target_roles = ["产品实习"]
        profile.preferences.preferred_cities = [""]

        keywords = discovery.generate_keywords(profile)

        self.assertEqual(keywords, ["产品实习", "产品实习 内推"])

    def test_company_preferences_support_targets_and_explicit_exclusions(self):
        profile = CandidateProfileData(
            preferences=JobPreferences(
                target_companies=["目标科技A", "目标科技B"],
                excluded_companies=["排除公司"],
                company_filter_mode="仅目标公司",
            )
        )

        self.assertEqual(discovery.company_preference("目标科技A", profile), (True, "符合"))
        self.assertEqual(
            discovery.company_preference("排除公司科技", profile),
            (False, "已排除公司"),
        )
        self.assertEqual(
            discovery.company_preference("示例创业公司", profile),
            (False, "不在目标公司名单"),
        )

    def test_text_filter_handles_empty_and_nonempty_discovery_results(self):
        columns = ["公司", "职位", "城市", "搜索关键词"]
        empty = pd.DataFrame(columns=columns)
        self.assertTrue(
            discovery.filter_discovery_items_by_text(empty, "战略").empty
        )

        items = pd.DataFrame(
            [
                {
                    "公司": "滴滴",
                    "职位": "战略实习生",
                    "城市": "北京",
                    "搜索关键词": "战略",
                },
                {
                    "公司": "示例科技",
                    "职位": "产品实习生",
                    "城市": "上海",
                    "搜索关键词": "产品",
                },
            ]
        )
        filtered = discovery.filter_discovery_items_by_text(items, "战略")
        self.assertEqual(filtered["公司"].tolist(), ["滴滴"])

    def test_collector_warnings_are_visible_in_run_summary(self):
        run_id = discovery.start_discovery_run("shixiseng", "产品实习")

        summary = discovery.complete_discovery_run(
            run_id,
            CollectorResult(
                platform="shixiseng",
                keyword="产品实习",
                warnings=["已切换官方移动搜索接口"],
            ),
        )

        self.assertEqual(summary.errors, ["已切换官方移动搜索接口"])
        runs = discovery.load_discovery_runs()
        self.assertEqual(runs.iloc[0]["提示与错误"], "已切换官方移动搜索接口")

    def test_xhs_recruitment_filter(self):
        recruitment = CollectedItem(
            platform="xiaohongshu",
            external_id="note-1",
            url="https://www.xiaohongshu.com/explore/note-1",
            title="产品实习生招聘｜内推",
            raw_text="岗位职责：用户研究。任职要求：每周到岗4天，请投递简历。",
        )
        non_recruitment = CollectedItem(
            platform="xiaohongshu",
            external_id="note-2",
            url="https://www.xiaohongshu.com/explore/note-2",
            title="我的面试复盘和上岸经验",
            raw_text="分享我的求职经验和面经。",
        )
        self.assertTrue(discovery.classify_recruitment(recruitment)[0])
        self.assertFalse(discovery.classify_recruitment(non_recruitment)[0])

    def test_same_source_item_is_deduplicated_without_writing_ledger(self):
        item = CollectedItem(
            platform="shixiseng",
            external_id="intern-100",
            url="https://www.shixiseng.com/intern/intern-100?utm_source=test",
            title="产品实习生",
            company="示例科技",
            role="产品实习生",
            location="上海",
            raw_text=full_job_text(),
            source_keyword="产品实习 上海",
        )
        first_run = discovery.start_discovery_run("shixiseng", "产品实习 上海")
        first = discovery.complete_discovery_run(
            first_run,
            CollectorResult(
                platform="shixiseng",
                keyword="产品实习 上海",
                items=[item],
            ),
        )
        second_run = discovery.start_discovery_run("shixiseng", "产品实习 上海")
        second = discovery.complete_discovery_run(
            second_run,
            CollectorResult(
                platform="shixiseng",
                keyword="产品实习 上海",
                items=[item],
            ),
        )
        self.assertEqual(first.inserted, 1)
        self.assertEqual(len(first.new_item_ids), 1)
        self.assertEqual(first.score_candidate_ids, first.new_item_ids)
        self.assertEqual(second.inserted, 0)
        self.assertEqual(second.duplicates, 1)
        self.assertEqual(second.score_candidate_ids, first.new_item_ids)
        self.assertEqual(len(discovery.load_discovery_items()), 1)
        self.assertEqual(len(database.load_applications()), 0)
        self.assertEqual(
            discovery.list_unscored_discovery_item_ids(),
            first.new_item_ids,
        )

    def test_private_font_glyphs_are_hidden_from_list_and_scoring_text(self):
        item = CollectedItem(
            platform="shixiseng",
            external_id="font-obfuscated",
            url="https://www.shixiseng.com/intern/font-obfuscated",
            title="\ue2d7\uef42产品实习\uee10（\uf7f4\ue2af）",
            raw_text="职位描述：参与\ue346行业研究。\n任职要求：每周5天。",
        )
        run_id = discovery.start_discovery_run("shixiseng", "产品实习")
        discovery.complete_discovery_run(
            run_id,
            CollectorResult(
                platform="shixiseng",
                keyword="产品实习",
                items=[item],
            ),
        )
        listed = discovery.load_discovery_items().iloc[0]
        detail = discovery.get_discovery_item(int(listed["线索ID"]))
        self.assertEqual(listed["职位"], "产品实习")
        self.assertNotRegex(detail["raw_text"], r"[\uE000-\uF8FF]")
        self.assertGreater(detail["private_glyph_count"], 0)
        with sqlite3.connect(database.DB_PATH) as conn:
            stored = conn.execute(
                "SELECT raw_text FROM discovery_items WHERE id = ?",
                (int(listed["线索ID"]),),
            ).fetchone()[0]
        self.assertIn("\ue346", stored)

    def test_summary_is_saved_for_review_but_never_auto_scored(self):
        item = CollectedItem(
            platform="shixiseng",
            external_id="summary-only",
            url="https://www.shixiseng.com/intern/summary-only",
            title="产品实习生",
            company="示例科技",
            role="产品实习生",
            location="上海",
            raw_text=(
                "职位：产品实习生\n公司：示例科技\n城市：上海\n学历：本科\n"
                "信息范围：实习僧官方岗位列表摘要；打开原始页面可查看完整职责。"
            ),
        )
        run_id = discovery.start_discovery_run("shixiseng", "产品实习")
        summary = discovery.complete_discovery_run(
            run_id,
            CollectorResult(platform="shixiseng", keyword="产品实习", items=[item]),
        )

        self.assertEqual(summary.score_candidate_ids, [])
        self.assertEqual(summary.incomplete_items, 1)
        listed = discovery.load_discovery_items().iloc[0]
        self.assertEqual(listed["状态代码"], "needs_details")
        self.assertEqual(listed["数据状态"], "仅列表摘要")
        self.assertTrue(discovery.list_unscored_discovery_item_ids() == [])

    def test_richer_duplicate_recovers_metadata_and_becomes_scorable(self):
        first = CollectedItem(
            platform="shixiseng",
            external_id="upgrade-me",
            url="https://www.shixiseng.com/intern/upgrade-me",
            title="产品实习生",
            raw_text="职位：产品实习生\n信息范围：实习僧官方岗位列表摘要。",
        )
        first_run = discovery.start_discovery_run("shixiseng", "产品实习")
        saved = discovery.complete_discovery_run(
            first_run,
            CollectorResult(platform="shixiseng", keyword="产品实习", items=[first]),
        )
        item_id = saved.new_item_ids[0]

        richer = first.model_copy(update={"raw_text": full_job_text("升级科技", "北京")})
        second_run = discovery.start_discovery_run("shixiseng", "产品实习")
        upgraded = discovery.complete_discovery_run(
            second_run,
            CollectorResult(platform="shixiseng", keyword="产品实习", items=[richer]),
        )
        detail = discovery.get_discovery_item(item_id)

        self.assertEqual(upgraded.score_candidate_ids, [item_id])
        self.assertEqual(detail["content_level"], "full")
        self.assertEqual(detail["company"], "升级科技")
        self.assertEqual(detail["location"], "北京")

    def test_manual_jd_enrichment_invalidates_old_score_and_becomes_scorable(self):
        item = CollectedItem(
            platform="shixiseng",
            external_id="manual-enrichment",
            url="https://www.shixiseng.com/intern/manual-enrichment",
            title="AI 产品实习生",
            raw_text="职位：AI 产品实习生\n信息范围：岗位列表摘要。",
        )
        run_id = discovery.start_discovery_run("shixiseng", "AI 产品实习")
        saved = discovery.complete_discovery_run(
            run_id,
            CollectorResult(
                platform="shixiseng",
                keyword="AI 产品实习",
                items=[item],
            ),
        )
        item_id = saved.new_item_ids[0]
        with sqlite3.connect(database.DB_PATH) as conn:
            cursor = conn.execute(
                """
                INSERT INTO discovery_evaluations(
                    discovery_item_id, evaluation_run_id, resume_version_id,
                    rubric_version, fit_score, gate_result, recommendation,
                    jd_json, eligibility_json, evaluation_json, created_at
                ) VALUES (?, 1, 1, 'v2-old', 100, '满足', '优先投递',
                          '{}', '{}', '{}', '2026-08-21T10:00:00')
                """,
                (item_id,),
            )
            conn.execute(
                "UPDATE discovery_items SET latest_evaluation_id = ? WHERE id = ?",
                (int(cursor.lastrowid), item_id),
            )

        result = discovery.update_discovery_content(
            item_id,
            raw_text=full_job_text("补全科技", "北京"),
            role="AI 产品实习生",
        )
        detail = discovery.get_discovery_item(item_id)

        self.assertTrue(result["scorable"])
        self.assertTrue(result["content_changed"])
        self.assertEqual(result["completeness_score"], 100)
        self.assertEqual(detail["content_level"], "full")
        self.assertEqual(detail["company"], "补全科技")
        self.assertEqual(detail["location"], "北京")
        self.assertEqual(detail["content_source"], "manual")
        self.assertEqual(detail["status"], "new")
        self.assertIsNone(detail["latest_evaluation_id"])
        self.assertIn(item_id, discovery.list_unscored_discovery_item_ids())

    def test_manual_jd_enrichment_keeps_incomplete_text_out_of_scoring(self):
        item = CollectedItem(
            platform="shixiseng",
            external_id="manual-still-partial",
            url="https://www.shixiseng.com/intern/manual-still-partial",
            title="产品实习生",
            raw_text="职位：产品实习生\n信息范围：岗位列表摘要。",
        )
        run_id = discovery.start_discovery_run("shixiseng", "产品实习")
        saved = discovery.complete_discovery_run(
            run_id,
            CollectorResult(
                platform="shixiseng",
                keyword="产品实习",
                items=[item],
            ),
        )
        item_id = saved.new_item_ids[0]

        result = discovery.update_discovery_content(
            item_id,
            raw_text=(
                "岗位职责：负责用户调研、需求分析、产品方案设计、项目推进和数据复盘。\n"
                "工作地点：上海。公司：信息不完整测试科技。\n"
                "这是从原始页面复制的部分岗位介绍，但页面没有提供任职要求。"
            ),
        )

        self.assertFalse(result["scorable"])
        self.assertEqual(discovery.get_discovery_item(item_id)["status"], "needs_details")
        self.assertNotIn(item_id, discovery.list_unscored_discovery_item_ids())

    def test_dismissed_item_leaves_default_list_and_can_be_restored(self):
        item = CollectedItem(
            platform="boss",
            external_id="dismiss-me",
            url="https://www.zhipin.com/job_detail/dismiss-me",
            title="不合适的岗位",
            raw_text="岗位职责：测试。",
        )
        run_id = discovery.start_discovery_run("boss", "测试")
        discovery.complete_discovery_run(
            run_id,
            CollectorResult(platform="boss", keyword="测试", items=[item]),
        )
        item_id = int(discovery.load_discovery_items().iloc[0]["线索ID"])
        discovery.update_discovery_status(item_id, "dismissed")
        visible = discovery.load_discovery_items(statuses=["new"])
        dismissed = discovery.load_discovery_items(statuses=["dismissed"])
        self.assertTrue(visible.empty)
        self.assertEqual(int(dismissed.iloc[0]["线索ID"]), item_id)
        discovery.update_discovery_status(item_id, "new")
        self.assertEqual(
            int(discovery.load_discovery_items(statuses=["new"]).iloc[0]["线索ID"]),
            item_id,
        )

    def test_failed_platform_run_does_not_delete_existing_items(self):
        item = CollectedItem(
            platform="boss",
            external_id="boss-1",
            url="https://www.zhipin.com/job_detail/boss-1",
            title="产品实习生",
            raw_text="产品实习生岗位详情",
        )
        run_id = discovery.start_discovery_run("boss", "产品实习")
        discovery.complete_discovery_run(
            run_id,
            CollectorResult(platform="boss", keyword="产品实习", items=[item]),
        )
        failed_run = discovery.start_discovery_run("xiaohongshu", "产品实习")
        discovery.complete_discovery_run(
            failed_run,
            CollectorResult(
                platform="xiaohongshu",
                keyword="产品实习",
                error="验证码",
                verification_required=True,
            ),
        )
        items = discovery.load_discovery_items()
        self.assertEqual(len(items), 1)
        runs = discovery.load_discovery_runs()
        self.assertIn("failed", runs["状态"].tolist())

    def test_scored_item_still_requires_manual_shortlist_before_ledger(self):
        item = CollectedItem(
            platform="shixiseng",
            external_id="intern-confirm",
            url="https://www.shixiseng.com/intern/intern-confirm",
            title="产品实习生",
            company="示例科技",
            role="产品实习生",
            location="上海",
            raw_text=full_job_text(),
        )
        run_id = discovery.start_discovery_run("shixiseng", "产品实习")
        discovery.complete_discovery_run(
            run_id,
            CollectorResult(
                platform="shixiseng",
                keyword="产品实习",
                items=[item],
            ),
        )
        item_id = int(discovery.load_discovery_items().iloc[0]["线索ID"])
        resume, _ = resumes.save_resume_version(
            resume_name="产品版",
            filename="resume.txt",
            mime_type="text/plain",
            raw=("真实产品项目经历" * 10).encode(),
            set_default=True,
        )
        _, profile_version_id, _, _ = load_default_profile(database.DB_PATH)
        evaluation_run_id = save_evaluation_run(
            database.DB_PATH,
            jd_hash="discovery-evaluation",
            resume_hash=resume.sha256,
            profile_version_id=profile_version_id,
            job_override={},
            input_snapshot={},
            rubric_version="v2-test",
            model="test",
            output={},
            resume_version_id=resume.id,
            company="示例科技",
            role="产品实习生",
            jd_text=item.raw_text,
            fit_score=80,
            gate_result="满足",
            recommendation="建议投递",
        )
        discovery.save_discovery_evaluation(
            item_id=item_id,
            evaluation_run_id=evaluation_run_id,
            resume_version_id=resume.id,
            rubric_version="v2-test",
            fit_score=80,
            gate_result="满足",
            recommendation="建议投递",
            jd_json={
                "company": "示例科技",
                "role": "产品实习生",
                "full_text": item.raw_text,
            },
            eligibility_json={"checks": []},
            evaluation_json={"recommendation": "建议投递"},
        )
        imported, message = discovery.import_to_application(item_id)
        self.assertFalse(imported)
        self.assertIn("人工点击", message)
        self.assertEqual(len(database.load_applications()), 0)
        discovery.update_discovery_status(item_id, "shortlisted")
        imported, _ = discovery.import_to_application(item_id)
        self.assertTrue(imported)
        self.assertEqual(len(database.load_applications()), 1)

        rescored_run_id = save_evaluation_run(
            database.DB_PATH,
            jd_hash="discovery-evaluation-rescored",
            resume_hash=resume.sha256,
            profile_version_id=profile_version_id,
            job_override={},
            input_snapshot={},
            rubric_version="v2-test-new",
            model="test",
            output={},
            resume_version_id=resume.id,
            company="示例科技",
            role="产品实习生",
            jd_text=item.raw_text,
            fit_score=72,
            gate_result="存疑",
            recommendation="谨慎投递",
        )
        discovery.save_discovery_evaluation(
            item_id=item_id,
            evaluation_run_id=rescored_run_id,
            resume_version_id=resume.id,
            rubric_version="v2-test-new",
            fit_score=72,
            gate_result="存疑",
            recommendation="谨慎投递",
            jd_json={
                "company": "示例科技",
                "role": "产品实习生",
                "full_text": item.raw_text,
            },
            eligibility_json={"checks": []},
            evaluation_json={"recommendation": "谨慎投递"},
        )
        rescored_detail = discovery.get_discovery_item(item_id)
        self.assertEqual(rescored_detail["status"], "imported")
        application = database.load_applications().iloc[0]
        self.assertEqual(int(application["匹配分"]), 72)
        self.assertEqual(application["投递建议"], "谨慎投递")


if __name__ == "__main__":
    unittest.main()
