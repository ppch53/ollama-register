from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from src.models import AppConfig


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


_REQUIRED_KEYS = (
    "SIGN_UP_URL",
    "SETTINGS_KEYS_URL",
    "TEMPMAIL_BASE_URL",
    "TEMPMAIL_API_KEY",
    "TURNSTILE_SOLVER_URL",
    "FLARESOLVERR_URL",
)


def _get_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _get_optional(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _get_optional_path(name: str) -> Path | None:
    value = os.getenv(name, "").strip()
    return Path(value) if value else None


def _get_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"Invalid boolean value for {name}: {raw_value}")



def _get_float(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        return float(raw_value)
    except ValueError as exc:
        raise ConfigError(f"Invalid float value for {name}: {raw_value}") from exc


def _get_optional_float(name: str) -> float | None:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return None
    try:
        return float(raw_value)
    except ValueError as exc:
        raise ConfigError(f"Invalid float value for {name}: {raw_value}") from exc


def _get_optional_int(name: str) -> int | None:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return None
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ConfigError(f"Invalid integer value for {name}: {raw_value}") from exc



def load_config(env_file: str | None = ".env") -> AppConfig:
    if env_file:
        load_dotenv(env_file, override=False)

    return AppConfig(
        sign_up_url=_get_required("SIGN_UP_URL"),
        settings_keys_url=_get_required("SETTINGS_KEYS_URL"),
        tempmail_base_url=_get_required("TEMPMAIL_BASE_URL"),
        tempmail_api_key=_get_required("TEMPMAIL_API_KEY"),
        turnstile_solver_url=_get_required("TURNSTILE_SOLVER_URL"),
        flaresolverr_url=_get_required("FLARESOLVERR_URL"),
        hero_sms_base_url=os.getenv("HERO_SMS_BASE_URL", "https://hero-sms.com").strip() or "https://hero-sms.com",
        hero_sms_api_key=_get_optional("HERO_SMS_API_KEY"),
        hero_sms_service=_get_optional("HERO_SMS_SERVICE"),
        hero_sms_country_id=_get_optional_int("HERO_SMS_COUNTRY_ID"),
        hero_sms_operator=_get_optional("HERO_SMS_OPERATOR"),
        hero_sms_max_price=_get_optional_float("HERO_SMS_MAX_PRICE"),
        hero_sms_fixed_price=_get_bool("HERO_SMS_FIXED_PRICE", False),
        hero_sms_phone_exception=_get_optional("HERO_SMS_PHONE_EXCEPTION"),
        hero_sms_poll_interval_seconds=_get_float("HERO_SMS_POLL_INTERVAL_SECONDS", 3.0),
        hero_sms_poll_timeout_seconds=_get_float("HERO_SMS_POLL_TIMEOUT_SECONDS", 180.0),
        accounts_file=Path(os.getenv("ACCOUNTS_FILE", "accounts.json")),
        api_key_file=Path(os.getenv("APIKEY_FILE", "apikey.txt")),
        api_key_validation_url=os.getenv("API_KEY_VALIDATION_URL", "https://ollama.com/api/tags").strip() or "https://ollama.com/api/tags",
        artifacts_dir=_get_optional_path("ARTIFACTS_DIR"),
        browser_headless=_get_bool("BROWSER_HEADLESS", True),
        playwright_proxy_server=os.getenv("PLAYWRIGHT_PROXY_SERVER") or None,
        registration_proxy=os.getenv("REGISTER_PROXY") or os.getenv("PLAYWRIGHT_PROXY_SERVER") or None,
        ollama_profile_root=Path(os.getenv("OLLAMA_PROFILE_ROOT", "ollama_profiles")),
        ollama_fingerprint_registry=Path(
            os.getenv("OLLAMA_FINGERPRINT_REGISTRY", "ollama_fingerprints.json")
        ),
        ollama_fingerprint_country=_get_optional("OLLAMA_FINGERPRINT_COUNTRY")
        or _get_optional("PROXY_COUNTRY"),
        default_timeout_seconds=_get_float("DEFAULT_TIMEOUT_SECONDS", 30.0),
        mail_poll_interval_seconds=_get_float("MAIL_POLL_INTERVAL_SECONDS", 3.0),
        mail_poll_timeout_seconds=_get_float("MAIL_POLL_TIMEOUT_SECONDS", 60.0),
        turnstile_poll_interval_seconds=_get_float("TURNSTILE_POLL_INTERVAL_SECONDS", 2.0),
        turnstile_poll_timeout_seconds=_get_float("TURNSTILE_POLL_TIMEOUT_SECONDS", 120.0),
        rate_limit_state_file=Path(os.getenv("RATE_LIMIT_STATE_FILE", ".ollama-rate-limit.json")),
        ollama_max_per_day=int(os.getenv("OLLAMA_MAX_PER_DAY", "10")),
        ollama_min_interval_minutes=int(os.getenv("OLLAMA_MIN_INTERVAL_MINUTES", "10")),
    )
