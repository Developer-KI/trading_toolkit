# Quantitative Trading Framework

A modular, multi-asset, multi-exchange Python framework for developing, backtesting, and live-trading quantitative strategies on crypto perpetual futures and spot markets. Hyperliquid and Binance Futures are supported out of the box; any exchange can be added via the registry pattern.

Designed with a clean C++/Python seam: execution-critical hot paths are isolated behind `typing.Protocol` interfaces so they can be replaced with `pybind11` C++ extensions without touching strategy code.

---

## Package Layout

```
src/
├── core/               # Stable contracts — models, protocols, events, parser
│   ├── models.py       # ALL data types (Side, Position, Trade, SignalResult, …)
│   ├── protocols.py    # typing.Protocol interfaces for C++ interop
│   ├── events.py       # Typed event structs (BarEvent, TradeEvent, L2Event)
│   └── parser.py       # L2 order-book parser + OHLCV utilities
│
├── risk/               # Risk layer — depends only on core/
│   ├── sizing.py       # Position sizers (Kelly, VolTarget, Fixed, Composite, …)
│   ├── stops.py        # Stop-loss modules (ATR, Trailing, RiskReward, …)
│   └── limits.py       # Kill-switch + daily loss/trade limits
│
├── strategy/           # Pure-Python strategy framework — depends on core/ + risk/
│   ├── base.py         # Signal, Strategy, CrossExchangeStrategy ABC + registries
│   ├── built_in.py     # SingleSignalStrategy, ZPairsSpread, CrossAssetMomentum, …
│   ├── indicators.py   # Stateless indicator functions (ema, rsi, atr, …)
│   ├── universe.py     # Universe + auxiliary data sources
│   └── overlay.py      # Cross-exchange portfolio overlays
│
├── backtester/         # Vectorized backtest engine — depends on core/ + risk/ + strategy/
│   ├── engine.py       # Backtester (single-asset and multi-asset APIs)
│   ├── costs.py        # Pluggable transaction cost models
│   └── stress.py       # Signal / cost / regime / Monte Carlo stress tests
│
├── execution/          # Live trading — depends on core/ + risk/ + strategy/
│   ├── live_engine.py          # LiveEngine (single exchange)
│   ├── multi_exchange_engine.py # MultiExchangeEngine (cross-exchange)
│   ├── live_state.py           # LiveState, _AssetLiveState data holders
│   ├── factory.py              # Registry-based executor + feed factory
│   ├── base_executor_feed.py   # BaseExecutor, BaseFeed, BaseBarBuilder ABCs
│   ├── hyperliquid/            # Hyperliquid executor + WebSocket feed
│   └── binance/                # Binance USD-M executor + WebSocket feed
│
├── data/               # Unified data layer — depends only on core/
│   ├── feeds/
│   │   ├── base.py              # DataFeedProtocol (subscribe/unsubscribe/start/stop)
│   │   ├── hyperliquid.py       # HL perp/spot multi-stream (trades, L2, funding, wallet)
│   │   ├── hyperliquid_bridge.py # Live Arbitrum bridge flows via Alchemy WebSocket
│   │   ├── binance.py           # Binance trades/depth/funding + batch backfill
│   │   └── binance_liquidations.py # Global liquidation stream (!forceOrder@arr)
│   ├── historical/
│   │   ├── hyperliquid_bridge.py # Retroactive bridge deposit/withdrawal (Arbiscan)
│   │   └── hyperliquid_l2.py    # Bulk L2 tick download from S3 archive (LZ4)
│   ├── sentiment/
│   │   ├── x.py        # X.com Playwright scraper
│   │   ├── reddit.py   # Reddit incremental post/comment scraper
│   │   ├── telegram.py # Telegram channel/group message fetcher
│   │   └── chan.py     # 4chan /biz/ scraper
│   └── auxiliary/
│       ├── bid_ask.py  # HL market microstructure scanner (spreads, MM scoring)
│       └── crypto.py   # Macro REST poller (Binance OI, DeFiLlama, Deribit vol)
│
└── utils/              # Shared utilities
    ├── plotting.py             # Equity curve + trade visualisation
    ├── sentiment_preprocess.py # Text cleaning + tokenisation
    ├── sentiment_score.py      # VADER / transformer sentiment scoring
    └── http.py                 # Lightweight GET/POST HTTP client (DataFetcher)
```

