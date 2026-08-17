"""Unit tests for the deterministic reconciliation service (PRD M5)."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trade_post.domain.models import ReconciliationOutcome
from trade_post.persistence.migrations import run_migrations
from trade_post.reconciliation.service import ReconciliationService

ACCOUNT = "test-wallet"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _insert_ledger(conn, delta, reference, balance_after) -> None:
    await conn.execute(
        text(
            "INSERT INTO ledger_entries"
            " (id, account_id, delta, currency, balance_after, type, reference,"
            " created_at, metadata)"
            " VALUES (:id, :a, :d, :c, :b, :t, :r, :ts, :m)"
        ),
        {
            "id": f"le_{reference}",
            "a": ACCOUNT,
            "d": str(delta),
            "c": "USDT",
            "b": str(balance_after),
            "t": "trade",
            "r": reference,
            "ts": _now(),
            "m": "{}",
        },
    )


async def _insert_fill(conn, side, qty, price, fee=0, fee_currency="USDT") -> None:
    await conn.execute(
        text(
            "INSERT INTO fills"
            " (id, order_id, exchange_order_id, symbol, side, quantity, price,"
            " fee_amount, fee_currency, liquidity, timestamp)"
            " VALUES (:id, :oid, :eoid, :sym, :side, :q, :p, :fa, :fc, :liq, :ts)"
        ),
        {
            "id": f"fill_{side}_{qty}_{price}",
            "oid": f"ord_{side}_{qty}_{price}",
            "eoid": f"ex_{side}_{qty}_{price}",
            "sym": "BTC/USDT",
            "side": side,
            "q": str(qty),
            "p": str(price),
            "fa": str(fee),
            "fc": fee_currency,
            "liq": "taker",
            "ts": _now(),
        },
    )


def _make_engine():
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = handle.name
    handle.close()
    return create_async_engine(f"sqlite+aiosqlite:///{db_path}"), db_path


def _run(coro):
    return asyncio.run(coro)


def test_reconciliation_passes_when_ledger_matches():
    engine, db_path = _make_engine()

    async def scenario():
        await run_migrations(engine)
        async with engine.begin() as conn:
            await _insert_fill(conn, "buy", "0.5", "60000", fee="0.50")
            await _insert_fill(conn, "sell", "0.5", "61000", fee="0.51")
            # Ledger: -30_000 (buy) + (30_500 - 1.01 fees) = +498.99
            await _insert_ledger(conn, "-30000.00", "buy-1", "-30000.00")
            await _insert_ledger(conn, "30498.99", "sell-net", "498.99")
        service = ReconciliationService(engine)
        return await service.reconcile(ACCOUNT)

    result = _run(scenario())
    try:
        assert result.passed is True
        assert result.outcome is ReconciliationOutcome.PASS
        assert result.ledger_balance == result.expected_balance
    finally:
        asyncio.run(engine.dispose())
        Path(db_path).unlink(missing_ok=True)


def test_reconciliation_fails_on_ledger_drift():
    engine, db_path = _make_engine()

    async def scenario():
        await run_migrations(engine)
        async with engine.begin() as conn:
            await _insert_fill(conn, "buy", "1.0", "10000", fee="0.50")
            # Correct ledger would be -10_000 - 0.50 = -10_000.50.
            await _insert_ledger(conn, "-10000.00", "buy-1", "-10000.00")
        service = ReconciliationService(engine)
        return await service.reconcile(ACCOUNT)

    result = _run(scenario())
    try:
        assert result.passed is False
        assert result.outcome is ReconciliationOutcome.FAIL
        assert len(result.mismatches) > 0
    finally:
        asyncio.run(engine.dispose())
        Path(db_path).unlink(missing_ok=True)


def test_reconciliation_persists_run():
    engine, db_path = _make_engine()

    async def scenario():
        await run_migrations(engine)
        async with engine.begin() as conn:
            await _insert_fill(conn, "buy", "0.1", "50000", fee="0.1")
            await _insert_ledger(conn, "-5000.10", "buy-1", "-5000.10")
        service = ReconciliationService(engine)
        result = await service.reconcile(ACCOUNT)
        async with engine.connect() as conn:
            row = (await conn.execute(text("SELECT result FROM reconciliations"))).first()
        return result, row

    result, row = _run(scenario())
    try:
        assert row is not None
        assert result.id
        assert "passed" in row.result
    finally:
        asyncio.run(engine.dispose())
        Path(db_path).unlink(missing_ok=True)
