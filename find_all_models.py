import urllib.request, json

req = urllib.request.Request(
    "http://127.0.0.1:3000/api/user/login",
    data=json.dumps({"username":"root","password":"AdminPass2026!"}).encode(),
    headers={"Content-Type":"application/json"},
    method="POST"
)
resp = urllib.request.urlopen(req)
cookies = resp.headers.get("Set-Cookie","")

paths = [
    "/api/models",
    "/api/model/",
    "/api/user/models",
    "/api/channel/model",
    "/v1/models",
]

for p in paths:
    req2 = urllib.request.Request("http://127.0.0.1:3000" + p, headers={"Cookie": cookies, "New-Api-User": "1"})
    try:
        resp2 = urllib.request.urlopen(req2)
        data = json.loads(resp2.read().decode())
        d = data.get("data")
        t = type(d).__name__
        if isinstance(d, dict) and "items" in d:
            print(p, "items", len(d["items"]))
        elif isinstance(d, dict):
            print(p, "dict keys", list(d.keys())[:5])
        elif isinstance(d, list):
            print(p, "list len", len(d))
        else:
            print(p, t, d)
    except Exception as e:
        print(p, "ERR", e)
