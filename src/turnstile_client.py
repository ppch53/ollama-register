from __future__ import annotations

import time

import httpx


class TurnstileClient:
    def __init__(self, base_url: str, http_client: httpx.Client | None = None, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def create_task(self, url: str, sitekey: str | None = None, action: str | None = None) -> str:
        params = {"url": url}
        if sitekey:
            params["sitekey"] = sitekey
        if action:
            params["action"] = action
        response = self._client.get(f"{self.base_url}/turnstile", params=params)
        response.raise_for_status()
        payload = response.json()
        if payload.get("errorId") == 1:
            raise RuntimeError(payload.get("errorDescription") or payload.get("errorCode") or "Turnstile task creation failed")
        return str(payload["taskId"])

    def get_result(self, task_id: str) -> dict:
        response = self._client.get(f"{self.base_url}/result", params={"id": task_id})
        response.raise_for_status()
        return response.json()

    def wait_for_result(self, task_id: str, poll_interval_seconds: float, timeout_seconds: float) -> str:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            payload = self.get_result(task_id)
            if payload.get("status") == "ready":
                return str(payload["solution"]["token"])
            if payload.get("errorId") == 1 and payload.get("status") != "processing":
                raise RuntimeError(payload.get("errorDescription") or payload.get("errorCode") or "Turnstile solve failed")
            time.sleep(poll_interval_seconds)
        raise TimeoutError(f"Timed out waiting for Turnstile result: {task_id}")
