import json
import unittest
from unittest.mock import Mock, patch

from services import credential_store


class CredentialStoreTests(unittest.TestCase):
    @patch("services.credential_store.shutil.which", return_value="/usr/bin/security")
    @patch("services.credential_store.subprocess.run")
    def test_loads_credentials_from_keychain(self, run, _which):
        run.return_value = Mock(
            returncode=0,
            stdout=json.dumps(
                {
                    "address": "candidate@163.com",
                    "authorization_code": "secret-code",
                }
            ),
            stderr="",
        )
        self.assertEqual(
            credential_store.load_email_credentials(),
            ("candidate@163.com", "secret-code"),
        )

    @patch("services.credential_store.shutil.which", return_value="/usr/bin/security")
    @patch("services.credential_store.subprocess.run")
    def test_saves_to_named_keychain_service(self, run, _which):
        run.return_value = Mock(returncode=0, stdout="", stderr="")
        credential_store.save_email_credentials(
            "candidate@163.com", "secret-code"
        )
        command = run.call_args.args[0]
        self.assertIn(credential_store.KEYCHAIN_SERVICE, command)
        self.assertNotIn("job_search.db", command)

    @patch("services.credential_store.shutil.which", return_value="/usr/bin/security")
    @patch("services.credential_store.subprocess.run")
    def test_loads_deepseek_credentials_from_keychain(self, run, _which):
        run.return_value = Mock(
            returncode=0,
            stdout=json.dumps(
                {"api_key": "sk-test-key", "model": "deepseek-chat"}
            ),
            stderr="",
        )
        self.assertEqual(
            credential_store.load_deepseek_credentials(),
            ("sk-test-key", "deepseek-chat"),
        )

    @patch("services.credential_store.shutil.which", return_value="/usr/bin/security")
    @patch("services.credential_store.subprocess.run")
    def test_saves_deepseek_to_separate_keychain_service(self, run, _which):
        run.return_value = Mock(returncode=0, stdout="", stderr="")
        credential_store.save_deepseek_credentials(
            "sk-test-key", "deepseek-chat"
        )
        command = run.call_args.args[0]
        self.assertIn(credential_store.DEEPSEEK_KEYCHAIN_SERVICE, command)
        self.assertNotIn(credential_store.KEYCHAIN_SERVICE, command)


if __name__ == "__main__":
    unittest.main()
