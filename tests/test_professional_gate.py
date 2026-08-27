from __future__ import annotations

import unittest

from candidate_profile import EligibilityAssessment
from services.professional_gate import augment_professional_eligibility


class ProfessionalGateTests(unittest.TestCase):
    def test_basic_sql_fails_explicit_proficiency_gate(self):
        result = augment_professional_eligibility(
            EligibilityAssessment(),
            jd_text="任职要求：熟练使用 SQL 取数及数据分析，为必要条件。",
            resume_text="技能：掌握 SQL 基础查询，熟练使用 Excel。",
        )
        self.assertEqual(result.unmet_count, 1)
        self.assertEqual(result.checks[0].result, "不满足")
        self.assertEqual(result.checks[0].constraint_type, "professional_skill")

    def test_optional_skill_does_not_create_gate(self):
        result = augment_professional_eligibility(
            EligibilityAssessment(),
            jd_text="有 CRM 系统使用经验者优先。",
            resume_text="未使用过 CRM。",
        )
        self.assertEqual(result.checks, [])

    def test_mentioned_skill_without_level_is_unknown(self):
        result = augment_professional_eligibility(
            EligibilityAssessment(),
            jd_text="必须熟练使用 Tableau 完成数据看板。",
            resume_text="技能列表：Tableau、Excel、Python。",
        )
        self.assertEqual(result.unknown_count, 1)

    def test_crm_in_responsibilities_is_not_automatically_a_gate(self):
        result = augment_professional_eligibility(
            EligibilityAssessment(),
            jd_text=(
                "工作职责\n熟练使用营销系统 / CRM 系统，对现有系统提出优化需求。\n"
                "任职要求\n本科及以上学历。"
            ),
            resume_text="没有 CRM 使用经历。",
        )
        self.assertEqual(result.checks, [])


if __name__ == "__main__":
    unittest.main()
