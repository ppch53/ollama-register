from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from urllib.parse import urlparse
from typing import Any

from src.flaresolverr_client import FlareSolverrClient
from src.mail_parser import extract_verification_code
from src.models import AccountRecord, AppConfig
from src.phone_provider import PhoneActivation, PhoneOtpProvider
from src.playwright_session import PageState, PlaywrightSession
from src.tempmail_client import TempMailClient
from src.turnstile_client import TurnstileClient

SIGNUP_TURNSTILE_SITEKEY = "0x4AAAAAAAMNIvC45A4Wjjln"
STANDARD_CHROME_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
API_KEY_PATTERNS = (
    re.compile(r"\bollama_[A-Za-z0-9._-]{8,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9._-]{8,}\b"),
    re.compile(r"\bok-[A-Za-z0-9._-]{16,}\b"),
    re.compile(r"\b[A-Za-z0-9][A-Za-z0-9._-]{31,}\b"),
)
API_KEY_REJECT_PREFIXES = (
    "account_",
    "auth_",
    "authorization_",
    "client_",
    "env_",
    "model_",
    "session_",
    "user_",
)


def merge_cookies(existing: list[dict[str, Any]], extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for cookie in [*existing, *extra]:
        key = (str(cookie.get("name")), str(cookie.get("domain", "")), str(cookie.get("path", "/")))
        merged[key] = cookie
    return list(merged.values())


def page_looks_like_cloudflare_challenge(url: str, title: str, html: str) -> bool:
    combined = "\n".join([url, title, html]).lower()
    markers = (
        "just a moment",
        "cf-browser-verification",
        "cf_chl_opt",
        "challenge-platform",
        "checking your browser",
        "please enable cookies",
    )
    return any(marker in combined for marker in markers)


def detect_signup_stage(page_state: PageState) -> str:
    visible = "\n".join([page_state.url, page_state.title, page_state.body]).lower()
    html = page_state.html.lower()
    if "access blocked" in visible and "contact support" in visible:
        return "blocked"
    if "/radar-challenge/send" in page_state.url or ('name="local_number"' in html and "verify your phone number" in visible):
        return "phone"
    if any(marker in html for marker in ('autocomplete="one-time-code"', 'name="verification_code"', 'name="code"')):
        return "verification"
    if "/sign-up/password" in page_state.url or 'autocomplete="new-password"' in html:
        return "password"
    if "/sign-up" in page_state.url or 'autocomplete="email"' in html:
        return "email"
    return "unknown"


def ensure_signup_advanced(previous: PageState, current: PageState) -> None:
    previous_stage = detect_signup_stage(previous)
    current_stage = detect_signup_stage(current)
    snippet = " ".join(current.body.split())[:200]
    if current_stage == "blocked":
        raise RuntimeError(f"Sign-up blocked before verification step: {snippet}")
    if current_stage == previous_stage:
        raise RuntimeError(f"Sign-up is still on {current_stage} step after submit: {snippet}")


def phone_number_rate_limited(page_state: PageState) -> bool:
    visible = " ".join(page_state.body.split()).lower()
    return "too many challenges sent for this phone number" in visible


class PhoneNumberRateLimitedError(RuntimeError):
    pass


def build_turnstile_action(page_url: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", urlparse(page_url).path)


def extract_site_key(page_state: PageState) -> str:
    patterns = (
        re.compile(r'siteKey\\\\":\\\\"([^"\\\\]+)\\\\\"'),
        re.compile(r'siteKey":"([^"]+)"'),
        re.compile(r"/(0x[0-9A-Za-z]+)/"),
    )
    haystacks = [page_state.html, *page_state.frames]
    for haystack in haystacks:
        for pattern in patterns:
            match = pattern.search(haystack)
            if match:
                return match.group(1)
    raise RuntimeError("Unable to extract Turnstile sitekey from page")


def iter_api_key_candidates(chunks: list[Any]) -> list[str]:
    seen: set[str] = set()
    candidates: list[str] = []
    for chunk in chunks:
        text = str(chunk).strip()
        if not text:
            continue
        contains_ssh_key = any(marker in text.lower() for marker in ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2"))
        for pattern in API_KEY_PATTERNS:
            for match in pattern.finditer(text):
                candidate = match.group(0)
                normalized = candidate.lower()
                if candidate in seen:
                    continue
                if normalized.startswith(("http://", "https://", "ssh-")):
                    continue
                if contains_ssh_key and candidate.startswith("AAAA"):
                    continue
                if any(normalized.startswith(prefix) for prefix in API_KEY_REJECT_PREFIXES):
                    continue
                if "@" in candidate:
                    continue
                seen.add(candidate)
                candidates.append(candidate)
    return candidates


class BrowserFlow:
    def __init__(
        self,
        config: AppConfig,
        *,
        tempmail_client: TempMailClient,
        turnstile_client: TurnstileClient,
        flaresolverr_client: FlareSolverrClient,
        phone_provider: PhoneOtpProvider | None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.tempmail_client = tempmail_client
        self.turnstile_client = turnstile_client
        self.flaresolverr_client = flaresolverr_client
        self.phone_provider = phone_provider
        self.progress = progress
        self._last_turnstile_debug: dict[str, Any] | None = None

    def run(self, *, email: str, jwt: str, password: str) -> AccountRecord:
        signup_session = self._create_session("signup")
        try:
            self._log(f"[signup] opening sign-up page: {self.config.sign_up_url}")
            signup_session.open(self.config.sign_up_url)
            self._log("[signup] filling email")
            self._fill_textbox(signup_session, "Email", email)
            self._submit_with_turnstile(signup_session)
            self._log("[signup] filling password")
            self._fill_textbox(signup_session, "Password", password)
            self._submit_with_turnstile(signup_session)
            self._log("[mail] waiting for verification email")
            code = self._poll_email_code(jwt)
            self._log("[signup] submitting email verification code")
            self._submit_verification_form(signup_session, code)
            self._handle_phone_challenge(signup_session)
            self._log("[signup] collecting authenticated cookies")
            cookies = self._get_cookies(signup_session)
        finally:
            signup_session.close()

        self._log("[keys] opening API keys page")
        keys_session, merged_cookies = self._open_keys_session(cookies)
        try:
            self._log("[keys] generating api key")
            api_key = self._generate_api_key(keys_session)
            self._log("[keys] api key extracted")
            final_cookies = self._get_cookies(keys_session)
        finally:
            keys_session.close()

        return AccountRecord(
            email=email,
            password=password,
            api_key=api_key,
            cookies=merge_cookies(merged_cookies, final_cookies),
        )

    def _create_session(self, suffix: str, user_agent: str | None = None) -> PlaywrightSession:
        return PlaywrightSession(
            self.config.artifacts_dir,
            headless=self.config.browser_headless,
            proxy_server=self.config.playwright_proxy_server,
            user_agent=user_agent or STANDARD_CHROME_USER_AGENT,
            session_name=f"{suffix}-{int(time.time())}",
        )

    def _fill_textbox(self, session: PlaywrightSession, label: str, value: str) -> None:
        session.fill_textbox(label, value)

    def _submit_with_turnstile(self, session: PlaywrightSession) -> None:
        page_state = session.get_page_state()
        last_error: RuntimeError | None = None
        for attempt in range(3):
            stage = detect_signup_stage(page_state)
            self._log(f"[turnstile] solving challenge for {stage} step (submit attempt {attempt + 1}/3)")
            token = self._solve_turnstile(page_state)
            submit_payload = self._submit_turnstile_form(session, token)
            next_state = self._page_state_from_submit_payload(submit_payload) or session.get_page_state()
            try:
                ensure_signup_advanced(page_state, next_state)
                return
            except RuntimeError as exc:
                last_error = exc
                self._write_debug_artifact(
                    "turnstile-submit",
                    {
                        "submit_attempt": attempt + 1,
                        "error": str(exc),
                        "before": self._page_debug_summary(page_state),
                        "after": self._page_debug_summary(next_state),
                        "turnstile": self._last_turnstile_debug,
                        "submit": self._submit_payload_debug_summary(submit_payload),
                    },
                )
                if detect_signup_stage(next_state) == "blocked" or attempt == 2:
                    raise
                self._log(f"[turnstile] page did not advance after submit: {exc}")
                page_state = next_state
        if last_error is not None:
            raise last_error

    def _submit_verification_form(self, session: PlaywrightSession, code: str) -> None:
        page_state = session.get_page_state()
        self._fill_verification_code(session, code)
        self._submit_current_form(session)
        ensure_signup_advanced(page_state, session.get_page_state())

    def _handle_phone_challenge(self, session: PlaywrightSession) -> None:
        if detect_signup_stage(session.get_page_state()) != "phone":
            self._log("[phone] phone challenge not required")
            return
        provider = self.phone_provider
        if provider is None or not provider.is_configured:
            raise RuntimeError("Phone verification requires a configured HeroSMS provider")
        self._log("[phone] requesting phone number")
        activation = provider.request_number()
        finished = False
        try:
            self._log("[phone] submitting phone number")
            self._submit_phone_number(session, activation)
            self._log("[phone] waiting for sms verification code")
            code = provider.wait_for_code(
                activation.activation_id,
                poll_interval_seconds=self.config.hero_sms_poll_interval_seconds,
                timeout_seconds=self.config.hero_sms_poll_timeout_seconds,
            )
            self._log("[phone] submitting sms verification code")
            self._submit_verification_form(session, code)
            provider.finish_activation(activation.activation_id)
            finished = True
            self._log("[phone] phone verification completed")
        finally:
            if not finished:
                self._safe_cancel_phone_activation(activation.activation_id)

    def _solve_turnstile(self, page_state: PageState) -> str:
        action = build_turnstile_action(page_state.url)
        debug_payload: dict[str, Any] = {
            "page": self._page_debug_summary(page_state),
            "sitekey": SIGNUP_TURNSTILE_SITEKEY,
            "action": action,
            "attempts": [],
        }
        errors: list[str] = []
        for attempt in range(8):
            attempt_payload: dict[str, Any] = {"attempt": attempt + 1}
            debug_payload["attempts"].append(attempt_payload)
            try:
                self._log(f"[turnstile] creating solver task (attempt {attempt + 1}/8)")
                task_id = self.turnstile_client.create_task(
                    page_state.url,
                    sitekey=SIGNUP_TURNSTILE_SITEKEY,
                    action=action,
                )
                attempt_payload["task_id"] = task_id
                self._log(f"[turnstile] waiting for solver result: {task_id}")
                token = self.turnstile_client.wait_for_result(
                    task_id,
                    poll_interval_seconds=self.config.turnstile_poll_interval_seconds,
                    timeout_seconds=self.config.turnstile_poll_timeout_seconds,
                )
                attempt_payload["status"] = "ready"
                attempt_payload["token_prefix"] = token[:16]
                attempt_payload["token_length"] = len(token)
                self._last_turnstile_debug = debug_payload
                self._write_debug_artifact("turnstile-solve", debug_payload)
                self._log("[turnstile] solver returned token")
                return token
            except Exception as exc:  # noqa: BLE001
                attempt_payload["status"] = "error"
                attempt_payload["error"] = str(exc)
                errors.append(str(exc))
                self._log(f"[turnstile] solver attempt failed: {exc}")
        self._last_turnstile_debug = debug_payload
        self._write_debug_artifact("turnstile-solve", debug_payload)
        raise RuntimeError(f"Turnstile solve failed after retries: {errors[-1] if errors else 'unknown error'}")

    def _log(self, message: str) -> None:
        if self.progress is None:
            return
        self.progress(message)

    def _page_debug_summary(self, page_state: PageState | None) -> dict[str, Any] | None:
        if page_state is None:
            return None
        return {
            "url": page_state.url,
            "title": page_state.title,
            "stage": detect_signup_stage(page_state),
            "body_snippet": " ".join(page_state.body.split())[:240],
        }

    def _write_debug_artifact(self, prefix: str, payload: dict[str, Any]) -> None:
        if self.config.artifacts_dir is None:
            return
        self.config.artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = self.config.artifacts_dir / f"{prefix}-{int(time.time())}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _fill_verification_code(self, session: PlaywrightSession, code: str) -> None:
        session.fill_verification_code(code)

    def _submit_phone_number(self, session: PlaywrightSession, activation: PhoneActivation) -> None:
        last_error: RuntimeError | None = None
        for attempt in range(3):
            page_state = session.get_page_state()
            session.fill_phone_number_and_submit(
                local_number=activation.local_number,
                e164_number=activation.e164_number,
                country_code=activation.country_code_value,
            )
            next_state = session.get_page_state()
            try:
                ensure_signup_advanced(page_state, next_state)
                return
            except RuntimeError as exc:
                if phone_number_rate_limited(next_state):
                    raise PhoneNumberRateLimitedError("Too many challenges sent for this phone number") from exc
                last_error = exc
                self._write_debug_artifact(
                    "phone-submit",
                    {
                        "submit_attempt": attempt + 1,
                        "error": str(exc),
                        "activation": {
                            "activation_id": activation.activation_id,
                            "phone_number": activation.phone_number,
                            "e164_number": activation.e164_number,
                            "local_number": activation.local_number,
                            "country_code": activation.country_code_value,
                        },
                        "before": self._page_debug_summary(page_state),
                        "after": self._page_debug_summary(next_state),
                    },
                )
                if attempt == 2 or detect_signup_stage(next_state) == "blocked":
                    raise
                self._log(f"[phone] number submit did not advance, retrying ({attempt + 1}/3): {exc}")
        if last_error is not None:
            raise last_error

    def _fill_phone_number(self, session: PlaywrightSession, activation: PhoneActivation) -> None:
        session.fill_phone_number(
            local_number=activation.local_number,
            e164_number=activation.e164_number,
            country_code=activation.country_code_value,
        )

    def _submit_current_form(self, session: PlaywrightSession) -> None:
        session.submit_current_form()

    def _submit_turnstile_form(self, session: PlaywrightSession, token: str) -> Any:
        return session.submit_turnstile_form(token)

    def _page_state_from_submit_payload(self, payload: Any) -> PageState | None:
        if not isinstance(payload, dict):
            return None
        page = payload.get("page")
        if not isinstance(page, dict):
            return None
        if not all(key in page for key in ("url", "title", "body", "html")):
            return None
        return PageState(
            url=str(page["url"]),
            title=str(page["title"]),
            body=str(page["body"]),
            html=str(page["html"]),
            frames=[],
        )

    def _submit_payload_debug_summary(self, payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        page_state = self._page_state_from_submit_payload(payload)
        return {
            "wait_result": payload.get("waitResult"),
            "event_count": payload.get("eventCount"),
            "events": payload.get("events"),
            "page": self._page_debug_summary(page_state),
        }

    def _poll_email_code(self, jwt: str) -> str:
        deadline = time.monotonic() + self.config.mail_poll_timeout_seconds
        poll_count = 0
        while time.monotonic() < deadline:
            poll_count += 1
            if poll_count == 1 or poll_count % 5 == 0:
                self._log(f"[mail] polling inbox for verification code (poll {poll_count})")
            items = self.tempmail_client.list_mails(jwt)
            for item in sorted(items, key=lambda data: int(data.get("id", data.get("mail_id", 0))), reverse=True):
                raw = item.get("raw")
                if not raw:
                    mail_id = int(item.get("id", item.get("mail_id", 0)))
                    if mail_id <= 0:
                        continue
                    raw = self.tempmail_client.get_mail(jwt, mail_id).raw
                try:
                    code = extract_verification_code(str(raw))
                    self._log("[mail] verification code received")
                    return code
                except ValueError:
                    continue
            time.sleep(self.config.mail_poll_interval_seconds)
        self._log("[mail] verification email wait timed out")
        raise TimeoutError("Timed out waiting for verification email")

    def _safe_cancel_phone_activation(self, activation_id: str) -> None:
        provider = self.phone_provider
        if provider is None:
            return
        try:
            provider.cancel_activation(activation_id)
        except Exception:  # noqa: BLE001
            return

    def _get_cookies(self, session: PlaywrightSession) -> list[dict[str, Any]]:
        return session.get_cookies()

    def _open_keys_session(self, cookies: list[dict[str, Any]]) -> tuple[PlaywrightSession, list[dict[str, Any]]]:
        session = self._create_session("keys")
        session.open("about:blank")
        self._inject_cookies(session, cookies)
        self._goto(session, self.config.settings_keys_url)
        page_state = session.get_page_state()
        if not page_looks_like_cloudflare_challenge(page_state.url, page_state.title, page_state.html):
            return session, cookies

        self._log("[keys] Cloudflare challenge detected, resolving cf_clearance with FlareSolverr")
        session.close()
        merged_cookies, user_agent = self._resolve_clearance(cookies)
        session = self._create_session("keys-clearance", user_agent=user_agent)
        session.open("about:blank")
        self._inject_cookies(session, merged_cookies)
        self._goto(session, self.config.settings_keys_url)
        page_state = session.get_page_state()
        if page_looks_like_cloudflare_challenge(page_state.url, page_state.title, page_state.html):
            raise RuntimeError("Cloudflare challenge still blocks keys page after FlareSolverr fallback")
        self._log("[keys] FlareSolverr clearance applied")
        return session, merged_cookies

    def _resolve_clearance(self, cookies: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
        session_id = self.flaresolverr_client.create_session()
        try:
            solution = self.flaresolverr_client.request_get(
                url=self.config.settings_keys_url,
                session_id=session_id,
                cookies=cookies,
                wait_in_seconds=5,
            )
        finally:
            self.flaresolverr_client.destroy_session(session_id)
        return merge_cookies(cookies, solution.cookies), solution.user_agent

    def _inject_cookies(self, session: PlaywrightSession, cookies: list[dict[str, Any]]) -> None:
        session.add_cookies(cookies)

    def _goto(self, session: PlaywrightSession, url: str) -> None:
        session.goto(url)

    def _generate_api_key(self, session: PlaywrightSession) -> str:
        payload = session.generate_api_key_payload() or {}
        priority_chunks: list[Any] = []
        fallback_chunks: list[Any] = []
        if isinstance(payload, dict):
            priority_chunks.extend(
                [
                    payload.get("clipboard"),
                    *(payload.get("texts") or []),
                    *(payload.get("buttons") or []),
                ]
            )
            fallback_chunks.append(payload.get("body"))
        elif isinstance(payload, list):
            priority_chunks.extend(payload)
        elif payload:
            priority_chunks.append(payload)
        for candidate in [*iter_api_key_candidates(priority_chunks), *iter_api_key_candidates(fallback_chunks)]:
            return candidate
        raise RuntimeError("Unable to extract API key from keys page")
