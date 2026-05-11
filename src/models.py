from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class AppConfig:
    sign_up_url: str
    settings_keys_url: str
    tempmail_base_url: str
    tempmail_api_key: str
    turnstile_solver_url: str
    flaresolverr_url: str
    hero_sms_base_url: str
    hero_sms_api_key: str | None
    hero_sms_service: str | None
    hero_sms_country_id: int | None
    hero_sms_operator: str | None
    hero_sms_max_price: float | None
    hero_sms_fixed_price: bool
    hero_sms_phone_exception: str | None
    hero_sms_poll_interval_seconds: float
    hero_sms_poll_timeout_seconds: float
    accounts_file: Path
    api_key_file: Path
    api_key_validation_url: str
    artifacts_dir: Path | None
    browser_headless: bool
    playwright_proxy_server: str | None
    registration_proxy: str | None
    ollama_sticky_proxy: bool
    ollama_profile_root: Path
    ollama_fingerprint_registry: Path
    ollama_fingerprint_country: str | None
    default_timeout_seconds: float
    mail_poll_interval_seconds: float
    mail_poll_timeout_seconds: float
    turnstile_poll_interval_seconds: float
    turnstile_poll_timeout_seconds: float
    rate_limit_state_file: Path
    ollama_max_per_day: int
    ollama_min_interval_minutes: int


@dataclass(slots=True)
class TempMailAddress:
    address_id: int
    address: str
    jwt: str
    created_at: str
    expires_at: str


@dataclass(slots=True)
class TempMailMessage:
    mail_id: int
    subject: str | None
    raw: str
    created_at: str | None = None


@dataclass(slots=True)
class FlareSolverrSolution:
    cookies: list[dict[str, Any]]
    user_agent: str | None
    response: str | None = None
    status_code: int | None = None


@dataclass(slots=True)
class AccountRecord:
    email: str
    password: str
    api_key: str
    cookies: list[dict[str, Any]]
    status: str = "verified"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AccountRecord":
        return cls(
            email=str(payload["email"]),
            password=str(payload["password"]),
            api_key=str(payload["api_key"]),
            cookies=list(payload.get("cookies", [])),
            status=str(payload.get("status") or "verified"),
        )
