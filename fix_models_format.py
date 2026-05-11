import sqlite3
conn = sqlite3.connect("/opt/new-api/data/one-api.db")
c = conn.cursor()

# Check if models use comma or newline in existing channels
# Change comma-separated to newline-separated (new-api often prefers \n)
c.execute("SELECT id, models FROM channels WHERE status=1")
rows = c.fetchall()
fixed = 0
for cid, models in rows:
    if models and ',' in models:
        new_models = models.replace(',', '\n')
        c.execute("UPDATE channels SET models = ? WHERE id = ?", (new_models, cid))
        fixed += 1

conn.commit()
print("fixed", fixed, "channels")
conn.close()
