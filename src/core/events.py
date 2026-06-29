"""
core/events.py — Typed event structs for the execution hot path.

These are plain dataclasses that carry data between the feed layer and
the strategy layer. Using typed structs (rather than raw dicts) makes
the C++ binding surface explicit: each field maps to a C++ POD member.

When the feed layer moves to C++, these structs can be replaced with
pybind11-exposed C++ structs with zero-copy NumPy/buffer-protocol access.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class TradeEvent:
    """A single trade tick from the exchange feed."""
    timestamp: pd.Timestamp
    price: float
    size: float
    symbol: str = ""
    exchange: str = ""
    is_buyer_maker: bool = False
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "price": self.price,
            "size": self.size,
        }


@dataclass
class BarEvent:
    """A completed OHLCV bar, fired by BaseBarBuilder on bar close."""
    timestamp: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float
    symbol: str = ""
    exchange: str = ""
    interval_s: int = 60

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass
class L2Event:
    """An order book snapshot update from the exchange feed."""
    timestamp: pd.Timestamp
    symbol: str = ""
    exchange: str = ""
    bids: list[tuple[float, float]] = field(default_factory=list)  # (price, size)
    asks: list[tuple[float, float]] = field(default_factory=list)
