from __future__ import annotations

import unittest
from unittest.mock import patch

from services.browser_launcher import open_in_chrome


class BrowserLauncherTests(unittest.TestCase):
    def test_rejects_non_http_urls(self):
        with self.assertRaisesRegex(ValueError, "HTTP"):
            open_in_chrome("javascript:alert(1)")

    @patch("services.browser_launcher.subprocess.run")
    @patch("services.browser_launcher.chrome_available", return_value=True)
    def test_opens_valid_url_with_chrome(self, _available, run):
        open_in_chrome("https://www.zhipin.com/web/geek/job?query=test")

        run.assert_called_once_with(
            [
                "open",
                "-a",
                "Google Chrome",
                "https://www.zhipin.com/web/geek/job?query=test",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )

    @patch("services.browser_launcher.chrome_available", return_value=False)
    def test_reports_missing_chrome(self, _available):
        with self.assertRaisesRegex(RuntimeError, "未检测到"):
            open_in_chrome("https://www.xiaohongshu.com/search_result?keyword=test")


if __name__ == "__main__":
    unittest.main()
