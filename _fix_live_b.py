"""Throwaway: append live.py methods (part B)."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

PATH = Path("trade_post/execution/adapters/live.py")

CONTENT_B = '''
    async def send_order(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        price: Decimal | None,
        quantity: Decimal,
        idempotency_key: str,
        snapshot: "object | None" = None,
    ) -> PlaceOrderResult:
        exch = self._exchange()

        async def _place() -> dict:
            return await exch.create_order(
                symbol,
                order_type.value,
                side.value,
                float(quantity),
                float(price) if price is not None else None,
                {"clientOrderId": client_order_id},
            )

        try:
            raw = await self._run("create_order", _place)
        except ccxt.InsufficientFunds as exc:
            raise OrderRejected(f"insufficient funds on {symbol}: {exc}") from exc
        except ccxt.InvalidOrder as exc:
            raise OrderRejected(f"invalid order on {symbol}: {exc}") from exc

        status = map_order_status(str(raw.get("status")))
        filled = Decimal(str(raw.get("filled") or 0))
        avg = raw.get("average") or raw.get("price")
        return PlaceOrderResult(
            exchange_order_id=str(raw.get("id", "")),
            status=status,
            filled_quantity=filled,
            average_price=Decimal(str(avg)) if avg is not None else None,
            raw=dict(raw),
        )

    async def cancel_order(self, exchange_order_id: str, symbol: str) -> None:
        exch = self._exchange()

        async def _cancel() -> None:
            await exch.cancel_order(exchange_order_id, symbol)

        try:
            await self._run("cancel_order", _cancel)
        except ccxt.OrderNotFound as exc:
            raise ExchangeError(f"order not found to cancel: {exc}") from exc

    async def fetch_order(self, exchange_order_id: str) -> FetchOrderResult:
        exch = self._exchange()

        async def _fetch() -> dict:
            return await exch.fetch_order(exchange_order_id)

        raw = await self._run("fetch_order", _fetch)
        return FetchOrderResult(
            exchange_order_id=exchange_order_id,
            status=map_order_status(str(raw.get("status"))),
            filled_quantity=Decimal(str(raw.get("filled") or 0)),
            average_price=(
                Decimal(str(raw["average"])) if raw.get("average") is not None else None
            ),
        )

    async def fetch_balance(self) -> dict[str, Decimal]:
        exch = self._exchange()

        async def _fetch() -> dict:
            return await exch.fetch_balance()

        raw = await self._run("fetch_balance", _fetch)
        free = raw.get("free") or {}
        return {
            key: Decimal(str(value)) for key, value in free.items() if value is not None
        }

    async def fetch_open_orders(self) -> list[dict]:
        exch = self._exchange()

        async def _fetch() -> list:
            return await exch.fetch_open_orders()

        return await self._run("fetch_open_orders", _fetch)
'''

fd, tmp_path = tempfile.mkstemp(dir=str(PATH.parent), prefix=".live_tmp_", suffix=".py")
try:
    existing = PATH.read_text(encoding="utf-8")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(existing + CONTENT_B)
    os.replace(tmp_path, PATH)
except Exception:
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    raise

print("live.py part B appended")
