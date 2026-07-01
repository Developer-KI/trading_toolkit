"""
strategy_backtest_demo.py — Demonstrates the single-asset strategy framework.

Shows:
  1. A SingleAssetStrategy subclass (EMA crossover)
  2. Running a backtest
  3. Parameter sweep with ParamSweep
"""

from pathlib import Path

from core.models import BacktestConfig, Side, Allocation
from core.parser import trades_to_ohlcv

from backtester.engine import Backtester
from backtester.costs import CompositeCostModel, aggressive_cost_stack
from backtester.stress import ParamSweep

from strategy.base import SingleAssetStrategy, register_strategy
from strategy.indicators import ema
from strategy.universe import Universe
from risk.sizing import VolatilityTargetSizer, CompositeSizer


# ═══════════════════════════════════════════════════════════════════════════
#  EMA crossover strategy
# ═══════════════════════════════════════════════════════════════════════════


@register_strategy("demo_ema_cross")
class DemoEMACrossStrategy(SingleAssetStrategy):
    """Simple EMA crossover for demo."""

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

    def bar(self, data, idx) -> Allocation:
        if idx < self.slow:
            return Allocation()
        fast_val = data["ema_fast"].iat[idx]
        slow_val = data["ema_slow"].iat[idx]
        if fast_val > slow_val:
            return Allocation(
                side=Side.LONG,
                weight=0.8,
                confidence=0.6,
                reason="EMA cross up",
            )
        elif fast_val < slow_val:
            return Allocation(
                side=Side.SHORT,
                weight=0.8,
                confidence=0.6,
                reason="EMA cross down",
            )
        return Allocation()


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"


def demo():
    timeframe = "1m"
    eth_data = trades_to_ohlcv(
        DATA_DIR / "trades" / "HYPERLIQUID_PERPETUALS" / "ETH",
        timeframe=timeframe,
    )

    config = BacktestConfig(
        initial_capital=1000,
        risk_per_trade=0.02,
        max_position_pct=0.25,
        leverage=1.0,
        taker_fee_bps=5.0,
        slippage_bps=1.0,
    )

    sizer = CompositeSizer(
        sizers=[VolatilityTargetSizer(target_vol=0.15)]
    )

    cost = CompositeCostModel(models=aggressive_cost_stack())

    universe = Universe(symbols=["ETH"])
    universe.add_asset("ETH", eth_data)

    strategy = DemoEMACrossStrategy(symbol="ETH", fast=2, slow=5)

    bt = Backtester(
        strategy=strategy, config=config, cost_model=cost, sizer=sizer
    )
    result = bt.run(universe=universe, timeframe=timeframe)

    print(result.summary())

    run_dir = result.save("demo_ema_cross")
    print(f"Backtest saved to: {run_dir}")

    sweep = ParamSweep(
        strategy_cls=DemoEMACrossStrategy,
        param_grid={"fast": [2, 3], "slow": [5, 7]},
        cost_model=cost,
    )
    sweep_result = sweep.run(universe=universe, timeframe=timeframe)
    sweep_result.plot_heatmap(x="fast", y="slow", save_path=f"{run_dir}/heatmap_sweep.png")


if __name__ == "__main__":
    demo()
