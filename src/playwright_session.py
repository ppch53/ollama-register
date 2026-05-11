from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

DEFAULT_ACTION_TIMEOUT_MS = 10_000
DEFAULT_NAVIGATION_TIMEOUT_MS = 60_000
NETWORK_IDLE_TIMEOUT_MS = 15_000
STEALTH_BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=AutomationControlled",
    "--disable-infobars",
    "--disable-search-engine-choice-screen",
    "--disable-sync",
]


class PlaywrightSessionError(RuntimeError):
    """Raised when the native Playwright session fails."""


@dataclass(slots=True)
class PageState:
    url: str
    title: str
    body: str
    html: str
    frames: list[str]


class PlaywrightSession:
    """Native Playwright browser session."""

    def __init__(
        self,
        artifacts_dir: Path | None,
        *,
        headless: bool,
        proxy_server: str | None = None,
        user_agent: str | None = None,
        locale: str | None = None,
        timezone_id: str | None = None,
        viewport: dict[str, int] | None = None,
        profile_dir: Path | None = None,
        language: str | None = None,
        session_name: str | None = None,
    ) -> None:
        self.artifacts_dir = artifacts_dir
        self.session_name = session_name or f"ollama-{uuid.uuid4().hex[:8]}"
        self.config_path = self.artifacts_dir / f"{self.session_name}-playwright.json" if self.artifacts_dir else None
        self._headless = headless
        self._proxy_server = proxy_server
        self._user_agent = user_agent
        self._locale = locale
        self._timezone_id = timezone_id
        self._viewport = viewport or {"width": 1440, "height": 900}
        self._profile_dir = profile_dir
        self._language = language or locale
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._write_config()

    def _write_config(self) -> None:
        if self.config_path is None or self.artifacts_dir is None:
            return
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        launch_options: dict[str, Any] = {
            "channel": "chrome",
            "headless": self._headless,
            "chromiumSandbox": False,
            "ignoreDefaultArgs": ["--enable-automation"],
            "args": STEALTH_BROWSER_ARGS,
        }
        if self._proxy_server:
            launch_options["proxy"] = {"server": self._proxy_server}
        context_options: dict[str, Any] = {"viewport": self._viewport}
        if self._user_agent:
            context_options["userAgent"] = self._user_agent
        if self._locale:
            context_options["locale"] = self._locale
        if self._timezone_id:
            context_options["timezoneId"] = self._timezone_id
        if self._language:
            context_options["extraHTTPHeaders"] = {"Accept-Language": self._language}
        config = {
            "browser": {
                "browserName": "chromium",
                "launchOptions": launch_options,
                "contextOptions": context_options,
                "userDataDir": str(self._profile_dir) if self._profile_dir else None,
            },
            "timeouts": {
                "action": DEFAULT_ACTION_TIMEOUT_MS,
                "navigation": DEFAULT_NAVIGATION_TIMEOUT_MS,
            },
        }
        self.config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    def _ensure_started(self) -> None:
        if self._page is not None:
            return
        self._playwright = sync_playwright().start()
        launch_options: dict[str, Any] = {
            "headless": self._headless,
            "chromium_sandbox": False,
            "ignore_default_args": ["--enable-automation"],
            "args": STEALTH_BROWSER_ARGS,
        }
        if self._proxy_server:
            launch_options["proxy"] = {"server": self._proxy_server}

        context_options: dict[str, Any] = {"viewport": self._viewport}
        if self._user_agent:
            context_options["user_agent"] = self._user_agent
        if self._locale:
            context_options["locale"] = self._locale
        if self._timezone_id:
            context_options["timezone_id"] = self._timezone_id
        if self._language:
            context_options["extra_http_headers"] = {"Accept-Language": self._language}

        try:
            if self._profile_dir:
                self._profile_dir.mkdir(parents=True, exist_ok=True)
                context = self._playwright.chromium.launch_persistent_context(
                    str(self._profile_dir),
                    channel="chrome",
                    **launch_options,
                    **context_options,
                )
                browser = context.browser
            else:
                browser = self._playwright.chromium.launch(channel="chrome", **launch_options)
                context = browser.new_context(**context_options)
        except PlaywrightError:
            try:
                if self._profile_dir:
                    self._profile_dir.mkdir(parents=True, exist_ok=True)
                    context = self._playwright.chromium.launch_persistent_context(
                        str(self._profile_dir),
                        **launch_options,
                        **context_options,
                    )
                    browser = context.browser
                else:
                    browser = self._playwright.chromium.launch(**launch_options)
                    context = browser.new_context(**context_options)
            except PlaywrightError as exc:
                msg = (
                    "Unable to launch browser with Python Playwright. "
                    "If Chrome is unavailable, run `uv run playwright install chromium`."
                )
                raise PlaywrightSessionError(msg) from exc

        context.set_default_timeout(DEFAULT_ACTION_TIMEOUT_MS)
        context.set_default_navigation_timeout(DEFAULT_NAVIGATION_TIMEOUT_MS)
        page = context.pages[0] if context.pages else context.new_page()

        self._browser = browser
        self._context = context
        self._page = page

    def _page_or_raise(self) -> Page:
        self._ensure_started()
        if self._page is None:
            raise PlaywrightSessionError("Playwright page is not available")
        return self._page

    def _context_or_raise(self) -> BrowserContext:
        self._ensure_started()
        if self._context is None:
            raise PlaywrightSessionError("Playwright context is not available")
        return self._context

    def _wait_for_ready_state(self) -> None:
        page = self._page_or_raise()
        try:
            page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(1500)

    def open(self, url: str) -> None:
        self.goto(url)

    def close(self) -> None:
        if self._page is not None:
            self._page.close()
        if self._context is not None:
            self._context.close()
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    def goto(self, url: str) -> None:
        page = self._page_or_raise()
        page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_NAVIGATION_TIMEOUT_MS)
        self._wait_for_ready_state()

    def get_page_state(self) -> PageState:
        page = self._page_or_raise()
        html = page.content()
        payload = page.evaluate(
            """() => ({
                url: window.location.href,
                title: document.title,
                body: document.body?.innerText || '',
                html: document.documentElement.outerHTML,
            })"""
        )
        if not isinstance(payload, dict):
            raise PlaywrightSessionError("Unable to read page state")
        return PageState(
            url=str(payload["url"]),
            title=str(payload["title"]),
            body=str(payload["body"]),
            html=html or str(payload["html"]),
            frames=[str(frame.url) for frame in page.frames],
        )

    def fill_textbox(self, label: str, value: str) -> None:
        page = self._page_or_raise()
        page.get_by_role("textbox", name=label).fill(value)

    def fill_verification_code(self, code: str) -> None:
        page = self._page_or_raise()
        page.evaluate(
            """
            value => {
              const form = document.querySelector('form') || document.body;
              const setNativeValue = (input, nextValue) => {
                const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
                descriptor?.set?.call(input, nextValue);
                input.setAttribute('value', nextValue);
              };
              const hidden = form.querySelector('input[name="code"][type="hidden"], input[name="verification_code"][type="hidden"]');
              const singleInputs = [...form.querySelectorAll('input[type="text"]')].filter(node => node.maxLength === 1);
              if (singleInputs.length >= value.length) {
                [...value].forEach((char, index) => {
                  const input = singleInputs[index];
                  setNativeValue(input, char);
                  input.dispatchEvent(new InputEvent('input', { bubbles: true, data: char, inputType: 'insertText' }));
                  input.dispatchEvent(new Event('change', { bubbles: true }));
                });
                if (hidden) {
                  setNativeValue(hidden, value);
                  hidden.dispatchEvent(new Event('input', { bubbles: true }));
                  hidden.dispatchEvent(new Event('change', { bubbles: true }));
                }
                return;
              }
              const named = form.querySelector('input[name="code"]:not([type="hidden"]), input[name="verification_code"]:not([type="hidden"]), input[autocomplete="one-time-code"]:not([type="hidden"])');
              if (named) {
                setNativeValue(named, value);
                named.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
                named.dispatchEvent(new Event('change', { bubbles: true }));
                if (hidden) {
                  setNativeValue(hidden, value);
                  hidden.dispatchEvent(new Event('input', { bubbles: true }));
                  hidden.dispatchEvent(new Event('change', { bubbles: true }));
                }
                return;
              }
              const fallback = form.querySelector('input:not([type="hidden"])');
              if (!fallback) throw new Error('verification input not found');
              setNativeValue(fallback, value);
              fallback.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
              fallback.dispatchEvent(new Event('change', { bubbles: true }));
              if (hidden) {
                setNativeValue(hidden, value);
                hidden.dispatchEvent(new Event('input', { bubbles: true }));
                hidden.dispatchEvent(new Event('change', { bubbles: true }));
              }
            }
            """,
            code,
        )

    def fill_phone_number(self, *, local_number: str, e164_number: str, country_code: str | None) -> None:
        page = self._page_or_raise()
        page.evaluate(
            """
            ({ localNumber, e164Number, countryCode }) => {
              const form = document.querySelector('form') || document.body;
              const setValue = (selector, value) => {
                if (!value) return;
                const input = form.querySelector(selector);
                if (!input) return;
                const prototype = input instanceof HTMLSelectElement ? HTMLSelectElement.prototype : input.constructor.prototype;
                const descriptor = Object.getOwnPropertyDescriptor(prototype, 'value');
                descriptor?.set?.call(input, value);
                input.setAttribute('value', value);
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
              };
              setValue('input[name="local_number"]', localNumber);
              setValue('input[name="phone_number"]', e164Number);
              setValue('input[name="country_code"], select[name="country_code"]', countryCode);
            }
            """,
            {
                "localNumber": local_number,
                "e164Number": e164_number,
                "countryCode": country_code,
            },
        )

    def fill_phone_number_and_submit(self, *, local_number: str, e164_number: str, country_code: str | None) -> None:
        page = self._page_or_raise()
        start_url = page.url
        page.evaluate(
            """
            ({ localNumber, e164Number, countryCode }) => {
              const form = document.querySelector('form') || document.body;
              const setValue = (selector, value) => {
                if (!value) return;
                const input = form.querySelector(selector);
                if (!input) return;
                const prototype = input instanceof HTMLSelectElement ? HTMLSelectElement.prototype : input.constructor.prototype;
                const descriptor = Object.getOwnPropertyDescriptor(prototype, 'value');
                descriptor?.set?.call(input, value);
                input.setAttribute('value', value);
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
              };
              setValue('input[name="local_number"]', localNumber);
              setValue('input[name="phone_number"]', e164Number);
              setValue('input[name="country_code"], select[name="country_code"]', countryCode);
              const submitter = form.querySelector('button[type="submit"], input[type="submit"]');
              form.requestSubmit(submitter || undefined);
            }
            """,
            {
                "localNumber": local_number,
                "e164Number": e164_number,
                "countryCode": country_code,
            },
        )
        self._wait_for_post_submit_progress(start_url)

    def submit_current_form(self) -> None:
        page = self._page_or_raise()
        page.evaluate(
            """
            () => {
              const form = document.querySelector('form');
              const submitter = form?.querySelector('button[type="submit"], input[type="submit"]');
              form?.requestSubmit(submitter || undefined);
            }
            """
        )
        self._wait_for_ready_state()

    def _wait_for_post_submit_progress(self, start_url: str) -> None:
        page = self._page_or_raise()
        for _ in range(25):
            if page.url != start_url:
                break
            page.wait_for_timeout(200)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(1000)

    def submit_turnstile_form(self, token: str) -> dict[str, Any]:
        page = self._page_or_raise()
        start_url = page.url
        events: list[dict[str, Any]] = []

        def include_url(url: str) -> bool:
            return bool(re.search(r"signin\.ollama\.com|ollama\.com|challenges\.cloudflare\.com", url))

        def push(entry: dict[str, Any]) -> None:
            if len(events) < 40:
                events.append(entry)

        def on_request(request) -> None:  # type: ignore[no-untyped-def]
            if not include_url(request.url):
                return
            push(
                {
                    "type": "request",
                    "method": request.method,
                    "url": request.url,
                    "resourceType": request.resource_type,
                    "isNavigationRequest": request.is_navigation_request(),
                }
            )

        def on_response(response) -> None:  # type: ignore[no-untyped-def]
            if not include_url(response.url):
                return
            push(
                {
                    "type": "response",
                    "url": response.url,
                    "status": response.status,
                    "ok": response.ok,
                }
            )

        def on_request_failed(request) -> None:  # type: ignore[no-untyped-def]
            if not include_url(request.url):
                return
            push(
                {
                    "type": "requestfailed",
                    "url": request.url,
                    "errorText": request.failure,
                }
            )

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)
        try:
            page.evaluate(
                """
                value => {
                  const form = document.querySelector('form');
                  if (!form) throw new Error('form not found');
                  let input = form.querySelector('input[name="bot_detection_token"]');
                  if (!input) {
                    input = document.createElement('input');
                    input.type = 'hidden';
                    input.name = 'bot_detection_token';
                    form.appendChild(input);
                  }
                  input.value = value;
                  input.dispatchEvent(new Event('input', { bubbles: true }));
                  input.dispatchEvent(new Event('change', { bubbles: true }));
                  const submitter = form.querySelector('button[type="submit"], input[type="submit"]');
                  form.requestSubmit(submitter || undefined);
                }
                """,
                token,
            )

            wait_result = "timeout"
            for _ in range(50):
                if page.url != start_url:
                    wait_result = "url_changed"
                    break
                page.wait_for_timeout(200)
            if wait_result == "timeout":
                page.wait_for_timeout(7000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(500)
            page_snapshot = page.evaluate(
                """() => ({
                    url: window.location.href,
                    title: document.title,
                    body: document.body?.innerText || '',
                    html: document.documentElement.outerHTML,
                })"""
            )
            return {
                "waitResult": wait_result,
                "eventCount": len(events),
                "events": events,
                "page": page_snapshot,
            }
        finally:
            page.remove_listener("request", on_request)
            page.remove_listener("response", on_response)
            page.remove_listener("requestfailed", on_request_failed)

    def get_cookies(self) -> list[dict[str, Any]]:
        context = self._context_or_raise()
        return list(context.cookies())

    def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
        context = self._context_or_raise()
        context.add_cookies(cookies)

    def generate_api_key_payload(self) -> dict[str, Any]:
        page = self._page_or_raise()
        context = self._context_or_raise()

        def read_clipboard() -> str | None:
            try:
                context.grant_permissions(["clipboard-read", "clipboard-write"])
                value = page.evaluate(
                    """
                    async () => {
                      if (!navigator.clipboard?.readText) return null;
                      const text = await navigator.clipboard.readText();
                      return text || null;
                    }
                    """
                )
            except PlaywrightError:
                return None
            return str(value) if value else None

        page.get_by_role("button", name=re.compile(r"add api key", re.I)).click(timeout=15_000)
        page.get_by_role("button", name=re.compile(r"generate api key", re.I)).click(timeout=15_000)
        page.wait_for_timeout(1500)

        clipboard = read_clipboard()
        if not clipboard:
            try:
                page.get_by_role("button", name=re.compile(r"copy", re.I)).first.click(timeout=3000)
                page.wait_for_timeout(300)
                clipboard = read_clipboard()
            except PlaywrightError:
                clipboard = None

        payload = page.evaluate(
            """
            ({ clipboardValue }) => {
              const texts = [];
              for (const selector of ['input', 'textarea', 'code', 'pre', '[role="dialog"]', '[data-slot]']) {
                for (const node of document.querySelectorAll(selector)) {
                  const value = 'value' in node ? node.value : node.textContent;
                  if (value) texts.push(value);
                }
              }
              return {
                clipboard: clipboardValue,
                texts,
                buttons: [...document.querySelectorAll('button')]
                  .map(node => node.innerText || node.getAttribute('aria-label'))
                  .filter(Boolean),
                body: document.body?.innerText || '',
              };
            }
            """,
            {"clipboardValue": clipboard},
        )
        return dict(payload or {})