### Dependency order

```
core/
  ↑
risk/        strategy/        data/
  ↑               ↑
      backtester/   execution/
                        ↑
                      utils/
```

No module imports upward or sideways outside this DAG.

---

## Core Concepts

### Data Models (`core/models.py`)

All data types live in one file with no internal dependencies:

| Type | Purpose |
|---|---|
| `Side` | `LONG` / `SHORT` enum |
| `OrderType` | `MARKET` / `LIMIT` enum |
| `OrderBookLevel` | Single price/size level |
| `OrderBookSnapshot` | Full L2 snapshot with timestamp |
| `FundingSnapshot` | Funding rate + mark/oracle prices |
| `Trade` | Completed trade record |
| `Position` | Open position state |
| `SignalResult` | Signal output (side, price, metadata) |
| `FillResult` | Order fill record from executor |
| `ExchangeCredentials` | API keys + testnet flag |
| `LiveConfig` | Risk limits for live trading |
| `BacktestConfig` | Fee rates + slippage for backtests |

### Protocol Interfaces (`core/protocols.py`)

Uses `typing.Protocol` (structural subtyping) rather than ABCs for the execution layer:

```python
from core.protocols import ExecutorProtocol, FeedProtocol, BarBuilderProtocol
```

A C++ class wrapped with `pybind11` satisfies these without Python inheritance — no `register()` calls, no base class required. All three protocols are `@runtime_checkable`.

### Risk Layer (`risk/`)

Risk is independent of strategy. It depends only on `core/`:

```python
# Position sizing
from risk.sizing import VolatilityTargetSizer, KellySizer, CompositeSizer

# Stop-loss
from risk.stops import ATRStop, TrailingStop, RiskRewardStop, CompositeStopLoss

# Kill-switch
from risk.limits import check_daily_loss_limit, DailyLimitState
```

Sizers and stops are pluggable: pass any `Sizer` / `StopLoss` instance to `LiveEngine` or `Backtester`.

### Exchange Registry (`execution/factory.py`)

Adding a new exchange requires two dict entries and no if/elif chains:

```python
from execution.factory import register_exchange

register_exchange(
    "bybit",
    executor_factory=make_bybit_executor,  # (ExchangeCredentials) → BaseExecutor
    feed_factory=make_bybit_feed,           # (symbol, testnet, **kw) → BaseFeed
)
```

---

## Writing a Strategy

### Single-asset signal

```python
from strategy.base import Signal, SignalResult, register_signal
from core.models import Side
import pandas as pd

@register_signal("ema_cross")
class EMACross(Signal):
    def __init__(self, fast=12, slow=26, **kw):
        super().__init__(**kw)
        self.fast, self.slow = fast, slow

    def setup(self, data: pd.DataFrame, l2=None):
        self.fast_ema = data["close"].ewm(span=self.fast).mean()
        self.slow_ema = data["close"].ewm(span=self.slow).mean()

    def generate(self, i: int, data: pd.DataFrame, l2=None) -> SignalResult | None:
        if self.fast_ema.iloc[i] > self.slow_ema.iloc[i]:
            return SignalResult(side=Side.LONG, price=data["close"].iloc[i])
        if self.fast_ema.iloc[i] < self.slow_ema.iloc[i]:
            return SignalResult(side=Side.SHORT, price=data["close"].iloc[i])
        return None
```

### Multi-asset strategy

```python
from strategy.base import Strategy, StrategyContext, Allocation, PortfolioTarget, register_strategy
from core.models import Side

@register_strategy("momentum")
class MomentumStrategy(Strategy):
    def setup(self, universe):
        pass  # precompute indicators from universe.get("ETH")

    def generate(self, ctx: StrategyContext) -> PortfolioTarget:
        targets = {}
        for symbol in ctx.symbols:
            data = ctx.data(symbol)
            # ...compute allocation...
            targets[symbol] = Allocation(side=Side.LONG, size=0.5)
        return PortfolioTarget(allocations=targets)
```

---

## Backtesting

```python
from backtester.engine import Backtester
from backtester.costs import CompositeCostModel, aggressive_cost_stack
from core.models import BacktestConfig
from core.parser import l2_to_orderbook
from strategy.universe import Universe

config = BacktestConfig(maker_fee=0.0002, taker_fee=0.0005)
costs = CompositeCostModel(aggressive_cost_stack())
universe = Universe().add("ETH", eth_df).add("BTC", btc_df)

bt = Backtester(strategy=my_strategy, config=config, cost_model=costs)
result = bt.run(universe=universe)
result.summary()
result.plot_equity()
```

