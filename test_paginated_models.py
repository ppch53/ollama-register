import urllib.request, json

req = urllib.request.Request(
    "http://127.0.0.1:3000/api/user/login",
    data=json.dumps({"username":"root","password":"AdminPass2026!"}).encode(),
    headers={"Content-Type":"application/json"},
    method="POST"
)
resp = urllib.request.urlopen(req)
cookies = resp.headers.get("Set-Cookie","")

for url in ["http://127.0.0.1:3000/api/models", "http://127.0.0.1:3000/api/models?p=0", "http://127.0.0.1:3000/api/models?p=0&page_size=100"]:
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
