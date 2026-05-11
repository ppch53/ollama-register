from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from pool_cleaner import PoolCleaner, atomic_write_json, channel_to_create_request


def validate_backup(backup_dir: Path) -> dict[str, Any]:
    state_path = backup_dir / "pool_state.json"
    channels_path = backup_dir / "channels_dump.json"
    if not state_path.exists():
        raise FileNotFoundError(f"missing backup state file: {state_path}")
    if not channels_path.exists():
        raise FileNotFoundError(f"missing channels dump: {channels_path}")

    state = json.loads(state_path.read_text(encoding="utf-8"))
    channels = json.loads(channels_path.read_text(encoding="utf-8"))
    if not isinstance(channels, list):
        raise ValueError("channels_dump.json must contain a JSON array")

    payloads = [channel_to_create_request(channel) for channel in channels]
    return {
        "pool_state_keys": {
            "puter": len((state.get("puter") or {}).get("keys") or []),
            "ollama": len((state.get("ollama") or {}).get("keys") or []),
        },
        "channel_count": len(channels),
        "validated_payload_count": len(payloads),
    }


def restore(
    *,
    backup_dir: Path,
    cleaner: PoolCleaner,
    yes: bool,
    restart_command_gateway: str | None,
    restart_command_newapi: str | None,
) -> dict[str, Any]:
    validation = validate_backup(backup_dir)
    if not yes:
        return {"dry_run": True, **validation}

    state = json.loads((backup_dir / "pool_state.json").read_text(encoding="utf-8"))
    atomic_write_json(cleaner.state_file, state)

    channels = json.loads((backup_dir / "channels_dump.json").read_text(encoding="utf-8"))
    for channel in channels:
        payload = channel_to_create_request(channel)
        response = cleaner.newapi.create_channel(payload)
        if response.status_code >= 400:
            raise RuntimeError(
                f"failed to restore channel {channel.get('name')}: "
                f"HTTP {response.status_code} {response.text[:300]}"
            )

    if restart_command_gateway:
        subprocess.run(restart_command_gateway, shell=True, check=True)
    if restart_command_newapi:
        subprocess.run(restart_command_newapi, shell=True, check=True)

    return {"dry_run": False, **validation}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Restore pool cleanup backup data")
    parser.add_argument("--backup-dir", required=True)
    parser.add_argument("--gateway-url", default=os.getenv("GATEWAY_URL", "http://127.0.0.1:8002"))
    parser.add_argument("--newapi-url", default=os.getenv("NEWAPI_URL", "http://127.0.0.1:3000"))
    parser.add_argument("--newapi-session", default=os.getenv("NEWAPI_SESSION"))
    parser.add_argument("--state-file", default="/opt/ollama-register/pool_state.json")
    parser.add_argument("--backup-root", default="/opt/backups")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--restart-command-gateway")
    parser.add_argument("--restart-command-newapi")
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
        result = restore(
            backup_dir=Path(args.backup_dir),
            cleaner=cleaner,
            yes=args.yes,
            restart_command_gateway=args.restart_command_gateway,
            restart_command_newapi=args.restart_command_newapi,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    finally:
        cleaner.close()


if __name__ == "__main__":
    main()
