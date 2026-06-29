"""
risk/ — Consolidated risk management layer.

Extracted from strategy/ (sizing, stops) and execution/ (hard limits).
This module depends only on core/ — it has no dependency on strategy/
or execution/, making it independently testable and replaceable by C++.

Dependency: core/ → risk/ → strategy/ and execution/
"""

from .sizing import (
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

from .stops import (
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

from .limits import (
    DailyLimitState,
    check_daily_loss_limit,
)

__all__ = [
    # Sizing
    "Sizer", "SizingContext",
    "FixedFractionalSizer", "FixedNotionalSizer", "VolatilityTargetSizer",
    "KellySizer", "AntiMartingaleSizer", "DrawdownScalingSizer",
    "L2LiquiditySizer", "CompositeSizer", "default_sizer",
    # Stops
    "StopLoss", "StopResult", "StopContext",
    "FixedPercentStop", "ATRStop", "TrailingStop", "TrailingATRStop",
    "BreakevenStop", "TimeStop", "RiskRewardStop",
    "CompositeStopLoss", "SignalStop", "default_stop_loss",
    # Limits
    "DailyLimitState", "check_daily_loss_limit",
]
