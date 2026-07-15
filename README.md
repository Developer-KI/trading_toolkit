# Quantitative Trading Framework

A modular, multi-asset, multi-exchange Python framework for developing, backtesting, stress-testing, and live-trading quantitative strategies on crypto perpetual futures, US equities, and spot. Built with Hyperliquid, Binance Futures, and Alpaca support out of the box, and extensible to any exchange via structural protocols.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Architecture](#architecture)
3. [File Structure](#file-structure)
4. [Dependency Chain](#dependency-chain)
5. [Core Concepts](#core-concepts)
6. [Writing Strategies](#writing-strategies)
   - [SingleAssetStrategy (single-symbol)](#singleassetstrategy-single-symbol)
   - [Strategy API (multi-asset / multi-exchange)](#strategy-api-multi-asset--multi-exchange)
   - [Built-in Strategies](#built-in-strategies)
7. [Backtesting](#backtesting)
   - [Single-Asset Backtest](#single-asset-backtest)
   - [Multi-Asset Backtest](#multi-asset-backtest)
   - [Multi-Exchange Backtest](#multi-exchange-backtest)
   - [Pluggable Components](#pluggable-components)
   - [BacktestResult](#backtestresult)
8. [Stress Testing](#stress-testing)
   - [Parameter Sweep](#1-parameter-sweep)
   - [Cost Stress Test](#2-cost-stress-test)
   - [Regime Stress Test](#3-regime-stress-test)
   - [Monte Carlo Simulation](#4-monte-carlo-simulation)
9. [Hypothesis Testing](#hypothesis-testing)
   - [Train / Test / Validate Splits](#train--test--validate-splits)
   - [Walk-Forward Analysis](#walk-forward-analysis)
   - [Statistical Tests](#statistical-tests)
   - [Overfitting Guards](#overfitting-guards)
10. [Live Trading](#live-trading)
    - [Single-Exchange Live](#single-exchange-live)
    - [Multi-Exchange Live](#multi-exchange-live)
    - [Risk Management](#risk-management)
11. [Extending the Framework](#extending-the-framework)
    - [Adding a New Exchange](#adding-a-new-exchange)
    - [Custom Data Sources](#custom-data-sources)
    - [Custom Sizers, Stops, and Cost Models](#custom-sizers-stops-and-cost-models)

---

## Quick Start

### Single-asset backtest (simplest path)

```python
from core.models import Allocation, BacktestConfig, Side
from core.universe import Universe
from strategy.built_in import SingleAssetStrategy
from strategy.indicators import ema, rsi
from testing.backtester.engine import Backtester


class EMACross(SingleAssetStrategy):
    def __init__(self, symbol, fast=12, slow=26, **kw):
        super().__init__(symbol=symbol, **kw)
        self.fast, self.slow = fast, slow

    @property
    def params(self):
        return {"fast": self.fast, "slow": self.slow}

    def setup_data(self, data, l2=None):
        data["ema_f"] = ema(data["close"], self.fast)
        data["ema_s"] = ema(data["close"], self.slow)

    def bar(self, data, idx):
        if idx < self.slow:
            return Allocation()
        if data["ema_f"].iat[idx] > data["ema_s"].iat[idx]:
            return Allocation(side=Side.LONG, weight=1.0, confidence=0.6)
        return Allocation(side=Side.SHORT, weight=1.0, confidence=0.6)


universe = Universe(symbols=["ETH"])
universe.add_asset("ETH", eth_df)

bt = Backtester(strategy=EMACross(symbol="ETH"))
result = bt.run(universe=universe)
print(result.summary())
result.plot_equity()
```

### Deploy the same strategy live

```python
from execution.engine import Engine
from core.models import LiveConfig, ExchangeCredentials

config = LiveConfig(
    exchanges=[ExchangeCredentials(
        exchange="hyperliquid",
        account_address="0x...",
        secret_key="0x...",
        testnet=True,
    )],
    symbols=["ETH"],
    bar_interval_s=60,
    warmup_bars=200,
)

engine = Engine(strategy=EMACross(symbol="ETH"), config=config)
engine.start()  # blocks, trades on every bar close
```

No code changes to the strategy. The backtester and live engine share the same `Strategy.setup()` / `Strategy.generate()` interface.

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                          BACKTESTER                                │
│  Strategy ──→ Engine loop ──→ Sizer ──→ StopLoss ──→ CostModel     │
│    ↑          (bar-by-bar)                                         │
│    │                                                               │
│  Universe (OHLCV + L2 + FundingSnapshots + Aux DataSources)        │
└────────────────────────────────────────────────────────────────────┘
          ↕ same Strategy interface
┌────────────────────────────────────────────────────────────────────┐
│                        LIVE ENGINE                                 │
│  Strategy ──→ Engine._process_bar() ──→ Sizer ──→ StopLoss         │
│    ↑          (on each bar close)           │                      │
│    │                                        ↓                      │
│  rolling Universe                      BaseExecutor                │
│    ↑                          (HyperliquidExecutor / BinanceExec   │
│    │                           / AlpacaExecutor)                   │
│    │                                                               │
│  BarBuilder ←── BaseFeed (WebSocket)                               │
│    (trades → OHLCV)    (L2 book + trades + candles)                │
│                                                                    │
│  _ManualKillSwitch (stdin listener: press 'q' + Enter to flatten)  │
└────────────────────────────────────────────────────────────────────┘
          ↕ extends to multiple exchanges
┌────────────────────────────────────────────────────────────────────┐
│                    MULTI-EXCHANGE ENGINE                           │
│  Strategy ──→ PortfolioTarget.exchange_allocations                  │
│    ↑                          │                                    │
│    │                          ├──→ HyperliquidExecutor             │
│    │                          └──→ BinanceExecutor                 │
│    │                                                               │
│  Per-exchange Universes + MultiExchangePortfolio                   │
│  PortfolioOverlays (net exposure cap, delta-neutral, …)            │
└────────────────────────────────────────────────────────────────────┘
```

The **C++ seam** lives in `core/protocols.py`. `ExecutorProtocol`, `FeedProtocol`, and `BarBuilderProtocol` are `typing.Protocol` definitions — pybind11-wrapped C++ classes satisfy them structurally without inheriting from any Python base class. The live engine and backtester only depend on these Protocols, so Python and C++ implementations are interchangeable.

---

## File Structure

```
src/
├── core/                     Shared foundation — no upstream imports
│     models.py                 Side, Allocation, Trade, Position, FillResult,
│                               OrderBookSnapshot, FundingSnapshot,
│                               BacktestConfig, LiveConfig, ExchangeCredentials,
│                               AggregatedPosition, ExchangePosition
│     protocols.py              ExecutorProtocol, FeedProtocol, BarBuilderProtocol
│                               (structural typing; C++ pybind11 seam)
│     universe.py               Universe, StaticDataSource, CallableDataSource
│     feeds.py                  BaseFeed, BaseBarBuilder base classes
│     events.py                 Event bus used by the live engine
│     parser.py                 Timeframe ↔ seconds helpers
│
├── data/                     Market data layer
│     feeds/
│       alpaca.py               AlpacaFeed  (US equities, async websocket)
│       binance.py              BinanceFeed (perpetual futures)
│       hyperliquid.py          HyperliquidFeed (perpetual futures)
│     auxiliary/
│       macro/
│         crypto.py             On-chain / macro data sources
│
├── strategy/                 Strategy logic — depends on core only
│     base.py                   Strategy (ABC), StrategyContext, PortfolioTarget,
│                               register_strategy / get_strategy / list_strategies
│     built_in.py               SingleAssetStrategy, CompositeStrategy,
│                               PerAssetStrategy, ZPairsSpreadStrategy,
│                               CrossAssetMomentumStrategy,
│                               MeanReversionBasketStrategy
│     indicators.py             ema, sma, rsi, atr, bollinger, vwap, OFI
│     sizing.py                 8 sizers + CompositeSizer
│     stops.py                  9 stops + CompositeStopLoss + NopStopLoss
│     overlay.py                NetExposureOverlay, DeltaNeutralOverlay
│
├── execution/                Live engine and exchange adapters
│     engine.py                 Engine (single- and multi-exchange unified)
│     executor.py               BaseExecutor ABC
│     factory.py                create_executor / create_feed / create_bar_builder
│     portfolio.py              MultiExchangePortfolio
│     state.py                  LiveState, _AssetLiveState, _ManualKillSwitch
│     alpaca/
│       alpaca_executor.py      US equity paper/live execution via alpaca-py
│     binance/
│       binance_executor.py     Binance Futures executor
│     hyperliquid/
│       hyperliquid_executor.py Hyperliquid executor
│
└── testing/                  Backtesting + validation
      backtester/
        engine.py               Backtester, BacktestResult
        costs.py                7 cost models + CompositeCostModel
        stress.py               ParamSweep, CostStressTest, RegimeStressTest,
                                MonteCarloStress
      hypothesis/
        splits.py               HoldoutSplit, WalkForwardSplits,
                                TrainTestValidateSplit
        walk_forward.py         WalkForwardAnalysis, WalkForwardResult
        tests.py                HypothesisTests, PermutationTest, BootstrapCI
        overfitting.py          DeflatedSharpeRatio, MultipleComparisonCorrection,
                                ProbabilityOfBacktestOverfitting

trading/                      Demo scripts (not installed as a package)
  backtest_demo.py              End-to-end backtest + hypothesis workflow
  alpaca_livetest_demo.py       Alpaca paper-trading live demo
```

---

## Dependency Chain

```
core/              Shared models, protocols, universe (no upstream deps)
     ↑
strategy/          Strategy logic, indicators, sizing, stops, overlays
     ↑
testing/           Backtester engine, cost models, stress tests, hypothesis tests
     ↑
execution/         Live engine, exchange adapters, WebSocket feeds
     ↑
trading/           Demo scripts
```

---

## Core Concepts

**Strategy** — The single base class for all trading logic. Implement `setup(universe)` once for indicator pre-computation and `generate(ctx)` to return a `PortfolioTarget` per bar. Single-asset, multi-asset, and multi-exchange strategies all subclass `Strategy`.

**SingleAssetStrategy** — A convenience subclass for single-symbol strategies. Implement `setup_data(data, l2)` and `bar(data, idx) → Allocation` instead of the lower-level `setup`/`generate`. Wiring to `PortfolioTarget` is automatic.

**Universe** — The data container. Holds per-asset OHLCV DataFrames, optional L2 book snapshots, optional per-bar `FundingSnapshot` lists, and pluggable `DataSource` objects (on-chain metrics, sentiment, etc.). Both the backtester and live engine pass a Universe to the strategy, so the same code runs in both contexts.

**Allocation** — The desired state for one (exchange, symbol) leg: direction (LONG / SHORT / FLAT), portfolio weight (0–1), confidence score, order type, and optional SL/TP levels.

**PortfolioTarget** — Dict-like output of `Strategy.generate()`. Keyed by `symbol` for single-exchange strategies, or by `(exchange, symbol)` for cross-exchange strategies. Assets absent from the target are treated as FLAT.

**StrategyContext** — Everything a strategy sees at each bar: the universe (or `universes` dict for multi-exchange), equity, positions, trade history, and convenience accessors like `ctx.price("ETH")`, `ctx.ohlcv("ETH")`, `ctx.funding("ETH")`, `ctx.is_positioned("ETH")`, `ctx.net_exposure()`.

**Pluggable Components** — Sizers, stop-losses, and cost models are all ABCs with concrete implementations that can be swapped, composed, and stress-tested independently. Each can be a single shared instance or a `dict[symbol, instance]` for per-asset overrides.

---

## Writing Strategies

### SingleAssetStrategy (single-symbol)

Implement `setup_data` for indicator pre-computation and `bar` for per-bar logic:

```python
from core.models import Allocation, Side
from strategy.built_in import SingleAssetStrategy
from strategy.indicators import ema, rsi


class EMACrossoverStrategy(SingleAssetStrategy):
    def __init__(self, symbol: str, fast: int = 12, slow: int = 26, **kw):
        super().__init__(symbol=symbol, **kw)
        self.fast = fast
        self.slow = slow

    @property
    def params(self):
        return {"fast": self.fast, "slow": self.slow}

    def setup_data(self, data, l2=None):
        data["ema_fast"] = ema(data["close"], self.fast)
        data["ema_slow"] = ema(data["close"], self.slow)
        data["rsi"] = rsi(data["close"])

    def bar(self, data, idx):
        if idx < self.slow:
            return Allocation()

        fast_val = data["ema_fast"].iat[idx]
        slow_val = data["ema_slow"].iat[idx]
        rsi_val  = data["rsi"].iat[idx]

        if fast_val > slow_val and rsi_val < 70:
            return Allocation(
                side=Side.LONG,
                weight=0.8,
                confidence=min((fast_val - slow_val) / slow_val * 100, 1.0),
                reason=f"EMA bullish cross, RSI={rsi_val:.0f}",
            )
        if fast_val < slow_val and rsi_val > 30:
            return Allocation(
                side=Side.SHORT,
                weight=0.8,
                confidence=min((slow_val - fast_val) / slow_val * 100, 1.0),
                reason=f"EMA bearish cross, RSI={rsi_val:.0f}",
            )
        return Allocation(reason="No signal")
```

Combine multiple single-asset strategies with weighted voting using `CompositeStrategy`:

```python
from strategy.built_in import CompositeStrategy

composite = CompositeStrategy(
    symbol="ETH",
    strategies=[ema_strategy, momentum_strategy],
    weights=[0.6, 0.4],
    threshold=0.4,
)
```

### Strategy API (multi-asset / multi-exchange)

For strategies that see multiple assets or exchanges simultaneously, subclass `Strategy` directly:

```python
from core.models import Allocation, Side
from strategy.base import Strategy, StrategyContext, PortfolioTarget, register_strategy
from strategy.indicators import rsi


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

    def setup(self, universe):
        self._symbols = universe.symbols

    def generate(self, ctx: StrategyContext) -> PortfolioTarget:
        target = PortfolioTarget(timestamp=ctx.timestamp)
        if ctx.bar_idx < self.lookback:
            return target

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

For multi-exchange strategies, key allocations by `(exchange, symbol)`:

```python
def generate(self, ctx: StrategyContext) -> PortfolioTarget:
    target = PortfolioTarget(timestamp=ctx.timestamp)
    target[("hyperliquid", "ETH")] = Allocation(side=Side.LONG, weight=0.3)
    target[("binance", "ETH")]     = Allocation(side=Side.SHORT, weight=0.3)
    return target
```

### Built-in Strategies

Three multi-asset strategies are registered out of the box:

```python
from strategy.base import get_strategy

# Z-score pairs trading
PairsStrategy = get_strategy("pairs_z_spread")
strat = PairsStrategy(asset_a="ETH", asset_b="BTC", lookback=60, entry_z=2.0)

# Rank by return, long top N / short bottom N
MomStrategy = get_strategy("cross_asset_momentum")

# Z-score + RSI mean reversion basket
MRStrategy = get_strategy("mean_reversion_basket")
```

---

## Backtesting

### Single-Asset Backtest

```python
from core.models import BacktestConfig
from core.universe import Universe
from testing.backtester.engine import Backtester

universe = Universe(symbols=["ETH"])
universe.add_asset("ETH", eth_ohlcv_df)

bt = Backtester(
    strategy=EMACrossoverStrategy(symbol="ETH", fast=12, slow=26),
    config=BacktestConfig(
        initial_capital=100_000,
        leverage=2.0,
    ),
)

result = bt.run(universe=universe, timeframe="1h")
print(result.summary())
result.plot_equity()
result.to_csv("trades.csv")
```

### Multi-Asset Backtest

```python
from core.models import FundingSnapshot
from core.universe import Universe, StaticDataSource

universe = Universe(symbols=["ETH", "BTC", "SOL"])
universe.add_asset("ETH", eth_df)
universe.add_asset("BTC", btc_df)
universe.add_asset("SOL", sol_df)

# Optional: attach per-bar funding rate snapshots
eth_funding = [
    FundingSnapshot(timestamp=row.name, rate=row["rate"],
                    rate_annualized=row["rate"] * 3 * 365 * 1e4)
    for _, row in funding_df.iterrows()
]
universe.add_asset("ETH", eth_df, funding=eth_funding)

# Optional: add auxiliary data sources
universe.add_data_source(StaticDataSource("sentiment", sentiment_df))

strategy = CrossAssetMomentumStrategy(long_n=2, short_n=1, lookback=20)

result = Backtester(strategy=strategy).run(universe=universe, timeframe="1h")
print(result.summary())
print(result.trades_by_symbol("ETH"))
```

### Multi-Exchange Backtest

The backtester supports distinct per-exchange cost models and capital splits:

```python
from testing.backtester.costs import CompositeCostModel, ExchangeFeeCost, FixedSlippageCost
from testing.backtester.engine import Backtester
from core.models import BacktestConfig

u_hl  = Universe(symbols=["ETH"]); u_hl.add_asset("ETH", eth_df)
u_bn  = Universe(symbols=["ETH"]); u_bn.add_asset("ETH", eth_df)

bt = Backtester(
    strategy=my_cross_exchange_strategy,
    config=BacktestConfig(initial_capital=100_000, leverage=1.0),
    exchange_costs={
        "hyperliquid": CompositeCostModel([ExchangeFeeCost(taker_bps=2.5)]),
        "binance":     CompositeCostModel([ExchangeFeeCost(taker_bps=4.0),
                                           FixedSlippageCost(slippage_bps=1)]),
    },
    capital_by_exchange={"hyperliquid": 50_000, "binance": 50_000},
)

result = bt.run(universes={"hyperliquid": u_hl, "binance": u_bn}, timeframe="1h")
print(result.equity_curves_by_exchange)
```

### Pluggable Components

```python
from strategy.sizing import VolatilityTargetSizer, KellySizer, CompositeSizer
from strategy.stops import TrailingATRStop, TimeStop, BreakevenStop, CompositeStopLoss
from testing.backtester.costs import CompositeCostModel, ExchangeFeeCost, SpreadCost, MarketImpactCost

# Composite sizer: take the most conservative of vol-target and Kelly
sizer = CompositeSizer(
    sizers=[VolatilityTargetSizer(target_vol=0.15), KellySizer(kelly_frac=0.5)],
    mode="min",
)

# Composite stop: first to trigger wins
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

# Per-asset sizer overrides
bt = Backtester(
    strategy=my_strategy,
    sizer={"ETH": VolatilityTargetSizer(target_vol=0.20),
           "BTC": VolatilityTargetSizer(target_vol=0.12)},
    stop_loss=stop,
    cost_model=costs,
)
```

**Available Sizers:** `FixedFractionalSizer`, `FixedNotionalSizer`, `VolatilityTargetSizer`, `KellySizer`, `AntiMartingaleSizer`, `DrawdownScalingSizer`, `L2LiquiditySizer`, `CompositeSizer`.

**Available Stops:** `FixedPercentStop`, `ATRStop`, `TrailingStop`, `TrailingATRStop`, `BreakevenStop`, `TimeStop`, `RiskRewardStop`, `SignalStop`, `NopStopLoss`, `CompositeStopLoss`.

**Available Cost Models:** `ExchangeFeeCost`, `FixedSlippageCost`, `ProportionalSlippageCost`, `L2BookSlippageCost`, `SpreadCost`, `FundingRateCost`, `MarketImpactCost`, `CompositeCostModel`.

### BacktestResult

- `result.summary()` — dict with total return, CAGR, Sharpe, Sortino, Calmar, max drawdown, win rate, profit factor, fees, and more.
- `result.trades_df()` — full trade log as a DataFrame.
- `result.trades_by_symbol("ETH")` — per-symbol trade filter (multi-asset runs).
- `result.equity_curve` — pandas Series of equity over time.
- `result.equity_curves_by_exchange` — per-exchange equity curves (multi-exchange runs).
- `result.plot_equity()` — equity curve + drawdown chart.
- `result.to_csv()` — exports trades to CSV.
- `result.save(name)` — writes `log.json`, `trades.csv`, and `equity_curve.png` to `logs/backtest/<name>/`.
- `result.positions_log` / `result.allocation_log` — per-bar, per-asset logs (multi-asset only).

---

## Stress Testing

Four stress test classes are in `testing/backtester/stress.py`. All return a `StressResult` with a summary DataFrame and optional plots.

### 1. Parameter Sweep

Grid search over strategy constructor parameters:

```python
from testing.backtester.stress import ParamSweep

sweep = ParamSweep(
    strategy_cls=EMACrossoverStrategy,
    param_grid={"fast": [5, 8, 12, 20], "slow": [21, 26, 50]},
    fixed_params={"symbol": "ETH"},
)

result = sweep.run(universe=universe, timeframe="1h")
print(result.best("sharpe_ratio"))
result.plot_heatmap("fast", "slow", z="sharpe_ratio")
```

### 2. Cost Stress Test

Sweep transaction cost assumptions to find where alpha breaks down:

```python
from testing.backtester.stress import CostStressTest

cst = CostStressTest(
    cost_grid={
        "ExchangeFeeCost": {"taker_bps": [3, 5, 8, 12]},
        "SpreadCost":      {"default_spread_bps": [1, 2, 4, 8]},
    },
)

result = cst.run(strategy=my_strategy, universe=universe)
```

### 3. Regime Stress Test

Split data by market regime and backtest each subset independently. Built-in classifiers include volatility and trend. Custom classifiers are supported:

```python
from testing.backtester.stress import RegimeStressTest

rst = RegimeStressTest(
    regime_fn=RegimeStressTest.trend_regime,  # or None for volatility, or custom
)

result = rst.run(strategy=my_strategy, universe=universe)
print(result.summary)
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

### 4. Monte Carlo Simulation

Bootstrap or shuffle trade PnLs to build confidence intervals around your backtest results:

```python
from testing.backtester.stress import MonteCarloStress

mc = MonteCarloStress(n_simulations=1000, method="bootstrap")
result = mc.run(backtest_result)

m = result.meta
print(f"Median return:  {m['median_return']:.2f}%")
print(f"5th percentile: {m['5th_pctl_return']:.2f}%")
print(f"95th percentile:{m['95th_pctl_return']:.2f}%")
```

---

## Hypothesis Testing

The `testing/hypothesis/` module provides statistical tools for validating that backtest performance represents genuine edge rather than overfitting.

### Train / Test / Validate Splits

```python
from testing.hypothesis import TrainTestValidateSplit

ttv = TrainTestValidateSplit.by_fractions(
    universe, train_frac=0.60, test_frac=0.20, embargo_bars=10
)
# ttv.train, ttv.test, ttv.validate — each a Universe over its window
```

### Walk-Forward Analysis

```python
from testing.hypothesis import WalkForwardAnalysis

wfa = WalkForwardAnalysis(
    strategy_cls=EMACrossoverStrategy,
    strategy_params={"fast": 12, "slow": 26},
    fixed_params={"symbol": "ETH"},
    config=config, cost_model=cost_model, sizer=sizer, stop_loss=stop_loss,
)

wf = wfa.run(universe=ttv.train, timeframe="1d", n_splits=5, split_method="expanding")
print(f"Consistency score: {wf.consistency_score:.0%}")   # fraction of OOS folds profitable
print(f"IS/OOS efficiency: {wf.efficiency_ratio:.2f}")    # OOS Sharpe / IS Sharpe
```

### Statistical Tests

```python
from testing.hypothesis import HypothesisTests, PermutationTest, BootstrapCI, report

# Battery of t-tests on the backtest result
tests = HypothesisTests.run_all(result)
print(report(tests))

# Compare two strategies on a metric
t = HypothesisTests.compare(result_a, result_b, metric="sharpe_ratio")
print(f"p={t.p_value:.4f}  reject_null={t.reject_null}")

# Permutation test on Sharpe
pt = PermutationTest(metric="sharpe_ratio", n_permutations=2_000)
pt_result = pt.run(result)

# Bootstrap 95% confidence intervals
ci = BootstrapCI(n_bootstrap=2_000, ci=0.95)
cis = ci.run(result)  # dict[metric, {"observed", "lower", "upper"}]
```

### Overfitting Guards

```python
from testing.hypothesis import (
    DeflatedSharpeRatio,
    MultipleComparisonCorrection,
    ProbabilityOfBacktestOverfitting,
)

# Deflated Sharpe — accounts for the number of trials during parameter search
dsr = DeflatedSharpeRatio()
d = dsr.compute(result, n_trials=21)
print(f"Deflated SR={d.deflated_sharpe:.4f}  p={d.p_value:.4f}  reject_null={d.reject_null}")

# Bonferroni / BH multiple comparison corrections
mc = MultipleComparisonCorrection()

# Probability of Backtest Overfitting (combinatorially symmetric cross-validation)
pbo = ProbabilityOfBacktestOverfitting()
```

---

## Live Trading

### Single-Exchange Live

The `Engine` in `execution/engine.py` handles all live scenarios. The single-exchange shorthand requires exactly one entry in `config.exchanges`:

```python
from execution.engine import Engine
from core.models import LiveConfig, ExchangeCredentials
from strategy.sizing import VolatilityTargetSizer
from strategy.stops import TrailingATRStop, TimeStop, CompositeStopLoss

config = LiveConfig(
    exchanges=[ExchangeCredentials(
        exchange="hyperliquid",
        account_address="0x...",
        secret_key="0x...",
        testnet=True,
    )],
    symbols=["ETH", "BTC"],
    bar_interval_s=60,
    warmup_bars=200,
    leverage=3.0,
    max_daily_loss_pct=5.0,
    max_daily_trades=50,
    order_type="market",
)

engine = Engine(
    strategy=my_strategy,
    config=config,
    sizer=VolatilityTargetSizer(target_vol=0.15),
    stop_loss=CompositeStopLoss([
        TrailingATRStop(atr_mult=2.5),
        TimeStop(max_bars=48),
    ]),
)

engine.start()  # blocks until interrupted or kill switch triggers
```

**Lifecycle:** creates executors and bar builders via the factory, sets leverage, seeds bar builders with historical candles during warm-up, starts WebSocket feeds, then enters the main heartbeat loop. On each bar close it syncs positions, runs stop-loss checks, calls `strategy.generate()`, diffs the target against current positions, and executes trades in a thread pool.

### Multi-Exchange Live

```python
from execution.engine import Engine
from core.models import LiveConfig, ExchangeCredentials
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

# Option A: cross-exchange strategy (funding arb, stat arb)
engine = Engine(cross_strategy=my_arb_strategy, config=config)

# Option B: independent strategies per exchange + risk overlay
engine = Engine(
    per_exchange_strategies={
        "hyperliquid": momentum_strategy,
        "binance": mean_reversion_strategy,
    },
    overlay=NetExposureOverlay(max_net_weight=0.5),
    config=config,
)

engine.start()
```

**Portfolio Overlays** sit between strategy output and execution. `NetExposureOverlay` caps net directional weight; `DeltaNeutralOverlay` auto-generates hedge legs.

### Risk Management

- **Daily loss kill switch** — If daily PnL loss exceeds `max_daily_loss_pct`, all positions are flattened and the engine shuts down.
- **Manual kill switch** — Press `q` + Enter at any time to immediately flatten all positions and shut down. Silently disables itself on non-interactive terminals (Docker, systemd).
- **Daily trade limit** — `max_daily_trades` caps new trades per day.
- **Position sync** — Each bar, local position state is compared against exchange state and mismatches are logged.
- **Leverage and margin** — Set via `config.leverage` and `config.margin_type`; applied to each symbol at startup.

---

## Extending the Framework

### Adding a New Exchange

Implement `BaseExecutor` and `BaseFeed`, then register in `factory.py`:

```python
# execution/myexchange/myexchange_executor.py
from execution.executor import BaseExecutor
from core.models import Side, Position, FillResult, FundingSnapshot


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

The same contract is expressed as `ExecutorProtocol` in `core/protocols.py` — a pybind11-wrapped C++ class implementing these methods satisfies the protocol without needing any Python base class.

Register in `execution/factory.py`:

```python
elif name == "myexchange":
    from .myexchange.myexchange_executor import MyExchangeExecutor
    return MyExchangeExecutor(...)
```

### Custom Data Sources

```python
from core.universe import Universe, StaticDataSource, CallableDataSource

# Static (backtesting)
universe.add_data_source(StaticDataSource("sentiment", sentiment_df))

# Live (queries an API on each call)
def fetch_onchain(symbols, start=None, end=None):
    return pd.DataFrame(...)

universe.add_data_source(CallableDataSource("onchain", fetch_onchain))

# Access in strategy:
# ctx.aux("sentiment")  → DataFrame
```

### Custom Sizers, Stops, and Cost Models

All three follow the same pattern — subclass the ABC, implement the core method, expose `params`:

```python
from strategy.sizing import Sizer, SizingContext


class MyCustomSizer(Sizer):
    def __init__(self, param_a: float = 0.5):
        self.param_a = param_a

    @property
    def params(self):
        return {"param_a": self.param_a}

    def compute(self, ctx: SizingContext) -> float:
        return ctx.equity * self.param_a / ctx.price
```

Sizers return size in base-asset units. The engine caps at `max_position_pct` after your call.

Stop-losses are stateful per position: `on_entry()` initializes, `update()` runs each bar, `check()` returns a `StopResult` indicating whether to exit.

Cost models return total cost in quote currency for a fill. Stack them via `CompositeCostModel`.
