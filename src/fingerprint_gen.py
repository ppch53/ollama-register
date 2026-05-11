"""FingerprintGenerator — uses Camoufox built-in BrowserForge for fingerprint injection.

Camoufox handles fingerprint generation at the C++ level (not JS patches).
This module generates and persists per-account fingerprint configs with country
consistency (timezone, locale, language must match proxy country).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from src.utils import atomic_write_json, load_json, new_uuid


@dataclass(slots=True)
class FingerprintConfig:
    fingerprint_id: str
    country: str
    browser_locale: str
    browser_language: str
    timezone: str
    typing_profile: str  # slow | medium | fast
    error_rate: float  # 0.0–0.03
    mouse_style: str  # smooth | jerky | cautious
    seed: str

    def fingerprint_hash(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FingerprintConfig:
        return cls(**data)


# Country presets — timezone, locale, language must be consistent
_COUNTRY_PRESETS: dict[str, dict[str, str]] = {
    "US": {"timezone": "America/Los_Angeles", "locale": "en-US", "language": "en-US"},
    "GB": {"timezone": "Europe/London", "locale": "en-GB", "language": "en-GB"},
    "DE": {"timezone": "Europe/Berlin", "locale": "de-DE", "language": "de-DE"},
    "FR": {"timezone": "Europe/Paris", "locale": "fr-FR", "language": "fr-FR"},
    "JP": {"timezone": "Asia/Tokyo", "locale": "ja-JP", "language": "ja-JP"},
    "KR": {"timezone": "Asia/Seoul", "locale": "ko-KR", "language": "ko-KR"},
    "BR": {"timezone": "America/Sao_Paulo", "locale": "pt-BR", "language": "pt-BR"},
    "CA": {"timezone": "America/Toronto", "locale": "en-CA", "language": "en-CA"},
    "AU": {"timezone": "Australia/Sydney", "locale": "en-AU", "language": "en-AU"},
    "NL": {"timezone": "Europe/Amsterdam", "locale": "nl-NL", "language": "nl-NL"},
}

_DEFAULT_COUNTRY = "US"

_TYPING_PROFILES = ("slow", "medium", "fast")
_MOUSE_STYLES = ("smooth", "jerky", "cautious")


class FingerprintGenerator:
    def __init__(self, registry_path: Path) -> None:
        self.registry_path = registry_path
        self._registry: dict[str, FingerprintConfig] = {}
        self._load_registry()

    def _load_registry(self) -> None:
        data = load_json(self.registry_path)
        if isinstance(data, dict):
            for account_id, fp_data in data.items():
                self._registry[account_id] = FingerprintConfig.from_dict(fp_data)

    def _save_registry(self) -> None:
        payload = {aid: fp.to_dict() for aid, fp in self._registry.items()}
        atomic_write_json(self.registry_path, payload)

    def generate(self, country: str | None = None) -> FingerprintConfig:
        country = (country or _DEFAULT_COUNTRY).upper()
        preset = _COUNTRY_PRESETS.get(country, _COUNTRY_PRESETS[_DEFAULT_COUNTRY])

        import random

        seed = new_uuid()
        r = random.Random(seed)
        typing_profile = r.choice(_TYPING_PROFILES)
        error_rate = r.uniform(0.0, 0.03)
        mouse_style = r.choice(_MOUSE_STYLES)

        return FingerprintConfig(
            fingerprint_id=new_uuid(),
            country=country,
            browser_locale=preset["locale"],
            browser_language=preset["language"],
            timezone=preset["timezone"],
            typing_profile=typing_profile,
            error_rate=error_rate,
            mouse_style=mouse_style,
            seed=seed,
        )

    def generate_unique(self, country: str | None = None, max_attempts: int = 5) -> FingerprintConfig:
        existing_hashes = {fp.fingerprint_hash() for fp in self._registry.values()}
        for _ in range(max_attempts):
            fp = self.generate(country)
            if fp.fingerprint_hash() not in existing_hashes:
                return fp
        raise RuntimeError(
            f"Failed to generate unique fingerprint after {max_attempts} attempts"
        )

    def register(self, account_id: str, fp: FingerprintConfig) -> None:
        self._registry[account_id] = fp
        self._save_registry()

    def get(self, account_id: str) -> FingerprintConfig | None:
        return self._registry.get(account_id)

    def save(self, account_id: str, fp: FingerprintConfig) -> None:
        self.register(account_id, fp)

    def load(self, account_id: str) -> FingerprintConfig | None:
        return self.get(account_id)

    def get_camoufox_kwargs(self, fp: FingerprintConfig, headless: bool = True) -> dict[str, Any]:
        """Return kwargs to pass to AsyncCamoufox() for this fingerprint config.

        Camoufox handles fingerprint injection at the C++ level via BrowserForge.
        We pass geoip=True so Camoufox auto-resolves IP-based geolocation, and
        set the locale/timezone explicitly from the fingerprint config.
        """
        return {
            "geoip": True,
            "humanize": True,
            "locale": fp.browser_locale,
            "headless": headless,
            "addons": [],
            "config": {
                "screen": {
                    "width": 1920,
                    "height": 1080,
                },
            },
        }
