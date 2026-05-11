"""PreflightChecker — validates environment before any registration attempt."""
from __future__ import annotations

import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from src.utils import utcnow


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


class PreflightError(Exception):
    def __init__(self, failures: list[CheckResult]) -> None:
        self.failures = failures
        lines = ["Preflight checks failed:"]
        for r in failures:
            lines.append(f"  FAIL [{r.name}]: {r.detail}")
        super().__init__("\n".join(lines))


class PreflightChecker:
    def __init__(
        self,
        profile_root: Path,
        state_dir: Path,
        proxy_host: str | None = None,
        proxy_port: int | None = None,
        tempmail_api_key: str | None = None,
        outlook_pool_file: Path | None = None,
        min_disk_mb: int = 1024,
        lock_path: Path | None = None,
    ) -> None:
        self.profile_root = profile_root
        self.state_dir = state_dir
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.tempmail_api_key = tempmail_api_key
        self.outlook_pool_file = outlook_pool_file
        self.min_disk_mb = min_disk_mb
        self.lock_path = lock_path or (state_dir / ".preflight.lock")

    def run_all(self) -> list[CheckResult]:
        checks = [
            self._check_python_version(),
            self._check_camoufox(),
            self._check_profile_dir(),
            self._check_state_dir(),
            self._check_disk_space(),
            self._check_proxy_config(),
            self._check_mailbox_config(),
            self._check_file_lock(),
            self._check_singleton(),
        ]
        return checks

    def run_or_fail(self) -> list[CheckResult]:
        results = self.run_all()
        failures = [r for r in results if not r.ok]
        if failures:
            raise PreflightError(failures)
        return results

    def _check_python_version(self) -> CheckResult:
        v = sys.version_info
        ok = v >= (3, 11)
        return CheckResult(
            name="python_version",
            ok=ok,
            detail=f"{v.major}.{v.minor}.{v.micro}",
        )

    def _check_camoufox(self) -> CheckResult:
        try:
            import camoufox  # noqa: F401
            return CheckResult(name="camoufox_import", ok=True, detail="import ok")
        except ImportError as e:
            return CheckResult(name="camoufox_import", ok=False, detail=str(e))

    def _check_profile_dir(self) -> CheckResult:
        try:
            self.profile_root.mkdir(parents=True, exist_ok=True)
            test_file = self.profile_root / ".preflight_write_test"
            test_file.write_text("test", encoding="utf-8")
            test_file.unlink()
            return CheckResult(
                name="profile_dir",
                ok=True,
                detail=str(self.profile_root),
            )
        except OSError as e:
            return CheckResult(name="profile_dir", ok=False, detail=str(e))

    def _check_state_dir(self) -> CheckResult:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            test_file = self.state_dir / ".preflight_write_test"
            test_file.write_text("test", encoding="utf-8")
            test_file.unlink()
            return CheckResult(
                name="state_dir",
                ok=True,
                detail=str(self.state_dir),
            )
        except OSError as e:
            return CheckResult(name="state_dir", ok=False, detail=str(e))

    def _check_disk_space(self) -> CheckResult:
        try:
            usage = shutil.disk_usage(str(self.profile_root.parent))
            free_mb = usage.free / (1024 * 1024)
            ok = free_mb >= self.min_disk_mb
            return CheckResult(
                name="disk_space",
                ok=ok,
                detail=f"{free_mb:.0f} MB free (need {self.min_disk_mb} MB)",
            )
        except OSError as e:
            return CheckResult(name="disk_space", ok=False, detail=str(e))

    def _check_proxy_config(self) -> CheckResult:
        if not self.proxy_host or not self.proxy_port:
            return CheckResult(
                name="proxy_config",
                ok=False,
                detail="proxy host/port not configured",
            )
        return CheckResult(
            name="proxy_config",
            ok=True,
            detail=f"{self.proxy_host}:{self.proxy_port}",
        )

    def _check_mailbox_config(self) -> CheckResult:
        has_tempmail = bool(self.tempmail_api_key)
        has_outlook = bool(
            self.outlook_pool_file and self.outlook_pool_file.exists()
        )
        if not has_tempmail and not has_outlook:
            return CheckResult(
                name="mailbox_config",
                ok=False,
                detail="neither TempMail API key nor Outlook pool file configured",
            )
        providers: list[str] = []
        if has_tempmail:
            providers.append("tempmail")
        if has_outlook:
            providers.append("outlook")
        return CheckResult(
            name="mailbox_config",
            ok=True,
            detail=", ".join(providers),
        )

    def _check_file_lock(self) -> CheckResult:
        try:
            from filelock import FileLock

            test_lock = FileLock(str(self.lock_path) + ".test")
            with test_lock:
                pass
            return CheckResult(name="file_lock", ok=True, detail="ok")
        except Exception as e:
            return CheckResult(name="file_lock", ok=False, detail=str(e))

    def _check_singleton(self) -> CheckResult:
        singleton_lock = self.state_dir / ".singleton.lock"
        try:
            from filelock import FileLock

            lock = FileLock(str(singleton_lock), timeout=0)
            lock.acquire()
            # store on self so caller can release later
            self._singleton_lock = lock
            return CheckResult(name="singleton", ok=True, detail="lock acquired")
        except Exception:
            return CheckResult(
                name="singleton",
                ok=False,
                detail="another instance is already running",
            )


def format_report(results: list[CheckResult]) -> str:
    lines: list[str] = []
    all_ok = True
    for r in results:
        status = "OK" if r.ok else "FAIL"
        if not r.ok:
            all_ok = False
        lines.append(f"  [{status}] {r.name}: {r.detail}")
    header = "Preflight: ALL CHECKS PASSED" if all_ok else "Preflight: SOME CHECKS FAILED"
    return header + "\n" + "\n".join(lines)
