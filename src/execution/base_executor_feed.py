"""
execution/base_executor.py — Abstract executor and feed interfaces.

Every exchange adapter implements these ABCs so the LiveEngine stays
exchange-agnostic.  The engine only ever touches BaseExecutor / BaseFeed /
BaseBarBuilder — it never imports anything Hyperliquid- or Binance-specific.

To add a new exchange:
  1. Subclass BaseExecutor  → exchange_myex.py
  2. Subclass BaseFeed      → exchange_myex.py  (or same file)
  3. (Optional) subclass BaseBarBuilder if the exchange has a special
     candle/trade format; otherwise re-use GenericBarBuilder.
"""

from __future__ import annotations

import abc
import logging
import threading
import time
from typing import Callable

import numpy as np
import pandas as pd

from core.models import AggregatedPosition, ExchangePosition, FundingSnapshot, FillResult, Side, Position, OrderBookSnapshot

logger = logging.getLogger(__name__)


# FillResult is now defined in core/models.py and imported above.


# ═══════════════════════════════════════════════════════════════════════════
#  Abstract Executor
# ═══════════════════════════════════════════════════════════════════════════


class BaseExecutor(abc.ABC):
    """
    Exchange order-execution layer.

    Every exchange adapter must implement these methods.  The LiveEngine
    calls only this interface — never exchange-specific details.
    """

    @property
    @abc.abstractmethod
    def exchange_name(self) -> str:
        """Lowercase identifier: 'hyperliquid', 'binance', 'bybit', …"""
        ...

    # ── Account state ────────────────────────────────────────────────────

    @abc.abstractmethod
    def get_equity(self) -> float:
        """Total account equity (margin + unrealized PnL)."""
        ...

    @abc.abstractmethod
    def get_position(self, symbol: str) -> Position:
        """Current position on *symbol* (FLAT if none)."""
        ...

    @abc.abstractmethod
    def get_mid_price(self, symbol: str) -> float:
        """Current mid price."""
        ...

    @abc.abstractmethod
    def get_open_orders(self, symbol: str) -> list[dict]:
        """All open orders on *symbol*."""
        ...

    # ── Order placement ──────────────────────────────────────────────────

    @abc.abstractmethod
    def market_order(
        self,
        symbol: str,
        side: Side,
        size: float,
        reduce_only: bool = False,
    ) -> FillResult:
        ...

    @abc.abstractmethod
    def limit_order(
        self,
        symbol: str,
        side: Side,
        size: float,
        price: float,
        reduce_only: bool = False,
    ) -> FillResult:
        ...

    @abc.abstractmethod
    def cancel_all(self, symbol: str) -> int:
        """Cancel all open orders on *symbol*. Return count cancelled."""
        ...

    @abc.abstractmethod
    def close_position(self, symbol: str) -> FillResult:
        """Flatten any open position on *symbol*."""
        ...

    # ── Optional ─────────────────────────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int, cross: bool = True):
        """Set leverage (not every exchange needs this)."""
        pass

    def fetch_historical_candles(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> list[dict]:
        """
        Fetch historical OHLCV candles for warm-up.

        Returns list of dicts with keys:
            timestamp (pd.Timestamp), open, high, low, close, volume
        """
        raise NotImplementedError(
            f"{self.exchange_name} does not implement fetch_historical_candles"
        )

    def fetch_funding_rate(self, symbol: str) -> FundingSnapshot | None:
        """
        Fetch the current (or most recent) funding rate for a perpetual.

        Returns a FundingSnapshot or None if the exchange doesn't support it.
        The live engine calls this periodically and injects the result into
        the Universe so strategies and cost models see real funding data.
        """
        return None


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


class MultiExchangePortfolio:
    """
    Aggregates positions and equity across registered exchanges.

    Thread-safe: all reads go through a lock because multiple engine
    threads (one per exchange) may query simultaneously.
    """

    def __init__(self):
        self._executors: dict[str, BaseExecutor] = {}
        self._lock = threading.Lock()
        # Cached equity per exchange (updated by refresh())
        self._equity: dict[str, float] = {}

    def register(self, executor: BaseExecutor):
        """Register an exchange executor."""
        name = executor.exchange_name
        with self._lock:
            self._executors[name] = executor
            self._equity[name] = 0.0
        logger.info("Portfolio registered exchange: %s", name)

    def unregister(self, exchange_name: str):
        with self._lock:
            self._executors.pop(exchange_name, None)
            self._equity.pop(exchange_name, None)

    @property
    def exchanges(self) -> list[str]:
        with self._lock:
            return list(self._executors.keys())

    def get_executor(self, exchange: str) -> BaseExecutor:
        with self._lock:
            if exchange not in self._executors:
                raise KeyError(
                    f"Exchange '{exchange}' not registered. "
                    f"Available: {list(self._executors.keys())}"
                )
            return self._executors[exchange]

    # ── Equity ───────────────────────────────────────────────────────────

    def refresh_equity(self):
        """Query equity from all exchanges (call periodically)."""
        with self._lock:
            executors = dict(self._executors)

        for name, ex in executors.items():
            try:
                eq = ex.get_equity()
                with self._lock:
                    self._equity[name] = eq
            except Exception as e:
                logger.warning("Equity refresh failed for %s: %s", name, e)

    def total_equity(self) -> float:
        """Sum of equity across all exchanges."""
        with self._lock:
            return sum(self._equity.values())

    def equity_breakdown(self) -> dict[str, float]:
        """Equity per exchange."""
        with self._lock:
            return dict(self._equity)

    # ── Positions ────────────────────────────────────────────────────────

    def get_position(self, symbol: str, exchange: str) -> Position:
        """Get position on a specific exchange."""
        ex = self.get_executor(exchange)
        return ex.get_position(symbol)

    def net_position(self, symbol: str) -> AggregatedPosition:
        """
        Compute net position for a symbol across all exchanges.
        Positive net_size = net long, negative = net short.
        """
        with self._lock:
            executors = dict(self._executors)

        agg = AggregatedPosition(symbol=symbol)
        net = 0.0

        for name, ex in executors.items():
            try:
                pos = ex.get_position(symbol)
                ep = ExchangePosition(
                    exchange=name,
                    symbol=symbol,
                    side=pos.side,
                    size=pos.size,
                    entry_price=pos.entry_price,
                    unrealized_pnl=getattr(pos, "unrealized_pnl", 0.0),
                )
                agg.per_exchange.append(ep)

                if pos.side == Side.LONG:
                    net += pos.size
                    agg.gross_long += pos.size
                elif pos.side == Side.SHORT:
                    net -= pos.size
                    agg.gross_short += pos.size
            except Exception as e:
                logger.warning("Position query failed for %s on %s: %s", symbol, name, e)

        agg.net_size = net
        if net > 1e-10:
            agg.net_side = Side.LONG
        elif net < -1e-10:
            agg.net_side = Side.SHORT
        else:
            agg.net_side = Side.FLAT

        return agg

    def all_positions(self, symbols: list[str] | None = None) -> dict[str, AggregatedPosition]:
        """
        Net positions for multiple symbols.

        If symbols not provided, queries each executor for all positions
        and collects the union of symbols seen.
        """
        if symbols:
            return {s: self.net_position(s) for s in symbols}

        # Discover all symbols with open positions
        with self._lock:
            executors = dict(self._executors)

        seen_symbols: set[str] = set()
        for name, ex in executors.items():
            try:
                # Try to get all positions — exchange-specific
                if hasattr(ex, "get_all_positions"):
                    for pos in ex.get_all_positions():
                        if pos.side != Side.FLAT:
                            seen_symbols.add(pos.symbol if hasattr(pos, "symbol") else "")
            except Exception:
                pass

        if symbols:
            seen_symbols.update(symbols)

        return {s: self.net_position(s) for s in seen_symbols if s}

    # ── Exposure ─────────────────────────────────────────────────────────

    def net_exposure(self, symbols: list[str], prices: dict[str, float]) -> float:
        """
        Net dollar exposure across all exchanges.

        Args:
            symbols: list of symbols to consider
            prices: {symbol: current_price}

        Returns:
            Net $ exposure (positive = net long, negative = net short).
        """
        total = 0.0
        for sym in symbols:
            agg = self.net_position(sym)
            px = prices.get(sym, 0.0)
            total += agg.net_size * px
        return total

    def net_exposure_pct(self, symbols: list[str], prices: dict[str, float]) -> float:
        """Net exposure as a percentage of total equity."""
        equity = self.total_equity()
        if equity <= 0:
            return 0.0
        return self.net_exposure(symbols, prices) / equity

    def gross_exposure(self, symbols: list[str], prices: dict[str, float]) -> float:
        """Total $ exposure ignoring direction (long + short)."""
        total = 0.0
        for sym in symbols:
            agg = self.net_position(sym)
            px = prices.get(sym, 0.0)
            total += agg.gross_exposure * px
        return total

    # ── Convenience ──────────────────────────────────────────────────────

    def flatten_all(self, symbols: list[str]):
        """Close all positions on all exchanges for the given symbols."""
        with self._lock:
            executors = dict(self._executors)

        for name, ex in executors.items():
            for sym in symbols:
                try:
                    pos = ex.get_position(sym)
                    if pos.side != Side.FLAT and pos.size > 0:
                        logger.info("Flattening %s on %s", sym, name)
                        ex.close_position(sym)
                        ex.cancel_all(sym)
                except Exception as e:
                    logger.error("Flatten %s on %s failed: %s", sym, name, e)

    def summary(self, symbols: list[str], prices: dict[str, float] | None = None) -> str:
        """Human-readable portfolio summary."""
        lines = [
            "═══ Portfolio Summary ═══",
            f"Total equity: ${self.total_equity():,.2f}",
        ]
        for name, eq in self.equity_breakdown().items():
            lines.append(f"  {name}: ${eq:,.2f}")

        lines.append("")
        for sym in symbols:
            agg = self.net_position(sym)
            px = prices.get(sym, 0.0) if prices else 0.0
            notional = abs(agg.net_size) * px

            lines.append(f"{sym}:")
            lines.append(
                f"  Net: {agg.net_side.name} {abs(agg.net_size):.4f}"
                f" (${notional:,.2f})"
            )
            if agg.is_hedged:
                lines.append(
                    f"  ⚡ HEDGED — long {agg.gross_long:.4f}, "
                    f"short {agg.gross_short:.4f}"
                )
            for ep in agg.per_exchange:
                if ep.side != Side.FLAT:
                    lines.append(
                        f"    {ep.exchange}: {ep.side.name} {ep.size:.4f} "
                        f"@ {ep.entry_price:.2f}"
                    )

        return "\n".join(lines)