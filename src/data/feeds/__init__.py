"""
data/feeds/ — Live WebSocket data feeds.

  hyperliquid.py          Multi-stream HL perp/spot: trades, L2, funding, wallet fills
  hyperliquid_bridge.py   Live Arbitrum bridge flows via Alchemy WebSocket
  binance.py              Binance trades, L2 depth, funding + batch backfill
  binance_liquidations.py Binance global liquidation stream (!forceOrder@arr)
  base.py                 DataFeedProtocol definition
"""

from .base import DataFeedProtocol

__all__ = ["DataFeedProtocol"]
