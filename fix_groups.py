import sqlite3
conn = sqlite3.connect("/opt/new-api/data/one-api.db")
c = conn.cursor()
c.execute("UPDATE channels SET `group` = ? WHERE `group` IN (?, ?)", ("default", "puter", "ollama"))
print("updated", c.rowcount)
conn.commit()
conn.close()
