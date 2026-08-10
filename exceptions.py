class TradingPostError(Exception):
    """Base exception for the Drox Trade Post system."""

    pass


class ExchangeExecutionError(TradingPostError):
    """Exception raised when an exchange operation fails via CCXT."""

    def __init__(
        self, message: str, symbol: str = None, original_error: Exception = None
    ):
        self.symbol = symbol
        self.original_error = original_error
        super().__init__(message)
