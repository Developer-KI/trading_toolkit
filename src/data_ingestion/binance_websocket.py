import asyncio
import json
import websockets
import os
import time
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import argparse
import requests
import glob
from pathlib import Path

###################################################################################
# Binance (Spot & Futures) Websocket Scraper
# Captures AggTrades, Order Book Depth (L2), and Funding Rates
###################################################################################

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "cleaned"

FUNDING_SCHEMA = pa.schema(
    [
        ("timestamp", pa.int64()),
        ("funding_rate", pa.float64()),
        ("mark_price", pa.float64()),
        ("oracle_price", pa.float64()),
    ]
)


def validate_symbol(coin: str, market: str) -> bool:
    """Validates if the symbol exists on Binance via a quick REST API check."""
    coin = coin.upper()
    print(f"🔍 Validating symbol '{coin.upper()}' on Binance {market.upper()}...")

    url = (
        f"https://api.binance.com/api/v3/ticker/price?symbol={coin}"
        if market == "spot"
        else f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={coin}"
    )

    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        print(f"✅ Symbol '{coin}' is valid!")
        return True
    except requests.exceptions.HTTPError as e:
        print(f"⚠️ API issue: {e}. Proceeding anyway...")
        return True
    except Exception as e:
        print(f"⚠️ Could not validate due to network issue: {e}. Proceeding anyway...")
        return True


def save_parquet_chunk(data_list, folder_path, stream_type):
    if not data_list:
        return
    try:
        df = pd.DataFrame(data_list)
        table = pa.Table.from_pandas(df)
        os.makedirs(folder_path, exist_ok=True)
        file_name = f"{int(time.time() * 1000)}.parquet"
        file_path = os.path.join(folder_path, file_name)
        pq.write_table(table, file_path)
        print(f"💾[DISK IO] Saved {len(data_list)} {stream_type} rows to {file_path}")
    except Exception as e:
        print(f"❌ Failed to save {stream_type} data: {e}")


