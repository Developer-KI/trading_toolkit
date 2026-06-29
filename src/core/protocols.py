"""
core/protocols.py — Structural interfaces for the execution layer.

These are typing.Protocol definitions, not ABCs. The distinction matters
for C++ interoperability: a pybind11-wrapped C++ class satisfies a Protocol
without needing to inherit from a Python base class. ABCs require explicit
registration or inheritance — Protocols do not.

Usage
-----
Python exchange adapters (HyperliquidExecutor, BinanceExecutor) currently
inherit from the ABCs in execution/base_executor_feed.py. Those ABCs remain
for now so no existing code breaks.

When writing C++ extensions via pybind11, implement these Protocols instead:
the executor, feed, and bar builder will be accepted anywhere these types
are expected, without any Python base class overhead.

Type-check with: isinstance(obj, ExecutorProtocol) is always True for
structural matches — use typing.runtime_checkable if you need isinstance().
"""

from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

import pandas as pd

from .models import (
    FillResult,
    FundingSnapshot,
    OrderBookSnapshot,
    Position,
    Side,
)


@runtime_checkable
class ExecutorProtocol(Protocol):
    """
    Exchange order-execution contract.

    Future C++ replacement target: pybind11 module implementing these methods
    will be accepted by LiveEngine / MultiExchangeEngine without any changes
    to the engine code.
    """

    @property
    def exchange_name(self) -> str: ...

    def get_equity(self) -> float: ...
    def get_position(self, symbol: str) -> Position: ...
    def get_mid_price(self, symbol: str) -> float: ...
    def get_open_orders(self, symbol: str) -> list[dict]: ...

    def market_order(
        self, symbol: str, side: Side, size: float, reduce_only: bool = False,
    ) -> FillResult: ...

    def limit_order(
        self, symbol: str, side: Side, size: float, price: float,
        reduce_only: bool = False,
    ) -> FillResult: ...

    def cancel_all(self, symbol: str) -> int: ...
    def close_position(self, symbol: str) -> FillResult: ...

    # Optional — return None / raise NotImplementedError if unsupported
    def set_leverage(self, symbol: str, leverage: int, cross: bool = True): ...
    def fetch_historical_candles(
        self, symbol: str, interval: str, start_ms: int, end_ms: int,
    ) -> list[dict]: ...
    def fetch_funding_rate(self, symbol: str) -> FundingSnapshot | None: ...


@runtime_checkable
class FeedProtocol(Protocol):
    """
    Real-time data feed contract (WebSocket or polling).

    Future C++ replacement: implement this Protocol to swap the WebSocket
    layer for a low-latency C++ feed without touching the engine.
    """

    @property
    def exchange_name(self) -> str: ...

    def start(
        self,
        on_trade: Callable | None = None,
        on_candle: Callable | None = None,
        on_l2: Callable | None = None,
    ): ...

    def stop(self): ...

    @property
    def latest_l2(self) -> OrderBookSnapshot | None: ...


@runtime_checkable
class BarBuilderProtocol(Protocol):
    """
    OHLCV bar construction contract.

    This is the hottest Python path — every trade tick passes through here.
    Future C++ replacement: implement a lock-free ring-buffer bar builder
    in C++ exposed via pybind11. The engine calls only the methods below.
    """

    def seed(self, historical_df: pd.DataFrame): ...
    def on_trade(self, trade: dict): ...
    def on_candle(self, candle: dict): ...
    def to_dataframe(self) -> pd.DataFrame: ...

    @property
    def last_close(self) -> float: ...

    @property
    def bar_count(self) -> int: ...
