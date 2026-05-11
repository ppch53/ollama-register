"""Natural username generation — varied human-looking patterns."""
from __future__ import annotations

import random
from pathlib import Path

from src.utils import load_json, atomic_write_json

_ADJECTIVES = [
    "cool", "fast", "blue", "dark", "wild", "calm", "deep", "pure",
    "warm", "keen", "bold", "soft", "mild", "pale", "rich", "wise",
    "rare", "true", "safe", "tall", "neon", "gold", "iron", "jade",
    "lone", "mist", "opal", "pine", "rain", "sage", "silk", "snow",
    "star", "tide", "vale", "wren", "zinc", "glad", "grim", "hush",
]

_NOUNS = [
    "wolf", "bear", "hawk", "fox", "lynx", "deer", "crow", "pike",
    "fern", "moss", "reed", "vine", "cliff", "creek", "dune", "fjord",
    "grove", "knoll", "marsh", "oasis", "peak", "ridge", "summit",
    "bluff", "delta", "field", "flint", "forge", "haven", "lodge",
    "quest", "storm", "trail", "vista", "anvil", "basin", "cedar",
    "drift", "ember", "frost", "gleam", "ivory", "jewel",
]

_FIRST_NAMES = [
    "alex", "sam", "jordan", "taylor", "casey", "morgan", "riley",
    "quinn", "avery", "dakota", "skyler", "blake", "drew", "eden",
    "hayden", "jamie", "kai", "logan", "mason", "noah", "owen",
    "phoenix", "reese", "sawyer", "tyler", "wesley", "zara", "luca",
    "nora", "milo", "aria", "leah", "zoe", "ivy", "eli", "max",
    "leo", "ava", "emma", "liam", "ethan", "nina", "ruby", "jade",
]


class UsernameGenerator:
    def __init__(self, registry_path: Path) -> None:
        self.registry_path = registry_path
        self._used: set[str] = set()
        self._load()

    def _load(self) -> None:
        data = load_json(self.registry_path)
        if isinstance(data, list):
            self._used = set(data)

    def _save(self) -> None:
        atomic_write_json(self.registry_path, sorted(self._used))

    def generate(self, max_attempts: int = 20) -> str:
        for _ in range(max_attempts):
            username = self._random_username()
            if username not in self._used and 6 <= len(username) <= 16:
                self._used.add(username)
                self._save()
                return username
        raise RuntimeError(f"Failed to generate unique username after {max_attempts} attempts")

    def _random_username(self) -> str:
        r = random.random()
        if r < 0.35:
            return self._first_name_number()
        elif r < 0.65:
            return self._adjective_noun()
        elif r < 0.85:
            return self._noun_number()
        else:
            return self._first_name_noun()

    def _first_name_number(self) -> str:
        name = random.choice(_FIRST_NAMES)
        num = random.randint(10, 9999)
        sep = random.choice(["", ".", "_"])
        return f"{name}{sep}{num}"

    def _adjective_noun(self) -> str:
        adj = random.choice(_ADJECTIVES)
        noun = random.choice(_NOUNS)
        sep = random.choice(["", "_", "."])
        num = random.randint(0, 99)
        if random.random() < 0.5:
            return f"{adj}{sep}{noun}"
        return f"{adj}{sep}{noun}{num}"

    def _noun_number(self) -> str:
        noun = random.choice(_NOUNS)
        num = random.randint(100, 9999)
        return f"{noun}{num}"

    def _first_name_noun(self) -> str:
        name = random.choice(_FIRST_NAMES)
        noun = random.choice(_NOUNS)
        sep = random.choice(["", "_"])
        return f"{name}{sep}{noun}"

    def is_used(self, username: str) -> bool:
        return username in self._used

    def register_existing(self, username: str) -> None:
        self._used.add(username)
        self._save()
