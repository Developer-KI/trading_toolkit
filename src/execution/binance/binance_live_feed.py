"""
execution/binance_live_feed.py — Binance USD-M Futures WebSocket feed.

Implements BaseFeed using the binance-futures-connector WebSocket manager.
Subscribes to:
  • Trade stream  (@aggTrade)
  • Kline stream  (@kline_1m)
  • Partial book  (@depth10@100ms)

Converts all data to the same dict format that BaseBarBuilder expects.

Install:
    pip install binance-futures-connector websocket-client

Usage:
    feed = BinanceFeed(symbol="ETH", testnet=True)
    feed.start(
        on_trade=bar_builder.on_trade,
        on_candle=bar_builder.on_candle,
    )
"""

from __future__ import annotations

import orjson
import logging
import threading
import time
from typing import Callable

import pandas as pd
import websocket

from abstract.models import OrderBookLevel, OrderBookSnapshot
from ..base_executor_feed import BaseFeed

logger = logging.getLogger(__name__)


class BinanceFeed(BaseFeed):
    """
    Binance USD-M Futures WebSocket feed for one symbol.

    Symbol mapping: the framework uses short names ("ETH"), but Binance
    WebSocket streams use lowercase tickers ("ethusdt").  The feed handles
    this translation automatically.
    """

    # ── Binance WS endpoints ─────────────────────────────────────────────
    MAINNET_WS = "wss://fstream.binance.com/ws"
    TESTNET_WS = "wss://fstream.binancefuture.com/ws"

    def __init__(
        self,
        symbol: str,
        testnet: bool = True,
        binance_symbol: str | None = None,
    ):
        self.symbol = symbol
        self.testnet = testnet

        # Binance lowercase ticker (e.g. "ethusdt")
        if binance_symbol:
            self._bsym = binance_symbol.lower()
        else:
            self._bsym = f"{symbol.lower()}usdt"

        self._ws_url = self.TESTNET_WS if testnet else self.MAINNET_WS

        # Latest L2
        self._latest_l2: OrderBookSnapshot | None = None
        self._l2_lock = threading.Lock()

        # Callbacks
        self._on_trade: Callable | None = None
        self._on_candle: Callable | None = None
        self._on_l2: Callable | None = None

        # Connection
        self._ws: websocket.WebSocketApp | None = None
        self._ws_thread: threading.Thread | None = None
        self._connected = threading.Event()
        self._running = False

    # ── BaseFeed interface ───────────────────────────────────────────────

    @property
    def exchange_name(self) -> str:
        return "binance"

    def start(self, on_trade=None, on_candle=None, on_l2=None):
        self._on_trade = on_trade
        self._on_candle = on_candle
        self._on_l2 = on_l2
        self._running = True

        # Binance combined stream URL
        streams = [
            f"{self._bsym}@aggTrade",
            f"{self._bsym}@kline_1m",
            f"{self._bsym}@depth10@100ms",
        ]
        stream_path = "/".join(streams)
        url = f"{self._ws_url}/{stream_path}"

        self._ws = websocket.WebSocketApp(
            url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws_thread = threading.Thread(
            target=self._run_forever, daemon=True,
            name=f"bn-ws-{self.symbol}",
        )
        self._ws_thread.start()
        self._connected.wait(timeout=15)
        if not self._connected.is_set():
            raise ConnectionError(
                f"Binance WebSocket timed out for {self.symbol}"
            )
        logger.info("BinanceFeed connected for %s (%s)", self.symbol, self._bsym)

    def stop(self):
        self._running = False
        if self._ws:
            self._ws.close()

    @property
    def latest_l2(self) -> OrderBookSnapshot | None:
        with self._l2_lock:
            return self._latest_l2

    # ── WebSocket lifecycle ──────────────────────────────────────────────

    def _run_forever(self):
        while self._running:
            try:
                self._ws.run_forever(
                    ping_interval=20,
                    ping_timeout=10,
                    reconnect=5,
                )
            except Exception as e:
                logger.error("Binance WS error: %s — reconnecting in 5s", e)
                time.sleep(5)

    def _on_open(self, ws):
        self._connected.set()
        logger.debug("Binance WS opened for %s", self.symbol)

    def _on_message(self, ws, raw: str):
        try:
            msg = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return

        # Combined streams wrap data in {"stream": "...", "data": {...}}
        if "stream" in msg:
            stream = msg["stream"]
            data = msg["data"]
        else:
            # Single-stream fallback
            stream = msg.get("e", "")
            data = msg

        if "@aggTrade" in stream or data.get("e") == "aggTrade":
            self._handle_trade(data)
        elif "@kline" in stream or data.get("e") == "kline":
            self._handle_kline(data)
        elif "@depth" in stream or data.get("e") == "depthUpdate":
            self._handle_depth(data)

    def _on_error(self, ws, error):
        logger.warning("Binance WS error: %s", error)

    def _on_close(self, ws, close_status, close_msg):
        logger.info("Binance WS closed (%s): %s", close_status, close_msg)
        self._connected.clear()

    # ── Message handlers ─────────────────────────────────────────────────

    def _handle_trade(self, data: dict):
        """
        Parse aggTrade → {timestamp, price, size, side}

        Binance aggTrade fields:
            T: trade time (ms)
            p: price
            q: quantity
            m: is buyer the maker? (True = seller aggressor = SELL)
        """
        if not self._on_trade:
            return
        try:
            self._on_trade({
                "timestamp": pd.Timestamp(int(data["T"]), unit="ms"),
                "price": float(data["p"]),
                "size": float(data["q"]),
                "side": "S" if data.get("m", False) else "B",
            })
        except (KeyError, ValueError, TypeError) as e:
            logger.debug("Binance trade parse error: %s", e)

    def _handle_kline(self, data: dict):
        """
        Parse kline → candle dict.

        Binance kline wrapper: {"e": "kline", "k": {kline_data}}
        kline_data keys: t (open time), T (close time), o, h, l, c, v, x (is closed)
        """
        if not self._on_candle:
            return
        try:
            k = data.get("k", data)  # unwrap if nested
            self._on_candle({
                "open": float(k["o"]),
                "high": float(k["h"]),
                "low": float(k["l"]),
                "close": float(k["c"]),
                "volume": float(k["v"]),
                "timestamp": pd.Timestamp(int(k["t"]), unit="ms"),
                "close_time": pd.Timestamp(int(k["T"]), unit="ms"),
                "is_closed": k.get("x", False),
            })
        except (KeyError, ValueError, TypeError) as e:
            logger.debug("Binance kline parse error: %s", e)

    def _handle_depth(self, data: dict):
        """
        Parse partial book depth (depth10) → OrderBookSnapshot.

        Binance depth fields:
            b: [[price, qty], ...]  (bids)
            a: [[price, qty], ...]  (asks)
            T or E: timestamp
        """
        try:
            ts_ms = data.get("T", data.get("E", int(time.time() * 1000)))
            ts = pd.Timestamp(int(ts_ms), unit="ms")

            bids = [
                OrderBookLevel(price=float(lvl[0]), size=float(lvl[1]))
                for lvl in data.get("b", data.get("bids", []))
            ]
            asks = [
                OrderBookLevel(price=float(lvl[0]), size=float(lvl[1]))
                for lvl in data.get("a", data.get("asks", []))
            ]

            snap = OrderBookSnapshot(timestamp=ts, bids=bids, asks=asks)
            with self._l2_lock:
                self._latest_l2 = snap

            if self._on_l2:
                self._on_l2(snap)

        except (KeyError, ValueError, TypeError) as e:
            logger.debug("Binance depth parse error: %s", e)
