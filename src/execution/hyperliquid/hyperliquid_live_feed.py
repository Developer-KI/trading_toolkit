"""
execution/hyperliquid_live_feed.py — Hyperliquid WebSocket feed (refactored).

Now inherits from BaseFeed. The bar builder is imported from base_executor
(exchange-agnostic BaseBarBuilder).
"""

from __future__ import annotations

import orjson
import logging
import threading
import time
from typing import Callable

import pandas as pd
import websocket

from core.models import OrderBookLevel, OrderBookSnapshot
from ..base_executor_feed import BaseFeed, BaseBarBuilder

logger = logging.getLogger(__name__)

# Re-export the generic bar builder under the old name for backward compat
HyperliquidBarBuilder = BaseBarBuilder


class HyperliquidFeed(BaseFeed):
    """Hyperliquid WebSocket feed for one symbol."""

    def __init__(self, ws_url: str, symbol: str):
        self.ws_url = ws_url
        self.symbol = symbol

        self._latest_l2: OrderBookSnapshot | None = None
        self._l2_lock = threading.Lock()

        self._on_trade: Callable | None = None
        self._on_candle: Callable | None = None
        self._on_l2: Callable | None = None

        self._ws: websocket.WebSocketApp | None = None
        self._ws_thread: threading.Thread | None = None
        self._connected = threading.Event()
        self._running = False

    @property
    def exchange_name(self) -> str:
        return "hyperliquid"

    def start(self, on_trade=None, on_candle=None, on_l2=None):
        self._on_trade = on_trade
        self._on_candle = on_candle
        self._on_l2 = on_l2
        self._running = True

        self._ws = websocket.WebSocketApp(
            self.ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws_thread = threading.Thread(
            target=self._run_forever, daemon=True, name=f"hl-ws-{self.symbol}",
        )
        self._ws_thread.start()
        self._connected.wait(timeout=15)
        if not self._connected.is_set():
            raise ConnectionError("Hyperliquid WebSocket timed out")
        logger.info("HyperliquidFeed connected for %s", self.symbol)

    def stop(self):
        self._running = False
        if self._ws:
            self._ws.close()

    @property
    def latest_l2(self) -> OrderBookSnapshot | None:
        with self._l2_lock:
            return self._latest_l2

    # ── WebSocket internals ──────────────────────────────────────────────

    def _run_forever(self):
        while self._running:
            try:
                self._ws.run_forever(ping_interval=20, ping_timeout=10, reconnect=5)
            except Exception as e:
                logger.error("WS error: %s — reconnecting in 5s", e)
                time.sleep(5)

    def _on_open(self, ws):
        self._connected.set()
        subs = [
            {"type": "l2Book", "coin": self.symbol},
            {"type": "trades", "coin": self.symbol},
            {"type": "candle", "coin": self.symbol, "interval": "1m"},
        ]
        for sub in subs:
            ws.send(orjson.dumps({"method": "subscribe", "subscription": sub}))

    def _on_message(self, ws, raw: str):
        try:
            msg = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return
        channel = msg.get("channel", "")
        if channel == "l2Book":
            self._handle_l2(msg["data"])
        elif channel == "trades":
            self._handle_trades(msg["data"])
        elif channel == "candle":
            self._handle_candle(msg["data"])

    def _on_error(self, ws, error):
        logger.warning("WS error: %s", error)

    def _on_close(self, ws, close_status, close_msg):
        logger.info("WS closed (%s): %s", close_status, close_msg)
        self._connected.clear()

    def _handle_l2(self, data: dict):
        try:
            levels = data.get("levels", [[], []])
            ts = pd.Timestamp(data.get("time", time.time_ns() // 1_000_000), unit="ms")
            bids = [OrderBookLevel(price=float(l["px"]), size=float(l["sz"])) for l in levels[0]]
            asks = [OrderBookLevel(price=float(l["px"]), size=float(l["sz"])) for l in levels[1]]
            snap = OrderBookSnapshot(timestamp=ts, bids=bids, asks=asks)
            with self._l2_lock:
                self._latest_l2 = snap
            if self._on_l2:
                self._on_l2(snap)
        except (KeyError, ValueError, TypeError) as e:
            logger.debug("L2 parse error: %s", e)

    def _handle_trades(self, data: list):
        if not self._on_trade:
            return
        for trade_list in (data if isinstance(data[0], list) else [data]):
            if isinstance(trade_list, dict):
                trade_list = [trade_list]
            for t in trade_list:
                try:
                    self._on_trade({
                        "timestamp": pd.Timestamp(int(t["time"]), unit="ms"),
                        "price": float(t["px"]),
                        "size": float(t["sz"]),
                        "side": t.get("side", "B"),
                    })
                except (KeyError, ValueError, TypeError) as e:
                    logger.debug("Trade parse error: %s", e)

    def _handle_candle(self, data: dict):
        if not self._on_candle:
            return
        try:
            self._on_candle({
                "open": float(data["o"]),
                "high": float(data["h"]),
                "low": float(data["l"]),
                "close": float(data["c"]),
                "volume": float(data["v"]),
                "timestamp": pd.Timestamp(int(data["t"]), unit="ms"),
                "close_time": pd.Timestamp(int(data["T"]), unit="ms"),
                "is_closed": data.get("T", 0) <= time.time() * 1000,
            })
        except (KeyError, ValueError, TypeError) as e:
            logger.debug("Candle parse error: %s", e)
