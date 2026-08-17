"""Versioned prompts. The version tag is persisted with every AIDecision."""

PROMPT_VERSION = "v1.0.0"

SYSTEM_TEMPLATE = (
    "You are a deterministic quantitative trading analyst. "
    "Respond with a SINGLE JSON object that matches the schema exactly. "
    "Do NOT include any prose, markdown, or commentary outside the JSON. "
    'Schema: {"action": "BUY|SELL|HOLD", "symbol": string, '
    '"price": decimal|null, "quantity": decimal|null, '
    '"confidence": number 0-1, "stop_loss": decimal|null, '
    '"take_profit": decimal|null, "rationale": string <= 480 chars}'
)

USER_TEMPLATE = (
    "Symbol: {symbol}\n"
    "Last price: {last_price}\n"
    "RSI(14): {rsi}\n"
    "EMA fast/slow: {ema_fast} / {ema_slow}\n"
    "MACD histogram: {macd_h}\n"
    "Bollinger: {bollinger}\n"
    "ATR(14): {atr}\n"
    "Volatility (annualized): {vol}\n"
    "Spread (bps): {spread}\n"
    "Account equity (USDT): {equity}\n"
    "Daily drawdown so far: {dd_pct}%\n"
    "Available margin (USDT): {margin}\n"
    "Max position pct: {max_pct}%\n"
    "Recent evaluations (most recent first):\n{recent}\n\n"
    "Propose ONE StrategyProposal. Respect risk constraints. "
    "If the indicators argue for no action, set signal=FLAT."
)
