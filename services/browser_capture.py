from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from models.discovery import CollectedItem, CollectorResult
from services.database import APP_DIR, init_database
from services.discovery_service import (
    complete_discovery_run,
    get_discovery_item,
    start_discovery_run,
)
from services.publication_date import normalize_posted_at
from services.discovery_quality import REQUIREMENT_MARKERS, RESPONSIBILITY_MARKERS


CAPTURE_HOST = "127.0.0.1"
CAPTURE_PORT = 8765
TOKEN_PATH = APP_DIR / "data" / "browser_capture_token.txt"
SNAPSHOT_ROOT = APP_DIR / "data" / "discovery_snapshots"
MAX_REQUEST_BYTES = 32 * 1024 * 1024

_SERVER: ThreadingHTTPServer | None = None
_SERVER_THREAD: threading.Thread | None = None
_SERVER_LOCK = threading.Lock()
_SCORING_LOCK = threading.Lock()
_SCORING_STATE: dict[int, dict[str, Any]] = {}


class BrowserCapturePayload(BaseModel):
    url: str
    title: str = ""
    raw_text: str = Field(min_length=20, max_length=50_000)
    company: str = ""
    role: str = ""
    location: str = ""
    salary: str = ""
    author: str = ""
    posted_at: str = ""
    manual_decision: str = "继续了解"
    screenshot_data_url: str = ""
    image_count: int = Field(default=0, ge=0, le=200)

    @field_validator("manual_decision")
    @classmethod
    def validate_manual_decision(cls, value: str) -> str:
        if value not in {
            "准备投递", "继续了解", "暂不投递", "信息待补全",
            "想投", "待定", "不投",
        }:
            raise ValueError("未知的个人判断。")
        return value


@dataclass(frozen=True)
class CaptureReceipt:
    item_id: int
    created: bool
    platform: str
    content_level: str
    completeness_score: int
    manual_decision: str
    availability_status: str
    capture_kind: str


EXPIRED_MARKERS = (
    "职位已下线", "岗位已下线", "停止招聘", "暂停招聘", "已结束招聘",
    "职位已关闭", "职位不存在", "该职位不存在", "职位已过期", "招聘已结束",
)


def classify_capture_quality(
    title: str,
    raw_text: str,
    image_count: int,
) -> tuple[str, str]:
    combined = f"{title}\n{raw_text}"
    availability = (
        "expired" if any(marker in combined for marker in EXPIRED_MARKERS) else "active"
    )
    has_responsibilities = any(
        marker in raw_text for marker in RESPONSIBILITY_MARKERS
    )
    has_requirements = any(
        marker in raw_text for marker in REQUIREMENT_MARKERS
    )
    if image_count > 0 and not (has_responsibilities and has_requirements):
        capture_kind = "image" if len(raw_text.strip()) < 600 else "mixed"
    elif image_count > 0:
        capture_kind = "mixed"
    else:
        capture_kind = "text"
    return availability, capture_kind


def schedule_capture_scoring(receipt: CaptureReceipt) -> str:
    with _SCORING_LOCK:
        _SCORING_STATE[receipt.item_id] = {
            "status": "manual",
            "message": "已收录。请在岗位决策箱确认信息后，逐条生成四层判断。",
        }
    return "manual"


def get_capture_scoring_state(item_id: int) -> dict[str, Any]:
    with _SCORING_LOCK:
        return dict(_SCORING_STATE.get(item_id, {"status": "not_started"}))


def get_capture_token() -> str:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    if TOKEN_PATH.exists():
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if len(token) >= 24:
            return token
    token = secrets.token_urlsafe(24)
    TOKEN_PATH.write_text(token, encoding="utf-8")
    TOKEN_PATH.chmod(0o600)
    return token


def _platform_from_url(url: str) -> str:
    hostname = (urlsplit(url).hostname or "").casefold()
    if hostname == "xiaohongshu.com" or hostname.endswith(".xiaohongshu.com"):
        return "xiaohongshu"
    if hostname == "zhipin.com" or hostname.endswith(".zhipin.com"):
        return "boss"
    if hostname == "shixiseng.com" or hostname.endswith(".shixiseng.com"):
        return "shixiseng"
    raise ValueError("目前只支持收录小红书、BOSS 直聘和实习僧页面。")


def _external_id(platform: str, url: str) -> str:
    patterns = {
        "xiaohongshu": [r"/explore/([^/?#]+)", r"/discovery/item/([^/?#]+)"],
        "boss": [r"/job_detail/([^/?#]+)", r"[?&]securityId=([^&#]+)"],
        "shixiseng": [r"/intern/([^/?#]+)", r"/interns/([^/?#]+)"],
    }
    for pattern in patterns[platform]:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


def _save_data_url_image(
    platform: str,
    external_id: str,
    data_url: str,
    *,
    label: str = "",
) -> str:
    if not data_url:
        return ""
    match = re.fullmatch(r"data:image/(png|jpeg);base64,([A-Za-z0-9+/=]+)", data_url)
    if not match:
        return ""
    raw = base64.b64decode(match.group(2), validate=True)
    if len(raw) > 5 * 1024 * 1024:
        raise ValueError("页面截图超过 5 MB，已停止收录。")
    suffix = "jpg" if match.group(1) == "jpeg" else "png"
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", external_id)[:80]
    suffix_label = f"_{label}" if label else ""
    target = SNAPSHOT_ROOT / platform / f"capture_{safe_id}{suffix_label}.{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return str(target.relative_to(APP_DIR))


