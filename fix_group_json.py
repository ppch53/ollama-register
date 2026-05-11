import sqlite3
conn = sqlite3.connect("/opt/new-api/data/one-api.db")
c = conn.cursor()

# Change group from plain string 'default' to JSON array '["default"]'
c.execute("UPDATE channels SET `group` = ? WHERE `group` = ?", ('["default"]', 'default'))
print("updated", c.rowcount, "channels")
conn.commit()

# verify
c.execute("SELECT id, `group` FROM channels LIMIT 3")
for r in c.fetchall():
    print(r[0], repr(r[1]))
conn.close()
