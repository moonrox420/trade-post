# Product Requirements Document — Drox Trade Post: “100% Production-Ready” (Technical Guts)
# PRD IS NOT TO BE MODIFIED OR DELETED

Goal
- Deliver a production-grade algorithmic trading platform that can safely operate with live capital under strict risk controls, observability, and operational practices. “100%” means: correct money-safe accounting, auditable order lifecycle, safe AI decisioning with hard validation, durable persistence, robust exchange adapters, SLO-backed observability, secure secrets and credentials, tested deployment and rollback, and runbooks for human ops.

Scope (what this PRD covers)
- Core runtime & services: API server, orchestrator loops, execution engine, market stream, risk engine, AI brain.
- Persistence: production-grade database (Postgres), migrations, backups, integrity constraints.
- AI safety: schema-validated outputs, circuit-breaker, fallback modes.
- Execution safety: idempotency, reconciliation, kill switch, simulation & paper mode parity.
- Observability & SLOs: metrics, traces, structured logs, dashboards, alerts.
- Security & secrets: secret management, hardened auth, encryption-at-rest/transport.
- Testing & CI/CD: unit, integration, e2e, chaos, staging pipeline, image signing.
- Operational: deployment manifests, runbooks, DR, team playbook.
- Excluded (out of scope for this document): marketing/UX polish, monetization/legal business packaging (legal items are noted but not delivered).

Deliverables (high level)
- Production-grade server image (Docker), docker-compose and Kubernetes manifests (Helm).
- Postgres-backed schema with migrations and tested migration path.
- AI decisioning module with JSON schema validation, retries, circuit-breaker, and monitoring.
- Execution adapters: paper adapter with deterministic simulation; live adapters for Kraken/Binance/Bybit/Coinbase with retries and idempotency.
- Risk engine with deterministic sizing (ATR), drawdown guard, position caps, stop-loss handling, and kill switch.
- Reconciliation service to make ledger reflect exchange state and to correct orphan orders.
- Observability: Prometheus metrics, OpenTelemetry traces, structured logs (trace_id), Grafana dashboards, alert rules.
- CI/CD workflow: tests, linters, security scans, image build/push, deploy to staging.
- Test harness: unit tests, integration tests (with network stubs), e2e harness with historical data replay and attack/failure simulations.
- Runbooks and incident response: playbooks for runaway positions, exchange outages, data corruption, and kill-switch events.

Acceptance Criteria (must pass to be “100%”)
- Data integrity
  - All monetary values stored and processed as Decimal in code and NUMERIC (Postgres) in DB.
  - All DB writes use parameterized queries and ORM transactions; every financial write flows through a Repository with audit entries.
  - End-to-end reconciliation shows ledger = sum(executed trades) ± fee adjustments. Reconciliation job with deterministic pass/fail.

- Safety & correctness
  - AI outputs validated by JSON Schema; invalid outputs are rejected, audited, and trigger safe fallback (no live orders).
  - Risk engine prevents any order that violates limits; kill switch trips immediately on policy breach and prevents further live orders until manual clearance.
  - Idempotency: order submissions can be retried safely without duplicate fills (idempotency key + durable dedup table).
  - Order lifecycle fully auditable in events table with immutable timestamps, trace_id linkage and raw decision payload saved.

- Reliability & Observability
  - Prometheus metrics provide counters/gauges/histograms with labels required below; traces include trace_id and sampled spans for AI calls, order placement, and DB transactions.
  - Alerting configured for circuit-breaker trips, kill-switch trips, daily drawdown > threshold, reconciliation failures, high error rate (5xx), and AI failure rate.
  - SLOs: 99.5% API success rate (5xx <0.5%), order submission latency p50 <500ms (paper/live varies by exchange), reconciliation must succeed within 10 minutes for routine jobs.

- Testing & deployment
  - CI pipeline executes unit tests, linters, mypy/pyright, security scanning, and integration tests with simulated exchange.
  - Staging deployment that is production-parallel (Postgres, secrets manager, monitoring) validated by running the e2e harness with historic market data.
  - Canary deployment strategy and rollback validated in staging.