async def scrape_binance(coin: str, streams: list, market: str, depth: int):
    market_label = f"BINANCE_{market.upper()}"

    if market == "spot" and "funding" in streams:
        print("⚠️ Notice: Spot market does not have funding rates. Removing 'funding'.")
        streams.remove("funding")
    if "funding" in streams:
        backfill_funding(coin.upper())

    buffers = {s: [] for s in streams}
    backoff = 1
    coin_lower = coin.lower()

    BUFFER_LIMITS = {"trades": 800, "l2": 2000, "funding": 100}
    BUFFER_TIME_LIMIT_SEC = 900
    last_flush_time = time.time()

    # Determine L2 stream name based on depth argument
    # Binance supports 5, 10, or 20 for partial book depths
    if depth == 1:
        l2_stream_name = f"{coin_lower}@bookTicker"
        l2_folder_name = "l2"  # Save in old folder
    else:
        # Defaults to nearest valid Binance tier (5, 10, or 20)
        valid_depth = min([5, 10, 20], key=lambda x: abs(x - depth))
        l2_stream_name = f"{coin_lower}@depth{valid_depth}@100ms"
        l2_folder_name = "l2"  # Save to NEW folder to avoid schema conflicts

    stream_map = {
        "trades": f"{coin_lower}@aggTrade",
        "l2": l2_stream_name,
        "funding": f"{coin_lower}@markPrice@1s",
    }

    subscription_params = [stream_map[s] for s in streams]
    stream_path = "/".join(subscription_params)

    ws_url = (
        f"wss://stream.binance.com:9443/stream?streams={stream_path}"
        if market == "spot"
        else f"wss://fstream.binance.com/stream?streams={stream_path}"
    )

    try:
        while True:
            try:
                async with websockets.connect(ws_url) as ws:
                    print(
                        f"🟢 Connected to Binance {market.upper()}: {', '.join(streams)} (Depth: {depth})"
                    )
                    backoff = 1

                    async for message in ws:
                        raw_data = json.loads(message)
                        if "data" not in raw_data:
                            continue

                        data = raw_data["data"]
                        event_type = data.get("e")

                        # --- 1. AGGREGATED TRADES ---
                        if event_type == "aggTrade" and "trades" in streams:
                            buffers["trades"].append(
                                {
                                    "timestamp": data["E"],
                                    "price": float(data["p"]),
                                    "size": float(data["q"]),
                                    "side": "S" if data["m"] else "B",
                                    "trade_id": data["a"],
                                }
                            )

                        # --- 2. ORDER BOOK (L2) ---
                        elif "l2" in streams:
                            row = None

                            # Case A: Depth == 1 (bookTicker payload format)
                            if (
                                depth == 1
                                and "u" in data
                                and "b" in data
                                and "a" in data
                                and "e" not in data
                            ):
                                row = {
                                    "timestamp": int(time.time() * 1000),
                                    "bids_px": [float(data["b"])],
                                    "bids_sz": [float(data["B"])],
                                    "asks_px": [float(data["a"])],
                                    "asks_sz": [float(data["A"])],
                                    "update_id": data.get("u"),
                                }

                            # Case B: Depth > 1 (Partial Book Depth payload format)
                            elif depth > 1 and ("bids" in data or "b" in data):
                                raw_bids = data.get("bids", data.get("b", []))
                                raw_asks = data.get("asks", data.get("a", []))

                                # Spot uses 'lastUpdateId', Futures uses 'u'
                                update_id = data.get("u", data.get("lastUpdateId"))

                                row = {
                                    "timestamp": data.get("E", int(time.time() * 1000)),
                                    "bids_px": [float(lvl[0]) for lvl in raw_bids],
                                    "bids_sz": [float(lvl[1]) for lvl in raw_bids],
                                    "asks_px": [float(lvl[0]) for lvl in raw_asks],
                                    "asks_sz": [float(lvl[1]) for lvl in raw_asks],
                                    "update_id": update_id,
                                }

                            if row:
                                buffers["l2"].append(row)

                        # --- 3. MARK PRICE & FUNDING ---
                        elif event_type == "markPriceUpdate" and "funding" in streams:
                            buffers["funding"].append(
                                {
                                    "timestamp": data["E"],
                                    "funding_rate": float(data["r"]),
                                    "mark_price": float(data["p"]),
                                    "oracle_price": float(data["i"]),
                                }
                            )

                        # --- BUFFER FLUSH CHECK ---
                        time_since_flush = time.time() - last_flush_time
                        for stream in streams:
                            if len(buffers[stream]) >= BUFFER_LIMITS.get(
                                stream, 1000
                            ) or (
                                time_since_flush >= BUFFER_TIME_LIMIT_SEC
                                and len(buffers[stream]) > 0
                            ):
                                data_to_save = buffers[stream].copy()
                                buffers[stream].clear()

                                # Use "l2" folder for depth > 1 to avoid Parquet schema crashes
                                folder_name = (
                                    l2_folder_name if stream == "l2" else stream
                                )
                                target_path = os.path.join(
                                    DATA_DIR, folder_name, market_label, coin.upper()
                                )

                                await asyncio.to_thread(
                                    save_parquet_chunk,
                                    data_to_save,
                                    target_path,
                                    stream,
                                )

                        if time_since_flush >= BUFFER_TIME_LIMIT_SEC:
                            last_flush_time = time.time()

            except websockets.exceptions.ConnectionClosed:
                print("⚠️ Binance Websocket closed. Reconnecting...")
            except Exception as e:
                print(f"❌ Connection lost: {e}. Retrying in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    except asyncio.CancelledError:
        pass
    finally:
        print(
            f"\n🛑 Shutting down. Flushing remaining {coin.upper()} buffers to disk..."
        )
        for stream in streams:
            if buffers[stream]:
                folder_name = l2_folder_name if stream == "l2" else stream
                target_path = os.path.join(
                    DATA_DIR, folder_name, market_label, coin.upper()
                )
                save_parquet_chunk(buffers[stream], target_path, stream)


def backfill_funding(coin: str) -> None:
    folder_path = os.path.join(DATA_DIR, "funding", "BINANCE_FUTURES", f"{coin}")
    os.makedirs(folder_path, exist_ok=True)

    # 1. Load ONLY timestamps (Memory Efficient)
    all_files = glob.glob(os.path.join(folder_path, "*.parquet"))
    existing_timestamps = pd.Series(dtype="int64")

    if all_files:
        existing_df = pd.read_parquet(folder_path, columns=["timestamp"])
        existing_timestamps = existing_df["timestamp"]

        # Check for gaps > 1 hour
        sorted_ts = existing_timestamps.sort_values()
        has_gap = (sorted_ts.diff() > 3600000).any()
    else:
        has_gap = True

    if not has_gap:
        print(f"✅ {coin} is healthy.")
        return

    # 2. Paginated Fetch
    all_api_rows = []
    # Start from 6 months ago (or 0 if you really want everything the API has)
    current_start_time = int(
        (pd.Timestamp.now() - pd.DateOffset(months=6)).timestamp() * 1000
    )

    print(f"🔍 Fetching {coin} history...")

    while True:
        time.sleep(0.1)
        try:
            url = "https://fapi.binance.com/fapi/v1/fundingRate"
            params = {"symbol": coin, "startTime": current_start_time, "limit": 1000}

            resp = requests.get(url, params=params)
            resp.raise_for_status()
            api_data = resp.json()

            if not api_data:
                break

            batch_df = pd.DataFrame(api_data)[
                ["fundingTime", "fundingRate", "markPrice"]
            ]
            batch_df = batch_df.rename(
                columns={
                    "fundingTime": "timestamp",
                    "fundingRate": "funding_rate",
                    "markPrice": "mark_price",
                }
            )

            batch_df["timestamp"] = pd.to_numeric(batch_df["timestamp"]).astype("int64")
            batch_df["funding_rate"] = pd.to_numeric(batch_df["funding_rate"]).astype(
                "float64"
            )
            batch_df["mark_price"] = pd.to_numeric(batch_df["mark_price"]).astype(
                "float64"
            )
            batch_df["oracle_price"] = None
            batch_df = batch_df[
                ["timestamp", "funding_rate", "mark_price", "oracle_price"]
            ]

            all_api_rows.append(batch_df)

            # Pagination logic: set next startTime to 1ms after the max timestamp received
            last_ts = int(batch_df["timestamp"].max())

            # Safety break: if the API stops moving forward, exit
            if last_ts <= current_start_time:
                break

            current_start_time = last_ts + 1

            # Stop if we've reached very close to 'now'
            if current_start_time > int(pd.Timestamp.now().timestamp() * 1000):
                break

        except Exception as e:
            print(f"❌ API Failure during pagination: {e}")
            break

    if not all_api_rows:
        return

    # 3. Combine and Filter
    full_api_df = pd.concat(all_api_rows, ignore_index=True)
    new_data = full_api_df[~full_api_df["timestamp"].isin(existing_timestamps)]

    if new_data.empty:
        print(f"ℹ️ {coin} API had data, but nothing new to add.")
        return

    # 4. Atomic Dump
    new_ts = new_data["timestamp"].min()
    save_path = os.path.join(folder_path, f"backfill_{new_ts}.parquet")
    temp_path = save_path + ".tmp"

    try:
        table = pa.Table.from_pandas(new_data, schema=FUNDING_SCHEMA)
        pq.write_table(table, temp_path)
        os.replace(temp_path, save_path)
        print(f"{coin} backfilled: Added {len(new_data)} rows to {save_path}")
    except Exception as e:
        print(f"❌ Write failed: {e}")
        if os.path.exists(temp_path):
            os.remove(temp_path)


async def main():
    parser = argparse.ArgumentParser(description="Binance Spot & Futures WS Scraper")
    parser.add_argument(
        "--coin", type=str, required=True, help="Ticker (e.g., BTCUSDT)"
    )
    parser.add_argument(
        "--market", type=str, choices=["futures", "spot"], default="futures"
    )
    parser.add_argument(
        "--streams",
        type=str,
        nargs="+",
        choices=["trades", "l2", "funding"],
        default=["trades", "l2", "funding"],
    )

    parser.add_argument(
        "--depth",
        type=int,
        choices=[1, 5, 10, 20],
        default=20,
        help="Order book depth (1=Top of book, 5/10/20 for deeper levels)",
    )

    args = parser.parse_args()

    if not validate_symbol(args.coin, args.market):
        return

    try:
        task = asyncio.create_task(
            scrape_binance(args.coin, args.streams, args.market, args.depth)
        )
        await task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
