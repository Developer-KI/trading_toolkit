"""
strategy/ — Unified trading strategy framework.

Modules:
    base.py        — Allocation, PortfolioTarget, Strategy, SingleAssetStrategy
    built_in.py    — CompositeStrategy, PerAssetStrategy, and multi-asset built-ins
    indicators.py  — Stateless indicator functions (EMA, RSI, ATR, …)
    universe.py    — Universe + auxiliary data sources
"""

# ── Universe & data ──────────────────────────────────────────────────────────

from .universe import (
    DataSource,
    StaticDataSource,
    CallableDataSource,
    AssetData,
    Universe,
)

# ── Strategy base ────────────────────────────────────────────────────────────

from .base import (
    PortfolioTarget,
    StrategyContext,
    Strategy,
    SingleAssetStrategy,
    register_strategy,
    get_strategy,
    list_strategies,
    # Cross-exchange
    MultiExchangeTarget,
    CrossExchangeContext,
    CrossExchangeStrategy,
    register_cross_strategy,
    get_cross_strategy,
    list_cross_strategies,
)

# ── Built-in strategies ──────────────────────────────────────────────────────

from .built_in import (
    CompositeStrategy,
    PerAssetStrategy,
    ZPairsSpreadStrategy,
    CrossAssetMomentumStrategy,
    MeanReversionBasketStrategy,
)

# ── Allocation (re-exported from core for convenience) ───────────────────────

from core.models import Allocation

# ── Indicators ──────────────────────────────────────────────────────────────

from .indicators import (
    ema,
    sma,
    rsi,
    atr,
    bollinger,
    vwap_rolling,
    order_flow_imbalance,
    book_imbalance,
    compute_atr_column,
)

# ── Overlays ────────────────────────────────────────────────────────────────

from .overlay import (
    PortfolioOverlay,
    NetExposureOverlay,
    DeltaNeutralOverlay,
)

# ── Public API ──────────────────────────────────────────────────────────────

__all__ = [
    # Universe & data
    "DataSource", "StaticDataSource", "CallableDataSource",
    "AssetData", "Universe",
    # Strategy base
    "Allocation", "PortfolioTarget", "StrategyContext",
    "Strategy", "SingleAssetStrategy",
    "register_strategy", "get_strategy", "list_strategies",
    # Cross-exchange strategy
    "MultiExchangeTarget", "CrossExchangeContext", "CrossExchangeStrategy",
    "register_cross_strategy", "get_cross_strategy", "list_cross_strategies",
    # Built-in strategies
    "CompositeStrategy", "PerAssetStrategy",
    "ZPairsSpreadStrategy", "CrossAssetMomentumStrategy",
    "MeanReversionBasketStrategy",
    # Indicators
    "ema", "sma", "rsi", "atr", "bollinger",
    "vwap_rolling", "order_flow_imbalance", "book_imbalance",
    "compute_atr_column",
    # Overlays
    "PortfolioOverlay", "NetExposureOverlay", "DeltaNeutralOverlay",
]
