"""
data/historical/ — Batch historical data downloaders.

  hyperliquid_bridge.py   Retroactive HL bridge deposit/withdrawal history (Arbiscan)
  hyperliquid_l2.py       Bulk download HL L2 tick data from S3 archive (LZ4)

Each loader returns pandas DataFrames or writes Parquet files.
"""
