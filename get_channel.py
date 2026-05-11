import urllib.request, json

req = urllib.request.Request(
    "http://127.0.0.1:3000/api/user/login",
    data=json.dumps({"username":"root","password":"AdminPass2026!"}).encode(),
    headers={"Content-Type":"application/json"},
    method="POST"
)
resp = urllib.request.urlopen(req)
cookies = resp.headers.get("Set-Cookie","")

req2 = urllib.request.Request("http://127.0.0.1:3000/api/channel/1", headers={"Cookie": cookies, "New-Api-User": "1"})
resp2 = urllib.request.urlopen(req2)
data = json.loads(resp2.read().decode())
ch = data.get("data", {})
print("group:", repr(ch.get("group")))
print("models:", repr(ch.get("models")[:80] if ch.get("models") else None))
print("model_mapping:", repr(ch.get("model_mapping")))
print("type:", ch.get("type"))
print("status:", ch.get("status"))
