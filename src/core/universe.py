"""
core/universe.py — Universe and auxiliary data management.

A Universe holds OHLCV DataFrames for every asset in the strategy's scope,
plus any number of auxiliary DataSources (funding rates, sentiment, on-chain
metrics, macro indicators, etc.).

The Universe is the single data container passed to Strategy.setup() and
Strategy.generate(), so signals have a uniform view of all available data
regardless of whether you're backtesting or running live.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from core.models import OrderBookSnapshot, FundingSnapshot


# ── Auxiliary data sources ───────────────────────────────────────────────────


class DataSource(abc.ABC):
    """
    Abstract base for any non-OHLCV data feed.

    Subclass this to plug in funding rates, sentiment scores,
    on-chain metrics, macro series, alternative data, etc.

    The contract:
      • name        — unique identifier for this source
      • fetch()     — return a DataFrame (rows aligned to timestamps)
      • schema      — column descriptions (for documentation/validation)
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Unique identifier, e.g. 'funding_rates', 'fear_greed'."""
        ...

    @abc.abstractmethod
    def fetch(
        self,
        symbols: list[str],
        start: pd.Timestamp | None = None,
        end: pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        """
        Return a DataFrame of auxiliary data.

        The returned DataFrame should have a DatetimeIndex.
        Columns can be anything — multi-level columns (symbol, metric)
        are encouraged for per-asset aux data.
        """
        ...

    @property
    def schema(self) -> dict[str, str]:
        """Optional: describe columns for documentation."""
        return {}


# ── Concrete data source examples ────────────────────────────────────────────


class StaticDataSource(DataSource):
    """
    Wraps a pre-loaded DataFrame as a DataSource.
    Useful for backtesting with historical auxiliary data.
    """

    def __init__(self, name: str, data: pd.DataFrame):
        self._name = name
        self._data = data

    @property
    def name(self) -> str:
        return self._name

    def fetch(self, symbols=None, start=None, end=None) -> pd.DataFrame:
        df = self._data
        if start is not None:
            df = df[df.index >= start]
        if end is not None:
            df = df[df.index <= end]
        return df


class CallableDataSource(DataSource):
    """
    Wraps any callable that returns a DataFrame.
    For live feeds: pass a function that queries an API.
    """

    def __init__(self, name: str, fetch_fn, schema: dict[str, str] | None = None):
        self._name = name
        self._fetch_fn = fetch_fn
        self._schema = schema or {}

    @property
    def name(self) -> str:
        return self._name

    def fetch(self, symbols=None, start=None, end=None) -> pd.DataFrame:
        return self._fetch_fn(symbols=symbols, start=start, end=end)

    @property
    def schema(self) -> dict[str, str]:
        return self._schema


# ── Universe ─────────────────────────────────────────────────────────────────


@dataclass
class AssetData:
    """All data associated with a single asset."""
    symbol: str
    ohlcv: pd.DataFrame                           # open/high/low/close/volume
    l2: list[OrderBookSnapshot] | None = None      # optional L2 book snapshots
    funding: list[FundingSnapshot] | None = None   # optional per-bar funding rates
    meta: dict[str, Any] = field(default_factory=dict)  # tick size, lot size, etc.


class Universe:
    """
    Multi-asset data container.

    Holds per-asset OHLCV + L2 data and any number of auxiliary DataSources.
    Provides a clean interface for strategies to access everything by symbol
    or in bulk.

    Usage (backtest):
        universe = Universe(symbols=["ETH", "BTC", "SOL"])
        universe.add_asset("ETH", eth_df)
        universe.add_asset("BTC", btc_df)
        universe.add_asset("SOL", sol_df)
        universe.add_data_source(StaticDataSource("funding", funding_df))

    Usage (live):
        universe = Universe(symbols=["ETH", "BTC"])
        # Assets populated incrementally by bar builders
        universe.add_data_source(CallableDataSource("funding", fetch_funding))
    """

    def __init__(self, symbols: list[str] | None = None):
        self._symbols: list[str] = symbols or []
        self._assets: dict[str, AssetData] = {}
        self._aux_sources: dict[str, DataSource] = {}
        self._aux_cache: dict[str, pd.DataFrame] = {}

    # ── Asset management ─────────────────────────────────────────────────

    @property
    def symbols(self) -> list[str]:
        return list(self._symbols)

    def add_asset(
        self,
        symbol: str,
        ohlcv: pd.DataFrame,
        l2: list[OrderBookSnapshot] | None = None,
        funding: list[FundingSnapshot] | None = None,
        meta: dict[str, Any] | None = None,
    ):
        """Register an asset with its OHLCV (and optional L2 / funding) data."""
        if symbol not in self._symbols:
            self._symbols.append(symbol)
        self._assets[symbol] = AssetData(
            symbol=symbol,
            ohlcv=ohlcv.copy(),
            l2=l2,
            funding=funding,
            meta=meta or {},
        )

    def update_asset_bars(self, symbol: str, ohlcv: pd.DataFrame):
        """Replace the OHLCV data for a live-updating asset."""
        if symbol in self._assets:
            self._assets[symbol].ohlcv = ohlcv
        else:
            self.add_asset(symbol, ohlcv)

    def get_asset(self, symbol: str) -> AssetData:
        if symbol not in self._assets:
            raise KeyError(f"Asset '{symbol}' not in universe. Available: {self.symbols}")
        return self._assets[symbol]

    def ohlcv(self, symbol: str) -> pd.DataFrame:
        """Shortcut: get OHLCV DataFrame for one asset."""
        return self.get_asset(symbol).ohlcv

    def close(self, symbol: str) -> pd.Series:
        """Shortcut: get close prices for one asset."""
        return self.ohlcv(symbol)["close"]

    def l2(self, symbol: str) -> list[OrderBookSnapshot] | None:
        return self.get_asset(symbol).l2

    def funding(self, symbol: str) -> list[FundingSnapshot] | None:
        """Funding rate snapshots for one asset (aligned to OHLCV index)."""
        return self.get_asset(symbol).funding

    def funding_at(self, symbol: str, idx: int) -> FundingSnapshot | None:
        """Single funding snapshot at bar index, or None."""
        f = self.funding(symbol)
        if f and idx < len(f):
            return f[idx]
        return None

    def update_funding(self, symbol: str, funding: list[FundingSnapshot]):
        """Replace the funding list for a live-updating asset."""
        if symbol in self._assets:
            self._assets[symbol].funding = funding

    def all_closes(self) -> pd.DataFrame:
        """
        DataFrame of close prices, one column per asset.
        Columns are symbol names. Index is the union of all timestamps.
        """
        series = {}
        for sym in self._symbols:
            if sym in self._assets:
                series[sym] = self._assets[sym].ohlcv["close"]
        if not series:
            return pd.DataFrame()
        return pd.DataFrame(series)

    def returns(self, symbol: str | None = None) -> pd.DataFrame | pd.Series:
        """Simple returns. If symbol given, returns Series; else DataFrame."""
        if symbol:
            return self.close(symbol).pct_change()
        return self.all_closes().pct_change()

    def correlation(self, window: int | None = None) -> pd.DataFrame:
        """Rolling or full-sample pairwise correlation of returns."""
        rets = self.returns()
        if window:
            return rets.rolling(window).corr()
        return rets.corr()

    # ── Auxiliary data ───────────────────────────────────────────────────

    def add_data_source(self, source: DataSource):
        """Register an auxiliary data source."""
        self._aux_sources[source.name] = source

    def aux(self, name: str, refresh: bool = False) -> pd.DataFrame:
        """
        Retrieve auxiliary data by source name.
        Results are cached; pass refresh=True to re-fetch.
        """
        if name not in self._aux_sources:
            raise KeyError(
                f"DataSource '{name}' not registered. "
                f"Available: {list(self._aux_sources.keys())}"
            )
        if refresh or name not in self._aux_cache:
            self._aux_cache[name] = self._aux_sources[name].fetch(self._symbols)
        return self._aux_cache[name]

    def aux_at(self, name: str, idx: int, symbol: str | None = None) -> dict[str, Any]:
        """
        Get auxiliary data values at a specific bar index.
        Returns a dict of {column: value}.
        If symbol given, filters to that symbol's columns.
        """
        df = self.aux(name)
        if idx >= len(df):
            return {}
        row = df.iloc[idx]
        if symbol and isinstance(df.columns, pd.MultiIndex):
            # Filter to this symbol's columns
            if symbol in df.columns.get_level_values(0):
                row = df[symbol].iloc[idx]
        return row.to_dict() if hasattr(row, "to_dict") else dict(row)

    def list_sources(self) -> list[str]:
        return list(self._aux_sources.keys())

    # ── Snapshot (what the strategy sees each bar) ───────────────────────

    def bar_count(self, symbol: str | None = None) -> int:
        """Number of bars. If no symbol given, returns the minimum across assets."""
        if symbol:
            return len(self.ohlcv(symbol))
        if not self._assets:
            return 0
        return min(len(a.ohlcv) for a in self._assets.values())

    def common_index(self) -> pd.DatetimeIndex | pd.Index:
        """
        Return the intersection of all asset indices.
        Useful for aligning bars across assets with different trading hours.
        """
        if not self._assets:
            return pd.DatetimeIndex([])
        indices = [a.ohlcv.index for a in self._assets.values()]
        common = indices[0]
        for idx in indices[1:]:
            common = common.intersection(idx)
        return common

    def __repr__(self):
        assets = ", ".join(self._symbols)
        sources = ", ".join(self._aux_sources.keys()) or "none"
        return f"Universe(assets=[{assets}], aux=[{sources}])"
