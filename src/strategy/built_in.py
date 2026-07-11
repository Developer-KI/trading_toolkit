"""
strategy/built_in.py — Built-in strategy implementations.

Single-asset:
  SingleAssetStrategy — convenience base for single-symbol strategies
  CompositeStrategy   — combine multiple SingleAssetStrategy instances with
                        weighted voting (registered as "composite")

Multi-asset:
  PerAssetStrategy    — run a different SingleAssetStrategy per symbol
  ZPairsSpreadStrategy, CrossAssetMomentumStrategy,
  MeanReversionBasketStrategy — pure Strategy subclasses
"""

from __future__ import annotations

import abc

import numpy as np
import pandas as pd

from core.models import Side, Allocation, OrderBookSnapshot
from strategy.indicators import ema, rsi, atr

from .base import (
    Strategy,
    StrategyContext,
    PortfolioTarget,
    register_strategy,
)
from core.universe import Universe


# ── SingleAssetStrategy — convenience base for single-symbol strategies ──────
class SingleAssetStrategy(Strategy):
    """
    Convenience base for single-asset strategies.

    Implement ``bar(data, idx) -> Allocation`` and optionally override
    ``setup_data(data, l2)`` for indicator pre-computation.
    ``setup``, ``generate``, and ``generate_all`` are auto-wired.
    """

    def __init__(self, symbol: str, **kw):
        super().__init__(**kw)
        self.symbol = symbol

    def setup_data(
        self, _data: pd.DataFrame, _l2: list[OrderBookSnapshot] | None = None
    ):
        """Optional: pre-compute indicator columns on data in-place."""
        pass

    @abc.abstractmethod
    def bar(self, data: pd.DataFrame, idx: int) -> Allocation:
        """Return the desired allocation for bar ``idx``."""
        ...

    # ── Auto-wired — generally do not override ──────────────────────────

    def setup(self, universe: Universe):
        data = universe.ohlcv(self.symbol)
        l2 = universe.l2(self.symbol)
        self.setup_data(data, l2)

    def generate(self, ctx: StrategyContext) -> PortfolioTarget:
        data = ctx.universe.ohlcv(self.symbol)
        alloc = self.bar(data, ctx.bar_idx)
        target = PortfolioTarget(timestamp=ctx.timestamp)
        target[self.symbol] = alloc
        return target

    def generate_all(self, universe: Universe):
        data = universe.ohlcv(self.symbol)
        n = len(data)
        sides = np.zeros(n, dtype=np.int8)
        weights = np.zeros(n, dtype=np.float64)
        confidences = np.zeros(n, dtype=np.float64)
        reasons: list[str] = []
        metas: list[dict] = []
        for i in range(n):
            a = self.bar(data, i)
            sides[i] = np.int8(a.side.value)
            weights[i] = a.weight
            confidences[i] = a.confidence
            reasons.append(a.reason)
            metas.append(dict(a.meta))
        return (
            {self.symbol: sides},
            {self.symbol: weights},
            {self.symbol: reasons},
            {self.symbol: metas},
            {self.symbol: confidences},
        )

    @property
    def params(self) -> dict:
        return {}


# ── Composite single-asset strategy ─────────────────────────────────────────
class CompositeStrategy(SingleAssetStrategy):
    """
    Combine multiple single-asset strategies with weighted voting.

    Each child strategy's ``bar()`` is called; their ``side * confidence``
    scores are summed with per-strategy weights.  If the aggregate exceeds
    ``threshold`` in either direction, the position is entered.

    Usage:
        comp = CompositeStrategy(
            symbol="BTC",
            strategies=[ema_cross, rsi_strat],
            weights=[0.6, 0.4],
        )
    """

    def __init__(
        self,
        symbol: str,
        strategies: list[SingleAssetStrategy] | None = None,
        weights: list[float] | None = None,
        threshold: float = 0.5,
        **kw,
    ):
        super().__init__(symbol=symbol, **kw)
        self.strategies = strategies or []
        self.weights = weights or [1.0 / len(self.strategies)] * len(self.strategies)
        self.threshold = threshold

    @property
    def params(self) -> dict:
        return {
            "threshold": self.threshold,
            "weights": self.weights,
            **{f"sub_{i}": s.params for i, s in enumerate(self.strategies)},
        }

    def setup_data(self, data: pd.DataFrame, l2=None):
        for s in self.strategies:
            s.setup_data(data, l2)

    def bar(self, data: pd.DataFrame, idx: int) -> Allocation:
        score = 0.0
        reasons = []
        for s, w in zip(self.strategies, self.weights):
            a = s.bar(data, idx)
            score += a.side.value * a.confidence * w
            if a.reason:
                reasons.append(f"[{s.__class__.__name__}] {a.reason}")

        if score > self.threshold:
            return Allocation(
                side=Side.LONG,
                weight=min(abs(score), 1.0),
                confidence=abs(score),
                reason=" | ".join(reasons),
            )
        elif score < -self.threshold:
            return Allocation(
                side=Side.SHORT,
                weight=min(abs(score), 1.0),
                confidence=abs(score),
                reason=" | ".join(reasons),
            )
        return Allocation(reason=f"Composite score={score:.3f} below threshold")


