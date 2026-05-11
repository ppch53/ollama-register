"""ProfileManager — isolated Camoufox/Firefox profile directories per account."""
from __future__ import annotations

import os
import shutil
from pathlib import Path


class ProfileManager:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def create_profile(self, account_id: str) -> Path:
        profile_dir = self.root / account_id
        profile_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(profile_dir, 0o700)
        return profile_dir

    def get_profile(self, account_id: str) -> Path | None:
        profile_dir = self.root / account_id
        return profile_dir if profile_dir.exists() else None

    def delete_profile(self, account_id: str) -> None:
        profile_dir = self.root / account_id
        if profile_dir.exists():
            shutil.rmtree(profile_dir)

    def list_profiles(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(
            entry.name
            for entry in self.root.iterdir()
            if entry.is_dir() and not entry.name.startswith(".")
        )

    def profile_path(self, account_id: str) -> Path:
        return self.root / account_id
