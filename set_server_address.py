import urllib.request, json

with open("/tmp/newapi_root.cookie") as f:
    cookies = f.read().strip()
with open("/tmp/newapi_root.userid") as f:
    user_id = f.read().strip()

req = urllib.request.Request(
    "http://127.0.0.1:3000/api/option/",
    data=json.dumps({"key": "ServerAddress", "value": "https://api.ppch.qzz.io"}).encode(),
    headers={"Cookie": cookies, "Content-Type": "application/json", "New-Api-User": user_id},
    method="PUT",
)
resp = urllib.request.urlopen(req, timeout=30)
print(resp.status, resp.read().decode())
