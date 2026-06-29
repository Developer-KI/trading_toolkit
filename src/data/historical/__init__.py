"""
data/historical/ — Historical OHLCV and funding rate loaders.

Moved from data_ingestion/:
  hype_bridge_historical.py  → data/historical/hyperliquid.py
  binance_websocket.py       → data/historical/binance.py (batch download)

Each loader returns pandas DataFrames or writes Parquet files.
"""
