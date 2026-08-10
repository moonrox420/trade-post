import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime
from engines import RiskEngine
from models import StrategyProposal, MarketSnapshot, PortfolioSnapshot, SignalSide


@pytest.mark.asyncio
async def test_risk_engine_drawdown_limit():
    # Initialize Risk Engine with mocks
    risk = RiskEngine()
    mock_db = AsyncMock()
    mock_portfolio = MagicMock()

    # 1. Setup Initial Session (10,000 USDT)
    initial_state = PortfolioSnapshot(
        timestamp=datetime.utcnow(),
        total_equity=10000.0,
        available_margin=10000.0,
        positions=[],
        base_balances={},
        risk_adjusted_equity=10000.0,
        margin_utilization=0.0,
    )
    mock_portfolio.refresh_state = AsyncMock(return_value=initial_state)

    # Restoring from clean state (no 'starting_equity' in DB).
    # The RiskEngine reaches into a Firestore-style chain:
    #   db.collection("system_state").document("risk_engine_state").get()
    # and later .set(...) when persisting state. Wire the chain with awaits.
    risk_engine_document = MagicMock(
        get=AsyncMock(return_value=MagicMock(exists=False)),
        set=AsyncMock(),
    )
    document_reference = MagicMock(
        document=MagicMock(return_value=risk_engine_document)
    )
    mock_db.collection = MagicMock(return_value=document_reference)
    await risk.initialize(mock_db, mock_portfolio)

    proposal = StrategyProposal(
        symbol="BTC/USDT",
        signal=SignalSide.LONG,
        # 0.004 BTC @ $50,000 = $200 notional — within the 2% position cap
        # ($200 of $10,000 equity) and the $1,000 absolute hard cap.
        amount=0.004,
        price=50000.0,
        conviction=5,
        rationale="Test",
    )
    market = MarketSnapshot(
        symbol="BTC/USDT",
        timestamp=datetime.utcnow(),
        last_price=50000.0,
        bid=49990.0,
        ask=50010.0,
        volume=100.0,
    )

    # Verify normal operation
    passed, _ = await risk.validate_proposal(proposal, market)
    assert passed is True
    assert risk.starting_equity == 10000.0

    # 2. Simulate Drawdown (drop to 9800 USDT, which is 2% loss. Limit is 1%)
    violated_state = initial_state.model_copy(update={"total_equity": 9800.0})
    mock_portfolio.refresh_state = AsyncMock(return_value=violated_state)

    passed, reason = await risk.validate_proposal(proposal, market)

    # Assertions
    assert passed is False
    assert "Drawdown limit" in reason
    assert risk.killed is True
