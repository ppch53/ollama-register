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

# /api/models (admin/global)
req2 = urllib.request.Request(BASE + "/api/models", headers={"Cookie": cookies, "New-Api-User": "1"})
resp2 = urllib.request.urlopen(req2)
data2 = json.loads(resp2.read().decode())
admin_models = data2.get("data", {})
admin_total = sum(len(v) for v in admin_models.values())
print("admin /api/models total:", admin_total)

# check what frontend might call
# try /api/models with no special header
req3 = urllib.request.Request(BASE + "/api/models", headers={"Cookie": cookies})
resp3 = urllib.request.urlopen(req3)
data3 = json.loads(resp3.read().decode())
print("/api/models (no User-Id) total:", sum(len(v) for v in data3.get("data", {}).values()))

# try /api/user/models
req4 = urllib.request.Request("http://127.0.0.1:3000/api/user/models", headers={"Cookie": cookies, "New-Api-User": "1"})
try:
    resp4 = urllib.request.urlopen(req4)
    data4 = json.loads(resp4.read().decode())
    print("/api/user/models:", data4.get("data") is not None, type(data4.get("data")).__name__)
except Exception as e:
    print("/api/user/models ERR:", e)
