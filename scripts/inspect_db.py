"""Quick DB inspection — verifies admin user exists and password hash format."""

import sqlite3
from pathlib import Path

db = sqlite3.connect(Path("trade_post.db"))
_tables_sql = "SELECT name FROM sqlite_master WHERE type='table'"
print("Tables:", [r[0] for r in db.execute(_tables_sql).fetchall()])
print()
print("Users:")
_users_sql = "SELECT id, username, role, length(password_hash), substr(password_hash, 1, 30) FROM users"
for row in db.execute(_users_sql).fetchall():
    print(f"  {row}")
print()
print("Sessions:")
for row in db.execute("SELECT id, user_id, expires_at, revoked FROM sessions").fetchall():
    print(f"  {row}")
print()
print("Login attempts (last 5):")
_attempts_sql = (
    "SELECT username, success, attempted_at FROM login_attempts ORDER BY attempted_at DESC LIMIT 5"
)
for row in db.execute(_attempts_sql).fetchall():
    print(f"  {row}")
