"""Puter.com auto-registration with our existing CapSolver + outlook stack.

Re-uses (no extra services):
  - CapSolver shim @ http://127.0.0.1:5072  (anti-captcha API for Turnstile)
  - TempMail        @ http://127.0.0.1:8080 (cloudflare_temp_email-compatible)
  - Local proxy     @ http://127.0.0.1:1081 (gost -> rayobyte residential)

Design:
  - Categorise every failure into {EMAIL_BLACKLIST, EMAIL_DUPLICATE,
    CAPTCHA_FAILED, NETWORK, OUTLOOK_UNREACHABLE, EMAIL_DELIVERY, OTHER}.
  - Unrecoverable kinds (EMAIL_BLACKLIST, EMAIL_DUPLICATE) -> mark outlook used,
    never retried.
  - Retryable kinds -> outlook NOT marked used; the runner does up to 3 passes
    of the whole pool. Each retry on the same outlook gets a fresh capsolver
    token, fresh proxy session.
  - Per-account 'pending' record persisted before signup so a crash never loses
    the username (recoverable later via outlook IMAP password reset).
  - confirm-email goes via plain httpx so the curl_cffi connection drop bug
    doesn't lose us tokens that were already issued.
  - Final pass writes report.json with the full breakdown.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import secrets
import string
import time
from dataclasses import dataclass, field, asdict
from email import message_from_string
from enum import Enum
from pathlib import Path

import httpx
from curl_cffi import requests as curl_requests
from dotenv import load_dotenv

from outlook_inbox import (
    OutlookAccount,
    load_pool,
    wait_for_code as outlook_wait_for_code,
)

PUTER_GUI = "https://puter.com"
PUTER_API = "https://api.puter.com"
PUTER_SITEKEY = "0x4AAAAAABvMyOLo9EwjFVzC"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

CODE_PATTERNS = (
    re.compile(r"verification code[^0-9]{0,20}(\d{6})", re.IGNORECASE),
    re.compile(r"\bcode[^0-9]{0,20}(\d{6})\b", re.IGNORECASE),
    re.compile(r"\b(\d{6})\b"),
)


# ─── classification ────────────────────────────────────────────


class FailureKind(str, Enum):
    EMAIL_BLACKLIST = "email_blacklist"
    EMAIL_DUPLICATE = "email_duplicate"
    CAPTCHA_FAILED = "captcha_failed"
    NETWORK = "network"
    OUTLOOK_UNREACHABLE = "outlook_unreachable"
    EMAIL_DELIVERY = "email_delivery_timeout"
    SERVER_ERROR = "server_error"
    OTHER = "other"


UNRECOVERABLE = {FailureKind.EMAIL_BLACKLIST, FailureKind.EMAIL_DUPLICATE}


class Skip(Exception):
    """Raised when this outlook should be permanently skipped."""

    def __init__(self, kind: FailureKind, msg: str):
        self.kind = kind
        super().__init__(msg)


class Retry(Exception):
    """Raised when this outlook should be retried in the next pass."""

    def __init__(self, kind: FailureKind, msg: str):
        self.kind = kind
        super().__init__(msg)


def classify_signup_error(status: int, body: str) -> Skip | Retry:
    b = body.lower()
    if "email cannot be used" in b or "this email is not allowed" in b:
        return Skip(FailureKind.EMAIL_BLACKLIST, body[:200])
    if "already exists" in b:
        return Skip(FailureKind.EMAIL_DUPLICATE, body[:200])
    if "captcha" in b:
        return Retry(FailureKind.CAPTCHA_FAILED, body[:200])
    if 500 <= status < 600:
        return Retry(FailureKind.SERVER_ERROR, body[:200])
    return Retry(FailureKind.OTHER, body[:200])


# ─── helpers ───────────────────────────────────────────────────


def log(prefix: str, msg: str) -> None:
    print(f"{prefix} {msg}", flush=True)


def gen_username() -> str:
    return "u" + secrets.token_hex(5)


def gen_password() -> str:
    body = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(14))
    return f"Aa!{body}"


def solve_turnstile(
    solver_url: str,
    page_url: str,
    sitekey: str,
    *,
    action: str = "signup",
    timeout: int = 120,
    attempts: int = 3,
) -> str:
    last_error: str = ""
    for attempt in range(1, attempts + 1):
        try:
            with httpx.Client(timeout=30.0) as c:
                r = c.get(
                    f"{solver_url}/turnstile",
                    params={"url": page_url, "sitekey": sitekey, "action": action},
                )
                r.raise_for_status()
                task_id = r.json().get("taskId")
                if not task_id:
                    last_error = f"no taskId: {r.text}"
                    continue
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    rr = c.get(f"{solver_url}/result", params={"id": task_id})
                    data = rr.json()
                    if data.get("status") == "ready":
                        return data["solution"]["token"]
                    if data.get("errorId"):
                        last_error = (
                            f"{data.get('errorCode')}:{data.get('errorDescription')}"
                        )
                        break
                    time.sleep(2)
                else:
                    last_error = "result timeout"
        except httpx.HTTPError as exc:
            last_error = f"shim http err: {exc}"
        if attempt < attempts:
            time.sleep(2 + attempt * 2)
    raise Retry(FailureKind.CAPTCHA_FAILED, last_error)


def signup(
    session: curl_requests.Session,
    *,
    username: str,
    email: str,
    password: str,
    cf_token: str,
) -> dict:
    body = {
        "username": username,
        "email": email,
        "password": password,
        "referrer": "",
        "send_confirmation_code": True,
        "p102xyzname": "",
        "cf-turnstile-response": cf_token,
    }
    try:
        r = session.post(
            f"{PUTER_GUI}/signup",
            json=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Origin": PUTER_GUI,
                "Referer": f"{PUTER_GUI}/",
            },
            timeout=30,
        )
    except Exception as exc:
        raise Retry(FailureKind.NETWORK, f"signup network: {exc}")
    if r.status_code >= 400:
        err = classify_signup_error(r.status_code, r.text)
        raise err
    try:
        data = r.json()
    except ValueError:
        raise Retry(FailureKind.OTHER, f"signup non-json: {r.text[:200]}")
    if not data.get("token"):
        raise Retry(FailureKind.OTHER, f"signup no token: {data}")
    return data


def confirm_email(token: str, code: str, proxy: str | None) -> dict:
    """Use plain httpx (not curl_cffi). curl_cffi has a TLS keep-alive
    quirk through the rayobyte gost chain that randomly drops on the second
    POST; httpx avoids it."""
    try:
        # httpx >=0.28 uses `proxy=` (singular) instead of `proxies=`
        client_kwargs: dict = {"timeout": 30.0, "http2": False}
        if proxy:
            client_kwargs["proxy"] = proxy
        with httpx.Client(**client_kwargs) as c:
            r = c.post(
                f"{PUTER_API}/confirm-email",
                json={"code": code},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Origin": PUTER_GUI,
                    "Referer": f"{PUTER_GUI}/",
                    "User-Agent": USER_AGENT,
                },
            )
            return {"status": r.status_code, "body": r.text[:500]}
    except Exception as exc:
        return {"status": -1, "body": f"net err: {exc}"}


def whoami(session: curl_requests.Session, token: str) -> dict:
    out: dict[str, dict] = {}
    for path in ("/whoami", "/version"):
        try:
            r = session.get(
                f"{PUTER_API}{path}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            out[path] = {"code": r.status_code, "body": r.text[:300]}
        except Exception as exc:
            out[path] = {"code": -1, "body": str(exc)}
    return out


def append_account(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8") or "[]")
    else:
        existing = []
    existing.append(record)
    path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")


def append_token(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(token + "\n")


def append_pending(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def mark_used(path: Path, email: str, status: str, note: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{email}\t{int(time.time())}\t{status}\t{note}\n")


def load_used(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            out[parts[0].strip()] = parts[2].strip()
    return out


def load_already_registered(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return {a["email"] for a in d}
    except Exception:
        return set()


# ─── single-account flow ───────────────────────────────────────


@dataclass
class Result:
    email: str
    success: bool
    kind: str | None = None
    detail: str = ""
    username: str | None = None
    user_uuid: str | None = None
    email_confirmed: bool = False


def run_single(
    *,
    prefix: str,
    outlook: OutlookAccount,
    cfg: dict,
) -> Result:
    """Attempt a single registration. May raise Skip or Retry."""
    username = gen_username()
    password = gen_password()
    email = outlook.email

    # Persist pending record BEFORE signup so a crash leaves a trail.
    pending_record = {
        "ts": int(time.time()),
        "username": username,
        "password": password,
        "email": email,
        "outlook": {
            "password": outlook.password,
            "client_id": outlook.client_id,
            "refresh_token": outlook.refresh_token,
        },
    }
    append_pending(cfg["pending_file"], pending_record)
    log(prefix, f"username={username} email={email}")

    # 1. solve turnstile (with retries)
    log(prefix, "solving turnstile...")
    cf_token = solve_turnstile(cfg["solver_url"], f"{PUTER_GUI}/", PUTER_SITEKEY)
    log(prefix, f"cf_token len={len(cf_token)}")

    # 2. signup via curl_cffi (TLS impersonate)
    session = curl_requests.Session(impersonate=cfg["impersonate"])
    if cfg["proxy"]:
        session.proxies = {"http": cfg["proxy"], "https": cfg["proxy"]}
    try:
        # warm up: GET / to set cookies
        try:
            session.get(PUTER_GUI + "/", headers={"User-Agent": USER_AGENT}, timeout=30)
        except Exception:
            pass

        log(prefix, "POST /signup ...")
        data = signup(
            session,
            username=username,
            email=email,
            password=password,
            cf_token=cf_token,
        )
        token = data["token"]
        user_obj = data.get("user") or {}
        user_uuid = user_obj.get("uuid")
        log(prefix, f"token len={len(token)} uuid={user_uuid}")

        # 3. wait for verification email via outlook IMAP
        confirmed = False
        try:
            log(prefix, "waiting for verification code via outlook IMAP...")
            code = outlook_wait_for_code(outlook, sender_hint="puter", timeout=180)
            log(prefix, f"got code={code}")
            res = confirm_email(token, code, cfg["proxy"])
            log(prefix, f"confirm status={res['status']} body={res['body'][:120]}")
            confirmed = res["status"] == 200
        except TimeoutError as exc:
            log(prefix, f"email did not arrive in time: {exc}")
            # token already issued, save anyway; mark unconfirmed
        except Exception as exc:
            log(prefix, f"outlook/confirm exception: {exc}")

        # 4. save permanent record  (always, even if confirm failed)
        record = {
            "username": username,
            "password": password,
            "email": email,
            "token": token,
            "user": user_obj,
            "created_at": time.time(),
            "email_confirmed": confirmed,
            "outlook": {
                "password": outlook.password,
                "client_id": outlook.client_id,
                "refresh_token": outlook.refresh_token,
            },
        }
        append_account(cfg["accounts_file"], record)
        append_token(cfg["tokens_file"], token)
        log(prefix, "saved")

        return Result(
            email=email,
            success=True,
            username=username,
            user_uuid=user_uuid,
            email_confirmed=confirmed,
        )
    finally:
        try:
            session.close()
        except Exception:
            pass


# ─── batch runner ──────────────────────────────────────────────


def main() -> None:
    load_dotenv("/opt/ollama-register/.env", override=False)

    p = argparse.ArgumentParser()
    p.add_argument("--pool", required=True, type=Path)
    p.add_argument("-n", "--count", type=int, default=0, help="0 = entire untried pool")
    p.add_argument("--passes", type=int, default=3)
    p.add_argument("--accounts", type=Path, default=Path("/opt/ollama-register/puter_accounts.json"))
    p.add_argument("--tokens", type=Path, default=Path("/opt/ollama-register/puter_tokens.txt"))
    p.add_argument("--used", type=Path, default=Path("/opt/ollama-register/outlook_used.txt"))
    p.add_argument("--pending", type=Path, default=Path("/opt/ollama-register/pending.jsonl"))
    p.add_argument("--report", type=Path, default=Path("/opt/ollama-register/report.json"))
    args = p.parse_args()

    cfg = {
        "solver_url": os.environ.get("TURNSTILE_SOLVER_URL", "http://127.0.0.1:5072"),
        "proxy": os.environ.get("REGISTER_PROXY", "").strip() or None,
        "impersonate": os.environ.get("CURL_IMPERSONATE", "chrome136"),
        "accounts_file": args.accounts,
        "tokens_file": args.tokens,
        "pending_file": args.pending,
    }

    pool: list[OutlookAccount] = load_pool(args.pool)
    if not pool:
        print(f"empty pool: {args.pool}")
        return

    used_status = load_used(args.used)
    already_done = load_already_registered(args.accounts)
    # any outlook with status that is "success" or in unrecoverable kinds is excluded
    unrecoverable_status = {k.value for k in UNRECOVERABLE} | {"success"}
    # everything else (incl. status starting with "retry_") is fair game

    fresh: list[OutlookAccount] = []
    for acc in pool:
        if acc.email in already_done:
            continue
        st = used_status.get(acc.email, "")
        if st in unrecoverable_status:
            continue
        fresh.append(acc)

    if args.count > 0:
        fresh = fresh[: args.count]

    print(
        f"[batch] pool={len(pool)} fresh={len(fresh)} (already_done={len(already_done)},"
        f" used={len(used_status)})",
        flush=True,
    )

    pending_queue: list[OutlookAccount] = list(fresh)
    successes: list[Result] = []
    last_failures: dict[str, tuple[FailureKind, str]] = {}

    for pass_idx in range(1, args.passes + 1):
        if not pending_queue:
            break
        print(f"\n[pass {pass_idx}/{args.passes}] {len(pending_queue)} accounts to try", flush=True)
        next_queue: list[OutlookAccount] = []
        for i, acc in enumerate(pending_queue, 1):
            prefix = f"[p{pass_idx} {i}/{len(pending_queue)}]"
            try:
                res = run_single(prefix=prefix, outlook=acc, cfg=cfg)
                mark_used(args.used, acc.email, "success", res.username or "")
                successes.append(res)
            except Skip as exc:
                mark_used(args.used, acc.email, exc.kind.value, str(exc)[:120])
                last_failures[acc.email] = (exc.kind, str(exc)[:200])
                log(prefix, f"SKIP({exc.kind.value}): {exc}")
            except Retry as exc:
                last_failures[acc.email] = (exc.kind, str(exc)[:200])
                log(prefix, f"RETRY({exc.kind.value}): {exc}")
                next_queue.append(acc)
            except Exception as exc:
                last_failures[acc.email] = (FailureKind.OTHER, str(exc)[:200])
                log(prefix, f"UNEXPECTED: {type(exc).__name__}: {exc}")
                next_queue.append(acc)
        pending_queue = next_queue
        # cool-down between passes so capsolver/proxy get a breather
        if pending_queue:
            print(f"[pass {pass_idx}] cooling down 30s before next pass", flush=True)
            time.sleep(30)

    # any still-pending after last pass: mark used with "exhausted_<kind>"
    for acc in pending_queue:
        kind, detail = last_failures.get(acc.email, (FailureKind.OTHER, ""))
        mark_used(args.used, acc.email, f"retry_exhausted_{kind.value}", detail[:120])

    # report
    by_kind: dict[str, int] = {}
    for _email, (kind, _) in last_failures.items():
        by_kind[kind.value] = by_kind.get(kind.value, 0) + 1
    report = {
        "timestamp": int(time.time()),
        "pool_path": str(args.pool),
        "pool_size": len(pool),
        "fresh_attempted": len(fresh),
        "passes": args.passes,
        "success_count": len(successes),
        "still_failing_count": len(pending_queue),
        "failure_kinds": by_kind,
        "successes": [
            {
                "email": r.email,
                "username": r.username,
                "uuid": r.user_uuid,
                "email_confirmed": r.email_confirmed,
            }
            for r in successes
        ],
    }
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[report] success={len(successes)}/{len(fresh)} written to {args.report}", flush=True)
    print(f"[report] failure kinds: {by_kind}", flush=True)


if __name__ == "__main__":
    main()
