"""
execution/factory.py — Create exchange-specific components by name.

The engine calls:
    executor = create_executor(cred)
    feed     = create_feed(exchange_name, symbol, testnet, ...)

To add a new exchange, just add an elif branch here (and write the
executor + feed modules).
"""

from __future__ import annotations

import logging

from .base_executor_feed import BaseExecutor, BaseFeed, BaseBarBuilder
from abstract.models import ExchangeCredentials

logger = logging.getLogger(__name__)


def create_executor(cred: ExchangeCredentials) -> BaseExecutor:
    """
    Instantiate the right executor for an exchange.

    Args:
        cred: credentials block with exchange name + auth details.
    """
    name = cred.exchange.lower()

    if name == "hyperliquid":
        from .hyperliquid.hyperliquid_executor import HyperliquidExecutor

        api_url = (
            "https://api.hyperliquid-testnet.xyz"
            if cred.testnet
            else "https://api.hyperliquid.xyz"
        )
        return HyperliquidExecutor(
            account_address=cred.account_address,
            secret_key=cred.secret_key,
            api_url=api_url,
        )

    elif name == "binance":
        from .binance.binance_executor import BinanceExecutor

        return BinanceExecutor(
            api_key=cred.api_key,
            api_secret=cred.api_secret,
            testnet=cred.testnet,
            symbol_map=cred.symbol_map or None,
        )

    else:
        raise ValueError(
            f"Unknown exchange '{name}'. "
            f"Supported: hyperliquid, binance"
        )


def create_feed(
    exchange: str,
    symbol: str,
    testnet: bool = True,
    symbol_map: dict[str, str] | None = None,
    **kwargs,
) -> BaseFeed:
    """
    Instantiate the right WebSocket feed for an exchange.
    """
    name = exchange.lower()

    if name == "hyperliquid":
        from .hyperliquid.hyperliquid_live_feed import HyperliquidFeed

        ws_url = (
            "wss://api.hyperliquid-testnet.xyz/ws"
            if testnet
            else "wss://api.hyperliquid.xyz/ws"
        )
        return HyperliquidFeed(ws_url=ws_url, symbol=symbol)

    elif name == "binance":
        from .binance.binance_live_feed import BinanceFeed

        binance_symbol = None
        if symbol_map and symbol in symbol_map:
            binance_symbol = symbol_map[symbol]
        return BinanceFeed(
            symbol=symbol,
            testnet=testnet,
            binance_symbol=binance_symbol,
        )

    else:
        raise ValueError(
            f"Unknown exchange '{name}'. "
            f"Supported: hyperliquid, binance"
        )


def create_bar_builder(
    interval_s: int = 60,
    max_bars: int = 2000,
    on_bar_close=None,
) -> BaseBarBuilder:
    """Create an exchange-agnostic bar builder."""
    return BaseBarBuilder(
        interval_s=interval_s,
        max_bars=max_bars,
        on_bar_close=on_bar_close,
    )