- Security & compliance
  - No plaintext secrets in production images or code; production uses secret manager (Vault, AWS Secrets Manager, GCP Secret Manager).
  - Transport TLS for API + exchange connectors; secure cookie flags, session rotation and CSRF protections for sensitive endpoints.
  - SAST and dependency vulnerability scans performed and no critical findings unresolved.

Key Components & Technical Specs

1) Persistence (Postgres primary)
- DB choice: Postgres 15+ (production), SQLite remains for local dev only.
- Migrations: Alembic or SQLAlchemy-migrate with versioned scripts in trade_post/persistence/migrations.
- Primary tables (columns abbreviated; all money as NUMERIC(30, 10)):
  - users (id UUID PK, username text unique, pw_hash text, roles text[], created_at timestamptz)
  - sessions (id UUID PK, user_id FK, session_token text unique, expires_at timestamptz, created_at)
  - ledger_entries (id UUID PK, account_id FK, delta NUMERIC, currency text, balance_after NUMERIC, type text, reference text, created_at, metadata jsonb)
  - orders (id UUID PK, client_order_id text unique, exchange_order_id text nullable, status enum, symbol text, side enum, price NUMERIC, qty NUMERIC, filled_qty NUMERIC, fees NUMERIC, created_at, updated_at, raw_exchange JSONB)
  - positions (id UUID PK, symbol, qty NUMERIC, avg_price NUMERIC, mark_price NUMERIC, realized_pnl NUMERIC)
  - events (id UUID, type text, level text, payload JSONB, trace_id text, created_at)
  - ai_decisions (id UUID, trace_id text, input JSONB, output JSONB, schema_version text, valid bool, rejection_reason text, created_at)
  - reconciliations (id UUID, run_started_at, run_ended_at, result jsonb)
  - idempotency_keys (key text PK, created_at, used_by text, expiration timestamptz)
- Indices: orders(client_order_id), orders(exchange_order_id), ledger_entries(account_id, created_at), ai_decisions(trace_id).
- Transactions: Use SERIALIZABLE/REPEATABLE READ where appropriate for money-critical flows; explicit transaction boundaries in repository methods. Add optimistic locking (version columns) on positions/orders if needed.

2) AI layer (trade_post/ai)
- API contract: All AI “decisions” must return strictly validated JSON according to a versioned schema.
  - Decision schema (v1) example keys: { action: "BUY"|"SELL"|"HOLD", symbol: string, price: decimal|null, quantity: decimal|null, confidence: number 0..1, stop_loss: decimal|null, take_profit: decimal|null, rationale: string }
- Validation: Use jsonschema + pydantic with strict types; schema version stored with each decision.
- Circuit breaker: track recent failure counts; trip after N failures (configurable), cooldown window, and degrade to deterministic fallback strategy.
- Retry/backoff: on transient errors, retry with exponential backoff up to configurable max_retries; respect ollama_max_concurrent.
- Concurrency limit: limit AI concurrency to a configurable number to prevent resource exhaustion.
- Prompt management: Prompt templates stored in repository or DB; do not include secrets. Minimal prompt context window; careful token management.
- Auditing: store raw model response and validated parsed output in ai_decisions table. All failures produce events.

3) Risk engine (trade_post/risk)
- Deterministic sizing:
  - ATR-based sizing: position_size = equity * per_trade_risk_pct / (ATR * atr_stop_multiplier)
  - Hard caps: position_size <= position_size_hard_cap_usd and <= equity * max_position_pct/100
  - Round quantities according to exchange lot sizes (adapter provides rounding)
- Daily drawdown:
  - Track peak equity and compute drawdown = (peak - current_equity)/peak; if drawdown > max_daily_loss_pct => trip kill switch.
- Open order caps and portfolio exposure checks:
  - Prevent new orders that would push exposure beyond max_portfolio_exposure_pct.
