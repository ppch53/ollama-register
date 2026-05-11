"""MailboxProvider — abstract email provider with health scoring, cooldown,
one-time-use registry, and failure reason classification.
"""
from __future__ import annotations

import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from src.utils import atomic_write_json, load_json, utcnow_iso


class FailureReason(str, Enum):
    DELIVERY_TIMEOUT = "delivery_timeout"
    BOUNCE = "bounce"
    DOMAIN_BLOCKED = "domain_blocked"
    VERIFICATION_EXPIRED = "verification_expired"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class ProviderHealth:
    attempts: int = 0
    successes: int = 0
    consecutive_failures: int = 0
    last_failure_ts: str = ""
    cooldown_until_ts: str = ""
    failure_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def health_score(self) -> float:
        if self.attempts == 0:
            return 1.0
        return self.successes / self.attempts

    @property
    def in_cooldown(self) -> bool:
        if not self.cooldown_until_ts:
            return False
        from datetime import datetime, timezone
        try:
            cooldown_end = datetime.fromisoformat(self.cooldown_until_ts)
            return datetime.now(timezone.utc) < cooldown_end
        except (ValueError, TypeError):
            return False


class MailboxProvider(ABC):
    @abstractmethod
    async def create_address(self) -> str:
        """Create a new email address. Returns the email string."""
        ...

    @abstractmethod
    async def get_verification_code(self, email: str, timeout: float = 60.0) -> str | None:
        """Poll for verification code sent to the given email. Returns code or None."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        ...


class MailboxProviderPool:
    """Manages multiple mailbox providers with health scoring, rotation, and cooldown."""

    HEALTH_SCORE_THRESHOLD = 0.3
    CONSECUTIVE_FAILURE_COOLDOWN = 3
    COOLDOWN_SECONDS = 3600  # 1 hour

    def __init__(
        self,
        registry_path: Path,
        health_path: Path,
    ) -> None:
        self.registry_path = registry_path
        self.health_path = health_path
        self._providers: list[MailboxProvider] = []
        self._used_emails: set[str] = set()
        self._health: dict[str, ProviderHealth] = {}
        self._load_state()

    def _load_state(self) -> None:
        reg_data = load_json(self.registry_path)
        if isinstance(reg_data, list):
            self._used_emails = set(reg_data)

        health_data = load_json(self.health_path)
        if isinstance(health_data, dict):
            for name, data in health_data.items():
                h = ProviderHealth()
                h.attempts = data.get("attempts", 0)
                h.successes = data.get("successes", 0)
                h.consecutive_failures = data.get("consecutive_failures", 0)
                h.last_failure_ts = data.get("last_failure_ts", "")
                h.cooldown_until_ts = data.get("cooldown_until_ts", "")
                h.failure_reasons = data.get("failure_reasons", {})
                self._health[name] = h

    def _save_state(self) -> None:
        atomic_write_json(self.registry_path, sorted(self._used_emails))
        health_payload = {}
        for name, h in self._health.items():
            health_payload[name] = {
                "attempts": h.attempts,
                "successes": h.successes,
                "consecutive_failures": h.consecutive_failures,
                "last_failure_ts": h.last_failure_ts,
                "cooldown_until_ts": h.cooldown_until_ts,
                "failure_reasons": h.failure_reasons,
            }
        atomic_write_json(self.health_path, health_payload)

    def register_provider(self, provider: MailboxProvider) -> None:
        self._providers.append(provider)
        if provider.name not in self._health:
            self._health[provider.name] = ProviderHealth()

    def _get_health(self, provider: MailboxProvider) -> ProviderHealth:
        if provider.name not in self._health:
            self._health[provider.name] = ProviderHealth()
        return self._health[provider.name]

    def _available_providers(self) -> list[MailboxProvider]:
        available = []
        for p in self._providers:
            h = self._get_health(p)
            if h.in_cooldown:
                continue
            if h.health_score < self.HEALTH_SCORE_THRESHOLD and h.attempts >= 3:
                continue
            available.append(p)
        return available

    async def create_address(self) -> tuple[str, MailboxProvider]:
        """Create a new email from a randomly selected available provider.

        Raises RuntimeError if no providers are available.
        """
        available = self._available_providers()
        if not available:
            raise RuntimeError(
                "No mailbox providers available (all in cooldown or below health threshold)"
            )

        random.shuffle(available)
        last_error: Exception | None = None

        for provider in available:
            try:
                email = await provider.create_address()
                if email in self._used_emails:
                    continue
                self._used_emails.add(email)
                self._save_state()
                return email, provider
            except Exception as e:
                last_error = e
                self._record_failure(provider, FailureReason.UNKNOWN)
                continue

        raise RuntimeError(f"All providers failed. Last error: {last_error}")

    def record_success(self, provider: MailboxProvider) -> None:
        h = self._get_health(provider)
        h.attempts += 1
        h.successes += 1
        h.consecutive_failures = 0
        self._save_state()

    def _record_failure(self, provider: MailboxProvider, reason: FailureReason) -> None:
        h = self._get_health(provider)
        h.attempts += 1
        h.consecutive_failures += 1
        h.last_failure_ts = utcnow_iso()
        h.failure_reasons[reason.value] = h.failure_reasons.get(reason.value, 0) + 1

        if h.consecutive_failures >= self.CONSECUTIVE_FAILURE_COOLDOWN:
            from datetime import datetime, timedelta, timezone
            cooldown_end = datetime.now(timezone.utc) + timedelta(seconds=self.COOLDOWN_SECONDS)
            h.cooldown_until_ts = cooldown_end.isoformat()

        self._save_state()

    def record_failure(
        self, provider: MailboxProvider, reason: FailureReason = FailureReason.UNKNOWN
    ) -> None:
        self._record_failure(provider, reason)

    def is_email_used(self, email: str) -> bool:
        return email in self._used_emails

    def get_failure_distribution(self) -> dict[str, int]:
        total: dict[str, int] = {}
        for h in self._health.values():
            for reason, count in h.failure_reasons.items():
                total[reason] = total.get(reason, 0) + count
        return total
