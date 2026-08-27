from __future__ import annotations

import base64
import hashlib
import imaplib
import json
import mimetypes
import re
import sqlite3
import ssl
import time
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Iterable

import pandas as pd

from models.email import (
    ClassificationResult,
    EmailConnectionConfig,
    MailboxInfo,
    MatchResult,
    NormalizedEmail,
    ScanSummary,
)
from services.database import (
    DB_PATH,
    append_application_event,
    undo_event,
)


CLASSIFICATION_LABELS = {
    "application_sent": "完成邮件投递",
    "application_received": "收到投递确认",
    "assessment": "收到笔试/测评",
    "interview_1": "收到一面邀请",
    "interview_2": "收到二面邀请",
    "interview_final": "收到终面邀请",
    "interview_unknown": "收到面试邀请（轮次待确认）",
    "rejection": "收到拒信",
    "offer": "收到 Offer",
    "other": "非求职或无法识别",
}

STATUS_BY_CLASSIFICATION = {
    "application_sent": "已投递",
    "application_received": "已投递",
    "assessment": "笔试",
    "interview_1": "一面",
    "interview_2": "二面",
    "interview_final": "终面",
    "interview_unknown": "",
    "rejection": "拒绝",
    "offer": "Offer",
    "other": "",
}

APPLICATION_TERMS = [
    "应聘", "求职", "申请", "岗位", "实习", "简历", "resume", "cv",
    "application", "candidate",
]
RECEIVED_TERMS = [
    "申请已收到", "已收到您的申请", "感谢申请", "感谢投递",
    "application received", "successfully submitted",
]
ASSESSMENT_TERMS = [
    "在线测评", "笔试邀请", "测评邀请", "测评链接", "笔试通知",
    "assessment", "online test", "written test",
]
INTERVIEW_TERMS = [
    "面试邀请", "面谈邀请", "面试通知", "interview invitation",
    "invite you to interview", "面试安排",
]
REJECTION_TERMS = [
    "很遗憾", "暂不匹配", "未能进入下一环节", "不再继续",
    "unfortunately", "not moving forward", "will not be proceeding",
]
OFFER_TERMS = [
    "录用通知", "正式录用", "offer letter", "employment offer",
]


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)


def _html_to_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(value)
    return "\n".join(parser.parts)


