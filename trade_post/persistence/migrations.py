"""Schema migrations. Idempotent, versioned, append-only."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

log = logging.getLogger(__name__)


SCHEMA_SQL = [
    "CREATE TABLE IF NOT EXISTS schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, email TEXT, password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'viewer', created_at TEXT NOT NULL, last_login_at TEXT, failed_login_count INTEGER NOT NULL DEFAULT 0, locked_until TEXT)",
    "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, issued_at TEXT NOT NULL, expires_at TEXT NOT NULL, ip TEXT NOT NULL, user_agent TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)",
    "CREATE TABLE IF NOT EXISTS login_attempts (id INTEGER PRIMARY KEY AUTOINCREMENT, ip TEXT NOT NULL, username TEXT NOT NULL, success INTEGER NOT NULL, attempted_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip, attempted_at)",
    "CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, type TEXT NOT NULL, severity TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, trace_id TEXT, session_id TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)",
    "CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, intent_id TEXT NOT NULL, exchange_order_id TEXT, symbol TEXT NOT NULL, side TEXT NOT NULL, type TEXT NOT NULL, quantity TEXT NOT NULL, filled_quantity TEXT NOT NULL, average_price TEXT, status TEXT NOT NULL, limit_price TEXT, stop_loss_pct TEXT, take_profit_pct TEXT, idempotency_key TEXT NOT NULL UNIQUE, strategy_id TEXT NOT NULL, signal TEXT NOT NULL, conviction INTEGER NOT NULL, rationale TEXT NOT NULL, created_at TEXT NOT NULL, submitted_at TEXT, completed_at TEXT, last_error TEXT, trace_id TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)",
    "CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at)",
    "CREATE TABLE IF NOT EXISTS fills (id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id) ON DELETE CASCADE, exchange_order_id TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL, price TEXT NOT NULL, fee_amount TEXT NOT NULL, fee_currency TEXT NOT NULL, liquidity TEXT NOT NULL, timestamp TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id)",
    "CREATE TABLE IF NOT EXISTS portfolio_snapshots (id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, total_equity TEXT NOT NULL, available_margin TEXT NOT NULL, positions TEXT NOT NULL, base_balances TEXT NOT NULL, risk_adjusted_equity TEXT NOT NULL, margin_utilization TEXT NOT NULL, drawdown_pct TEXT NOT NULL DEFAULT '0')",
    "CREATE INDEX IF NOT EXISTS idx_portfolio_ts ON portfolio_snapshots(timestamp)",
    "CREATE TABLE IF NOT EXISTS market_snapshots (id TEXT PRIMARY KEY, symbol TEXT NOT NULL, timestamp TEXT NOT NULL, last_price TEXT NOT NULL, bid TEXT, ask TEXT, spread_bps TEXT, volume_24h TEXT, indicators TEXT, source TEXT NOT NULL DEFAULT 'ccxt')",
    "CREATE INDEX IF NOT EXISTS idx_market_symbol_ts ON market_snapshots(symbol, timestamp)",
    "CREATE TABLE IF NOT EXISTS ai_decisions (id TEXT PRIMARY KEY, symbol TEXT NOT NULL, signal TEXT NOT NULL, conviction INTEGER NOT NULL, confidence TEXT NOT NULL, rationale TEXT NOT NULL, raw_output TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL, validated INTEGER NOT NULL, validation_errors TEXT NOT NULL, timestamp TEXT NOT NULL, trace_id TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_ai_symbol_ts ON ai_decisions(symbol, timestamp)",
    "CREATE TABLE IF NOT EXISTS strategy_evaluations (id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, order_id TEXT NOT NULL, symbol TEXT NOT NULL, entry_price TEXT NOT NULL, exit_price TEXT NOT NULL, realized_pnl_amount TEXT NOT NULL, realized_pnl_currency TEXT NOT NULL DEFAULT 'USDT', return_bps TEXT NOT NULL, hold_duration_sec INTEGER NOT NULL, score INTEGER NOT NULL, critique TEXT NOT NULL, timestamp TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS risk_state (id INTEGER PRIMARY KEY, killed INTEGER NOT NULL DEFAULT 0, kill_reason TEXT, killed_at TEXT, starting_equity TEXT, daily_realized_pnl TEXT NOT NULL DEFAULT '0', session_id TEXT NOT NULL, failures_in_window INTEGER NOT NULL DEFAULT 0, circuit_open INTEGER NOT NULL DEFAULT 0, last_failure_at TEXT)",
    "CREATE TABLE IF NOT EXISTS ai_circuit_state (id INTEGER PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0, last_failure_at TEXT, opened_at TEXT)",
]


async def run_migrations(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for stmt in SCHEMA_SQL:
            await conn.execute(text(stmt))
        # Mark schema version (single version, full schema is atomic).
        await conn.execute(
            text("INSERT OR IGNORE INTO schema_migrations(id, applied_at) VALUES(:id, :ts)"),
            {"id": "0001_init", "ts": datetime.now(timezone.utc).isoformat()},
        )
    log.info("migrations complete")
