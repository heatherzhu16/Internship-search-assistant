import unittest

from models.evaluation import JDInfo
from services.scoring import normalize_jd_contact_fields


class JDContactTests(unittest.TestCase):
    def test_moves_jd_email_out_of_source(self):
        result = normalize_jd_contact_fields(
            JDInfo(source="talent@example.com"),
            "请将简历发送至 Talent@Example.com，邮件主题注明岗位。",
        )
        self.assertEqual(result.application_email, "talent@example.com")
        self.assertEqual(result.source, "")

    def test_keeps_real_source_and_extracts_email(self):
        result = normalize_jd_contact_fields(
            JDInfo(source="小红书"),
            "投递邮箱 jobs@example.com",
        )
        self.assertEqual(result.application_email, "jobs@example.com")
        self.assertEqual(result.source, "小红书")


if __name__ == "__main__":
    unittest.main()
