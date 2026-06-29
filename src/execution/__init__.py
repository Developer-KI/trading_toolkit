"""
execution/ — Exchange-agnostic live trading framework.

Supports Hyperliquid and Binance out of the box.
Add new exchanges by implementing BaseExecutor + BaseFeed (or satisfying
ExecutorProtocol / FeedProtocol from core/protocols.py for C++ extensions).
"""

from .base_executor_feed import (
    BaseExecutor,
    BaseFeed,
    BaseBarBuilder,
    MultiExchangePortfolio,
)
from .live_state import LiveState, _AssetLiveState
from .factory import create_executor, create_feed, create_bar_builder
from .live_engine import LiveEngine, _ManualKillSwitch, _sizer_config_shim
from .multi_exchange_engine import MultiExchangeEngine

# Exchange-specific (lazy-importable, but exposed for direct use)
from .hyperliquid.hyperliquid_executor import HyperliquidExecutor
from .hyperliquid.hyperliquid_live_feed import HyperliquidFeed, HyperliquidBarBuilder
from .binance.binance_executor import BinanceExecutor
from .binance.binance_live_feed import BinanceFeed

# core.models re-export so callers don't need two imports
from core.models import FillResult

__all__ = [
    # Abstract base
    "BaseExecutor", "BaseFeed", "BaseBarBuilder",
    # Portfolio aggregator
    "MultiExchangePortfolio",
    # State containers
    "LiveState", "_AssetLiveState",
    # Factory
    "create_executor", "create_feed", "create_bar_builder",
    # Engines
    "LiveEngine", "MultiExchangeEngine",
    # Engine internals (exposed for testing / subclassing)
    "_ManualKillSwitch", "_sizer_config_shim",
    # Fill result
    "FillResult",
    # Hyperliquid
    "HyperliquidExecutor", "HyperliquidFeed", "HyperliquidBarBuilder",
    # Binance
    "BinanceExecutor", "BinanceFeed",
]
