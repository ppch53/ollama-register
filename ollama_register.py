from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from filelock import FileLock

from src.account_store import append_api_key, load_accounts, persist_account_result, save_accounts
from src.browser_flow import BrowserFlow
from src.config import load_config
from src.flaresolverr_client import FlareSolverrClient
from src.hero_sms_provider import HeroSmsProvider
from src.logging_config import StructuredRunLogger, configure_structured_logging
from src.models import AccountRecord, AppConfig
from src.passwords import generate_strong_password
from src.proxy_session import ProxySession, ProxySessionFactory
from src.tempmail_client import TempMailClient
from src.turnstile_client import TurnstileClient


@dataclass(slots=True)
class ValidationResult:
    ok: bool
    status_code: int | None
    body_preview: str


@dataclass(slots=True)
class RegistrationResult:
    record: AccountRecord
    validation: ValidationResult
    persistence: dict[str, Any]
    proxy_identity: str


class OllamaPlaywrightRegister:
    def __init__(
        self,
        config: AppConfig,
        *,
        browser_flow_factory=BrowserFlow,
        tempmail_client_factory=TempMailClient,
        turnstile_client_factory=TurnstileClient,
        flaresolverr_client_factory=FlareSolverrClient,
        phone_provider_factory=HeroSmsProvider,
        http_client: httpx.Client | None = None,
        logger: StructuredRunLogger | None = None,
        proxy_session_factory: ProxySessionFactory | None = None,
    ) -> None:
        self.config = config
        self.browser_flow_factory = browser_flow_factory
        self.tempmail_client_factory = tempmail_client_factory
        self.turnstile_client_factory = turnstile_client_factory
        self.flaresolverr_client_factory = flaresolverr_client_factory
        self.phone_provider_factory = phone_provider_factory
        self._http_client = http_client
        self.proxy_session_factory = proxy_session_factory or ProxySessionFactory()
        self.logger = logger or configure_structured_logging(
            "ollama_register",
            artifacts_root=config.artifacts_dir or "artifacts",
        )

    def register_single(self) -> RegistrationResult:
        proxy_session = self._create_proxy_session()
        owned_http_clients: list[httpx.Client] = []
        tempmail_client = None
        turnstile_client = None
        flaresolverr_client = None
        phone_provider = None

        try:
            sticky_proxy_url = proxy_session.proxy_url if proxy_session else None
            identity_proxy = sticky_proxy_url or self.config.registration_proxy
            proxy_identity = (
                proxy_session.ip_pre if proxy_session else self._resolve_proxy_identity(identity_proxy)
            )

            if proxy_session:
                self.logger.info(
                    "proxy",
                    "sticky proxy session acquired",
                    **proxy_session.safe_summary(),
                )
            fingerprint_country = self._resolve_fingerprint_country(proxy_identity, proxy_session)
            self._enforce_rate_limits(proxy_identity)
            self._record_attempt(proxy_identity)
            effective_config = self._config_with_proxy(sticky_proxy_url)

            tempmail_client = self.tempmail_client_factory(
                base_url=self.config.tempmail_base_url,
                api_key=self.config.tempmail_api_key,
                http_client=self._make_helper_http_client(
                    base_url=self.config.tempmail_base_url,
                    timeout=self.config.default_timeout_seconds * 2,
                    proxy_url=sticky_proxy_url,
                    owned_clients=owned_http_clients,
                ),
                timeout=self.config.default_timeout_seconds * 2,
            )
            turnstile_client = self.turnstile_client_factory(
                self.config.turnstile_solver_url,
                http_client=self._make_helper_http_client(
                    base_url=self.config.turnstile_solver_url,
                    timeout=self.config.default_timeout_seconds,
                    proxy_url=sticky_proxy_url,
                    owned_clients=owned_http_clients,
                ),
                timeout=self.config.default_timeout_seconds,
            )
            flaresolverr_client = self.flaresolverr_client_factory(
                self.config.flaresolverr_url,
                http_client=self._make_helper_http_client(
                    base_url=self.config.flaresolverr_url,
                    timeout=self.config.default_timeout_seconds * 2,
                    proxy_url=sticky_proxy_url,
                    owned_clients=owned_http_clients,
                ),
                timeout=self.config.default_timeout_seconds * 2,
            )
            phone_provider = self.phone_provider_factory(
                base_url=self.config.hero_sms_base_url,
                api_key=self.config.hero_sms_api_key,
                service=self.config.hero_sms_service,
                country=self.config.hero_sms_country_id,
                operator=self.config.hero_sms_operator,
                max_price=self.config.hero_sms_max_price,
                fixed_price=self.config.hero_sms_fixed_price,
                phone_exception=self.config.hero_sms_phone_exception,
                artifacts_dir=self.logger.artifacts_dir,
                http_client=self._make_helper_http_client(
                    base_url=self.config.hero_sms_base_url,
                    timeout=self.config.default_timeout_seconds * 2,
                    proxy_url=sticky_proxy_url,
                    owned_clients=owned_http_clients,
                ),
                timeout=self.config.default_timeout_seconds * 2,
                progress=self._progress,
            )
            flow = self.browser_flow_factory(
                effective_config,
                tempmail_client=tempmail_client,
                turnstile_client=turnstile_client,
                flaresolverr_client=flaresolverr_client,
                phone_provider=phone_provider,
                progress=self._progress,
                fingerprint_country=fingerprint_country,
                proxy_checkpoint=(
                    (lambda stage: self._check_proxy_mid(proxy_session, stage))
                    if proxy_session
                    else None
                ),
            )

            address = tempmail_client.create_address()
            password = generate_strong_password()
            self.logger.info(
                "register",
                "created temp mail address",
                email=address.address,
                proxy_identity=proxy_identity,
            )

            record = flow.run(
                email=address.address,
                jwt=address.jwt,
                password=password,
            )

            self._check_proxy_mid(proxy_session)
            validation_proxy = None if self._helpers_use_direct_connection() else sticky_proxy_url
            validation = self.validate_api_key(record.api_key, proxy_url=validation_proxy)
            self._check_proxy_post(proxy_session)
            record.status = "verified" if validation.ok else "unverified"
            persistence = persist_account_result(
                self.config.accounts_file,
                self.config.api_key_file,
                record,
                append_production_key=validation.ok,
            )
            if not validation.ok:
                self.logger.warning(
                    "validate",
                    "api key did not validate; account persisted as unverified",
                    email=record.email,
                    status_code=validation.status_code,
                )
            return RegistrationResult(
                record=record,
                validation=validation,
                persistence=persistence,
                proxy_identity=proxy_identity,
            )
        finally:
            for resource in (tempmail_client, turnstile_client, flaresolverr_client, phone_provider):
                if resource is not None:
                    resource.close()
            for client in owned_http_clients:
                client.close()
            if proxy_session:
                proxy_session.close()

    def revalidate(self) -> list[RegistrationResult]:
        accounts = load_accounts(self.config.accounts_file)
        results: list[RegistrationResult] = []
        mutated = False
        for account in accounts:
            if account.status != "unverified":
                continue
            validation = self.validate_api_key(account.api_key)
            if not validation.ok:
                results.append(
                    RegistrationResult(
                        record=account,
                        validation=validation,
                        persistence={
                            "account_added": False,
                            "api_key_added": False,
                            "status": account.status,
                        },
                        proxy_identity="revalidate",
                    )
                )
                continue
            account.status = "verified"
            save_accounts(self.config.accounts_file, accounts)
            append_api_key(self.config.api_key_file, account.api_key)
            mutated = True
            results.append(
                RegistrationResult(
                    record=account,
                    validation=validation,
                    persistence={
                        "account_added": False,
                        "api_key_added": True,
                        "status": account.status,
                    },
                    proxy_identity="revalidate",
                )
            )
        if mutated:
            self.logger.info("revalidate", "promoted unverified accounts", promoted_count=sum(1 for result in results if result.validation.ok))
        return results

    def validate_api_key(self, api_key: str, proxy_url: str | None = None) -> ValidationResult:
        client = self._http_client or httpx.Client(
            timeout=self.config.default_timeout_seconds,
            proxy=proxy_url,
        )
        close_client = self._http_client is None
        try:
            response = client.get(
                self.config.api_key_validation_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                },
            )
            return ValidationResult(
                ok=response.status_code == 200,
                status_code=response.status_code,
                body_preview=response.text[:300],
            )
        except httpx.HTTPError as exc:
            return ValidationResult(
                ok=False,
                status_code=None,
                body_preview=str(exc),
            )
        finally:
            if close_client:
                client.close()

    def _progress(self, message: str) -> None:
        self.logger.info("browser", message)

    def _create_proxy_session(self) -> ProxySession | None:
        if not self.config.ollama_sticky_proxy:
            return None
        session = self.proxy_session_factory.create()
        if session is None:
            return None
        try:
            session.check_ip("pre")
        except Exception:
            session.close()
            raise
        return session

    def _config_with_proxy(self, proxy_url: str | None) -> AppConfig:
        if not proxy_url:
            return self.config
        return replace(
            self.config,
            playwright_proxy_server=proxy_url,
            registration_proxy=proxy_url,
        )

    def _make_helper_http_client(
        self,
        *,
        base_url: str,
        timeout: float,
        proxy_url: str | None,
        owned_clients: list[httpx.Client],
    ) -> httpx.Client | None:
        if not proxy_url or self._helpers_use_direct_connection():
            return None
        client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, proxy=proxy_url)
        owned_clients.append(client)
        return client

    @staticmethod
    def _helpers_use_direct_connection() -> bool:
        value = os.getenv("PROXY_HELPERS_DIRECT", "").strip().lower()
        return value in {"1", "true", "yes", "on"}

    def _check_proxy_mid(self, proxy_session: ProxySession | None, stage: str = "mid") -> None:
        if proxy_session is None:
            return
        proxy_session.check_ip("mid")
        self.logger.info(
            "proxy",
            "sticky proxy mid-check",
            checkpoint=stage,
            **proxy_session.safe_summary(),
        )

    def _check_proxy_post(self, proxy_session: ProxySession | None) -> None:
        if proxy_session is None:
            return
        proxy_session.check_ip("post")
        self.logger.info("proxy", "sticky proxy post-check", **proxy_session.safe_summary())

    def _resolve_proxy_identity(self, proxy: str | None = None) -> str:
        effective_proxy = proxy if proxy is not None else self.config.registration_proxy
        if not effective_proxy:
            return "direct"
        transport_kwargs: dict[str, Any] = {"timeout": min(self.config.default_timeout_seconds, 10)}
        transport_kwargs["proxy"] = effective_proxy
        try:
            with httpx.Client(**transport_kwargs) as client:
                response = client.get("https://api.ipify.org?format=json")
                response.raise_for_status()
                data = response.json()
                ip = str(data.get("ip") or "").strip()
                if ip:
                    return ip
        except Exception:
            return self._redact_proxy_identity(effective_proxy)
        return self._redact_proxy_identity(effective_proxy)

    def _resolve_fingerprint_country(
        self,
        proxy_identity: str,
        proxy_session: ProxySession | None = None,
    ) -> str | None:
        if self.config.ollama_fingerprint_country:
            return self.config.ollama_fingerprint_country
        if proxy_identity == "direct" or ":" in proxy_identity:
            return None
        if proxy_session:
            return proxy_session.resolve_country(
                proxy_identity,
                timeout=min(self.config.default_timeout_seconds, 10),
            )
        try:
            with httpx.Client(timeout=min(self.config.default_timeout_seconds, 10)) as client:
                response = client.get(f"https://ipinfo.io/{proxy_identity}/country")
                response.raise_for_status()
                country = response.text.strip().upper()
                if len(country) == 2 and country.isalpha():
                    return country
        except Exception:
            return None
        return None

    @staticmethod
    def _redact_proxy_identity(proxy_url: str | None) -> str:
        if not proxy_url:
            return "direct"
        try:
            from urllib.parse import urlparse

            parsed = urlparse(proxy_url)
            if parsed.hostname:
                suffix = f":{parsed.port}" if parsed.port else ""
                scheme = parsed.scheme or "proxy"
                return f"{scheme}://{parsed.hostname}{suffix}"
        except Exception:
            pass
        return "proxy-unresolved"

    def _load_rate_limit_state(self) -> dict[str, Any]:
        path = self.config.rate_limit_state_file
        if not path.exists():
            return {"identities": {}}
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return {"identities": {}}
        return json.loads(raw)

    def _save_rate_limit_state(self, payload: dict[str, Any]) -> None:
        path = self.config.rate_limit_state_file
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name,
            suffix=".tmp",
            dir=path.parent,
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    def _with_rate_limit_lock(self) -> FileLock:
        return FileLock(str(self.config.rate_limit_state_file) + ".lock")

    def _enforce_rate_limits(self, proxy_identity: str) -> None:
        now = time.time()
        with self._with_rate_limit_lock():
            state = self._load_rate_limit_state()
            identity_state = state.setdefault("identities", {}).setdefault(proxy_identity, {})
            attempts = [
                float(ts)
                for ts in identity_state.get("attempts", [])
                if now - float(ts) < 86400
            ]
            effective_min_interval = self.config.ollama_min_interval_minutes * 60
            effective_max_per_day = self.config.ollama_max_per_day
            if attempts:
                effective_min_interval *= 2
                effective_max_per_day = min(effective_max_per_day, 5)
            if len(attempts) >= effective_max_per_day:
                raise RuntimeError(
                    f"Rate limit hit for {proxy_identity}: {len(attempts)} attempts in the last 24h "
                    f"(max {effective_max_per_day})"
                )
            if attempts and now - attempts[-1] < effective_min_interval:
                wait_seconds = int(effective_min_interval - (now - attempts[-1]))
                raise RuntimeError(
                    f"Rate limit hit for {proxy_identity}: wait {wait_seconds}s before the next registration"
                )

    def _record_attempt(self, proxy_identity: str) -> None:
        now = time.time()
        with self._with_rate_limit_lock():
            state = self._load_rate_limit_state()
            identity_state = state.setdefault("identities", {}).setdefault(proxy_identity, {})
            attempts = [
                float(ts)
                for ts in identity_state.get("attempts", [])
                if now - float(ts) < 86400
            ]
            attempts.append(now)
            identity_state["attempts"] = attempts
            self._save_rate_limit_state(state)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Register Ollama accounts with the Playwright browser flow",
    )
    parser.add_argument(
        "-n",
        "--count",
        type=int,
        default=int(os.getenv("COUNT", "1")),
        help="How many accounts to register sequentially",
    )
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=int(os.getenv("OLLAMA_MIN_INTERVAL_MINUTES", "10")),
        help="Delay between sequential registrations",
    )
    parser.add_argument(
        "--revalidate",
        action="store_true",
        help="Revalidate previously unverified API keys",
    )
    return parser


def cli() -> None:
    load_dotenv(".env", override=False)
    args = build_argument_parser().parse_args()
    config = load_config(".env")
    register = OllamaPlaywrightRegister(config)

    if args.revalidate:
        results = register.revalidate()
        print(f"[revalidate] processed={len(results)}", flush=True)
        return

    for index in range(args.count):
        result = register.register_single()
        print(
            f"[register] {index + 1}/{args.count} email={result.record.email} "
            f"status={result.record.status} validated={result.validation.ok}",
            flush=True,
        )
        if index + 1 < args.count:
            time.sleep(max(args.interval_minutes, 0) * 60)


if __name__ == "__main__":
    cli()
