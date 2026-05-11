from __future__ import annotations

import re
from email import message_from_string
from email.message import Message


CONTEXTUAL_CODE_PATTERNS = (
    re.compile(r"verification code[^0-9]{0,20}(\d{6,8})", re.IGNORECASE),
    re.compile(r"enter code[^0-9]{0,20}(\d{6,8})", re.IGNORECASE),
    re.compile(r"\bcode[^0-9]{0,20}(\d{6,8})\b", re.IGNORECASE),
)
FALLBACK_CODE_PATTERN = re.compile(r"\b(\d{6})\b")


def _extract_text_parts(message: Message) -> list[str]:
    if not message.is_multipart():
        payload = message.get_payload(decode=True)
        if payload is None:
            return [message.get_payload() or ""]
        return [payload.decode(message.get_content_charset() or "utf-8", errors="ignore")]

    contents: list[str] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        payload = part.get_payload(decode=True)
        text = payload.decode(part.get_content_charset() or "utf-8", errors="ignore") if payload else (part.get_payload() or "")
        contents.append(text)
    return contents


def _find_code(candidates: list[str], patterns: tuple[re.Pattern[str], ...]) -> str | None:
    for candidate in candidates:
        for pattern in patterns:
            match = pattern.search(candidate)
            if match:
                return match.group(1)
    return None


def extract_verification_code(raw_email: str) -> str:
    message = message_from_string(raw_email)
    subject = message.get("Subject", "")
    parts = _extract_text_parts(message)
    code = _find_code([subject, *parts, raw_email], CONTEXTUAL_CODE_PATTERNS)
    if code:
        return code
    code = _find_code(parts, (FALLBACK_CODE_PATTERN,))
    if code:
        return code
    raise ValueError("Verification code not found in email")
