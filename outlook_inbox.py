"""Outlook (personal @outlook.com) inbox helper.

Given a refresh_token + client_id, exchange for an access_token via
Microsoft Live OAuth and read INBOX messages via IMAP/XOAUTH2.

Pool format (one account per line):
  email----password----refresh_token----client_id
"""

from __future__ import annotations

import imaplib
import re
import time
from dataclasses import dataclass
from email import message_from_bytes
from pathlib import Path
from typing import Iterable

import httpx

OAUTH_URL = "https://login.live.com/oauth20_token.srf"
IMAP_HOST = "imap-mail.outlook.com"
IMAP_PORT = 993
IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"

CODE_PATTERNS = (
    re.compile(r"verification code[^0-9]{0,20}(\d{6})", re.IGNORECASE),
    re.compile(r"\bcode[^0-9]{0,20}(\d{6})\b", re.IGNORECASE),
    re.compile(r"\b(\d{6})\b"),
)


@dataclass
class OutlookAccount:
    email: str
    password: str
    refresh_token: str
    client_id: str


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def parse_pool_line(line: str) -> OutlookAccount | None:
    parts = [p.strip() for p in line.strip().split("----")]
    if len(parts) < 4 or "@" not in parts[0]:
        return None
    email, password = parts[0], parts[1]
    rest = parts[2:]
    # auto-detect which is refresh_token and which is client_id
    refresh_token = ""
    client_id = ""
    for tok in rest:
        if _UUID_RE.match(tok):
            client_id = tok
        elif tok.startswith("M.C") or len(tok) > 100:
            refresh_token = tok
    if not refresh_token or not client_id:
        return None
    return OutlookAccount(
        email=email,
        password=password,
        refresh_token=refresh_token,
        client_id=client_id,
    )


def load_pool(path: Path) -> list[OutlookAccount]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        acc = parse_pool_line(line)
        if acc:
            out.append(acc)
    return out


def acquire_unused(pool_path: Path, used_path: Path) -> OutlookAccount:
    pool = load_pool(pool_path)
    used: set[str] = set()
    if used_path.exists():
        used = {
            ln.strip().split("\t", 1)[0]
            for ln in used_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        }
    for acc in pool:
        if acc.email not in used:
            return acc
    raise RuntimeError(f"no unused outlook accounts in {pool_path}")


def mark_used(used_path: Path, email: str, note: str = "") -> None:
    used_path.parent.mkdir(parents=True, exist_ok=True)
    with used_path.open("a", encoding="utf-8") as f:
        f.write(f"{email}\t{int(time.time())}\t{note}\n")


def get_access_token(account: OutlookAccount, scope: str = IMAP_SCOPE) -> tuple[str, str]:
    """Exchange refresh_token for access_token. Returns (access_token, new_refresh_token)."""
    r = httpx.post(
        OAUTH_URL,
        data={
            "client_id": account.client_id,
            "refresh_token": account.refresh_token,
            "grant_type": "refresh_token",
            "scope": scope,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30.0,
    )
    r.raise_for_status()
    payload = r.json()
    if "access_token" not in payload:
        raise RuntimeError(f"oauth no access_token: {payload}")
    return payload["access_token"], payload.get("refresh_token") or account.refresh_token


def _imap_login(email: str, access_token: str) -> imaplib.IMAP4_SSL:
    auth_string = f"user={email}\x01auth=Bearer {access_token}\x01\x01"
    m = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    m.authenticate("XOAUTH2", lambda _x: auth_string.encode())
    return m


def _msg_text(msg) -> str:
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            payload = part.get_payload(decode=True) or b""
            parts.append(payload.decode(part.get_content_charset() or "utf-8", "ignore"))
    else:
        payload = msg.get_payload(decode=True) or b""
        parts.append(payload.decode(msg.get_content_charset() or "utf-8", "ignore"))
    parts.append(str(msg.get("Subject", "")))
    return "\n".join(parts)


def wait_for_code(
    account: OutlookAccount,
    *,
    sender_hint: str = "",
    subject_hint: str = "",
    timeout: int = 180,
    poll_interval: int = 5,
    folders: Iterable[str] = ("INBOX", "Junk"),
) -> str:
    """Poll IMAP until a verification code email matching hints arrives. Returns the 6-digit code."""
    deadline = time.monotonic() + timeout
    access_token, _new_rt = get_access_token(account)
    seen_uids: set[tuple[str, bytes]] = set()

    while time.monotonic() < deadline:
        try:
            m = _imap_login(account.email, access_token)
        except imaplib.IMAP4.error:
            access_token, _new_rt = get_access_token(account)
            m = _imap_login(account.email, access_token)
        try:
            for folder in folders:
                try:
                    typ, _ = m.select(f'"{folder}"', readonly=False)
                except imaplib.IMAP4.error:
                    continue
                if typ != "OK":
                    continue
                typ, data = m.search(None, "ALL")
                if typ != "OK":
                    continue
                uids = data[0].split()
                # newest first
                for uid in reversed(uids):
                    key = (folder, uid)
                    if key in seen_uids:
                        continue
                    seen_uids.add(key)
                    typ, msg_data = m.fetch(uid, "(BODY.PEEK[])")
                    if typ != "OK" or not msg_data or not msg_data[0]:
                        continue
                    raw_bytes = msg_data[0][1]
                    msg = message_from_bytes(raw_bytes)
                    sender = str(msg.get("From", ""))
                    subject = str(msg.get("Subject", ""))
                    if sender_hint and sender_hint.lower() not in sender.lower() and sender_hint.lower() not in subject.lower():
                        continue
                    if subject_hint and subject_hint.lower() not in subject.lower():
                        continue
                    text = _msg_text(msg)
                    for pat in CODE_PATTERNS:
                        match = pat.search(text)
                        if match:
                            return match.group(1)
        finally:
            try:
                m.logout()
            except Exception:
                pass
        time.sleep(poll_interval)
    raise TimeoutError("outlook verification code not arrived in time")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("pool", type=Path)
    p.add_argument("--list", action="store_true")
    p.add_argument("--peek", action="store_true", help="just print latest 5 subjects per account")
    args = p.parse_args()
    accounts = load_pool(args.pool)
    print(f"loaded {len(accounts)} accounts")
    if args.list:
        for a in accounts:
            print(a.email)
    if args.peek:
        for a in accounts:
            print(f"=== {a.email} ===")
            try:
                tok, _ = get_access_token(a)
                m = _imap_login(a.email, tok)
                m.select("INBOX")
                _, d = m.search(None, "ALL")
                ids = d[0].split()
                for uid in ids[-5:]:
                    _, x = m.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (Subject From Date)])")
                    print(x[0][1].decode("utf-8", "ignore").strip())
                    print("---")
                m.logout()
            except Exception as exc:
                print(f"  ERROR: {exc}")
