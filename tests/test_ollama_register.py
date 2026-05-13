from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from ollama_register import OllamaPlaywrightRegister
from src.models import AccountRecord, AppConfig, TempMailAddress
from src.proxy_session import ProxyDriftError


class FakeClosable:
    instances: list["FakeClosable"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.closed = False
        self.__class__.instances.append(self)

    def close(self) -> None:
        self.closed = True


class FakeTempmailClient(FakeClosable):
    def create_address(self) -> TempMailAddress:
        return TempMailAddress(
            address_id=1,
            address="user@example.com",
            jwt="jwt-token",
            created_at="now",
            expires_at="later",
        )


class FakePhoneProvider(FakeClosable):
    @property
    def is_configured(self) -> bool:
        return False


class FakeBrowserFlow:
    instances: list["FakeBrowserFlow"] = []

    def __init__(self, config, **kwargs) -> None:
        self.config = config
        self.kwargs = kwargs
        self.called_with = None
        self.raise_error = kwargs.get("raise_error", False)
        FakeBrowserFlow.instances.append(self)

    def run(self, *, email: str, jwt: str, password: str) -> AccountRecord:
        self.called_with = {"email": email, "jwt": jwt, "password": password}
        if self.raise_error:
            raise RuntimeError("browser flow failed")
        return AccountRecord(
            email=email,
            password=password,
            api_key="ok_test_key_1234567890",
            cookies=[{"name": "session", "value": "abc"}],
        )


class FakeProxySession:
    def __init__(self, *, drift_on: str | None = None) -> None:
        self.proxy_url = "http://sticky-user:secret@proxy.test:8000"
        self.ip_pre = ""
        self.ip_mid = ""
        self.ip_post = ""
        self.country = "DE"
        self.drift_detected = False
        self.session_id = "sticky-session"
        self.closed = False
        self.drift_on = drift_on

    def check_ip(self, stage: str) -> str:
        if self.drift_on == stage:
            self.drift_detected = True
            raise ProxyDriftError("proxy drift")
        ip = "203.0.113.10"
        if stage == "pre":
            self.ip_pre = ip
        elif stage == "mid":
            self.ip_mid = ip
        elif stage == "post":
            self.ip_post = ip
        return ip

    def safe_summary(self) -> dict:
        return {
            "provider": "test",
            "session_id": self.session_id,
            "ip_pre": self.ip_pre,
            "ip_mid": self.ip_mid,
            "ip_post": self.ip_post,
            "drift_detected": self.drift_detected,
            "country": self.country,
        }

    def resolve_country(self, ip: str, *, timeout: float = 10.0) -> str | None:
        return self.country

    def close(self) -> None:
        self.closed = True

class FakeProxySessionFactory:
    def __init__(self, session: FakeProxySession) -> None:
        self.session = session

    def create(self, account_hint: str = "") -> FakeProxySession:
        return self.session


class RaisingBrowserFlow(FakeBrowserFlow):
    def run(self, *, email: str, jwt: str, password: str) -> AccountRecord:
        raise RuntimeError("browser flow failed")


def build_config(root: Path) -> AppConfig:
    return AppConfig(
        sign_up_url="https://signin.ollama.com/sign-up",
        settings_keys_url="https://ollama.com/settings/keys",
        tempmail_base_url="http://tempmail.test",
        tempmail_api_key="tm-key",
        turnstile_solver_url="http://turnstile.test",
        flaresolverr_url="http://flaresolverr.test",
        hero_sms_base_url="https://hero-sms.test",
        hero_sms_api_key=None,
        hero_sms_service=None,
        hero_sms_country_id=None,
        hero_sms_operator=None,
        hero_sms_max_price=None,
        hero_sms_fixed_price=False,
        hero_sms_phone_exception=None,
        hero_sms_poll_interval_seconds=1.0,
        hero_sms_poll_timeout_seconds=10.0,
        accounts_file=root / "accounts.json",
        api_key_file=root / "apikey.txt",
        api_key_validation_url="https://ollama.com/api/tags",
        artifacts_dir=root / "artifacts",
        browser_headless=True,
        playwright_proxy_server=None,
        registration_proxy=None,
        ollama_sticky_proxy=False,
        ollama_profile_root=root / "ollama_profiles",
        ollama_fingerprint_registry=root / "ollama_fingerprints.json",
        ollama_fingerprint_country="US",
        default_timeout_seconds=5.0,
        mail_poll_interval_seconds=1.0,
        mail_poll_timeout_seconds=10.0,
        turnstile_poll_interval_seconds=1.0,
        turnstile_poll_timeout_seconds=10.0,
        rate_limit_state_file=root / ".rate-limit.json",
        ollama_max_per_day=10,
        ollama_min_interval_minutes=0,
    )


class OllamaRegisterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()
        FakeBrowserFlow.instances.clear()
        FakeClosable.instances.clear()

    def make_register(
        self,
        *,
        browser_flow_factory=FakeBrowserFlow,
        validation_status: int = 200,
        config: AppConfig | None = None,
        proxy_session_factory=None,
    ) -> OllamaPlaywrightRegister:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(validation_status, text='{"models":[]}')
        )
        config = config or build_config(self.root)
        http_client = httpx.Client(transport=transport)
        return OllamaPlaywrightRegister(
            config,
            browser_flow_factory=browser_flow_factory,
            tempmail_client_factory=FakeTempmailClient,
            turnstile_client_factory=FakeClosable,
            flaresolverr_client_factory=FakeClosable,
            phone_provider_factory=FakePhoneProvider,
            http_client=http_client,
            proxy_session_factory=proxy_session_factory,
        )

    def test_register_single_uses_browser_flow(self) -> None:
        register = self.make_register()
        result = register.register_single()
        self.assertEqual("verified", result.record.status)
        self.assertEqual(1, len(FakeBrowserFlow.instances))
        self.assertEqual("user@example.com", FakeBrowserFlow.instances[0].called_with["email"])

    def test_persistence_format_matches_expected_schema(self) -> None:
        register = self.make_register()
        register.register_single()
        accounts_payload = json.loads((self.root / "accounts.json").read_text(encoding="utf-8"))
        self.assertEqual(
            ["api_key", "cookies", "email", "password", "status"],
            sorted(accounts_payload[0].keys()),
        )
        self.assertEqual("verified", accounts_payload[0]["status"])
        self.assertIn("ok_test_key_1234567890", (self.root / "apikey.txt").read_text(encoding="utf-8"))

    def test_failed_registration_does_not_write_partial_records(self) -> None:
        register = self.make_register(browser_flow_factory=RaisingBrowserFlow)
        with self.assertRaises(RuntimeError):
            register.register_single()
        self.assertFalse((self.root / "accounts.json").exists())
        self.assertFalse((self.root / "apikey.txt").exists())

    def test_resources_are_closed_on_exception(self) -> None:
        register = self.make_register(browser_flow_factory=RaisingBrowserFlow)
        with self.assertRaises(RuntimeError):
            register.register_single()
        self.assertTrue(FakeClosable.instances)
        self.assertTrue(all(instance.closed for instance in FakeClosable.instances))


    def test_sticky_proxy_session_is_passed_to_browser_flow(self) -> None:
        config = build_config(self.root)
        config.ollama_sticky_proxy = True
        config.ollama_fingerprint_country = None
        proxy_session = FakeProxySession()
        register = self.make_register(
            config=config,
            proxy_session_factory=FakeProxySessionFactory(proxy_session),
        )

        result = register.register_single()

        self.assertEqual("verified", result.record.status)
        flow = FakeBrowserFlow.instances[0]
        self.assertEqual("http://sticky-user:secret@proxy.test:8000", flow.config.playwright_proxy_server)
        self.assertEqual("http://sticky-user:secret@proxy.test:8000", flow.config.registration_proxy)
        self.assertEqual("DE", flow.kwargs["fingerprint_country"])
        self.assertTrue(proxy_session.closed)

    def test_sticky_proxy_helper_direct_mode_preserves_browser_proxy(self) -> None:
        config = build_config(self.root)
        config.ollama_sticky_proxy = True
        proxy_session = FakeProxySession()
        register = self.make_register(
            config=config,
            proxy_session_factory=FakeProxySessionFactory(proxy_session),
        )

        with patch.dict("os.environ", {"PROXY_HELPERS_DIRECT": "1"}, clear=False):
            result = register.register_single()

        self.assertEqual("verified", result.record.status)
        flow = FakeBrowserFlow.instances[0]
        self.assertEqual("http://sticky-user:secret@proxy.test:8000", flow.config.playwright_proxy_server)
        self.assertEqual("US", flow.kwargs["fingerprint_country"])
        self.assertTrue(proxy_session.closed)

    def test_proxy_drift_aborts_before_persistence(self) -> None:
        config = build_config(self.root)
        config.ollama_sticky_proxy = True
        proxy_session = FakeProxySession(drift_on="mid")
        register = self.make_register(
            config=config,
            proxy_session_factory=FakeProxySessionFactory(proxy_session),
        )

        with self.assertRaises(ProxyDriftError):
            register.register_single()

        self.assertFalse((self.root / "accounts.json").exists())
        self.assertFalse((self.root / "apikey.txt").exists())
        self.assertTrue(proxy_session.closed)


if __name__ == "__main__":
    unittest.main()
