import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from candidate_profile import AvailabilityWindow, CandidateProfileData
from profile_store import (
    ensure_default_profile,
    init_profile_tables,
    load_default_profile,
    save_evaluation_run,
    save_profile_version,
)


class ProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        init_profile_tables(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_profile_versions_are_persistent(self):
        profile_id = ensure_default_profile(self.db_path)
        profile = CandidateProfileData(
            availability_windows=[
                AvailabilityWindow(
                    start_date=date(2026, 8, 1),
                    end_date=date(2026, 12, 31),
                    days_per_week=4,
                )
            ]
        )
        _, version_no, created = save_profile_version(
            self.db_path, profile_id, profile, "test"
        )
        _, _, loaded_version_no, loaded = load_default_profile(self.db_path)
        self.assertTrue(created)
        self.assertEqual(version_no, 2)
        self.assertEqual(loaded_version_no, 2)
        self.assertEqual(loaded.availability_windows[0].days_per_week, 4)

    def test_evaluation_run_stores_snapshot(self):
        ensure_default_profile(self.db_path)
        _, version_id, _, _ = load_default_profile(self.db_path)
        run_id = save_evaluation_run(
            self.db_path,
            jd_hash="jd",
            resume_hash="resume",
            profile_version_id=version_id,
            job_override={"enabled": False},
            input_snapshot={"candidate_context": {"facts": {}}},
            rubric_version="test",
            model="test-model",
            output={"score": 80},
        )
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT input_snapshot_json FROM evaluation_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        self.assertEqual(
            json.loads(row[0])["candidate_context"]["facts"],
            {},
        )


if __name__ == "__main__":
    unittest.main()

