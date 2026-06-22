import asyncio
import json
import websockets
import os
import time
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import argparse
from pathlib import Path

##############################################################################
# Binance Global Liquidations WebSocket Scraper
# Captures EVERY liquidation across ALL USD-M Futures pairs in real-time.
##############################################################################

# The special endpoint for all market liquidations
URL = "wss://fstream.binance.com/ws/!forceOrder@arr"

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "cleaned" / "BINANCE_GLOBAL_LIQUIDATIONS"


def save_parquet_chunk(data_list, folder_path):
    if not data_list:
        return
    df = pd.DataFrame(data_list)
    table = pa.Table.from_pandas(df)
    os.makedirs(folder_path, exist_ok=True)

    file_name = f"liquidations_{int(time.time() * 1000)}.parquet"
    file_path = os.path.join(folder_path, file_name)
    pq.write_table(table, file_path)
    print(f"💾 [DISK IO] Saved {len(data_list)} liquidations to {file_path}")


async def scrape_global_liquidations(min_print_value_usd: float):
    buffer = []
    backoff = 1

    BUFFER_LIMIT = 500  # Flush after 500 liquidations
    BUFFER_TIME_LIMIT_SEC = 300  # Or flush every 5 minutes
    last_flush_time = time.time()

    print(f"📂 Saving data to: {DATA_DIR}")
    print(
        f"Console will only print liquidations > ${min_print_value_usd:,.0f} to avoid spam."
    )

    try:
        while True:
            try:
                async with websockets.connect(URL) as ws:
                    print(
                        "🟢 Connected to Binance Global Liquidation (!forceOrder@arr)"
                    )
                    backoff = 1

                    async for message in ws:
                        data = json.loads(message)

                        # Verify it's a forceOrder event
                        if data.get("e") == "forceOrder":
                            order = data.get("o", {})

                            symbol = order.get("s")
                            side = order.get(
                                "S"
                            )  # "SELL" means a Long was liquidated, "BUY" means a Short
                            price = float(order.get("p", 0))
                            qty = float(order.get("q", 0))
                            timestamp = data.get("E")

                            usd_value = price * qty

                            row = {
                                "timestamp": timestamp,
                                "symbol": symbol,
                                "side": side,
                                "price": price,
                                "quantity": qty,
                                "usd_value": usd_value,
                            }

                            buffer.append(row)

                            # --- CONSOLE PRINTING (Filtered by USD Value) ---
                            if usd_value >= min_print_value_usd:
                                # If the forced order is a SELL, it means a LONG position got wiped out (Price dropped)
                                if side == "SELL":
                                    trade_type = "LONG Liquidated"
                                # If the forced order is a BUY, it means a SHORT position got wiped out (Price pumped)
                                else:
                                    trade_type = "SHORT Liquidated"

                                dt = pd.to_datetime(timestamp, unit="ms")
                                print(
                                    f"[{dt}] {symbol} {trade_type} | ${usd_value:,.2f} | Px: {price}"
                                )

                        # --- BUFFER FLUSH CHECK ---
                        time_since_flush = time.time() - last_flush_time

                        if len(buffer) >= BUFFER_LIMIT or (
                            time_since_flush >= BUFFER_TIME_LIMIT_SEC
                            and len(buffer) > 0
                        ):
                            data_to_save = buffer.copy()
                            buffer.clear()
                            last_flush_time = time.time()
                            await asyncio.to_thread(
                                save_parquet_chunk, data_to_save, DATA_DIR
                            )

            except websockets.exceptions.ConnectionClosed:
                print("⚠️ Binance Websocket closed. Reconnecting...")
            except Exception as e:
                print(f"❌ Connection lost: {e}. Retrying in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    except asyncio.CancelledError:
        print(
            f"\n🛑 Shutting down Liquidation scraper. Flushing {len(buffer)} remaining items to disk..."
        )
        if buffer:
            save_parquet_chunk(buffer, DATA_DIR)
        raise


async def main():
    parser = argparse.ArgumentParser(description="Binance Global Liquidations Scraper")
    # Add an argument to filter what gets printed to the console so it doesn't scroll too fast
    parser.add_argument(
        "--min_usd",
        type=float,
        default=10000.0,
        help="Minimum USD value of liquidation to print to console (default: $10,000)",
    )
    args = parser.parse_args()

    try:
        await scrape_global_liquidations(args.min_usd)
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 KeyboardInterrupt received. Exiting safely...")
