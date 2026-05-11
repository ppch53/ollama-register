from __future__ import annotations

import time
from typing import Any

import httpx

from src.models import TempMailAddress, TempMailMessage


class TempMailClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        http_client: httpx.Client | None = None,
        timeout: float = 30.0,
        retry_count: int = 3,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.retry_count = retry_count
        self.retry_delay_seconds = retry_delay_seconds
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.retry_count):
            try:
                response = self._client.request(method, url, **kwargs)
                response.raise_for_status()
                return response
            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError) as exc:
                last_error = exc
                if attempt == self.retry_count - 1:
                    break
                time.sleep(self.retry_delay_seconds)
        raise RuntimeError("Tempmail request failed after retries") from last_error

    def create_address(self) -> TempMailAddress:
        response = self._request(
            "POST",
            f"{self.base_url}/api/new_address",
            headers={"x-custom-auth": self.api_key},
        )
        payload = response.json()
        return TempMailAddress(
            address_id=int(payload["address_id"]),
            address=str(payload["address"]),
            jwt=str(payload["jwt"]),
            created_at=str(payload["created_at"]),
            expires_at=str(payload["expires_at"]),
        )

    def list_mails(self, jwt: str, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        response = self._request(
            "GET",
            f"{self.base_url}/api/mails",
            params={"limit": limit, "offset": offset},
            headers={"Authorization": f"Bearer {jwt}"},
        )
        payload = response.json()
        if isinstance(payload, list):
            return payload
        return list(payload.get("items") or payload.get("mails") or payload.get("results") or [])

    def get_mail(self, jwt: str, mail_id: int) -> TempMailMessage:
        response = self._request(
            "GET",
            f"{self.base_url}/api/mail/{mail_id}",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        payload = response.json()
        return TempMailMessage(
            mail_id=int(payload.get("id", payload.get("mail_id", mail_id))),
            subject=payload.get("subject"),
            raw=str(payload["raw"]),
            created_at=payload.get("created_at"),
        )
