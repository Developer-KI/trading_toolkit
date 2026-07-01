"""
strategy_live_demo.py — Deploy the EMA crossover strategy to Hyperliquid live.

Usage:
    # 1. Set credentials:
    #    export HL_ACCOUNT_ADDRESS="0x..."
    #    export HL_SECRET_KEY="0x..."
    #
    # 2. Run:
    #    python strategy_live_demo.py
"""

import os
from dotenv import load_dotenv

import pandas as pd

from core.models import LiveConfig, Side, Allocation
from execution.live_engine import LiveEngine

from strategy.base import SingleAssetStrategy, register_strategy
from strategy.indicators import compute_atr_column, ema
from risk.sizing import VolatilityTargetSizer, CompositeSizer, KellySizer


@register_strategy("ema_crossover")
class EMACrossoverStrategy(SingleAssetStrategy):
    """Go long when fast EMA > slow EMA, short when fast < slow."""

    def __init__(self, symbol: str, fast: int = 12, slow: int = 26, **kw):
        super().__init__(symbol=symbol, **kw)
        self.fast = fast
        self.slow = slow

    @property
    def params(self):
        return dict(fast=self.fast, slow=self.slow)

    def setup_data(self, data: pd.DataFrame, l2=None):
        data["ema_fast"] = ema(data["close"], self.fast)
        data["ema_slow"] = ema(data["close"], self.slow)
        compute_atr_column(data)

    def bar(self, data: pd.DataFrame, idx: int) -> Allocation:
        if idx < self.slow:
            return Allocation()

        fast_val = data["ema_fast"].iat[idx]
        slow_val = data["ema_slow"].iat[idx]

        if fast_val > slow_val:
            return Allocation(
                side=Side.LONG,
                weight=1.0,
                confidence=0.7,
                reason=f"EMA {self.fast} > EMA {self.slow}",
            )
        elif fast_val < slow_val:
            return Allocation(
                side=Side.SHORT,
                weight=1.0,
                confidence=0.7,
                reason=f"EMA {self.fast} < EMA {self.slow}",
            )
        return Allocation()


def load_credentials() -> dict:
    load_dotenv()
    return {
        "account_address": os.getenv("HL_ACCOUNT_ADDRESS", ""),
        "secret_key": os.getenv("HL_SECRET_KEY", ""),
    }


def main():
    creds = load_credentials()

    if not creds["account_address"] or not creds["secret_key"]:
        print("ERROR: Set HL_ACCOUNT_ADDRESS and HL_SECRET_KEY env vars")
        return

    config = LiveConfig(
        exchange="hyperliquid",
        account_address=creds["account_address"],
        secret_key=creds["secret_key"],
        use_testnet=True,
        symbol="ETH",
        bar_interval_s=60,
        warmup_bars=200,
        max_daily_trades=50,
        max_daily_loss_pct=5.0,
        risk_per_trade=0.02,
        max_position_pct=0.25,
        leverage=1.0,
    )

    strategy = EMACrossoverStrategy(symbol=config.symbol, fast=16, slow=22)

    sizer = CompositeSizer(
        sizers=[VolatilityTargetSizer(target_vol=0.15),
                KellySizer(kelly_frac=0.5)],
        mode="avg",
    )

    engine = LiveEngine(
        strategy=strategy,
        config=config,
        sizer=sizer,
    )

    print(f"Starting live engine on {'testnet' if config.use_testnet else 'MAINNET'}...")
    print(f"Symbol: {config.symbol} | Strategy: {strategy.__class__.__name__}")
    print("Press Ctrl+C to stop.\n")

    engine.start()


if __name__ == "__main__":
    main()
