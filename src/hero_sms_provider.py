from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from src.phone_provider import PhoneActivation

WAIT_STATUSES = ("STATUS_WAIT_CODE", "STATUS_WAIT_RETRY", "STATUS_WAIT_RESEND")
TERMINAL_STATUSES = ("STATUS_CANCEL", "STATUS_FINISH")
CODE_PATTERN = re.compile(r"\b(\d{4,10})\b")


class HeroSmsProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        service: str | None,
        country: int | None,
        operator: str | None = None,
        max_price: float | None = None,
        fixed_price: bool = False,
        phone_exception: str | None = None,
        artifacts_dir: Path | None = None,
        progress: Callable[[str], None] | None = None,
        http_client: httpx.Client | None = None,
        timeout: float = 30.0,
        retry_count: int = 3,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""
        self.service = service
        self.country = country
        self.operator = operator
        self.max_price = max_price
        self.fixed_price = fixed_price
        self.phone_exception = phone_exception
        self.artifacts_dir = artifacts_dir
        self.progress = progress
        self.retry_count = retry_count
        self.retry_delay_seconds = retry_delay_seconds
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(base_url=self.base_url, timeout=timeout)

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.service and self.country is not None)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def request_number(self, *, operator: str | None = None) -> PhoneActivation:
        self._ensure_ready()
        payload = self._call_action(
            "getNumberV2",
            service=self.service,
            country=self.country,
            operator=operator or self.operator,
            maxPrice=self.max_price,
            fixedPrice="true" if self.fixed_price else None,
            phoneException=self.phone_exception,
        )
        activation = self._parse_activation(payload)
        self._log(
            f"[phone] number acquired: activation={activation.activation_id}, phone={self._mask_phone_number(activation.phone_number)}"
        )
        self._write_debug_artifact(
            "hero-sms-request-number",
            {
                "service": self.service,
                "country": self.country,
                "operator": operator or self.operator,
                "max_price": self.max_price,
                "fixed_price": self.fixed_price,
                "phone_exception": self.phone_exception,
                "response": payload,
                "activation": {
                    "activation_id": activation.activation_id,
                    "phone_number": activation.phone_number,
                    "e164_number": activation.e164_number,
                    "local_number": activation.local_number,
                    "country_code_value": activation.country_code_value,
                },
            },
        )
        return activation

    def wait_for_code(
        self,
        activation_id: str,
        *,
        poll_interval_seconds: float,
        timeout_seconds: float,
    ) -> str:
        deadline = time.monotonic() + timeout_seconds
        last_status = "STATUS_WAIT_CODE"
        poll_history: list[dict[str, Any]] = []
        poll_count = 0
        while time.monotonic() < deadline:
            poll_count += 1
            status_payload = self._call_action("getStatus", id=activation_id)
            poll_entry: dict[str, Any] = {"getStatus": status_payload}
            status = ""
            if isinstance(status_payload, dict):
                code = self._extract_code_from_payload(status_payload)
                status = str(status_payload.get("status") or "")
                if status:
                    last_status = status
                    poll_entry["status"] = status
                poll_entry["code"] = code
                if code:
                    poll_history.append(poll_entry)
                    self._write_debug_artifact(
                        "hero-sms-wait-code",
                        {
                            "activation_id": activation_id,
                            "result": "code_from_getStatus_object",
                            "code": code,
                            "polls": poll_history,
                        },
                    )
                    return code
            else:
                status = str(status_payload)
                last_status = status
                code = self._extract_code_from_status(status)
                poll_entry["status"] = status
                poll_entry["code"] = code
                if code:
                    poll_history.append(poll_entry)
                    self._write_debug_artifact(
                        "hero-sms-wait-code",
                        {
                            "activation_id": activation_id,
                            "result": "code_from_getStatus_status",
                            "code": code,
                            "polls": poll_history,
                        },
                    )
                    self._log(f"[phone] sms code received for activation {activation_id}")
                    return code
            if poll_count == 1 or poll_count % 5 == 0:
                self._log(f"[phone] waiting for sms code (poll {poll_count}, status={status or 'unknown'})")
            payload, payload_error = self._try_call_action("getStatusV2", id=activation_id)
            if payload is not None:
                poll_entry["getStatusV2"] = payload
                code = self._extract_code_from_payload(payload)
                poll_entry["code_from_v2"] = code
            elif payload_error is not None:
                poll_entry["getStatusV2_error"] = payload_error
                code = None
            poll_history.append(poll_entry)
            if code:
                self._write_debug_artifact(
                    "hero-sms-wait-code",
                    {
                        "activation_id": activation_id,
                        "result": "code_from_getStatusV2",
                        "code": code,
                        "polls": poll_history,
                    },
                )
                self._log(f"[phone] sms code received for activation {activation_id}")
                return code
            all_sms_payload, all_sms_error = self._try_call_action("getAllSms", id=activation_id)
            if all_sms_payload is not None:
                poll_entry["getAllSms"] = all_sms_payload
                code = self._extract_code_from_payload(all_sms_payload)
                poll_entry["code_from_all_sms"] = code
            elif all_sms_error is not None:
                poll_entry["getAllSms_error"] = all_sms_error
                code = None
            if code:
                self._write_debug_artifact(
                    "hero-sms-wait-code",
                    {
                        "activation_id": activation_id,
                        "result": "code_from_getAllSms",
                        "code": code,
                        "polls": poll_history,
                    },
                )
                self._log(f"[phone] sms code received for activation {activation_id}")
                return code
            if status.startswith(TERMINAL_STATUSES):
                self._log(f"[phone] activation ended before sms code arrived: {status}")
                self._write_debug_artifact(
                    "hero-sms-wait-code",
                    {
                        "activation_id": activation_id,
                        "result": "terminal_status",
                        "polls": poll_history,
                    },
                )
                raise RuntimeError(f"HeroSMS activation ended before code arrived: {status}")
            if status.startswith(WAIT_STATUSES) or not status:
                time.sleep(poll_interval_seconds)
                continue
            if status:
                self._log(f"[phone] unexpected activation status: {status}")
                self._write_debug_artifact(
                    "hero-sms-wait-code",
                    {
                        "activation_id": activation_id,
                        "result": "unexpected_status",
                        "polls": poll_history,
                    },
                )
                raise RuntimeError(f"Unexpected HeroSMS activation status: {status}")
            time.sleep(poll_interval_seconds)
        self._log(f"[phone] timed out waiting for sms code: {last_status}")
        self._write_debug_artifact(
            "hero-sms-wait-code",
            {
                "activation_id": activation_id,
                "result": "timeout",
                "last_status": last_status,
                "polls": poll_history,
            },
        )
        raise TimeoutError(f"Timed out waiting for HeroSMS code: {last_status}")

    def finish_activation(self, activation_id: str) -> None:
        self._request("GET", "/stubs/handler_api.php", params=self._build_params("finishActivation", id=activation_id))

    def cancel_activation(self, activation_id: str) -> None:
        self._request("GET", "/stubs/handler_api.php", params=self._build_params("cancelActivation", id=activation_id))

    def _ensure_ready(self) -> None:
        if self.is_configured:
            return
        raise RuntimeError("HeroSMS provider is missing HERO_SMS_API_KEY, HERO_SMS_SERVICE, or HERO_SMS_COUNTRY_ID")

    def _build_params(self, action: str, **params: Any) -> dict[str, Any]:
        payload = {"action": action, "api_key": self.api_key}
        payload.update({key: value for key, value in params.items() if value is not None and value != ""})
        return payload

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.retry_count):
            try:
                response = self._client.request(method, url, **kwargs)
                if response.status_code >= 400:
                    raise RuntimeError(self._extract_error(response))
                return response
            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError) as exc:
                last_error = exc
                if attempt == self.retry_count - 1:
                    break
                time.sleep(self.retry_delay_seconds)
        raise RuntimeError("HeroSMS request failed after retries") from last_error

    def _call_action(self, action: str, **params: Any) -> dict[str, Any] | str:
        response = self._request("GET", "/stubs/handler_api.php", params=self._build_params(action, **params))
        body = response.text.strip()
        if not body:
            return ""
        if body[0] in "[{":
            return json.loads(body)
        return body

    def _try_call_action(self, action: str, **params: Any) -> tuple[dict[str, Any] | str | None, str | None]:
        try:
            return self._call_action(action, **params), None
        except RuntimeError as exc:
            return None, str(exc)

    def _parse_activation(self, payload: dict[str, Any] | str) -> PhoneActivation:
        if isinstance(payload, dict):
            return PhoneActivation(
                activation_id=str(payload["activationId"]),
                phone_number=str(payload["phoneNumber"]),
                country_phone_code=self._to_int(payload.get("countryPhoneCode")),
            )
        match = re.match(r"ACCESS_NUMBER:([^:]+):(.+)", payload)
        if match:
            return PhoneActivation(activation_id=match.group(1), phone_number=match.group(2))
        raise RuntimeError(f"Unexpected HeroSMS activation response: {payload}")

    def _extract_code_from_status(self, status: str) -> str | None:
        if not status.startswith("STATUS_OK"):
            return None
        return self._extract_code(status.split(":", 1)[1] if ":" in status else status)

    def _extract_code_from_payload(self, payload: dict[str, Any] | str) -> str | None:
        if not isinstance(payload, (dict, list)):
            return None
        for value in self._iter_code_candidates(payload):
            code = self._extract_code(value)
            if code:
                return code
        return None

    def _iter_code_candidates(self, payload: Any) -> list[Any]:
        candidates: list[Any] = []
        if isinstance(payload, dict):
            status = payload.get("status")
            if status:
                candidates.append(status)
            for key in ("code", "text", "message", "verificationCode"):
                if key in payload:
                    candidates.append(payload[key])
            for key in ("sms", "call", "messages", "data"):
                if key in payload:
                    candidates.extend(self._iter_code_candidates(payload[key]))
        elif isinstance(payload, list):
            for item in payload:
                candidates.extend(self._iter_code_candidates(item))
        elif payload is not None:
            candidates.append(payload)
        return candidates

    def _extract_code(self, value: Any) -> str | None:
        if value is None:
            return None
        match = CODE_PATTERN.search(str(value))
        return match.group(1) if match else None

    def _extract_error(self, response: httpx.Response) -> str:
        body = response.text.strip()
        if not body:
            return f"HeroSMS HTTP {response.status_code}"
        try:
            payload = response.json()
        except ValueError:
            return body
        if isinstance(payload, dict):
            return str(payload.get("message") or payload.get("detail") or payload)
        return str(payload)

    def _to_int(self, value: Any) -> int | None:
        if value in (None, ""):
            return None
        return int(value)

    def _log(self, message: str) -> None:
        if self.progress is None:
            return
        self.progress(message)

    def _mask_phone_number(self, phone_number: str) -> str:
        if len(phone_number) <= 4:
            return phone_number
        return f"{'*' * (len(phone_number) - 4)}{phone_number[-4:]}"

    def _write_debug_artifact(self, prefix: str, payload: dict[str, Any]) -> None:
        if self.artifacts_dir is None:
            return
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifacts_dir / f"{prefix}-{time.time_ns()}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
