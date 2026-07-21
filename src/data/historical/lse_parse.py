"""
data/historical/lse_parse.py — London Strategic Edge historical data fetcher.

Public API
----------
TIMEFRAMES          All timeframes the LSE API accepts.
BACKTEST_TIMEFRAMES Subset safe to pass to the core backtester.

get_api_key()       Resolve an LSE API key from argument or .env.
fetch_ohlcv()       Fetch OHLCV bars for a single symbol.
fetch_multi()       Fetch OHLCV bars for multiple symbols (returns a dict).
fetch_catalog()     Retrieve the full LSE instrument catalog.
build_universe()    Wrap one or many OHLCV DataFrames in a Universe.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pandas as pd

# ── Timeframe constants ───────────────────────────────────────────────────────

# All intervals the LSE API supports
TIMEFRAMES: list[str] = [
    "1s", "5s", "15s", "30s",
    "1m", "3m", "5m", "15m", "30m",
    "1h", "4h",
    "1d", "1w", "1mo",
]

# Subset that maps cleanly to core/parser.py's _TIMEFRAME_MAP
BACKTEST_TIMEFRAMES: list[str] = [
    "1m", "5m", "15m", "30m",
    "1h", "4h",
    "1d",
]


# ── Credentials ───────────────────────────────────────────────────────────────

def get_api_key(api_key: str = "", env_file: Path | str | None = None) -> str:
    """
    Return a resolved LSE API key.

    Resolution order:
      1. ``api_key`` argument (if non-empty)
      2. LSE_DATA in the .env file at ``env_file`` (or the project root .env)
      3. LSE_DATA environment variable

    Raises ``ValueError`` when no key is found.
    """
    if api_key.strip():
        return api_key.strip()

    # Walk up to find the project .env when not specified
    if env_file is None:
        env_file = _find_env()

    try:
        from dotenv import dotenv_values
        key = dotenv_values(env_file).get("LSE_DATA", "")
    except Exception:
        key = ""

    if not key:
        import os
        key = os.environ.get("LSE_DATA", "")

    if not key:
        raise ValueError(
            "LSE API key not found. "
            "Set LSE_DATA in your .env file, pass it as api_key, "
            "or export it as an environment variable."
        )
    return key


def _find_env() -> Path:
    """Walk from this file's location upward until we find a .env, or give up."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / ".env"
        if candidate.exists():
            return candidate
        if (parent / ".git").exists():
            break
    return Path(".env")


# ── Core fetch ────────────────────────────────────────────────────────────────

def fetch_ohlcv(
    symbol: str,
    timeframe: str = "1d",
    start: str = "2000-01-01",
    end: str = "2100-01-01",
    api_key: str = "",
    env_file: Path | str | None = None,
) -> pd.DataFrame:
    """
    Fetch OHLCV bars from the LSE API for a single symbol.

    Parameters
    ----------
    symbol    : Ticker exactly as in the LSE catalog, e.g. ``"AAPL"``, ``"BTC/USD"``.
    timeframe : One of :data:`TIMEFRAMES` (default ``"1d"``).
    start     : ISO date string, e.g. ``"2010-01-01"``.
    end       : ISO date string, e.g. ``"2025-12-31"``.
    api_key   : LSE key; falls back to ``LSE_DATA`` env var / .env.
    env_file  : Path to a .env file that contains ``LSE_DATA``.

    Returns
    -------
    pandas.DataFrame
        UTC-aware ``DatetimeIndex`` named ``timestamp``, columns
        ``[open, high, low, close, volume]``, all ``float64``.

    Raises
    ------
    ImportError  – ``lse-data`` package not installed.
    ValueError   – no API key, or the API returns no data.
    """
    _require_lse()
    from lse import LSE

    key = get_api_key(api_key, env_file)
    client = LSE(api_key=key)

    try:
        rows = client.candles(symbol, timeframe, start=start, end=end)
    except Exception as exc:
        msg = str(exc).lower()
        if "no candle data" in msg:
            raise ValueError(
                f"LSE returned no candle data for {symbol!r} "
                f"({timeframe}, {start} → {end})."
            ) from exc
        raise

    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError(
            f"No data returned for {symbol!r} {timeframe} {start} → {end}."
        )

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp").sort_index()

    # Forex/crypto candles may carry no volume field
    if "volume" not in df.columns:
        df["volume"] = 0.0

    return df[["open", "high", "low", "close", "volume"]].astype(float)


