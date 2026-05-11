from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from src.logging_config import StructuredRunLogger, configure_structured_logging

DEFAULT_STATE_FILE = Path("/opt/ollama-register/pool_state.json")
DEFAULT_BACKUP_ROOT = Path("/opt/backups")
DEFAULT_CHANNEL_PAGE_SIZE = 100
PUTER_ADAPTER_HINTS = ("127.0.0.1:8001", "puter-adapter", "api.puter.com", "puter.com")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            shutil.copystat(path, tmp_path)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def channel_to_create_request(channel: dict[str, Any]) -> dict[str, Any]:
    required = ("name", "type", "key", "models", "group")
    missing = [field for field in required if not channel.get(field)]
    if missing:
        raise ValueError(f"channel is missing required fields: {missing}")

    payload = {
        "name": channel["name"],
        "type": channel["type"],
        "key": channel["key"],
        "base_url": channel.get("base_url") or "",
        "models": channel["models"],
        "group": channel["group"],
        "tag": channel.get("tag"),
        "priority": channel.get("priority", 0),
        "weight": channel.get("weight", 1),
        "status": channel.get("status", 1),
        "test_model": channel.get("test_model"),
        "auto_ban": channel.get("auto_ban", 1),
        "model_mapping": channel.get("model_mapping"),
        "openai_organization": channel.get("openai_organization"),
        "other": channel.get("other", ""),
        "other_info": channel.get("other_info", ""),
        "param_override": channel.get("param_override"),
        "header_override": channel.get("header_override"),
        "remark": channel.get("remark"),
        "settings": channel.get("settings") or channel.get("other_settings"),
    }
    cleaned = {key: value for key, value in payload.items() if value not in (None, "")}
    return {"mode": "single", "channel": cleaned}