def decode_modified_utf7(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        if value[index] != "&":
            next_amp = value.find("&", index)
            if next_amp == -1:
                next_amp = len(value)
            result.append(value[index:next_amp])
            index = next_amp
            continue
        end = value.find("-", index)
        if end == -1:
            result.append(value[index:])
            break
        token = value[index + 1 : end]
        if not token:
            result.append("&")
        else:
            encoded = token.replace(",", "/")
            encoded += "=" * ((4 - len(encoded) % 4) % 4)
            result.append(base64.b64decode(encoded).decode("utf-16-be"))
        index = end + 1
    return "".join(result)


def _parse_mailbox_line(raw: bytes) -> MailboxInfo | None:
    text = raw.decode("ascii", errors="replace")
    match = re.match(r"\((?P<flags>[^)]*)\)\s+(?P<delimiter>NIL|\".*?\")\s+(?P<name>.+)$", text)
    if not match:
        return None
    wire_name = match.group("name").strip()
    if wire_name.startswith('"') and wire_name.endswith('"'):
        wire_name = wire_name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    flags = [item for item in match.group("flags").split() if item]
    display_name = decode_modified_utf7(wire_name)
    sent_names = {"已发送", "已发邮件", "sent", "sent messages", "sent mail"}
    draft_names = {"草稿箱", "草稿", "drafts", "draft"}
    is_sent = any(flag.casefold() == r"\sent".casefold() for flag in flags)
    is_sent = is_sent or display_name.casefold() in sent_names
    is_draft = any(flag.casefold() == r"\drafts".casefold() for flag in flags)
    is_draft = is_draft or display_name.casefold() in draft_names
    return MailboxInfo(
        display_name=display_name,
        wire_name=wire_name,
        flags=flags,
        is_sent=is_sent,
        is_draft=is_draft,
    )


def _connect(config: EmailConnectionConfig) -> imaplib.IMAP4_SSL:
    client = imaplib.IMAP4_SSL(
        config.host,
        config.port,
        ssl_context=ssl.create_default_context(),
        timeout=15,
    )
    client.login(config.address, config.authorization_code)
    if "ID" not in imaplib.Commands:
        imaplib.Commands["ID"] = ("AUTH", "SELECTED")
    try:
        client._simple_command(  # noqa: SLF001 - IMAP ID has no public imaplib wrapper.
            "ID",
            '("name" "local-job-search-assistant" "version" "1.0" "vendor" "local")',
        )
    except imaplib.IMAP4.error:
        pass
    return client


def list_mailboxes(config: EmailConnectionConfig) -> list[MailboxInfo]:
    client = _connect(config)
    try:
        status, rows = client.list()
        if status != "OK":
            raise ConnectionError("163 邮箱未返回文件夹列表。")
        mailboxes = [
            mailbox
            for row in rows or []
            if row and (mailbox := _parse_mailbox_line(row)) is not None
        ]
        return sorted(mailboxes, key=lambda item: (not item.is_sent, item.display_name))
    finally:
        try:
            client.logout()
        except imaplib.IMAP4.error:
            pass


def test_connection(config: EmailConnectionConfig) -> list[MailboxInfo]:
    if not config.address.lower().endswith("@163.com"):
        raise ValueError("阶段 6 当前只配置了个人 163 邮箱。")
    return list_mailboxes(config)


def _message_text(message) -> tuple[str, list[str]]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[str] = []
    for part in message.walk():
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        if filename or disposition == "attachment":
            if filename:
                attachments.append(str(filename))
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeDecodeError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if content_type == "text/plain":
            plain_parts.append(str(content))
        else:
            html_parts.append(_html_to_text(str(content)))
    text = "\n".join(plain_parts).strip() or "\n".join(html_parts).strip()
    return re.sub(r"\n{3,}", "\n\n", text), attachments


def _addresses(header_values: Iterable[str]) -> list[str]:
    return [
        address.casefold()
        for _, address in getaddresses(
            [value for value in header_values if value and value.strip()]
        )
        if address.strip()
    ]


def parse_message(
    raw: bytes,
    *,
    folder_name: str,
    direction: str,
    uid_validity: int,
    imap_uid: int,
) -> NormalizedEmail:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    body_text, attachments = _message_text(message)
    date_header = str(message.get("Date", "")).strip()
    try:
        received_at = parsedate_to_datetime(date_header) if date_header else None
    except (TypeError, ValueError, OverflowError):
        received_at = None
    if received_at is None:
        received_at = datetime.now(timezone.utc)
    elif received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    from_addresses = _addresses([str(message.get("From", ""))])
    recipients = _addresses(
        [
            str(message.get("To", "")),
            str(message.get("Cc", "")),
            str(message.get("Bcc", "")),
        ]
    )
    excerpt = re.sub(r"\s+", " ", body_text).strip()[:1200]
    return NormalizedEmail(
        folder_name=folder_name,
        direction=direction,
        uid_validity=uid_validity,
        imap_uid=imap_uid,
        message_id=str(message.get("Message-ID", "")).strip(),
        received_at=received_at,
        from_address=from_addresses[0] if from_addresses else "",
        to_addresses=recipients,
        subject=str(message.get("Subject", "")).strip(),
        body_text=body_text,
        body_excerpt=excerpt,
        body_hash=hashlib.sha256(raw).hexdigest(),
        attachment_names=attachments,
    )


def _contains(text: str, terms: Iterable[str]) -> list[str]:
    normalized = text.casefold()
    return [term for term in terms if term.casefold() in normalized]


def classify_email(message: NormalizedEmail) -> ClassificationResult:
    subject = message.subject.casefold()
    combined = f"{message.subject}\n{message.body_text}".casefold()
    if message.direction == "outgoing":
        subject_terms = _contains(message.subject, APPLICATION_TERMS)
        body_terms = _contains(message.body_text[:4000], APPLICATION_TERMS)
        resume_attachments = [
            name
            for name in message.attachment_names
            if re.search(r"(简历|resume|cv).*\.(pdf|docx?)$", name, re.I)
            or re.search(r"\.(pdf|docx?)$", name, re.I)
        ]
        if subject_terms and resume_attachments:
            return ClassificationResult(
                classification="application_sent",
                suggested_status="已投递",
                confidence=0.96,
                reason_codes=["subject_application_term", "resume_attachment"],
            )
        if subject_terms and body_terms:
            return ClassificationResult(
                classification="application_sent",
                suggested_status="已投递",
                confidence=0.88,
                reason_codes=["subject_application_term", "body_application_term"],
            )
        if resume_attachments and body_terms:
            return ClassificationResult(
                classification="application_sent",
                suggested_status="已投递",
                confidence=0.78,
                reason_codes=["resume_attachment", "body_application_term"],
            )
        return ClassificationResult(
            classification="other",
            confidence=0.05,
            reason_codes=["outgoing_not_application_like"],
        )

    rules = [
        ("offer", OFFER_TERMS, "Offer", 0.96, "offer_term"),
        ("rejection", REJECTION_TERMS, "拒绝", 0.94, "rejection_term"),
    ]
    for classification, terms, status, confidence, reason in rules:
        matches = _contains(combined, terms)
        if matches:
            return ClassificationResult(
                classification=classification,
                suggested_status=status,
                confidence=confidence,
                reason_codes=[reason],
            )

    if _contains(combined, INTERVIEW_TERMS) or "面试" in combined:
        if re.search(r"(终面|最终面|final interview)", combined, re.I):
            classification, status, confidence = "interview_final", "终面", 0.96
        elif re.search(r"(二面|第二轮|复试|second interview|2nd interview)", combined, re.I):
            classification, status, confidence = "interview_2", "二面", 0.95
        elif re.search(r"(一面|第一轮|初面|first interview|1st interview)", combined, re.I):
            classification, status, confidence = "interview_1", "一面", 0.95
        else:
            classification, status, confidence = "interview_unknown", "", 0.82
        return ClassificationResult(
            classification=classification,
            suggested_status=status,
            confidence=confidence,
            reason_codes=["interview_term"],
        )

    if matches := _contains(combined, ASSESSMENT_TERMS):
        return ClassificationResult(
            classification="assessment",
            suggested_status="笔试",
            confidence=0.93,
            reason_codes=["assessment_term"],
        )
    if matches := _contains(combined, RECEIVED_TERMS):
        return ClassificationResult(
            classification="application_received",
            suggested_status="已投递",
            confidence=0.91,
            reason_codes=["application_received_term"],
        )
    return ClassificationResult(
        classification="other",
        confidence=0.1,
        reason_codes=["no_recruitment_rule_matched"],
    )


def _compact(value: str) -> str:
    return re.sub(r"[\s\-_—·（）()【】\[\]]+", "", value.casefold())


def match_application(message: NormalizedEmail) -> MatchResult:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT id, COALESCE(company, ''), COALESCE(role, ''),
                   COALESCE(application_email, ''),
                   COALESCE(application_reference, '')
            FROM applications ORDER BY id DESC
            """
        ).fetchall()
    content = _compact(f"{message.subject}\n{message.body_text[:5000]}")
    contact_addresses = {address.casefold() for address in message.to_addresses}
    if message.direction == "incoming" and message.from_address:
        contact_addresses.add(message.from_address.casefold())
    candidates: list[tuple[int, float, list[str]]] = []
    for application_id, company, role, application_email, reference in rows:
        score = 0.0
        reasons: list[str] = []
        if reference and _compact(reference) in content:
            score += 0.65
            reasons.append("exact_application_reference")
        if application_email and application_email.casefold() in contact_addresses:
            score += 0.6
            reasons.append("exact_application_email")
        if company and _compact(company) in content:
            score += 0.22
            reasons.append("company_in_message")
        if role and _compact(role) in content:
            score += 0.22
            reasons.append("role_in_message")
        if score > 0:
            candidates.append((int(application_id), min(score, 1.0), reasons))
    candidates.sort(key=lambda item: item[1], reverse=True)
    if not candidates:
        return MatchResult(reason_codes=["no_application_candidate"])
    top_id, top_score, top_reasons = candidates[0]
    second_score = candidates[1][1] if len(candidates) > 1 else 0
    unique = top_score >= 0.55 and top_score - second_score >= 0.12
    return MatchResult(
        application_id=top_id if unique else None,
        confidence=top_score if unique else min(top_score, 0.6),
        reason_codes=top_reasons + ([] if unique else ["ambiguous_candidates"]),
        candidate_ids=[item[0] for item in candidates[:5]],
    )


def _account_hash(address: str) -> str:
    return hashlib.sha256(address.strip().casefold().encode("utf-8")).hexdigest()


def _mask_address(address: str) -> str:
    local, _, domain = address.partition("@")
    visible = local[:2] + "***" if local else "***"
    return f"{visible}@{domain}" if domain else visible


def save_account_settings(config: EmailConnectionConfig) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    address_hash = _account_hash(config.address)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO email_sync_accounts(
                provider, address_hash, address_masked, sent_folder, incoming_folder,
                draft_folder, host, port, max_messages, auto_apply, created_at, updated_at
            ) VALUES ('163', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, address_hash) DO UPDATE SET
                address_masked = excluded.address_masked,
                sent_folder = excluded.sent_folder,
                incoming_folder = excluded.incoming_folder,
                draft_folder = excluded.draft_folder,
                host = excluded.host,
                port = excluded.port,
                max_messages = excluded.max_messages,
                auto_apply = excluded.auto_apply,
                updated_at = excluded.updated_at
            """,
            (
                address_hash,
                _mask_address(config.address),
                config.sent_folder,
                config.incoming_folder,
                config.draft_folder,
                config.host,
                config.port,
                config.max_messages_per_folder,
                int(config.auto_apply_high_confidence),
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT id FROM email_sync_accounts WHERE provider = '163' AND address_hash = ?",
            (address_hash,),
        ).fetchone()
    return int(row[0])


def load_account_settings(address: str) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT id, sent_folder, incoming_folder, draft_folder, host, port,
                   max_messages, auto_apply, last_success_at
            FROM email_sync_accounts
            WHERE provider = '163' AND address_hash = ?
            """,
            (_account_hash(address),),
        ).fetchone()
    if not row:
        return None
    return {
        "id": int(row[0]),
        "sent_folder": row[1],
        "incoming_folder": row[2],
        "draft_folder": row[3],
        "host": row[4],
        "port": int(row[5]),
        "max_messages": int(row[6]),
        "auto_apply": bool(row[7]),
        "last_success_at": row[8],
    }


def build_draft_message(
    *,
    from_address: str,
    to_address: str,
    subject: str,
    body: str,
    attachment_bytes: bytes | None = None,
    attachment_filename: str = "",
    attachment_mime_type: str = "",
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = from_address.strip()
    message["To"] = to_address.strip()
    message["Subject"] = subject.strip()
    message["Date"] = format_datetime(datetime.now().astimezone())
    message.set_content(body.rstrip() + "\n")
    if attachment_bytes is not None:
        guessed_type = (
            attachment_mime_type
            or mimetypes.guess_type(attachment_filename)[0]
            or "application/octet-stream"
        )
        maintype, subtype = guessed_type.split("/", 1)
        message.add_attachment(
            attachment_bytes,
            maintype=maintype,
            subtype=subtype,
            filename=attachment_filename or "resume",
        )
    return message


def create_email_draft(
    config: EmailConnectionConfig,
    *,
    to_address: str,
    subject: str,
    body: str,
    attachment_bytes: bytes | None = None,
    attachment_filename: str = "",
    attachment_mime_type: str = "",
) -> tuple[bool, str]:
    recipient = to_address.strip().casefold()
    if not re.fullmatch(r"[\w.+-]+@(?:[\w-]+\.)+[A-Za-z]{2,}", recipient):
        raise ValueError("请填写有效的投递邮箱。")
    if not subject.strip() or not body.strip():
        raise ValueError("邮件主题和正文不能为空。")

    account_id = save_account_settings(config)
    content_hash = hashlib.sha256(
        "\n".join(
            [
                recipient,
                subject.strip(),
                body.strip().replace("\r\n", "\n"),
                (
                    hashlib.sha256(attachment_bytes).hexdigest()
                    if attachment_bytes is not None
                    else ""
                ),
            ]
        ).encode("utf-8")
    ).hexdigest()
    with sqlite3.connect(DB_PATH) as conn:
        duplicate = conn.execute(
            """
            SELECT id FROM email_draft_exports
            WHERE account_id = ? AND content_hash = ?
            """,
            (account_id, content_hash),
        ).fetchone()
    if duplicate:
        return False, "相同内容已经写入过草稿箱，没有重复创建。"

    client = _connect(config)
    try:
        status, rows = client.list()
        if status != "OK":
            raise ConnectionError("163 邮箱未返回文件夹列表。")
        mailboxes = [
            mailbox
            for row in rows or []
            if row and (mailbox := _parse_mailbox_line(row)) is not None
        ]
        mailbox = next(
            (
                item
                for item in mailboxes
                if item.display_name == config.draft_folder
            ),
            None,
        )
        if mailbox is None:
            mailbox = next((item for item in mailboxes if item.is_draft), None)
        if mailbox is None:
            raise ValueError(
                f"找不到草稿文件夹“{config.draft_folder}”，请在同步中心重新读取并选择。"
            )
        message = build_draft_message(
            from_address=config.address,
            to_address=recipient,
            subject=subject,
            body=body,
            attachment_bytes=attachment_bytes,
            attachment_filename=attachment_filename,
            attachment_mime_type=attachment_mime_type,
        )
        append_status, _ = client.append(
            f'"{mailbox.wire_name}"',
            r"(\Draft)",
            imaplib.Time2Internaldate(time.time()),
            message.as_bytes(policy=policy.SMTP),
        )
        if append_status != "OK":
            raise ConnectionError("163 邮箱拒绝写入草稿，请检查草稿箱设置。")
    finally:
        try:
            client.logout()
        except imaplib.IMAP4.error:
            pass

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO email_draft_exports(
                account_id, content_hash, to_address, subject,
                draft_folder, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                content_hash,
                recipient,
                subject.strip(),
                mailbox.display_name,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
    return True, f"已写入 163 的“{mailbox.display_name}”，请人工检查后再发送。"


def _message_is_duplicate(
    conn: sqlite3.Connection,
    account_id: int,
    message: NormalizedEmail,
) -> bool:
    uid_row = conn.execute(
        """
        SELECT 1 FROM email_messages
        WHERE account_id = ? AND folder_name = ? AND uid_validity = ? AND imap_uid = ?
        """,
        (account_id, message.folder_name, message.uid_validity, message.imap_uid),
    ).fetchone()
    if uid_row:
        return True
    return bool(
        conn.execute(
            """
            SELECT 1 FROM email_messages
            WHERE account_id = ? AND direction = ?
              AND ((message_id != '' AND message_id = ?) OR body_hash = ?)
            """,
            (
                account_id,
                message.direction,
                message.message_id,
                message.body_hash,
            ),
        ).fetchone()
    )


def _event_external_id(account_id: int, message_id: int, classification: str) -> str:
    return f"email:163:{account_id}:{message_id}:{classification}"


def _event_id(external_id: str) -> int | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT id FROM application_events WHERE external_id = ? ORDER BY id DESC LIMIT 1",
            (external_id,),
        ).fetchone()
    return int(row[0]) if row else None


def _insert_message(
    account_id: int,
    message: NormalizedEmail,
    classification: ClassificationResult,
    match: MatchResult,
    state: str,
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO email_messages(
                account_id, folder_name, direction, uid_validity, imap_uid,
                message_id, received_at, from_address, to_addresses_json,
                subject, body_excerpt, body_hash, attachment_names_json,
                classification, suggested_status, classification_confidence,
                matched_application_id, match_confidence, processing_state,
                reason_codes_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                message.folder_name,
                message.direction,
                message.uid_validity,
                message.imap_uid,
                message.message_id,
                message.received_at.isoformat(),
                message.from_address,
                json.dumps(message.to_addresses, ensure_ascii=False),
                message.subject,
                message.body_excerpt,
                message.body_hash,
                json.dumps(message.attachment_names, ensure_ascii=False),
                classification.classification,
                classification.suggested_status,
                classification.confidence,
                match.application_id,
                match.confidence,
                state,
                json.dumps(
                    classification.reason_codes + match.reason_codes,
                    ensure_ascii=False,
                ),
                now,
                now,
            ),
        )
        message_id = int(cursor.lastrowid)
        for candidate_id in match.candidate_ids:
            score = match.confidence if candidate_id == match.application_id else 0
            conn.execute(
                """
                INSERT OR IGNORE INTO email_match_candidates(
                    email_message_id, application_id, match_score,
                    reason_codes_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    candidate_id,
                    score,
                    json.dumps(match.reason_codes, ensure_ascii=False),
                    now,
                ),
            )
    return message_id


