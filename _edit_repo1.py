"""Throwaway helper: patch repository.py imports."""
from __future__ import annotations

from pathlib import Path

PATH = Path("trade_post/persistence/repository.py")
text = PATH.read_text(encoding="utf-8")

old_import = (
    "    MarketSnapshot,\n    Money,\n    Order,\n    OrderSide,\n"
    "    OrderStatus,\n    OrderType,\n    PortfolioSnapshot,\n"
    "    RiskState,\n    SignalSide,\n)"
)
new_import = (
    "    MarketSnapshot,\n    Money,\n    Order,\n    OrderSide,\n"
    "    OrderStatus,\n    OrderType,\n    PortfolioSnapshot,\n"
    "    ReconciliationResult,\n    RiskState,\n    SignalSide,\n    LedgerEntry,\n)"
)
if old_import not in text:
    raise SystemExit("import anchor not found")
text = text.replace(old_import, new_import, 1)
PATH.write_text(text, encoding="utf-8")
print("imports patched ok")
