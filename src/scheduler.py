"""RegistrationScheduler — UTC time-window rate limiting with slot randomization.

Rules:
  - max 5 registrations per UTC calendar day (midnight-to-midnight)
  - >= 30 minutes between consecutive registrations
  - only within 8:00–22:00 UTC
  - at start of each UTC day, pre-generate randomized time slots
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock

from src.utils import atomic_write_json, load_json, utcnow

REG_WINDOW_START_UTC = 8   # 08:00 UTC
REG_WINDOW_END_UTC = 22    # 22:00 UTC
MAX_PER_DAY = 5
MIN_INTERVAL_MINUTES = 30


class RegistrationScheduler:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self.lock = FileLock(str(state_path) + ".lock", timeout=5)
        self._state: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        data = load_json(self.state_path)
        if isinstance(data, dict):
            self._state = data
        else:
            self._state = {
                "registrations": [],
                "slots": {},
            }

    def _save(self) -> None:
        atomic_write_json(self.state_path, self._state)

    def _today_key(self) -> str:
        return utcnow().strftime("%Y-%m-%d")

    def _today_registrations(self) -> list[dict[str, Any]]:
        today = self._today_key()
        return [r for r in self._state.get("registrations", []) if r.get("date") == today]

    def _generate_daily_slots(self) -> list[str]:
        """Generate randomized time slots for today within 8:00–22:00 UTC."""
        today = self._today_key()
        slots = self._state.get("slots", {})
        if slots.get("date") == today and slots.get("times"):
            return slots["times"]

        # generate 5 random times spread across the 14-hour window
        window_minutes = (REG_WINDOW_END_UTC - REG_WINDOW_START_UTC) * 60
        points = sorted(random.sample(range(window_minutes), k=min(MAX_PER_DAY, window_minutes)))

        times = []
        base = utcnow().replace(
            hour=REG_WINDOW_START_UTC, minute=0, second=0, microsecond=0
        )
        for p in points:
            slot_time = base + timedelta(minutes=p)
            times.append(slot_time.strftime("%H:%M"))

        self._state["slots"] = {"date": today, "times": times}
        self._save()
        return times

    def can_register_now(self) -> bool:
        """Check if registration is allowed right now."""
        now = utcnow()

        # check UTC window
        if not (REG_WINDOW_START_UTC <= now.hour < REG_WINDOW_END_UTC):
            return False

        # check daily limit
        if len(self._today_registrations()) >= MAX_PER_DAY:
            return False

        # check interval
        registrations = self._state.get("registrations", [])
        if registrations:
            last_ts = registrations[-1].get("ts", "")
            if last_ts:
                last_time = datetime.fromisoformat(last_ts)
                if (now - last_time).total_seconds() < MIN_INTERVAL_MINUTES * 60:
                    return False

        return True

    def next_available_slot(self) -> datetime:
        """Return the next datetime when registration is allowed."""
        now = utcnow()

        # check if we're before the window
        if now.hour < REG_WINDOW_START_UTC:
            return now.replace(hour=REG_WINDOW_START_UTC, minute=0, second=0, microsecond=0)

        # check daily limit
        if len(self._today_registrations()) >= MAX_PER_DAY:
            # next available is tomorrow at first slot
            tomorrow = now + timedelta(days=1)
            return tomorrow.replace(hour=REG_WINDOW_START_UTC, minute=0, second=0, microsecond=0)

        # check interval from last registration
        registrations = self._state.get("registrations", [])
        if registrations:
            last_ts = registrations[-1].get("ts", "")
            if last_ts:
                last_time = datetime.fromisoformat(last_ts)
                next_time = last_time + timedelta(minutes=MIN_INTERVAL_MINUTES)
                if next_time > now:
                    # also ensure it's within the window
                    if next_time.hour >= REG_WINDOW_END_UTC:
                        tomorrow = now + timedelta(days=1)
                        return tomorrow.replace(
                            hour=REG_WINDOW_START_UTC, minute=0, second=0, microsecond=0
                        )
                    return next_time

        return now

    def record_registration(self, account_id: str) -> None:
        with self.lock:
            self._load()
            self._state.setdefault("registrations", []).append({
                "account_id": account_id,
                "ts": utcnow().isoformat(),
                "date": self._today_key(),
                "type": "success",
            })
            self._save()

    def record_platform_error(self, account_id: str) -> None:
        """Record a platform-touching error (counts toward daily limit)."""
        with self.lock:
            self._load()
            self._state.setdefault("registrations", []).append({
                "account_id": account_id,
                "ts": utcnow().isoformat(),
                "date": self._today_key(),
                "type": "platform_error",
            })
            self._save()

    def get_daily_slots(self) -> list[str]:
        return self._generate_daily_slots()

    async def wait_for_slot(self, poll_interval: float = 30.0) -> None:
        """Block until the next available registration slot."""
        while not self.can_register_now():
            next_slot = self.next_available_slot()
            wait_seconds = (next_slot - utcnow()).total_seconds()
            if wait_seconds <= 0:
                continue
            await asyncio.sleep(min(wait_seconds, poll_interval))
