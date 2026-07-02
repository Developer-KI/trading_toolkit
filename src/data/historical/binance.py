"""
historical/binance.py — Batch download Binance OHLCV klines to Parquet.

CLI:
    python -m src.data.historical.binance --coin ETHUSDT --market futures \\
        --interval 1m --start 2024-01-01 --end 2024-12-31

Futures endpoint: https://fapi.binance.com/fapi/v1/klines
Spot endpoint:    https://api.binance.com/api/v3/klines

No API key required (public endpoints). Paginates automatically.
Saves timestamped Parquet to data/cleaned/historical/binance/{COIN}/.
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "cleaned" / "historical" / "binance"

FUTURES_URL = "https://fapi.binance.com/fapi/v1/klines"
SPOT_URL = "https://api.binance.com/api/v3/klines"

# Binance klines return 12 positional columns
_ALL_COLS = [
    "timestamp", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades",
    "taker_buy_base", "taker_buy_quote", "_ignore",
]
_KEEP_COLS = [c for c in _ALL_COLS if c != "_ignore"]


def _date_to_ms(date_str: str) -> int:
    return int(
        datetime.strptime(date_str, "%Y-%m-%d")
        .replace(tzinfo=timezone.utc)
        .timestamp() * 1000
    )


def fetch_klines(
    coin: str, market: str, interval: str, start_ms: int, end_ms: int
) -> list[list]:
    """Download all klines in [start_ms, end_ms) with automatic pagination."""
    url = FUTURES_URL if market == "futures" else SPOT_URL
    results: list[list] = []
    current_start = start_ms

    while current_start < end_ms:
        params = {
            "symbol": coin.upper(),
            "interval": interval,
            "startTime": current_start,
            "endTime": end_ms - 1,
            "limit": 1500,
        }
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        batch = resp.json()

        if not batch:
            break

        results.extend(batch)
        last_open_time = batch[-1][0]

        if last_open_time <= current_start or len(batch) < 1500:
            break

        current_start = last_open_time + 1
        time.sleep(0.1)

    return results


def save_parquet(coin: str, rows: list[list]) -> Path:
    df = pd.DataFrame(rows, columns=_ALL_COLS)[_KEEP_COLS]

    df["timestamp"]  = pd.to_datetime(df["timestamp"],  unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)

    for col in ["open", "high", "low", "close", "volume",
                "quote_volume", "taker_buy_base", "taker_buy_quote"]:
        df[col] = df[col].astype(float)
    df["trades"] = df["trades"].astype(int)

    out_dir = DATA_DIR / coin.upper()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{int(time.time() * 1000)}.parquet"
    pq.write_table(pa.Table.from_pandas(df), out_path)
    return out_path


def main():
    parser = argparse.ArgumentParser(
        description="Download Binance OHLCV klines to Parquet."
    )
    parser.add_argument("--coin", required=True,
                        help="Symbol e.g. ETHUSDT BTCUSDT")
    parser.add_argument("--market", choices=["futures", "spot"], default="futures",
                        help="Market type (default: futures)")
    parser.add_argument("--interval", default="1m",
                        help="Kline interval e.g. 1m 5m 15m 1h 4h 1d (default: 1m)")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD (exclusive)")
    args = parser.parse_args()

    start_ms = _date_to_ms(args.start)
    end_ms = _date_to_ms(args.end)

    print(
        f"Downloading {args.coin} {args.interval} klines "
        f"({args.market}) {args.start} → {args.end} …"
    )
    rows = fetch_klines(args.coin, args.market, args.interval, start_ms, end_ms)
    print(f"Fetched {len(rows):,} rows")

    if rows:
        out = save_parquet(args.coin, rows)
        print(f"Saved → {out}")
    else:
        print("No data returned.")


if __name__ == "__main__":
    main()
