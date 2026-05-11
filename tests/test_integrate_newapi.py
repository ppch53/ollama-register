from __future__ import annotations

import unittest
from argparse import Namespace
from io import StringIO
from unittest.mock import patch

import integrate_newapi


class NewApiIntegrationTests(unittest.TestCase):
    def test_build_gateway_payload_uses_single_openai_compatible_channel(self) -> None:
        payload = integrate_newapi.build_gateway_channel_payload(
            name="pool",
            gateway_key="secret-key",
            gateway_url="http://127.0.0.1:8002/",
            models=["gpt-4o-mini", "gpt-oss:20b"],
            group="default",
            tag="pool",
            test_model="gpt-4o-mini",
        )

        self.assertEqual("single", payload["mode"])
        channel = payload["channel"]
        self.assertEqual("pool", channel["name"])
        self.assertEqual(1, channel["type"])
        self.assertEqual("http://127.0.0.1:8002", channel["base_url"])
        self.assertEqual("secret-key", channel["key"])
        self.assertEqual("gpt-4o-mini,gpt-oss:20b", channel["models"])

    def test_redacted_summary_hides_key_and_collapses_models(self) -> None:
        payload = integrate_newapi.build_gateway_channel_payload(
            name="pool",
            gateway_key="secret-key",
            gateway_url="http://gateway",
            models=["a", "b", "c"],
            group="default",
            tag="pool",
            test_model="a",
        )

        summary = integrate_newapi.redacted_channel_summary(payload)

        self.assertEqual("<redacted>", summary["channel"]["key"])
        self.assertEqual("3 models", summary["channel"]["models"])

    def test_existing_gateway_defaults_to_dry_run(self) -> None:
        args = Namespace(
            gateway_key=None,
            models="gpt-4o-mini",
            channel_name="pool",
            gateway_url="http://gateway",
            group="default",
            tag="pool",
            test_model="gpt-4o-mini",
            yes=False,
            newapi_session=None,
            newapi_url="http://newapi",
            newapi_username=None,
            newapi_password=None,
        )

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("sys.stdout", new=StringIO()) as stdout,
        ):
            rc = integrate_newapi.inject_existing_gateway(args)

        self.assertEqual(0, rc)
        self.assertIn("[dry-run] no changes made", stdout.getvalue())

    def test_session_parser_accepts_json_and_pipe_forms(self) -> None:
        self.assertEqual(
            ("session=abc", "7"),
            integrate_newapi.parse_newapi_session('{"cookie":"session=abc","user_id":7}'),
        )
        self.assertEqual(
            ("session=abc", "7"),
            integrate_newapi.parse_newapi_session("session=abc|7"),
        )


if __name__ == "__main__":
    unittest.main()
