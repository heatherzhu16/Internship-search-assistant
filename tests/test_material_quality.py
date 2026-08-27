import unittest

from models.evaluation import ResumeRewrite
from services.scoring import _valid_resume_rewrites


class MaterialQualityTests(unittest.TestCase):
    def test_rejects_unchanged_heading_only_rewrite(self):
        resume = (
            "示例证券研究所/金融组实习生 2025.11-2026.01\n"
            "底稿维护：基于Wind数据库，运用Excel函数维护周报底稿数据库，"
            "涵盖15项核心指标并确保口径一致。"
        )
        items = [
            ResumeRewrite(
                location="实习经历",
                original="示例证券研究所/金融组实习生 2025.11-2026.01",
                suggested="示例证券研究所/金融组实习生 2025.11-2026.01",
                rationale="突出相关性",
            )
        ]
        self.assertEqual(_valid_resume_rewrites(items, resume), [])

    def test_keeps_materially_changed_verbatim_bullet(self):
        original = (
            "底稿维护：基于Wind数据库，运用Excel函数维护周报底稿数据库，"
            "涵盖15项核心指标并确保口径一致。"
        )
        suggested = (
            "底稿维护：运用Excel函数维护涵盖15项核心指标的周报底稿数据库，"
            "统一数据口径并提升时间序列可比性。"
        )
        items = [
            ResumeRewrite(
                location="示例证券研究所",
                original=original,
                suggested=suggested,
                rationale="把工具、数据规模和结果放在同一条职责中。",
            )
        ]
        self.assertEqual(len(_valid_resume_rewrites(items, original)), 1)


if __name__ == "__main__":
    unittest.main()
