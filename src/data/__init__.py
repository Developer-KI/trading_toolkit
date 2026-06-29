"""
data/ — Unified data layer.

Organizes all data concerns under a single coherent module:

  feeds/       Live WebSocket feeds (implement DataFeedProtocol)
  historical/  REST/file-based historical OHLCV loaders
  sentiment/   Sentiment scrapers (4chan, Reddit, Telegram, X)
  auxiliary/   One-off scrapers and misc data sources

All live feed classes implement DataFeedProtocol from data/feeds/base.py,
making them interchangeable and independently testable.
"""
