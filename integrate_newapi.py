"""Optional new-api integration for the registration projects.

This file intentionally treats new-api as an external gateway target, not as a
core part of the registration flows. The default path is to add one new-api
channel that points at the existing local pool gateway.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_NEWAPI_URL = "http://127.0.0.1:3000"
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8002"
DEFAULT_CHANNEL_NAME = "local-pool-gateway"
DEFAULT_GROUP = "default"
DEFAULT_TAG = "registration-pool"
DEFAULT_TEST_MODEL = "gpt-4o-mini"

POOL_MODELS = [
    "claude-3-5-sonnet-latest",
    "claude-3-7-sonnet",
    "claude-sonnet-4-5",
    "deepseek-chat",
    "deepseek-reasoner",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite",
    "gemini-3.1-pro-preview",
    "gemma3:27b",
    "gemma3:4b",
    "gemma4:31b",
    "glm-4.7",
    "glm-4.7:cloud",
    "glm-5",
    "glm-5.1",
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-5",
    "gpt-oss:120b",
    "gpt-oss:20b",
    "kimi-k2.6",
    "minimax-m2.5",
    "minimax-m2.5:cloud",
    "minimax-m2.7",
    "nemotron-3-super",
    "nemotron-3-super:cloud",
    "qwen3-coder:480b",
    "qwen3-next:80b",
    "qwen3.5",
]


@dataclass(slots=True)
class NewApiAuth:
    cookie: str | None = None
    user_id: str | None = None
    username: str | None = None
    password: str | None = None


class NewApiSession:
    def __init__(self, base_url: str, auth: NewApiAuth) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.client = httpx.Client(timeout=30.0, follow_redirects=True)

    def close(self) -> None:
        self.client.close()

    def ensure_authenticated(self) -> None:
        if self.auth.cookie and self.auth.user_id:
            return
        if not self.auth.username or not self.auth.password:
            raise RuntimeError(
                "new-api credentials are required: set NEWAPI_SESSION or "
                "NEWAPI_USERNAME/NEWAPI_PASSWORD"
            )
        response = self.client.post(
            f"{self.base_url}/api/user/login",
            json={"username": self.auth.username, "password": self.auth.password},
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise RuntimeError(f"new-api login failed: {payload.get('message') or payload}")
        user = payload.get("data") or {}
        self.auth.user_id = str(user.get("id") or "")
        self.auth.cookie = response.headers.get("set-cookie", "").split(";", 1)[0]
        if not self.auth.cookie or not self.auth.user_id:
            raise RuntimeError(
                "new-api login succeeded but did not return a session cookie/user id"
            )

    def headers(self) -> dict[str, str]:
        self.ensure_authenticated()
        return {
            "Cookie": self.auth.cookie or "",
            "New-Api-User": self.auth.user_id or "",
            "Content-Type": "application/json",
        }

    def create_channel(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(
            f"{self.base_url}/api/channel/",
            headers=self.headers(),
            json=payload,
        )
        response.raise_for_status()
        return response.json()


def parse_newapi_session(raw: str | None) -> tuple[str | None, str | None]:
    if not raw:
        return None, None
    raw = raw.strip()
    if raw.startswith("{"):
        payload = json.loads(raw)
        return str(payload.get("cookie") or ""), str(payload.get("user_id") or "")
    if "|" in raw:
        cookie, user_id = raw.split("|", 1)
        return cookie.strip(), user_id.strip()
    raise ValueError("NEWAPI_SESSION must be JSON or '<cookie>|<user_id>'")


def model_list(raw: str | None) -> list[str]:
    if not raw:
        return POOL_MODELS
    return [item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()]


def build_gateway_channel_payload(
    *,
    name: str,
    gateway_key: str,
    gateway_url: str,
    models: list[str],
    group: str,
    tag: str,
    test_model: str,
) -> dict[str, Any]:
    return {
        "mode": "single",
        "channel": {
            "name": name,
            "type": 1,
            "key": gateway_key,
            "base_url": gateway_url.rstrip("/"),
            "models": ",".join(models),
            "group": group,
            "tag": tag,
            "priority": 0,
            "weight": 1,
            "status": 1,
            "test_model": test_model,
            "auto_ban": 1,
        },
    }


def redacted_channel_summary(payload: dict[str, Any]) -> dict[str, Any]:
    channel = dict(payload["channel"])
    channel["key"] = "<redacted>"
    channel["models"] = f"{len(channel.get('models', '').split(','))} models"
    return {"mode": payload["mode"], "channel": channel}


def inject_existing_gateway(args: argparse.Namespace) -> int:
    gateway_key = args.gateway_key or os.getenv("MASTER_KEY")
    models = model_list(args.models)
    payload = build_gateway_channel_payload(
        name=args.channel_name,
        gateway_key=gateway_key or "dry-run-placeholder",
        gateway_url=args.gateway_url,
        models=models,
        group=args.group,
        tag=args.tag,
        test_model=args.test_model,
    )

    print("[plan] add one new-api channel that points to the existing pool gateway")
    print(json.dumps(redacted_channel_summary(payload), indent=2, ensure_ascii=False))

    if not args.yes:
        print("[dry-run] no changes made. Re-run with --yes to write to new-api.")
        return 0
    if not gateway_key:
        raise RuntimeError("--gateway-key or MASTER_KEY is required when --yes is used")

    cookie, user_id = parse_newapi_session(args.newapi_session or os.getenv("NEWAPI_SESSION"))
    session = NewApiSession(
        args.newapi_url,
        NewApiAuth(
            cookie=cookie,
            user_id=user_id,
            username=args.newapi_username or os.getenv("NEWAPI_USERNAME"),
            password=args.newapi_password or os.getenv("NEWAPI_PASSWORD"),
        ),
    )
    try:
        result = session.create_channel(payload)
    finally:
        session.close()

    if not result.get("success"):
        raise RuntimeError(f"new-api channel creation failed: {result.get('message') or result}")
    print("[ok] new-api channel created for the existing pool gateway")
    return 0


def deploy_newapi(args: argparse.Namespace) -> int:
    print("[plan] run an explicit external command to deploy or prepare new-api")
    print(f"[plan] new-api target URL: {args.newapi_url}")
    if args.deploy_command:
        print(f"[plan] deploy command: {args.deploy_command}")
    else:
        print("[plan] no deploy command provided; this mode will only document the choice")

    if not args.yes:
        print("[dry-run] no command executed. Re-run with --yes to run the deploy command.")
        return 0
    if not args.deploy_command:
        raise RuntimeError("--deploy-command is required when --mode deploy-newapi --yes is used")

    completed = subprocess.run(args.deploy_command, shell=True, check=False)
    if completed.returncode != 0:
        return completed.returncode
    if args.post_deploy_inject:
        return inject_existing_gateway(args)
    print("[ok] deploy command completed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Optional new-api integration for Ollama/Puter registration outputs"
    )
    parser.add_argument(
        "--mode",
        choices=("existing-gateway", "deploy-newapi"),
        default="existing-gateway",
        help=(
            "Choose whether to inject an existing gateway pool or run an explicit "
            "new-api deploy command."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually mutate new-api or run commands.",
    )
    parser.add_argument("--newapi-url", default=os.getenv("NEWAPI_URL", DEFAULT_NEWAPI_URL))
    parser.add_argument(
        "--newapi-session",
        help="JSON or '<cookie>|<user_id>'; env: NEWAPI_SESSION",
    )
    parser.add_argument("--newapi-username", help="new-api username; env: NEWAPI_USERNAME")
    parser.add_argument("--newapi-password", help="new-api password; env: NEWAPI_PASSWORD")
    parser.add_argument("--gateway-url", default=os.getenv("GATEWAY_URL", DEFAULT_GATEWAY_URL))
    parser.add_argument("--gateway-key", help="pool gateway bearer key; env fallback: MASTER_KEY")
    parser.add_argument("--channel-name", default=DEFAULT_CHANNEL_NAME)
    parser.add_argument("--group", default=DEFAULT_GROUP)
    parser.add_argument("--tag", default=DEFAULT_TAG)
    parser.add_argument("--test-model", default=DEFAULT_TEST_MODEL)
    parser.add_argument(
        "--models",
        help="Comma/newline separated model allowlist for the new-api channel",
    )
    parser.add_argument(
        "--deploy-command",
        help="Explicit external command that deploys or starts new-api. Never runs without --yes.",
    )
    parser.add_argument(
        "--post-deploy-inject",
        action="store_true",
        help="After a successful deploy command, also add the pool-gateway channel.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.mode == "existing-gateway":
            return inject_existing_gateway(args)
        if args.mode == "deploy-newapi":
            return deploy_newapi(args)
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    parser.error(f"unknown mode: {args.mode}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
