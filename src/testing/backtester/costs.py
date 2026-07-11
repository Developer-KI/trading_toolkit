"""
costs.py — Modular transaction cost framework.

Each CostModel is a callable that returns the total cost (in quote currency)
for a fill.  Models can be composed (stacked) and stress-tested independently.

Typical crypto costs:
  • Exchange fee (maker/taker)
  • Spread cost / slippage
  • Market impact (large orders walking the book)
  • Funding rate (perps)
  • Borrowing cost (margin shorts)
"""

from __future__ import annotations

import abc
from typing import Any

import numpy as np

from core.models import BacktestConfig, OrderBookSnapshot, Side


# ── Abstract cost model ──────────────────────────────────────────────────────


class CostModel(abc.ABC):
    """Single cost component.  Stack them via CompositeCostModel."""

    @abc.abstractmethod
    def compute(
        self,
        price: float,
        size: float,
        side: Side,
        config: BacktestConfig,
        l2: OrderBookSnapshot | None = None,
        bar_data: dict | None = None,
    ) -> float:
        """Return cost in quote currency (always positive)."""
        ...

    @property
    @abc.abstractmethod
    def params(self) -> dict[str, Any]: ...

    def set_params(self, new: dict[str, Any]):
        for k, v in new.items():
            if hasattr(self, k):
                setattr(self, k, v)

    def __repr__(self):
        return f"{self.__class__.__name__}({self.params})"


# ── Concrete models ──────────────────────────────────────────────────────────


class ExchangeFeeCost(CostModel):
    """Flat maker/taker fee in basis points."""

    def __init__(self, maker_bps: float = 2.0, taker_bps: float = 5.0):
        self.maker_bps = maker_bps
        self.taker_bps = taker_bps

    @property
    def params(self):
        return dict(maker_bps=self.maker_bps, taker_bps=self.taker_bps)

    def compute(self, price, size, side, config, l2=None, bar_data=None):
        notional = price * abs(size)
        return notional * self.taker_bps / 1e4


class FixedSlippageCost(CostModel):
    """Constant slippage in basis points."""

    def __init__(self, slippage_bps: float = 1.0):
        self.slippage_bps = slippage_bps

    @property
    def params(self):
        return dict(slippage_bps=self.slippage_bps)

    def compute(self, price, size, side, config, l2=None, bar_data=None):
        return price * abs(size) * self.slippage_bps / 1e4


class ProportionalSlippageCost(CostModel):
    """Slippage scales linearly with order size relative to bar volume."""

    def __init__(self, impact_coef: float = 0.1, volume_col: str = "volume"):
        self.impact_coef = impact_coef
        self.volume_col = volume_col

    @property
    def params(self):
        return dict(impact_coef=self.impact_coef)

    def compute(self, price, size, side, config, l2=None, bar_data=None):
        bar_volume = (bar_data or {}).get(self.volume_col, abs(size) * 100)
        participation = abs(size) / max(bar_volume, 1e-12)
        slippage_bps = self.impact_coef * participation * 1e4
        return price * abs(size) * slippage_bps / 1e4


class L2BookSlippageCost(CostModel):
    """
    Walk the L2 order book to compute realistic fill price and slippage.
    Falls back to FixedSlippageCost when no book snapshot is available.
    """

    def __init__(self, fallback_bps: float = 2.0):
        self.fallback_bps = fallback_bps

    @property
    def params(self):
        return dict(fallback_bps=self.fallback_bps)

    def compute(self, price, size, side, config, l2=None, bar_data=None):
        if l2 is None:
            return price * abs(size) * self.fallback_bps / 1e4
        vwap = l2.vwap_fill_price(abs(size), side)
        if np.isnan(vwap):
            return price * abs(size) * self.fallback_bps / 1e4
        slippage = abs(vwap - price) * abs(size)
        return slippage


class SpreadCost(CostModel):
    """Half-spread cost: you always cross the spread on market orders."""

    def __init__(self, default_spread_bps: float = 2.0):
        self.default_spread_bps = default_spread_bps

    @property
    def params(self):
        return dict(default_spread_bps=self.default_spread_bps)

    def compute(self, price, size, side, config, l2=None, bar_data=None):
        if l2 is not None and not np.isnan(l2.spread_bps):
            half_spread_bps = l2.spread_bps / 2
        else:
            half_spread_bps = self.default_spread_bps / 2
        return price * abs(size) * half_spread_bps / 1e4