- Staleness/spread checks:
  - Reject orders if last market price older than max_stale_data_sec or spread > max_spread_pct.
- Fail-closed: if risk module is unavailable or returns any uncertainty, default to deny actions requiring live funds.

4) Execution layer (trade_post/execution)
- Interface: ExchangeAdapter abstract class with methods:
  - send_order(client_order_id, symbol, side, type, price, quantity, idempotency_key) -> exchange_order_id, status, filled_qty, raw
  - cancel_order(exchange_order_id)
  - fetch_order(exchange_order_id)
  - fetch_balance()
  - fetch_open_orders()
- Idempotency: every order must be submitted with a client_order_id and persisted idempotency_keys entry. Re-submits reuse client_order_id and are ignored if already matched to an exchange order.
- Paper adapter: deterministic execution engine that simulates fills based on market snapshots (configurable latency and slippage model). Must be deterministic and reproducible for replay tests.
- Live adapters: wrap CCXT or exchange SDKs with retry policy and exponential backoff; implement per-exchange rate limits and error normalization; map exchange states to local order statuses; persist raw exchange response.
- Reconciliation: background job that reconciles orders and ledger every X minutes (configurable). Reconciliation algorithm:
  - For each order with status not final, fetch exchange state.
  - If mismatch found, generate reconciliation event, fix ledger or mark order for manual review if ambiguous.
  - For orphaned ledger entries (unmatched), attempt to find matching exchange fill; otherwise create remediation actions.

5) Orchestrator & background loops
- Loops:
  - Market Stream: subscribe to market data for subscribed_symbols with OHLCV, L2 book snapshots (where available).
  - Portfolio Snapshot: compute equity, PnL, update peaks for drawdown.
  - AI Scan: schedule AI decisions (periodic or event-driven) with concurrency limits.
  - Reconciliation: periodic reconciliation loop with alerts on failures.
  - Snapshot persistence and metrics emission.
- Graceful shutdown: structured shutdown with draining in-flight AI tasks and order submission queues.

6) API & WebSocket contract (trade_post/api)
- REST endpoints:
  - POST /api/v1/auth/login -> sets secure HttpOnly SameSite=Strict session cookie; returns user info
  - POST /api/v1/auth/logout -> clears session
  - GET /api/v1/me -> user info
  - GET /api/v1/portfolio -> equity history (paginated)
  - GET /api/v1/orders -> recent orders (filters: status, symbol)
  - GET /api/v1/events -> audit events (admin only)
  - GET /api/v1/ai-decisions -> recent decisions
  - GET /api/v1/risk -> risk state
  - POST /api/v1/kill -> admin/operator only, trip/untrip kill switch
- WebSocket /ws/control:
  - Auth: must require session token or mTLS; if exposed publicly require get_current_user enforcement.
  - Protocol: JSON messages with schema {type: string, trace_id?: string, payload: {...}}; reply includes trace_id.
  - Allowed control messages: request snapshot, replay decision, manual order, override (admin only).
  - All ws messages logged with trace_id, user_id, and stored in events.

- Security for API:
  - Session cookies: Secure, HttpOnly, SameSite=Strict; session rotation on sensitive actions.
  - CSRF: For state-changing requests from browsers, require CSRF token (or require SameSite and use session cookie + custom header).
  - Rate limiting: per-IP and per-account rate limits with soft blocks and reporting.

7) Observability & telemetry
- Logging:
  - Structured JSON logs written to rotating file and stdout for container (option). Must include trace_id, level, module, and sanitized message.
  - SecretScrubbingFilter active to scrub accidental secrets from logs.
- Tracing: OpenTelemetry integration with spans for AI calls, db transactions, exchange calls, and order placement. Trace id passed in logs and DB events.
- Metrics (Prometheus) — required metric names:
  - trade_post_api_requests_total{method,endpoint,status}
  - trade_post_ai_calls_total{model,success}
  - trade_post_ai_failures_total{reason}
  - trade_post_orders_submitted_total{mode,paper_live,symbol}
  - trade_post_orders_failed_total{reason}
  - trade_post_reconciliation_runs_total{result}
  - trade_post_kill_switch_trips_total
  - trade_post_reconciliation_mismatch_errors_total
  - trade_post_equity_current, trade_post_peak_equity
  - trade_post_daily_drawdown_pct
  - job_duration_seconds_bucket (for background loops)
