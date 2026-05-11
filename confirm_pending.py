"""Walk puter_accounts.json, find email_confirmed=False, re-fetch verification
code from the outlook inbox, POST /confirm-email, and update the record."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from outlook_inbox import OutlookAccount, wait_for_code as outlook_wait_for_code
from puter_register import confirm_email


def main() -> None:
    load_dotenv("/opt/ollama-register/.env", override=False)
    proxy = os.environ.get("REGISTER_PROXY", "").strip() or None
    accounts_path = Path("/opt/ollama-register/puter_accounts.json")
    data = json.loads(accounts_path.read_text(encoding="utf-8"))

    pending = [a for a in data if not a.get("email_confirmed")]
    print(f"[confirm] total accounts: {len(data)}, unconfirmed: {len(pending)}")

    fixed = 0
    for i, a in enumerate(pending, 1):
        ol = a.get("outlook")
        if not ol:
            print(f"[{i}/{len(pending)}] {a['email']}: no outlook creds, skip")
            continue
        outlook = OutlookAccount(
            email=a["email"],
            password=ol["password"],
            client_id=ol["client_id"],
            refresh_token=ol["refresh_token"],
        )
        try:
            print(f"[{i}/{len(pending)}] {a['email']}: fetching code...", flush=True)
            code = outlook_wait_for_code(outlook, sender_hint="puter", timeout=120)
            print(f"  code={code} -> POST /confirm-email", flush=True)
            res = confirm_email(a["token"], code, proxy)
            print(f"  status={res['status']} body={res['body'][:200]}", flush=True)
            if res["status"] == 200:
                a["email_confirmed"] = True
                fixed += 1
        except Exception as exc:
            print(f"  FAIL: {exc}", flush=True)

    accounts_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[confirm] fixed {fixed}/{len(pending)} written back")


if __name__ == "__main__":
    main()
