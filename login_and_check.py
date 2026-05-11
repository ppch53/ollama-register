from __future__ import annotations

import json
import os
import urllib.request

BASE = os.getenv("NEWAPI_URL", "http://127.0.0.1:3000")
USERNAME = os.getenv("NEWAPI_USERNAME", "root")
PASSWORD = os.getenv("NEWAPI_PASSWORD")
if not PASSWORD:
    raise SystemExit("NEWAPI_PASSWORD is required")

# login
req = urllib.request.Request(
    BASE + "/api/user/login",
    data=json.dumps({"username": USERNAME, "password": PASSWORD}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
resp = urllib.request.urlopen(req)
body = json.loads(resp.read().decode())
print("login success:", body.get("success"))
print("group:", body.get("data", {}).get("group"))

# save cookie
cookies = resp.headers.get("Set-Cookie", "")
with open("/tmp/newapi.cookie", "w") as f:
    f.write(cookies)

# get models
req2 = urllib.request.Request(
    BASE + "/api/models",
    headers={"Cookie": cookies, "New-Api-User": "1"},
)
resp2 = urllib.request.urlopen(req2)
data = json.loads(resp2.read().decode())
models = data.get("data", {})
if isinstance(models, dict):
    items = models.get("items", [])
    total = models.get("total", 0)
else:
    items = models
    total = len(items)
print("models total:", total, "items:", len(items))
for model in items[:10]:
    print(" ", model.get("id"))
