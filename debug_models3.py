import urllib.request, json

req = urllib.request.Request(
    "http://127.0.0.1:3000/api/user/login",
    data=json.dumps({"username":"root","password":"AdminPass2026!"}).encode(),
    headers={"Content-Type":"application/json"},
    method="POST"
)
resp = urllib.request.urlopen(req)
cookies = resp.headers.get("Set-Cookie","")
req2 = urllib.request.Request("http://127.0.0.1:3000/api/models", headers={"Cookie": cookies, "New-Api-User": "1"})
resp2 = urllib.request.urlopen(req2)
data = json.loads(resp2.read().decode())

for k in list(data["data"].keys())[:3]:
    v = data["data"][k]
    print("key", k, "count", len(v))
    for m in v[:5]:
        print("  ", repr(m))
    print()
