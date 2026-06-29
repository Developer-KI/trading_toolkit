"""
data/feeds/base.py — Unified DataFeed Protocol for all live data sources.

Every live WebSocket feed in this package implements DataFeedProtocol.
This gives the execution layer a single contract to program against,
regardless of whether the underlying transport is Hyperliquid, Binance,
or a future C++ feed.

Relationship to execution/base_executor_feed.py:
  BaseFeed (execution/) — tied to the execution pipeline; binds to BarBuilder.
  DataFeedProtocol (here) — broader contract for data archival, analysis,
  and any consumer that doesn't need order execution.

Future direction: a C++ feed can satisfy DataFeedProtocol via pybind11
without inheriting from any Python class.
"""

from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

import pandas as pd


@runtime_checkable
class DataFeedProtocol(Protocol):
    """
    Minimal contract for a live market data feed.

    Implement subscribe/unsubscribe for selective symbol management,
    and latest_snapshot for polling the most recent data.

    Implementations:
      HyperliquidArchivalFeed (data/feeds/hyperliquid.py)
      BinanceArchivalFeed     (data/feeds/binance.py)
    """

    @property
    def name(self) -> str:
        """Human-readable feed identifier (e.g. 'hyperliquid-perp')."""
        ...

    def subscribe(self, symbol: str, **kwargs): ...
    def unsubscribe(self, symbol: str): ...

    def start(self): ...
    def stop(self): ...

    def latest_snapshot(self, symbol: str) -> dict | None:
        """
        Return the most recent data snapshot for a symbol.

        Format is feed-specific but should at minimum include:
            {timestamp, open, high, low, close, volume}
        for OHLCV feeds, or {timestamp, bids, asks} for L2 feeds.
        """
        ...
