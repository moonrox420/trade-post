"""Typed exception hierarchy. Never raise bare `Exception`."""

from __future__ import annotations


class TradingPostError(Exception):
    """Root of the project exception tree. Catch this at the application boundary."""


class ConfigurationError(TradingPostError):
    """Startup configuration is invalid or missing."""


class DatabaseError(TradingPostError):
    """Persistence layer failure."""


class ExchangeError(TradingPostError):
    """Exchange adapter failure (network, auth, malformed response)."""

    def __init__(self, message: str, *, symbol: str | None = None,
                 original: Exception | None = None) -> None:
        super().__init__(message)
        self.symbol = symbol
        self.original = original


class RiskViolation(TradingPostError):
    """A trade was rejected by the risk engine."""

    def __init__(self, reason: str, *, code: str = "RISK_REJECTED") -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class AIProviderError(TradingPostError):
    """Ollama (or any future provider) failed in a recoverable way."""


class AIResponseInvalid(AIProviderError):
    """The provider returned something the system cannot safely interpret."""


class AICircuitOpen(AIProviderError):
    """The provider circuit breaker is open; no attempt was made."""


class OrderRejected(TradingPostError):
    """Exchange rejected the order. Includes idempotency-aware retries."""


class OrderNotFound(TradingPostError):
    """An order id was supplied that does not exist in the ledger."""


class StaleMarketData(TradingPostError):
    """Market snapshot is older than the configured staleness threshold."""


class AuthenticationError(TradingPostError):
    """Credentials were missing, invalid, or expired."""


class AuthorizationError(TradingPostError):
    """Authenticated user lacks the required role/permission."""
