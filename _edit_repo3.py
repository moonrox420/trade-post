"""Throwaway helper: insert reconciliation/idempotency repository methods (part B)."""
from __future__ import annotations

from pathlib import Path

PATH = Path("trade_post/persistence/repository.py")
text = PATH.read_text(encoding="utf-8")
anchor = "\n\ndef _order_from_row(row):\n"
if anchor not in text:
    raise SystemExit("anchor not found")

methods = """
    async def insert_reconciliation(self, result: ReconciliationResult) -> None:
        \"\"\"Record a reconciliation run outcome (result serialised as JSON).\"\"\"
        import json as _json

        await self._s.execute(
            text(
                "INSERT INTO reconciliations (id, run_started_at, run_ended_at, result)"
                " VALUES (:id, :start, :end, :result)"
            ),
            {
                "id": result.id,
                "start": result.run_started_at.isoformat(),
                "end": (result.run_ended_at or result.run_started_at).isoformat(),
                "result": _json.dumps(
                    {
                        "outcome": result.outcome.value,
                        "ledger_balance": str(result.ledger_balance),
                        "expected_balance": str(result.expected_balance),
                        "mismatches": result.mismatches,
                        "passed": result.passed,
                    }
                ),
            },
        )

    async def list_reconciliations(self, limit: int = 20) -> list[dict]:
        \"\"\"Return the most recent reconciliation outcomes (most recent first).\"\"\"
        import json as _json

        rows = (
            await self._s.execute(
                text(
                    "SELECT id, run_started_at, run_ended_at, result"
                    " FROM reconciliations ORDER BY run_started_at DESC LIMIT :n"
                ),
                {"n": limit},
            )
        ).fetchall()
        return [
            {
                "id": r.id,
                "run_started_at": r.run_started_at,
                "run_ended_at": r.run_ended_at,
                "result": _json.loads(r.result or "{}"),
            }
            for r in rows
        ]

    async def claim_idempotency(
        self, key: str, used_by: str, expiration: datetime
    ) -> bool:
        \"\"\"Atomically reserve an idempotency key. Returns True if newly claimed.\"\"\"
        from sqlalchemy import inspect

        if inspect(self._s.bind).dialect.name == "postgresql":
            result = await self._s.execute(
                text(
                    "INSERT INTO idempotency_keys (key, created_at, used_by, expiration)"
                    " VALUES (:k, :ca, :u, :e) ON CONFLICT (key) DO NOTHING"
                ),
                {"k": key, "ca": datetime.utcnow().isoformat(),
                 "u": used_by, "e": expiration.isoformat()},
            )
            return result.rowcount == 1
        result = await self._s.execute(
            text(
                "INSERT OR IGNORE INTO idempotency_keys"
                " (key, created_at, used_by, expiration)"
                " VALUES (:k, :ca, :u, :e)"
            ),
            {"k": key, "ca": datetime.utcnow().isoformat(),
             "u": used_by, "e": expiration.isoformat()},
        )
        return int(cast(CursorResult, result).rowcount) == 1

    async def idempotency_used(self, key: str) -> bool:
        \"\"\"Return True when an idempotency key has already been claimed.\"\"\"
        row = (
            await self._s.execute(
                text("SELECT 1 FROM idempotency_keys WHERE key = :k"), {"k": key}
            )
        ).first()
        return row is not None

"""
text = text.replace(anchor, methods + anchor, 1)
PATH.write_text(text, encoding="utf-8")
print("part B ok")
