"""
Shared indicator functions.
These are static, stateless functions usable by any signal, sizer, or stop.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def atr(high: pd.Series, low: pd.Series, close: pd.Series,
        period: int = 14) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def bollinger(series: pd.Series, window: int = 20, num_std: float = 2.0):
    mid = series.rolling(window).mean()
    std = series.rolling(window).std()
    return mid, mid + num_std * std, mid - num_std * std


def vwap_rolling(price: pd.Series, volume: pd.Series, window: int) -> pd.Series:
    cum_pv = (price * volume).rolling(window).sum()
    cum_v = volume.rolling(window).sum()
    return cum_pv / cum_v


def order_flow_imbalance(
    bid_vol: pd.Series, ask_vol: pd.Series, window: int = 20
) -> pd.Series:
    diff = bid_vol - ask_vol
    total = bid_vol + ask_vol
    ofi = (diff / total.replace(0, np.nan)).rolling(window).mean()
    return ofi


def book_imbalance(bids: list, asks: list, levels: int = 5) -> float:
    """Bid/ask volume imbalance across top N levels of L2 snapshot."""
    bid_vol = sum(lvl.size for lvl in bids[:levels])
    ask_vol = sum(lvl.size for lvl in asks[:levels])
    total = bid_vol + ask_vol
    if total == 0:
        return 0.0
    return (bid_vol - ask_vol) / total


def compute_atr_column(data: pd.DataFrame, period: int = 14,
                       col_name: str = "atr") -> pd.DataFrame:
    """Add an ATR column to a DataFrame in-place and return it."""
    data[col_name] = atr(data["high"], data["low"], data["close"], period)
    return data