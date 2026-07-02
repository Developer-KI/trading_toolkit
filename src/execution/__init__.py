"""
execution/ — Exchange-agnostic live trading framework.

Supports Hyperliquid, Binance, and Alpaca out of the box.
Add new exchanges by implementing BaseExecutor (execution/) + BaseFeed (data/feeds/)
and registering in factory.py. See ExecutorProtocol / FeedProtocol in core/protocols.py
for the C++ pybind11 extension path.
"""

from .base_executor_feed import (
    BaseExecutor,
    BaseBarBuilder,
    MultiExchangePortfolio,
)
from .live_state import LiveState, _AssetLiveState
from .factory import create_executor, create_feed, create_bar_builder
from .single_exchange_engine import LiveEngine, _ManualKillSwitch, _sizer_config_shim
from .multi_exchange_engine import MultiExchangeEngine

# Executors (exchange-specific order placement)
from .hyperliquid.hyperliquid_executor import HyperliquidExecutor
from .binance.binance_executor import BinanceExecutor
from .alpaca.alpaca_executor import AlpacaExecutor

# Feeds now live in data/feeds/ (canonical data acquisition layer)
from data.feeds.hyperliquid import HyperliquidFeed
from data.feeds.binance import BinanceFeed
from data.feeds.alpaca import AlpacaFeed

# core.models re-export so callers don't need two imports
from core.models import FillResult

__all__ = [
    # Abstract base
    "BaseExecutor", "BaseBarBuilder",
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
    "HyperliquidExecutor", "HyperliquidFeed",
    # Binance
    "BinanceExecutor", "BinanceFeed",
    # Alpaca
    "AlpacaExecutor", "AlpacaFeed",
]
