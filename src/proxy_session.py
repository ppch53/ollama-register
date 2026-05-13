"""Provider-aware sticky proxy sessions for registration flows."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, unquote, urlparse

import httpx

_ALLOWED_SCHEMES = {"http", "https", "socks5"}
_DEFAULT_SESSION_TEMPLATE = "{username}-session-{session}"
_DEFAULT_TEMPLATE_PROVIDERS = {"smartproxy", "iproyal"}
_TEMPLATE_REQUIRED_PROVIDERS = {"rayobyte", "oxylabs", "brightdata", "generic"}
_API_EXTRACTION_PROVIDERS = {"b2proxy", "bestgo"}
_SUPPORTED_PROVIDERS = (
    _DEFAULT_TEMPLATE_PROVIDERS | _TEMPLATE_REQUIRED_PROVIDERS | _API_EXTRACTION_PROVIDERS
)
_DEFAULT_IP_CHECK_URL = "https://api.ipify.org?format=json"
_PROVIDER_ALIASES = {
    "b2": "b2proxy",
    "b2proxyresidential": "b2proxy",
    "bestgo": "b2proxy",
    "bestgorrp": "b2proxy",
    "bright": "brightdata",
    "bright_data": "brightdata",
    "bright-data": "brightdata",
    "ip-royal": "iproyal",
    "ip_royal": "iproyal",
    "smart-proxy": "smartproxy",
    "smart_proxy": "smartproxy",
}


class ProxyConfigError(ValueError):
    """Raised when sticky proxy configuration is incomplete or unsupported."""


class ProxyDriftError(RuntimeError):
    """Raised when a sticky proxy session changes IP during registration."""


@dataclass(slots=True)
class ProxyConfig:
    enabled: bool = False
    provider: str = "generic"
    scheme: str = "http"
    host: str = ""
    port: int | None = None
    username: str = ""
    password: str = ""
    country: str | None = None
    session_template: str | None = None
    url_template: str | None = None
    api_url: str | None = None
    raw_proxy: str | None = None
    ip_check_url: str = _DEFAULT_IP_CHECK_URL

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ProxyConfig":
        source = env or os.environ
        enabled = _env_bool(source.get("OLLAMA_STICKY_PROXY"), default=False)
        raw_proxy = _clean(source.get("REGISTER_PROXY")) or _clean(source.get("PLAYWRIGHT_PROXY_SERVER"))
        provider = _normalise_provider(_clean(source.get("PROXY_PROVIDER")) or _infer_provider(raw_proxy))
        scheme = _clean(source.get("PROXY_SCHEME"))
        host = _clean(source.get("PROXY_HOST"))
        port_raw = _clean(source.get("PROXY_PORT"))
        username = _clean(source.get("PROXY_USERNAME"))
        password = _clean(source.get("PROXY_PASSWORD"))

        if host and username and password:
            port = _parse_port(port_raw, required=True)
            scheme = scheme or "http"
        elif raw_proxy:
            parsed = urlparse(raw_proxy)
            scheme = scheme or parsed.scheme
            host = host or (parsed.hostname or "")
            port = _parse_port(port_raw, required=False) or parsed.port
            username = username or unquote(parsed.username or "")
            password = password or unquote(parsed.password or "")
        else:
            port = _parse_port(port_raw, required=False)

        scheme = (scheme or "http").lower()
        country = _clean(source.get("PROXY_COUNTRY"))
        session_template = _clean(source.get("PROXY_SESSION_TEMPLATE"))
        url_template = _clean(source.get("PROXY_URL_TEMPLATE"))
        api_url = _clean(source.get("PROXY_API_URL")) or _clean(source.get("REGISTER_PROXY_API"))
        ip_check_url = _clean(source.get("PROXY_IP_CHECK_URL")) or _DEFAULT_IP_CHECK_URL

        config = cls(
            enabled=enabled,
            provider=provider,
            scheme=scheme,
            host=host,
            port=port,
            username=username,
            password=password,
            country=country.upper() if country else None,
            session_template=session_template,
            url_template=url_template,
            api_url=api_url,
            raw_proxy=raw_proxy,
            ip_check_url=ip_check_url,
        )
        if enabled:
            config.validate_sticky()
        return config

    def validate_sticky(self) -> None:
        if self.provider not in _SUPPORTED_PROVIDERS:
            raise ProxyConfigError(f"Unsupported proxy provider: {self.provider}")
        if self.provider in _API_EXTRACTION_PROVIDERS:
            if not self.api_url:
                raise ProxyConfigError(
                    f"Provider {self.provider} requires PROXY_API_URL or REGISTER_PROXY_API"
                )
            return
        if self.url_template:
            return
        if self.scheme not in _ALLOWED_SCHEMES:
            raise ProxyConfigError(f"Unsupported proxy scheme: {self.scheme}")
        missing = [
            name
            for name, value in (
                ("PROXY_HOST", self.host),
                ("PROXY_PORT", self.port),
                ("PROXY_USERNAME", self.username),
                ("PROXY_PASSWORD", self.password),
            )
            if not value
        ]
        if missing:
            raise ProxyConfigError(
                "Sticky proxy requires split PROXY_* fields or REGISTER_PROXY; missing "
                + ", ".join(missing)
            )
        if self.provider in _TEMPLATE_REQUIRED_PROVIDERS and not self.session_template:
            raise ProxyConfigError(
                f"Provider {self.provider} requires PROXY_SESSION_TEMPLATE or PROXY_URL_TEMPLATE"
            )

    def username_for_session(self, session_id: str) -> str:
        template = self.session_template
        if template is None and self.provider in _DEFAULT_TEMPLATE_PROVIDERS:
            template = _DEFAULT_SESSION_TEMPLATE
        if template is None:
            raise ProxyConfigError(
                f"Provider {self.provider} requires PROXY_SESSION_TEMPLATE or PROXY_URL_TEMPLATE"
            )
        return _render_template(template, self, session_id)

    def proxy_url_for_session(self, session_id: str) -> str:
        if self.url_template:
            return _render_template(self.url_template, self, session_id, encode_password=False)
        username = quote(self.username_for_session(session_id), safe="")
        password = quote(self.password, safe="")
        return f"{self.scheme}://{username}:{password}@{self.host}:{self.port}"

    def proxy_url_from_api_response(self, payload: str) -> str:
        if self.provider not in _API_EXTRACTION_PROVIDERS:
            raise ProxyConfigError(f"Provider {self.provider} does not support API extraction")
        first_line = payload.strip().splitlines()[0].strip()
        if not first_line:
            raise ProxyConfigError("Proxy API returned an empty response")
        parsed = urlparse(first_line)
        if parsed.scheme and parsed.hostname and parsed.port:
            return first_line
        if ":" not in first_line:
            raise ProxyConfigError(f"Unexpected proxy API response: {first_line}")
        return f"{self.scheme}://{first_line}"


@dataclass(slots=True)
class ProxySession:
    config: ProxyConfig
    session_id: str
    proxy_url: str
    ip_pre: str = ""
    ip_mid: str = ""
    ip_post: str = ""
    country: str | None = None
    drift_detected: bool = False
    _client: httpx.Client | None = field(default=None, repr=False)

    def check_ip(self, stage: str) -> str:
        ip = self._resolve_ip()
        if stage == "pre":
            self.ip_pre = ip
        elif stage == "post":
            self.ip_post = ip
        else:
            self.ip_mid = ip
        if self.ip_pre and ip != self.ip_pre:
            self.drift_detected = True
            raise ProxyDriftError(
                f"proxy_drift at {stage}: {self.ip_pre} -> {ip}"
            )
        return ip

    def safe_summary(self) -> dict[str, Any]:
        return {
            "provider": self.config.provider,
            "session_id": self.session_id,
            "ip_pre": self.ip_pre,
            "ip_mid": self.ip_mid,
            "ip_post": self.ip_post,
            "drift_detected": self.drift_detected,
            "country": self.country or self.config.country,
        }

    def resolve_country(self, ip: str, *, timeout: float = 10.0) -> str | None:
        if self.config.country:
            self.country = self.config.country
            return self.country
        try:
            with httpx.Client(proxy=self.proxy_url, timeout=timeout) as client:
                response = client.get(f"https://ipinfo.io/{ip}/country")
                response.raise_for_status()
                country = response.text.strip().upper()
        except httpx.HTTPError:
            return None
        if len(country) == 2 and country.isalpha():
            self.country = country
            return country
        return None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _resolve_ip(self) -> str:
        client = self._client or httpx.Client(proxy=self.proxy_url, timeout=20.0)
        owns_client = self._client is None
        try:
            response = client.get(self.config.ip_check_url)
            response.raise_for_status()
            return _extract_ip(response)
        finally:
            if owns_client:
                client.close()


class ProxySessionFactory:
    def __init__(self, config: ProxyConfig | None = None) -> None:
        self.config = config or ProxyConfig.from_env()

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def create(self, account_hint: str = "", session_id: str | None = None) -> ProxySession | None:
        if not self.config.enabled:
            return None
        sid = session_id or uuid.uuid4().hex[:16]
        proxy_url = self._build_proxy_url(sid)
        return ProxySession(
            config=self.config,
            session_id=sid,
            proxy_url=proxy_url,
            country=self.config.country,
        )

    def _build_proxy_url(self, session_id: str) -> str:
        if self.config.provider in _API_EXTRACTION_PROVIDERS:
            return self._create_api_proxy_url()
        return self.config.proxy_url_for_session(session_id)

    def _create_api_proxy_url(self) -> str:
        api_url = self.config.api_url
        if not api_url:
            raise ProxyConfigError(
                f"Provider {self.config.provider} requires PROXY_API_URL or REGISTER_PROXY_API"
            )
        try:
            with httpx.Client(timeout=20.0) as client:
                response = client.get(api_url)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProxyConfigError(f"Proxy API request failed: {exc}") from exc
        return self.config.proxy_url_from_api_response(response.text)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().strip('"').strip("'")
    return value or None


def _env_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ProxyConfigError(f"Invalid OLLAMA_STICKY_PROXY value: {raw}")


def _parse_port(raw: str | None, *, required: bool) -> int | None:
    if not raw:
        if required:
            raise ProxyConfigError("PROXY_PORT is required")
        return None
    try:
        port = int(raw)
    except ValueError as exc:
        raise ProxyConfigError(f"Invalid PROXY_PORT: {raw}") from exc
    if not (1 <= port <= 65535):
        raise ProxyConfigError(f"Invalid PROXY_PORT: {raw}")
    return port


def _normalise_provider(provider: str | None) -> str:
    value = (provider or "generic").strip().lower().replace(" ", "")
    return _PROVIDER_ALIASES.get(value, value)


def _infer_provider(raw_proxy: str | None) -> str:
    if not raw_proxy:
        return "generic"
    host = (urlparse(raw_proxy).hostname or "").lower()
    if "bestgo.work" in host:
        return "b2proxy"
    if "rayobyte" in host:
        return "rayobyte"
    if "smartproxy" in host:
        return "smartproxy"
    if "iproyal" in host:
        return "iproyal"
    if "oxylabs" in host:
        return "oxylabs"
    if "brightdata" in host or "brd.superproxy" in host:
        return "brightdata"
    return "generic"


def _render_template(
    template: str,
    config: ProxyConfig,
    session_id: str,
    *,
    encode_password: bool = True,
) -> str:
    password = quote(config.password, safe="") if encode_password else config.password
    return template.format(
        scheme=config.scheme,
        host=config.host,
        port=config.port or "",
        username=config.username,
        password=password,
        session=session_id,
        country=(config.country or "").lower(),
        country_upper=(config.country or "").upper(),
    )


def _extract_ip(response: httpx.Response) -> str:
    content_type = response.headers.get("content-type", "")
    text = response.text.strip()
    if "json" in content_type or text.startswith("{"):
        payload = response.json()
        for key in ("ip", "origin", "query"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value.split(",", 1)[0].strip()
    return text.splitlines()[0].strip()
