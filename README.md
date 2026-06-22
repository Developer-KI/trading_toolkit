# Quantitative Trading Framework

A modular, multi-asset, multi-exchange Python framework for developing, backtesting, stress-testing, and live-trading quantitative strategies on crypto perpetual futures and spot. Built with Hyperliquid and Binance Futures support out of the box, and extensible to any exchange via abstract interfaces.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Architecture](#architecture)
3. [File Structure](#file-structure)
4. [Dependency Chain](#dependency-chain)
5. [Core Concepts](#core-concepts)
6. [Writing Strategies](#writing-strategies)
   - [Signal API (Single-Asset)](#signal-api-single-asset)
   - [Strategy API (Multi-Asset)](#strategy-api-multi-asset)
   - [CrossExchangeStrategy API](#crossexchangestrategy-api)
7. [Backtesting](#backtesting)
   - [Single-Asset Backtest](#single-asset-backtest)
   - [Multi-Asset Backtest](#multi-asset-backtest)
   - [Pluggable Components](#pluggable-components)
   - [BacktestResult](#backtestresult)
8. [Stress Testing](#stress-testing)
   - [Signal Parameter Sweep](#1-signal-parameter-sweep)
   - [Strategy Parameter Sweep](#2-strategy-parameter-sweep)
   - [Cost Stress Test](#3-cost-stress-test)
   - [Regime Stress Test](#4-regime-stress-test)
   - [Monte Carlo Simulation](#5-monte-carlo-simulation)
9. [Live Trading](#live-trading)
   - [Single-Exchange Live](#single-exchange-live)
   - [Multi-Exchange Live](#multi-exchange-live)
   - [Risk Management](#risk-management)
10. [Extending the Framework](#extending-the-framework)
    - [Adding a New Exchange](#adding-a-new-exchange)
    - [Custom Data Sources](#custom-data-sources)
    - [Custom Sizers, Stops, and Cost Models](#custom-sizers-stops-and-cost-models)

---

## Quick Start

### Single-asset backtest (simplest path)

```python
from backtester.engine import Backtester
from abstract.models import BacktestConfig, Side
from strategy.base import Signal, SignalResult, register_signal
from strategy.indicators import ema

@register_signal("ema_cross")
class EMACross(Signal):
    def __init__(self, fast=12, slow=26, **kw):
        super().__init__(**kw)
        self.fast, self.slow = fast, slow

    @property
    def params(self):
        return {"fast": self.fast, "slow": self.slow}

    def setup(self, data, l2=None):
        data["ema_f"] = ema(data["close"], self.fast)
        data["ema_s"] = ema(data["close"], self.slow)

    def generate(self, data, idx):
        if idx < self.slow:
            return SignalResult()
        if data["ema_f"].iat[idx] > data["ema_s"].iat[idx]:
            return SignalResult(target_side=Side.LONG, target_weight=0.8, confidence=0.6)
        return SignalResult(target_side=Side.SHORT, target_weight=0.8, confidence=0.6)

bt = Backtester(signal=EMACross())
result = bt.run(data=ohlcv_df)
print(result.summary())
result.plot_equity("equity.png")
```

### Multi-asset backtest

```python
from backtester.engine import Backtester
from strategy.universe import Universe
from strategy.built_in import CrossAssetMomentumStrategy

universe = Universe(symbols=["ETH", "BTC", "SOL"])
universe.add_asset("ETH", eth_df)
universe.add_asset("BTC", btc_df)
universe.add_asset("SOL", sol_df)

strategy = CrossAssetMomentumStrategy(long_n=2, short_n=1, lookback=20)

result = Backtester(strategy=strategy).run(universe=universe)
print(result.summary())
```

### Deploy the same signal live

```python
from execution.live_engine import LiveEngine
from abstract.models import LiveConfig

engine = LiveEngine(
    signal=EMACross(),
    config=LiveConfig(
        exchange="hyperliquid",
        account_address="0x...",
        secret_key="0x...",
        symbol="ETH",
        use_testnet=True,
    ),
)
engine.start()  # blocks, trades on every bar close
```

No code changes to the signal. The backtester and live engine share the same `Signal.setup()` / `Signal.generate()` interface.

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                          BACKTESTER                                │
│  Strategy ──→ Engine loop ──→ Sizer ──→ StopLoss ──→ CostModel     │
│    ↑          (bar-by-bar)                                         │
│    │                                                               │
│  static OHLCV Universe + L2 + FundingSnapshots + Aux DataSources   │
└────────────────────────────────────────────────────────────────────┘
          ↕ same Strategy / Signal interfaces
┌────────────────────────────────────────────────────────────────────┐
│                        LIVE ENGINE                                 │
│  Strategy ──→ LiveEngine._process_bar() ──→ Sizer ──→ StopLoss     │
│    ↑          (on each bar close)           │                      │
│    │                                        ↓                      │
│  rolling Universe                      BaseExecutor                │
│    ↑                          (HyperliquidExecutor / BinanceExec)  │
│    │                                                               │
│  BaseBarBuilder ←── BaseFeed (WebSocket)                           │
│    (trades → OHLCV)    (L2 book + trades + rates)                  │
│                                                                    │
│  _ManualKillSwitch (stdin listener: press 'q' + Enter to flatten)  │
└────────────────────────────────────────────────────────────────────┘
          ↕ extends to multiple exchanges
┌────────────────────────────────────────────────────────────────────┐
│                    MULTI-EXCHANGE ENGINE                           │
│  CrossExchangeStrategy ──→ MultiExchangeTarget                     │
│    ↑                          │                                    │
│    │                          ├──→ HyperliquidExecutor (HL orders) │
│    │                          └──→ BinanceExecutor (BN orders)     │
│    │                                                               │
│  Per-exchange Universes + MultiExchangePortfolio                   │
│  PortfolioOverlays (net exposure cap, delta-neutral, …)            │
│                                                                    │
│  _ManualKillSwitch (stdin listener: press 'q' + Enter to flatten)  │
└────────────────────────────────────────────────────────────────────┘
```

---

## File Structure

```
┌──────────────────────────────────────────────────────────────────────┐
│  Your strategy script                                                │
│  (defines signals/strategies, picks sizers/stops, runs backtest)     │
├──────────────────────────────────────────────────────────────────────┤
│  strategy/              Multi-asset strategy layer                   │
│    base.py                Signal, Strategy, CrossExchangeStrategy    │
│                           PortfolioTarget, Allocation, registries    │
│    universe.py            Universe, AssetData, DataSource            │
│    built_in.py            SingleSignalStrategy, adapters,            │
│                           ZPairsSpread, Momentum, MeanReversion      │
│    indicators.py          ema, sma, rsi, atr, bollinger, vwap, OFI  │
│    sizing.py              8 sizers + CompositeSizer                  │
│    stoploss.py            9 stops + CompositeStopLoss                │
│    overlay.py             NetExposureOverlay, DeltaNeutralOverlay    │
├──────────────────────────────────────────────────────────────────────┤
│  abstract/              Shared data models                           │
│    models.py              BacktestConfig, LiveConfig,                │
│                           ExchangeCredentials, Side, Trade,          │
│                           Position, OrderBookSnapshot,               │
│                           FundingSnapshot,                           │
│                           AggregatedPosition, ExchangePosition       │
├──────────────────────────────────────────────────────────────────────┤
│  backtester/            Backtest engine + cost/stress framework      │
│    engine.py              Backtester (handles Signal or Strategy)    │
│    costs.py               7 cost models + CompositeCostModel         │
│    stress.py              Signal/Strategy/Cost/Regime/MC stress      │
├──────────────────────────────────────────────────────────────────────┤
│  execution/             Live trading (exchange-agnostic)             │
│    base_executor_feed.py  BaseExecutor, BaseFeed, BaseBarBuilder,    │
│                           FillResult, MultiExchangePortfolio         │
│    factory.py             create_executor/feed/bar_builder           │
│    live_engine.py         LiveEngine, MultiExchangeEngine,           │
│                           _ManualKillSwitch                          │
│    hyperliquid/                                                      │
│      hyperliquid_executor.py  Order placement, position queries      │
│      hyperliquid_live_feed.py WebSocket L2/trade/candle feed         │
│    binance/                                                          │
│      binance_executor.py      Binance Futures executor               │
│      binance_live_feed.py     Binance Futures WebSocket feed         │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Dependency Chain

```
abstract/          Shared data models (config, position, trade, order book, funding)
     ↑
strategy/          Strategy logic, indicators, sizing, stop-losses, universe
     ↑
backtester/        Backtest engine, cost models, stress tests
     ↑
execution/         Live engine, exchange adapters, WebSocket feeds
```

The **dual-API design** is a central principle: every component accepts either the legacy single-asset `Signal + DataFrame` interface or the new multi-asset `Strategy + Universe` interface. Old code never needs rewriting — it works as-is alongside the new API.

---

## Core Concepts

**Signal** — A single-asset trading signal. Operates on one OHLCV DataFrame. Implements `setup(data)` for vectorized indicator pre-computation and `generate(data, idx)` which returns a `SignalResult` per bar with target side, weight, confidence, and optional SL/TP levels.

**Strategy** — A multi-asset generalization of Signal. Sees an entire `Universe` (all assets plus auxiliary data) and returns a `PortfolioTarget` mapping each symbol to an `Allocation` (side, weight, confidence). The engine diffs the target against current positions and executes the necessary trades.

**Universe** — The data container. Holds per-asset OHLCV DataFrames, optional L2 book snapshots for choseb book depth, optional per-bar funding snapshots of rate, orcale and mark price, and any number of pluggable `DataSource` objects (sentiment, on-chain metrics, etc.). Strategies receive a Universe both in backtesting and live trading, so the same strategy code runs in both contexts.

**Allocation** — The desired state for one asset: direction (LONG/SHORT/FLAT), portfolio weight (0–1), confidence score, and optional order parameters.

**PortfolioTarget** — A dict mapping `symbol → Allocation`. Returned by `Strategy.generate()`. Assets not in the target are assumed FLAT. The target can normalize its total weight to prevent over-allocation.

**Pluggable Components** — Sizers, stop-losses, and cost models are all abstract base classes with concrete implementations that can be swapped, composed, and stress-tested independently. Each component can be specified as a single shared instance or a `dict[symbol, instance]` for per-asset overrides.

---

## Writing Strategies

### End-to-End Workflow and Registries

All signals, strategies, and cross-exchange strategies each have a name registry:

```python
from strategy.base import register_signal, get_signal, list_signals
from strategy.base import register_strategy, get_strategy, list_strategies
from strategy.base import register_cross_strategy, get_cross_strategy, list_cross_strategies

@register_signal("my_signal")
class MySignal(Signal): ...

# Later:
cls = get_signal("my_signal")
sig = cls(fast=12, slow=26)
```

The same strategy code runs unchanged from backtest through live execution. The framework handles the translation between historical DataFrames and real-time WebSocket feeds transparently through the Universe abstraction. This can be done using the following framework:

```
1.  Write a Strategy (or Signal) or use the registry
2.  Build a Universe with your data
3.  Backtest:     Backtester(strategy=...).run(universe=...)
4.  Stress test:  StrategyStressTest(...).run(universe=...)
                  CostStressTest(...).run(strategy=..., universe=...)
                  RegimeStressTest(...).run(strategy=..., universe=...)
                  MonteCarloStress(...).run(backtest_result)
5.  Go live:      LiveEngine(strategy=..., config=LiveConfig(...)).start()
```

### Signal API (Single-Asset)

The original API for single-asset strategies. Implement `Signal` and register it with a name:

```python
from strategy.base import Signal, SignalResult, register_signal
from strategy.indicators import ema, rsi
from abstract.models import Side


@register_signal("ema_crossover")
class EMACrossoverSignal(Signal):
    def __init__(self, fast: int = 12, slow: int = 26, **kw):
        super().__init__(**kw)
        self.fast = fast
        self.slow = slow

    @property
    def params(self):
        return {"fast": self.fast, "slow": self.slow}

    def setup(self, data, l2=None):
        """Pre-compute indicators on the DataFrame (in-place)."""
        data["ema_fast"] = ema(data["close"], self.fast)
        data["ema_slow"] = ema(data["close"], self.slow)
        data["rsi"] = rsi(data["close"])

    def generate(self, data, idx):
        """Called on every bar. Return a SignalResult."""
        if idx < self.slow:
            return SignalResult()

        fast_val = data["ema_fast"].iat[idx]
        slow_val = data["ema_slow"].iat[idx]
        rsi_val  = data["rsi"].iat[idx]

        if fast_val > slow_val and rsi_val < 70:
            return SignalResult(
                target_side=Side.LONG,
                target_weight=0.8,
                confidence=min((fast_val - slow_val) / slow_val * 100, 1.0),
                reason=f"EMA bullish cross, RSI={rsi_val:.0f}",
            )
        elif fast_val < slow_val and rsi_val > 30:
            return SignalResult(
                target_side=Side.SHORT,
                target_weight=0.8,
                confidence=min((slow_val - fast_val) / slow_val * 100, 1.0),
                reason=f"EMA bearish cross, RSI={rsi_val:.0f}",
            )
        return SignalResult(reason="No signal")
```

You can combine multiple signals with weighted voting using `CompositeSignal`:

```python
from strategy.built_in import CompositeSignal

composite = CompositeSignal(
    signals=[ema_signal, momentum_signal, book_signal],
    weights=[0.5, 0.3, 0.2],
    threshold=0.4,
)
```

### Strategy API (Multi-Asset)

For strategies that operate across multiple assets simultaneously:

```python
from strategy.base import Strategy, StrategyContext, PortfolioTarget, Allocation, register_strategy
from strategy.indicators import rsi, atr
from strategy.universe import Universe
from abstract.models import Side
import numpy as np


@register_strategy("momentum_basket")
class MomentumBasketStrategy(Strategy):
    def __init__(self, lookback: int = 20, top_n: int = 2, total_weight: float = 0.8, **kw):
        super().__init__(**kw)
        self.lookback = lookback
        self.top_n = top_n
        self.total_weight = total_weight

    @property
    def params(self):
        return {"lookback": self.lookback, "top_n": self.top_n, "total_weight": self.total_weight}

    def setup(self, universe: Universe):
        """Pre-compute indicators across all assets. Called once."""
        self._symbols = universe.symbols
        for sym in self._symbols:
            data = universe.ohlcv(sym)
            data["rsi"] = rsi(data["close"])

    def generate(self, ctx: StrategyContext) -> PortfolioTarget:
        """Called every bar. Return desired portfolio state."""
        target = PortfolioTarget(timestamp=ctx.timestamp)

        if ctx.bar_idx < self.lookback:
            return target

        # Rank assets by momentum
        scores = {}
        for sym in self._symbols:
            ohlcv = ctx.universe.ohlcv(sym)
            cur  = ohlcv["close"].iat[ctx.bar_idx]
            prev = ohlcv["close"].iat[ctx.bar_idx - self.lookback]
            if prev > 0:
                scores[sym] = (cur - prev) / prev

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        per_asset_w = self.total_weight / self.top_n

        for sym, mom in ranked[:self.top_n]:
            target[sym] = Allocation(
                side=Side.LONG,
                weight=per_asset_w,
                confidence=min(abs(mom) * 10, 1.0),
                reason=f"Momentum top: {mom:.4f}",
            )

        target.normalize(self.total_weight)
        return target
```

The `StrategyContext` gives you access to everything you need each bar: the full universe, current bar index, current equity, all positions, and trade history. Convenience methods like `ctx.price("ETH")`, `ctx.ohlcv("ETH")`, `ctx.funding("ETH")`, `ctx.aux("sentiment")`, `ctx.is_positioned("ETH")`, and `ctx.net_exposure()` keep strategy code clean.

### Wrapping Existing Signals

You never need to rewrite old signals. Wrap them with adapters:

```python
from strategy.built_in import SingleSignalStrategy, PerAssetSignalStrategy

# Wrap one signal for one asset
strategy = SingleSignalStrategy(signal=my_ema_signal, symbol="ETH")

# Run different signals per asset
strategy = PerAssetSignalStrategy(signals={
    "ETH": ema_signal_eth,
    "BTC": momentum_signal_btc,
    "SOL": mean_rev_signal_sol,
})
```

### CrossExchangeStrategy API

For strategies that trade across multiple exchanges (funding arbitrage, cross-exchange hedging):

```python
from strategy.base import (
    CrossExchangeStrategy, CrossExchangeContext,
    MultiExchangeTarget, Allocation, register_cross_strategy,
)
from strategy.universe import Universe
from abstract.models import Side


@register_cross_strategy("funding_arb")
class FundingArbStrategy(CrossExchangeStrategy):
    def __init__(self, threshold: float = 0.0003, **kw):
        super().__init__(**kw)
        self.threshold = threshold

    @property
    def params(self):
        return {"threshold": self.threshold}

    def setup(self, universes: dict[str, Universe]):
        pass

    def generate(self, ctx: CrossExchangeContext) -> MultiExchangeTarget:
        target = MultiExchangeTarget(timestamp=ctx.timestamp)

        # Read live funding rates from each exchange
        hl_funding = ctx.funding("ETH", "hyperliquid")
        bn_funding = ctx.funding("ETH", "binance")

        if hl_funding and bn_funding:
            spread = hl_funding.rate - bn_funding.rate
            if abs(spread) > self.threshold:
                long_ex = "hyperliquid" if spread > 0 else "binance"
                short_ex = "binance" if spread > 0 else "hyperliquid"
                target[long_ex, "ETH"]  = Allocation(side=Side.LONG,  weight=0.3)
                target[short_ex, "ETH"] = Allocation(side=Side.SHORT, weight=0.3)

        return target
```

Three built-in multi-asset strategies are registered out of the box: `ZPairsSpreadStrategy` (z-score pairs trading), `CrossAssetMomentumStrategy` (rank by return, long top N / short bottom N), and `MeanReversionBasketStrategy` (z-score + RSI mean reversion). Look them up by name:

```python
from strategy.base import get_strategy
PairsStrategy = get_strategy("pairs_z_spread")
strat = PairsStrategy(asset_a="ETH", asset_b="BTC", lookback=60, entry_z=2.0)
```

---

## Backtesting

### Single-Asset Backtest

The classic API works exactly as before:

```python
from backtester.engine import Backtester
from abstract.models import BacktestConfig

signal = EMACrossoverSignal(fast=12, slow=26)

bt = Backtester(
    signal=signal,
    config=BacktestConfig(
        initial_capital=100_000,
        leverage=2.0,
        taker_fee_bps=5.0,
        slippage_bps=1.0,
    ),
)

result = bt.run(data=eth_ohlcv_df)
print(result.summary())
result.plot_equity(save_path="equity.png")
result.to_csv("trades.csv")
```

### Multi-Asset Backtest

Build a Universe, attach a Strategy, and run:

```python
from backtester.engine import Backtester
from strategy.universe import Universe, StaticDataSource
from strategy.built_in import CrossAssetMomentumStrategy
from abstract.models import FundingSnapshot

# 1. Build Universe
universe = Universe(symbols=["ETH", "BTC", "SOL"])
universe.add_asset("ETH", eth_df)
universe.add_asset("BTC", btc_df)
universe.add_asset("SOL", sol_df)

# Optional: attach per-bar funding rate snapshots
eth_funding = [
    FundingSnapshot(timestamp=row.name, rate=row["rate"], rate_annualized=row["rate"] * 3 * 365 * 1e4)
    for _, row in funding_df.iterrows()
]
universe.add_asset("ETH", eth_df, funding=eth_funding)

# Optional: add auxiliary data sources
universe.add_data_source(StaticDataSource("sentiment", sentiment_df))

# 2. Create Strategy
strategy = CrossAssetMomentumStrategy(
    long_n=2, short_n=1,
    lookback=20, total_weight=0.8,
)

# 3. Backtest
bt = Backtester(strategy=strategy)
result = bt.run(universe=universe)

print(result.summary())
print(result.trades_by_symbol("ETH"))
```

When `FundingSnapshot` lists are attached, the engine automatically injects `funding_rate` and `funding_rate_ann_bps` into every bar's data dict. The `FundingRateCost` model picks these up, so backtest funding costs reflect actual historical rates instead of a flat assumption.

### Pluggable Components

Sizers, stop-losses, and cost models can be shared across all assets or overridden per asset:

```python
from strategy.sizing import VolatilityTargetSizer, KellySizer, CompositeSizer
from strategy.stoploss import TrailingATRStop, TimeStop, BreakevenStop, CompositeStopLoss
from backtester.costs import CompositeCostModel, ExchangeFeeCost, SpreadCost, MarketImpactCost

# Composite sizer: take the most conservative of vol-target and Kelly
sizer = CompositeSizer(
    sizers=[VolatilityTargetSizer(target_vol=0.15), KellySizer(kelly_frac=0.5)],
    mode="min",
)

# Composite stop-loss: first to trigger wins
stop = CompositeStopLoss([
    TrailingATRStop(atr_mult=2.5),
    TimeStop(max_bars=48),
    BreakevenStop(activation_pct=1.5),
])

# Custom cost stack
costs = CompositeCostModel([
    ExchangeFeeCost(taker_bps=5.0),
    SpreadCost(default_spread_bps=2.0),
    MarketImpactCost(impact_coef=0.5),
])

# Per-asset overrides
bt = Backtester(
    strategy=my_strategy,
    sizer={"ETH": VolatilityTargetSizer(target_vol=0.20),
           "BTC": VolatilityTargetSizer(target_vol=0.12)},
    stop_loss=stop,          # shared across all assets
    cost_model=costs,        # shared across all assets
)
```

**Available Sizers:** `FixedFractionalSizer`, `FixedNotionalSizer`, `VolatilityTargetSizer`, `KellySizer`, `AntiMartingaleSizer`, `DrawdownScalingSizer`, `L2LiquiditySizer`, `CompositeSizer`.

**Available Stops:** `FixedPercentStop`, `ATRStop`, `TrailingStop`, `TrailingATRStop`, `BreakevenStop`, `TimeStop`, `RiskRewardStop`, `CompositeStopLoss`, `SignalStop`.

**Available Cost Models:** `ExchangeFeeCost`, `FixedSlippageCost`, `ProportionalSlippageCost`, `L2BookSlippageCost`, `SpreadCost`, `FundingRateCost`, `MarketImpactCost`, `CompositeCostModel`.

`FundingRateCost` resolves the funding rate in priority order: per-bar snapshot from `bar_data["funding_rate_ann_bps"]` (injected from `FundingSnapshot`), then the model's own `annual_bps` override, then `config.funding_rate_annual_bps` as fallback.

### BacktestResult

The result object provides:

- `result.summary()` — dict with total return, annualized return, Sharpe, Sortino, Calmar, max drawdown, win rate, profit factor, fees, and more.
- `result.trades_df()` — full trade log as a DataFrame.
- `result.trades_by_symbol("ETH")` — per-symbol trade filter (multi-asset runs).
- `result.equity_curve` — pandas Series of equity over time.
- `result.plot_equity()` — generates equity curve + drawdown chart.
- `result.to_csv()` — exports trades to CSV.
- `result.positions_log` / `result.allocation_log` — per-bar, per-asset position and allocation DataFrames (multi-asset only).

---

## Stress Testing

Five stress test classes help you probe strategy robustness. All return a `StressResult` with a summary DataFrame and optional plots.

### 1. Signal Parameter Sweep

Grid search over signal constructor parameters:

```python
from backtester.stress import SignalStressTest

sst = SignalStressTest(
    signal_cls=EMACrossoverSignal,
    param_grid={"fast": [5, 8, 12, 20], "slow": [21, 26, 50]},
    fixed_params={},
    n_random=None,  # set to e.g. 20 for random subset
)

result = sst.run(data=eth_df)
print(result.summary)
print(result.best("sharpe_ratio"))
result.plot_heatmap("fast", "slow", z="sharpe_ratio")
```

### 2. Strategy Parameter Sweep

The multi-asset analogue of SignalStressTest:

```python
from backtester.stress import StrategyStressTest

sst = StrategyStressTest(
    strategy_cls=ZPairsSpreadStrategy,
    param_grid={
        "lookback": [30, 60, 120],
        "entry_z":  [1.5, 2.0, 2.5],
        "exit_z":   [0.3, 0.5, 1.0],
    },
    fixed_params={"asset_a": "ETH", "asset_b": "BTC"},
)

result = sst.run(universe=my_universe)
result.plot_heatmap("lookback", "entry_z")
```

### 3. Cost Stress Test

Sweep transaction cost assumptions to find where alpha breaks down. Works with both APIs:

```python
from backtester.stress import CostStressTest

cst = CostStressTest(
    cost_grid={
        "ExchangeFeeCost": {"taker_bps": [3, 5, 8, 12]},
        "SpreadCost":      {"default_spread_bps": [1, 2, 4, 8]},
    },
)

# Old API
result = cst.run(signal=my_signal, data=eth_df)

# New API
result = cst.run(strategy=my_strategy, universe=my_universe)
```

### 4. Regime Stress Test

Split data by market regime and backtest each subset independently. Built-in classifiers include volatility regime, trend regime, and volume regime. Custom classifiers are supported:

```python
from backtester.stress import RegimeStressTest

rst = RegimeStressTest(
    regime_fn=RegimeStressTest.trend_regime,  # or .volume_regime or custom
    regime_symbol="BTC",  # reference asset for regime classification
)

# Old API
result = rst.run(signal=my_signal, data=eth_df)

# New API
result = rst.run(strategy=my_strategy, universe=my_universe)
```

Custom regime classifier — any function that takes a DataFrame and returns a Series of labels:

```python
def my_regime(data):
    vol = data["close"].pct_change().rolling(20).std()
    labels = pd.Series("normal", index=data.index)
    labels[vol > vol.quantile(0.8)] = "crisis"
    labels[vol < vol.quantile(0.2)] = "calm"
    return labels

rst = RegimeStressTest(regime_fn=my_regime)
```

### 5. Monte Carlo Simulation

Bootstrap or shuffle trade PnLs to build confidence intervals around your backtest results:

```python
from backtester.stress import MonteCarloStress

mc = MonteCarloStress(
    n_simulations=1000,
    method="bootstrap",  # or "shuffle" or "block_bootstrap"
)

result = mc.run(backtest_result)

print(f"Median return:  {result.meta['median_return']:.2f}%")
print(f"5th percentile: {result.meta['5th_pctl_return']:.2f}%")
print(f"95th percentile: {result.meta['95th_pctl_return']:.2f}%")

mc.plot_distribution(result, metric="total_return_pct")
mc.plot_distribution(result, metric="max_drawdown_pct")
```

### StressResult

All stress tests return a `StressResult`:

- `result.summary` — DataFrame with one row per scenario.
- `result.results` — dict mapping scenario key to full `BacktestResult`.
- `result.best("sharpe_ratio")` / `result.worst(...)` — best/worst scenario rows.
- `result.plot_heatmap(x, y, z)` — 2D parameter heatmap for any metric.
- `result.to_csv()` — export summary to CSV.

---

## Live Trading

### Single-Exchange Live

The `LiveEngine` runs a strategy in real-time on one exchange. The API accepts either a Signal or a Strategy, just like the backtester:

```python
from execution.live_engine import LiveEngine
from abstract.models import LiveConfig
from strategy.sizing import VolatilityTargetSizer
from strategy.stoploss import TrailingATRStop, CompositeStopLoss, TimeStop

config = LiveConfig(
    exchange="hyperliquid",
    account_address="0x...",
    secret_key="0x...",
    use_testnet=True,
    symbols=["ETH", "BTC"],
    bar_interval_s=60,
    warmup_bars=200,
    risk_per_trade=0.02,
    max_position_pct=0.25,
    leverage=3.0,
    max_daily_loss_pct=5.0,
    max_daily_trades=50,
    order_type="market",
    log_dir="logs/live",
)

engine = LiveEngine(
    strategy=my_strategy,
    config=config,
    sizer=VolatilityTargetSizer(target_vol=0.15),
    stop_loss=CompositeStopLoss([
        TrailingATRStop(atr_mult=2.5),
        TimeStop(max_bars=48),
    ]),
)

engine.start()  # Blocks until interrupted or kill switch triggers
```

**Lifecycle:** The engine creates an executor and feeds via the factory, sets leverage, syncs account state, seeds bar builders with historical candles for warm-up, starts WebSocket feeds, and enters the main heartbeat loop. On each new bar close, it syncs positions from the exchange, runs stop-loss checks, calls `strategy.generate()`, diffs the target against current positions, and executes trades.

### Multi-Exchange Live

The `MultiExchangeEngine` runs strategies across multiple exchanges simultaneously, with a shared `MultiExchangePortfolio` for cross-exchange position tracking:

```python
from execution.live_engine import MultiExchangeEngine
from abstract.models import LiveConfig, ExchangeCredentials
from strategy.overlay import NetExposureOverlay

config = LiveConfig(
    exchanges=[
        ExchangeCredentials(
            exchange="hyperliquid",
            account_address="0x...",
            secret_key="0x...",
            testnet=True,
        ),
        ExchangeCredentials(
            exchange="binance",
            api_key="...",
            api_secret="...",
            testnet=True,
            symbol_map={"ETH": "ETHUSDT", "BTC": "BTCUSDT"},
        ),
    ],
    symbols=["ETH", "BTC"],
    bar_interval_s=60,
    warmup_bars=200,
    leverage=2.0,
)

# Option A: one CrossExchangeStrategy across all exchanges
engine = MultiExchangeEngine(
    cross_strategy=my_funding_arb_strategy,
    config=config,
)

# Option B: independent strategies per exchange + risk overlay
engine = MultiExchangeEngine(
    per_exchange_strategies={
        "hyperliquid": momentum_strategy,
        "binance": mean_reversion_strategy,
    },
    overlay=NetExposureOverlay(max_net_weight=0.5),
    config=config,
)

engine.start()
```

**Portfolio Overlays** sit between strategy output and execution. After each exchange's strategy generates its PortfolioTarget, overlays can adjust allocations for cross-exchange constraints. Built-in overlays include `NetExposureOverlay` (cap net directional exposure) and `DeltaNeutralOverlay` (auto-generate hedge legs to enforce delta-neutral).

### Risk Management

The live engine includes several built-in risk controls:

- **Automated daily loss kill switch** — If daily PnL loss exceeds `max_daily_loss_pct`, all positions are flattened across all exchanges and the engine shuts down.
- **Manual kill switch** — Press `q` + Enter at any time to immediately flatten all positions and shut down. Runs in a background thread listening on stdin. On non-interactive terminals (Docker, systemd) it silently disables itself. The kill key is configurable via the `KILL_KEY` constant in `live_engine.py`.
- **Daily trade limit** — `max_daily_trades` caps the number of new trades per day.
- **Max position sizing** — Each position is capped by `max_position_pct` of equity (single-asset) or by the allocation weight (multi-asset).
- **Position sync** — On every bar, the engine compares local position state against exchange state and logs warnings on mismatch.
- **Stall detection** — A watchdog in the heartbeat loop detects when a feed stops producing new bars.

---

## Extending the Framework

### Adding a New Exchange

Implement `BaseExecutor` and `BaseFeed`, then add branches in `factory.py`:

```python
# execution/myexchange/myexchange_executor.py
from execution.base_executor_feed import BaseExecutor, FillResult
from abstract.models import Side, Position, FundingSnapshot

class MyExchangeExecutor(BaseExecutor):
    @property
    def exchange_name(self) -> str:
        return "myexchange"

    def get_equity(self) -> float: ...
    def get_position(self, symbol) -> Position: ...
    def get_mid_price(self, symbol) -> float: ...
    def get_open_orders(self, symbol) -> list[dict]: ...
    def market_order(self, symbol, side, size, reduce_only=False) -> FillResult: ...
    def limit_order(self, symbol, side, size, price, reduce_only=False) -> FillResult: ...
    def cancel_all(self, symbol) -> int: ...
    def close_position(self, symbol) -> FillResult: ...
    def set_leverage(self, symbol, leverage, cross=True): ...
    def fetch_historical_candles(self, symbol, interval, start_ms, end_ms) -> list[dict]: ...
    def fetch_funding_rate(self, symbol) -> FundingSnapshot | None: ...
```

Similarly for `BaseFeed`: implement `start(on_trade, on_candle, on_l2)`, `stop()`, and the `latest_l2` property. The feed converts exchange-native messages into the standard dict format (`{timestamp, price, size, side}` for trades; `{open, high, low, close, volume, ...}` for candles).

Then register in `factory.py`:

```python
elif name == "myexchange":
    from .myexchange.myexchange_executor import MyExchangeExecutor
    return MyExchangeExecutor(...)
```

### Custom Data Sources

Plug any auxiliary data into the Universe via `DataSource`:

```python
from strategy.universe import StaticDataSource, CallableDataSource
import pandas as pd

# Static data source (backtesting)
sentiment_source = StaticDataSource("sentiment", sentiment_df)

# Live data source (queries API each time)
def fetch_onchain(symbols, start=None, end=None):
    return pd.DataFrame(...)

onchain_source = CallableDataSource("onchain", fetch_onchain)

universe.add_data_source(sentiment_source)
universe.add_data_source(onchain_source)

# Access in strategy:
# ctx.aux("sentiment")   → DataFrame
# ctx.universe.aux_at("onchain", idx, "ETH")  → dict
```

### Custom Sizers, Stops, and Cost Models

All three follow the same pattern: subclass the ABC, implement the core method, expose `params`:

```python
from strategy.sizing import Sizer, SizingContext

class MyCustomSizer(Sizer):
    def __init__(self, param_a: float = 0.5):
        self.param_a = param_a

    @property
    def params(self):
        return {"param_a": self.param_a}

    def compute(self, ctx: SizingContext) -> float:
        # Return position size in base-asset units
        return ctx.equity * self.param_a / ctx.price
```

Sizers return size in base-asset units. The engine caps at `max_position_pct` after your call.

Stop-losses are stateful per-position: `on_entry()` initializes, `update()` runs each bar, `check()` returns a `StopResult` indicating whether to exit.

Cost models return total cost in quote currency for a fill. Stack them via `CompositeCostModel`.

---