def _save_screenshot(platform: str, external_id: str, data_url: str) -> str:
    return _save_data_url_image(platform, external_id, data_url)


def ingest_browser_capture(payload: BrowserCapturePayload | dict[str, Any]) -> CaptureReceipt:
    capture = (
        payload
        if isinstance(payload, BrowserCapturePayload)
        else BrowserCapturePayload.model_validate(payload)
    )
    platform = _platform_from_url(capture.url)
    external_id = _external_id(platform, capture.url)
    snapshot_path = _save_screenshot(
        platform, external_id, capture.screenshot_data_url
    )
    merged_raw_text = capture.raw_text.strip()
    availability_status, capture_kind = classify_capture_quality(
        capture.title, merged_raw_text, capture.image_count
    )
    run_id = start_discovery_run(platform, "浏览器一键收录")
    summary = complete_discovery_run(
        run_id,
        CollectorResult(
            platform=platform,
            keyword="浏览器一键收录",
            items=[
                CollectedItem(
                    platform=platform,
                    external_id=external_id,
                    url=capture.url,
                    access_url=capture.url,
                    title=capture.title,
                    company=capture.company,
                    role=capture.role,
                    location=capture.location,
                    salary=capture.salary,
                    posted_at=normalize_posted_at(capture.posted_at),
                    author=capture.author,
                    raw_text=merged_raw_text,
                    source_keyword="浏览器一键收录",
                    snapshot_path=snapshot_path,
                    capture_method="browser_extension",
                    manual_decision=capture.manual_decision,
                    availability_status=availability_status,
                    capture_kind=capture_kind,
                )
            ],
        ),
    )
    if summary.new_item_ids:
        item_id = summary.new_item_ids[0]
        created = True
    else:
        from services.discovery_service import find_discovery_item_id

        item_id = find_discovery_item_id(platform, external_id)
        created = False
    from services.discovery_service import apply_capture_quality_flags

    apply_capture_quality_flags(
        item_id,
        availability_status=availability_status,
        capture_kind=capture_kind,
    )
    detail = get_discovery_item(item_id)
    return CaptureReceipt(
        item_id=item_id,
        created=created,
        platform=platform,
        content_level=str(detail.get("content_level") or "summary"),
        completeness_score=int(detail.get("completeness_score") or 0),
        manual_decision=str(detail.get("manual_decision") or "继续了解"),
        availability_status=str(detail.get("availability_status") or "unknown"),
        capture_kind=str(detail.get("capture_kind") or "text"),
    )


def _allowed_origin(origin: str) -> bool:
    return not origin or origin.startswith("chrome-extension://")


class _CaptureHandler(BaseHTTPRequestHandler):
    server_version = "CareerOSCapture/1.0"

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        origin = self.headers.get("Origin", "")
        if _allowed_origin(origin) and origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        origin = self.headers.get("Origin", "")
        if not _allowed_origin(origin):
            self._send_json(403, {"ok": False, "error": "不允许的页面来源。"})
            return
        self.send_response(204)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers", "Content-Type, X-Career-OS-Token"
        )
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(200, {"ok": True, "service": "browser_capture"})
            return
        self._send_json(404, {"ok": False, "error": "接口不存在。"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/capture":
            self._send_json(404, {"ok": False, "error": "接口不存在。"})
            return
        origin = self.headers.get("Origin", "")
        if not _allowed_origin(origin):
            self._send_json(403, {"ok": False, "error": "不允许的页面来源。"})
            return
        if not secrets.compare_digest(
            self.headers.get("X-Career-OS-Token", ""), get_capture_token()
        ):
            self._send_json(401, {"ok": False, "error": "配对码不正确。"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > MAX_REQUEST_BYTES:
                raise ValueError("收录内容为空或超过大小限制。")
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
            receipt = ingest_browser_capture(payload)
            scoring_status = schedule_capture_scoring(receipt)
            self._send_json(
                200,
                {
                    "ok": True,
                    "item_id": receipt.item_id,
                    "created": receipt.created,
                    "platform": receipt.platform,
                    "content_level": receipt.content_level,
                    "completeness_score": receipt.completeness_score,
                    "manual_decision": receipt.manual_decision,
                    "availability_status": receipt.availability_status,
                    "capture_kind": receipt.capture_kind,
                    "scoring_status": scoring_status,
                },
            )
        except Exception as exc:
            self._send_json(400, {"ok": False, "error": str(exc)[:300]})

    def log_message(self, format: str, *args: object) -> None:
        return


def start_capture_server() -> ThreadingHTTPServer:
    global _SERVER, _SERVER_THREAD
    with _SERVER_LOCK:
        if _SERVER_THREAD and _SERVER_THREAD.is_alive() and _SERVER:
            return _SERVER
        init_database()
        get_capture_token()
        server = ThreadingHTTPServer((CAPTURE_HOST, CAPTURE_PORT), _CaptureHandler)
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever,
            name="career-os-browser-capture",
            daemon=True,
        )
        thread.start()
        _SERVER = server
        _SERVER_THREAD = thread
        return server
