"""
data/feeds/alpaca.py — Alpaca Markets live feed for US equities.

Implements BaseFeed for the LiveEngine using alpaca-py StockDataStream.
Subscribes to trades, 1-minute bars, and quotes for one symbol.
Paper trading is supported natively by alpaca-py.

The stream is async-native; it runs in a dedicated event-loop daemon thread
so the sync/threaded LiveEngine can consume it without modification.

Install:
    pip install alpaca-py
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable

import pandas as pd

from core.feeds import BaseFeed
from core.models import OrderBookLevel, OrderBookSnapshot

logger = logging.getLogger(__name__)


class AlpacaFeed(BaseFeed):
    """Alpaca Markets live feed for one US equity symbol."""

    def __init__(self, symbol: str, api_key: str, api_secret: str, paper: bool = True):
        self.symbol = symbol
        self._api_key = api_key
        self._api_secret = api_secret
        self._paper = paper

        self._latest_l2: OrderBookSnapshot | None = None
        self._l2_lock = threading.Lock()

        self._on_trade: Callable | None = None
        self._on_candle: Callable | None = None
        self._on_l2: Callable | None = None

        self._stream = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._running = False

    @property
    def exchange_name(self) -> str:
        return "alpaca"

    def start(self, on_trade=None, on_candle=None, on_l2=None):
        self._on_trade = on_trade
        self._on_candle = on_candle
        self._on_l2 = on_l2
        self._running = True

        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name=f"alp-ws-{self.symbol}"
        )
        self._thread.start()
        self._ready.wait(timeout=20)
        if not self._ready.is_set():
            raise ConnectionError(
                f"AlpacaFeed: stream did not start within 20s for {self.symbol}"
            )
        logger.info("AlpacaFeed started for %s (paper=%s)", self.symbol, self._paper)

    def stop(self):
        self._running = False
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)

    @property
    def latest_l2(self) -> OrderBookSnapshot | None:
        with self._l2_lock:
            return self._latest_l2

    # ── Internal ──────────────────────────────────────────────────────────

    def _run_loop(self):
        """Run the alpaca-py async stream in a dedicated event loop."""
        try:
            from alpaca.data.live import StockDataStream
        except ImportError as exc:
            raise ImportError(
                "Missing dependency: alpaca-py. Install with: pip install alpaca-py"
            ) from exc

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        self._stream = StockDataStream(
            api_key=self._api_key,
            secret_key=self._api_secret,
            feed="iex",  # free real-time; use "sip" for full tape (paid plan)
        )

        self._stream.subscribe_trades(self._handle_trade, self.symbol)
        self._stream.subscribe_bars(self._handle_bar, self.symbol)
        self._stream.subscribe_quotes(self._handle_quote, self.symbol)

        # Signal that subscriptions are wired before blocking on run()
        self._ready.set()

        try:
            self._stream.run()
        except Exception as exc:
            if self._running:
                logger.error("AlpacaFeed stream error: %s", exc)

    async def _handle_trade(self, trade):
        if not self._on_trade:
            return
        try:
            self._on_trade({
                "timestamp": pd.Timestamp(trade.timestamp),
                "price": float(trade.price),
                "size": float(trade.size),
                "side": "B",  # Alpaca trade data does not expose aggressor side
            })
        except Exception as exc:
            logger.debug("AlpacaFeed trade parse error: %s", exc)

    async def _handle_bar(self, bar):
        if not self._on_candle:
            return
        try:
            self._on_candle({
                "timestamp": pd.Timestamp(bar.timestamp),
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(bar.volume),
                "is_closed": True,
            })
        except Exception as exc:
            logger.debug("AlpacaFeed bar parse error: %s", exc)

    async def _handle_quote(self, quote):
        try:
            ts = pd.Timestamp(quote.timestamp)
            bids = [OrderBookLevel(price=float(quote.bid_price), size=float(quote.bid_size))]
            asks = [OrderBookLevel(price=float(quote.ask_price), size=float(quote.ask_size))]
            snap = OrderBookSnapshot(timestamp=ts, bids=bids, asks=asks)
            with self._l2_lock:
                self._latest_l2 = snap
            if self._on_l2:
                self._on_l2(snap)
        except Exception as exc:
            logger.debug("AlpacaFeed quote parse error: %s", exc)
