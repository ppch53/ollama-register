from __future__ import annotations

import inspect
import unittest

import puter_register_v2


class CliEntrypointTests(unittest.TestCase):
    def test_puter_v2_console_entrypoint_is_sync_wrapper(self) -> None:
        self.assertFalse(inspect.iscoroutinefunction(puter_register_v2.main))
        self.assertTrue(inspect.iscoroutinefunction(puter_register_v2.async_main))


if __name__ == "__main__":
    unittest.main()