def _can_auto_apply(
    config: EmailConnectionConfig,
    classification: ClassificationResult,
    match: MatchResult,
) -> bool:
    return bool(
        config.auto_apply_high_confidence
        and classification.classification not in {"offer", "interview_unknown", "other"}
        and classification.suggested_status
        and classification.confidence >= 0.9
        and match.application_id is not None
        and match.confidence >= 0.9
    )


def _select_folder(
    client: imaplib.IMAP4_SSL,
    mailbox: MailboxInfo,
) -> int:
    status, _ = client.select(f'"{mailbox.wire_name}"', readonly=True)
    if status != "OK":
        raise ValueError(f"无法只读打开文件夹“{mailbox.display_name}”。")
    response = client.response("UIDVALIDITY")
    raw = response[1][0] if response and response[1] else b"0"
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", errors="ignore")
    digits = re.findall(r"\d+", str(raw))
    return int(digits[-1]) if digits else 0


def _fetch_folder(
    client: imaplib.IMAP4_SSL,
    mailbox: MailboxInfo,
    direction: str,
    limit: int,
) -> list[NormalizedEmail]:
    uid_validity = _select_folder(client, mailbox)
    status, rows = client.uid("search", None, "ALL")
    if status != "OK" or not rows:
        return []
    uids = [int(item) for item in rows[0].split() if item]
    messages: list[NormalizedEmail] = []
    for uid in uids[-limit:]:
        status, payload = client.uid(
            "fetch",
            str(uid),
            "(BODY.PEEK[] RFC822.SIZE)",
        )
        if status != "OK":
            continue
        raw = next(
            (
                item[1]
                for item in payload or []
                if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes)
            ),
            None,
        )
        if raw:
            messages.append(
                parse_message(
                    raw,
                    folder_name=mailbox.display_name,
                    direction=direction,
                    uid_validity=uid_validity,
                    imap_uid=uid,
                )
            )
    return messages


