import urllib.request, json

# login
req = urllib.request.Request(
    "http://127.0.0.1:3000/api/user/login",
    data=json.dumps({"username":"root","password":"AdminPass2026!"}).encode(),
    headers={"Content-Type":"application/json"},
    method="POST"
)
resp = urllib.request.urlopen(req)
body = json.loads(resp.read().decode())
print("login success:", body.get("success"))
print("group:", body.get("data",{}).get("group"))

# save cookie
cookies = resp.headers.get("Set-Cookie","")
with open("/tmp/newapi.cookie", "w") as f:
    f.write(cookies)

# get models
req2 = urllib.request.Request(
    "http://127.0.0.1:3000/api/models",
    headers={"Cookie": cookies, "New-Api-User": "1"}
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
for m in items[:10]:
    print(" ", m.get("id"))
