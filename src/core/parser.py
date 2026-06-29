"""
core/parser.py — Parse multi-level L2 order book data.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

from core.models import OrderBookSnapshot, OrderBookLevel


@staticmethod
def l2_to_orderbook(
    input_folder: str | Path, ohlcv_data: pd.DataFrame | None, aligned=True
) -> list[OrderBookSnapshot]:
    data = pd.read_parquet(input_folder, engine="pyarrow")

    data["datetime"] = pd.to_datetime(data["timestamp"], unit="ms")
    data.set_index("datetime", inplace=True)
    data.drop("timestamp", axis=1, inplace=True)

    orderbook = parse_l2(data)
    if aligned:
        orderbook = align_l2_to_ohlcv(orderbook, ohlcv_data)

    return orderbook


@staticmethod
def trades_to_ohlc(input_folder: str | Path) -> pd.DataFrame:
    output = pd.DataFrame()
    data = pd.read_parquet(input_folder, engine="pyarrow")

    data["datetime"] = pd.to_datetime(data["timestamp"], unit="ms")
    data.set_index("datetime", inplace=True)

    output = data["price"].resample("1min").ohlc().dropna()
    output["volume"] = data["size"].resample("1min").sum()

    return output


# ── Column detection ─────────────────────────────────────────────────────────


# rewrite this into list len
def _detect_levels(df: pd.DataFrame) -> int:
    """Auto-detect number of L2 levels from column names."""
    last_row_bid_px = len(df["bids_px"].iloc[-1])
    last_row_bid_sz = len(df["bids_sz"].iloc[-1])
    last_row_ask_px = len(df["asks_px"].iloc[-1])
    last_row_ask_sz = len(df["asks_sz"].iloc[-1])
    level = min(last_row_bid_px, last_row_bid_sz, last_row_ask_px, last_row_ask_sz)
    if not level or level < 1:
        raise KeyError(f"Cannot detect L2 levelsGot: {list(df.columns)}")
    return level


def _ensure_datetime_index(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure the DataFrame has a DatetimeIndex."""
    if isinstance(df.index, pd.DatetimeIndex):
        return df

    # Try known timestamp columns
    ts_aliases = ["timestamp", "ts", "epoch", "time", "exchange_ts"]
    cols_lower = {c.lower().strip(): c for c in df.columns}

    for alias in ts_aliases:
        if alias in cols_lower:
            col = cols_lower[alias]
            vals = df[col]
            if vals.dtype in ("int64", "float64"):
                sample = vals.iloc[0]
                if sample > 1e15:
                    unit = "ns"
                elif sample > 1e12:
                    unit = "ms"
                else:
                    unit = "s"
                df = df.copy()
                df.index = pd.to_datetime(vals, unit=unit)
            else:
                df = df.copy()
                df.index = pd.to_datetime(vals)
            df.index.name = "datetime"
            return df

    # Try parsing existing index
    df = df.copy()
    df.index = pd.to_datetime(df.index)
    return df


# ── Main parser ──────────────────────────────────────────────────────────────
def parse_l2(
    df: pd.DataFrame,
    n_levels: int | None = None,
) -> list[OrderBookSnapshot]:
    """
    Parse a multi-level L2 DataFrame into OrderBookSnapshots.

    Parameters
    ----------
    df       : DataFrame with datetime index and bid_px_N/bid_sz_N/ask_px_N/ask_sz_N columns.
    n_levels : override auto-detection (e.g. 10 if you know you have 10 levels).

    Returns
    -------
    list[OrderBookSnapshot] — one per row, same length and order as df
    """

    df = _ensure_datetime_index(df)
    levels = n_levels or _detect_levels(df)

    # Convert Pandas Series to Python iterables upfront (MUCH faster than .iloc in a loop)
    timestamps = df.index
    bids_px_col = df["bids_px"]
    bids_sz_col = df["bids_sz"]
    asks_px_col = df["asks_px"]
    asks_sz_col = df["asks_sz"]

    snapshots: list[OrderBookSnapshot] = []

    # Zip iterates through the rows without using index/iloc
    for ts, b_pxs, b_szs, a_pxs, a_szs in zip(
        timestamps, bids_px_col, bids_sz_col, asks_px_col, asks_sz_col
    ):
        bids = []
        # list[:levels] safely limits to 'levels' WITHOUT out-of-bounds errors.
        # zip() safely stops at the length of the shortest list provided.
        # Check if they are iterable (in case missing data resulted in a float/NaN instead of a list)
        if isinstance(b_pxs, (list, np.ndarray, tuple)):
            for px, sz in zip(b_pxs[:levels], b_szs[:levels]):
                if (
                    px is not None
                    and sz is not None
                    and not np.isnan(px)
                    and not np.isnan(sz)
                    and sz > 0
                ):
                    bids.append(OrderBookLevel(price=float(px), size=float(sz)))

        asks = []
        if isinstance(a_pxs, (list, np.ndarray, tuple)):
            for px, sz in zip(a_pxs[:levels], a_szs[:levels]):
                if (
                    px is not None
                    and sz is not None
                    and not np.isnan(px)
                    and not np.isnan(sz)
                    and sz > 0
                ):
                    asks.append(OrderBookLevel(price=float(px), size=float(sz)))

        snapshots.append(
            OrderBookSnapshot(
                timestamp=ts,
                bids=bids,
                asks=asks,
            )
        )

    return snapshots