class FundingRateCost(CostModel):
    """
    Perpetual swap funding cost (accrues while position is held).

    Priority for the funding rate (highest → lowest):
      1. ``bar_data["funding_rate_ann_bps"]`` — actual per-bar snapshot
         injected by the engine from ``FundingSnapshot.rate_annualized``.
      2. ``self.annual_bps`` — explicit value set on this cost model.
    """

    def __init__(self, annual_bps: float = 0.0, bars_per_day: float = 24):
        self.annual_bps = annual_bps
        self.bars_per_day = bars_per_day

    @property
    def params(self):
        return dict(annual_bps=self.annual_bps, bars_per_day=self.bars_per_day)

    def compute(self, price, size, side, config, l2=None, bar_data=None):
        bar = bar_data or {}
        ann = bar.get("funding_rate_ann_bps", self.annual_bps)
        per_bar_bps = ann / (365 * self.bars_per_day)
        return price * abs(size) * per_bar_bps / 1e4


class MarketImpactCost(CostModel):
    """
    Square-root market impact model: cost ∝ σ * √(size/ADV).
    Standard Almgren-Chriss style.
    """

    def __init__(
        self,
        volatility_col: str = "atr",
        adv_col: str = "adv",
        impact_coef: float = 0.5,
    ):
        self.volatility_col = volatility_col
        self.adv_col = adv_col
        self.impact_coef = impact_coef

    @property
    def params(self):
        return dict(impact_coef=self.impact_coef)

    def compute(self, price, size, side, config, l2=None, bar_data=None):
        bar = bar_data or {}
        sigma = bar.get(self.volatility_col, price * 0.01)
        adv = bar.get(self.adv_col, abs(size) * 100)
        participation = abs(size) / max(adv, 1e-12)
        impact = self.impact_coef * sigma * np.sqrt(participation)
        return impact * abs(size)


# ── Null (frictionless baseline) ─────────────────────────────────────────────


class NullCostModel(CostModel):
    """Zero-cost model — frictionless baseline used when no cost model is supplied."""

    @property
    def params(self) -> dict:
        return {}

    def compute(self, price, size, side, config, l2=None, bar_data=None) -> float:
        return 0.0


# ── Composite (stack multiple cost components) ───────────────────────────────


class CompositeCostModel(CostModel):
    """
    Layer multiple cost models.  Total cost = sum of all components.

    Usage:
        costs = CompositeCostModel([
            ExchangeFeeCost(),
            SpreadCost(),
            L2BookSlippageCost(),
            FundingRateCost(),
        ])
    """

    def __init__(self, models: list[CostModel] | None = None):
        self.models = models or default_cost_stack()

    @property
    def params(self) -> dict:
        return {m.__class__.__name__: m.params for m in self.models}

    def compute(self, price, size, side, config, l2=None, bar_data=None):
        return sum(
            m.compute(price, size, side, config, l2, bar_data) for m in self.models
        )

    def breakdown(
        self, price, size, side, config, l2=None, bar_data=None
    ) -> dict[str, float]:
        """Return per-component cost breakdown."""
        return {
            m.__class__.__name__: m.compute(price, size, side, config, l2, bar_data)
            for m in self.models
        }

    def with_overrides(self, **overrides) -> "CompositeCostModel":
        """Return a copy with parameter overrides for stress testing.

        Usage:
            stressed = costs.with_overrides(ExchangeFeeCost={"taker_bps": 10})
        """
        import copy

        new = copy.deepcopy(self)
        for model in new.models:
            cls_name = model.__class__.__name__
            if cls_name in overrides:
                model.set_params(overrides[cls_name])
        return new


# ── Defaults ─────────────────────────────────────────────────────────────────


def default_cost_stack() -> list[CostModel]:
    """Sensible default cost stack for crypto perps."""
    return [
        ExchangeFeeCost(),
        SpreadCost(),
        FixedSlippageCost(),
    ]


def aggressive_cost_stack() -> list[CostModel]:
    """Cost stack that includes market impact + L2 slippage."""
    return [
        ExchangeFeeCost(),
        SpreadCost(),
        L2BookSlippageCost(),
        MarketImpactCost(),
        FundingRateCost(),
    ]