import os

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")
EXCHANGE_ID = os.getenv("EXCHANGE_ID", "binance")
API_KEY = os.getenv("EXCHANGE_API_KEY")
API_SECRET = os.getenv("EXCHANGE_API_SECRET")
MODE = os.getenv("TRADING_MODE", "paper")
ENABLE_SANDBOX = os.getenv("ENABLE_SANDBOX", "true").lower() == "true"

MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", 2.0))
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", 1.0))
STALE_THRESHOLD_SEC = int(os.getenv("STALE_THRESHOLD_SEC", 30))
POSITION_SIZE_HARD_CAP = float(os.getenv("POSITION_SIZE_HARD_CAP", 1000.0))
SIMULATED_SLIPPAGE_BPS = float(os.getenv("SIMULATED_SLIPPAGE_BPS", 5.0))
ENABLE_CHAOS_TEST = os.getenv("ENABLE_CHAOS_TEST", "false").lower() == "true"
ORPHAN_AGE_THRESHOLD_SEC = int(os.getenv("ORPHAN_AGE_THRESHOLD_SEC", 300))
RECOVERY_COOLOFF_SEC = int(os.getenv("RECOVERY_COOLOFF_SEC", 300))