- Dashboards:
  - AI health, order pipeline latency, reconciliation status, equity & drawdown, exchange latencies, error rates.
- Alerts:
  - Kill switch tripped.
  - Daily drawdown > threshold (configurable).
  - Reconciliation failed > 1 run.
  - AI failure rate > X% over Y minutes.
  - Exchange unreachable or rate-limited.

8) Security & secrets
- Secrets management: production must use a secret manager; no secrets in env vars on build. CI reads secrets from GitHub Actions secrets only for deploy.
- Exchange credentials: rotated periodically; minimum permission API keys for trading and not used for withdrawals.
- Keys encryption: DB fields with sensitive data must be encrypted at rest (Postgres pgcrypto or managed disk encryption).
- Network: Use TLS for API; mTLS recommended for internal services. Restrict DB/Redis to private networks.
- Authentication: PBKDF2/bcrypt for password hashing (configurable cost). Brute-force protection using per-IP counters stored in DB/Redis.
- Authorization: Role-based access control: viewer, operator, admin. Admin-only actions require 2FA (optional) or approval workflow.

9) Testing strategy
- Unit tests:
  - Coverage target: >=85% for core modules (persistence, risk, execution, ai parsing).
  - Mock external calls (httpx/CCXT) with deterministic fixtures.
- Integration tests:
  - Use Docker Compose for a test Postgres and a stub exchange (or simulated CCXT adapter).
  - Test end-to-end flows: login -> AI decision -> risk -> execution -> ledger -> reconciliation.
- End-to-end:
  - Historical market data replay with deterministic paper-run producing expected PnL. Validate no risk rule violations.
- Chaos testing:
  - Simulate AI failure, exchange timeouts, DB lock contentions, and verify fail-closed behavior: no live money orders are placed when safety constraints fail.
- Fuzzing:
  - Randomized AI output fuzz tests to ensure JSON schema validators catch malformed outputs.
- Performance & load:
  - Load test API and order submission path (k6 or locust) to ensure latency/resilience under expected QPS.

10) CI/CD
- Workflows:
  - PR pipeline: lint, format check, type check, unit tests, security scan (pip-audit/safety), build image (no secrets), run integration tests (optional or required on main).
  - Release pipeline: build signed images, run e2e in staging, promote to production with canary.
- Artifacts:
  - Build images pushed to registry with immutable tags and digest pinned in deployment manifests.
- Rollback:
  - Automatic rollback on healthcheck failure during canary or on alert.

11) Deployment topology & infra
- Minimum production stack:
  - 2+ application instances (ASGI worker + Uvicorn/Gunicorn) behind load balancer.
  - Postgres cluster (managed or self-hosted) with automated backups and PITR.
  - Redis (optional) for session store and rate-limit counters.
  - Secrets manager (Vault or cloud-native).
  - Monitoring: Prometheus + Grafana + Alertmanager.
  - Tracing: OTel collector + backend (Tempo/Jaeger).
  - Optional: Message broker (RabbitMQ, Kafka) if you scale orchestrator responsibilities.
- Network:
  - Private VPC, TLS termination at LB, mTLS between services optional.
- Deployment choices:
  - Preferred: Kubernetes (for scale and observability) with Helm chart and pod disruption budgets.
  - Alternative: Docker Compose on dedicated hosts with systemd-managed container runtime for simpler setups.

12) Operational runbooks (deliver as markdown)
- Runbook items:
  - How to interpret kill switch and steps to safely untrip.
  - Steps for emergency stop: immediately disable exchange API keys or network egress to exchange.
  - Reconciling ledger vs exchange: commands and SQL checks.
  - How to rotate exchange API keys and update secrets manager.
  - Disaster recovery: restore Postgres from latest backup and re-run reconciliation; verify ledger integrity.
  - Playbook for AI model failure or hallucination: disable AI path, enable manual operator decisions.
  - Incident response templates for legal/performance/security incidents.