def fetch_multi(
    symbols: Sequence[str],
    timeframe: str = "1d",
    start: str = "2000-01-01",
    end: str = "2100-01-01",
    api_key: str = "",
    env_file: Path | str | None = None,
    skip_errors: bool = False,
) -> dict[str, pd.DataFrame]:
    """
    Fetch OHLCV bars for multiple symbols in one call.

    Parameters
    ----------
    symbols      : Sequence of tickers.
    timeframe    : Shared timeframe for all symbols.
    start / end  : Date range applied to every symbol.
    api_key      : LSE key; falls back to env.
    env_file     : Path to a .env file that contains ``LSE_DATA``.
    skip_errors  : When ``True`` symbols that fail are omitted from the result
                   instead of raising. Useful for mixed catalogs that include
                   tickers with limited history.

    Returns
    -------
    dict mapping symbol → DataFrame (same schema as :func:`fetch_ohlcv`).
    """
    _require_lse()
    from lse import LSE

    key = get_api_key(api_key, env_file)
    client = LSE(api_key=key)
    out: dict[str, pd.DataFrame] = {}

    for sym in symbols:
        try:
            rows = client.candles(sym, timeframe, start=start, end=end)
            df = pd.DataFrame(rows)
            if df.empty:
                raise ValueError(f"Empty response for {sym!r}.")
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            df = df.set_index("timestamp").sort_index()
            if "volume" not in df.columns:
                df["volume"] = 0.0
            out[sym] = df[["open", "high", "low", "close", "volume"]].astype(float)
        except Exception:
            if not skip_errors:
                raise
    return out


# ── Catalog ───────────────────────────────────────────────────────────────────

def fetch_catalog(
    api_key: str = "",
    env_file: Path | str | None = None,
) -> list[dict]:
    """
    Return the full LSE instrument catalog.

    Each entry is a dict with keys:
    ``symbol``, ``name``, ``category``, ``dataset``, ``ticks``,
    ``first``, ``last``, ``country``.

    Raises ``ValueError`` when no API key is available.
    """
    _require_lse()
    from lse import LSE

    key = get_api_key(api_key, env_file)
    client = LSE(api_key=key)
    return client.catalog()


def filter_catalog(
    catalog: list[dict],
    category: str | None = None,
    dataset: str | None = None,
    country: str | None = None,
    query: str | None = None,
) -> list[dict]:
    """
    Filter a catalog list returned by :func:`fetch_catalog`.

    Parameters
    ----------
    category : e.g. ``"equity"``, ``"crypto"``, ``"fx"``
    dataset  : e.g. ``"us_stocks"``, ``"crypto_spot"``
    country  : Two-letter ISO code, e.g. ``"US"``, ``"GB"``
    query    : Case-insensitive substring match on ``symbol`` or ``name``.
    """
    rows = catalog
    if category:
        rows = [r for r in rows if r.get("category", "").lower() == category.lower()]
    if dataset:
        rows = [r for r in rows if r.get("dataset", "").lower() == dataset.lower()]
    if country:
        rows = [r for r in rows if r.get("country", "").upper() == country.upper()]
    if query:
        q = query.lower()
        rows = [
            r for r in rows
            if q in r.get("symbol", "").lower() or q in r.get("name", "").lower()
        ]
    return rows


# ── Universe builder ──────────────────────────────────────────────────────────

def build_universe(
    data: pd.DataFrame | dict[str, pd.DataFrame],
    symbol: str | None = None,
):
    """
    Wrap OHLCV data in a :class:`core.universe.Universe` for the backtester.

    Accepts either:
    - a single DataFrame + ``symbol`` name, or
    - a ``dict[symbol, DataFrame]`` (``symbol`` is ignored in this case).

    Returns a ``Universe`` ready to pass to ``Backtester.run()``.
    """
    from core.universe import Universe

    if isinstance(data, dict):
        symbols = list(data.keys())
        uni = Universe(symbols=symbols)
        for sym, df in data.items():
            uni.add_asset(sym, df)
        return uni

    if symbol is None:
        raise ValueError("Pass symbol= when data is a single DataFrame.")
    uni = Universe(symbols=[symbol])
    uni.add_asset(symbol, data)
    return uni


# ── Internal helpers ──────────────────────────────────────────────────────────

def _require_lse() -> None:
    try:
        __import__("lse")
    except ImportError as exc:
        raise ImportError(
            "Missing dependency: lse-data. "
            "Install with:  pip install 'lse-data[frames]'"
        ) from exc
