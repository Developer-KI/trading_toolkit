"""
data/auxiliary/ — Miscellaneous one-off data sources.

Moved from data_ingestion/:
  bid_ask_scrape.py            → data/auxiliary/bid_ask.py
  misc_crypto_scraper.py       → data/auxiliary/crypto.py
  github_fetch.py              → data/auxiliary/github.py
  binance_global_liquidations.py → data/auxiliary/liquidations.py
  myapi.py                     → data/auxiliary/myapi.py

These should all implement DataFeedProtocol from data/feeds/base.py
when used as live sources, or return DataFrames when used as batch loaders.
"""
