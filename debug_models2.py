import json, os, urllib.request

BASE = os.getenv("NEWAPI_URL", "http://127.0.0.1:3000")
USERNAME = os.getenv("NEWAPI_USERNAME", "root")
PASSWORD = os.getenv("NEWAPI_PASSWORD")
if not PASSWORD:
    raise SystemExit("NEWAPI_PASSWORD is required")

req = urllib.request.Request(
    BASE + "/api/user/login",
    data=json.dumps({"username": USERNAME, "password": PASSWORD}).encode(),
    headers={"Content-Type":"application/json"},
    method="POST"
)
resp = urllib.request.urlopen(req)
cookies = resp.headers.get("Set-Cookie","")
req2 = urllib.request.Request(BASE + "/api/models", headers={"Cookie": cookies, "New-Api-User": "1"})
resp2 = urllib.request.urlopen(req2)
data = json.loads(resp2.read().decode())
print("top keys:", list(data["data"].keys())[:5])
for k in list(data["data"].keys())[:3]:
    v = data["data"][k]
    print("key", k, "type", type(v).__name__, "len", len(v) if hasattr(v, "__len__") else "?")
    if isinstance(v, list) and v:
        print("  first item keys:", list(v[0].keys())[:10])
        print("  first item id:", v[0].get("id"))
