"""
Per-channel smoke test using the channel-specific test endpoint of new-api.
Calls each channel directly (bypassing distributor random selection) so we know
which individual upstream keys are alive.

Endpoint: GET /api/channel/test/{id}  — uses the channel's `test_model`
"""
import urllib.request, json
import concurrent.futures

with open("/tmp/newapi_root.cookie") as f:
    COOKIES = f.read().strip()
with open("/tmp/newapi_root.userid") as f:
    USER_ID = f.read().strip()

HEADERS = {"Cookie": COOKIES, "New-Api-User": USER_ID}


def list_channels():
    req = urllib.request.Request(
        "http://127.0.0.1:3000/api/channel/?p=0&page_size=200",
        headers=HEADERS,
    )
    resp = urllib.request.urlopen(req, timeout=30)
    data = json.loads(resp.read().decode())
    return (data.get("data", {}) or {}).get("items", [])


def test_channel(channel):
    cid = channel["id"]
    name = channel["name"]
    tag = channel.get("tag", "?")
    req = urllib.request.Request(
        f"http://127.0.0.1:3000/api/channel/test/{cid}",
        headers=HEADERS,
    )
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        body = json.loads(resp.read().decode())
        ok = body.get("success", False)
        msg = body.get("message", "")
        time_ms = body.get("time", 0)
        return cid, name, tag, ok, msg, time_ms
    except Exception as e:
        return cid, name, tag, False, str(e), 0


channels = list_channels()
print(f"testing {len(channels)} channels...")

results = []
with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
    futures = [pool.submit(test_channel, ch) for ch in channels]
    for fut in concurrent.futures.as_completed(futures):
        cid, name, tag, ok, msg, t = fut.result()
        results.append((cid, name, tag, ok, msg, t))
        flag = "OK" if ok else "FAIL"
        print(f"  [{cid:3d}] {flag:4s} {tag:6s} {name[:35]:35s} t={t:.2f}s {msg[:70]}")

results.sort(key=lambda x: x[0])
ok_count = sum(1 for r in results if r[3])
puter_ok = sum(1 for r in results if r[3] and r[2] == "puter")
puter_total = sum(1 for r in results if r[2] == "puter")
ollama_ok = sum(1 for r in results if r[3] and r[2] == "ollama")
ollama_total = sum(1 for r in results if r[2] == "ollama")
print()
print(f"summary: total OK = {ok_count}/{len(results)}")
print(f"  puter:  {puter_ok}/{puter_total}")
print(f"  ollama: {ollama_ok}/{ollama_total}")
