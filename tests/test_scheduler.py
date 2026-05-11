from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.scheduler import MAX_PER_DAY, RegistrationScheduler


class RegistrationSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.state_path = self.root / "scheduler_state.json"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_record_registration_persists_state(self) -> None:
        scheduler = RegistrationScheduler(self.state_path)
        scheduler.record_registration("acct-1")

        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual("acct-1", payload["registrations"][0]["account_id"])
        self.assertEqual("success", payload["registrations"][0]["type"])

        reloaded = RegistrationScheduler(self.state_path)
        self.assertEqual(1, len(reloaded._state["registrations"]))

    def test_daily_slots_are_stable_for_current_day(self) -> None:
        scheduler = RegistrationScheduler(self.state_path)

        first = scheduler.get_daily_slots()
        second = scheduler.get_daily_slots()

        self.assertEqual(first, second)
        self.assertEqual(MAX_PER_DAY, len(first))

    def test_platform_errors_count_toward_daily_limit(self) -> None:
        scheduler = RegistrationScheduler(self.state_path)
        for index in range(MAX_PER_DAY):
            scheduler.record_platform_error(f"acct-{index}")

        self.assertFalse(scheduler.can_register_now())


if __name__ == "__main__":
    unittest.main()
