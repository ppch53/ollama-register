import urllib.request, json

# login
req = urllib.request.Request(
    "http://127.0.0.1:3000/api/user/login",
    data=json.dumps({"username":"root","password":"AdminPass2026!"}).encode(),
    headers={"Content-Type":"application/json"},
    method="POST"
)
resp = urllib.request.urlopen(req)
cookies = resp.headers.get("Set-Cookie","")

# get models
req2 = urllib.request.Request(
    "http://127.0.0.1:3000/api/models",
    headers={"Cookie": cookies, "New-Api-User": "1"}
)
resp2 = urllib.request.urlopen(req2)
data = json.loads(resp2.read().decode())
print("models count:", len(data.get("data", [])))
for m in data.get("data", [])[:10]:
    print(" ", m.get("id"))
