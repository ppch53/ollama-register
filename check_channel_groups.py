import sqlite3
conn = sqlite3.connect("/opt/new-api/data/one-api.db")
c = conn.cursor()
c.execute("SELECT id, name, `group`, status, models FROM channels LIMIT 5")
for r in c.fetchall():
    print(r[0], r[1], "group="+r[2], "status="+str(r[3]), "models="+r[4][:50])
conn.close()
