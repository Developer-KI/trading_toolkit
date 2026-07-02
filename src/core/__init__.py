"""
core/ — Exchange-agnostic contracts, data models, and events.

This package has NO dependencies on other src/ modules.
It defines the stable interfaces that both Python strategy code and
future C++ extensions must satisfy.

Import from here when you want to express a protocol/interface rather
than a concrete implementation.
"""

from .models import (
    # Primitives
    Side,
    OrderType,
    # Order book
    OrderBookLevel,
    OrderBookSnapshot,
    # Funding
    FundingSnapshot,
    # Trades & positions
    Trade,
    Position,
    # Strategy output
    Allocation,
    FillResult,
    # Portfolio
    ExchangePosition,
    AggregatedPosition,
    # Config
    BacktestConfig,
    ExchangeCredentials,
    LiveConfig,
)

from .protocols import (
    ExecutorProtocol,
    FeedProtocol,
    BarBuilderProtocol,
)

from .events import (
    BarEvent,
    TradeEvent,
    L2Event,
)

from .parser import (
    parse_l2,
    align_l2_to_ohlcv,
    l2_to_orderbook,
)

__all__ = [
    # Models
    "Side", "OrderType",
    "OrderBookLevel", "OrderBookSnapshot",
    "FundingSnapshot",
    "Trade", "Position",
    "Allocation", "FillResult",
    "ExchangePosition", "AggregatedPosition",
    "BacktestConfig", "ExchangeCredentials", "LiveConfig",
    # Protocols
    "ExecutorProtocol", "FeedProtocol", "BarBuilderProtocol",
    # Events
    "BarEvent", "TradeEvent", "L2Event",
    # Parser
    "parse_l2", "align_l2_to_ohlcv", "l2_to_orderbook",
]
