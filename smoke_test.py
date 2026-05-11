"""Create a master token and verify chat completion routes correctly."""
import urllib.request, json

BASE = "http://127.0.0.1:3000"

with open("/tmp/newapi_root.cookie") as f:
    COOKIES = f.read().strip()
with open("/tmp/newapi_root.userid") as f:
    USER_ID = f.read().strip()

HEADERS = {"Cookie": COOKIES, "Content-Type": "application/json", "New-Api-User": USER_ID}


def http(method, path, body=None, headers=None):
    h = dict(HEADERS)
    if headers:
        h.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# Step 1: create token (unlimited quota, no expiry)
token_body = {
    "name": "master-key",
    "remain_quota": -1,
    "unlimited_quota": True,
    "expired_time": -1,
    "model_limits_enabled": False,
    "model_limits": "",
    "allow_ips": "",
    "group": "",
}
s, b = http("POST", "/api/token/", body=token_body)
print(f"[1] POST /api/token/ -> {s}: {b}")

# Step 2: list tokens to grab the actual key
s, b = http("GET", "/api/token/?p=0&page_size=10")
items = (b.get("data", {}) or {}).get("items", []) if isinstance(b, dict) else []
master_key = None
for t in items:
    if t.get("name") == "master-key":
        # need a separate query to get full key (list returns masked)
        tid = t["id"]
        s2, b2 = http("GET", f"/api/token/{tid}")
        if isinstance(b2, dict) and b2.get("success"):
            master_key = b2["data"]["key"]
            break
print(f"[2] master_key (first 20): {master_key[:20] if master_key else 'NONE'}...")

if master_key:
    with open("/tmp/master_key.txt", "w") as f:
        f.write(master_key)

# Step 3: test chat completion through new-api
test_body = {
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "say ok"}],
    "max_tokens": 5,
}
req = urllib.request.Request(
    BASE + "/v1/chat/completions",
    data=json.dumps(test_body).encode(),
    headers={"Authorization": f"Bearer {master_key}", "Content-Type": "application/json"},
    method="POST",
)
try:
    resp = urllib.request.urlopen(req, timeout=60)
    body = resp.read().decode()
    print(f"[3] /v1/chat/completions -> {resp.status}: {body[:200]}")
except urllib.error.HTTPError as e:
    print(f"[3] /v1/chat/completions ERROR -> {e.code}: {e.read().decode()[:200]}")

# Step 4: test ollama free-tier model
test_body2 = {
    "model": "gpt-oss:20b",
    "messages": [{"role": "user", "content": "hi"}],
    "max_tokens": 5,
}
req2 = urllib.request.Request(
    BASE + "/v1/chat/completions",
    data=json.dumps(test_body2).encode(),
    headers={"Authorization": f"Bearer {master_key}", "Content-Type": "application/json"},
    method="POST",
)
try:
    resp = urllib.request.urlopen(req2, timeout=60)
    body = resp.read().decode()
    print(f"[4] gpt-oss:20b -> {resp.status}: {body[:200]}")
except urllib.error.HTTPError as e:
    print(f"[4] gpt-oss:20b ERROR -> {e.code}: {e.read().decode()[:200]}")
