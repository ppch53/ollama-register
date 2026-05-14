from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from puter_register_v2 import load_puter_proxy_config, register_puter_mailbox_providers
from src.mailbox_provider import MailboxProviderPool
from src.outlook_mailbox_provider import OutlookMailboxProvider
from src.sticky_proxy import ProxyProviderConfig, StickyProxyManager, _extract_ip


class FakeSyncClient:
    requested_urls: list[str] = []
    created_proxies: list[str | None] = []

    def __init__(self, *args, **kwargs) -> None:
        self.proxy = kwargs.get("proxy")
        self.closed = False
        self.__class__.created_proxies.append(self.proxy)

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def get(self, url: str) -> httpx.Response:
        self.__class__.requested_urls.append(url)
        return httpx.Response(
            200,
            text="107.151.234.173:10001\r\n",
            request=httpx.Request("GET", url),
        )

    def close(self) -> None:
        self.closed = True


class StickyProxyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tempdir.name)
        FakeSyncClient.requested_urls.clear()
        FakeSyncClient.created_proxies.clear()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_b2proxy_config_from_env(self) -> None:
        config = ProxyProviderConfig.from_env(
            {
                "PROXY_PROVIDER": "bestgo",
                "PROXY_SCHEME": "http",
                "PROXY_API_URL": "http://proxy-api.test/gen",
                "PROXY_COUNTRY": "us",
            }
        )

        self.assertEqual("b2proxy", config.provider)
        self.assertEqual("http", config.proxy_type)
        self.assertEqual("http://proxy-api.test/gen", config.api_url)
        self.assertEqual("US", config.country)
        self.assertTrue(config.uses_api_extraction)

    def test_puter_proxy_env_overrides_state_file(self) -> None:
        (self.state_dir / "proxy_config.json").write_text(
            """
{
  "host": "old-proxy.test",
  "port": 9000,
  "username": "old-user",
  "password": "old-pass",
  "country": "GB",
  "proxy_type": "socks5"
}
""".strip(),
            encoding="utf-8",
        )

        with patch.dict(
            "os.environ",
            {
                "PROXY_PROVIDER": "b2proxy",
                "PROXY_SCHEME": "http",
                "PROXY_API_URL": "http://proxy-api.test/gen",
            },
            clear=False,
        ):
            config = load_puter_proxy_config(self.state_dir)

        self.assertEqual("b2proxy", config.provider)
        self.assertEqual("http", config.proxy_type)
        self.assertEqual("http://proxy-api.test/gen", config.api_url)
        self.assertEqual("la.residential.rayobyte.com", config.host)

    def test_puter_registers_outlook_pool_from_env(self) -> None:
        pool = MailboxProviderPool(
            registry_path=self.state_dir / "used_emails.json",
            health_path=self.state_dir / "mailbox_health.json",
        )

        with patch.dict(
            "os.environ",
            {"PUTER_OUTLOOK_POOL_FILE": str(self.state_dir / "outlook_pool.txt")},
            clear=False,
        ):
            register_puter_mailbox_providers(pool, self.state_dir)

        self.assertEqual(1, len(pool._providers))
        self.assertIsInstance(pool._providers[0], OutlookMailboxProvider)

    async def test_outlook_provider_creates_address_and_marks_used(self) -> None:
        pool_path = self.state_dir / "outlook_pool.txt"
        used_path = self.state_dir / "outlook_used.txt"
        pool_path.write_text(
            "user@example.com----pass----9e5f94bc-e8a4-4e73-b8be-63364c29d753"
            "----M.C1234567890abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ\n",
            encoding="utf-8",
        )
        provider = OutlookMailboxProvider(pool_path, used_path)

        email = await provider.create_address()

        self.assertEqual("user@example.com", email)
        self.assertIn("user@example.com", used_path.read_text(encoding="utf-8"))

    async def test_b2proxy_api_extraction_acquires_session_proxy_url(self) -> None:
        config = ProxyProviderConfig.from_env(
            {
                "PROXY_PROVIDER": "b2proxy",
                "PROXY_SCHEME": "http",
                "PROXY_API_URL": "http://proxy-api.test/gen",
            }
        )
        manager = StickyProxyManager(config, self.state_dir)

        async def fake_resolve_ip(proxy_url: str) -> str:
            self.assertEqual("http://107.151.234.173:10001", proxy_url)
            return "203.0.113.8"

        manager._resolve_ip = fake_resolve_ip  # type: ignore[method-assign]

        with patch("src.sticky_proxy.httpx.Client", FakeSyncClient):
            session = await manager.acquire_session("account-1")
            await manager.release_session(session)

        self.assertEqual(["http://proxy-api.test/gen"], FakeSyncClient.requested_urls)
        self.assertEqual("http://107.151.234.173:10001", session.proxy_url)
        self.assertEqual("203.0.113.8", session.ip_pre)
        self.assertEqual("203.0.113.8", session.ip_post)

    async def test_b2proxy_requires_api_url(self) -> None:
        config = ProxyProviderConfig.from_env(
            {
                "PROXY_PROVIDER": "b2proxy",
                "PROXY_SCHEME": "http",
            }
        )
        manager = StickyProxyManager(config, self.state_dir)

        with self.assertRaisesRegex(ValueError, "PROXY_API_URL"):
            await manager.acquire_session("account-1")

    def test_extract_ip_supports_ipify_json(self) -> None:
        response = httpx.Response(200, json={"ip": "203.0.113.9"})

        self.assertEqual("203.0.113.9", _extract_ip(response))

    def test_extract_ip_supports_plain_text(self) -> None:
        response = httpx.Response(200, text="203.0.113.10\n")

        self.assertEqual("203.0.113.10", _extract_ip(response))


if __name__ == "__main__":
    unittest.main()
