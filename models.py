import uuid
import hashlib
from enum import Enum
from datetime import datetime
from typing import Any, List
from pydantic import BaseModel, Field, field_validator, ConfigDict, model_validator
from config import POSITION_SIZE_HARD_CAP


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class SignalSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class MarketSnapshot(BaseModel):
    symbol: str
    timestamp: datetime
    last_price: float
    bid: float
    ask: float
    volume: float
    indicators: dict[str, Any] = {}


class OrderSnapshot(BaseModel):
    order_id: str
    symbol: str
    side: OrderSide
    amount: float
    price: float | None
    status: str
    filled: float
    timestamp: datetime
    proposal_price: float | None = None
    slippage_adjusted_notional: float = 0.0

    @model_validator(mode="after")
    def validate_slippage(self) -> "OrderSnapshot":
        if (
            self.price is not None
            and self.proposal_price is not None
            and self.proposal_price > 0
        ):
            deviation = abs(self.price - self.proposal_price) / self.proposal_price
            if deviation > 0.05:
                raise ValueError(f"Slippage violation: {deviation:.2%}")
        return self


class PositionSnapshot(BaseModel):
    symbol: str
    side: SignalSide
    entry_price: float
    notional: float
    unrealized_pnl: float
    leverage: float
    contracts: float
    timestamp: datetime


class PortfolioSnapshot(BaseModel):
    timestamp: datetime
    total_equity: float
    available_margin: float
    positions: List[PositionSnapshot]
    base_balances: dict[str, float]
    risk_adjusted_equity: float
    margin_utilization: float


class PerformanceReport(BaseModel):
    report_period: str
    total_evaluations: int
    average_score: float
    net_performance_bps: float
    executive_summary: str
    key_learnings: List[str]


class QualitativeEvaluation(BaseModel):
    score: int = Field(ge=1, le=10)
    critique: str


class SymbolPrioritization(BaseModel):
    prioritized_symbols: List[str]


class StrategyProposal(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    proposal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    symbol: str = Field(..., pattern=r"^[A-Z0-9]+/[A-Z0-9]+$")
    side: SignalSide = Field(..., alias="signal")
    amount: float = Field(gt=0)
    price: float | None = None
    order_type: str = Field(default="limit", pattern="^(market|limit)$")
    conviction: int = Field(ge=1, le=10)
    rationale: str
    trailing_stop_pct: float | None = Field(default=None, gt=0.1, le=5.0)
    market_snapshot_id: str | None = None

    @field_validator("amount")
    @classmethod
    def validate_amount_safety(cls, value: float) -> float:
        if value > POSITION_SIZE_HARD_CAP:
            raise ValueError(f"Exceeds cap: {POSITION_SIZE_HARD_CAP}")
        return value

    def get_idempotency_key(self) -> str:
        payload = f"{self.symbol}:{self.side}:{self.amount}:{self.order_type}"
        return hashlib.sha256(payload.encode()).hexdigest()
