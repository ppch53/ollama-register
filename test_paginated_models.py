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

for url in [BASE + "/api/models", BASE + "/api/models?p=0", BASE + "/api/models?p=0&page_size=100"]:
    req2 = urllib.request.Request(url, headers={"Cookie": cookies, "New-Api-User": "1"})
    resp2 = urllib.request.urlopen(req2)
    data = json.loads(resp2.read().decode())
    d = data.get("data")
    print(url, "type", type(d).__name__)
    if isinstance(d, dict) and "items" in d:
        print("  items", len(d["items"]))
    elif isinstance(d, dict):
        print("  keys", list(d.keys())[:5])
    print()
