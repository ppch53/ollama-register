"""Read channels from one-api.db backup and dump as JSON for re-import."""
import sqlite3, json, sys

db_path = sys.argv[1] if len(sys.argv) > 1 else "/opt/backups/1778349178/one-api.db"
out_path = sys.argv[2] if len(sys.argv) > 2 else "/opt/backups/1778349178/channels_dump.json"

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
c = conn.cursor()
c.execute("SELECT * FROM channels ORDER BY id")
rows = [dict(r) for r in c.fetchall()]

# convert any bytes columns to strings/decoded
for r in rows:
    for k, v in list(r.items()):
        if isinstance(v, bytes):
            try:
                r[k] = v.decode("utf-8")
            except:
                r[k] = v.hex()

with open(out_path, "w", encoding="utf-8") as f:
    json.dump(rows, f, ensure_ascii=False, indent=2)

print(f"dumped {len(rows)} channels to {out_path}")
print("first row keys:", list(rows[0].keys()) if rows else [])
print("sample (id=1):")
if rows:
    r = rows[0]
    print(f"  name: {r.get('name')}")
    print(f"  type: {r.get('type')}")
    print(f"  group: {r.get('group')}")
    print(f"  models: {(r.get('models') or '')[:80]}")
    print(f"  base_url: {r.get('base_url')}")
    print(f"  status: {r.get('status')}")
    print(f"  tag: {r.get('tag')}")
conn.close()
