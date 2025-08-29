import sqlite3
db = 'network_tools.db'
conn = sqlite3.connect(db)
cur = conn.cursor()

# add devices.company_id
cur.execute("PRAGMA table_info(devices)")
cols = {r[1] for r in cur.fetchall()}
if 'company_id' not in cols:
    cur.execute("ALTER TABLE devices ADD COLUMN company_id INTEGER")

# create multi-tenant/auth tables if missing
cur.execute("CREATE TABLE IF NOT EXISTS companies (id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(128) UNIQUE NOT NULL, notes TEXT)")
cur.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, email VARCHAR(255) UNIQUE NOT NULL, password_hash VARCHAR(255) NOT NULL, is_superadmin BOOLEAN NOT NULL DEFAULT 0)")
cur.execute("CREATE TABLE IF NOT EXISTS user_companies (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, company_id INTEGER NOT NULL, role VARCHAR(32) NOT NULL DEFAULT 'viewer')")

conn.commit()
conn.close()
print("Migration done")
