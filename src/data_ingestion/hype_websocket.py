import asyncio
import json
import websockets
import os
import time
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import argparse
from collections import deque
import sys
import requests
import glob
from pathlib import Path

##############################################################################
# https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket
##############################################################################

URL_WS = "wss://api.hyperliquid.xyz/ws"
URL_POST = "https://api.hyperliquid.xyz/info"

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


def validate_coin(coin):
    display_name = coin.upper()
    lookup_name = display_name
    if "USDT" in lookup_name and "USDT0" not in lookup_name:
        lookup_name = lookup_name.replace("USDT", "USDT0")

    print(f"🔍 Validating '{display_name}'...")

    try:
        # --- PERP CHECK ---
        perp_res = requests.post(URL_POST, json={"type": "meta"}).json()
        perp_coins = [c["name"] for c in perp_res.get("universe", [])]

        coin_suffix = lookup_name.split(":")[1] if ":" in lookup_name else lookup_name
        if lookup_name in perp_coins or coin_suffix in perp_coins:
            ws_name = coin_suffix if coin_suffix in perp_coins else lookup_name
            return "perp", ws_name, lookup_name

        # --- SPOT CHECK ---
        spot_res = requests.post(URL_POST, json={"type": "spotMeta"}).json()
        tokens = spot_res.get("tokens", [])
        universe = spot_res.get("universe", [])

        for idx, asset in enumerate(universe):
            base_name = tokens[asset["tokens"][0]]["name"]
            quote_name = tokens[asset["tokens"][1]]["name"]
            full_pair = f"{base_name}/{quote_name}"

            if lookup_name in [full_pair.upper(), base_name.upper()]:
                if base_name == "PURR":
                    return "spot", "PURR/USDC", "PURR"
                ws_name = f"@{idx}"
                return "spot", ws_name, base_name.upper()

        print(f"❌ Error: '{display_name}' not found.")
        sys.exit(1)
    except Exception as e:
        print(f"⚠️ Validation fallback: {e}")
        sys.exit(1)


def save_parquet_chunk(data_list, folder_path, stream_type):
    if not data_list:
        return
    df = pd.DataFrame(data_list)
    table = pa.Table.from_pandas(df)

    os.makedirs(folder_path, exist_ok=True)
    file_name = f"{int(time.time() * 1000)}.parquet"
    file_path = os.path.join(folder_path, file_name)

    pq.write_table(table, file_path)
    print(f"💾 [DISK IO] Saved {len(data_list)} {stream_type} rows to {file_path}")


async def keepalive_ping(ws):
    try:
        while True:
            await asyncio.sleep(40)
            try:
                await ws.send(json.dumps({"method": "ping"}))
            except websockets.exceptions.ConnectionClosed:
                break
    except asyncio.CancelledError:
        pass


