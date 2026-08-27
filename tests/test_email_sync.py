import tempfile
import unittest
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

import services.database as database
import services.email_sync as email_sync
from models.email import EmailConnectionConfig, NormalizedEmail


def normalized_email(
    *,
    direction: str,
    subject: str,
    body: str,
    to_addresses: list[str] | None = None,
    from_address: str = "",
    attachments: list[str] | None = None,
) -> NormalizedEmail:
    return NormalizedEmail(
        folder_name="已发送" if direction == "outgoing" else "求职同步",
        direction=direction,
        uid_validity=1,
        imap_uid=1,
        message_id="<test@example.com>",
        received_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        from_address=from_address,
        to_addresses=to_addresses or [],
        subject=subject,
        body_text=body,
        body_excerpt=body,
        body_hash="test-hash",
        attachment_names=attachments or [],
    )


class EmailSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = database.DB_PATH
        self.original_backup_dir = database.BACKUP_DIR
        self.original_email_path = email_sync.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "job_search.db"
        database.BACKUP_DIR = Path(self.temp_dir.name) / "backups"
        email_sync.DB_PATH = database.DB_PATH
        database.init_database()

    def tearDown(self):
        database.DB_PATH = self.original_database_path
        database.BACKUP_DIR = self.original_backup_dir
        email_sync.DB_PATH = self.original_email_path
        self.temp_dir.cleanup()

    def test_outgoing_resume_email_is_application_sent(self):
        message = normalized_email(
            direction="outgoing",
            subject="应聘产品实习生-张三",
            body="您好，附件是我的求职简历。",
            to_addresses=["jobs@example.com"],
            attachments=["张三-简历.pdf"],
        )
        result = email_sync.classify_email(message)
        self.assertEqual(result.classification, "application_sent")
        self.assertEqual(result.suggested_status, "已投递")
        self.assertGreaterEqual(result.confidence, 0.9)

    def test_incoming_interview_without_round_requires_confirmation(self):
        message = normalized_email(
            direction="incoming",
            subject="面试邀请",
            body="邀请您参加产品实习生岗位面试，请确认时间。",
            from_address="jobs@example.com",
        )
        result = email_sync.classify_email(message)
        self.assertEqual(result.classification, "interview_unknown")
        self.assertEqual(result.suggested_status, "")

    def test_mime_message_parser_keeps_attachment_name(self):
        message = EmailMessage()
        message["From"] = "me@163.com"
        message["To"] = "jobs@example.com"
        message["Subject"] = "应聘产品实习生"
        message["Message-ID"] = "<mime-test@example.com>"
        message.set_content("您好，附件是我的简历。")
        message.add_attachment(
            b"pdf",
            maintype="application",
            subtype="pdf",
            filename="产品实习简历.pdf",
        )
        parsed = email_sync.parse_message(
            message.as_bytes(),
            folder_name="已发送",
            direction="outgoing",
            uid_validity=1,
            imap_uid=2,
        )
        self.assertIn("产品实习简历.pdf", parsed.attachment_names)
        self.assertIn("jobs@example.com", parsed.to_addresses)

    def test_manual_confirmation_appends_and_can_undo_event(self):
        created, application_id = database.save_application(
            fields={
                "status": "待投递",
                "company": "示例科技",
                "role": "产品实习生",
                "application_email": "jobs@example.com",
            },
            jd_hash="email-stage-job",
            jd_text="JD",
            evaluation_json="{}",
            score=80,
            recommendation="建议投递",
            resume_version_id=None,
            analysis_run_id=None,
        )
        self.assertTrue(created)
        message = normalized_email(
            direction="outgoing",
            subject="应聘产品实习生",
            body="您好，附件是简历。",
            to_addresses=["jobs@example.com"],
            attachments=["简历.pdf"],
        )
        classification = email_sync.classify_email(message)
        match = email_sync.match_application(message)
        self.assertEqual(match.application_id, application_id)
        account_id = email_sync.save_account_settings(
            EmailConnectionConfig(
                address="candidate@163.com",
                authorization_code="not-stored",
            )
        )
        email_id = email_sync._insert_message(
            account_id, message, classification, match, "pending"
        )
        confirmed, _ = email_sync.confirm_email_message(
            email_id, application_id, "已投递"
        )
        self.assertTrue(confirmed)
        self.assertEqual(
            database.load_applications().iloc[0]["投递状态"], "已投递"
        )
        undone, _ = email_sync.undo_email_message(email_id)
        self.assertTrue(undone)
        self.assertEqual(
            database.load_applications().iloc[0]["投递状态"], "待投递"
        )

    def test_creates_reviewable_draft_with_resume_and_deduplicates(self):
        class FakeImap:
            def __init__(self):
                self.appended = []

            def list(self):
                return "OK", [b'(\\Drafts) "/" "Drafts"']

            def append(self, mailbox, flags, internal_date, raw):
                self.appended.append((mailbox, flags, internal_date, raw))
                return "OK", [b"1"]

            def logout(self):
                return "BYE", []

        fake = FakeImap()
        config = EmailConnectionConfig(
            address="candidate@163.com",
            authorization_code="not-stored",
            draft_folder="Drafts",
        )
        with patch.object(email_sync, "_connect", return_value=fake):
            created, _ = email_sync.create_email_draft(
                config,
                to_address="jobs@example.com",
                subject="应聘产品实习生",
                body="您好，请查收附件。",
                attachment_bytes=b"pdf-content",
                attachment_filename="产品实习版.pdf",
                attachment_mime_type="application/pdf",
            )
        self.assertTrue(created)
        self.assertEqual(len(fake.appended), 1)
        parsed = EmailMessage()
        from email import policy
        from email.parser import BytesParser
        parsed = BytesParser(policy=policy.default).parsebytes(fake.appended[0][3])
        self.assertEqual(parsed["To"], "jobs@example.com")
        self.assertIn("产品实习版.pdf", [
            part.get_filename() for part in parsed.iter_attachments()
        ])

        with patch.object(email_sync, "_connect", return_value=fake):
            created_again, message = email_sync.create_email_draft(
                config,
                to_address="jobs@example.com",
                subject="应聘产品实习生",
                body="您好，请查收附件。",
                attachment_bytes=b"pdf-content",
                attachment_filename="产品实习版.pdf",
                attachment_mime_type="application/pdf",
            )
        self.assertFalse(created_again)
        self.assertIn("没有重复创建", message)


if __name__ == "__main__":
    unittest.main()