### Stress testing

```python
from backtester.stress import SignalStressTest, CostStressTest, MonteCarloStress

# Sweep signal parameters
sweep = SignalStressTest(signal_cls=EMACross, param_grid={"fast": [8,12,20], "slow": [26,50]})
sweep.run(data=eth_df).summary()

# Bootstrap trade sequence
mc = MonteCarloStress(n_samples=1000)
mc.run(result).plot_confidence_bands()
```

---

## Live Trading

### Single exchange

```python
from execution.live_engine import LiveEngine
from execution.factory import create_executor, create_feed
from core.models import ExchangeCredentials, LiveConfig
from risk.sizing import VolatilityTargetSizer
from risk.stops import ATRStop

cred = ExchangeCredentials(exchange="hyperliquid", account_address="0x...", secret_key="0x...", testnet=True)
config = LiveConfig(max_daily_loss_pct=2.0, max_position_size=0.1)

engine = LiveEngine(
    strategy=my_strategy,
    credentials=cred,
    config=config,
    sizer=VolatilityTargetSizer(target_vol=0.15),
    stop_loss=ATRStop(multiplier=2.0),
)
engine.run(symbol="ETH", interval_s=60)
```

### Cross-exchange (funding arb, delta-neutral)

```python
from execution.multi_exchange_engine import MultiExchangeEngine
from strategy.base import CrossExchangeStrategy

engine = MultiExchangeEngine(
    strategy=FundingArbStrategy(),
    credentials=[hl_cred, binance_cred],
    config=live_config,
)
engine.run(symbols=["ETH", "BTC"])
```

---

## C++/Python Migration Path

The project is designed so execution-critical hot paths can be replaced with C++ extensions one module at a time, without touching strategy code:

| Module | Current | Future C++ target |
|---|---|---|
| `core/protocols.py` `BarBuilderProtocol` | Python bar builder | Lock-free ring buffer via `pybind11` |
| `execution/base_executor_feed.py` `BaseBarBuilder` | Pure Python | C++ OHLCV aggregator |
| `execution/live_state.py` | Python dataclass | C++ POD struct |
| `execution/live_engine.py` `_process_bar` | Python per-bar loop | C++ event loop |

**Binding approach:** `pybind11` (recommended). C++ classes that satisfy the `Protocol` shapes in `core/protocols.py` are accepted anywhere in the stack without Python inheritance.

```cpp
// bar_builder.cpp (example pybind11 binding)
#include <pybind11/pybind11.h>
namespace py = pybind11;

class CppBarBuilder { /* ... */ };

PYBIND11_MODULE(cpp_bar_builder, m) {
    py::class_<CppBarBuilder>(m, "CppBarBuilder")
        .def("push_trade", &CppBarBuilder::push_trade)
        .def("latest_bar", &CppBarBuilder::latest_bar);
}
```

The Python `BarBuilderProtocol` check (`isinstance(bb, BarBuilderProtocol)`) will pass automatically.

---

## Adding an Exchange

1. Create `src/execution/<exchange>/` with an executor and feed module that inherit from `BaseExecutor` and `BaseFeed`.
2. Add two factory functions and register them:

```python
# execution/bybit/__init__.py (or any startup file)
from execution.factory import register_exchange
from .bybit_executor import BybitExecutor
from .bybit_feed import BybitFeed

register_exchange(
    "bybit",
    executor_factory=lambda cred: BybitExecutor(cred.api_key, cred.api_secret, cred.testnet),
    feed_factory=lambda symbol, testnet, **_: BybitFeed(symbol, testnet),
)
```

3. Pass `ExchangeCredentials(exchange="bybit", ...)` to `LiveEngine` — factory resolves the rest.

---

## Demo Scripts

| Script | What it shows |
|---|---|
| `trading/strategy_backtest_demo.py` | Multi-asset backtest with cost model and stress test |
| `trading/strategy_live_demo.py` | Single-asset live run on Hyperliquid testnet |
| `trading/p_strategy.py` | Cross-exchange funding-rate arbitrage (delta-neutral) |