Failure modes & mitigations
- AI hallucination / malformed output -> Rejected by JSON schema, archived, and triggers audit + manual review; no auto orders placed.
- Exchange misbehavior -> Idempotency prevents double fills; reconciliation job reconciles and emits alerts for manual action.
- DB corruption -> Backups and PITR; read-only mode for API until restore.
- Latency spikes -> Circuit breakers, degrade AI decisions to deterministic strategies or pause orders.
- Runaway orders -> Kill switch, emergency revoke API key, disable live mode via config.
- Insider compromise -> Secret manager with rotation, limit permissions for API keys, audit logs stored externally.

Data retention & privacy
- Audit logs, ai_decisions, and events retained for configurable window (e.g., 365 days) and exportable for compliance.
- PII minimization: store only necessary user info; if storing more, ensure encryption at rest and masking in logs.

Schema & API examples (concise)
- Decision accepted example (POST ai-decision):
  {
    "action": "BUY",
    "symbol": "BTC/USDT",
    "price": "29321.12345",
    "quantity": "0.005",
    "confidence": 0.84,
    "stop_loss": "28900.00",
    "take_profit": "30500.00",
    "rationale": "ATR breakout with supportive volume"
  }
- Order request flow:
  1. AI decision -> Risk.validate(decision) -> Execution.prepare_order() -> Repository.persist_intent(client_order_id, decision...) -> ExecutionAdapter.send_order(...) with idempotency_key -> Repository.record_exchange_response -> ledger.update on fill

Milestones
- M0 — Specification & infra design
- M1 — Postgres migration, schema & migrations
- M2 — Execution adapters + idempotency + paper adapter parity
- M3 — Risk engine implementation & tests (property tests + backtest harness)
- M4 — AI safety layer (JSON schema validation, circuit-breaker, prompt audit)
- M5 — Reconciliation engine + background loops + tests
- M6 — Observability (metrics, traces, dashboards) + alerts
- M7 — CI/CD, containerization, staging deploy & e2e
- M8 — Security hardening & SAST + pen-test remediation
- M9 — Runbooks, DR, final validation & sign-off

Resource assumptions
- Access to necessary exchange sandbox accounts and API keys.
- Cloud infra for staging/production or access to VMs + managed Postgres.
- Monitoring/tracing backend (Prometheus/Grafana/Tempo) available or budget to provision.
- Decision authority for kill-switch and escalation policies.

Definition of Done (DoD)
- All acceptance criteria above satisfied and demonstrated in staging with simulated live traffic and historical replay.
- Security scans show no critical findings; at most 1 or 2 medium findings with mitigation plans.
- Production runbook tested in at least one tabletop/DR exercise.
- Team sign-off: engineering lead, ops lead, and risk owner approve go-live checklist.

Appendix — Quick checklist for immediate technical build tasks (developer actionable)
- Create DB migration baseline for Postgres and convert current SQLite schema.
- Implement Repository layer with typed DTOs (Pydantic) and explicit transactions.
- Implement idempotency middleware and table.
- Implement AI output JSON Schema v1 + pydantic model for decisions and unit tests for rejection cases.
- Implement circuit breaker for AI (Redis or in-process sliding window) and fallback deterministic strategy.
- Implement execution adapters: paper first, then one live exchange adapter (Kraken) with end-to-end test.
- Implement risk engine with unit + property tests and backtest harness using historical market dataset.
- Implement reconciliation job and unit tests simulating mismatched states.
- Add Prometheus metric instrumentation to all critical paths.
- Add OpenTelemetry instrumentation with trace_id propagation across async boundaries.
- Build Dockerfile and docker-compose for app + Postgres + Redis + Prometheus.
- Create CI workflows: tests + lint + build image + integration steps.
- Draft runbooks for kill switch and emergency stop.