# ── Align snapshots to OHLCV bars ───────────────────────────────────────────


def align_l2_to_ohlcv(
    snapshots: list[OrderBookSnapshot],
    ohlcv: pd.DataFrame,
    method: str = "last",
) -> list[OrderBookSnapshot]:
    """
    Pick one L2 snapshot per OHLCV bar.

    Methods:
      "last"    — last snapshot at or before the bar close (default, most realistic)
      "nearest" — closest snapshot by absolute time
      "vwap"    — merge all snapshots within the bar into combined depth

    Returns exactly len(ohlcv) snapshots aligned 1:1 with the bars.
    """
    if not snapshots:
        raise ValueError("No snapshots to align")

    snap_times = pd.DatetimeIndex([s.timestamp for s in snapshots])
    bar_times = ohlcv.index
    aligned: list[OrderBookSnapshot] = []

    if method == "last":
        snap_idx = pd.Series(range(len(snapshots)), index=snap_times).sort_index()
        for bt in bar_times:
            mask = snap_idx.index <= bt
            if mask.any():
                aligned.append(snapshots[snap_idx[mask].iloc[-1]])
            else:
                aligned.append(snapshots[0])

    elif method == "nearest":
        snap_ts_ns = snap_times.astype("int64").values
        for bt in bar_times:
            bt_ns = bt.value
            idx = int(np.argmin(np.abs(snap_ts_ns - bt_ns)))
            aligned.append(snapshots[idx])

    elif method == "vwap":
        freq = pd.infer_freq(bar_times) or "1h"
        snap_idx = pd.Series(range(len(snapshots)), index=snap_times).sort_index()

        for i in range(len(bar_times)):
            start = bar_times[i - 1] if i > 0 else bar_times[i] - pd.Timedelta(freq)
            end = bar_times[i]
            indices = snap_idx[
                (snap_idx.index > start) & (snap_idx.index <= end)
            ].values

            if len(indices) == 0:
                before = snap_idx.index <= end
                aligned.append(
                    snapshots[snap_idx[before].iloc[-1]]
                    if before.any()
                    else snapshots[0]
                )
                continue

            bid_agg: dict[float, float] = defaultdict(float)
            ask_agg: dict[float, float] = defaultdict(float)
            for si in indices:
                for lvl in snapshots[si].bids:
                    bid_agg[lvl.price] += lvl.size
                for lvl in snapshots[si].asks:
                    ask_agg[lvl.price] += lvl.size

            aligned.append(
                OrderBookSnapshot(
                    timestamp=end,
                    bids=[
                        OrderBookLevel(p, s)
                        for p, s in sorted(bid_agg.items(), key=lambda x: -x[0])
                    ],
                    asks=[
                        OrderBookLevel(p, s)
                        for p, s in sorted(ask_agg.items(), key=lambda x: x[0])
                    ],
                )
            )
    else:
        raise ValueError(
            f"Unknown method '{method}'. Use 'last', 'nearest', or 'vwap'."
        )

    return aligned
