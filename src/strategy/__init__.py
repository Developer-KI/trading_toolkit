"""
strategy/ — Unified trading strategy framework.

Consolidates signals, indicators, sizing, stop-losses, and multi-asset /
multi-exchange strategy logic into a single package.

Modules:
    base.py        — Allocation, PortfolioTarget, Strategy (single-exchange)
                      MultiExchangeTarget, CrossExchangeStrategy (cross-exchange)
    built_in.py    — Signal adapters + built-in multi-asset strategies
    indicators.py  — Stateless indicator functions (EMA, RSI, ATR, …)
    sizing.py      — Pluggable position sizers
    stoploss.py    — Pluggable stop-loss / take-profit modules
    overlay.py     — Cross-exchange risk overlays (net exposure cap, delta-neutral)
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

# --- Strategy base (signal) ----
from .base import (
    Signal,
    SignalResult,
    register_signal,
    get_signal,
    list_signals,
)

# ── Strategy base (single-exchange) ─────────────────────────────────────────

from .base import (
    Allocation,
    PortfolioTarget,
    StrategyContext,
    Strategy,
    register_strategy,
    get_strategy,
    list_strategies,
)

# ── Strategy base (cross-exchange) ──────────────────────────────────────────

from .base import (
    MultiExchangeTarget,
    CrossExchangeContext,
    CrossExchangeStrategy,
    register_cross_strategy,
    get_cross_strategy,
    list_cross_strategies,
)

# ── Adapters & built-in strategies ──────────────────────────────────────────

from .built_in import (
    SingleSignalStrategy,
    CompositeSignal,
    PerAssetSignalStrategy,
    ZPairsSpreadStrategy,
    CrossAssetMomentumStrategy,
    MeanReversionBasketStrategy,
)

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

# ── Sizing ──────────────────────────────────────────────────────────────────

from risk.sizing import (
    Sizer,
    SizingContext,
    FixedFractionalSizer,
    FixedNotionalSizer,
    VolatilityTargetSizer,
    KellySizer,
    AntiMartingaleSizer,
    DrawdownScalingSizer,
    L2LiquiditySizer,
    CompositeSizer,
    default_sizer,
)

# ── Stop-loss ───────────────────────────────────────────────────────────────

from risk.stops import (
    StopLoss,
    StopResult,
    StopContext,
    FixedPercentStop,
    ATRStop,
    TrailingStop,
    TrailingATRStop,
    BreakevenStop,
    TimeStop,
    RiskRewardStop,
    CompositeStopLoss,
    SignalStop,
    default_stop_loss,
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
    # Signals base
    "Signal", "SignalResult", "register_signal", "get_signal", "list_signals",
    #Signals built in
    "CompositeSignal",
    # Single-exchange strategy
    "Allocation", "PortfolioTarget", "StrategyContext",
    "Strategy", "register_strategy", "get_strategy", "list_strategies",
    # Cross-exchange strategy
    "MultiExchangeTarget", "CrossExchangeContext", "CrossExchangeStrategy",
    "register_cross_strategy", "get_cross_strategy", "list_cross_strategies",
    # Adapters & built-ins
    "SingleSignalStrategy", "PerAssetSignalStrategy",
    "ZPairsSpreadStrategy", "CrossAssetMomentumStrategy",
    "MeanReversionBasketStrategy",
    # Indicators
    "ema", "sma", "rsi", "atr", "bollinger",
    "vwap_rolling", "order_flow_imbalance", "book_imbalance",
    "compute_atr_column",
    # Sizing
    "Sizer", "SizingContext",
    "FixedFractionalSizer", "FixedNotionalSizer", "VolatilityTargetSizer",
    "KellySizer", "AntiMartingaleSizer", "DrawdownScalingSizer",
    "L2LiquiditySizer", "CompositeSizer", "default_sizer",
    # Stop-loss
    "StopLoss", "StopResult", "StopContext",
    "FixedPercentStop", "ATRStop", "TrailingStop", "TrailingATRStop",
    "BreakevenStop", "TimeStop", "RiskRewardStop",
    "CompositeStopLoss", "SignalStop", "default_stop_loss",
    # Overlays
    "PortfolioOverlay", "NetExposureOverlay", "DeltaNeutralOverlay",
]