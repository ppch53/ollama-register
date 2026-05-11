"""
Minimal tempmail backend compatible with cloudflare_temp_email API contract
expected by ollama-register's src/tempmail_client.py.

Endpoints:
  POST /api/new_address           header: x-custom-auth: <ADMIN_KEY>
  GET  /api/mails?limit&offset    header: Authorization: Bearer <jwt>
  GET  /api/mail/<mail_id>        header: Authorization: Bearer <jwt>

SMTP server listens on :25 and accepts mail for any local-part @MAIL_DOMAIN.
Stores raw RFC822 in sqlite. Addresses are randomly generated 12-hex.

Env:
  ADMIN_KEY     shared secret for /api/new_address  (x-custom-auth)
  JWT_SECRET    HS256 secret used to sign per-address tokens
  MAIL_DOMAIN   accepted recipient domain, default mail.ppch.qzz.io
  HTTP_PORT     default 8080
  SMTP_PORT     default 25
  DB_PATH       default /opt/tempmail/mail.db
  ADDRESS_TTL_HOURS  default 24
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sqlite3
import time
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.parser import BytesParser
from email.policy import default as default_policy

import jwt as pyjwt
import uvicorn
from aiosmtpd.controller import Controller
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
JWT_SECRET = os.environ.get("JWT_SECRET", "")
MAIL_DOMAIN = os.environ.get("MAIL_DOMAIN", "mail.ppch.qzz.io").lower()
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
SMTP_PORT = int(os.environ.get("SMTP_PORT", "25"))
DB_PATH = os.environ.get("DB_PATH", "/opt/tempmail/mail.db")
ADDRESS_TTL_HOURS = int(os.environ.get("ADDRESS_TTL_HOURS", "24"))

if not ADMIN_KEY or not JWT_SECRET:
    raise SystemExit("ADMIN_KEY and JWT_SECRET must be set")

_db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS addresses (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              address TEXT UNIQUE NOT NULL,
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mails (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              address_id INTEGER NOT NULL,
              subject TEXT,
              from_addr TEXT,
              raw TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(address_id) REFERENCES addresses(id)
            );
            CREATE INDEX IF NOT EXISTS idx_mails_address_created
              ON mails(address_id, id DESC);
            """
        )


@contextmanager
def locked_db():
    with _db_lock:
        conn = db()
        try:
            yield conn
        finally:
            conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_jwt(address_id: int, address: str) -> str:
    payload = {
        "sub": str(address_id),
        "addr": address,
        "iat": int(time.time()),
        "exp": int(time.time()) + ADDRESS_TTL_HOURS * 3600,
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm="HS256")


def parse_jwt(token: str) -> tuple[int, str]:
    try:
        data = pyjwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except pyjwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"jwt: {exc}") from exc
    return int(data["sub"]), str(data["addr"])


# ─── SMTP receiver ─────────────────────────────────────────────


class Handler:
    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        local_part, _, dom = address.partition("@")
        if dom.lower() != MAIL_DOMAIN:
            return f"550 not relaying to {dom}"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        raw_bytes = envelope.content if isinstance(envelope.content, bytes) else envelope.content.encode("utf-8", errors="replace")
        try:
            msg = BytesParser(policy=default_policy).parsebytes(raw_bytes)
            subject = str(msg.get("Subject", "") or "")
            from_addr = str(msg.get("From", "") or "")
        except Exception:
            subject = ""
            from_addr = envelope.mail_from or ""
        raw_text = raw_bytes.decode("utf-8", errors="replace")
        ts = now_iso()
        accepted = 0
        with locked_db() as c:
            for rcpt in envelope.rcpt_tos:
                addr = rcpt.lower()
                row = c.execute(
                    "SELECT id, expires_at FROM addresses WHERE address = ?",
                    (addr,),
                ).fetchone()
                if not row:
                    continue
                c.execute(
                    "INSERT INTO mails(address_id, subject, from_addr, raw, created_at) VALUES (?,?,?,?,?)",
                    (row["id"], subject, from_addr, raw_text, ts),
                )
                accepted += 1
        if accepted == 0:
            return "550 unknown recipient"
        return "250 Message accepted"


# ─── HTTP API ──────────────────────────────────────────────────

app = FastAPI()


def require_admin(x_custom_auth: str | None = Header(default=None)):
    if x_custom_auth != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="bad admin key")


def require_jwt(authorization: str | None = Header(default=None)) -> tuple[int, str]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer")
    return parse_jwt(authorization[7:])


@app.post("/api/new_address")
def new_address(_: None = Depends(require_admin)):
    local = secrets.token_hex(6)
    addr = f"{local}@{MAIL_DOMAIN}"
    created = now_iso()
    expires = (datetime.now(timezone.utc) + timedelta(hours=ADDRESS_TTL_HOURS)).isoformat(timespec="seconds")
    with locked_db() as c:
        cur = c.execute(
            "INSERT INTO addresses(address, created_at, expires_at) VALUES (?,?,?)",
            (addr, created, expires),
        )
        addr_id = cur.lastrowid
    return {
        "address_id": addr_id,
        "address": addr,
        "jwt": make_jwt(addr_id, addr),
        "created_at": created,
        "expires_at": expires,
    }


@app.get("/api/mails")
def list_mails(limit: int = 20, offset: int = 0, scope: tuple[int, str] = Depends(require_jwt)):
    addr_id, _addr = scope
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    with locked_db() as c:
        rows = c.execute(
            "SELECT id, subject, from_addr, created_at FROM mails WHERE address_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (addr_id, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/mail/{mail_id}")
def get_mail(mail_id: int, scope: tuple[int, str] = Depends(require_jwt)):
    addr_id, _addr = scope
    with locked_db() as c:
        row = c.execute(
            "SELECT id, subject, from_addr, raw, created_at FROM mails WHERE id = ? AND address_id = ?",
            (mail_id, addr_id),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    return dict(row)


@app.get("/api/health")
def health():
    return {"ok": True, "domain": MAIL_DOMAIN}


# ─── Bootstrap ─────────────────────────────────────────────────


def main():
    init_db()
    smtp = Controller(
        Handler(),
        hostname="0.0.0.0",
        port=SMTP_PORT,
        # Decode to str True is broken with binary; we keep bytes.
    )
    smtp.start()
    print(f"[tempmail] smtp listening on :{SMTP_PORT}, http on :{HTTP_PORT}, domain={MAIL_DOMAIN}")
    try:
        uvicorn.run(app, host="0.0.0.0", port=HTTP_PORT, log_level="info")
    finally:
        smtp.stop()


if __name__ == "__main__":
    main()
