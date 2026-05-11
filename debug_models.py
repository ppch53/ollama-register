import urllib.request, json

req = urllib.request.Request(
    "http://127.0.0.1:3000/api/user/login",
    data=json.dumps({"username":"root","password":"AdminPass2026!"}).encode(),
    headers={"Content-Type":"application/json"},
    method="POST"
)
resp = urllib.request.urlopen(req)
cookies = resp.headers.get("Set-Cookie","")

for path in ["/api/models", "/api/user/models", "/v1/models"]:
    req2 = urllib.request.Request("http://127.0.0.1:3000" + path, headers={"Cookie": cookies, "New-Api-User": "1"})
    try:
        resp2 = urllib.request.urlopen(req2)
        data = json.loads(resp2.read().decode())
        print("===", path, "===")
        print("keys:", list(data.keys()))
        d = data.get("data")
        if isinstance(d, dict):
            print("data keys:", list(d.keys())[:10])
            if "items" in d:
                print("items count:", len(d["items"]))
            elif "data" in d:
                print("nested data count:", len(d["data"]))
        elif isinstance(d, list):
            print("list count:", len(d))
        else:
            print("data type:", type(d))
    except Exception as e:
        print(path, "ERR", e)
    print()
