"""Lightweight browser fingerprint profiles for the Ollama registration flow."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from src.utils import atomic_write_json, load_json

STANDARD_CHROME_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

COUNTRY_PRESETS: dict[str, dict[str, str]] = {
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
DEFAULT_COUNTRY = "US"
VIEWPORTS = (
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1600, "height": 900},
    {"width": 1920, "height": 1080},
)


@dataclass(slots=True)
class OllamaBrowserProfile:
    profile_id: str
    country: str
    locale: str
    language: str
    timezone: str
    user_agent: str
    viewport: dict[str, int]
    profile_dir: Path
    seed: str

    def fingerprint_hash(self) -> str:
        payload = self.to_dict()
        payload["profile_dir"] = str(payload["profile_dir"])
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["profile_dir"] = str(self.profile_dir)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> OllamaBrowserProfile:
        data = dict(payload)
        data["profile_dir"] = Path(str(data["profile_dir"]))
        data["viewport"] = {
            "width": int(data["viewport"]["width"]),
            "height": int(data["viewport"]["height"]),
        }
        return cls(**data)


class OllamaBrowserProfileManager:
    def __init__(self, root: Path, registry_path: Path) -> None:
        self.root = Path(root)
        self.registry_path = Path(registry_path)
        self._registry: dict[str, OllamaBrowserProfile] = {}
        self._load()

    def _load(self) -> None:
        payload = load_json(self.registry_path)
        if isinstance(payload, dict):
            for profile_id, data in payload.items():
                self._registry[str(profile_id)] = OllamaBrowserProfile.from_dict(data)

    def _save(self) -> None:
        atomic_write_json(
            self.registry_path,
            {profile_id: profile.to_dict() for profile_id, profile in self._registry.items()},
        )

    def create(self, *, account_hint: str, country: str | None = None) -> OllamaBrowserProfile:
        normalized_country = (country or DEFAULT_COUNTRY).upper()
        preset = COUNTRY_PRESETS.get(normalized_country, COUNTRY_PRESETS[DEFAULT_COUNTRY])
        if normalized_country not in COUNTRY_PRESETS:
            normalized_country = DEFAULT_COUNTRY
        seed = hashlib.sha256(account_hint.encode()).hexdigest()
        rng = random.Random(seed)
        profile_id = hashlib.sha256(f"ollama:{account_hint}".encode()).hexdigest()[:16]
        profile_dir = self.root / profile_id
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile = OllamaBrowserProfile(
            profile_id=profile_id,
            country=normalized_country,
            locale=preset["locale"],
            language=preset["language"],
            timezone=preset["timezone"],
            user_agent=STANDARD_CHROME_USER_AGENT,
            viewport=dict(rng.choice(VIEWPORTS)),
            profile_dir=profile_dir,
            seed=seed,
        )
        self._registry[profile_id] = profile
        self._save()
        return profile

    def get(self, profile_id: str) -> OllamaBrowserProfile | None:
        return self._registry.get(profile_id)
