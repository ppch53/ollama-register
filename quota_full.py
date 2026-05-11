import json
import os
import urllib.request as u

os.environ["http_proxy"] = "http://127.0.0.1:1081"
os.environ["https_proxy"] = "http://127.0.0.1:1081"

data = json.load(open("/opt/ollama-register/puter_accounts.json"))
print(f"total accounts: {len(data)}")

allowances: dict[int, int] = {}
alive = 0
dead: list[tuple[str, str]] = []
total_remaining = 0

for a in data:
    try:
        req = u.Request(
            "https://api.puter.com/metering/usage",
            headers={"Authorization": f"Bearer {a['token']}"},
        )
        resp = json.load(u.urlopen(req, timeout=12))
        ai = resp.get("allowanceInfo", {})
        m = int(ai.get("monthUsageAllowance", 0))
        r = int(ai.get("remaining", 0))
        allowances[m] = allowances.get(m, 0) + 1
        total_remaining += r
        alive += 1
    except Exception as e:
        dead.append((a["email"], str(e)[:60]))

print(f"alive: {alive}, dead: {len(dead)}")
print("allowance distribution (cap -> count):")
for cap, cnt in sorted(allowances.items()):
    print(f"  ${cap / 1e8:.4f} -> {cnt} accounts")
print(f"total monthly cap   = ${sum(c * n for c, n in allowances.items()) / 1e8:.4f}")
print(f"total remaining now = ${total_remaining / 1e8:.4f}")
if dead:
    print("dead accounts (first 10):")
    for e, msg in dead[:10]:
        print(f"  {e}: {msg}")
