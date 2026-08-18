"""Domain-level configuration. Validated at startup. Fails fast on bad config."""

from __future__ import annotations

import secrets
from enum import Enum
from typing import Literal, Self

from pydantic import Field, HttpUrl, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(str, Enum):
    """Whether the engine submits real orders to a live exchange or simulates in-memory."""

    PAPER = "paper"
    LIVE = "live"


class ExchangeId(str, Enum):
    """Supported exchange identifiers. Keep narrow: each is a real boundary."""

    KRAKEN = "kraken"
    BINANCE = "binance"
    BYBIT = "bybit"
    COINBASE = "coinbase"


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class Settings(BaseSettings):
    """Authoritative runtime configuration. All access goes through this object."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="_",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Drox Trade Post"
    app_env: Literal["development", "staging", "production"] = "development"
    instance_id: str = Field(default_factory=lambda: secrets.token_hex(8))

    host: str = "0.0.0.0"
    port: int = 8065
    public_host: str = "localhost"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:8065"])

    database_url: str = "sqlite+aiosqlite:///./trade_post.db"
    redis_url: str | None = None

    exchange_id: ExchangeId = ExchangeId.KRAKEN
    exchange_api_key: str = ""
    exchange_api_secret: str = ""
    exchange_sandbox: bool = True
    exchange_max_retries: int = Field(default=3, ge=0, le=20)
    exchange_retry_base_sec: float = Field(default=1.0, ge=0.0, le=60.0)

    ollama_url: HttpUrl = HttpUrl("http://localhost:11434")
    ollama_api_key: str = ""
    ollama_model: str = "gpt-oss:120b-cloud"
    ollama_timeout_sec: float = 30.0
    ollama_max_retries: int = 3
    ollama_circuit_breaker_threshold: int = 5
    ollama_circuit_breaker_cooldown_sec: float = 60.0
    ollama_max_concurrent: int = 4

    trading_mode: TradingMode = TradingMode.PAPER
    paper_initial_equity: float = 10_000.0

    max_position_pct: float = Field(default=2.0, ge=0.01, le=100.0)
    max_portfolio_exposure_pct: float = Field(default=50.0, ge=0.1, le=1000.0)
    max_daily_loss_pct: float = Field(default=1.0, ge=0.01, le=100.0)
    max_per_trade_risk_pct: float = Field(default=0.5, ge=0.01, le=100.0)
    max_open_orders: int = Field(default=8, ge=1, le=1000)
    max_price_deviation_pct: float = Field(default=2.0, ge=0.0, le=100.0)
    max_spread_pct: float = Field(default=1.0, ge=0.0, le=100.0)
    max_stale_data_sec: int = Field(default=30, ge=1, le=3600)
    position_size_hard_cap_usd: float = Field(default=1000.0, ge=0.0)
    slippage_bps: float = Field(default=5.0, ge=0.0, le=1000.0)

    per_trade_risk_pct: float = Field(default=0.5, ge=0.01, le=10.0)
    atr_period: int = Field(default=14, ge=2, le=200)
    atr_stop_multiplier: float = Field(default=2.0, ge=0.1, le=20.0)

    orphan_age_threshold_sec: int = Field(default=300, ge=1, le=86_400)
    circuit_breaker_failure_threshold: int = Field(default=3, ge=1, le=1000)
    circuit_breaker_window_sec: int = Field(default=60, ge=1, le=86_400)
    recovery_cooloff_sec: int = Field(default=300, ge=1, le=86_400)

    market_data_poll_sec: int = Field(default=15, ge=1, le=600)
    market_data_ohlcv_limit: int = Field(default=200, ge=20, le=5000)
    subscribed_symbols: list[str] = Field(default_factory=lambda: ["BTC/USDT", "ETH/USDT"])

    session_secret: str = Field(default_factory=lambda: secrets.token_urlsafe(48))
    session_ttl_minutes: int = Field(default=480, ge=1, le=43_200)
    bcrypt_work_factor: int = Field(default=12, ge=4, le=16)
    login_max_attempts_per_ip: int = Field(default=8, ge=1, le=1000)
    login_lockout_minutes: int = Field(default=15, ge=1, le=10_080)
    password_min_length: int = Field(default=10, ge=8, le=128)

    # Bootstrap administrator credential. When None and no admin user exists
    # at startup, a one-time random password is generated and logged exactly
    # once at WARNING level. Never hard-code a production password here.
    drox_admin_password: str | None = None

    log_level: LogLevel = LogLevel.INFO
    enable_metrics: bool = True
    enable_prometheus_endpoint: bool = False

    enable_chaos_test: bool = False
    enable_demo_seeding: bool = False

    @field_validator("session_secret")
    @classmethod
    def _warn_default_secret(
        cls,
        value: str,
    ) -> str:
        if len(value) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 characters")
        return value

    @model_validator(mode="after")
    def _validate_live_mode_safety(self) -> Self:
        if self.trading_mode is TradingMode.LIVE:
            if not self.exchange_api_key or not self.exchange_api_secret:
                raise ValueError("Live trading requires EXCHANGE_API_KEY and EXCHANGE_API_SECRET")
        return self

    @property
    def is_paper(self) -> bool:
        return self.trading_mode is TradingMode.PAPER

    @property
    def has_exchange_credentials(self) -> bool:
        return bool(self.exchange_api_key and self.exchange_api_secret)

    @property
    def base_url(self) -> str:
        return f"http://{self.public_host}:{self.port}"

    @property
    def ollama_chat_url(self) -> str:
        base = str(self.ollama_url).rstrip("/")
        return f"{base}/api/chat"

    @property
    def ollama_tags_url(self) -> str:
        base = str(self.ollama_url).rstrip("/")
        return f"{base}/api/tags"


def load_settings() -> Settings:
    """Public entry point. Raises on invalid configuration; never returns a half-built object."""
    return Settings()  # type: ignore[call-arg]