def scan_mailbox(config: EmailConnectionConfig) -> ScanSummary:
    account_id = save_account_settings(config)
    started_at = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO email_sync_runs(account_id, started_at, status)
            VALUES (?, ?, 'running')
            """,
            (account_id, started_at),
        )
        run_id = int(cursor.lastrowid)
    summary = ScanSummary(run_id=run_id)
    try:
        client = _connect(config)
        try:
            status, rows = client.list()
            if status != "OK":
                raise ConnectionError("无法读取163邮箱文件夹。")
            mailboxes = [
                mailbox
                for row in rows or []
                if row and (mailbox := _parse_mailbox_line(row)) is not None
            ]
            by_display = {item.display_name: item for item in mailboxes}
            folder_specs = [
                (config.sent_folder, "outgoing"),
                (config.incoming_folder, "incoming"),
            ]
            all_messages: list[NormalizedEmail] = []
            for folder_name, direction in folder_specs:
                mailbox = by_display.get(folder_name)
                if mailbox is None:
                    summary.errors.append(f"找不到文件夹：{folder_name}")
                    continue
                try:
                    all_messages.extend(
                        _fetch_folder(
                            client,
                            mailbox,
                            direction,
                            config.max_messages_per_folder,
                        )
                    )
                except Exception as exc:
                    summary.errors.append(f"{folder_name}：{exc}")
            for message in all_messages:
                summary.scanned += 1
                with sqlite3.connect(DB_PATH) as conn:
                    if _message_is_duplicate(conn, account_id, message):
                        summary.duplicates += 1
                        continue
                classification = classify_email(message)
                if (
                    message.direction == "outgoing"
                    and classification.classification == "other"
                ):
                    summary.ignored_non_job += 1
                    continue
                match = match_application(message)
                state = (
                    "pending"
                    if match.application_id is not None
                    and classification.classification != "other"
                    else "unmatched"
                )
                email_message_id = _insert_message(
                    account_id,
                    message,
                    classification,
                    match,
                    state,
                )
                summary.new_messages += 1
                if _can_auto_apply(config, classification, match):
                    external_id = _event_external_id(
                        account_id,
                        email_message_id,
                        classification.classification,
                    )
                    created, _ = append_application_event(
                        int(match.application_id),
                        classification.suggested_status,
                        source="163邮件自动同步",
                        event_type=CLASSIFICATION_LABELS[classification.classification],
                        external_id=external_id,
                    )
                    if created:
                        event_id = _event_id(external_id)
                        with sqlite3.connect(DB_PATH) as conn:
                            conn.execute(
                                """
                                UPDATE email_messages
                                SET processing_state = 'auto_applied', event_id = ?,
                                    updated_at = ?
                                WHERE id = ?
                                """,
                                (
                                    event_id,
                                    datetime.now().isoformat(timespec="seconds"),
                                    email_message_id,
                                ),
                            )
                        summary.auto_applied += 1
                        continue
                if state == "pending":
                    summary.pending += 1
                else:
                    summary.unmatched += 1
        finally:
            try:
                client.logout()
            except imaplib.IMAP4.error:
                pass
        run_status = "completed_with_warnings" if summary.errors else "completed"
    except Exception as exc:
        summary.errors.append(str(exc))
        run_status = "failed"
    finished_at = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE email_sync_runs
            SET finished_at = ?, status = ?, scanned_count = ?, new_count = ?,
                duplicate_count = ?, pending_count = ?, unmatched_count = ?,
                auto_event_count = ?, error_text = ?
            WHERE id = ?
            """,
            (
                finished_at,
                run_status,
                summary.scanned,
                summary.new_messages,
                summary.duplicates,
                summary.pending,
                summary.unmatched,
                summary.auto_applied,
                "\n".join(summary.errors),
                run_id,
            ),
        )
        if run_status != "failed":
            conn.execute(
                """
                UPDATE email_sync_accounts
                SET last_success_at = ?, updated_at = ? WHERE id = ?
                """,
                (finished_at, finished_at, account_id),
            )
    return summary


