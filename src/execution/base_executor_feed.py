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
from typing import Callable

import pandas as pd

from core.models import AggregatedPosition, ExchangePosition, FundingSnapshot, FillResult, Side, Position, OrderBookSnapshot
from core.feeds import BaseFeed, BaseBarBuilder  # noqa: F401 — re-exported for backward compat

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