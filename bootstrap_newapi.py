"""
Bootstrap a fresh new-api install via the official /api/setup endpoint.
After setup completes, login as root and create one test channel to confirm
the real DB format of the `group` field.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BASE = os.getenv("NEWAPI_URL", "http://127.0.0.1:3000")
USERNAME = os.getenv("NEWAPI_USERNAME", "root")
PASSWORD = os.getenv("NEWAPI_PASSWORD")
if not PASSWORD:
    raise SystemExit("NEWAPI_PASSWORD is required")


def http(method, path, body=None, cookies=None, headers=None):
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    if cookies:
        request_headers["Cookie"] = cookies
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=request_headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        body_text = resp.read().decode()
        return resp.status, dict(resp.headers), json.loads(body_text) if body_text else None
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode()


# Step 1: confirm setup status
s, h, b = http("GET", "/api/setup")
print(f"[1] GET /api/setup -> {s}: {b}")

# Step 2: do the setup (creates root user)
setup_body = {
    "username": USERNAME,
    "password": PASSWORD,
    "confirmPassword": PASSWORD,
    "SelfUseModeEnabled": True,
    "DemoSiteEnabled": False,
}
s, h, b = http("POST", "/api/setup", body=setup_body)
print(f"[2] POST /api/setup -> {s}: {b}")

# Step 3: login as root
s, h, b = http("POST", "/api/user/login", body={"username": USERNAME, "password": PASSWORD})
print(f"[3] POST /api/user/login -> {s}: success={b.get('success') if isinstance(b, dict) else b}")
cookies = h.get("Set-Cookie", "")
user_data = b.get("data", {}) if isinstance(b, dict) else {}
user_id = str(user_data.get("id"))
print(f"     user_id={user_id}, role={user_data.get('role')}, group={user_data.get('group')}")

# Step 4: create one test channel
test_channel = {
    "mode": "single",
    "channel": {
        "name": "TEST-bootstrap-channel",
        "type": 1,
        "key": "sk-test-key-NOT-REAL",
        "base_url": "http://127.0.0.1:8001",
        "models": "gpt-4o-mini,gemini-2.5-flash",
        "group": "default",
        "tag": "test",
        "priority": 0,
        "weight": 1,
        "status": 1,
        "test_model": "gpt-4o-mini",
        "auto_ban": 1,
    },
}
s, h, b = http(
    "POST",
    "/api/channel/",
    body=test_channel,
    cookies=cookies,
    headers={"New-Api-User": user_id},
)
print(f"[4] POST /api/channel/ -> {s}: {b}")

# Step 5: list channels via API
s, h, b = http(
    "GET",
    "/api/channel/?p=0&page_size=5",
    cookies=cookies,
    headers={"New-Api-User": user_id},
)
items = (b.get("data", {}) or {}).get("items", []) if isinstance(b, dict) else []
print(f"[5] GET /api/channel/ -> {s}, items={len(items)}")
for item in items:
    print(f"     [id={item['id']}] name={item['name']} group={item.get('group')!r}")

# Save cookie for later use
with open("/tmp/newapi_root.cookie", "w") as f:
    f.write(cookies)
with open("/tmp/newapi_root.userid", "w") as f:
    f.write(user_id)
print("\n[saved] cookie -> /tmp/newapi_root.cookie")
print(f"[saved] user_id -> /tmp/newapi_root.userid ({user_id})")
