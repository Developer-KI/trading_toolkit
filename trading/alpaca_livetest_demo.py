"""
trading/alpaca_livetest_demo.py — Alpaca paper-trading live demo.

Runs the EMA/RSI strategy on Alpaca paper trading using the LiveEngine.
The feed fires 1-minute bars from Alpaca's IEX stream; the strategy
accumulates them into whichever bar_interval_s is configured.

Usage:
    python trading/alpaca_livetest_demo.py
    python trading/alpaca_livetest_demo.py --symbol AAPL --bar-interval 300

Kill switch: press 'q' + Enter to flatten all positions and stop.

Credentials: set ALP_PAPER_KEY and ALP_PAPER_SECRET in a .env file (or env).
"""

from __future__ import annotations

import pandas as pd
from dotenv import load_dotenv, dotenv_values

from core.models import Side, Allocation, LiveConfig, ExchangeCredentials
from execution.single_exchange_engine import LiveEngine
from strategy.base import SingleAssetStrategy, register_strategy
from strategy.sizing import FixedNotionalSizer
from strategy.stops import NopStopLoss


# ═══════════════════════════════════════════════════════════════════════════
#  Strategy: always-long smoke test
# ═══════════════════════════════════════════════════════════════════════════


@register_strategy("alpaca_live_always_long")
class AlpacaLiveAlwaysLongStrategy(SingleAssetStrategy):
    """Test strategy: goes long immediately on the first bar and stays long."""

    @property
    def params(self) -> dict:
        return {}

    def setup_data(self, data: pd.DataFrame, l2=None):  # noqa: ARG002
        pass

    def bar(self, data: pd.DataFrame, idx: int) -> Allocation:  # noqa: ARG002
        return Allocation(side=Side.LONG, weight=1.0, confidence=1.0, reason="always long")


# ═══════════════════════════════════════════════════════════════════════════
#  Live demo runner
# ═══════════════════════════════════════════════════════════════════════════


def demo(
    symbol: str = "SPY",
    bar_interval_s: int = 60,
    warmup_bars: int = 250,
    max_position_pct: float = 0.10,
    paper: bool = True,
):
    load_dotenv()
    _env = dotenv_values()
    api_key = _env.get("ALP_PAPER_KEY", "")
    api_secret = _env.get("ALP_PAPER_SECRET", "")
    if not api_key or not api_secret:
        raise ValueError(
            "Alpaca credentials missing — set ALP_PAPER_KEY and ALP_PAPER_SECRET in .env"
        )

    print(f"\n{'='*60}")
    print(f"  Alpaca {'PAPER' if paper else 'LIVE'} trading demo")
    print(f"  Symbol        : {symbol}")
    print(f"  Bar interval  : {bar_interval_s}s")
    print(f"  Warmup bars   : {warmup_bars}")
    print(f"  Max position  : {max_position_pct*100:.0f}% of equity")
    print(f"{'='*60}\n")

    cred = ExchangeCredentials(
        exchange="alpaca",
        api_key=api_key,
        api_secret=api_secret,
        testnet=paper,
    )

    config = LiveConfig(
        exchange="alpaca",
        use_testnet=paper,
        exchanges=[cred],
        symbol=symbol,
        bar_interval_s=bar_interval_s,
        warmup_bars=warmup_bars,
        max_bars_in_memory=2000,
        max_position_pct=max_position_pct,
        leverage=1.0,
        order_type="market",
        max_daily_trades=20,
        max_daily_loss_pct=3.0,
        log_level="INFO",
        trade_log_csv="trades.csv",
    )

    strategy = AlpacaLiveAlwaysLongStrategy(symbol=symbol)
    sizer = FixedNotionalSizer()
    stop_loss = NopStopLoss()

    engine = LiveEngine(strategy=strategy, config=config, sizer=sizer, stop_loss=stop_loss)

    print("Starting live engine — press 'q' + Enter to flatten & stop.\n")
    engine.start()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Alpaca paper-trading live demo")
    parser.add_argument("--symbol",       default="SPY",  help="Ticker symbol (default: SPY)")
    parser.add_argument("--bar-interval", default=60,     type=int, help="Bar interval in seconds (default: 60)")
    parser.add_argument("--warmup",       default=250,    type=int, help="Warmup bars (default: 250)")
    parser.add_argument("--max-pos-pct",  default=0.10,   type=float, help="Max position as fraction of equity (default: 0.10)")
    parser.add_argument("--live",         action="store_true",        help="Use live account instead of paper")
    args = parser.parse_args()

    demo(
        symbol=args.symbol,
        bar_interval_s=args.bar_interval,
        warmup_bars=args.warmup,
        max_position_pct=args.max_pos_pct,
        paper=not args.live,
    )