def load_email_messages(states: list[str] | None = None) -> pd.DataFrame:
    query = """
        SELECT em.id AS 邮件ID, em.received_at AS 时间,
               CASE em.direction WHEN 'outgoing' THEN '已发送' ELSE '收到' END AS 方向,
               em.folder_name AS 文件夹, em.from_address AS 发件人,
               em.subject AS 主题,
               em.body_excerpt AS 正文摘要,
               em.classification AS 分类代码,
               em.suggested_status AS 建议状态,
               em.classification_confidence AS 分类置信度,
               em.match_confidence AS 匹配置信度,
               em.matched_application_id AS 匹配岗位ID,
               a.company AS 匹配公司, a.role AS 匹配职位,
               em.processing_state AS 处理状态,
               em.event_id AS 事件ID
        FROM email_messages em
        LEFT JOIN applications a ON a.id = em.matched_application_id
    """
    params: list[str] = []
    if states:
        placeholders = ",".join("?" for _ in states)
        query += f" WHERE em.processing_state IN ({placeholders})"
        params.extend(states)
    query += " ORDER BY em.received_at DESC, em.id DESC"
    with sqlite3.connect(DB_PATH) as conn:
        data = pd.read_sql_query(query, conn, params=params)
    if not data.empty:
        data["识别类型"] = data["分类代码"].map(CLASSIFICATION_LABELS).fillna("未知")
    else:
        data["识别类型"] = pd.Series(dtype="string")
    return data


