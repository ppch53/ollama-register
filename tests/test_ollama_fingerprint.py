from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.ollama_fingerprint import OllamaBrowserProfileManager


class OllamaFingerprintTests(unittest.TestCase):
    def test_profile_uses_country_consistent_browser_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            manager = OllamaBrowserProfileManager(root / "profiles", root / "registry.json")

            profile = manager.create(account_hint="user@example.com", country="GB")

            self.assertEqual("GB", profile.country)
            self.assertEqual("en-GB", profile.locale)
            self.assertEqual("en-GB", profile.language)
            self.assertEqual("Europe/London", profile.timezone)
            self.assertTrue(profile.profile_dir.exists())
            self.assertIn("Chrome/", profile.user_agent)
            self.assertIn(profile.viewport["width"], {1366, 1440, 1536, 1600, 1920})

    def test_profile_is_persisted_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            registry = root / "registry.json"
            manager = OllamaBrowserProfileManager(root / "profiles", registry)

            profile = manager.create(account_hint="user@example.com", country="US")
            payload = json.loads(registry.read_text(encoding="utf-8"))

            self.assertIn(profile.profile_id, payload)
            saved = payload[profile.profile_id]
            self.assertEqual("US", saved["country"])
            self.assertNotIn("password", json.dumps(saved).lower())
            self.assertNotIn("api_key", json.dumps(saved).lower())


if __name__ == "__main__":
    unittest.main()
