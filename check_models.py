import json, os, urllib.request

BASE = os.getenv("NEWAPI_URL", "http://127.0.0.1:3000")
USERNAME = os.getenv("NEWAPI_USERNAME", "root")
PASSWORD = os.getenv("NEWAPI_PASSWORD")
if not PASSWORD:
    raise SystemExit("NEWAPI_PASSWORD is required")

# login
req = urllib.request.Request(
    BASE + "/api/user/login",
    data=json.dumps({"username": USERNAME, "password": PASSWORD}).encode(),
    headers={"Content-Type":"application/json"},
    method="POST"
)
resp = urllib.request.urlopen(req)
cookies = resp.headers.get("Set-Cookie","")

# get models
req2 = urllib.request.Request(
    BASE + "/api/models",
    headers={"Cookie": cookies, "New-Api-User": "1"}
)
resp2 = urllib.request.urlopen(req2)
data = json.loads(resp2.read().decode())
print("models count:", len(data.get("data", [])))
for m in data.get("data", [])[:10]:
    print(" ", m.get("id"))
