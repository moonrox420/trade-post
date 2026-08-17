"""Domain models. Money is always Decimal. DTOs are explicit and immutable where possible."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


def _new_id() -> str:
    return uuid.uuid4().hex


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class SignalSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    ORPHANED = "orphaned"


class TimeInForce(str, Enum):
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


class Money(BaseModel):
    model_config = ConfigDict(frozen=True)

    amount: Decimal
    currency: str = "USDT"

    @classmethod
    def zero(cls, currency: str = "USDT") -> Money:
        return cls(amount=Decimal("0"), currency=currency)

    def __add__(self, other: Money) -> Money:
        self._assert_same_currency(other)
        return Money(amount=self.amount + other.amount, currency=self.currency)

    def __sub__(self, other: Money) -> Money:
        self._assert_same_currency(other)
        return Money(amount=self.amount - other.amount, currency=self.currency)

    def __mul__(self, factor) -> Money:
        return Money(amount=(self.amount * Decimal(str(factor))), currency=self.currency)

    def __lt__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.amount <= other.amount

    def _assert_same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise ValueError(f"Currency mismatch: {self.currency} vs {other.currency}")

    def to_float(self) -> float:
        return float(self.amount)


class MarketSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    timestamp: datetime
    last_price: Decimal
    bid: Decimal | None = None
    ask: Decimal | None = None
    spread_bps: Decimal | None = None
    volume_24h: Decimal | None = None
    indicators: dict = Field(default_factory=dict)
    source: str = "ccxt"


class OrderIntent(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=_new_id)
    symbol: str
    side: OrderSide
    type: OrderType = OrderType.MARKET
    quantity: Decimal = Field(gt=Decimal("0"))
    limit_price: Decimal | None = None
    stop_loss_pct: Decimal | None = Field(default=None, gt=Decimal("0"), le=Decimal("20"))
    take_profit_pct: Decimal | None = Field(default=None, gt=Decimal("0"), le=Decimal("100"))
    time_in_force: TimeInForce = TimeInForce.GTC
    idempotency_key: str
    strategy_id: str
    signal: SignalSide
    conviction: int = Field(ge=1, le=10)
    rationale: str
    created_at: datetime = Field(default_factory=_now_utc)
    client_request_id: str | None = None


class Fill(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=_new_id)
    order_id: str
    exchange_order_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal = Field(gt=Decimal("0"))
    price: Decimal = Field(gt=Decimal("0"))
    fee: Money
    liquidity: str
    timestamp: datetime = Field(default_factory=_now_utc)


class Order(BaseModel):
    id: str
    intent_id: str
    exchange_order_id: str | None = None
    symbol: str
    side: OrderSide
    type: OrderType
    quantity: Decimal
    filled_quantity: Decimal = Decimal("0")
    average_price: Decimal | None = None
    status: OrderStatus = OrderStatus.PENDING
    limit_price: Decimal | None = None
    stop_loss_pct: Decimal | None = None
    take_profit_pct: Decimal | None = None
    idempotency_key: str
    strategy_id: str
    signal: SignalSide
    conviction: int
    rationale: str
    created_at: datetime
    submitted_at: datetime | None = None
    completed_at: datetime | None = None
    last_error: str | None = None
    trace_id: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        )

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity


class Position(BaseModel):
    symbol: str
    side: SignalSide
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Money
    leverage: Decimal
    liquidation_price: Decimal | None = None
    opened_at: datetime
    updated_at: datetime


class PortfolioSnapshot(BaseModel):
    timestamp: datetime
    total_equity: Money
    available_margin: Money
    positions: list
    base_balances: dict
    risk_adjusted_equity: Money
    margin_utilization: Decimal
    drawdown_pct: Decimal = Decimal("0")


class RiskDecision(BaseModel):
    approved: bool
    reason: str
    code: str
    requested_size_usd: Money
    capped_size_usd: Money | None = None
    checks: dict = Field(default_factory=dict)


class RiskState(BaseModel):
    killed: bool = False
    kill_reason: str | None = None
    killed_at: datetime | None = None
    starting_equity: Money | None = None
    daily_realized_pnl: Money = Money.zero()
    session_id: str
    failures_in_window: int = 0
    circuit_open: bool = False
    last_failure_at: datetime | None = None


class StrategySignal(BaseModel):
    symbol: str
    signal: SignalSide
    strength: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    features: dict
    rationale: str
    timestamp: datetime = Field(default_factory=_now_utc)


class AIDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=_new_id)
    symbol: str
    signal: SignalSide
    conviction: int = Field(ge=1, le=10)
    confidence: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    rationale: str
    raw_output: dict
    model: str
    prompt_version: str
    schema_version: str = "v1"
    validated: bool
    validation_errors: list = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=_now_utc)
    trace_id: str | None = None


class StrategyEvaluation(BaseModel):
    id: str = Field(default_factory=_new_id)
    decision_id: str
    order_id: str
    symbol: str
    entry_price: Decimal
    exit_price: Decimal
    realized_pnl: Money
    return_bps: Decimal
    hold_duration_sec: int
    score: int = Field(ge=1, le=10)
    critique: str
    timestamp: datetime = Field(default_factory=_now_utc)


class EventSeverity(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class Event(BaseModel):
    id: str = Field(default_factory=_new_id)
    timestamp: datetime = Field(default_factory=_now_utc)
    type: str
    severity: EventSeverity = EventSeverity.INFO
    actor: str = "system"
    payload: dict = Field(default_factory=dict)
    trace_id: str | None = None
    session_id: str | None = None


class Role(str, Enum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


class User(BaseModel):
    id: str = Field(default_factory=_new_id)
    username: str = Field(min_length=3, max_length=64)
    email: str | None = None
    password_hash: str
    role: Role = Role.VIEWER
    created_at: datetime = Field(default_factory=_now_utc)
    last_login_at: datetime | None = None
    failed_login_count: int = 0
    locked_until: datetime | None = None


class Session(BaseModel):
    id: str = Field(default_factory=_new_id)
    user_id: str
    issued_at: datetime = Field(default_factory=_now_utc)
    expires_at: datetime
    ip: str
    user_agent: str
    revoked: bool = False


class LedgerEntryType(str, Enum):
    """Categorisation of a cash-flow line on the money ledger."""

    TRADE = "trade"
    FEE = "fee"
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    ADJUSTMENT = "adjustment"


class LedgerEntry(BaseModel):
    """An auditable, immutable cash-flow line. Money is always ``Decimal``."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=_new_id)
    account_id: str
    delta: Decimal  # signed: positive credits, negative debits the account
    currency: str = "USDT"
    balance_after: Decimal
    type: LedgerEntryType = LedgerEntryType.TRADE
    reference: str
    created_at: datetime = Field(default_factory=_now_utc)
    metadata: dict = Field(default_factory=dict)


class ReconciliationOutcome(str, Enum):
    """Whether a reconciliation run satisfied the ledger equation."""

    PASS = "pass"
    FAIL = "fail"


class ReconciliationResult(BaseModel):
    """Deterministic outcome of a reconciliation run."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=_new_id)
    run_started_at: datetime = Field(default_factory=_now_utc)
    run_ended_at: datetime | None = None
    outcome: ReconciliationOutcome
    ledger_balance: Decimal = Decimal("0")
    expected_balance: Decimal = Decimal("0")
    tolerance: Decimal = Decimal("0.01")
    mismatches: list = Field(default_factory=list)
    passed: bool = False


def idempotency_key_for(
    symbol: str,
    side,
    type_,
    quantity,
    strategy_id: str,
    ts_window_sec: int = 60,
    now: datetime | None = None,
) -> str:
    now = now or _now_utc()
    bucket = int(now.timestamp()) // ts_window_sec
    payload = f"{symbol}|{side.value}|{type_.value}|{quantity}|{strategy_id}|{bucket}"
    return hashlib.sha256(payload.encode()).hexdigest()


def positive_decimal(value, field: str) -> Decimal:
    d = Decimal(str(value))
    if d <= 0:
        raise ValueError(f"{field} must be positive")
    return d