def load_sync_runs() -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        return pd.read_sql_query(
            """
            SELECT r.id AS 扫描ID, r.started_at AS 开始时间,
                   r.finished_at AS 结束时间, r.status AS 状态,
                   r.scanned_count AS 扫描数, r.new_count AS 新邮件,
                   r.duplicate_count AS 重复,
                   r.pending_count AS 待确认,
                   r.unmatched_count AS 未匹配,
                   r.auto_event_count AS 自动事件,
                   r.error_text AS 错误
            FROM email_sync_runs r ORDER BY r.id DESC
            """,
            conn,
        )


def confirm_email_message(
    email_message_id: int,
    application_id: int,
    new_status: str,
) -> tuple[bool, str]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT account_id, classification, received_at, processing_state
            FROM email_messages WHERE id = ?
            """,
            (email_message_id,),
        ).fetchone()
    if not row:
        return False, "找不到这封邮件。"
    account_id, classification, _received_at, state = row
    if state in {"confirmed", "auto_applied"}:
        return False, "这封邮件已经创建过事件。"
    external_id = _event_external_id(account_id, email_message_id, classification)
    created, message = append_application_event(
        application_id,
        new_status,
        source="163邮件人工确认",
        event_type=CLASSIFICATION_LABELS.get(classification, "邮件状态同步"),
        external_id=external_id,
    )
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        if created:
            conn.execute(
                """
                UPDATE email_messages
                SET matched_application_id = ?, suggested_status = ?,
                    processing_state = 'confirmed', event_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    application_id,
                    new_status,
                    _event_id(external_id),
                    now,
                    email_message_id,
                ),
            )
        elif "已经是这个状态" in message:
            conn.execute(
                """
                UPDATE email_messages
                SET matched_application_id = ?, suggested_status = ?,
                    processing_state = 'confirmed_no_change', updated_at = ?
                WHERE id = ?
                """,
                (application_id, new_status, now, email_message_id),
            )
            return True, "岗位已经是该状态；邮件已标记为已确认，没有重复追加事件。"
    return created, message


def ignore_email_message(email_message_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE email_messages
            SET processing_state = 'ignored', updated_at = ? WHERE id = ?
            """,
            (datetime.now().isoformat(timespec="seconds"), email_message_id),
        )


def undo_email_message(email_message_id: int) -> tuple[bool, str]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT event_id FROM email_messages WHERE id = ?",
            (email_message_id,),
        ).fetchone()
    if not row or not row[0]:
        return False, "这封邮件没有可撤销的投递事件。"
    success, message = undo_event(int(row[0]), "撤销邮件同步事件")
    if success:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                UPDATE email_messages
                SET processing_state = 'voided', updated_at = ? WHERE id = ?
                """,
                (datetime.now().isoformat(timespec="seconds"), email_message_id),
            )
    return success, message
