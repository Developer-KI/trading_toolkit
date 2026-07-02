"""
historical/alpaca.py — Batch download Alpaca historical OHLCV bars to Parquet.

CLI:
    python -m src.data.historical.alpaca --symbols SPY AAPL \\
        --start 2024-01-01 --end 2024-12-31 --timeframe 1D

Reads ALP_PAPER_KEY / ALP_PAPER_SECRET from .env.
Saves one timestamped Parquet per symbol to data/cleaned/historical/alpaca/{SYMBOL}/.

Supported timeframes: 1Min 5Min 15Min 30Min 1H 4H 1D
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from dotenv import load_dotenv, dotenv_values

load_dotenv()
_env = dotenv_values()

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "cleaned" / "historical" / "alpaca"

_TF_LABELS = ["1Min", "5Min", "15Min", "30Min", "1H", "4H", "1D"]


def _build_timeframe(tf: str):
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    _map = {
        "1Min":  TimeFrame(1,  TimeFrameUnit.Minute),
        "5Min":  TimeFrame(5,  TimeFrameUnit.Minute),
        "15Min": TimeFrame(15, TimeFrameUnit.Minute),
        "30Min": TimeFrame(30, TimeFrameUnit.Minute),
        "1H":    TimeFrame(1,  TimeFrameUnit.Hour),
        "4H":    TimeFrame(4,  TimeFrameUnit.Hour),
        "1D":    TimeFrame(1,  TimeFrameUnit.Day),
    }
    if tf not in _map:
        raise ValueError(f"Unknown timeframe {tf!r}. Valid: {_TF_LABELS}")
    return _map[tf]


def _get_credentials() -> tuple[str, str]:
    key = _env.get("ALP_PAPER_KEY", "")
    secret = _env.get("ALP_PAPER_SECRET", "")
    if not key or not secret:
        raise ValueError(
            "Missing credentials — set ALP_PAPER_KEY and ALP_PAPER_SECRET in .env"
        )
    return key, secret


def fetch_bars(symbol: str, timeframe_str: str, start: str, end: str) -> pd.DataFrame:
    """Download OHLCV bars for one symbol and return as a DataFrame."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest

    api_key, api_secret = _get_credentials()
    client = StockHistoricalDataClient(api_key=api_key, secret_key=api_secret)
    tf = _build_timeframe(timeframe_str)

    start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=tf,
        start=start_dt,
        end=end_dt,
    )
    bars = client.get_stock_bars(req)
    df = bars.df

    # Multi-index (symbol, timestamp) → flat
    if isinstance(df.index, pd.MultiIndex):
        df = df.reset_index(level="symbol", drop=True)
    df = df.reset_index()

    # Normalise column names
    df = df.rename(columns={"timestamp": "timestamp"})
    wanted = ["timestamp", "open", "high", "low", "close", "volume", "trade_count", "vwap"]
    df = df[[c for c in wanted if c in df.columns]]

    return df


def save_parquet(symbol: str, df: pd.DataFrame) -> Path:
    out_dir = DATA_DIR / symbol.upper()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{int(time.time() * 1000)}.parquet"
    pq.write_table(pa.Table.from_pandas(df), out_path)
    return out_path


def main():
    parser = argparse.ArgumentParser(
        description="Download Alpaca historical OHLCV bars to Parquet."
    )
    parser.add_argument("--symbols", nargs="+", required=True,
                        help="One or more symbols e.g. SPY AAPL MSFT")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD (exclusive)")
    parser.add_argument("--timeframe", default="1D",
                        help=f"Bar timeframe (default: 1D). Options: {', '.join(_TF_LABELS)}")
    args = parser.parse_args()

    for sym in args.symbols:
        print(f"Downloading {sym} {args.timeframe} bars {args.start} → {args.end} …")
        try:
            df = fetch_bars(sym, args.timeframe, args.start, args.end)
            print(f"  {len(df):,} bars")
            if not df.empty:
                out = save_parquet(sym, df)
                print(f"  Saved → {out}")
            else:
                print("  No data returned.")
        except Exception as exc:
            print(f"  ERROR {sym}: {exc}")


if __name__ == "__main__":
    main()
