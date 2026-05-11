from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from filelock import FileLock

from src.models import AccountRecord


def _backup_with_timestamp(file_path: Path) -> Path | None:
    if not file_path.exists():
        return None
    timestamp = int(time.time())
    backup_path = file_path.with_name(f"{file_path.name}.{timestamp}.bak")
    shutil.copy2(file_path, backup_path)
    return backup_path


def _atomic_write_text(file_path: Path, content: str) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=file_path.name,
        suffix=".tmp",
        dir=file_path.parent,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if file_path.exists():
            shutil.copystat(file_path, tmp_path)
        os.replace(tmp_path, file_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _read_existing_keys(file_path: Path) -> set[str]:
    if not file_path.exists():
        return set()
    return {
        line.strip()
        for line in file_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def load_accounts(file_path: Path) -> list[AccountRecord]:
    if not file_path.exists():
        return []
    raw_content = file_path.read_text(encoding="utf-8").strip()
    if not raw_content:
        return []
    payload = json.loads(raw_content)
    return [AccountRecord.from_dict(item) for item in payload]


def append_account_record(file_path: Path, record: AccountRecord) -> bool:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(file_path) + ".lock"):
        accounts = load_accounts(file_path)
        if any(item.api_key == record.api_key or item.email == record.email for item in accounts):
            return False
        _backup_with_timestamp(file_path)
        accounts.append(record)
        payload = json.dumps(
            [item.to_dict() for item in accounts],
            ensure_ascii=False,
            indent=2,
        )
        _atomic_write_text(file_path, payload)
        return True


def save_accounts(file_path: Path, accounts: list[AccountRecord]) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(file_path) + ".lock"):
        _backup_with_timestamp(file_path)
        payload = json.dumps(
            [item.to_dict() for item in accounts],
            ensure_ascii=False,
            indent=2,
        )
        _atomic_write_text(file_path, payload)


def append_api_key(file_path: Path, api_key: str) -> bool:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(file_path) + ".lock"):
        existing_keys = _read_existing_keys(file_path)
        if api_key in existing_keys:
            return False
        _backup_with_timestamp(file_path)
        with file_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{api_key}\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True


def persist_account_result(
    accounts_file: Path,
    api_key_file: Path,
    record: AccountRecord,
    *,
    append_production_key: bool,
) -> dict[str, Any]:
    account_added = append_account_record(accounts_file, record)
    api_key_added = False
    if append_production_key:
        api_key_added = append_api_key(api_key_file, record.api_key)
    return {
        "account_added": account_added,
        "api_key_added": api_key_added,
        "status": record.status,
    }
