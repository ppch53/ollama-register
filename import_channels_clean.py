"""
Clean import of 73 puter + 4 ollama channels into a fresh new-api install.
Uses ONLY the official HTTP API. group=default (plain string), models=comma-separated.

Reads:
  /opt/ollama-register/puter_accounts.json   (73 puter tokens)
  /opt/ollama-register/accounts.json          (4 ollama accounts)

Login: uses /tmp/newapi_root.cookie + /tmp/newapi_root.userid produced by bootstrap_newapi.py
"""
import urllib.request, json, time

BASE = "http://127.0.0.1:3000"

PUTER_MODELS = ",".join([
    "gemini-3.1-pro-preview", "gemini-3.1-flash-lite", "gemini-3-flash-preview",
    "gemini-2.5-pro", "gemini-2.5-flash",
    "claude-sonnet-4-5", "claude-3-7-sonnet", "claude-3-5-sonnet-latest",
    "gpt-5", "gpt-4o", "gpt-4o-mini",
    "deepseek-chat", "deepseek-reasoner",
    "kimi-k2.6", "minimax-m2.5", "minimax-m2.7",
    "glm-5", "glm-5.1", "glm-4.7",
    "qwen3-coder:480b", "qwen3-next:80b", "qwen3.5",
    "nemotron-3-super", "gemma4:31b", "gemma3:27b",
])

OLLAMA_MODELS = ",".join([
    "gpt-oss:120b", "gpt-oss:20b",
    "qwen3-coder:480b", "qwen3-next:80b",
    "gemma3:27b", "gemma3:4b",
    "glm-4.7:cloud", "minimax-m2.5:cloud", "nemotron-3-super:cloud",
])

with open("/tmp/newapi_root.cookie") as f:
    COOKIES = f.read().strip()
with open("/tmp/newapi_root.userid") as f:
    USER_ID = f.read().strip()
HEADERS = {"Cookie": COOKIES, "Content-Type": "application/json", "New-Api-User": USER_ID}


def http(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=HEADERS, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def add_channel(*, name, key, base_url, models, tag, test_model):
    body = {
        "mode": "single",
        "channel": {
            "name": name,
            "type": 1,
            "key": key,
            "base_url": base_url,
            "models": models,
            "group": "default",  # plain string, not JSON array
            "tag": tag,
            "priority": 0,
            "weight": 1,
            "status": 1,
            "test_model": test_model,
            "auto_ban": 1,
        },
    }
    return http("POST", "/api/channel/", body=body)


# Step 0: delete bootstrap test channel
s, b = http("DELETE", "/api/channel/1")
print(f"[0] delete test channel -> {s}: {b}")

# Step 1: Puter
puter_added = 0
with open("/opt/ollama-register/puter_accounts.json") as f:
    puter_accounts = json.load(f)
for i, acc in enumerate(puter_accounts, 1):
    name = f"puter-{i:03d}-{acc['username']}"
    s, b = add_channel(
        name=name,
        key=acc["token"],
        base_url="http://127.0.0.1:8001",
        models=PUTER_MODELS,
        tag="puter",
        test_model="gpt-4o-mini",
    )
    if s == 200 and isinstance(b, dict) and b.get("success"):
        puter_added += 1
    else:
        print(f"  [puter {i}] {name} FAIL: status={s} body={b}")
    if i % 20 == 0:
        print(f"  [puter] progress {i}/{len(puter_accounts)}")
    time.sleep(0.05)
print(f"[1] PUTER: added {puter_added}/{len(puter_accounts)}")

# Step 2: Ollama
ollama_added = 0
with open("/opt/ollama-register/accounts.json") as f:
    ollama_accounts = json.load(f)
for i, acc in enumerate(ollama_accounts, 1):
    name = f"ollama-{i:02d}-{acc['email'].split('@')[0][:10]}"
    s, b = add_channel(
        name=name,
        key=acc["api_key"],
        base_url="https://ollama.com",
        models=OLLAMA_MODELS,
        tag="ollama",
        test_model="gpt-oss:20b",
    )
    if s == 200 and isinstance(b, dict) and b.get("success"):
        ollama_added += 1
    else:
        print(f"  [ollama {i}] {name} FAIL: status={s} body={b}")
print(f"[2] OLLAMA: added {ollama_added}/{len(ollama_accounts)}")

# Step 3: summary
s, b = http("GET", "/api/channel/?p=0&page_size=200")
total = (b.get("data", {}) or {}).get("total", "?") if isinstance(b, dict) else "?"
print(f"[3] total channels in new-api: {total}")