async def scrape_data(coin, mode, coin_type, coin_display, depth=1, show=True):
    buffer = []
    last_state = None
    backoff = 1
    subscription_type = None
    last_funding_rate = None
    recent_trades = deque(maxlen=500)

    safe_coin = (
        coin_display.replace(":", "_").replace("/", "_").replace("@", "").upper()
    )
    exchange_folder = (
        "HYPERLIQUID_SPOT" if coin_type == "spot" else "HYPERLIQUID_PERPETUALS"
    )
    if safe_coin != "USDT0":
        target_path = os.path.join(DATA_DIR, mode, exchange_folder, safe_coin)
    else:
        target_path = os.path.join(DATA_DIR, mode, exchange_folder, "USDT_USDC")
    if mode == "l2":
        BUFFER_LIMIT = 1800
        subscription_type = "l2Book"
    elif mode == "trades":
        BUFFER_LIMIT = 50
        subscription_type = "trades"
    elif mode == "funding":
        BUFFER_LIMIT = 500
        subscription_type = "activeAssetCtx"
        if os.path.exists(target_path):
            backfill_funding(safe_coin)
    else:
        raise ValueError("Select valid scraping mode")

    BUFFER_TIME_LIMIT_SEC = 900
    last_flush_time = time.time()

    try:
        while True:
            try:
                async with websockets.connect(URL_WS) as ws:
                    print(
                        f"🟢 Connected: {mode.upper()} for {coin_display} "
                        + (f"(Depth: {depth})" if mode == "l2" else "")
                    )
                    backoff = 1
                    ping_task = asyncio.create_task(keepalive_ping(ws))

                    subscribe_msg = {
                        "method": "subscribe",
                        "subscription": {"type": subscription_type, "coin": coin},
                    }
                    await ws.send(json.dumps(subscribe_msg))

                    try:
                        async for message in ws:
                            data = json.loads(message)
                            if (
                                data.get("channel") == "pong"
                                or data.get("channel") == "subscriptionResponse"
                            ):
                                continue
                            if "data" not in data:
                                continue

                            payload = data["data"]

                            # --- MODE: L2 BOOK ---
                            if mode == "l2":
                                # Slicing the lists to the specified depth
                                bids = payload["levels"][0][:depth]
                                asks = payload["levels"][1][:depth]

                                if not bids or not asks:
                                    continue

                                bids_px = [float(b["px"]) for b in bids]
                                bids_sz = [float(b["sz"]) for b in bids]
                                asks_px = [float(a["px"]) for a in asks]
                                asks_sz = [float(a["sz"]) for a in asks]

                                current_row = {
                                    "timestamp": payload["time"],
                                    "bids_px": bids_px,
                                    "bids_sz": bids_sz,
                                    "asks_px": asks_px,
                                    "asks_sz": asks_sz,
                                }

                                # Convert lists to tuples to compare states (lists aren't hashable)
                                current_state = (
                                    tuple(bids_px),
                                    tuple(bids_sz),
                                    tuple(asks_px),
                                    tuple(asks_sz),
                                )

                                if current_state != last_state:
                                    buffer.append(current_row)
                                    last_state = current_state
                                    if show:
                                        print(
                                            f"📖 [{pd.to_datetime(current_row['timestamp'], unit='ms')}] {coin_display} L2 | B: {bids_px[0]} | A: {asks_px[0]}"
                                        )

                            # --- MODE: TRADES ---
                            elif mode == "trades":
                                for t in payload:
                                    if t["hash"] in recent_trades:
                                        continue
                                    recent_trades.append(t["hash"])
                                    trade_row = {
                                        "timestamp": t["time"],
                                        "price": float(t["px"]),
                                        "size": float(t["sz"]),
                                        "side": t["side"],
                                        "hash": t["hash"],
                                    }
                                    buffer.append(trade_row)
                                    if show:
                                        side_color = "🟢" if t["side"] == "B" else "🔴"
                                        print(
                                            f"[{pd.to_datetime(trade_row['timestamp'], unit='ms')}] {side_color} Trade | {t['px']} | {t['sz']}"
                                        )

                            # --- MODE: FUNDING ---
                            elif (
                                mode == "funding"
                                and data.get("channel") == "activeAssetCtx"
                            ):
                                ctx = payload.get("ctx", {})
                                current_funding = float(ctx.get("funding", 0.0))

                                if current_funding != last_funding_rate:
                                    funding_row = {
                                        "timestamp": int(time.time() * 1000),
                                        "funding_rate": current_funding,
                                        "mark_price": float(ctx.get("markPx", 0.0)),
                                        "oracle_price": float(ctx.get("oraclePx", 0.0)),
                                    }
                                    buffer.append(funding_row)
                                    last_funding_rate = current_funding

                            # --- BUFFER CHECK ---
                            time_since_flush = time.time() - last_flush_time
                            if len(buffer) >= BUFFER_LIMIT or (
                                time_since_flush >= BUFFER_TIME_LIMIT_SEC
                                and len(buffer) > 0
                            ):
                                data_to_save = buffer.copy()
                                buffer.clear()
                                last_flush_time = time.time()
                                await asyncio.to_thread(
                                    save_parquet_chunk, data_to_save, target_path, mode
                                )

                    finally:
                        ping_task.cancel()

            except websockets.exceptions.ConnectionClosed:
                print(f"⚠️ Websocket closed ({mode}). Reconnecting...")
            except Exception as e:
                print(f"❌ Connection lost ({mode}): {e}. Retrying in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    except asyncio.CancelledError:
        print(f"\n🛑 Shutting down {mode} scraper. Flushing items...")
        if buffer:
            save_parquet_chunk(buffer, target_path, mode)
        raise


def save_parquet_wallet_chunk(data, folder_path):
    """Saves the flushed buffer to a partitioned parquet file."""
    if not data:
        return

    os.makedirs(folder_path, exist_ok=True)
    df = pd.DataFrame(data)

    # Generate a filename based on the earliest timestamp in the chunk
    min_ts = int(df["timestamp"].min())

    file_name = f"{min_ts}.parquet"
    file_path = os.path.join(folder_path, file_name)

    df.to_parquet(file_path, index=False, engine="pyarrow")

    print(f"💾 [DISK IO] Saved {len(data)} rows to {file_path}")


async def scrape_wallet(wallet: str, show=True):
    # Separate buffers because the schemas for fills and orders differ significantly
    fills_buffer = []
    orders_buffer = []
    backoff = 1

    # Keep track of recent IDs to prevent saving duplicates on reconnects/snapshots
    recent_fills = deque(maxlen=500)
    recent_orders = deque(maxlen=500)

    fills_folder = os.path.join(DATA_DIR, "HYPERLIQUID_fills", wallet)
    orders_folder = os.path.join(DATA_DIR, "HYPERLIQUID_orders", wallet)

    BUFFER_LIMIT = 1000
    BUFFER_TIME_LIMIT_SEC = 1800
    last_flush_time = time.time()

    try:
        while True:
            try:
                async with websockets.connect(URL_WS) as ws:
                    print(f"🟢 Connected: Streaming Wallet Data for {wallet}")
                    backoff = 1

                    ping_task = asyncio.create_task(keepalive_ping(ws))

                    # Subscribing to both streams on a single multiplexed connection
                    # Note: "userOrders" was changed to "orderUpdates" (the official Hyperliquid WS topic)
                    subs = [
                        {"type": "userFills", "user": wallet},
                        {"type": "orderUpdates", "user": wallet},
                    ]

                    for sub in subs:
                        msg = {
                            "method": "subscribe",
                            "subscription": sub,
                        }
                        await ws.send(json.dumps(msg))

                    try:
                        async for message in ws:
                            data = json.loads(message)

                            # Ignore ping responses
                            if data.get("channel") == "pong":
                                continue

                            if data.get("channel") == "subscriptionResponse":
                                sub_type = (
                                    data.get("data", {})
                                    .get("subscription", {})
                                    .get("type")
                                )
                                print(
                                    f"✅ Subscription Confirmed: {sub_type} for {wallet}"
                                )
                                continue

                            if "data" not in data:
                                continue

                            channel = data["channel"]
                            payload = data["data"]

                            # --- MODE: FILLS ---
                            if channel == "userFills":
                                # Payload format: {"isSnapshot": bool, "user": str, "fills": [...]}
                                fills = payload.get("fills", [])
                                for f in fills:
                                    if f["hash"] in recent_fills:
                                        continue

                                    recent_fills.append(f["hash"])

                                    fill_row = {
                                        "timestamp": f["time"],
                                        "coin": f["coin"],
                                        "price": float(f["px"]),
                                        "size": float(f["sz"]),
                                        "side": f["side"],
                                        "dir": f["dir"],
                                        "closed_pnl": float(f.get("closedPnl", 0.0)),
                                        "hash": f["hash"],
                                        "oid": f["oid"],
                                        "crossed": f.get("crossed", False),
                                        "fee": float(f.get("fee", 0.0)),
                                    }
                                    fills_buffer.append(fill_row)

                                    side_color = "🟢" if f["side"] == "B" else "🔴"
                                    dt = pd.to_datetime(f["time"], unit="ms")
                                    if show:
                                        print(
                                            f"[{dt}] {side_color} Fill | {f['coin']} | Sz: {f['sz']} @ {f['px']} (PnL: {f.get('closedPnl')})"
                                        )

                            # --- MODE: ORDER UPDATES ---
                            elif channel == "orderUpdates":
                                # Payload format is a list of WsOrder objects
                                for o in payload:
                                    order_info = o.get("order", {})
                                    oid = order_info.get("oid")
                                    status = o.get("status")
                                    status_ts = o.get("statusTimestamp")

                                    # Unique combination key to store state lifecycle changes
                                    dedup_key = f"{oid}_{status}_{status_ts}"
                                    if dedup_key in recent_orders:
                                        continue

                                    recent_orders.append(dedup_key)

                                    order_row = {
                                        "timestamp": status_ts,
                                        "oid": oid,
                                        "coin": order_info.get("coin"),
                                        "limit_px": float(
                                            order_info.get("limitPx", 0.0)
                                        )
                                        if "limitPx" in order_info
                                        else None,
                                        "size": float(order_info.get("sz", 0.0))
                                        if "sz" in order_info
                                        else None,
                                        "side": order_info.get("side"),
                                        "status": status,
                                        "order_type": order_info.get("orderType"),
                                        "reduce_only": order_info.get(
                                            "reduceOnly", False
                                        ),
                                    }
                                    orders_buffer.append(order_row)

                                    dt = pd.to_datetime(status_ts, unit="ms")
                                    if show:
                                        print(
                                            f"[{dt}] 📝 Order Update | {order_info.get('coin')} | {status} (OID: {oid})"
                                        )
                            time_since_flush = time.time() - last_flush_time
                            # --- BUFFER CHECKS ---
                            if len(fills_buffer) >= BUFFER_LIMIT or (
                                time_since_flush >= BUFFER_TIME_LIMIT_SEC
                                and len(fills_buffer) > 0
                            ):
                                data_to_save = fills_buffer.copy()
                                fills_buffer.clear()
                                last_flush_time = time.time()
                                await asyncio.to_thread(
                                    save_parquet_wallet_chunk,
                                    data_to_save,
                                    fills_folder,
                                )

                            if len(orders_buffer) >= BUFFER_LIMIT:
                                data_to_save = orders_buffer.copy()
                                orders_buffer.clear()
                                await asyncio.to_thread(
                                    save_parquet_wallet_chunk,
                                    data_to_save,
                                    orders_folder,
                                )

                    finally:
                        ping_task.cancel()

            except websockets.exceptions.ConnectionClosed:
                print("⚠️ Websocket closed. Reconnecting...")
            except Exception as e:
                print(f"❌ Connection lost: {e}. Retrying in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    except asyncio.CancelledError:
        print(
            f"\n🛑 Shutting down wallet scraper for {wallet}. Flushing items to disk..."
        )
        if fills_buffer:
            save_parquet_wallet_chunk(fills_buffer, fills_folder)
        if orders_buffer:
            save_parquet_wallet_chunk(orders_buffer, orders_folder)
        raise


def backfill_funding(coin: str) -> None:
    folder_path = os.path.join(DATA_DIR, "funding", "HYPERLIQUID_PERPETUALS", f"{coin}")
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
            # Note: Hyperliquid uses "startTime" (camelCase) in the payload
            payload = {
                "type": "fundingHistory",
                "coin": coin,
                "startTime": current_start_time,
            }
            resp = requests.post(URL_POST, json=payload)
            resp.raise_for_status()
            api_data = resp.json()

            if not api_data:
                break

            batch_df = pd.DataFrame(api_data)[["time", "fundingRate"]]
            batch_df = batch_df.rename(
                columns={"time": "timestamp", "fundingRate": "funding_rate"}
            )

            batch_df["timestamp"] = pd.to_numeric(batch_df["timestamp"]).astype("int64")
            batch_df["funding_rate"] = pd.to_numeric(batch_df["funding_rate"]).astype(
                "float64"
            )
            batch_df["mark_price"] = None
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
    parser = argparse.ArgumentParser(description="Hyperliquid Scraper")
    parser.add_argument(
        "--coin", type=str, default=None, help="Ticker (e.g., BTC, vntl:OPENAI)"
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["l2", "trades", "funding", "l2/trades", "all"],
        default="all",
    )

    # NEW ARGUMENT FOR DEPTH
    parser.add_argument(
        "--depth",
        type=int,
        default=1,
        help="Order book depth levels to track (e.g. 1 for Top-of-Book, 5, 20, etc.)",
    )

    parser.add_argument(
        "--wallet", type=str, default=None, help="Wallet address to track"
    )
    parser.add_argument("--no-show", dest="show", action="store_false")
    parser.set_defaults(show=True)

    args = parser.parse_args()
    tasks = []

    if args.coin:
        coin_type, resolved_coin, original_coin = validate_coin(args.coin)

        if args.mode == "all":
            active_modes = ["l2", "trades", "funding"]
        elif args.mode == "l2/trades":
            active_modes = ["l2", "trades"]
        else:
            active_modes = [args.mode]

        if coin_type == "spot" and "funding" in active_modes:
            active_modes.remove("funding")

        for m in active_modes:
            tasks.append(
                asyncio.create_task(
                    scrape_data(
                        coin=resolved_coin,
                        mode=m,
                        coin_type=coin_type,
                        coin_display=original_coin,
                        depth=args.depth,  # PASSING DEPTH HERE
                        show=args.show,
                    )
                )
            )

    if args.wallet:
        tasks.append(asyncio.create_task(scrape_wallet(args.wallet, show=args.show)))

    if not tasks:
        print("⚠️ No tasks to run! Please provide a --coin or a --wallet.")
        return

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 KeyboardInterrupt received. Exiting safely...")
