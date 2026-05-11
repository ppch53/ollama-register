"""StickyProxyManager — session-stable proxy management.

For providers like Rayobyte that rotate IP per TCP connection, a "session"
means holding a single long-lived connection alive throughout a registration.
True sticky-session providers (IPRoyal, SmartProxy) use username-session-xxx
format — the abstraction supports both.

Phone-triggered IPs are blacklisted for the rest of the day.
"""
from __future__ import annotations

import json
import random
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from src.utils import append_jsonl, load_json, read_jsonl, utcnow_iso


@dataclass(slots=True)
class ProxyProviderConfig:
    host: str
    port: int
    username: str
    password: str
    country: str = "US"
    proxy_type: str = "socks5"  # socks5 | http


@dataclass(slots=True)
class ProxySession:
    session_id: str
    account_id: str
    proxy_url: str
    config: ProxyProviderConfig
    ip_pre: str = ""
    ip_mid: str = ""
    ip_post: str = ""
    drift_detected: bool = False
    _client: Any = field(default=None, repr=False)
    _closed: bool = False


class StickyProxyManager:
    def __init__(
        self,
        config: ProxyProviderConfig,
        state_dir: Path,
        phone_blacklist_path: Path | None = None,
        ip_check_url: str = "https://httpbin.org/ip",
    ) -> None:
        self.config = config
        self.state_dir = state_dir
        self.phone_blacklist_path = phone_blacklist_path or (
            state_dir / "phone_triggered_ips.jsonl"
        )
        self.ip_check_url = ip_check_url
        self._blacklisted_ips: set[str] = set()
        self._active_sessions: dict[str, ProxySession] = {}
        self._load_blacklist()

    def _load_blacklist(self) -> None:
        for record in read_jsonl(self.phone_blacklist_path):
            ip = record.get("ip", "")
            if ip:
                self._blacklisted_ips.add(ip)

    def mark_phone_triggered(self, session: ProxySession) -> None:
        self._blacklisted_ips.add(session.ip_pre)
        append_jsonl(
            self.phone_blacklist_path,
            {
                "ip": session.ip_pre,
                "country": session.config.country,
                "ts": utcnow_iso(),
                "account_id": session.account_id,
            },
        )

    def _build_proxy_url(self, session_suffix: str = "") -> str:
        username = self.config.username
        if session_suffix:
            # IPRoyal/SmartProxy sticky session format:
            # username-session-{suffix}
            username = f"{username}-session-{session_suffix}"
        auth = f"{username}:{self.config.password}"
        return f"{self.config.proxy_type}://{auth}@{self.config.host}:{self.config.port}"

    async def _resolve_ip(self, proxy_url: str) -> str:
        async with httpx.AsyncClient(proxy=proxy_url, timeout=15.0) as client:
            resp = await client.get(self.ip_check_url)
            resp.raise_for_status()
            return resp.json().get("origin", "")

    async def acquire_session(self, account_id: str, duration_minutes: int = 30) -> ProxySession:
        from src.utils import new_uuid

        session_id = new_uuid()
        proxy_url = self._build_proxy_url(session_suffix=session_id)

        # resolve initial IP
        ip_pre = await self._resolve_ip(proxy_url)
        if ip_pre in self._blacklisted_ips:
            raise RuntimeError(
                f"Proxy IP {ip_pre} is blacklisted (phone-triggered). "
                "Try a different session or provider."
            )

        # create a long-lived client for this session
        client = httpx.AsyncClient(proxy=proxy_url, timeout=30.0)

        session = ProxySession(
            session_id=session_id,
            account_id=account_id,
            proxy_url=proxy_url,
            config=self.config,
            ip_pre=ip_pre,
            _client=client,
        )
        self._active_sessions[session_id] = session
        return session

    async def check_mid(self, session: ProxySession) -> str:
        ip = await self._resolve_ip(session.proxy_url)
        session.ip_mid = ip
        if ip != session.ip_pre:
            session.drift_detected = True
        return ip

    async def release_session(self, session: ProxySession) -> None:
        if session._closed:
            return
        session._closed = True

        # record final IP
        try:
            session.ip_post = await self._resolve_ip(session.proxy_url)
            if session.ip_post != session.ip_pre:
                session.drift_detected = True
        except Exception:
            session.ip_post = ""

        # close the HTTP client
        if session._client is not None:
            await session._client.aclose()
            session._client = None

        self._active_sessions.pop(session.session_id, None)

    async def release_all(self) -> None:
        for session in list(self._active_sessions.values()):
            await self.release_session(session)

    def get_session_proof(self, session: ProxySession) -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "ip_pre": session.ip_pre,
            "ip_mid": session.ip_mid,
            "ip_post": session.ip_post,
            "drift_detected": session.drift_detected,
        }
