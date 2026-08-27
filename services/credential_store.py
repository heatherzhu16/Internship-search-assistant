from __future__ import annotations

import json
import shutil
import subprocess


KEYCHAIN_SERVICE = "local-job-search-assistant.163-imap"
KEYCHAIN_ACCOUNT = "default"
DEEPSEEK_KEYCHAIN_SERVICE = "local-job-search-assistant.deepseek"
DEEPSEEK_KEYCHAIN_ACCOUNT = "default"


def keychain_available() -> bool:
    return shutil.which("security") is not None


def load_email_credentials() -> tuple[str, str] | None:
    if not keychain_available():
        return None
    result = subprocess.run(
        [
            "security", "find-generic-password",
            "-a", KEYCHAIN_ACCOUNT, "-s", KEYCHAIN_SERVICE, "-w",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        payload = json.loads(result.stdout.strip())
        address = str(payload.get("address", "")).strip()
        authorization_code = str(payload.get("authorization_code", "")).strip()
    except (json.JSONDecodeError, AttributeError):
        return None
    return (address, authorization_code) if address and authorization_code else None


def save_email_credentials(address: str, authorization_code: str) -> None:
    if not keychain_available():
        raise RuntimeError("当前系统没有可用的 macOS 钥匙串命令。")
    if not address.strip() or not authorization_code.strip():
        raise ValueError("邮箱地址和客户端授权码不能为空。")
    secret = json.dumps(
        {
            "address": address.strip(),
            "authorization_code": authorization_code.strip(),
        },
        ensure_ascii=False,
    )
    result = subprocess.run(
        [
            "security", "add-generic-password", "-U",
            "-a", KEYCHAIN_ACCOUNT, "-s", KEYCHAIN_SERVICE, "-w", secret,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "写入 macOS 钥匙串失败。")


def delete_email_credentials() -> bool:
    if not keychain_available():
        return False
    result = subprocess.run(
        [
            "security", "delete-generic-password",
            "-a", KEYCHAIN_ACCOUNT, "-s", KEYCHAIN_SERVICE,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.returncode == 0


def load_deepseek_credentials() -> tuple[str, str] | None:
    if not keychain_available():
        return None
    result = subprocess.run(
        [
            "security", "find-generic-password",
            "-a", DEEPSEEK_KEYCHAIN_ACCOUNT,
            "-s", DEEPSEEK_KEYCHAIN_SERVICE,
            "-w",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        payload = json.loads(result.stdout.strip())
        api_key = str(payload.get("api_key", "")).strip()
        model = str(payload.get("model", "deepseek-chat")).strip()
    except (json.JSONDecodeError, AttributeError):
        return None
    return (api_key, model or "deepseek-chat") if api_key else None


def save_deepseek_credentials(api_key: str, model: str) -> None:
    if not keychain_available():
        raise RuntimeError("当前系统没有可用的 macOS 钥匙串命令。")
    cleaned_key = api_key.strip()
    if not cleaned_key:
        raise ValueError("DeepSeek API Key 不能为空。")
    secret = json.dumps(
        {
            "api_key": cleaned_key,
            "model": model.strip() or "deepseek-chat",
        },
        ensure_ascii=True,
    )
    result = subprocess.run(
        [
            "security", "add-generic-password", "-U",
            "-a", DEEPSEEK_KEYCHAIN_ACCOUNT,
            "-s", DEEPSEEK_KEYCHAIN_SERVICE,
            "-w", secret,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "写入 macOS 钥匙串失败。")


def delete_deepseek_credentials() -> bool:
    if not keychain_available():
        return False
    result = subprocess.run(
        [
            "security", "delete-generic-password",
            "-a", DEEPSEEK_KEYCHAIN_ACCOUNT,
            "-s", DEEPSEEK_KEYCHAIN_SERVICE,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.returncode == 0
