"""Restore script — rebuild derived state files from append-only JSONL logs.

Reads:
  - puter_states.jsonl (source of truth for state transitions)
  - puter_audit.jsonl (source of truth for attempt metadata)

Rebuilds:
  - puter_accounts_v2.json (only exportable accounts)
  - puter_quarantine.json (accounts currently in quarantine)
  - puter_failures.jsonl (all terminal-failed attempts)
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.utils import (
    atomic_write_json,
    ensure_dir,
    read_jsonl,
    set_file_permissions,
    utcnow_iso,
)

DEFAULT_V2_ROOT = Path("/opt/ollama-register/v2")
DEFAULT_STATE_DIR = DEFAULT_V2_ROOT / "state"
DEFAULT_AUDIT_DIR = DEFAULT_V2_ROOT / "audit"

QUARANTINE_HOURS = 24
OPTIONAL_RECHECK_HOURS = 72


def _get_latest_state(transitions: list[dict[str, Any]], account_id: str) -> str | None:
    """Get the latest state for an account from state transitions."""
    account_transitions = [
        t for t in transitions if t.get("account_id") == account_id
    ]
    if not account_transitions:
        return None
    # sort by timestamp
    account_transitions.sort(key=lambda t: t.get("ts", ""))
    return account_transitions[-1].get("to")


def _get_audit_record(audit_records: list[dict[str, Any]], account_id: str) -> dict[str, Any] | None:
    """Find the audit record for an account."""
    for record in audit_records:
        if record.get("account_id") == account_id:
            return record
    return None


def rebuild(
    state_dir: Path,
    audit_dir: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    states_path = state_dir / "puter_states.jsonl"
    audit_path = audit_dir / "puter_audit.jsonl"
    accounts_path = state_dir / "puter_accounts_v2.json"
    quarantine_path = state_dir / "puter_quarantine.json"
    failures_path = audit_dir / "puter_failures.jsonl"

    # read source files
    transitions = read_jsonl(states_path)
    audit_records = read_jsonl(audit_path)

    if not transitions:
        return {"error": "No state transitions found in puter_states.jsonl"}

    # collect all account IDs
    account_ids: set[str] = set()
    for t in transitions:
        aid = t.get("account_id")
        if aid:
            account_ids.add(aid)

    # classify accounts by their latest state
    accounts_by_state: dict[str, list[str]] = defaultdict(list)
    for aid in account_ids:
        latest = _get_latest_state(transitions, aid)
        if latest:
            accounts_by_state[latest].append(aid)

    # rebuild exportable accounts
    exportable: list[dict[str, Any]] = []
    for aid in accounts_by_state.get("exportable", []):
        audit = _get_audit_record(audit_records, aid)
        if audit:
            exportable.append({
                "account_id": aid,
                "email": audit.get("email", ""),
                "username": audit.get("username", ""),
                "password": "",  # not stored in audit
                "status": "exportable",
                "registered_at": audit.get("registration_time_utc", audit.get("timestamp", "")),
                "exportable_at": audit.get("timestamp", ""),
            })
        else:
            exportable.append({
                "account_id": aid,
                "email": "unknown",
                "username": "unknown",
                "password": "",
                "status": "exportable",
            })

    # rebuild quarantine (accounts in quarantined/audited state)
    quarantine: dict[str, Any] = {}
    now = datetime.now(timezone.utc)
    for state in ("quarantined", "audited"):
        for aid in accounts_by_state.get(state, []):
            # find when quarantine started
            q_entry_time = None
            for t in transitions:
                if t.get("account_id") == aid and t.get("to") in ("quarantined", "session_established"):
                    q_entry_time = t.get("ts", "")
            audit = _get_audit_record(audit_records, aid)
            quarantine[aid] = {
                "email": audit.get("email", "") if audit else "",
                "username": audit.get("username", "") if audit else "",
                "password": "",
                "entered_at": q_entry_time or utcnow_iso(),
                "recheck_24h_done": state == "audited",
                "recheck_72h_done": False,
            }

    # rebuild failures
    failures: list[dict[str, Any]] = []
    for aid in accounts_by_state.get("failed", []):
        last_transition = None
        for t in transitions:
            if t.get("account_id") == aid and t.get("to") == "failed":
                last_transition = t
        if last_transition:
            failures.append({
                "account_id": aid,
                "attempt_id": last_transition.get("attempt_id", ""),
                "state_at_failure": last_transition.get("from", ""),
                "error_category": last_transition.get("reason", ""),
                "ts": last_transition.get("ts", ""),
            })

    # summary
    summary = {
        "total_accounts": len(account_ids),
        "exportable": len(exportable),
        "quarantined": len(quarantine),
        "failed": len(failures),
        "skipped_phone": len(accounts_by_state.get("skipped_phone_verification", [])),
        "other_states": {
            state: len(aids)
            for state, aids in accounts_by_state.items()
            if state not in ("exportable", "quarantined", "audited", "failed", "skipped_phone_verification")
        },
    }

    if dry_run:
        return {"dry_run": True, "summary": summary}

    # write derived files
    atomic_write_json(accounts_path, exportable)
    set_file_permissions(accounts_path)

    atomic_write_json(quarantine_path, quarantine)
    set_file_permissions(quarantine_path)

    # write failures (append-only, but rebuild means full rewrite)
    failures_path.parent.mkdir(parents=True, exist_ok=True)
    with open(failures_path, "w", encoding="utf-8") as f:
        for record in failures:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    set_file_permissions(failures_path)

    return {"rebuilt": True, "summary": summary}


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore Puter v2 state from JSONL logs")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--audit-dir", type=Path, default=DEFAULT_AUDIT_DIR)
    parser.add_argument("--dry-run", action="store_true", help="Show what would be rebuilt without writing")
    args = parser.parse_args()

    result = rebuild(args.state_dir, args.audit_dir, dry_run=args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
