import sqlite3, json
conn = sqlite3.connect("/opt/new-api/data/one-api.db")
c = conn.cursor()
c.execute("SELECT * FROM channels WHERE id=1")
cols = [d[0] for d in c.description]
row = c.fetchone()
for i, v in enumerate(row):
    if v is None:
        continue
    val = str(v)
    if len(val) > 100:
        val = val[:100] + "..."
    print(f"{cols[i]}: {val}")
conn.close()
