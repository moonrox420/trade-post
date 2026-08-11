# Drox Trade Post

**A local-first, AI-assisted algorithmic trading system.**

Drox Trade Post is a production-grade trading engine built around a hardened
Ollama (local) AI brain, a deterministic risk pipeline, and a money-safe
ledger. Everything is yours: no cloud dependencies, no Google AI, no
Firestore. Real auth, real metrics, real data — every value on the dashboard
comes from the database or the exchange.

## Highlights

- **100% local AI via Ollama.** No cloud AI. No Vertex. No Firestore. Circuit
  breaker, retries, JSON guard, deterministic fallback.
- **Money-safe by default.** Every monetary value is `Decimal`. The ledger
  uses parameterized SQL with explicit migrations. No binary float anywhere
  in the financial path.
- **Real authentication.** PBKDF2-hashed passwords, server-side session
  cookies, brute-force logging, role-based access (`viewer` / `operator` /
  `admin`).
- **Hardened risk engine.** Daily drawdown cap, position-size caps,
  volatility-aware ATR sizing, spread/staleness guards, kill switch,
  circuit breaker. Fail-closed on missing risk subsystem.
- **Real-time dashboard.** WebSocket-driven. Renders real data fetched from
  `/api/v1/*` — no fabricated values.
- **Decoupled orchestration.** Background loops, async DB, structured
  shutdown, lifespan-managed lifespan.
- **Observable.** Trace IDs on every log line, Prometheus-style `/metrics`
  endpoint, structured events table for audit.

## Architecture

```
trade_post/                     # The Python package
├── core/                       # config, errors, logging
├── domain/                     # Pydantic models (Decimal money)
├── persistence/                # async SQLAlchemy, Repository, migrations
├── market/                     # CCXT adapter, indicator library
├── strategy/                   # deterministic signal pipeline
├── ai/                         # hardened Ollama client, brain, prompts
├── risk/                       # risk engine, drawdown, sizing
├── execution/                  # order lifecycle, idempotency, paper
├── observability/              # metrics counters
├── security/                   # password hashing, session helpers
├── api/                        # FastAPI app (auth, dashboard, ws)
├── orchestrator/               # lifecycle, background loops
└── app.py                      # entry point
```

The single source of truth for runtime configuration is `Settings`
(pydantic-settings). The single source of truth for persistence is the
`Repository` pattern — every DB read/write flows through it.

## Quick start

```bash
# 1. Install dependencies
python -m venv .venv
.venv\Scripts\activate   # or `source .venv/bin/activate`
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env: set OLLAMA_URL, EXCHANGE_*, etc.

# 3. Run
python -m uvicorn trade_post.api.server:create_app --factory --host 127.0.0.1 --port 8080
# OR
python -m trade_post.app
```

On first startup the system creates the SQLite schema, prints a one-time
bootstrap admin password to the log, and starts four background loops:
market stream, portfolio snapshot, AI scan, and orphan reconciliation.

## HTTP API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/` | none | Dashboard (serves `index.html`) |
| GET | `/health` | none | Liveness + DB status |
| GET | `/metrics` | none | Prometheus-format counters |
| POST | `/api/v1/auth/login` | none | Exchange credentials for session cookie |
| POST | `/api/v1/auth/logout` | session | Revoke current session |
| GET | `/api/v1/me` | session | Current user info |
| GET | `/api/v1/portfolio` | session | Equity history |
| GET | `/api/v1/orders` | session | Recent orders |
| GET | `/api/v1/events` | session | Recent system events |
| GET | `/api/v1/ai-decisions` | session | Recent AI decisions |
| GET | `/api/v1/risk` | session | Risk state |
| POST | `/api/v1/kill` | admin/operator | Trip the kill switch |
| WS | `/ws/control` | none* | Bidirectional control plane |

\* The WebSocket endpoint should be authenticated at the application edge
(proxy / mTLS). Add `get_current_user` enforcement when exposing publicly.

## Configuration

All configuration is via environment variables. See `.env.example` for the
full list. The most important:

| Var | Default | Description |
|---|---|---|
| `TRADING_MODE` | `paper` | `paper` or `live` |
| `EXCHANGE_ID` | `kraken` | `kraken` / `binance` / `bybit` / `coinbase` |
| `EXCHANGE_API_KEY` | empty | Required for live |
| `EXCHANGE_API_SECRET` | empty | Required for live |
| `OLLAMA_URL` | `http://localhost:11434` | Local or cloud Ollama |
| `OLLAMA_API_KEY` | empty | Required for cloud |
| `OLLAMA_MODEL` | `gpt-oss:120b-cloud` | Any pulled model |
| `MAX_DAILY_LOSS_PCT` | `1.0` | Drawdown cap, trips kill switch |
| `MAX_POSITION_PCT` | `2.0` | Per-position cap vs equity |
| `POSITION_SIZE_HARD_CAP` | `1000.0` | Absolute USDT cap per order |
| `DATABASE_URL` | `sqlite+aiosqlite:///./trade_post.db` | Production should use Postgres |

## Testing

```bash
pytest -v
```

Currently covers:
- 14 indicator tests (RSI/EMA/SMA/MACD/ATR/Bollinger/volatility)
- 6 auth tests (hashing, sessions, brute-force behavior)

Integration tests against the live server are scripted in the e2e harness.

## What this is NOT

- Not a get-rich-quick scheme. Algorithmic trading is risky.
- Not a cloud product. Everything runs on your machine.
- Not a managed service. You operate it. You are responsible.
- Not advice. Use paper mode first, then sandbox, then real.

## License

MIT
