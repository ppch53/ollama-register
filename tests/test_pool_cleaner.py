from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from pool_cleaner import PoolCleaner


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload


class FakeNewApi:
    def __init__(self, channels: list[dict], *, delete_statuses: dict[int, int] | None = None) -> None:
        self.channels = [dict(channel) for channel in channels]
        self.delete_statuses = delete_statuses or {}
        self.delete_calls: list[int] = []

    def list_all_channels(self, *, page_size: int = 100) -> list[dict]:
        return [dict(channel) for channel in self.channels]

    def get_channel(self, channel_id: int) -> FakeResponse:
        for channel in self.channels:
            if channel["id"] == channel_id:
                return FakeResponse(200, {"success": True, "data": dict(channel)})
        return FakeResponse(200, {"success": True, "data": None})

    def delete_channel(self, channel_id: int) -> FakeResponse:
        self.delete_calls.append(channel_id)
        status = self.delete_statuses.get(channel_id, 200)
        if status < 400:
            self.channels = [channel for channel in self.channels if channel["id"] != channel_id]
        return FakeResponse(status, {"success": status < 400})


class PoolCleanerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.state_file = self.root / "pool_state.json"
        self.backup_root = self.root / "backups"
        self.backup_root.mkdir()
        self.state_file.write_text(
            json.dumps(
                {
                    "puter": {"keys": [{"key": "p1", "healthy": True}]},
                    "ollama": {"keys": [{"key": "o1", "healthy": True}]},
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def make_cleaner(self, fake_newapi: FakeNewApi) -> PoolCleaner:
        cleaner = PoolCleaner(
            "http://gateway.test",
            "http://newapi.test",
            None,
            state_file=self.state_file,
            backup_root=self.backup_root,
            http_client=httpx.Client(),
        )
        cleaner.newapi = fake_newapi
        return cleaner

    def test_backup_creates_expected_files(self) -> None:
        fake_newapi = FakeNewApi(
            [
                {"id": 1, "name": "puter-1", "tag": "puter", "key": "p1", "type": 1, "models": "gpt-4o", "group": "default"},
                {"id": 2, "name": "ollama-1", "tag": "ollama", "key": "o1", "type": 1, "models": "gpt-oss:20b", "group": "default"},
            ]
        )
        cleaner = self.make_cleaner(fake_newapi)
        backup_dir = cleaner.backup_current_state()
        self.assertTrue((backup_dir / "pool_state.json").exists())
        self.assertTrue((backup_dir / "channels_dump.json").exists())
        self.assertTrue((backup_dir / "metadata.json").exists())
        self.assertEqual(
            self.state_file.read_text(encoding="utf-8"),
            (backup_dir / "pool_state.json").read_text(encoding="utf-8"),
        )

    def test_purge_preserves_ollama_pool(self) -> None:
        cleaner = self.make_cleaner(FakeNewApi([]))
        before = json.loads(self.state_file.read_text(encoding="utf-8"))["ollama"]["keys"]
        result = cleaner.purge_puter_pool(yes=True)
        after = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertFalse(result["dry_run"])
        self.assertEqual([], after["puter"]["keys"])
        self.assertEqual(before, after["ollama"]["keys"])

    def test_delete_stops_on_first_unexpected_failure(self) -> None:
        fake_newapi = FakeNewApi(
            [
                {"id": 1, "name": "puter-1", "tag": "puter", "type": 1, "base_url": "http://127.0.0.1:8001", "models": "gpt-4o", "group": "default", "status": 1},
                {"id": 2, "name": "puter-2", "tag": "puter", "type": 1, "base_url": "http://127.0.0.1:8001", "models": "gpt-4o", "group": "default", "status": 1},
                {"id": 10, "name": "ollama-1", "tag": "ollama", "type": 1, "base_url": "https://ollama.com", "models": "gpt-oss:20b", "group": "default", "status": 1},
                {"id": 11, "name": "ollama-2", "tag": "ollama", "type": 1, "base_url": "https://ollama.com", "models": "gpt-oss:20b", "group": "default", "status": 1},
                {"id": 12, "name": "ollama-3", "tag": "ollama", "type": 1, "base_url": "https://ollama.com", "models": "gpt-oss:20b", "group": "default", "status": 1},
                {"id": 13, "name": "ollama-4", "tag": "ollama", "type": 1, "base_url": "https://ollama.com", "models": "gpt-oss:20b", "group": "default", "status": 1},
            ],
            delete_statuses={1: 500},
        )
        cleaner = self.make_cleaner(fake_newapi)
        with self.assertRaises(RuntimeError):
            cleaner.delete_dead_channels(yes=True, expected_count=2, allow_count_mismatch=True)
        self.assertEqual([1], fake_newapi.delete_calls)

    def test_idempotent_when_puter_pool_already_empty(self) -> None:
        self.state_file.write_text(
            json.dumps(
                {
                    "puter": {"keys": []},
                    "ollama": {"keys": [{"key": "o1", "healthy": True}]},
                }
            ),
            encoding="utf-8",
        )
        cleaner = self.make_cleaner(FakeNewApi([]))
        result = cleaner.purge_puter_pool(yes=True)
        self.assertEqual(0, result["puter_keys_removed"])
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual([], state["puter"]["keys"])

    def test_delete_dry_run_writes_report_without_mutating(self) -> None:
        fake_newapi = FakeNewApi(
            [
                {"id": 1, "name": "puter-1", "tag": "puter", "type": 1, "base_url": "http://127.0.0.1:8001", "models": "gpt-4o", "group": "default", "status": 1},
                {"id": 10, "name": "ollama-1", "tag": "ollama", "type": 1, "base_url": "https://ollama.com", "models": "gpt-oss:20b", "group": "default", "status": 1},
            ]
        )
        cleaner = self.make_cleaner(fake_newapi)
        result = cleaner.delete_dead_channels(yes=False, expected_count=1)
        self.assertTrue(result["dry_run"])
        self.assertEqual([], fake_newapi.delete_calls)
        self.assertTrue(Path(result["report_path"]).exists())


if __name__ == "__main__":
    unittest.main()
