"""
data/ — Unified data layer.

  feeds/       Live WebSocket feeds (BaseFeed — consumed by LiveEngine)
  historical/  Batch historical downloaders — one module per exchange:
                 alpaca.py, binance.py, hyperliquid.py
  auxiliary/   Supplementary non-price data, organised by category:
    market/      Market microstructure tools (spreads, funding, MM scoring)
    macro/       Macro REST pollers (OI, stablecoin supply, vol index)
    sentiment/   Social sentiment scrapers (4chan, Reddit, Telegram)
"""
