"""One-shot admin password reset / bootstrap. Run: python -m scripts.reset_admin_password

If no admin user exists, creates one using DROX_ADMIN_PASSWORD (via
settings/config or the environment). If an admin already exists, rotates its
password. Never hard-codes a default credential: when no password is supplied a
one-time random password is generated and printed to the console for manual
capture. After this, log in at the dashboard with admin / <password>.
"""

import asyncio
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trade_post.core.config import load_settings
from trade_post.core.logging_setup import configure_logging
from trade_post.persistence.database import init_database
from trade_post.persistence.migrations import run_migrations
from trade_post.persistence.repository import Repository
from trade_post.security.auth import hash_password


async def main() -> None:
    settings = load_settings()
    configure_logging(settings)
    new_password = settings.drox_admin_password or __import__("os").environ.get("DROX_ADMIN_PASSWORD")
    generated = False
    if not new_password:
        new_password = secrets.token_urlsafe(18)
        generated = True
    db = init_database(settings)
    await db.connect()
    try:
        await run_migrations(db.engine)
        async with db.session() as s:
            repo = Repository(s)
            user = await repo.get_user_by_username("admin")
            if user is None:
                await repo.insert_user(
                    id=secrets.token_hex(8),
                    username="admin",
                    email=None,
                    password_hash=hash_password(new_password, settings),
                    role="admin",
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
                print("Admin user created.")
            else:
                await repo.update_user_password(user["id"], hash_password(new_password, settings))
                print("Admin password rotated.")
        if generated:
            print(f"One-time admin password (shown once): {new_password}")
        else:
            print("Admin password is set from DROX_ADMIN_PASSWORD.")
        print("Log in at http://localhost:8065 with admin / <password>")
    finally:
        await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
