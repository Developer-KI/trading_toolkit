"""
core/feeds.py — Abstract feed and bar-builder interfaces.

Separated from execution/base_executor_feed.py so that data/feeds/
implementations can import BaseFeed / BaseBarBuilder without pulling
in any execution-layer code (avoids a data→execution circular import).

execution/base_executor_feed.py re-exports these for backward compat.
"""

from __future__ import annotations

import abc
import logging
import threading
import time
from typing import Callable

import numpy as np
import pandas as pd

from core.models import OrderBookSnapshot

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  Abstract Feed
# ═══════════════════════════════════════════════════════════════════════════


class BaseFeed(abc.ABC):
    """
    Real-time data feed (WebSocket or polling).

    Provides L2 snapshots, trade ticks, and candle updates for one symbol.
    """

    @property
    @abc.abstractmethod
    def exchange_name(self) -> str:
        ...

    @abc.abstractmethod
    def start(
        self,
        on_trade: Callable | None = None,
        on_candle: Callable | None = None,
        on_l2: Callable | None = None,
    ):
        """Start the feed in a background thread."""
        ...

    @abc.abstractmethod
    def stop(self):
        """Shut down the feed."""
        ...

    @property
    @abc.abstractmethod
    def latest_l2(self) -> OrderBookSnapshot | None:
        ...


# ═══════════════════════════════════════════════════════════════════════════
#  Generic Bar Builder (works for any exchange)
# ═══════════════════════════════════════════════════════════════════════════


