"""Bulk-import 73 puter + 4 ollama channels into new-api via REST API.

Run on the VPS (or anywhere with cookies + reachability to :3000).

Reads tokens from:
  /opt/ollama-register/puter_accounts.json   - 73 puter tokens
  /opt/ollama-register/apikey.txt            - 4 ollama keys

Posts to: http://127.0.0.1:3000/api/channel/  (single-channel add, mode=single)

Channel schema:
  - puter: type=1 (OpenAI-compatible), base_url=http://127.0.0.1:8001,
           tag="puter", group="puter", models=<puter model list>
  - ollama: type=1, base_url=https://ollama.com, tag="ollama", group="ollama",
            models=<ollama models that free tier supports>
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

NEW_API = "http://127.0.0.1:3000"
PUTER_ACCOUNTS = Path("/opt/ollama-register/puter_accounts.json")
OLLAMA_KEYS = Path("/opt/ollama-register/apikey.txt")
ROOT_USER = "root"
ROOT_PASSWORD = "AdminPass2026!"

# Models we expose. Listed here for the "models" field of each channel
# (same comma list across all channels of same kind for now).
PUTER_MODELS = ",".join(
    [
        "gemini-3.1-pro-preview",
        "gemini-3.1-flash-lite",
        "gemini-3-flash-preview",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "claude-sonnet-4-5",
        "claude-3-7-sonnet",
        "claude-3-5-sonnet-latest",
        "gpt-5",
        "gpt-4o",
        "gpt-4o-mini",
        "deepseek-chat",
        "deepseek-reasoner",
        "kimi-k2.6",
        "minimax-m2.5",
        "minimax-m2.7",
        "glm-5",
        "glm-5.1",
        "glm-4.7",
        "qwen3-coder:480b",
        "qwen3-next:80b",
        "qwen3.5",
        "nemotron-3-super",
        "gemma4:31b",
        "gemma3:27b",
    ]
)

# Free-tier accessible ollama cloud models (verified earlier this session).
OLLAMA_MODELS = ",".join(
    [
        "gpt-oss:120b",
        "gpt-oss:20b",
        "qwen3-coder:480b",
        "qwen3-next:80b",
        "gemma3:27b",
        "gemma3:4b",
        "glm-4.7:cloud",
        "minimax-m2.5:cloud",
        "nemotron-3-super:cloud",
    ]
)


def login() -> tuple[httpx.Client, dict]:
    client = httpx.Client(base_url=NEW_API, timeout=30.0)
    r = client.post(
        "/api/user/login",
        json={"username": ROOT_USER, "password": ROOT_PASSWORD},
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"login failed: {data}")
    user_id = str(data["data"]["id"])
    headers = {"New-Api-User": user_id, "Content-Type": "application/json"}
    return client, headers


def add_channel(
    client: httpx.Client,
    headers: dict,
    *,
    name: str,
    key: str,
    base_url: str,
    models: str,
    tag: str,
    group: str,
    test_model: str,
) -> dict:
    body = {
        "mode": "single",
        "multi_key_mode": "",
        "batch_add_set_key_prefix_2_name": False,
        "channel": {
            "name": name,
            "type": 1,
            "key": key,
            "base_url": base_url,
            "models": models,
            "group": group,
            "tag": tag,
            "priority": 0,
            "weight": 1,
            "status": 1,
            "test_model": test_model,
            "auto_ban": 1,
        },
    }
    r = client.post("/api/channel/", json=body, headers=headers)
    r.raise_for_status()
    return r.json()


def ensure_group(client: httpx.Client, headers: dict, name: str) -> None:
    """Create a user group named `name` if it doesn't exist."""
    r = client.get("/api/user_group/", headers=headers)
    if r.status_code == 200:
        existing = {g.get("name") for g in r.json().get("data", []) or []}
        if name in existing:
            return
    body = {
        "symbol": name,
        "name": name,
        "ratio": 1.0,
        "promotion": 0,
        "min_topup": 0,
        "auto_ban_status": 1,
        "max_quota_count": 0,
        "delete_status": 0,
    }
    client.post("/api/user_group/", json=body, headers=headers)


def main() -> None:
    print(f"[bulk] login as {ROOT_USER}...", flush=True)
    client, headers = login()

    # 1. PUTER channels
    accounts = json.loads(PUTER_ACCOUNTS.read_text(encoding="utf-8"))
    puter_added = 0
    for i, acc in enumerate(accounts, 1):
        name = f"puter-{i:03d}-{acc['username']}"
        try:
            res = add_channel(
                client,
                headers,
                name=name,
                key=acc["token"],
                base_url="http://127.0.0.1:8001",
                models=PUTER_MODELS,
                tag="puter",
                group="puter",
                test_model="gpt-4o-mini",
            )
            if res.get("success"):
                puter_added += 1
            else:
                print(f"  [{i}] {name} fail: {res.get('message')}", flush=True)
        except Exception as exc:
            print(f"  [{i}] {name} exc: {exc}", flush=True)
        time.sleep(0.05)
    print(f"[bulk] puter: {puter_added}/{len(accounts)} added", flush=True)

    # 2. OLLAMA channels
    if OLLAMA_KEYS.exists():
        ollama_keys = [
            ln.strip() for ln in OLLAMA_KEYS.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]
    else:
        ollama_keys = []
    ollama_added = 0
    for i, key in enumerate(ollama_keys, 1):
        name = f"ollama-{i:02d}"
        try:
            res = add_channel(
                client,
                headers,
                name=name,
                key=key,
                base_url="https://ollama.com",
                models=OLLAMA_MODELS,
                tag="ollama",
                group="ollama",
                test_model="gpt-oss:20b",
            )
            if res.get("success"):
                ollama_added += 1
            else:
                print(f"  [{i}] {name} fail: {res.get('message')}", flush=True)
        except Exception as exc:
            print(f"  [{i}] {name} exc: {exc}", flush=True)
        time.sleep(0.05)
    print(f"[bulk] ollama: {ollama_added}/{len(ollama_keys)} added", flush=True)

    # summary
    r = client.get("/api/channel/?p=0&page_size=200", headers=headers)
    if r.status_code == 200:
        d = r.json().get("data", {})
        print(f"[bulk] total channels in new-api: {d.get('total')}", flush=True)


if __name__ == "__main__":
    main()
