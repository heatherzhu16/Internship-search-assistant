import unittest
from unittest.mock import patch

from candidate_profile import EligibilityAssessment, EligibilityCheck, JDConstraint
from models.evaluation import DimensionScore, Evaluation, JDInfo
from services.discovery_scoring import prepare_discovery_jd_text
from services.scoring import (
    _normalize_professional_gap,
    calibrate_evaluation_dimensions,
    extract_jd,
    get_client,
    total_score,
)


def dimension(score: int, maximum: int, evidence=None, gaps=None):
    return DimensionScore(
        score=score,
        max_score=maximum,
        evidence=evidence or [],
        gaps=gaps or [],
    )


class ScoringV22Tests(unittest.TestCase):
    def test_first_internet_internship_welcome_is_not_a_core_gap(self):
        jd = JDInfo(
            full_text=(
                "有互联网战略/商分实习经历优先，"
                "也欢迎希望积累首段互联网实习经验的同学。"
            )
        )
        self.assertIsNone(
            _normalize_professional_gap("核心缺口：缺少互联网战略实习经历", jd)
        )

    def test_rejects_non_ascii_api_key_before_http_request(self):
        with self.assertRaisesRegex(ValueError, "含有中文"):
            get_client("这里不是API Key")

    def test_rejects_incomplete_api_key_before_http_request(self):
        with self.assertRaisesRegex(ValueError, "格式不正确"):
            get_client("not-a-deepseek-key")

    def test_blank_hallucinated_constraints_are_discarded(self):
        extracted = JDInfo(
            constraints=[
                JDConstraint(
                    constraint_type="arrival_date",
                    value="",
                    quote="",
                ),
                JDConstraint(
                    constraint_type="work_mode",
                    value="现场",
                    quote="",
                ),
                JDConstraint(
                    constraint_type="days_per_week",
                    value="5",
                    quote="5天／周",
                ),
            ]
        )
        with patch("services.scoring.call_json", return_value=extracted):
            result = extract_jd(None, "test", "岗位要求：5天／周")
        self.assertEqual(len(result.constraints), 1)
        self.assertEqual(result.constraints[0].constraint_type, "days_per_week")

    def test_arrival_month_range_uses_end_of_range(self):
        extracted = JDInfo(
            constraints=[
                JDConstraint(
                    constraint_type="arrival_date",
                    value="2026-08-01",
                    quote="预计入职时间：2026年8—9月",
                )
            ]
        )
        with patch("services.scoring.call_json", return_value=extracted):
            result = extract_jd(None, "test", "预计入职时间：2026年8—9月")
        self.assertEqual(result.constraints[0].value, "2026-09-30")
        self.assertEqual(result.constraints[0].operator, "不晚于")

    def test_unknown_salary_constraint_is_tolerated_and_discarded(self):
        extracted = JDInfo.model_validate(
            {
                "salary": "200-300元/天",
                "constraints": [
                    {
                        "constraint_type": "salary",
                        "value": "200-300",
                        "unit": "元/天",
                        "importance": "hard",
                        "quote": "薪资200-300元/天",
                    },
                    {
                        "constraint_type": "weekly-days",
                        "value": "5",
                        "unit": "天/周",
                        "importance": "required",
                        "quote": "每周实习5天",
                    },
                ],
            }
        )
        self.assertEqual(extracted.constraints[0].constraint_type, "other")
        self.assertEqual(
            extracted.constraints[1].constraint_type,
            "days_per_week",
        )
        with patch("services.scoring.call_json", return_value=extracted):
            result = extract_jd(
                None,
                "test",
                "薪资200-300元/天；每周实习5天",
            )
        self.assertEqual(result.salary, "200-300元/天")
        self.assertEqual(len(result.constraints), 1)
        self.assertEqual(result.constraints[0].constraint_type, "days_per_week")

    def test_discovery_text_keeps_job_metadata_and_removes_footer(self):
        raw = (
            "首页 金融战略实习生 150-200/天 北京 本科 4天／周 实习4个月 "
            "职位描述：参与战略规划。任职资格：英语流利。"
            "投递要求：简历要求中文 公司简介 滴滴 产品服务 联系我们"
        )
        cleaned = prepare_discovery_jd_text(raw, {"location": "北京"})
        self.assertIn("4天／周", cleaned)
        self.assertIn("实习4个月", cleaned)
        self.assertIn("参与战略规划", cleaned)
        self.assertNotIn("联系我们", cleaned)

    def test_calibration_removes_logistics_and_wrong_cohort_penalties(self):
        evaluation = Evaluation(
            hard_requirements=dimension(
                0,
                25,
                gaps=["未明确到岗时间", "27届优先但为26届"],
            ),
            experience_match=dimension(
                16,
                25,
                evidence=["经历A → 行业研究", "经历B → 竞对分析", "经历C → 数据分析"],
            ),
            skills_tools=dimension(
                15,
                20,
                evidence=["工具A → 数据分析", "工具B → 实证研究"],
            ),
            motivation=dimension(
                5,
                10,
                evidence=["战略求职意向 → 战略岗位", "科技课程 → 科技行业兴趣"],
            ),
            education=dimension(
                6,
                10,
                evidence=["示例财经大学金融学本科 → 本科在校"],
                gaps=["27届优先但为26届"],
            ),
            keyword_evidence=dimension(
                6,
                10,
                evidence=[
                    "战略 → 战略岗位",
                    "行业研究 → 行业研究",
                    "竞对分析 → 竞对分析",
                    "数据分析 → 数据分析",
                ],
            ),
            hard_gate_result="存疑",
            recommendation="不建议投递",
            recommendation_reason="",
        )
        eligibility = EligibilityAssessment(
            checks=[
                EligibilityCheck(
                    constraint_type="education",
                    requirement="本科在校",
                    result="满足",
                    candidate_evidence="2027届本科在校",
                )
            ]
        )
        calibrated = calibrate_evaluation_dimensions(
            evaluation,
            JDInfo(),
            eligibility,
            {"facts": {"graduation_date": "2027-06-30"}},
        )
        self.assertGreaterEqual(total_score(calibrated), 70)
        self.assertEqual(calibrated.raw_dimension_scores["hard_requirements"], 0)
        self.assertTrue(calibrated.calibration_notes)
        self.assertFalse(any("到岗" in gap for gap in calibrated.hard_requirements.gaps))
        self.assertFalse(any("26届" in gap for gap in calibrated.education.gaps))

    def test_full_scores_are_rejected_when_explanations_admit_gaps(self):
        evaluation = Evaluation(
            hard_requirements=dimension(25, 25, evidence=["本科在读 → 本科要求"]),
            experience_match=dimension(
                25, 25,
                evidence=["咨询研究经历 → 能力可能迁移到产品分析"],
                gaps=["缺乏直接产品实习经历"],
            ),
            skills_tools=dimension(
                20, 20,
                evidence=["Python → 可迁移到产品数据分析"],
                gaps=["缺少产品原型工具"],
            ),
            motivation=dimension(
                10, 10,
                evidence=["运营意向 → 与产品岗位部分相关"],
                gaps=["没有直接产品动机证据"],
            ),
            education=dimension(
                10, 10,
                evidence=["金融学本科 → 满足本科学历"],
                gaps=["不是计算机相关专业"],
            ),
            keyword_evidence=dimension(
                10, 10,
                evidence=["数据分析 → 与产品工作间接相关"],
                gaps=["缺少用户、需求和原型证据"],
            ),
            hard_gate_result="满足",
            recommendation="优先投递",
            recommendation_reason="",
        )

        calibrated = calibrate_evaluation_dimensions(
            evaluation,
            JDInfo(),
            EligibilityAssessment(),
            {},
        )

        self.assertLess(total_score(calibrated), 100)
        self.assertEqual(calibrated.experience_match.score, 13)
        self.assertEqual(calibrated.skills_tools.score, 11)
        self.assertTrue(any("间接" in note for note in calibrated.calibration_notes))

    def test_many_transferable_evidence_items_do_not_create_a_high_floor(self):
        evaluation = Evaluation(
            hard_requirements=dimension(5, 25, evidence=["可迁移证据：研究经历 → 产品理解"] * 4),
            experience_match=dimension(5, 25, evidence=["可迁移证据：数据分析 → 需求分析"] * 4),
            skills_tools=dimension(4, 20, evidence=["可迁移证据：Excel → CRM 使用"] * 4),
            motivation=dimension(2, 10, evidence=["可迁移证据：AI 课程 → AI 产品兴趣"] * 4),
            education=dimension(2, 10, evidence=["可迁移证据：金融学 → 数学基础"] * 4),
            keyword_evidence=dimension(2, 10, evidence=["可迁移证据：分析 → 产品分析"] * 4),
            hard_gate_result="存疑",
            recommendation="不建议投递",
            recommendation_reason="",
        )

        calibrated = calibrate_evaluation_dimensions(
            evaluation,
            JDInfo(),
            EligibilityAssessment(),
            {},
        )

        self.assertEqual(total_score(calibrated), 20)
        self.assertLess(calibrated.experience_match.score, 15)

    def test_missing_direct_role_experience_caps_core_professional_score(self):
        evaluation = Evaluation(
            hard_requirements=dimension(25, 25, evidence=["本科在读 → 本科要求"]),
            experience_match=dimension(
                20,
                25,
                evidence=["可迁移证据：行业研究 → 产品调研"],
                gaps=["无产品方案和上线经验"],
            ),
            skills_tools=dimension(
                13,
                20,
                evidence=["直接证据：Excel → Excel"],
                gaps=["无医疗智能体知识"],
            ),
            motivation=dimension(4, 10, evidence=["可迁移证据：AI课程 → AI兴趣"]),
            education=dimension(7, 10, evidence=["金融学本科 → 本科"]),
            keyword_evidence=dimension(5, 10, evidence=["竞品分析 → 竞品调研"]),
            hard_gate_result="满足",
            recommendation="建议投递",
            recommendation_reason="",
        )

        calibrated = calibrate_evaluation_dimensions(
            evaluation,
            JDInfo(),
            EligibilityAssessment(),
            {},
        )

        self.assertEqual(calibrated.hard_requirements.score, 10)
        self.assertTrue(any("核心职能没有直接经历" in note for note in calibrated.calibration_notes))

    def test_multiple_core_duty_gaps_cap_optimistic_direct_labels(self):
        evaluation = Evaluation(
            hard_requirements=dimension(
                25,
                25,
                evidence=[
                    "直接证据：本科在读 → 本科及以上",
                    "直接证据：金融学 → 专业不限",
                    "直接证据：多段实习 → 有实习经历者优先",
                ],
            ),
            experience_match=dimension(
                20,
                25,
                evidence=[
                    "直接证据：行业研究 → 数据报告",
                    "直接证据：证券数据整理 → 数据分析",
                    "直接证据：竞品分析 → 决策支持",
                ],
                gaps=[
                    "核心缺口：无用户增长策略经验",
                    "核心缺口：无A/B测试和渠道投放经验",
                    "核心缺口：无增长指标迭代经验",
                ],
            ),
            skills_tools=dimension(
                16,
                20,
                evidence=["直接证据：Excel → Excel", "直接证据：SQL基础 → SQL"],
                gaps=["核心缺口：SQL仅为基础查询，未达到熟练要求"],
            ),
            motivation=dimension(6, 10, evidence=["运营意向 → 运营岗位"]),
            education=dimension(8, 10, evidence=["金融学 → 数据分析基础"]),
            keyword_evidence=dimension(8, 10, evidence=["数据分析 → 数据分析"]),
            hard_gate_result="不满足",
            recommendation="不建议投递",
            recommendation_reason="",
        )

        calibrated = calibrate_evaluation_dimensions(
            evaluation,
            JDInfo(),
            EligibilityAssessment(),
            {},
        )

        self.assertEqual(calibrated.hard_requirements.score, 10)
        self.assertEqual(calibrated.experience_match.score, 13)
        self.assertLessEqual(total_score(calibrated), 61)
        self.assertTrue(any("3项相互独立" in note for note in calibrated.calibration_notes))

    def test_related_coursework_does_not_turn_major_mismatch_into_high_score(self):
        evaluation = Evaluation(
            hard_requirements=dimension(
                25, 25,
                evidence=[
                    "直接证据：本科在读 → 本科要求",
                    "直接证据：金融学包含数学类课程 → 计算机/数学/医学类专业优先",
                ],
            ),
            experience_match=dimension(
                14, 25,
                evidence=[
                    "直接证据：竞品分析 → 竞品调研",
                    "可迁移证据：数据分析 → 产品需求对接",
                ],
                gaps=["核心缺口：无产品方案和研发上线经历"],
            ),
            skills_tools=dimension(
                10, 20,
                evidence=["直接证据：Office → Excel、Word", "可迁移证据：SQL → 医疗数据"],
                gaps=["核心缺口：无产品设计工具和智能体产品经验"],
            ),
            motivation=dimension(5, 10, evidence=["可迁移证据：AI赛道研究 → AI兴趣"]),
            education=dimension(
                6, 10,
                evidence=["本科在读 → 本科要求", "可迁移证据：数学课程 → 数学专业优先"],
                gaps=["加分项缺口：无计算机或医学专业背景"],
            ),
            keyword_evidence=dimension(
                5, 10,
                evidence=["直接证据：AI教育机器人 → AI", "无直接证据：医疗智能体"],
                gaps=["核心缺口：无智能体、产品和需求对接证据"],
            ),
            hard_gate_result="满足",
            recommendation="建议投递",
            recommendation_reason="",
        )
        eligibility = EligibilityAssessment(
            checks=[
                EligibilityCheck(
                    constraint_type="education",
                    requirement="本科及以上",
                    result="满足",
                    candidate_evidence="本科在读",
                )
            ]
        )
        calibrated = calibrate_evaluation_dimensions(
            evaluation,
            JDInfo(full_text="计算机、数学、医学类相关专业优先"),
            eligibility,
            {},
        )
        self.assertLessEqual(total_score(calibrated), 50)
        self.assertEqual(calibrated.hard_requirements.score, 10)
        self.assertEqual(calibrated.education.score, 6)
        self.assertEqual(calibrated.keyword_evidence.score, 5)

    def test_eligibility_evidence_does_not_fill_professional_core_score(self):
        evaluation = Evaluation(
            hard_requirements=dimension(
                25, 25,
                evidence=[
                    "直接证据：本科在读 → 本科及以上学历",
                    "直接证据：雅思7.5，可作为工作语言 → 英语要求",
                ],
            ),
            experience_match=dimension(
                20, 25,
                evidence=["直接证据：数据分析 → 用户增长分析", "直接证据：行业研究 → 策略设计"],
                gaps=["核心缺口：无营销活动策划经历"],
            ),
            skills_tools=dimension(
                12, 20,
                evidence=["直接证据：Excel → 数据分析"],
                gaps=["核心缺口：无CRM系统经验"],
            ),
            motivation=dimension(7, 10, evidence=["运营意向 → 用户运营"]),
            education=dimension(8, 10, evidence=["金融学 → 支付业务基础"]),
            keyword_evidence=dimension(8, 10, evidence=["数据分析 → 数据分析"]),
            hard_gate_result="满足",
            recommendation="建议投递",
            recommendation_reason="",
        )
        calibrated = calibrate_evaluation_dimensions(
            evaluation,
            JDInfo(full_text="本科以上，英语听说读写能力；负责海外支付用户运营。"),
            EligibilityAssessment(),
            {},
        )
        self.assertEqual(calibrated.hard_requirements.score, 18)
        self.assertEqual(total_score(calibrated), 73)

    def test_target_role_preference_does_not_create_motivation_floor(self):
        evaluation = Evaluation(
            hard_requirements=dimension(5, 25, evidence=["直接证据：流程执行 → 日常工作"]),
            experience_match=dimension(8, 25, evidence=["可迁移证据：数据整理 → 信息录入"]),
            skills_tools=dimension(8, 20, evidence=["直接证据：Excel → 表格整理"]),
            motivation=dimension(
                3, 10,
                evidence=["直接证据：求职意向为运营/战略 → 商品运营"],
                gaps=["缺乏采购或商品运营领域投入"],
            ),
            education=dimension(5, 10, evidence=["本科 → 学历"]),
            keyword_evidence=dimension(4, 10, evidence=["数据核对 → 数据核对"]),
            hard_gate_result="满足",
            recommendation="谨慎投递",
            recommendation_reason="",
        )
        calibrated = calibrate_evaluation_dimensions(
            evaluation, JDInfo(), EligibilityAssessment(), {}
        )
        self.assertEqual(calibrated.motivation.score, 3)


if __name__ == "__main__":
    unittest.main()
