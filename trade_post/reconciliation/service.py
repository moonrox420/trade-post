"""Deterministic ledger reconciliation (PRD M5).

Verifies the money ledger equation ``ledger_balance == sum(fill cash) - fees``
entirely in ``Decimal`` and records each run's outcome plus Prometheus metrics.
A BUY consumes cash, a SELL produces cash; fees are debited. Passes only when
the absolute difference is within an explicit tolerance.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ..core.errors import DatabaseError
from ..domain.models import ReconciliationOutcome, ReconciliationResult
from ..observability.metrics import metrics

log = logging.getLogger(__name__)

_DEFAULT_TOLERANCE = Decimal("0.01")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class ReconciliationService:
    """Runs the deterministic reconciliation job over a bound engine.

    Opens its own short-lived, committed sessions (mirroring the risk engine's
    pattern) so reconciliation never depends on a stale startup session.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def reconcile(self, account_id: str = "system-wallet") -> ReconciliationResult:
        """Run one deterministic reconciliation and persist its outcome."""
        started = _now_utc()
        mismatches: list[str] = []

        async with self._engine.begin() as conn:
            ledger_rows = (
                await conn.execute(
                    text("SELECT delta FROM ledger_entries WHERE account_id = :a ORDER BY created_at ASC"),
                    {"a": account_id},
                )
            ).fetchall()
            fill_rows = (
                await conn.execute(
                    text(
                        "SELECT side, quantity, price, fee_amount, fee_currency"
                        " FROM fills ORDER BY timestamp ASC"
                    )
                )
            ).fetchall()

        ledger_balance = sum((Decimal(r.delta) for r in ledger_rows), Decimal("0"))
        trade_cash = Decimal("0")
        total_fees = Decimal("0")
        for row in fill_rows:
            notional = Decimal(row.quantity) * Decimal(row.price)
            trade_cash = trade_cash - notional if row.side == "buy" else trade_cash + notional
            if row.fee_currency == "USDT":
                total_fees += Decimal(row.fee_amount)
        expected = trade_cash - total_fees
        difference = ledger_balance - expected
        passed = abs(difference) <= _DEFAULT_TOLERANCE

        if not passed:
            mismatches.append(f"ledger={ledger_balance} expected={expected} diff={difference}")
            log.error(
                "RECONCILIATION MISMATCH account=%s ledger=%s expected=%s diff=%s",
                account_id,
                ledger_balance,
                expected,
                difference,
            )

        result = ReconciliationResult(
            run_started_at=started,
            run_ended_at=_now_utc(),
            outcome=(ReconciliationOutcome.PASS if passed else ReconciliationOutcome.FAIL),
            ledger_balance=ledger_balance,
            expected_balance=expected,
            tolerance=_DEFAULT_TOLERANCE,
            mismatches=mismatches,
            passed=passed,
        )

        metrics.inc(
            "trade_post_reconciliation_runs_total",
            1,
            (("result", result.outcome.value),),
        )
        if not passed:
            metrics.inc("trade_post_reconciliation_mismatch_errors_total", 1)

        try:
            await self._persist(account_id, result)
        except DatabaseError as exc:
            log.error("failed to persist reconciliation result: %s", exc)
            raise

        log.info(
            "reconciliation %s ledger=%s expected=%s passed=%s",
            result.outcome.value,
            ledger_balance,
            expected,
            passed,
        )
        return result

    async def _persist(self, account_id: str, result: ReconciliationResult) -> None:
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO reconciliations"
                        " (id, run_started_at, run_ended_at, result)"
                        " VALUES (:id, :start, :end, :result)"
                    ),
                    {
                        "id": result.id,
                        "start": result.run_started_at.isoformat(),
                        "end": (result.run_ended_at or result.run_started_at).isoformat(),
                        "result": json.dumps(
                            {
                                "account_id": account_id,
                                "outcome": result.outcome.value,
                                "ledger_balance": str(result.ledger_balance),
                                "expected_balance": str(result.expected_balance),
                                "mismatches": result.mismatches,
                                "passed": result.passed,
                            }
                        ),
                    },
                )
        except Exception as exc:  # noqa: BLE001
            raise DatabaseError(f"reconciliation persist failed: {exc}") from exc