@dataclass(slots=True)
class NewApiSession:
    base_url: str
    client: httpx.Client
    session_cookie: str | None = None
    user_id: str | None = None
    username: str | None = None
    password: str | None = None

    def ensure_authenticated(self) -> None:
        if self.session_cookie and self.user_id:
            return
        if not self.username or not self.password:
            raise RuntimeError("NEWAPI_USERNAME and NEWAPI_PASSWORD are required when NEWAPI_SESSION is not provided")
        response = self.client.post(
            f"{self.base_url}/api/user/login",
            json={"username": self.username, "password": self.password},
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise RuntimeError(f"new-api login failed: {payload}")
        user = payload.get("data") or {}
        self.user_id = str(user.get("id"))
        self.session_cookie = response.headers.get("set-cookie", "").split(";", 1)[0]
        if not self.session_cookie or not self.user_id:
            raise RuntimeError("new-api login succeeded but session cookie or user id is missing")

    def _headers(self) -> dict[str, str]:
        self.ensure_authenticated()
        return {
            "Cookie": self.session_cookie or "",
            "New-Api-User": self.user_id or "",
            "Content-Type": "application/json",
        }

    def list_channels_page(self, *, page: int, page_size: int) -> tuple[list[dict[str, Any]], int]:
        response = self.client.get(
            f"{self.base_url}/api/channel/",
            params={"p": page, "page_size": page_size},
            headers=self._headers(),
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") or {}
        return list(data.get("items") or []), int(data.get("total") or 0)

    def list_all_channels(self, *, page_size: int = DEFAULT_CHANNEL_PAGE_SIZE) -> list[dict[str, Any]]:
        channels: list[dict[str, Any]] = []
        page = 0
        total = None
        while total is None or len(channels) < total:
            items, total = self.list_channels_page(page=page, page_size=page_size)
            channels.extend(items)
            if not items:
                break
            page += 1
        return channels

    def get_channel(self, channel_id: int) -> httpx.Response:
        return self.client.get(
            f"{self.base_url}/api/channel/{channel_id}",
            headers=self._headers(),
        )

    def delete_channel(self, channel_id: int) -> httpx.Response:
        return self.client.delete(
            f"{self.base_url}/api/channel/{channel_id}",
            headers=self._headers(),
        )

    def create_channel(self, payload: dict[str, Any]) -> httpx.Response:
        return self.client.post(
            f"{self.base_url}/api/channel/",
            headers=self._headers(),
            json=payload,
        )


class PoolCleaner:
    def __init__(
        self,
        gateway_url: str,
        newapi_url: str,
        newapi_token: str | None,
        *,
        state_file: Path = DEFAULT_STATE_FILE,
        backup_root: Path = DEFAULT_BACKUP_ROOT,
        http_client: httpx.Client | None = None,
        logger: StructuredRunLogger | None = None,
    ) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.newapi_url = newapi_url.rstrip("/")
        self.state_file = Path(state_file)
        self.backup_root = Path(backup_root)
        self.logger = logger or configure_structured_logging("pool_cleaner")
        self._owns_client = http_client is None
        self.client = http_client or httpx.Client(timeout=30.0, follow_redirects=True)

        session_cookie = None
        user_id = None
        if newapi_token:
            parsed = self._parse_session_token(newapi_token)
            session_cookie = parsed.get("cookie")
            user_id = parsed.get("user_id")

        self.newapi = NewApiSession(
            base_url=self.newapi_url,
            client=self.client,
            session_cookie=session_cookie,
            user_id=user_id,
            username=os.getenv("NEWAPI_USERNAME"),
            password=os.getenv("NEWAPI_PASSWORD"),
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    @staticmethod
    def _parse_session_token(raw: str) -> dict[str, str]:
        if raw.strip().startswith("{"):
            payload = json.loads(raw)
            return {
                "cookie": str(payload.get("cookie") or ""),
                "user_id": str(payload.get("user_id") or ""),
            }
        cookie = ""
        user_id = ""
        for segment in raw.split("|"):
            if segment.startswith("cookie="):
                cookie = segment.split("=", 1)[1]
            elif segment.startswith("user_id="):
                user_id = segment.split("=", 1)[1]
        return {"cookie": cookie, "user_id": user_id}

    def verified_api_summary(self) -> dict[str, Any]:
        return {
            "new_api": {
                "list_endpoint": "GET /api/channel/?p=<page>&page_size=<size>",
                "create_endpoint": "POST /api/channel/",
                "delete_endpoint": "DELETE /api/channel/{id}",
                "requires_headers": ["Cookie", "New-Api-User"],
                "delete_runtime_cache_refresh": "controller.DeleteChannel calls model.InitChannelCache() immediately",
                "delete_missing_behavior": "observed live behavior returns HTTP 200 success=true for a missing id",
            },
            "pool_gateway": {
                "state_load": "startup only",
                "pool_state_hot_reload": False,
                "state_file_locking": "state file is opened on load/persist only; no persistent file handle is kept open",
            },
            "upstreams": {
                "puter_health_endpoint": "GET https://api.puter.com/whoami",
                "puter_auth_scheme": "Authorization: Bearer <JWT>",
                "puter_suspended_response": "HTTP 403 with Account suspended payload",
                "ollama_validation_endpoint": "GET https://ollama.com/api/tags",
                "ollama_auth_scheme": "Authorization: Bearer <api_key>",
                "ollama_success_response": "HTTP 200 JSON model list",
            },
        }

    def backup_current_state(self) -> Path:
        channels = self.newapi.list_all_channels(page_size=DEFAULT_CHANNEL_PAGE_SIZE)
        channel_bytes = json.dumps(channels, ensure_ascii=False).encode("utf-8")
        state_size = self.state_file.stat().st_size if self.state_file.exists() else 0
        expected_size = state_size + len(channel_bytes) + 8192
        disk_probe_path = self.backup_root.parent if self.backup_root.parent.exists() else self.backup_root
        free_space = shutil.disk_usage(disk_probe_path).free
        if free_space < expected_size + 100 * 1024 * 1024:
            raise RuntimeError(
                f"Insufficient free space under {disk_probe_path}: "
                f"need at least {expected_size + 100 * 1024 * 1024} bytes, have {free_space}"
            )

        timestamp = str(int(time.time()))
        backup_dir = self.backup_root / timestamp
        backup_dir.mkdir(parents=True, exist_ok=False)
        self.logger.info("backup", "created backup directory", backup_dir=str(backup_dir))

        if self.state_file.exists():
            copied_state = backup_dir / "pool_state.json"
            shutil.copy2(self.state_file, copied_state)
            if sha256_file(self.state_file) != sha256_file(copied_state):
                raise RuntimeError("pool_state.json backup hash mismatch")

        channels_dump_path = backup_dir / "channels_dump.json"
        channels_dump_path.write_text(
            json.dumps(channels, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        metadata = {
            "timestamp": timestamp,
            "hostname": socket.gethostname(),
            "source_paths": {
                "pool_state": str(self.state_file),
                "channels": f"{self.newapi_url}/api/channel/",
            },
            "channel_count": len(channels),
            "puter_candidate_count": sum(1 for channel in channels if (channel.get("tag") or "") == "puter"),
            "ollama_count": sum(1 for channel in channels if (channel.get("tag") or "") == "ollama"),
            "verified_api_summary": self.verified_api_summary(),
        }
        (backup_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return backup_dir

    def purge_puter_pool(self, *, yes: bool, restart_command: str | None = None) -> dict[str, Any]:
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        original_ollama = copy.deepcopy((state.get("ollama") or {}).get("keys") or [])
        original_puter = copy.deepcopy((state.get("puter") or {}).get("keys") or [])
        if not yes:
            return {
                "dry_run": True,
                "puter_keys_to_remove": len(original_puter),
                "ollama_keys_to_preserve": len(original_ollama),
            }

        state.setdefault("puter", {})["keys"] = []
        atomic_write_json(self.state_file, state)
        reloaded = json.loads(self.state_file.read_text(encoding="utf-8"))
        if (reloaded.get("puter") or {}).get("keys") != []:
            raise RuntimeError("puter pool purge verification failed")
        if (reloaded.get("ollama") or {}).get("keys") != original_ollama:
            raise RuntimeError("ollama keys changed during puter pool purge")
        if restart_command:
            subprocess.run(restart_command, shell=True, check=True)
        return {
            "dry_run": False,
            "puter_keys_removed": len(original_puter),
            "ollama_keys_preserved": len(original_ollama),
        }

    def list_dead_channels(self, *, report_path: Path | None = None) -> list[dict[str, Any]]:
        channels = self.newapi.list_all_channels(page_size=DEFAULT_CHANNEL_PAGE_SIZE)
        candidates: list[dict[str, Any]] = []
        for channel in channels:
            reasons: list[str] = []
            tag = str(channel.get("tag") or "")
            base_url = str(channel.get("base_url") or "")
            if tag == "puter":
                reasons.append("tag=puter")
            if any(hint in base_url for hint in PUTER_ADAPTER_HINTS):
                reasons.append(f"base_url={base_url}")
            if not reasons:
                continue
            candidates.append(
                {
                    "id": channel["id"],
                    "name": channel.get("name"),
                    "type": channel.get("type"),
                    "base_url": base_url,
                    "models": channel.get("models"),
                    "group": channel.get("group"),
                    "status": channel.get("status"),
                    "matched_reason": ", ".join(sorted(set(reasons))),
                }
            )
        if report_path:
            report_path.write_text(
                json.dumps(candidates, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return candidates

    def delete_dead_channels(
        self,
        *,
        yes: bool,
        expected_count: int = 73,
        allow_count_mismatch: bool = False,
        restart_command: str | None = None,
    ) -> dict[str, Any]:
        self.backup_root.mkdir(parents=True, exist_ok=True)
        report_path = self.backup_root / f"dead-channel-report-{int(time.time())}.json"
        candidates = self.list_dead_channels(report_path=report_path)
        if len(candidates) != expected_count and not allow_count_mismatch:
            raise RuntimeError(
                f"candidate count mismatch: expected {expected_count}, got {len(candidates)}; "
                "rerun with --allow-count-mismatch if this is expected"
            )
        if not yes:
            return {
                "dry_run": True,
                "candidate_count": len(candidates),
                "report_path": str(report_path),
            }

        for candidate in candidates:
            response = self.newapi.delete_channel(int(candidate["id"]))
            if response.status_code == 404:
                continue
            if response.status_code >= 400:
                raise RuntimeError(
                    f"unexpected delete failure for channel {candidate['id']}: "
                    f"HTTP {response.status_code} {response.text[:300]}"
                )
            if not self._channel_absent(int(candidate["id"])):
                raise RuntimeError(f"channel {candidate['id']} still exists after deletion")
            remaining = self.newapi.list_all_channels(page_size=DEFAULT_CHANNEL_PAGE_SIZE)
            ollama_count = sum(1 for channel in remaining if (channel.get("tag") or "") == "ollama")
            if ollama_count != 4:
                raise RuntimeError(f"ollama channel count changed unexpectedly after deleting {candidate['id']}: {ollama_count}")
        if restart_command:
            subprocess.run(restart_command, shell=True, check=True)
        return {
            "dry_run": False,
            "deleted_count": len(candidates),
            "report_path": str(report_path),
        }

    def verify_cleanup(self, *, gateway_master_key: str | None = None) -> dict[str, Any]:
        channels = self.newapi.list_all_channels(page_size=DEFAULT_CHANNEL_PAGE_SIZE)
        puter_count = sum(1 for channel in channels if (channel.get("tag") or "") == "puter")
        ollama_count = sum(1 for channel in channels if (channel.get("tag") or "") == "ollama")

        models_response = self.client.get(f"{self.gateway_url}/v1/models")
        models_response.raise_for_status()
        models_payload = models_response.json()
        model_ids = [item["id"] for item in models_payload.get("data", [])]

        health_response = self.client.get(f"{self.gateway_url}/health")
        health_response.raise_for_status()
        health_payload = health_response.json()

        chat_result: dict[str, Any] | None = None
        if gateway_master_key:
            response = self.client.post(
                f"{self.gateway_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {gateway_master_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-oss:20b",
                    "messages": [{"role": "user", "content": "say ok"}],
                    "max_tokens": 8,
                },
            )
            chat_result = {
                "status_code": response.status_code,
                "body_preview": response.text[:300],
            }

        report = {
            "channel_counts": {
                "total": len(channels),
                "puter": puter_count,
                "ollama": ollama_count,
            },
            "gateway_models": model_ids,
            "health": health_payload,
            "chat_test": chat_result,
        }
        return report

    def _channel_absent(self, channel_id: int) -> bool:
        response = self.newapi.get_channel(channel_id)
        if response.status_code == 404:
            return True
        if response.status_code >= 400:
            return False
        payload = response.json()
        data = payload.get("data")
        return not data or data.get("id") != channel_id


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Back up and clean the pool-gateway/new-api state")
    parser.add_argument("action", choices=("backup", "purge", "delete", "verify"))
    parser.add_argument("--gateway-url", default=os.getenv("GATEWAY_URL", "http://127.0.0.1:8002"))
    parser.add_argument("--newapi-url", default=os.getenv("NEWAPI_URL", "http://127.0.0.1:3000"))
    parser.add_argument("--newapi-session", default=os.getenv("NEWAPI_SESSION"))
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    parser.add_argument("--backup-root", default=str(DEFAULT_BACKUP_ROOT))
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--allow-count-mismatch", action="store_true")
    parser.add_argument("--restart-command")
    parser.add_argument("--gateway-master-key", default=os.getenv("GATEWAY_MASTER_KEY"))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    cleaner = PoolCleaner(
        args.gateway_url,
        args.newapi_url,
        args.newapi_session,
        state_file=Path(args.state_file),
        backup_root=Path(args.backup_root),
    )
    try:
        if args.action == "backup":
            print(cleaner.backup_current_state(), flush=True)
        elif args.action == "purge":
            print(
                json.dumps(
                    cleaner.purge_puter_pool(yes=args.yes, restart_command=args.restart_command),
                    ensure_ascii=False,
                    indent=2,
                ),
                flush=True,
            )
        elif args.action == "delete":
            print(
                json.dumps(
                    cleaner.delete_dead_channels(
                        yes=args.yes,
                        allow_count_mismatch=args.allow_count_mismatch,
                        restart_command=args.restart_command,
                    ),
                    ensure_ascii=False,
                    indent=2,
                ),
                flush=True,
            )
        elif args.action == "verify":
            print(
                json.dumps(
                    cleaner.verify_cleanup(gateway_master_key=args.gateway_master_key),
                    ensure_ascii=False,
                    indent=2,
                ),
                flush=True,
            )
    finally:
        cleaner.close()


if __name__ == "__main__":
    main()
