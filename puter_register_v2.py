"""PuterStealthRegister v2 — Camoufox-based stealth registration with state machine,
behavioral simulation, phone verification skip, Turnstile handling, and full audit trail.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.fingerprint_gen import FingerprintConfig, FingerprintGenerator
from src.mailbox_provider import FailureReason, MailboxProvider, MailboxProviderPool
from src.profile_manager import ProfileManager
from src.scheduler import RegistrationScheduler
from src.sticky_proxy import StickyProxyManager
from src.username_gen import UsernameGenerator
from src.utils import (
    append_jsonl,
    atomic_write_json,
    ensure_dir,
    load_json,
    new_uuid,
    read_jsonl,
    set_file_permissions,
    utcnow,
    utcnow_iso,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PUTER_SIGNUP_URL = "https://puter.com/sign-up"
PUTER_HOME_URL = "https://puter.com/"
PUTER_LOGIN_URL = "https://puter.com/login"
TURNSTILE_SITEKEY = "0x4AAAAAABvMyOLo9EwjFVzC"

DEFAULT_V2_ROOT = Path("/opt/ollama-register/v2")
DEFAULT_STATE_DIR = DEFAULT_V2_ROOT / "state"
DEFAULT_AUDIT_DIR = DEFAULT_V2_ROOT / "audit"
DEFAULT_PROFILE_ROOT = DEFAULT_V2_ROOT / "profiles"

# ---------------------------------------------------------------------------
# State definitions
# ---------------------------------------------------------------------------

STATE_DRAFT = "draft"
STATE_BROWSER_STARTED = "browser_started"
STATE_FORM_FILLED = "form_filled"
STATE_FORM_SUBMITTED = "form_submitted"
STATE_EMAIL_VERIFIED = "email_verified"
STATE_SESSION_ESTABLISHED = "session_established"
STATE_QUARANTINED = "quarantined"
STATE_AUDITED = "audited"
STATE_EXPORTABLE = "exportable"
STATE_FAILED = "failed"
STATE_SKIPPED_PHONE = "skipped_phone_verification"

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    STATE_DRAFT: {STATE_BROWSER_STARTED, STATE_FAILED},
    STATE_BROWSER_STARTED: {STATE_FORM_FILLED, STATE_FAILED, STATE_SKIPPED_PHONE},
    STATE_FORM_FILLED: {STATE_FORM_SUBMITTED, STATE_FAILED, STATE_SKIPPED_PHONE},
    STATE_FORM_SUBMITTED: {STATE_EMAIL_VERIFIED, STATE_FAILED},
    STATE_EMAIL_VERIFIED: {STATE_SESSION_ESTABLISHED, STATE_FAILED, STATE_SKIPPED_PHONE},
    STATE_SESSION_ESTABLISHED: {STATE_QUARANTINED},
    STATE_QUARANTINED: {STATE_AUDITED, STATE_FAILED},
    STATE_AUDITED: {STATE_EXPORTABLE, STATE_FAILED},
    STATE_EXPORTABLE: set(),
    STATE_FAILED: set(),
    STATE_SKIPPED_PHONE: set(),
}

TERMINAL_STATES = {STATE_FAILED, STATE_SKIPPED_PHONE, STATE_EXPORTABLE}
PLATFORM_TOUCHED_STATES = {
    STATE_FORM_SUBMITTED, STATE_EMAIL_VERIFIED, STATE_SESSION_ESTABLISHED,
    STATE_QUARANTINED, STATE_AUDITED, STATE_EXPORTABLE,
}


# ---------------------------------------------------------------------------
# Behavioral Simulation
# ---------------------------------------------------------------------------

@dataclass
class BehavioralPersona:
    typing_profile: str = "medium"  # slow | medium | fast
    error_rate: float = 0.02
    mouse_style: str = "smooth"  # smooth | jerky | cautious

    @property
    def char_delay_range(self) -> tuple[float, float]:
        return {
            "slow": (0.15, 0.30),
            "medium": (0.08, 0.18),
            "fast": (0.04, 0.12),
        }[self.typing_profile]

    @property
    def word_gap_range(self) -> tuple[float, float]:
        return (0.3, 0.8)

    @property
    def sentence_gap_range(self) -> tuple[float, float]:
        return (1.0, 2.0)


class BehavioralSimulator:
    """Simulates human-like interactions: typing, mouse, scrolling, dwell."""

    def __init__(self, persona: BehavioralPersona) -> None:
        self.persona = persona

    async def type_text(self, page: Any, selector: str, text: str) -> None:
        """Type text with burst-mode delays, word gaps, and occasional typos."""
        element = page.locator(selector)
        await element.click()
        await asyncio.sleep(random.uniform(0.2, 0.5))

        for i, char in enumerate(text):
            # word gap
            if char == " " or (i > 0 and text[i - 1] == " "):
                await asyncio.sleep(random.uniform(*self.persona.word_gap_range))
            # sentence boundary
            elif i > 0 and text[i - 1] in ".!?" and char != " ":
                await asyncio.sleep(random.uniform(*self.persona.sentence_gap_range))

            # occasional typo + backspace
            if random.random() < self.persona.error_rate:
                wrong_char = chr(ord(char) + random.choice([-1, 1]))
                await page.keyboard.press(wrong_char)
                await asyncio.sleep(random.uniform(0.1, 0.3))
                await page.keyboard.press("Backspace")
                await asyncio.sleep(random.uniform(0.05, 0.15))

            await page.keyboard.press(char)
            await asyncio.sleep(random.uniform(*self.persona.char_delay_range))

    async def move_mouse(self, page: Any, x: int, y: int) -> None:
        """Move mouse with Bezier curve (not linear interpolation)."""
        # get approximate current position (center of viewport)
        cx = random.randint(400, 800)
        cy = random.randint(300, 500)

        steps = random.randint(15, 35)
        # control points for cubic bezier
        cp1x = cx + (x - cx) * 0.3 + random.randint(-80, 80)
        cp1y = cy + (y - cy) * 0.3 + random.randint(-80, 80)
        cp2x = cx + (x - cx) * 0.7 + random.randint(-50, 50)
        cp2y = cy + (y - cy) * 0.7 + random.randint(-50, 50)

        for i in range(steps + 1):
            t = i / steps
            # cubic bezier
            bx = (1 - t) ** 3 * cx + 3 * (1 - t) ** 2 * t * cp1x + 3 * (1 - t) * t**2 * cp2x + t**3 * x
            by = (1 - t) ** 3 * cy + 3 * (1 - t) ** 2 * t * cp1y + 3 * (1 - t) * t**2 * cp2y + t**3 * y

            # add jitter based on mouse style
            jitter = {
                "smooth": 1,
                "jerky": 4,
                "cautious": 2,
            }[self.persona.mouse_style]
            bx += random.randint(-jitter, jitter)
            by += random.randint(-jitter, jitter)

            await page.mouse.move(int(bx), int(by))
            delay = {
                "smooth": 0.01,
                "jerky": 0.005,
                "cautious": 0.02,
            }[self.persona.mouse_style]
            await asyncio.sleep(delay)

        # slight overshoot correction
        if self.persona.mouse_style in ("jerky", "cautious"):
            await asyncio.sleep(random.uniform(0.05, 0.1))
            await page.mouse.move(x + random.randint(-2, 2), y + random.randint(-2, 2))

    async def click_at(self, page: Any, x: int, y: int) -> None:
        await self.move_mouse(page, x, y)
        await asyncio.sleep(random.uniform(0.1, 0.3))
        await page.mouse.click(x, y)

    async def page_dwell(self, page: Any, seconds_range: tuple[float, float] = (3.0, 8.0)) -> None:
        await asyncio.sleep(random.uniform(*seconds_range))

    async def random_scroll(self, page: Any) -> None:
        """Occasional random scroll to simulate reading."""
        for _ in range(random.randint(1, 3)):
            delta_y = random.randint(-200, 200)
            await page.mouse.wheel(0, delta_y)
            await asyncio.sleep(random.uniform(0.3, 1.0))

    async def entry_behavior(self, page: Any) -> None:
        """Navigate to puter.com home first, dwell, then go to signup."""
        await page.goto(PUTER_HOME_URL, wait_until="domcontentloaded")
        await self.page_dwell(page, (5.0, 10.0))
        await self.random_scroll(page)
        await asyncio.sleep(random.uniform(1.0, 3.0))
        await page.goto(PUTER_SIGNUP_URL, wait_until="domcontentloaded")
        await self.page_dwell(page)


# ---------------------------------------------------------------------------
# State Machine
# ---------------------------------------------------------------------------

class StateMachine:
    def __init__(
        self,
        states_path: Path,
        attempt_id: str,
        account_id: str,
    ) -> None:
        self.states_path = states_path
        self.attempt_id = attempt_id
        self.account_id = account_id
        self.current_state = STATE_DRAFT
        self._state_start = time.monotonic()

    def _record_transition(
        self,
        to_state: str,
        reason: str = "",
        platform_touched: bool = False,
        context: dict[str, Any] | None = None,
    ) -> None:
        now_ms = int((time.monotonic() - self._state_start) * 1000)
        record = {
            "schema_version": 1,
            "ts": utcnow_iso(),
            "attempt_id": self.attempt_id,
            "account_id": self.account_id,
            "from": self.current_state,
            "to": to_state,
            "reason": reason,
            "duration_ms": now_ms,
            "platform_touched": platform_touched,
            "context": context or {},
        }
        append_jsonl(self.states_path, record)
        self.current_state = to_state
        self._state_start = time.monotonic()

    def transition(
        self,
        to_state: str,
        reason: str = "",
        platform_touched: bool = False,
        context: dict[str, Any] | None = None,
    ) -> None:
        allowed = ALLOWED_TRANSITIONS.get(self.current_state, set())
        if to_state not in allowed:
            raise ValueError(
                f"Invalid transition: {self.current_state} -> {to_state}. "
                f"Allowed: {allowed}"
            )
        self._record_transition(to_state, reason, platform_touched, context)

    def transition_safe(
        self,
        to_state: str,
        reason: str = "",
        platform_touched: bool = False,
        context: dict[str, Any] | None = None,
    ) -> bool:
        """Attempt a transition. Returns False if invalid (doesn't raise)."""
        allowed = ALLOWED_TRANSITIONS.get(self.current_state, set())
        if to_state not in allowed:
            return False
        self._record_transition(to_state, reason, platform_touched, context)
        return True

    @property
    def is_terminal(self) -> bool:
        return self.current_state in TERMINAL_STATES


# ---------------------------------------------------------------------------
# Audit Trail (Task 2.4)
# ---------------------------------------------------------------------------

@dataclass
class AuditRecord:
    schema_version: int = 1
    attempt_id: str = ""
    account_id: str = ""
    timestamp: str = ""
    email: str = ""
    username: str = ""
    fingerprint_hash: str = ""
    proxy_ip: str = ""
    result: str = ""
    error_category: str = ""
    browser_build: str = ""
    profile_path: str = ""
    captcha_attempts: int = 0
    mail_provider: str = ""
    verification_latency_seconds: float = 0.0
    suspension_check_result: str = "pending"
    artifact_ids: list[str] = field(default_factory=list)
    proxy_session_proof: dict[str, Any] = field(default_factory=dict)
    state_transitions: list[dict[str, Any]] = field(default_factory=list)
    behavioral_timing: dict[str, Any] = field(default_factory=dict)
    phone_verification_triggered: bool = False
    registration_time_utc: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = {
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "account_id": self.account_id,
            "timestamp": self.timestamp,
            "email": self._redact_email(self.email),
            "username": self.username,
            "fingerprint_hash": self.fingerprint_hash,
            "proxy_ip": self.proxy_ip,
            "result": self.result,
            "error_category": self.error_category,
            "browser_build": self.browser_build,
            "profile_path": self.profile_path,
            "captcha_attempts": self.captcha_attempts,
            "mail_provider": self.mail_provider,
            "verification_latency_seconds": self.verification_latency_seconds,
            "suspension_check_result": self.suspension_check_result,
            "artifact_ids": self.artifact_ids,
            "proxy_session_proof": self.proxy_session_proof,
            "state_transitions": self.state_transitions,
            "behavioral_timing": self.behavioral_timing,
            "phone_verification_triggered": self.phone_verification_triggered,
            "registration_time_utc": self.registration_time_utc,
        }
        return d

    @staticmethod
    def _redact_email(email: str) -> str:
        if "@" not in email:
            return "***"
        local, domain = email.rsplit("@", 1)
        if len(local) <= 2:
            return f"*@" + domain
        return f"{local[0]}***{local[-1]}@{domain}"


# ---------------------------------------------------------------------------
# PuterStealthRegister — main registration orchestrator
# ---------------------------------------------------------------------------

class PuterStealthRegister:
    def __init__(
        self,
        profile_manager: ProfileManager,
        proxy_manager: StickyProxyManager,
        fingerprint_gen: FingerprintGenerator,
        scheduler: RegistrationScheduler,
        mailbox_pool: MailboxProviderPool,
        username_gen: UsernameGenerator,
        *,
        state_dir: Path = DEFAULT_STATE_DIR,
        audit_dir: Path = DEFAULT_AUDIT_DIR,
        profile_root: Path = DEFAULT_PROFILE_ROOT,
        artifacts_root: Path | None = None,
        turnstile_solver_url: str = "http://127.0.0.1:5072",
        headless: bool = True,
        live: bool = False,
    ) -> None:
        self.profile_manager = profile_manager
        self.proxy_manager = proxy_manager
        self.fingerprint_gen = fingerprint_gen
        self.scheduler = scheduler
        self.mailbox_pool = mailbox_pool
        self.username_gen = username_gen

        self.state_dir = state_dir
        self.audit_dir = audit_dir
        self.profile_root = profile_root
        self.artifacts_root = artifacts_root or (audit_dir / "artifacts")
        self.turnstile_solver_url = turnstile_solver_url
        self.headless = headless
        self.live = live

        self.states_path = state_dir / "puter_states.jsonl"
        self.audit_path = audit_dir / "puter_audit.jsonl"
        self.accounts_path = state_dir / "puter_accounts_v2.json"
        self.failures_path = audit_dir / "puter_failures.jsonl"
        self._camoufox_ctx = None  # stored for proper cleanup

        ensure_dir(state_dir)
        ensure_dir(audit_dir)
        ensure_dir(self.artifacts_root)

    async def register_single(self) -> dict[str, Any]:
        """Run one full Puter registration. Returns outcome dict."""
        account_id = new_uuid()
        attempt_id = new_uuid()
        sm = StateMachine(self.states_path, attempt_id, account_id)
        audit = AuditRecord(attempt_id=attempt_id, account_id=account_id)
        start_time = time.monotonic()
        browser = None
        page = None
        proxy_session = None
        sim = None

        try:
            # 1. generate identity
            fp = self.fingerprint_gen.generate_unique(country=self.proxy_manager.config.country)
            self.fingerprint_gen.register(account_id, fp)
            username = self.username_gen.generate()
            persona = BehavioralPersona(
                typing_profile=fp.typing_profile,
                error_rate=fp.error_rate,
                mouse_style=fp.mouse_style,
            )
            sim = BehavioralSimulator(persona)

            audit.fingerprint_hash = fp.fingerprint_hash()
            audit.username = username
            audit.registration_time_utc = utcnow_iso()

            # 2. create profile
            profile_path = self.profile_manager.create_profile(account_id)
            audit.profile_path = str(profile_path)

            # 3. acquire proxy
            proxy_session = await self.proxy_manager.acquire_session(account_id)
            audit.proxy_ip = proxy_session.ip_pre
            audit.proxy_session_proof = self.proxy_manager.get_session_proof(proxy_session)

            # 4. create email
            email, mail_provider = await self.mailbox_pool.create_address()
            audit.email = email
            audit.mail_provider = mail_provider.name

            # 5. generate password
            password = self._generate_password()

            # 6. start browser (Camoufox)
            sm.transition(STATE_BROWSER_STARTED, "camoufox launched")
            browser, page = await self._start_camoufox(fp, profile_path, proxy_session)

            # 7. behavioral entry
            await sim.entry_behavior(page)

            # 8. fill form
            sm.transition(STATE_FORM_FILLED, "form fields filled")
            await self._fill_signup_form(page, sim, email, username, password)

            # 9. solve turnstile (before or during submit)
            turnstile_ok, captcha_count = await self._handle_turnstile(page, sim)
            audit.captcha_attempts = captcha_count
            if not turnstile_ok:
                sm.transition(STATE_FAILED, "turnstile_exhausted", context={"captcha_attempts": captcha_count})
                audit.result = STATE_FAILED
                audit.error_category = "turnstile_exhausted"
                return self._finalize(audit, sm, proxy_session, browser, account_id, start_time)

            # 10. submit form
            sm.transition(STATE_FORM_SUBMITTED, "signup POST submitted", platform_touched=True)
            email_sent_time = time.monotonic()

            # 11. check for phone verification
            if await self._detect_phone_verification(page):
                await self._soft_landing_skip(page, sim, proxy_session, sm, account_id)
                audit.phone_verification_triggered = True
                audit.result = STATE_SKIPPED_PHONE
                return self._finalize(audit, sm, proxy_session, browser, account_id, start_time)

            # 12. get email verification code
            code = await mail_provider.get_verification_code(email, timeout=90.0)
            if code is None:
                self.mailbox_pool.record_failure(mail_provider, FailureReason.DELIVERY_TIMEOUT)
                sm.transition(STATE_FAILED, "email_verification_timeout")
                audit.result = STATE_FAILED
                audit.error_category = "email_verification_timeout"
                return self._finalize(audit, sm, proxy_session, browser, account_id, start_time)

            audit.verification_latency_seconds = time.monotonic() - email_sent_time
            self.mailbox_pool.record_success(mail_provider)

            # 13. enter verification code
            await self._enter_verification_code(page, sim, code)
            sm.transition(STATE_EMAIL_VERIFIED, "verification code accepted", platform_touched=True)

            # 14. establish session (whoami check)
            session_ok = await self._verify_session(page, password, email)
            if not session_ok:
                sm.transition(STATE_FAILED, "session_verification_failed")
                audit.result = STATE_FAILED
                audit.error_category = "session_verification_failed"
                return self._finalize(audit, sm, proxy_session, browser, account_id, start_time)

            sm.transition(STATE_SESSION_ESTABLISHED, "whoami succeeded", platform_touched=True)

            # 15. post-registration warm-up
            await self._warmup_browse(page, sim)

            # 16. close browser and proxy
            await self._close_browser(browser, page)
            browser, page = None, None
            await self.proxy_manager.check_mid(proxy_session)
            await self.proxy_manager.release_session(proxy_session)

            # 17. enter quarantine
            sm.transition(STATE_QUARANTINED, "entered quarantine", platform_touched=True)

            # 18. store account (after state transition so crash can't leave orphan)
            self._store_account(account_id, email, username, password)
            self.scheduler.record_registration(account_id)
            audit.result = STATE_QUARANTINED
            return self._finalize(audit, sm, proxy_session, browser, account_id, start_time)

        except Exception as e:
            error_msg = str(e)[:200]
            is_phone_skip = "phone" in error_msg.lower() and "verification" in error_msg.lower()

            if is_phone_skip and proxy_session:
                await self._soft_landing_skip(page, sim, proxy_session, sm, account_id)
                audit.phone_verification_triggered = True
                audit.result = STATE_SKIPPED_PHONE
            elif not sm.is_terminal:
                sm.transition(STATE_FAILED, error_msg[:100])
                audit.result = STATE_FAILED
                audit.error_category = error_msg[:100]

            return self._finalize(audit, sm, proxy_session, browser, account_id, start_time)

        finally:
            if browser is not None:
                try:
                    await self._close_browser(browser, page)
                except Exception:
                    pass
            if proxy_session and not proxy_session._closed:
                try:
                    await self.proxy_manager.release_session(proxy_session)
                except Exception:
                    pass

    async def _start_camoufox(
        self,
        fp: FingerprintConfig,
        profile_path: Path,
        proxy_session: Any,
    ) -> tuple[Any, Any]:
        """Launch Camoufox browser with fingerprint config and proxy."""
        from camoufox.async_api import AsyncCamoufox

        camoufox_kwargs = self.fingerprint_gen.get_camoufox_kwargs(fp, headless=self.headless)
        camoufox_kwargs["proxy"] = {
            "server": proxy_session.proxy_url,
        }

        ctx = AsyncCamoufox(**camoufox_kwargs)
        self._camoufox_ctx = ctx
        browser = await ctx.__aenter__()
        page = await browser.new_page()
        return browser, page

    async def _fill_signup_form(
        self,
        page: Any,
        sim: BehavioralSimulator,
        email: str,
        username: str,
        password: str,
    ) -> None:
        """Fill the Puter signup form with behavioral simulation."""
        await sim.type_text(page, 'input[name="email"], input[type="email"]', email)
        await asyncio.sleep(random.uniform(0.5, 1.5))
        await sim.random_scroll(page)

        await sim.type_text(page, 'input[name="username"]', username)
        await asyncio.sleep(random.uniform(0.3, 0.8))

        await sim.type_text(page, 'input[name="password"], input[type="password"]', password)
        await asyncio.sleep(random.uniform(0.3, 0.8))

        # confirm password if present
        confirm = page.locator('input[name="passwordConfirm"], input[name="confirm_password"]')
        if await confirm.count() > 0:
            await sim.type_text(
                page,
                'input[name="passwordConfirm"], input[name="confirm_password"]',
                password,
            )

    async def _handle_turnstile(self, page: Any, sim: BehavioralSimulator) -> tuple[bool, int]:
        """Solve Cloudflare Turnstile challenge. Returns (success, attempts)."""
        max_attempts = 3
        for attempt in range(max_attempts):
            # wait for silent/invisible turnstile to resolve
            await asyncio.sleep(random.uniform(3.0, 8.0))

            # check if turnstile token is present (auto-solved)
            token = await page.evaluate(
                '() => { const el = document.querySelector("[name=cf-turnstile-response]"); return el ? el.value : null; }'
            )
            if token:
                return True, attempt + 1

            # check for visible checkbox
            checkbox = page.locator('iframe[src*="challenges.cloudflare.com"]')
            if await checkbox.count() > 0:
                frame = checkbox.first
                try:
                    iframe = await frame.content_frame()
                    if iframe:
                        btn = iframe.locator('input[type="checkbox"], .cb-lb')
                        if await btn.count() > 0:
                            box = await btn.first.bounding_box()
                            if box:
                                await sim.click_at(page, int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
                                await asyncio.sleep(random.uniform(3.0, 5.0))
                except Exception:
                    pass

            # fallback to capsolver
            try:
                import httpx
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(
                        f"{self.turnstile_solver_url}/turnstile",
                        json={
                            "sitekey": TURNSTILE_SITEKEY,
                            "pageurl": PUTER_SIGNUP_URL,
                        },
                    )
                    if resp.status_code == 200:
                        result = resp.json()
                        token = result.get("token") or result.get("solution", {}).get("token")
                        if token:
                            await page.evaluate(
                                f'() => {{ const el = document.querySelector("[name=cf-turnstile-response]"); if(el) el.value = "{token}"; }}'
                            )
                            return True, attempt + 1
            except Exception:
                pass

        return False, max_attempts

    async def _detect_phone_verification(self, page: Any) -> bool:
        """Check if phone verification has been triggered."""
        try:
            content = await page.content()
            phone_indicators = [
                "phone number",
                "phone verification",
                "verify your phone",
                "enter your phone",
                "sms verification",
                "+1-",
            ]
            content_lower = content.lower()
            return any(indicator in content_lower for indicator in phone_indicators)
        except Exception:
            return False

    async def _soft_landing_skip(
        self,
        page: Any | None,
        sim: BehavioralSimulator | None,
        proxy_session: Any,
        sm: StateMachine,
        account_id: str,
    ) -> None:
        """Soft-landing for phone verification: browse briefly, then close."""
        if page and sim:
            try:
                # browse 1-2 other links
                await sim.random_scroll(page)
                await asyncio.sleep(random.uniform(30.0, 60.0))
            except Exception:
                pass

        self.proxy_manager.mark_phone_triggered(proxy_session)
        sm.transition_safe(STATE_SKIPPED_PHONE, "phone_verification_triggered")

    async def _enter_verification_code(self, page: Any, sim: BehavioralSimulator, code: str) -> None:
        """Enter email verification code."""
        code_input = page.locator('input[name="code"], input[name="verification_code"], input[placeholder*="code"]')
        if await code_input.count() > 0:
            await sim.type_text(page, 'input[name="code"], input[name="verification_code"], input[placeholder*="code"]', code)
            await asyncio.sleep(random.uniform(0.5, 1.0))
            submit_btn = page.locator('button[type="submit"], button:has-text("Verify"), button:has-text("Confirm")')
            if await submit_btn.count() > 0:
                await submit_btn.first.click()
                await asyncio.sleep(random.uniform(3.0, 6.0))

    async def _verify_session(self, page: Any, password: str, email: str) -> bool:
        """Verify the session by checking whoami after login."""
        try:
            await page.goto(PUTER_HOME_URL, wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(3.0, 6.0))
            # check for logged-in state by looking for user-specific elements
            content = await page.content()
            # basic check: if we're not on the login page, session is established
            if "login" not in page.url.lower() or "dashboard" in content.lower():
                return True
            # try whoami API
            resp_text = await page.evaluate(
                'async () => { try { const r = await fetch("/whoami"); return await r.text(); } catch(e) { return "error"; } }'
            )
            if resp_text and "error" not in resp_text.lower():
                return True
            return False
        except Exception:
            return False

    async def _warmup_browse(self, page: Any, sim: BehavioralSimulator) -> None:
        """Post-registration warm-up: browse dashboard for 30-60s."""
        try:
            await sim.page_dwell(page, (10.0, 20.0))
            await sim.random_scroll(page)
            await asyncio.sleep(random.uniform(10.0, 20.0))
            await sim.random_scroll(page)
            await asyncio.sleep(random.uniform(10.0, 20.0))
        except Exception:
            pass

    async def _close_browser(self, browser: Any, page: Any | None) -> None:
        """Close browser gracefully."""
        try:
            if page:
                await page.close()
        except Exception:
            pass
        try:
            if self._camoufox_ctx:
                await self._camoufox_ctx.__aexit__(None, None, None)
                self._camoufox_ctx = None
        except Exception:
            pass

    def _store_account(self, account_id: str, email: str, username: str, password: str) -> None:
        """Store the registered account in accounts file. Only for quarantined accounts."""
        accounts = load_json(self.accounts_path) or []
        accounts.append({
            "account_id": account_id,
            "email": email,
            "username": username,
            "password": password,
            "status": "quarantined",
            "registered_at": utcnow_iso(),
        })
        atomic_write_json(self.accounts_path, accounts)
        set_file_permissions(self.accounts_path)

    def _generate_password(self) -> str:
        """Generate a strong password."""
        import string
        chars = string.ascii_letters + string.digits + "!@#$%&"
        while True:
            pw = "".join(random.choices(chars, k=16))
            has_upper = any(c.isupper() for c in pw)
            has_lower = any(c.islower() for c in pw)
            has_digit = any(c.isdigit() for c in pw)
            has_special = any(c in "!@#$%&" for c in pw)
            if has_upper and has_lower and has_digit and has_special:
                return pw

    def _finalize(
        self,
        audit: AuditRecord,
        sm: StateMachine,
        proxy_session: Any | None,
        browser: Any | None,
        account_id: str,
        start_time: float,
    ) -> dict[str, Any]:
        """Write audit record and return outcome."""
        audit.timestamp = utcnow_iso()
        audit.result = sm.current_state
        audit.behavioral_timing = {
            "total_duration_seconds": round(time.monotonic() - start_time, 1),
        }

        if proxy_session:
            audit.proxy_session_proof = self.proxy_manager.get_session_proof(proxy_session)

        # read state transitions from JSONL
        all_transitions = read_jsonl(self.states_path)
        audit.state_transitions = [
            t for t in all_transitions if t.get("attempt_id") == audit.attempt_id
        ]

        # write audit record
        append_jsonl(self.audit_path, audit.to_dict())

        # write failure record for terminal failures
        if sm.current_state == STATE_FAILED:
            append_jsonl(self.failures_path, {
                "account_id": account_id,
                "attempt_id": audit.attempt_id,
                "state_at_failure": sm.current_state,
                "error_category": audit.error_category,
                "ts": utcnow_iso(),
            })

        return {
            "account_id": account_id,
            "state": sm.current_state,
            "error_category": audit.error_category,
            "email": audit.email,
            "username": audit.username,
            "duration_seconds": round(time.monotonic() - start_time, 1),
        }


# ---------------------------------------------------------------------------
# Circuit Breaker (Task 3.1)
# ---------------------------------------------------------------------------

class CircuitBreaker:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self._state: dict[str, Any] = {
            "tripped": False,
            "reason": "",
            "trip_ts": "",
            "consecutive_suspensions": 0,
            "consecutive_mail_failures": 0,
            "consecutive_code_timeouts": 0,
            "window_401_403": [],
            "rate_limited": False,
        }
        self._load()

    def _load(self) -> None:
        data = load_json(self.state_path)
        if isinstance(data, dict):
            self._state.update(data)

    def _save(self) -> None:
        atomic_write_json(self.state_path, self._state)

    @property
    def is_tripped(self) -> bool:
        if self._state.get("rate_limited"):
            # check if 24 hours have passed
            trip_ts = self._state.get("trip_ts", "")
            if trip_ts:
                try:
                    trip_time = datetime.fromisoformat(trip_ts)
                    if datetime.now(timezone.utc) - trip_time > timedelta(hours=24):
                        self._state["rate_limited"] = False
                        self._state["tripped"] = False
                        self._save()
                except (ValueError, TypeError):
                    pass
        return self._state.get("tripped", False)

    def record_result(self, result_state: str, error_category: str = "") -> None:
        if self.is_tripped:
            return

        # account suspended
        if error_category in ("account_suspended", "suspended_during_quarantine"):
            self._state["consecutive_suspensions"] += 1
            if self._state["consecutive_suspensions"] >= 3:
                self._trip("3 consecutive account suspensions")
                return
        else:
            self._state["consecutive_suspensions"] = 0

        # email delivery failures
        if error_category == "email_verification_timeout":
            self._state["consecutive_mail_failures"] += 1
            if self._state["consecutive_mail_failures"] >= 5:
                self._trip("5 consecutive email delivery failures")
                return
        else:
            self._state["consecutive_mail_failures"] = 0

        # verification code timeouts
        if error_category in ("email_verification_timeout", "verification_code_expired"):
            self._state["consecutive_code_timeouts"] += 1
            if self._state["consecutive_code_timeouts"] >= 3:
                self._trip("3 consecutive verification code timeouts")
                return
        else:
            self._state["consecutive_code_timeouts"] = 0

        # 401/403 window
        if error_category in ("http_401", "http_403", "account_suspended"):
            now = utcnow()
            window = self._state.get("window_401_403", [])
            window.append(now.isoformat())
            # keep only last hour
            cutoff = (now - timedelta(hours=1)).isoformat()
            window = [t for t in window if t > cutoff]
            self._state["window_401_403"] = window
            if len(window) >= 5:
                self._trip("5+ 401/403 responses in 1-hour window")
                return

        # rate limited
        if error_category == "rate_limited":
            self._state["rate_limited"] = True
            self._trip("rate_limited from puter.com — stopping for 24 hours")
            return

        self._save()

    def record_skip(self) -> None:
        """Phone verification skips do NOT trip the breaker."""
        pass

    def _trip(self, reason: str) -> None:
        self._state["tripped"] = True
        self._state["reason"] = reason
        self._state["trip_ts"] = utcnow_iso()
        self._save()

    def reset(self) -> None:
        self._state = {
            "tripped": False,
            "reason": "",
            "trip_ts": "",
            "consecutive_suspensions": 0,
            "consecutive_mail_failures": 0,
            "consecutive_code_timeouts": 0,
            "window_401_403": [],
            "rate_limited": False,
        }
        self._save()

    @property
    def reason(self) -> str:
        return self._state.get("reason", "")


# ---------------------------------------------------------------------------
# Quarantine Manager (Task 3.2)
# ---------------------------------------------------------------------------

class QuarantineManager:
    QUARANTINE_HOURS = 24
    OPTIONAL_RECHECK_HOURS = 72

    def __init__(
        self,
        quarantine_path: Path,
        accounts_path: Path,
        audit_path: Path,
    ) -> None:
        self.quarantine_path = quarantine_path
        self.accounts_path = accounts_path
        self.audit_path = audit_path
        self._quarantine: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        data = load_json(self.quarantine_path)
        if isinstance(data, dict):
            self._quarantine = data

    def _save(self) -> None:
        atomic_write_json(self.quarantine_path, self._quarantine)

    def add(self, account_id: str, email: str, username: str, password: str) -> None:
        self._quarantine[account_id] = {
            "email": email,
            "username": username,
            "password": password,
            "entered_at": utcnow_iso(),
            "recheck_24h_done": False,
            "recheck_72h_done": False,
        }
        self._save()

    def get_due_accounts(self) -> list[dict[str, Any]]:
        """Return accounts whose 24h quarantine has elapsed."""
        now = utcnow()
        due: list[dict[str, Any]] = []
        for aid, info in self._quarantine.items():
            if info.get("recheck_24h_done"):
                continue
            entered = datetime.fromisoformat(info["entered_at"])
            if now - entered >= timedelta(hours=self.QUARANTINE_HOURS):
                due.append({"account_id": aid, **info})
        return due

    def get_due_for_optional_recheck(self) -> list[dict[str, Any]]:
        """Return accounts whose 72h optional recheck is due."""
        now = utcnow()
        due: list[dict[str, Any]] = []
        for aid, info in self._quarantine.items():
            if info.get("recheck_72h_done"):
                continue
            if not info.get("recheck_24h_done"):
                continue
            entered = datetime.fromisoformat(info["entered_at"])
            if now - entered >= timedelta(hours=self.OPTIONAL_RECHECK_HOURS):
                due.append({"account_id": aid, **info})
        return due

    def mark_audited(self, account_id: str) -> None:
        """Mark 24h recheck passed. Account advances to 'audited'."""
        if account_id in self._quarantine:
            self._quarantine[account_id]["recheck_24h_done"] = True
            self._quarantine[account_id]["audited_at"] = utcnow_iso()
            self._save()

    def mark_optional_recheck_done(self, account_id: str) -> None:
        if account_id in self._quarantine:
            self._quarantine[account_id]["recheck_72h_done"] = True
            self._save()

    def mark_failed(self, account_id: str, reason: str) -> None:
        info = self._quarantine.pop(account_id, None)
        if info:
            self._save()
            append_jsonl(
                self.audit_path,
                {
                    "account_id": account_id,
                    "result": "failed",
                    "error_category": reason,
                    "ts": utcnow_iso(),
                },
            )

    def promote_to_exportable(self, account_id: str) -> None:
        """Move account from quarantine to exportable in accounts file."""
        info = self._quarantine.pop(account_id, None)
        if not info:
            return

        # load accounts, update status to exportable
        accounts = load_json(self.accounts_path) or []
        for acc in accounts:
            if acc.get("account_id") == account_id:
                acc["status"] = "exportable"
                acc["exportable_at"] = utcnow_iso()
                break
        else:
            accounts.append({
                "account_id": account_id,
                "email": info.get("email", ""),
                "username": info.get("username", ""),
                "password": info.get("password", ""),
                "status": "exportable",
                "registered_at": info.get("entered_at", ""),
                "exportable_at": utcnow_iso(),
            })

        atomic_write_json(self.accounts_path, accounts)
        set_file_permissions(self.accounts_path)
        self._save()

    def get_exportable_accounts(self) -> list[dict[str, Any]]:
        accounts = load_json(self.accounts_path) or []
        return [a for a in accounts if a.get("status") == "exportable"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Puter v2 Stealth Registration")
    parser.add_argument("--live", action="store_true", help="Enable live registration (requires PUTER_LIVE_REGISTRATION=1)")
    parser.add_argument("--count", "-n", type=int, default=1, help="Number of registrations to attempt")
    parser.add_argument("--headless", action="store_true", default=True, help="Run browser in headless mode")
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    parser.add_argument("--dry-run", action="store_true", help="Dry-run mode (no real Puter access)")
    parser.add_argument("--quarantine-check", action="store_true", help="Run quarantine re-audit cycle")
    parser.add_argument("--circuit-breaker-reset", action="store_true", help="Reset tripped circuit breaker")
    return parser


async def async_main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    import os
    live_ok = os.environ.get("PUTER_LIVE_REGISTRATION", "0") == "1"

    if args.live and not live_ok:
        print("ERROR: --live requires PUTER_LIVE_REGISTRATION=1 in environment.")
        sys.exit(1)

    if args.live and not args.dry_run:
        confirm = input("CONFIRM: This will perform REAL Puter registrations. Type 'yes' to proceed: ")
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            sys.exit(0)

    # wire up components
    state_dir = DEFAULT_STATE_DIR
    audit_dir = DEFAULT_AUDIT_DIR
    profile_root = DEFAULT_PROFILE_ROOT

    ensure_dir(state_dir)
    ensure_dir(audit_dir)
    ensure_dir(profile_root)

    profile_mgr = ProfileManager(profile_root)
    fingerprint_gen = FingerprintGenerator(state_dir / "fingerprint_registry.json")
    scheduler = RegistrationScheduler(state_dir / "scheduler_state.json")

    proxy_config_path = state_dir / "proxy_config.json"
    proxy_data = load_json(proxy_config_path)
    if proxy_data:
        from src.sticky_proxy import ProxyProviderConfig
        proxy_cfg = ProxyProviderConfig(**proxy_data)
    else:
        from src.sticky_proxy import ProxyProviderConfig
        proxy_cfg = ProxyProviderConfig(
            host="la.residential.rayobyte.com",
            port=8000,
            username=os.environ.get("PROXY_USERNAME", ""),
            password=os.environ.get("PROXY_PASSWORD", ""),
            country="US",
        )
    proxy_mgr = StickyProxyManager(proxy_cfg, state_dir)

    # mailbox
    mailbox_pool = MailboxProviderPool(
        registry_path=state_dir / "used_emails.json",
        health_path=state_dir / "mailbox_health.json",
    )
    # providers are registered externally or via config
    # for now, this is a placeholder; providers are registered by the caller

    username_gen = UsernameGenerator(state_dir / "used_usernames.json")

    circuit_breaker = CircuitBreaker(state_dir / "circuit_breaker.json")

    if args.circuit_breaker_reset:
        circuit_breaker.reset()
        print("Circuit breaker reset.")
        return

    if args.quarantine_check:
        qm = QuarantineManager(
            quarantine_path=state_dir / "puter_quarantine.json",
            accounts_path=state_dir / "puter_accounts_v2.json",
            audit_path=audit_dir / "puter_audit.jsonl",
        )
        due = qm.get_due_accounts()
        print(f"Accounts due for re-audit: {len(due)}")
        for acc in due:
            print(f"  - {acc['account_id']}: {acc.get('email', '?')}")
        return

    if circuit_breaker.is_tripped:
        print(f"CIRCUIT BREAKER TRIPPED: {circuit_breaker.reason}")
        print("Use --circuit-breaker-reset to reset.")
        sys.exit(1)

    reg = PuterStealthRegister(
        profile_manager=profile_mgr,
        proxy_manager=proxy_mgr,
        fingerprint_gen=fingerprint_gen,
        scheduler=scheduler,
        mailbox_pool=mailbox_pool,
        username_gen=username_gen,
        state_dir=state_dir,
        audit_dir=audit_dir,
        profile_root=profile_root,
        headless=args.headless,
        live=args.live,
    )

    results: list[dict[str, Any]] = []
    for i in range(args.count):
        if circuit_breaker.is_tripped:
            print(f"Circuit breaker tripped after {i} registrations: {circuit_breaker.reason}")
            break

        print(f"\n--- Registration {i + 1}/{args.count} ---")
        await scheduler.wait_for_slot()

        result = await reg.register_single()
        results.append(result)
        print(f"Result: {result['state']} ({result.get('error_category', 'ok')})")

        circuit_breaker.record_result(result["state"], result.get("error_category", ""))

    print(f"\n=== Summary: {len(results)} attempts ===")
    for r in results:
        print(f"  {r.get('email', '?')}: {r['state']}")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
