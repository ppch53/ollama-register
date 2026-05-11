import sqlite3
conn = sqlite3.connect("/opt/new-api/data/one-api.db")
c = conn.cursor()
c.execute("SELECT id, username, role, status FROM users ORDER BY id")
for r in c.fetchall():
    print(r)
conn.close()