# ── Multi-asset per-symbol strategy ─────────────────────────────────────────
class PerAssetStrategy(Strategy):
    """
    Run a different SingleAssetStrategy on each asset independently.

    Usage:
        strategy = PerAssetStrategy(
            strategies={"ETH": eth_strategy, "BTC": btc_strategy},
        )
    """

    def __init__(self, strategies: dict[str, SingleAssetStrategy], **kwargs):
        super().__init__(**kwargs)
        self.strategies = strategies

    @property
    def params(self) -> dict:
        return {sym: s.params for sym, s in self.strategies.items()}

    def setup(self, universe: Universe):
        for sym, s in self.strategies.items():
            data = universe.ohlcv(sym)
            l2 = universe.l2(sym)
            s.setup_data(data, l2)

    def generate(self, ctx: StrategyContext) -> PortfolioTarget:
        target = PortfolioTarget(timestamp=ctx.timestamp)
        n_assets = len(self.strategies)
        for sym, s in self.strategies.items():
            data = ctx.universe.ohlcv(sym)
            alloc = s.bar(data, ctx.bar_idx)
            target[sym] = Allocation(
                side=alloc.side,
                weight=alloc.weight / max(n_assets, 1),
                confidence=alloc.confidence,
                reason=f"[{sym}] {alloc.reason}",
                meta=alloc.meta,
            )
        return target

    def generate_all(self, universe):
        n_assets = len(self.strategies)
        sides_all = {}
        weights_all = {}
        reasons_all = {}
        metas_all = {}
        confidences_all = {}
        for sym, s in self.strategies.items():
            batch = s.generate_all(universe)
            if batch is None:
                return None
            sides_all[sym]       = batch[0][sym]
            weights_all[sym]     = batch[1][sym] / max(n_assets, 1)
            reasons_all[sym]     = [f"[{sym}] {r}" for r in batch[2][sym]]
            metas_all[sym]       = batch[3][sym]
            confidences_all[sym] = batch[4][sym] if len(batch) > 4 else np.zeros(len(batch[0][sym]))
        return sides_all, weights_all, reasons_all, metas_all, confidences_all


# ═══════════════════════════════════════════════════════════════════════════
#  Built-in multi-asset strategies
# ═══════════════════════════════════════════════════════════════════════════

@register_strategy("mean_reversion_basket")
class MeanReversionBasketStrategy(Strategy):
    """
    Mean-revert each asset toward its rolling mean, weighted by
    relative deviation.  Uses RSI as a filter.

    When an asset's z-score is extreme AND RSI confirms, take a
    contrarian position.  Portfolio-level exposure is capped.
    """

    def __init__(
        self,
        lookback: int = 40,
        entry_z: float = 1.5,
        exit_z: float = 0.3,
        rsi_period: int = 14,
        rsi_oversold: float = 30,
        rsi_overbought: float = 70,
        max_total_weight: float = 1.0,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.lookback = lookback
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.max_total_weight = max_total_weight
        self._indicators: dict[str, dict] = {}

    @property
    def params(self) -> dict:
        return {
            "lookback": self.lookback,
            "entry_z": self.entry_z,
            "exit_z": self.exit_z,
            "rsi_period": self.rsi_period,
        }

    def setup(self, universe: Universe):
        for sym in universe.symbols:
            close = universe.close(sym)
            mu = close.rolling(self.lookback).mean()
            sigma = close.rolling(self.lookback).std()
            zscore = (close - mu) / sigma.replace(0, np.nan)
            rsi_vals = rsi(close, self.rsi_period)
            self._indicators[sym] = {"zscore": zscore, "rsi": rsi_vals}

    def generate(self, ctx: StrategyContext) -> PortfolioTarget:
        target = PortfolioTarget(timestamp=ctx.timestamp)
        i = ctx.bar_idx

        for sym in ctx.universe.symbols:
            if sym not in self._indicators:
                continue
            ind = self._indicators[sym]
            if i >= len(ind["zscore"]):
                continue

            z = ind["zscore"].iat[i]
            r = ind["rsi"].iat[i]

            if np.isnan(z) or np.isnan(r):
                continue

            if z < -self.entry_z and r < self.rsi_oversold:
                target[sym] = Allocation(
                    side=Side.LONG,
                    weight=min(abs(z) / 4, 0.3),
                    confidence=min(abs(z) / 3, 1.0),
                    reason=f"MR z={z:.2f} rsi={r:.0f}",
                )
            elif z > self.entry_z and r > self.rsi_overbought:
                target[sym] = Allocation(
                    side=Side.SHORT,
                    weight=min(abs(z) / 4, 0.3),
                    confidence=min(abs(z) / 3, 1.0),
                    reason=f"MR z={z:.2f} rsi={r:.0f}",
                )
            elif abs(z) < self.exit_z:
                target[sym] = Allocation(reason=f"MR exit z={z:.2f}")

        target.normalize(self.max_total_weight)
        return target

    def generate_all(self, universe):
        return self._batch_generate(universe)


