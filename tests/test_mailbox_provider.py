from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.mailbox_provider import FailureReason, MailboxProvider, MailboxProviderPool


class FakeMailboxProvider(MailboxProvider):
    def __init__(self, name: str, addresses: list[str], *, fail: bool = False) -> None:
        self._name = name
        self.addresses = list(addresses)
        self.fail = fail

    @property
    def name(self) -> str:
        return self._name

    async def create_address(self) -> str:
        if self.fail:
            raise RuntimeError("provider failed")
        if not self.addresses:
            raise RuntimeError("no addresses left")
        return self.addresses.pop(0)

    async def get_verification_code(self, email: str, timeout: float = 60.0) -> str | None:
        return "123456"


class MailboxProviderPoolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.registry_path = self.root / "used_emails.json"
        self.health_path = self.root / "mailbox_health.json"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def make_pool(self) -> MailboxProviderPool:
        return MailboxProviderPool(
            registry_path=self.registry_path,
            health_path=self.health_path,
        )

    async def test_create_address_persists_used_email(self) -> None:
        pool = self.make_pool()
        provider = FakeMailboxProvider("fake", ["user@example.com"])
        pool.register_provider(provider)

        email, selected = await pool.create_address()

        self.assertEqual("user@example.com", email)
        self.assertIs(selected, provider)
        self.assertTrue(pool.is_email_used("user@example.com"))

        reloaded = self.make_pool()
        self.assertTrue(reloaded.is_email_used("user@example.com"))

    async def test_provider_enters_cooldown_after_consecutive_failures(self) -> None:
        pool = self.make_pool()
        provider = FakeMailboxProvider("flaky", [], fail=True)
        pool.register_provider(provider)

        for _ in range(pool.CONSECUTIVE_FAILURE_COOLDOWN):
            pool.record_failure(provider, FailureReason.DELIVERY_TIMEOUT)

        with self.assertRaises(RuntimeError):
            await pool.create_address()
        self.assertEqual(
            {FailureReason.DELIVERY_TIMEOUT.value: pool.CONSECUTIVE_FAILURE_COOLDOWN},
            pool.get_failure_distribution(),
        )


if __name__ == "__main__":
    unittest.main()
