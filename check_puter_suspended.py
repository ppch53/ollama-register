"""Confirm puter.com side: check whoami for first 5 puter accounts."""
import json, subprocess

with open("/opt/ollama-register/puter_accounts.json") as f:
    accounts = json.load(f)

for i, acc in enumerate(accounts[:5]):
    token = acc["token"]
    cmd = [
        "curl", "-s", "-w", "\\n%{http_code}\\n",
        "--proxy", "http://127.0.0.1:1081",
        "-H", f"Authorization: Bearer {token}",
        "https://api.puter.com/whoami",
    ]
    out = subprocess.check_output(cmd, timeout=30).decode()
    print(f"[{i+1}] {acc['email']}")
    print(f"    {out.strip()[:200]}")
    print()
