"""Throwaway helper: insert ledger/fills repository methods (part A)."""
from __future__ import annotations

from pathlib import Path

PATH = Path("trade_post/persistence/repository.py")
text = PATH.read_text(encoding="utf-8")
anchor = "\n\ndef _order_from_row(row):\n"
if anchor not in text:
    raise SystemExit("anchor not found")

methods = """
    async def insert_ledger_entry(self, entry: LedgerEntry) -> None:
        \"\"\"Persist one immutable ledger cash-flow line.\"\"\"
        await self._s.execute(
            text(
                "INSERT INTO ledger_entries (id, account_id, delta, currency,"
                " balance_after, type, reference, created_at, metadata)"
                " VALUES (:id, :a, :d, :c, :b, :t, :r, :ca, :m)"
            ),
            {
                "id": entry.id,
                "a": entry.account_id,
                "d": str(entry.delta),
                "c": entry.currency,
                "b": str(entry.balance_after),
                "t": entry.type.value,
                "r": entry.reference,
                "ca": entry.created_at.isoformat(),
                "m": json.dumps(entry.metadata or {}),
            },
        )

    async def list_ledger_entries(self, account_id: str, limit: int = 1000) -> list:
        \"\"\"Return the most recent ledger lines for an account, oldest first.\"\"\"
        rows = (
            await self._s.execute(
                text(
                    "SELECT id, account_id, delta, currency, balance_after, type,"
                    " reference, created_at, metadata FROM ledger_entries"
                    " WHERE account_id = :a ORDER BY created_at ASC LIMIT :n"
                ),
                {"a": account_id, "n": limit},
            )
        ).fetchall()
        return [
            {
                "id": r.id,
                "account_id": r.account_id,
                "delta": Decimal(r.delta),
                "currency": r.currency,
                "balance_after": Decimal(r.balance_after),
                "type": r.type,
                "reference": r.reference,
                "created_at": r.created_at,
                "metadata": json.loads(r.metadata or "{}"),
            }
            for r in rows
        ]

    async def ledger_balance(self, account_id: str) -> Decimal:
        \"\"\"Sum of signed ledger deltas for an account (Decimal, never float).\"\"\"
        rows = await self.list_ledger_entries(account_id)
        return sum((r["delta"] for r in rows), Decimal("0"))

    async def list_fills(self, limit: int = 1000) -> list[Fill]:
        \"\"\"Return the most recent fills for reconciliation analysis.\"\"\"
        rows = (
            await self._s.execute(
                text(
                    "SELECT id, order_id, exchange_order_id, symbol, side, quantity,"
                    " price, fee_amount, fee_currency, liquidity, timestamp"
                    " FROM fills ORDER BY timestamp ASC LIMIT :n"
                ),
                {"n": limit},
            )
        ).fetchall()
        return [
            Fill(
                id=r.id,
                order_id=r.order_id,
                exchange_order_id=r.exchange_order_id,
                symbol=r.symbol,
                side=OrderSide(r.side),
                quantity=Decimal(r.quantity),
                price=Decimal(r.price),
                fee=Money(amount=Decimal(r.fee_amount), currency=r.fee_currency),
                liquidity=r.liquidity,
                timestamp=datetime.fromisoformat(r.timestamp),
            )
            for r in rows
        ]

"""
text = text.replace(anchor, methods + anchor, 1)
PATH.write_text(text, encoding="utf-8")
print("part A ok")
