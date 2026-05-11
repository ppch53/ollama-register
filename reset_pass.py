import sqlite3
import bcrypt

conn = sqlite3.connect("/opt/new-api/data/one-api.db")
c = conn.cursor()

# generate bcrypt hash for AdminPass2026!
hashed = bcrypt.hashpw(b"AdminPass2026!", bcrypt.gensalt()).decode()
print("hash:", hashed[:30] + "...")

c.execute("UPDATE users SET password = ? WHERE id = 1", (hashed,))
conn.commit()

# verify
c.execute("SELECT password FROM users WHERE id=1")
row = c.fetchone()
print("db val:", row[0][:30] + "...")

conn.close()
print("done")
