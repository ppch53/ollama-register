from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from src.models import FlareSolverrSolution


class FlareSolverrClient:
    def __init__(self, base_url: str, http_client: httpx.Client | None = None, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(
            f"{self.base_url}/v1",
            content=json.dumps(payload, separators=(",", ":")),
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        return response.json()

    def create_session(self, session_id: str | None = None) -> str:
        chosen_session = session_id or str(uuid.uuid4())
        payload = self._post({"cmd": "sessions.create", "session": chosen_session})
        if payload.get("status") != "ok":
            raise RuntimeError(payload)
        return str(payload["session"])

    def destroy_session(self, session_id: str) -> None:
        payload = self._post({"cmd": "sessions.destroy", "session": session_id})
        if payload.get("status") != "ok":
            raise RuntimeError(payload)

    def request_get(
        self,
        url: str,
        session_id: str,
        cookies: list[dict[str, Any]] | None = None,
        max_timeout_ms: int = 60000,
        wait_in_seconds: int | None = None,
    ) -> FlareSolverrSolution:
        payload: dict[str, Any] = {
            "cmd": "request.get",
            "url": url,
            "session": session_id,
            "maxTimeout": max_timeout_ms,
        }
        if cookies:
            payload["cookies"] = cookies
        if wait_in_seconds is not None:
            payload["waitInSeconds"] = wait_in_seconds

        response_payload = self._post(payload)
        solution = response_payload.get("solution") or {}
        return FlareSolverrSolution(
            cookies=list(solution.get("cookies", [])),
            user_agent=solution.get("userAgent"),
            response=solution.get("response"),
            status_code=solution.get("status"),
        )
