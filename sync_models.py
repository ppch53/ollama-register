import sqlite3
import time

conn = sqlite3.connect("/opt/new-api/data/one-api.db")
c = conn.cursor()

# extract all models from channels.models (comma-separated)
c.execute("SELECT models FROM channels WHERE status = 1 AND models IS NOT NULL AND models != ''")
model_set = set()
for row in c.fetchall():
    if row[0]:
        for m in row[0].split(","):
            m = m.strip()
            if m:
                model_set.add(m)

print(f"found {len(model_set)} models from channels")

# insert into models table if not exists
tnow = int(time.time())
inserted = 0
for m in sorted(model_set):
    try:
        c.execute(
            "INSERT INTO models (model_name, description, status, created_time, updated_time) VALUES (?, ?, 1, ?, ?)",
            (m, m, tnow, tnow)
        )
        inserted += 1
    except sqlite3.IntegrityError:
        pass  # duplicate

conn.commit()
print(f"inserted {inserted} new models")
conn.close()
