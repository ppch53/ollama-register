from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from src.proxy_session import ProxyConfig, ProxyConfigError, ProxyDriftError, ProxySessionFactory


class ProxySessionTests(unittest.TestCase):
    def test_disabled_mode_preserves_raw_proxy(self) -> None:
        config = ProxyConfig.from_env(
            {
                "REGISTER_PROXY": "http://user:pass@example.test:8000",
            }
        )

        self.assertFalse(config.enabled)
        self.assertEqual("example.test", config.host)
        self.assertEqual("user", config.username)
        self.assertIsNone(ProxySessionFactory(config).create(session_id="abc"))

    def test_parse_register_proxy_and_generate_smartproxy_url(self) -> None:
        config = ProxyConfig.from_env(
            {
                "OLLAMA_STICKY_PROXY": "1",
                "PROXY_PROVIDER": "smartproxy",
                "REGISTER_PROXY": "http://user:pa%24%24@example.test:8000",
            }
        )

        session = ProxySessionFactory(config).create(session_id="abc123")

        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual("http://user-session-abc123:pa%24%24@example.test:8000", session.proxy_url)
        summary = session.safe_summary()
        self.assertNotIn("pa$$", str(summary))
        self.assertNotIn(session.proxy_url, str(summary))

    def test_rayobyte_uses_explicit_session_template(self) -> None:
        config = ProxyConfig.from_env(
            {
                "OLLAMA_STICKY_PROXY": "1",
                "PROXY_PROVIDER": "rayobyte",
                "PROXY_SCHEME": "http",
                "PROXY_HOST": "la.residential.rayobyte.com",
                "PROXY_PORT": "8000",
                "PROXY_USERNAME": "base-user",
                "PROXY_PASSWORD": "secret",
                "PROXY_SESSION_TEMPLATE": "{username}-session-{session}-country-{country_upper}",
                "PROXY_COUNTRY": "us",
            }
        )

        session = ProxySessionFactory(config).create(session_id="sticky1")

        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(
            "http://base-user-session-sticky1-country-US:secret@la.residential.rayobyte.com:8000",
            session.proxy_url,
        )

    def test_template_required_providers_fail_fast_without_template(self) -> None:
        for provider in ("rayobyte", "oxylabs", "brightdata", "generic"):
            with self.subTest(provider=provider):
                with self.assertRaises(ProxyConfigError):
                    ProxyConfig.from_env(
                        {
                            "OLLAMA_STICKY_PROXY": "1",
                            "PROXY_PROVIDER": provider,
                            "REGISTER_PROXY": "http://user:pass@example.test:8000",
                        }
                    )

    def test_unknown_provider_fails_fast(self) -> None:
        with self.assertRaises(ProxyConfigError):
            ProxyConfig.from_env(
                {
                    "OLLAMA_STICKY_PROXY": "1",
                    "PROXY_PROVIDER": "mysteryproxy",
                    "PROXY_URL_TEMPLATE": "http://{username}:{password}@{host}:{port}",
                }
            )

    def test_iproyal_default_template(self) -> None:
        config = ProxyConfig.from_env(
            {
                "OLLAMA_STICKY_PROXY": "1",
                "PROXY_PROVIDER": "iproyal",
                "REGISTER_PROXY": "socks5://user:pass@example.test:1234",
            }
        )

        session = ProxySessionFactory(config).create(session_id="xyz")

        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual("socks5://user-session-xyz:pass@example.test:1234", session.proxy_url)

    def test_b2proxy_api_extraction_returns_http_proxy_url(self) -> None:
        config = ProxyConfig.from_env(
            {
                "OLLAMA_STICKY_PROXY": "1",
                "PROXY_PROVIDER": "b2proxy",
                "PROXY_SCHEME": "http",
                "PROXY_API_URL": "http://proxy-api.test/gen?foo=bar",
            }
        )

        class FakeApiClient:
            def __init__(self, *args, **kwargs) -> None:
                self.requested_url = None

            def __enter__(self):
                return self

            def __exit__(self, *args) -> None:
                return None

            def get(self, url: str) -> httpx.Response:
                self.requested_url = url
                return httpx.Response(200, text="107.151.234.173:10001\r\n", request=httpx.Request("GET", url))

        with patch("src.proxy_session.httpx.Client", FakeApiClient):
            session = ProxySessionFactory(config).create(session_id="sticky-api")

        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual("http://107.151.234.173:10001", session.proxy_url)

    def test_b2proxy_requires_api_url(self) -> None:
        with self.assertRaises(ProxyConfigError):
            ProxyConfig.from_env(
                {
                    "OLLAMA_STICKY_PROXY": "1",
                    "PROXY_PROVIDER": "b2proxy",
                    "PROXY_SCHEME": "http",
                }
            )

    def test_proxy_drift_raises(self) -> None:
        calls = iter([
            httpx.Response(200, json={"ip": "1.1.1.1"}),
            httpx.Response(200, json={"ip": "2.2.2.2"}),
        ])
        client = httpx.Client(transport=httpx.MockTransport(lambda request: next(calls)))
        config = ProxyConfig.from_env(
            {
                "OLLAMA_STICKY_PROXY": "1",
                "PROXY_PROVIDER": "smartproxy",
                "REGISTER_PROXY": "http://user:pass@example.test:8000",
            }
        )
        session = ProxySessionFactory(config).create(session_id="abc")
        self.assertIsNotNone(session)
        assert session is not None
        session._client = client

        self.assertEqual("1.1.1.1", session.check_ip("pre"))
        with self.assertRaises(ProxyDriftError):
            session.check_ip("mid")
        self.assertTrue(session.drift_detected)


if __name__ == "__main__":
    unittest.main()
