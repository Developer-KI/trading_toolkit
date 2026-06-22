"""
execution/ — Exchange-agnostic live trading framework.

Supports Hyperliquid and Binance out of the box.
Add new exchanges by implementing BaseExecutor + BaseFeed.
"""

from .base_executor_feed import (
    BaseExecutor,
    BaseFeed,
    BaseBarBuilder,
    FillResult,
    MultiExchangePortfolio
)
from .factory import create_executor, create_feed, create_bar_builder
from .live_engine import LiveEngine, LiveState, MultiExchangeEngine

# Exchange-specific (lazy-importable, but exposed for direct use)
from .hyperliquid.hyperliquid_executor import HyperliquidExecutor
from .hyperliquid.hyperliquid_live_feed import HyperliquidFeed, HyperliquidBarBuilder
from .binance.binance_executor import BinanceExecutor
from .binance.binance_live_feed import BinanceFeed

__all__ = [
    # Abstract base
    "BaseExecutor", "BaseFeed", "BaseBarBuilder", "FillResult",
    # Factory
    "create_executor", "create_feed", "create_bar_builder",
    # Engines
    "LiveEngine", "LiveState", "MultiExchangeEngine",
    # Hyperliquid
    "HyperliquidExecutor", "HyperliquidFeed", "HyperliquidBarBuilder",
    # Binance
    "BinanceExecutor", "BinanceFeed",
    #Portfolio
    "MultiExchangePortfolio"
]