"""Quick DB inspection — verifies admin user exists and password hash format."""
import sqlite3
from pathlib import Path

db = sqlite3.connect(Path("trade_post.db"))
print("Tables:", [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()])
print()
print("Users:")
for row in db.execute("SELECT id, username, role, length(password_hash), substr(password_hash, 1, 30) FROM users").fetchall():
    print(f"  {row}")
print()
print("Sessions:")
for row in db.execute("SELECT id, user_id, expires_at, revoked FROM sessions").fetchall():
    print(f"  {row}")
print()
print("Login attempts (last 5):")
for row in db.execute("SELECT username, success, attempted_at FROM login_attempts ORDER BY attempted_at DESC LIMIT 5").fetchall():
    print(f"  {row}")