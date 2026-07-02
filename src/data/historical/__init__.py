"""
data/historical/ — Batch historical data downloaders (one module per exchange).

  alpaca.py      OHLCV bars via alpaca-py StockHistoricalDataClient
  binance.py     OHLCV klines via Binance REST API (spot + futures)
  hyperliquid.py L2 tick data from S3 archive; OHLCV via REST candleSnapshot

Each loader is CLI-runnable and writes Parquet to data/cleaned/historical/{exchange}/.
"""
