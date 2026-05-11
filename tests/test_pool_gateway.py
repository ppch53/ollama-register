from __future__ import annotations

import json
import importlib.util
import tempfile
import unittest
from pathlib import Path

HAS_QUART = importlib.util.find_spec("quart") is not None


@unittest.skipUnless(HAS_QUART, "quart is not installed in the current test environment")
class PoolGatewayLoaderTests(unittest.TestCase):
    def test_unverified_ollama_accounts_are_skipped_but_legacy_records_still_load(self) -> None:
        import pool_gateway

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            puter_path = root / "puter_accounts.json"
            ollama_path = root / "accounts.json"
            state_path = root / "pool_state.json"

            puter_path.write_text(json.dumps([{"token": "puter-token"}]), encoding="utf-8")
            ollama_path.write_text(
                json.dumps(
                    [
                        {"api_key": "verified-key", "status": "verified"},
                        {"api_key": "legacy-key"},
                        {"api_key": "skip-me", "status": "unverified"},
                    ]
                ),
                encoding="utf-8",
            )
            state_path.write_text("{}", encoding="utf-8")

            old_puter = pool_gateway.PUTER_ACCOUNTS_FILE
            old_ollama = pool_gateway.OLLAMA_ACCOUNTS_FILE
            old_state = pool_gateway.STATE_FILE
            try:
                pool_gateway.PUTER_ACCOUNTS_FILE = str(puter_path)
                pool_gateway.OLLAMA_ACCOUNTS_FILE = str(ollama_path)
                pool_gateway.STATE_FILE = str(state_path)
                gateway = pool_gateway.Gateway()
                gateway._load_state()
                self.assertEqual(["puter-token"], [item.key for item in gateway.pools["puter"].keys])
                self.assertEqual(
                    ["verified-key", "legacy-key"],
                    [item.key for item in gateway.pools["ollama"].keys],
                )
            finally:
                pool_gateway.PUTER_ACCOUNTS_FILE = old_puter
                pool_gateway.OLLAMA_ACCOUNTS_FILE = old_ollama
                pool_gateway.STATE_FILE = old_state


if __name__ == "__main__":
    unittest.main()
