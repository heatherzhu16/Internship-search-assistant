from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


CHROME_APP_PATHS = (
    Path("/Applications/Google Chrome.app"),
    Path.home() / "Applications/Google Chrome.app",
)


def chrome_available() -> bool:
    return sys.platform == "darwin" and any(path.exists() for path in CHROME_APP_PATHS)


def open_in_chrome(url: str) -> None:
    """Open an explicit HTTP(S) URL in Chrome without changing the default browser."""
    target = str(url or "").strip()
    parsed = urlparse(target)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("只能用 Chrome 打开有效的 HTTP(S) 页面。")
    if not chrome_available():
        raise RuntimeError("未检测到 Google Chrome，请先安装或移动到“应用程序”目录。")
    try:
        subprocess.run(
            ["open", "-a", "Google Chrome", target],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("无法启动 Google Chrome，请确认 Chrome 可以正常打开。") from exc
