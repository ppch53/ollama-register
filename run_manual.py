"""Manual-email mode launcher for ollama-register.

Replaces TempMailClient with a stub that:
  - returns a user-provided email on create_address()
  - polls /opt/ollama-register/manual_code.txt on list_mails(),
    treating it as the verification code (6 digits)

Usage:
  MANUAL_EMAIL=foo@bar.com /opt/ollama-register/venv/bin/python run_manual.py
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.models import TempMailAddress, TempMailMessage

CODE_FILE = Path(os.environ.get("MANUAL_CODE_FILE", "/opt/ollama-register/manual_code.txt"))
MANUAL_EMAIL = os.environ["MANUAL_EMAIL"].strip()


class ManualTempMailClient:
    """Drop-in stand-in for TempMailClient."""

    def __init__(self, base_url: str = "", api_key: str = "", **_kwargs):
        self.base_url = base_url
        self.api_key = api_key
        self._returned_code = False

    def close(self) -> None:  # noqa: D401
        return None

    def create_address(self) -> TempMailAddress:
        now = datetime.now(timezone.utc)
        return TempMailAddress(
            address_id=1,
            address=MANUAL_EMAIL,
            jwt="manual",
            created_at=now.isoformat(timespec="seconds"),
            expires_at=(now + timedelta(hours=24)).isoformat(timespec="seconds"),
        )

    def list_mails(self, jwt: str, limit: int = 20, offset: int = 0):
        if self._returned_code:
            return []
        if not CODE_FILE.exists():
            return []
        code = CODE_FILE.read_text(encoding="utf-8").strip()
        if not code:
            return []
        # Build a minimal RFC822 raw email that extract_verification_code can parse
        raw = (
            "From: WorkOS <noreply@workos.com>\r\n"
            f"To: {MANUAL_EMAIL}\r\n"
            "Subject: Verification code\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            f"Your verification code is {code}\r\n"
        )
        # consume the code so it isn't reused
        self._returned_code = True
        try:
            CODE_FILE.unlink()
        except OSError:
            pass
        return [{"id": 1, "raw": raw, "subject": "Verification code"}]

    def get_mail(self, jwt: str, mail_id: int) -> TempMailMessage:
        # list_mails already returns raw inline, so this is normally not called.
        return TempMailMessage(mail_id=mail_id, subject="", raw="", created_at=None)


def main() -> None:
    import main as _m

    _m.TempMailClient = ManualTempMailClient
    print(f"[manual] using email = {MANUAL_EMAIL}", flush=True)
    print(f"[manual] waiting code at {CODE_FILE}", flush=True)
    _m.main()


if __name__ == "__main__":
    main()
