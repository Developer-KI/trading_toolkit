"""LSE (London Strategic Edge) historical bar fetching for the UI."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in [str(_ROOT / "src"), str(_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Timeframes offered in the UI — all supported by the LSE API
TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]

# Subset safe to pass to core's timeframe_to_seconds / backtester
BACKTEST_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]


def get_api_key(override: str = "") -> str:
    """Return the LSE API key: UI field if filled, else .env LSE_DATA value."""
    if override.strip():
        return override.strip()
    try:
        from dotenv import dotenv_values
        env = dotenv_values(_ROOT / ".env")
        return env.get("LSE_DATA", "")
    except Exception:
        return ""


def fetch_ohlcv(
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    api_key: str = "",
) -> pd.DataFrame:
    """
    Fetch OHLCV bars from the LSE API.

    Returns a DataFrame with a UTC-aware DatetimeIndex and columns
    [open, high, low, close, volume]. Raises on missing credentials or empty data.
    """
    try:
        from lse import LSE
    except ImportError as exc:
        raise ImportError(
            "Missing dependency: lse-data. Install with: pip install 'lse-data[frames]'"
        ) from exc

    key = get_api_key(api_key)
    if not key:
        raise ValueError(
            "LSE API key not set. Add LSE_DATA=your_key to .env or enter it in the sidebar."
        )

    client = LSE(api_key=key)
    try:
        rows = client.candles(symbol, timeframe, start=start, end=end)
    except Exception as exc:
        msg = str(exc)
        if "no candle data" in msg.lower():
            raise ValueError(f"NO_CANDLE_DATA:{symbol}") from exc
        raise

    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError(f"No data returned for {symbol} {timeframe} {start} → {end}.")

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp").sort_index()

    if "volume" not in df.columns:
        df["volume"] = 0

    return df[["open", "high", "low", "close", "volume"]].astype(float)


def load_bars_cached(
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    api_key: str = "",
    cache_key_prefix: str = "lse",
) -> pd.DataFrame | None:
    """Session-state cached wrapper around fetch_ohlcv. Returns None on error."""
    cache_key = f"_bars_{cache_key_prefix}_{symbol}_{timeframe}_{start}_{end}"
    if cache_key in st.session_state:
        return st.session_state[cache_key]
    try:
        df = fetch_ohlcv(symbol, timeframe, start, end, api_key)
        st.session_state[cache_key] = df
        return df
    except ValueError as e:
        msg = str(e)
        if msg.startswith("NO_CANDLE_DATA:"):
            sym = msg.split(":", 1)[1]
            st.warning(
                f"**{sym}** has no candlestick data on LSE. "
                "Select a different symbol from the catalog."
            )
        elif "No data returned" in msg:
            st.warning(
                f"{msg} The requested start date may be before available history. "
                "Try a more recent start date."
            )
        else:
            st.error(f"Data fetch failed: {e}")
        return None
    except Exception as e:
        st.error(f"Data fetch failed: {e}")
        return None


def fetch_option_chain(
    underlying: str,
    expiry: str | None = None,
    option_type: str | None = None,
    min_dte: int | None = None,
    max_dte: int | None = None,
    api_key: str = "",
):
    """
    Fetch the current option chain for an underlying from the LSE API.

    Returns a `core.derivatives.OptionChain`. Raises on missing credentials or when
    the provider returns no contracts. The raw rows (price, provider IV/greeks, OSI ticker)
    are preserved in each contract's `meta`, so our own reconstruction can be sanity-checked
    against the provider's values.
    """
    from core.derivatives import OptionChain

    try:
        from lse import LSE
    except ImportError as exc:
        raise ImportError(
            "Missing dependency: lse-data. Install with: pip install 'lse-data[frames]'"
        ) from exc

    key = get_api_key(api_key)
    if not key:
        raise ValueError(
            "LSE API key not set. Add LSE_DATA=your_key to .env or enter it in the sidebar."
        )

    client = LSE(api_key=key)
    rows = client.options(
        underlying,
        type=option_type,
        expiry=expiry,
        min_dte=min_dte,
        max_dte=max_dte,
    )
    if not rows:
        raise ValueError(f"NO_OPTION_DATA:{underlying}")

    return OptionChain.from_records(rows, underlying=underlying, asof=pd.Timestamp.utcnow())


def load_chain_cached(
    underlying: str,
    expiry: str | None = None,
    min_dte: int | None = None,
    max_dte: int | None = None,
    api_key: str = "",
):
    """Session-state cached wrapper around fetch_option_chain. Returns None on error."""
    cache_key = f"_chain_{underlying}_{expiry}_{min_dte}_{max_dte}"
    if cache_key in st.session_state:
        return st.session_state[cache_key]
    try:
        chain = fetch_option_chain(
            underlying, expiry=expiry, min_dte=min_dte, max_dte=max_dte, api_key=api_key
        )
        st.session_state[cache_key] = chain
        return chain
    except ValueError as e:
        msg = str(e)
        if msg.startswith("NO_OPTION_DATA:"):
            sym = msg.split(":", 1)[1]
            st.warning(
                f"**{sym}** has no listed options on LSE, or the market is closed. "
                "Pick another underlying or upload a chain file below."
            )
        else:
            st.error(f"Option chain fetch failed: {e}")
        return None
    except Exception as e:
        st.error(f"Option chain fetch failed: {e}")
        return None


def fetch_option_underlyings(api_key: str = "") -> list[dict] | None:
    """Every underlying with listed options: [{'symbol', 'name'}, ...] or None on failure."""
    try:
        from lse import LSE
    except ImportError:
        return None
    key = get_api_key(api_key)
    if not key:
        return None
    try:
        return LSE(api_key=key).options_underlyings()
    except Exception:
        return None


def fetch_catalog(api_key: str = "") -> list[dict] | None:
    """
    Fetch the full LSE instrument catalog.

    Returns a list of dicts: {symbol, name, category, dataset, ticks, first, last, country}.
    Returns None when credentials are missing or the call fails.
    """
    try:
        from lse import LSE
    except ImportError:
        return None
    key = get_api_key(api_key)
    if not key:
        return None
    try:
        client = LSE(api_key=key)
        return client.catalog()
    except Exception:
        return None


def build_universe(symbols, ohlcv_data):
    """
    Wrap OHLCV data in a Universe for backtester consumption.

    Single asset:  build_universe("AAPL", df)
    Multi-asset:   build_universe(["AAPL", "MSFT"], {"AAPL": df1, "MSFT": df2})
    """
    from core.universe import Universe
    if isinstance(symbols, str):
        uni = Universe(symbols=[symbols])
        uni.add_asset(symbols, ohlcv_data)
        return uni
    syms = list(symbols)
    uni = Universe(symbols=syms)
    for sym in syms:
        uni.add_asset(sym, ohlcv_data[sym])
    return uni
