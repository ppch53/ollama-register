"""StickyProxyManager — session-stable proxy management.

For providers like Rayobyte that rotate IP per TCP connection, a "session"
means holding a single long-lived connection alive throughout a registration.
True sticky-session providers (IPRoyal, SmartProxy) use username-session-xxx
format — the abstraction supports both.

Phone-triggered IPs are blacklisted for the rest of the day.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from src.utils import append_jsonl, read_jsonl, utcnow_iso

_API_EXTRACTION_PROVIDERS = {"b2proxy", "bestgo"}
_PROVIDER_ALIASES = {
    "b2": "b2proxy",
    "b2proxyresidential": "b2proxy",
    "bestgo": "b2proxy",
    "bestgorrp": "b2proxy",
}


@dataclass(slots=True)
class ProxyProviderConfig:
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""
    country: str = "US"
    proxy_type: str = "socks5"  # socks5 | http
    provider: str = "generic"
    api_url: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ProxyProviderConfig":
        source = env or os.environ
        provider = _normalise_provider(source.get("PROXY_PROVIDER"))
        proxy_type = (source.get("PROXY_SCHEME") or source.get("PROXY_TYPE") or "socks5").strip()
        port_raw = (source.get("PROXY_PORT") or "8000").strip()
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise ValueError(f"Invalid PROXY_PORT: {port_raw}") from exc
        return cls(
            host=(source.get("PROXY_HOST") or "la.residential.rayobyte.com").strip(),
            port=port,
            username=(source.get("PROXY_USERNAME") or "").strip(),
            password=(source.get("PROXY_PASSWORD") or "").strip(),
            country=(source.get("PROXY_COUNTRY") or "US").strip().upper(),
            proxy_type=proxy_type.lower(),
            provider=provider,
            api_url=_clean(source.get("PROXY_API_URL")) or _clean(source.get("REGISTER_PROXY_API")),
        )

    @property
    def uses_api_extraction(self) -> bool:
        return self.provider in _API_EXTRACTION_PROVIDERS


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

    async def _extract_api_proxy_url(self) -> str:
        if not self.config.api_url:
            raise ValueError("Provider b2proxy requires PROXY_API_URL or REGISTER_PROXY_API")
        return await asyncio.to_thread(_extract_api_proxy_url_sync, self.config)

    async def _resolve_ip(self, proxy_url: str) -> str:
        return await asyncio.to_thread(_resolve_ip_sync, proxy_url, self.ip_check_url)

    async def acquire_session(self, account_id: str, duration_minutes: int = 30) -> ProxySession:
        from src.utils import new_uuid

        session_id = new_uuid()
        if self.config.uses_api_extraction:
            proxy_url = await self._extract_api_proxy_url()
        else:
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


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().strip('"').strip("'")
    return value or None


def _normalise_provider(provider: str | None) -> str:
    value = (provider or "generic").strip().lower().replace(" ", "")
    return _PROVIDER_ALIASES.get(value, value)


def _proxy_url_from_api_response(payload: str, proxy_type: str) -> str:
    first_line = payload.strip().splitlines()[0].strip()
    if not first_line:
        raise ValueError("Proxy API returned an empty response")
    parsed = urlparse(first_line)
    if parsed.scheme and parsed.hostname and parsed.port:
        return first_line
    if ":" not in first_line:
        raise ValueError(f"Unexpected proxy API response: {first_line}")
    return f"{proxy_type}://{first_line}"


def _extract_api_proxy_url_sync(config: ProxyProviderConfig) -> str:
    if not config.api_url:
        raise ValueError("Provider b2proxy requires PROXY_API_URL or REGISTER_PROXY_API")
    with httpx.Client(timeout=20.0) as client:
        response = client.get(config.api_url)
        response.raise_for_status()
    return _proxy_url_from_api_response(response.text, config.proxy_type)


def _resolve_ip_sync(proxy_url: str, ip_check_url: str) -> str:
    with httpx.Client(proxy=proxy_url, timeout=15.0) as client:
        response = client.get(ip_check_url)
        response.raise_for_status()
        return _extract_ip(response)


def _extract_ip(response: httpx.Response) -> str:
    text = response.text.strip()
    content_type = response.headers.get("content-type", "")
    if "json" in content_type or text.startswith("{"):
        payload = response.json()
        for key in ("origin", "ip", "query"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value.split(",", 1)[0].strip()
    return text.splitlines()[0].strip()
