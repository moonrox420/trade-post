"""Throwaway verification of the new migrations against a fresh SQLite DB."""
from __future__ import annotations

import asyncio
import os
import secrets

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from trade_post.persistence.migrations import run_migrations

EXPECTED_TABLES = [
    "ledger_entries",
    "positions",
    "idempotency_keys",
    "reconciliations",
    "orders",
    "users",
    "events",
    "ai_decisions",
]


async def main() -> None:
    db_path = f"verify_{secrets.token_hex(4)}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///./{db_path}")
    try:
        await run_migrations(engine)
        async with engine.connect() as conn:
            for table in EXPECTED_TABLES:
                row = await conn.execute(
                    text(
                        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=:n"
                    ),
                    {"n": table},
                )
                present = bool(row.scalar())
                print(f"{table}: {'OK' if present else 'MISSING'}")
            # Confirm ledger money type resolved as TEXT on sqlite.
            row = await conn.execute(
                text("SELECT type FROM pragma_table_info('ledger_entries') "
                     "WHERE name='delta'")
            )
            print("ledger_entries.delta type:", row.scalar())
    finally:
        await engine.dispose()
        if os.path.exists(db_path):
            os.remove(db_path)


if __name__ == "__main__":
    asyncio.run(main())
