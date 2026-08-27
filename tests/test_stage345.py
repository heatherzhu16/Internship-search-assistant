import sqlite3
import tempfile
import unittest
from pathlib import Path

import services.database as database
import services.resume_service as resumes


class Stage345Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.original_db = database.DB_PATH
        self.original_backup = database.BACKUP_DIR
        self.original_resume_db = resumes.DB_PATH
        self.original_resume_dir = resumes.RESUME_DIR
        database.DB_PATH = self.root / "job_search.db"
        database.BACKUP_DIR = self.root / "backups"
        resumes.DB_PATH = database.DB_PATH
        resumes.RESUME_DIR = self.root / "resumes"

    def tearDown(self):
        database.DB_PATH = self.original_db
        database.BACKUP_DIR = self.original_backup
        resumes.DB_PATH = self.original_resume_db
        resumes.RESUME_DIR = self.original_resume_dir
        self.temp_dir.cleanup()

    def test_migration_preserves_application_and_backfills_event(self):
        with sqlite3.connect(database.DB_PATH) as conn:
            conn.execute(
                """
                CREATE TABLE applications (
                    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                    status TEXT, highest_stage TEXT, resume_version TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO applications(id, created_at, status, highest_stage, resume_version)
                VALUES (1, '2026-07-27T10:00:00', '拒绝', '二面', '产品版')
                """
            )
        database.init_database()
        with sqlite3.connect(database.DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0], 1)
            self.assertEqual(database.current_status(conn, 1), "拒绝")
            self.assertEqual(database.highest_stage(conn, 1), "二面")

    def test_events_keep_highest_stage_and_can_be_undone(self):
        database.init_database()
        created, application_id = database.save_application(
            fields={"status": "待投递", "company": "示例", "role": "产品"},
            jd_hash="unique-job",
            jd_text="JD",
            evaluation_json="{}",
            score=80,
            recommendation="建议投递",
            resume_version_id=None,
            analysis_run_id=None,
        )
        self.assertTrue(created)
        database.append_application_event(application_id, "一面", source="手动确认")
        database.append_application_event(application_id, "拒绝", source="邮件同步")
        data = database.load_applications()
        self.assertEqual(data.iloc[0]["投递状态"], "拒绝")
        self.assertEqual(data.iloc[0]["最高进展"], "一面")
        duplicate, _ = database.append_application_event(
            application_id, "拒绝", source="邮件同步"
        )
        self.assertFalse(duplicate)
        events = database.load_events(application_id)
        rejection_id = int(events.loc[events["新状态"] == "拒绝", "事件ID"].iloc[0])
        success, _ = database.undo_event(rejection_id)
        self.assertTrue(success)
        self.assertEqual(database.load_applications().iloc[0]["投递状态"], "一面")

    def test_resume_versions_are_immutable_and_default_is_reloaded(self):
        database.init_database()
        first, first_created = resumes.save_resume_version(
            resume_name="产品实习版",
            filename="resume.txt",
            mime_type="text/plain",
            raw=("第一版真实简历内容" * 10).encode(),
            set_default=True,
        )
        second, second_created = resumes.save_resume_version(
            resume_name="产品实习版",
            filename="resume.txt",
            mime_type="text/plain",
            raw=("第二版真实简历内容" * 10).encode(),
        )
        self.assertTrue(first_created)
        self.assertTrue(second_created)
        self.assertEqual(first.version_no, 1)
        self.assertEqual(second.version_no, 2)
        self.assertEqual(resumes.default_resume_version().id, second.id)
        self.assertIn("第一版", resumes.get_resume_version(first.id).extracted_text)


if __name__ == "__main__":
    unittest.main()
