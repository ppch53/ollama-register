"""
CapSolver shim:
  Expose anti-captcha-style endpoints expected by ollama-register's
  src/turnstile_client.py:

    GET  /turnstile?url=&sitekey=&action=  -> {"taskId": "..."}
    GET  /result?id=                       -> {"status": "ready", "solution":{"token":"..."}}
                                             | {"status": "processing"}
                                             | {"errorId":1, "errorDescription":"..."}

  Internally calls CapSolver:
    POST https://api.capsolver.com/createTask
    POST https://api.capsolver.com/getTaskResult

Env:
  CAPSOLVER_KEY   required, CAP-xxxx
  HTTP_PORT       default 5072
  PROXY           optional CapSolver proxy spec (e.g. http://user:pass@host:port)
                  if set, uses AntiTurnstileTask + proxy params instead of proxyless
"""

from __future__ import annotations

import os
import time

import httpx
from quart import Quart, jsonify, request

CAPSOLVER_KEY = os.environ["CAPSOLVER_KEY"]
HTTP_PORT = int(os.environ.get("HTTP_PORT", "5072"))
CAPSOLVER_API = "https://api.capsolver.com"
PROXY = os.environ.get("PROXY", "").strip()

app = Quart(__name__)
client = httpx.AsyncClient(timeout=30.0)


def _build_proxy_fields() -> dict:
    """Parse PROXY env into AntiTurnstileTask proxy params."""
    if not PROXY:
        return {}
    # http://user:pass@host:port
    from urllib.parse import urlparse

    p = urlparse(PROXY)
    return {
        "proxyType": p.scheme or "http",
        "proxyAddress": p.hostname or "",
        "proxyPort": int(p.port or 0),
        "proxyLogin": p.username or "",
        "proxyPassword": p.password or "",
    }


@app.route("/turnstile")
async def create_task():
    url = request.args.get("url", "")
    sitekey = request.args.get("sitekey", "")
    action = request.args.get("action", "")
    cdata = request.args.get("cdata", "")

    if not url or not sitekey:
        return jsonify({"errorId": 1, "errorDescription": "url and sitekey required"}), 400

    proxy_fields = _build_proxy_fields()
    task: dict = {
        "type": "AntiTurnstileTask" if proxy_fields else "AntiTurnstileTaskProxyLess",
        "websiteURL": url,
        "websiteKey": sitekey,
    }
    metadata: dict = {}
    # CapSolver rejects actions with leading non-alphanumeric chars; the
    # ollama-register project synthesises actions like "-sign-up" from URL paths.
    action = action.lstrip("-_/ ")
    if action:
        metadata["action"] = action
    if cdata:
        metadata["cdata"] = cdata
    if metadata:
        task["metadata"] = metadata
    if proxy_fields:
        task.update(proxy_fields)

    try:
        r = await client.post(
            f"{CAPSOLVER_API}/createTask",
            json={"clientKey": CAPSOLVER_KEY, "task": task},
        )
        data = r.json()
    except Exception as exc:
        return jsonify({"errorId": 1, "errorDescription": f"createTask network: {exc}"}), 502

    if data.get("errorId"):
        return jsonify(
            {
                "errorId": 1,
                "errorCode": data.get("errorCode"),
                "errorDescription": data.get("errorDescription"),
            }
        )
    task_id = data.get("taskId")
    if not task_id:
        return jsonify({"errorId": 1, "errorDescription": f"no taskId: {data}"}), 502
    return jsonify({"taskId": task_id})


@app.route("/result")
async def get_result():
    task_id = request.args.get("id", "")
    if not task_id:
        return jsonify({"errorId": 1, "errorDescription": "missing id"}), 400
    try:
        r = await client.post(
            f"{CAPSOLVER_API}/getTaskResult",
            json={"clientKey": CAPSOLVER_KEY, "taskId": task_id},
        )
        data = r.json()
    except Exception as exc:
        return jsonify({"errorId": 1, "errorDescription": f"getTaskResult network: {exc}"}), 502

    if data.get("errorId"):
        return jsonify(
            {
                "errorId": 1,
                "errorCode": data.get("errorCode"),
                "errorDescription": data.get("errorDescription"),
                "status": "failed",
            }
        )

    status = data.get("status")
    if status == "ready":
        solution = data.get("solution") or {}
        token = solution.get("token") or solution.get("gRecaptchaResponse")
        return jsonify(
            {
                "status": "ready",
                "solution": {
                    "token": token,
                    "userAgent": solution.get("userAgent"),
                    **solution,
                },
            }
        )
    # processing / idle
    return jsonify({"status": "processing"})


@app.route("/health")
async def health():
    try:
        r = await client.post(
            f"{CAPSOLVER_API}/getBalance", json={"clientKey": CAPSOLVER_KEY}
        )
        return jsonify(r.json())
    except Exception as exc:
        return jsonify({"errorId": 1, "errorDescription": str(exc)}), 502


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=HTTP_PORT)
