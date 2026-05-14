"""Outlook mailbox provider for Puter v2 registration."""

from __future__ import annotations

import asyncio
from pathlib import Path

from outlook_inbox import OutlookAccount, acquire_unused, mark_used, wait_for_code

from src.mailbox_provider import MailboxProvider


class OutlookMailboxProvider(MailboxProvider):
    def __init__(self, pool_path: Path, used_path: Path) -> None:
        self.pool_path = pool_path
        self.used_path = used_path
        self._accounts_by_email: dict[str, OutlookAccount] = {}

    @property
    def name(self) -> str:
        return "outlook"

    async def create_address(self) -> str:
        account = await asyncio.to_thread(acquire_unused, self.pool_path, self.used_path)
        self._accounts_by_email[account.email] = account
        await asyncio.to_thread(mark_used, self.used_path, account.email, "puter_v2")
        return account.email

    async def get_verification_code(self, email: str, timeout: float = 60.0) -> str | None:
        account = self._accounts_by_email.get(email)
        if account is None:
            return None
        try:
            return await asyncio.to_thread(
                wait_for_code,
                account,
                sender_hint="puter",
                timeout=int(timeout),
            )
        except TimeoutError:
            return None