class BaseBarBuilder:
    """
    Builds OHLCV bars from a stream of trades.

    This is exchange-agnostic — the trade dict just needs:
        {timestamp: pd.Timestamp, price: float, size: float}

    Each exchange's Feed converts its native trade format into this dict
    before calling bar_builder.on_trade().
    """

    def __init__(
        self,
        interval_s: int = 60,
        max_bars: int = 2000,
        on_bar_close: Callable[[pd.DataFrame], None] | None = None,
    ):
        self.interval_s = interval_s
        self.max_bars = max_bars
        self.on_bar_close = on_bar_close

        self._lock = threading.Lock()
        self._bars: list[dict] = []
        self._current: dict | None = None
        self._current_end: pd.Timestamp | None = None

        # Diagnostics
        self._trade_count: int = 0
        self._candle_count: int = 0
        self._bar_close_count: int = 0
        self._last_diag_time: float = 0.0

    # ── Public API ───────────────────────────────────────────────────────

    def seed(self, historical_df: pd.DataFrame):
        """Seed with historical OHLCV bars (for indicator warm-up)."""
        with self._lock:
            for ts, row in historical_df.iterrows():
                self._bars.append({
                    "timestamp": ts,
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                    "volume": row.get("volume", 0.0),
                })
            self._trim()

            now = pd.Timestamp.utcnow()
            bar_end = self._bar_boundary(now)
            last_close = self._bars[-1]["close"] if self._bars else 0.0
            bar_start_epoch = (now.value // 10**9 // self.interval_s) * self.interval_s
            self._current = {
                "timestamp": pd.Timestamp(bar_start_epoch, unit="s"),
                "open": last_close,
                "high": last_close,
                "low": last_close,
                "close": last_close,
                "volume": 0.0,
            }
            self._current_end = bar_end

        logger.info(
            "Seeded bar builder with %d historical bars | "
            "current_end=%s | now=%s | interval=%ds",
            len(historical_df),
            self._current_end,
            pd.Timestamp.utcnow(),
            self.interval_s,
        )

    def on_trade(self, trade: dict):
        """Process a trade tick: {timestamp, price, size}."""
        ts: pd.Timestamp = trade["timestamp"]
        px: float = trade["price"]
        sz: float = trade["size"]

        self._trade_count += 1

        # Periodic diagnostic: log every 30s to confirm trades are flowing
        now = time.time()
        if now - self._last_diag_time > 30:
            with self._lock:
                cur_end = self._current_end
                n_bars = len(self._bars)
            logger.info(
                "BarBuilder diag | trades_in=%d | bars_closed=%d | "
                "completed_bars=%d | current_end=%s | last_trade_ts=%s",
                self._trade_count,
                self._bar_close_count,
                n_bars,
                cur_end,
                ts,
            )
            self._last_diag_time = now

        fire_callback = False

        with self._lock:
            bar_end = self._bar_boundary(ts)

            if self._current is None:
                self._start_bar(ts, px, sz, bar_end)
                logger.debug(
                    "BarBuilder: first trade, started bar ending at %s", bar_end
                )
                return

            if bar_end > self._current_end:
                self._close_current_bar()
                self._bar_close_count += 1
                fire_callback = True
                self._start_bar(ts, px, sz, bar_end)
                logger.info(
                    "BarBuilder: BAR CLOSED (#%d) | trade_ts=%s | "
                    "old_end=%s | new_end=%s | total_bars=%d",
                    self._bar_close_count,
                    ts,
                    self._current_end,
                    bar_end,
                    len(self._bars),
                )
            else:
                self._current["high"] = max(self._current["high"], px)
                self._current["low"] = min(self._current["low"], px)
                self._current["close"] = px
                self._current["volume"] += sz

        if fire_callback and self.on_bar_close:
            logger.info(
                "BarBuilder: firing on_bar_close callback (bars=%d)",
                len(self._bars),
            )
            try:
                self.on_bar_close(self.to_dataframe())
            except Exception as e:
                logger.error("BarBuilder: on_bar_close callback FAILED: %s", e, exc_info=True)

    def on_candle(self, candle: dict):
        """Process a candle update (fallback source)."""
        self._candle_count += 1
        if not candle.get("is_closed", False):
            return
        ts = candle["timestamp"]
        with self._lock:
            if self._bars and self._bars[-1]["timestamp"] >= ts:
                logger.debug("BarBuilder: candle for %s already covered, skipping", ts)
                return
            self._bars.append({
                "timestamp": ts,
                "open": candle["open"],
                "high": candle["high"],
                "low": candle["low"],
                "close": candle["close"],
                "volume": candle["volume"],
            })
            self._trim()
            self._bar_close_count += 1

        logger.info(
            "BarBuilder: CANDLE BAR CLOSED (#%d) | ts=%s | total_bars=%d",
            self._bar_close_count, ts, len(self._bars),
        )

        if self.on_bar_close:
            try:
                self.on_bar_close(self.to_dataframe())
            except Exception as e:
                logger.error("BarBuilder: on_bar_close (candle) FAILED: %s", e, exc_info=True)

    def to_dataframe(self) -> pd.DataFrame:
        with self._lock:
            if not self._bars:
                return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            df = pd.DataFrame(self._bars)
            df.set_index("timestamp", inplace=True)
            df.index.name = None
            return df

    @property
    def last_close(self) -> float:
        with self._lock:
            if self._bars:
                return self._bars[-1]["close"]
            return np.nan

    @property
    def bar_count(self) -> int:
        with self._lock:
            return len(self._bars)

    # ── Internal ─────────────────────────────────────────────────────────

    def _bar_boundary(self, ts: pd.Timestamp) -> pd.Timestamp:
        epoch_s = ts.value // 10**9
        bar_start = (epoch_s // self.interval_s) * self.interval_s
        bar_end = bar_start + self.interval_s
        return pd.Timestamp(bar_end, unit="s")

    def _start_bar(self, ts, px, sz, bar_end):
        bar_start_epoch = (ts.value // 10**9 // self.interval_s) * self.interval_s
        self._current = {
            "timestamp": pd.Timestamp(bar_start_epoch, unit="s"),
            "open": px, "high": px, "low": px, "close": px, "volume": sz,
        }
        self._current_end = bar_end

    def _close_current_bar(self):
        if self._current is None:
            return
        self._bars.append(self._current)
        self._trim()
        self._current = None
        self._current_end = None

    def _trim(self):
        if len(self._bars) > self.max_bars:
            self._bars = self._bars[-self.max_bars:]
