# Drox Trade Post

A **local-first, AI-assisted algorithmic trading system**. Drox Trade Post combines real-time exchange connectivity (via CCXT), an LLM-powered multi-agent strategy brain (via Ollama), a layered risk engine, and a live web dashboard — all running on a single machine with SQLite as the single source of truth.

The system is designed around a simple philosophy: **no cloud quota, no shared credentials, no permission bottlenecks.** Markets, decisions, orders, and evaluations are tracked locally; both paper and live trading are supported through the same code path.

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Web Dashboard](#web-dashboard)
- [WebSocket Command Protocol](#websocket-command-protocol)
- [Trading Modes](#trading-modes)
- [Risk Controls](#risk-controls)
- [Data & Persistence](#data--persistence)
- [Testing](#testing)
- [Project Structure](#project-structure)
- [Troubleshooting](#troubleshooting)
- [Security Notes](#security-notes)
- [Disclaimer](#disclaimer)

---

## Features

- **Paper & live trading** — switch with a single environment variable; paper mode runs keyless with a simulated 10,000 USDT account.
- **LLM-driven strategy generation** — a multi-agent brain queries Ollama (local or cloud) for structured `StrategyProposal` candidates, with strict Pydantic schema validation and JSON-mode output.
- **Real-time market intelligence** — RSI(14) and volatility indicators computed from OHLCV streaming on a 15-second cadence (BTC/ETH).
- **Hard risk gates** — kill switch, daily drawdown cap, position-size hard cap, dynamic leverage, and slippage violation checks, enforced *before* any order leaves the system.
- **Autonomous execution** — background loops for market streaming, portfolio snapshots, strategy scanning, execution, and evaluation — all managed and cancelled cleanly at shutdown.
- **Trailing stop-loss monitors** — per-order coroutines that continuously ratchet the stop and emit market exits automatically.
- **Circuit breaker** — repeated execution failures trip a recovery mode that suspends trading until a cooling-off period elapses.
- **Self-improving evaluation loop** — every executed proposal is scored (1–10) and critiqued; past evaluations are fed back into future prompts.
- **Replay & diagnostics** — simulate the full decision process for any historical session.
- **Realtime HUD dashboard** — a single-file neon-carbon terminal UI served over WebSocket with live market, portfolio, execution, and evaluation feeds.
- **Fully local persistence** — every event, order, portfolio snapshot, and evaluation is stored in SQLite. No external database required.

---

## Architecture
```
┌──────────────────────────────────────────────────────────────────────┐
│                        Web Dashboard (index.html)                    │
│                Neon-Carbon HUD · htmx + Chart.js · WebSocket client  │
└───────────────────────────────────────┬──────────────────────────────┘
                                        │  ws://<host>:8080/ws/control
                                        ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     FastAPI  Application  (uvicorn)                  │
│   ┌──────────────┐  ┌──────────────────┐  ┌───────────────────────┐  │
│   │  /  (HTML)   │  │  /ws/control     │  │  lifespan bootstrap   │  │
│   │  dashboard   │  │  WS command bus  │  │  dependency injection │  │
│   └──────────────┘  └──────────────────┘  └───────────────────────┘  │
└───────────────────────────────────────┬──────────────────────────────┘
                                        │  structured concurrency
                                        ▼
┌──────────────────────────────────────────────────────────────────────┐
│                         Core Runtime Layer                           │
│  ┌───────────────────┐  ┌───────────────────┐  ┌──────────────────┐  │
│  │  MultiAgentStra-  │  │  RiskEngine       │  │  ExecutionEngine │  │
│  │  tegyBrain        │  │  · kill switch    │  │  · idempotency   │  │
│  │  · proposals      │  │  · drawdown cap   │  │  · slippage gate │  │
│  │  · priorities     │  │  · dynamic lev.   │  │  · trailing stop │  │
│  └───────────────────┘  └───────────────────┘  └──────────────────┘  │
│  ┌───────────────────┐  ┌───────────────────┐  ┌──────────────────┐  │
│  │  MarketDataService│  │  PortfolioEngine  │  │  StrategyEval-   │  │
│  │  · RSI/volatility │  │  · balances       │  │  uator          │  │
│  │  · snapshot cache │  │  · positions      │  │  · scoring/crit. │  │
│  └───────────────────┘  └───────────────────┘  └──────────────────┘  │
│  ┌───────────────────┐  ┌───────────────────┐  ┌──────────────────┐  │
│  │  EventStore       │  │  ReplayService    │  │  TaskRegistry    │  │
│  │  · circuit break. │  │  · session replay │  │  · managed tasks │  │
│  └───────────────────┘  └───────────────────┘  └──────────────────┘  │
└───────────────┬───────────────────────────────┬──────────────────────┘
                │                               │
                ▼                               ▼
┌──────────────────────────┐        ┌──────────────────────────────────┐
│   CCXT Async Adapter     │        │      Ollama (local / cloud)      │
│   Kraken · Binance ·     │        │  /api/chat · JSON mode · temp 0.1│
│   Bybit (paper or live)  │        │  default: gpt-oss:120b-cloud     │
└──────────────────────────┘        └──────────────────────────────────┘
                │                               │
                ▼                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│                  SQLite  (single source of truth)                    │
│   event_store · execution_ledger · portfolio_history                 │
│   strategy_evaluations · performance_reports · risk_engine_state     │
└──────────────────────────────────────────────────────────────────────┘
```

### Module map

| File | Responsibility |
|---|---|
| `drox_trade_post.py` | **Entry point.** Application bootstrap, `.env` parsing, SQLite schema, all core services, FastAPI routes, WebSocket protocol, and background task orchestration. |
| `models.py` | Canonical Pydantic contracts (`MarketSnapshot`, `StrategyProposal`, `PortfolioSnapshot`, `OrderSnapshot`, …) with validation and safety rules. |
| `config.py` | Process-level configuration loaded from environment variables. |
| `adapters.py` | CCXT async exchange adapter — tickers, OHLCV, balances, positions, order placement, sandbox mode. |
| `services.py` | Market data streaming, portfolio accounting, and event/circuit-breaker services. |
| `engines.py` | Risk engine (validate proposal / drawdown / leverage) and execution engine (order placement, trailing stops, reconciliation). |
| `brain.py` | Legacy multi-agent strategy brain variant (Vertex AI / Firestore-backed). *Not wired into the runnable runtime.* |
| `runtime.py` | Managed task registry and WebSocket connection manager. |
| `exceptions.py` | Domain exceptions (`TradingPostError`, `ExchangeExecutionError`). |
| `test_risk_engine.py` | Risk-engine unit tests (drawdown-limits scenario). |
| `index.html` | The full web dashboard (self-contained, no build step). |

> **Note on `brain.py` / `config.py`**: the runnable runtime is entirely contained in `drox_trade_post.py`. `brain.py` is a standalone/legacy module (Google Vertex AI + Firestore) kept out of the default startup path; `config.py` mirrors env handling for those standalone modules and defaults to `binance`.

---

## Quick Start

### Prerequisites

- **Python 3.12+** (developed against 3.12.10)
- **Ollama** running locally (or an Ollama Cloud API key) with the configured model pulled, e.g.:

  ```bash
  ollama pull gpt-oss:120b-cloud
  ```

  The system will also work with any other model available to your Ollama server — set it via `OLLAMA_MODEL`.
- **(Optional)** API keys for the exchange you intend to trade on.
### 1. Clone & create a virtual environment

```bash
git clone <your-repo-url> trade_post
cd trade_post
python -m venv .venv
```

### 2. Install dependencies

```bash
# Windows
.venv\Scripts\activate
pip install -r requirements.txt

# Linux / macOS
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure environment

Copy the template and fill in your values:

```bash
cp .env.example .env      # PowerShell: Copy-Item .env.example .env
```

At minimum, confirm the LLM and trading mode settings:

```dotenv
MODE=paper                # "paper" | "live"
EXCHANGE_ID=kraken        # kraken | binance | bybit
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=gpt-oss:120b-cloud
```

> If `OLLAMA_URL` points at `localhost`/`127.0.0.1` **and** `OLLAMA_API_KEY` is set, the client automatically routes to `https://ollama.com` (Ollama Cloud).

### 4. Run the system

```bash
python drox_trade_post.py
```

Or via your port of choice:

```bash
$env:PORT = 8080          # PowerShell — optional, default is 8080
python drox_trade_post.py
```

The server is now listening on **http://0.0.0.0:8080**.

### 5. Open the dashboard

Navigate to **http://localhost:8080** in your browser.

On startup, the app initializes the local SQLite schema, connects to the exchange, validates the Ollama model, and starts six managed background loops:

| Loop | Interval | Purpose |
|---|---|---|
| Market streamer | 15s | Streams BTC/ETH snapshots to subscribed clients |
| Portfolio snapshot | 60s | Persists equity/margin/position history |
| Autonomous strategy scan | 60s | Scores markets and executes proposals |
| Strategy evaluator | — | Scores and critiques past executions |
| Weekly report | 7 days | Generates executive performance summaries |
| Execution reconciliation | — | Detects missed/orphaned orders |

---

## Configuration

All configuration is read from environment variables at startup (a `.env` file in the project root is parsed natively by `drox_trade_post.py`).
| Variable | Default | Description |
|---|---|---|
| `EXCHANGE_ID` | `kraken` | CCXT exchange id: `kraken`, `binance`, `bybit`, or any supported by ccxt. |
| `EXCHANGE_API_KEY` | *(empty)* | Exchange API key. **Required for live mode.** |
| `EXCHANGE_API_SECRET` | *(empty)* | Exchange API secret. **Required for live mode.** |
| `TRADING_MODE` | `paper` | `paper` (simulated, keyless) or `live` (real orders). |
| `ENABLE_SANDBOX` | `true` | Use the exchange's sandbox/testnet environment when available. |
| `PORT` | `8080` | HTTP/WebSocket port for the FastAPI server. |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama server base URL. |
| `OLLAMA_API_KEY` | *(empty)* | Bearer token. If set with a localhost URL, traffic routes to Ollama Cloud automatically. |
| `OLLAMA_MODEL` | `gpt-oss:120b-cloud` | Model id deployed on your Ollama server / cloud. |
| `MAX_POSITION_PCT` | `2.0` | Max single-position size as a % of total equity. |
| `MAX_DAILY_LOSS_PCT` | `1.0` | Daily drawdown cap (%) from session starting equity — triggers the kill switch. |
| `POSITION_SIZE_HARD_CAP` | `1000.0` | Absolute notional cap (USDT) per proposal. |
| `STALE_THRESHOLD_SEC` | `30` | Seconds after which a cached market snapshot is considered stale. |
| `SIMULATED_SLIPPAGE_BPS` | `5.0` | Simulated slippage (basis points) applied in paper mode. |
| `ENABLE_CHAOS_TEST` | `false` | Enables fault-injection helpers for resilience testing. |
| `ORPHAN_AGE_THRESHOLD_SEC` | `300` | Age (seconds) at which an unreconciled order is treated as orphaned. |
| `RECOVERY_COOLOFF_SEC` | `300` | Cooling-off period before the circuit breaker re-arms the system. |
| `DB_PATH` | `trade_post.db` | Location of the SQLite database file. |

---

## Web Dashboard

`index.html` is a self-contained "Neon-Carbon HUD" terminal. It connects to `ws://<host>:8080/ws/control` and provides:

- Live **market snapshots** (price, RSI, volatility) for subscribed symbols
- **Portfolio** equity/margin utilization panel
- **Execution and brain-activity** logs in terminal-style feed
- **Kill switch**, autonomous scanning start/stop, and per-symbol analysis buttons
- **Reports & evaluations** viewers (last 5 weekly reports, last 10 evaluations)

Dashboard controls (available as global browser functions via the console):

```js
killSwitch();   // Emergency shutdown — trips the risk-engine kill switch
startAuto();    // Start autonomous scanning
stopAuto();     // Stop autonomous scanning
analyzeBTC();   // Run a single analysis + execution cycle on BTC/USDT
getReports();   // Fetch recent performance reports
getEvals();     // Fetch recent strategy evaluations
```
---

## WebSocket Command Protocol

Connect to `ws://<host>:8080/ws/control` and send JSON command frames:

```json
{ "cmd": "START_AUTO" }
```

| Command | Payload | Description |
|---|---|---|
| `KILL` | — | Emergency kill switch. Halts all new proposals and clamps position sizing. |
| `START_AUTO` | — | Enable autonomous market scanning. |
| `STOP_AUTO` | — | Disable autonomous market scanning. |
| `ANALYZE` | `{ "symbol": "BTC/USDT" }` | Generate and execute a single strategy proposal for a symbol. |
| `GET_REPORTS` | — | Broadcast the 5 most recent performance reports. |
| `GET_EVALS` | — | Broadcast the 10 most recent strategy evaluations. |
| `REPLAY` | `{ "session_id": "..." }` | Replay a historical session's full decision process. |
| `REBALANCE` | `{ "allocations": { "BTC/USDT": 0.5, "ETH/USDT": 0.5 } }` | Generate and execute rebalance proposals toward target allocations. |
| `SUBSCRIBE` | `{ "symbols": ["BTC/USDT"] }` | Subscribe to market snapshots for symbols. |
| `UNSUBSCRIBE` | `{ "symbols": ["BTC/USDT"] }` | Unsubscribe from market snapshots. |

**Outbound broadcast frames** include `market_snapshot`, `portfolio_snapshot`, `brain_thinking`, `execution`, `evaluations`, `performance_reports`, `replay_report`, `rebalance_step`, and `trade_executed`.

---

## Trading Modes

### Paper trading (default — keyless)

- No exchange credentials required. `PortfolioEngine.refresh_state()` automatically falls back to a simulated **10,000 USDT** account when `TRADING_MODE=paper` and no API keys are present.
- Orders are modeled locally (no network calls to the exchange), and simulated slippage is applied per `SIMULATED_SLIPPAGE_BPS`.
- All other subsystems — risk gates, circuit breaker, trailing stops, evaluation loop, SQLite persistence — behave identically to live mode.

### Live trading

1. Set `TRADING_MODE=live`.
2. Provide `EXCHANGE_API_KEY` and `EXCHANGE_API_SECRET` for your exchange.
3. Keep `ENABLE_SANDBOX=true` initially to validate against the exchange testnet.

> **Recommended path:** run paper mode first, review a full day of `strategy_evaluations` and a weekly `performance_reports` summary, then graduate to live.

---

## Risk Controls

Every proposal passes a mandatory gate in `RiskEngine.validate_proposal()` **before** execution:

1. **Kill switch** — if tripped (manually via `KILL`, or automatically on drawdown breach), all proposals are rejected until a session reset.
2. **Daily drawdown cap** — equity is snapshotted at session start (`starting_equity`, persisted in SQLite). A breach of `MAX_DAILY_LOSS_PCT` trips the kill switch.
3. **Position-size caps** — both a relative limit (`MAX_POSITION_PCT` of equity) and an absolute limit (`POSITION_SIZE_HARD_CAP`) are enforced at the model-validation layer.
4. **Dynamic leverage** — leverage scales inversely with margin utilization (10× → 1×) to protect the account as risk grows.
5. **Slippage guards** — `OrderSnapshot` validates that fill prices do not deviate more than 5% from the proposal price; `SIMULATED_SLIPPAGE_BPS` models slippage in paper mode.
6. **Circuit breaker** — 3 execution failures within 60 seconds trip recovery mode; the system re-arms after `RECOVERY_COOLOFF_SEC`.
7. **Idempotency** — every order carries a deterministic SHA-256 idempotency key (symbol + side + amount + type) recorded in the `execution_ledger`, preventing duplicate fills.
8. **Trailing stop-loss monitors** — every executed proposal with a `trailing_stop_pct` spawns a monitor that ratchets the stop and exits automatically at the trigger price.
---

## Data & Persistence

SQLite (`trade_post.db`, configurable via `DB_PATH`) is the single source of truth. Schema created automatically at startup:

| Table | Contents |
|---|---|
| `event_store` | Append-only event log (startup, shutdown, execution failures, system events) with severity. |
| `execution_ledger` | Idempotency-keyed order records for duplicate-prevention and reconciliation. |
| `portfolio_history` | Time-series of equity, margin, risk-adjusted equity, margin utilization, positions. |
| `strategy_evaluations` | Post-trade performance (bps) and LLM qualitative scores (1–10) + critiques. |
| `performance_reports` | Weekly executive summaries generated by the LLM. |
| `risk_engine_state` | Persisted `starting_equity` and kill-switch state (survives restarts). |

---

## Testing

The project ships a `pytest` suite covering the risk engine's drawdown-limits behavior:

```bash
# with the venv active
pip install pytest pytest-asyncio
pytest -v
```

Expected result: `test_risk_engine_drawdown_limit` passes — verifying that a 2% equity drop correctly rejects proposals (1% limit) and trips the kill switch.

---

## Project Structure

```
trade_post/
├── .env                  # Local secrets & runtime config (git-ignored)
├── .env.example          # Sanitized config template (safe to commit)
├── .gitignore
├── requirements.txt      # Pinned production dependencies
├── drox_trade_post.py    # ★ Entry point — FastAPI app + core services
├── models.py             # Pydantic data contracts
├── config.py             # Env-driven configuration
├── adapters.py           # CCXT exchange adapter
├── services.py           # Market / portfolio / event services
├── engines.py            # Risk + execution engines
├── brain.py              # Legacy Vertex-AI strategy brain (standalone)
├── runtime.py            # Task registry + connection manager
├── exceptions.py         # Domain exceptions
├── test_risk_engine.py   # Risk engine tests
├── index.html            # Web dashboard (self-contained)
├── diagnose.py           # Scratch diagnostic script (troubleshooting aid)
└── trade_post.db         # Runtime SQLite database (git-ignored)
```
---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Model '...' not found inside your Ollama server` (HTTP 404) | Model not pulled | `ollama pull gpt-oss:120b-cloud` and restart. |
| `Could not reach Ollama` | Ollama not running / wrong URL | Confirm `ollama serve` is running and `OLLAMA_URL` is reachable from this machine. |
| No market data on dashboard | No WebSocket subscription | Use `SUBSCRIBE` with `["BTC/USDT"]` (or click a symbol in the UI). |
| Positions show zero / no balance | Keyless paper mode active | That is expected in keyless paper mode — equity is simulated at 10,000 USDT. |
| Kill switch engaged without `KILL` | Drawdown limit breached | Reset by deleting the `risk_engine_state` row (or the DB) and reviewing recent evaluations. |
| Orders not executing in live mode | Sandbox mode still on | Set `ENABLE_SANDBOX=false` only after testnet validation. |

---

## Security Notes

- **Never commit real credentials.** `.env` is git-ignored; only the sanitized `.env.example` template should be tracked. Rotate any key that has been accidentally exposed.
- The system validates every LLM *output* against strict Pydantic schemas before anything can be executed — malformed or off-schema proposals are rejected.
- LLM reasoning is advisory. All decisions still flow through the hard risk gates described in [Risk Controls](#risk-controls).
- Run live trading behind a network you trust; the dashboard is not auth-protected and is bound to `0.0.0.0`.

---

## Disclaimer

**This software is provided for educational and research purposes only. Nothing in it constitutes financial advice. Algorithmic trading involves substantial risk of loss, including complete loss of capital, and is not suitable for everyone. You are solely responsible for all decisions, risk management, and compliance with the rules of any exchange you connect to. Always validate thoroughly in paper/sandbox mode before committing real funds. Past performance (including LLM-evaluated "successes") does not guarantee future results.**