import sqlite3
conn = sqlite3.connect("/opt/new-api/data/one-api.db")
c = conn.cursor()
c.execute("SELECT model_mapping FROM channels WHERE id=1")
row = c.fetchone()
print("model_mapping:", repr(row[0]))
conn.close()
