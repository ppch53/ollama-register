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
print("top keys:", list(data["data"].keys())[:5])
for k in list(data["data"].keys())[:3]:
    v = data["data"][k]
    print("key", k, "type", type(v).__name__, "len", len(v) if hasattr(v, "__len__") else "?")
    if isinstance(v, list) and v:
        print("  first item keys:", list(v[0].keys())[:10])
        print("  first item id:", v[0].get("id"))
