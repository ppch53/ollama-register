import sqlite3
conn = sqlite3.connect("/opt/new-api/data/one-api.db")
c = conn.cursor()
# Revert: change \n back to ,
c.execute("SELECT id, models FROM channels WHERE models LIKE '%' || char(10) || '%'")
rows = c.fetchall()
fixed = 0
for cid, models in rows:
    new_models = models.replace('\n', ',')
    c.execute("UPDATE channels SET models = ? WHERE id = ?", (new_models, cid))
    fixed += 1
conn.commit()
print("reverted", fixed, "channels back to comma-separated")

# verify
c.execute("SELECT id, models FROM channels LIMIT 2")
for r in c.fetchall():
    print(r[0], repr(r[1][:80]))
conn.close()
