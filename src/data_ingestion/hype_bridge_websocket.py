import asyncio
import json
import websockets
import os
from dotenv import load_dotenv
import time
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from datetime import datetime
import aiohttp
import argparse
from collections import deque
from pathlib import Path

# ---------------------------------------------------------
# CONSTANTS & CONFIGURATION
# ---------------------------------------------------------
load_dotenv()
KEY = os.getenv("ALCHEMY_ARBITRUM")

ALCHEMY_WS_URL = f"wss://arb-mainnet.g.alchemy.com/v2/{KEY}"
ALCHEMY_HTTP_URL = f"https://arb-mainnet.g.alchemy.com/v2/{KEY}"

USDC_ARBITRUM = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
HYPERLIQUID_BRIDGE = "0x2df1c51e09aecf9cacb7bc98cb1742757f163df7"
TRANSFER_EVENT_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)
PADDED_BRIDGE_ADDRESS = "0x000000000000000000000000" + HYPERLIQUID_BRIDGE.replace(
    "0x", ""
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "cleaned" / "bridge_flows"
STATE_FILE = os.path.join(DATA_DIR, "scraper_state.json")


# ---------------------------------------------------------
# STATE MANAGEMENT
# ---------------------------------------------------------
def load_last_block():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                return data.get("highest_block_seen_hex")
        except Exception as e:
            print(f"⚠️ Could not load state file: {e}")
    return None


def save_last_block(block_hex):
    if block_hex:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump({"highest_block_seen_hex": block_hex}, f)


# ---------------------------------------------------------
# PARQUET SAVING LOGIC
# ---------------------------------------------------------
def save_parquet_chunk(data_list, folder_path, block_hex):
    if not data_list:
        return

    df = pd.DataFrame(data_list)
    table = pa.Table.from_pandas(df)
    os.makedirs(folder_path, exist_ok=True)

    file_name = f"{int(time.time() * 1000)}.parquet"
    file_path = os.path.join(folder_path, file_name)

    pq.write_table(table, file_path)
    print(f"💾 [DISK IO] Saved {len(data_list)} flows to {file_path}")

    save_last_block(block_hex)


# ---------------------------------------------------------
# HTTP BACKFILL LOGIC
# ---------------------------------------------------------
async def get_latest_block_number(session):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
    async with session.post(ALCHEMY_HTTP_URL, json=payload) as resp:
        data = await resp.json()
        return int(data["result"], 16)


async def backfill_missed_blocks(
    last_seen_block_hex, seen_events, buffer, max_lookback_blocks=None
):
    headers = {"Content-Type": "application/json"}
    backfilled_count = 0
    CHUNK_SIZE = 50000  # Scan 50k blocks per API call
    last_successful_block = None

    try:
        async with aiohttp.ClientSession() as session:
            latest_block = await get_latest_block_number(session)

            # 1. Determine our start block based on state and CLI args
            if last_seen_block_hex:
                start_block = int(last_seen_block_hex, 16)
            else:
                if max_lookback_blocks is not None:
                    start_block = latest_block - max_lookback_blocks
                else:
                    return None  # No state and no lookback arg -> Nothing to backfill

            # 2. Clamp start_block if a maximum lookback was provided
            if max_lookback_blocks is not None:
                earliest_allowed = latest_block - max_lookback_blocks
                if start_block < earliest_allowed:
                    print(
                        f"⚠️ Saved block is too old. Clamping start block to {earliest_allowed} (Max Lookback Constraint)."
                    )
                    start_block = earliest_allowed

            if start_block >= latest_block:
                return hex(latest_block)

            print(
                f"🔄 Backfilling from block {start_block} to {latest_block} (Gap: {latest_block - start_block} blocks)..."
            )

            current_from = start_block
            last_successful_block = start_block

            while current_from <= latest_block:
                current_to = min(current_from + CHUNK_SIZE - 1, latest_block)
                print(f"   -> Fetching chunk: {current_from} to {current_to}")

                from_hex = hex(current_from)
                to_hex = hex(current_to)

                deposit_params = {
                    "fromBlock": from_hex,
                    "toBlock": to_hex,
                    "address": USDC_ARBITRUM,
                    "topics": [TRANSFER_EVENT_TOPIC, None, PADDED_BRIDGE_ADDRESS],
                }
                withdrawal_params = {
                    "fromBlock": from_hex,
                    "toBlock": to_hex,
                    "address": USDC_ARBITRUM,
                    "topics": [TRANSFER_EVENT_TOPIC, PADDED_BRIDGE_ADDRESS],
                }

                payloads = [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "eth_getLogs",
                        "params": [deposit_params],
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "eth_getLogs",
                        "params": [withdrawal_params],
                    },
                ]

                for payload in payloads:
                    async with session.post(
                        ALCHEMY_HTTP_URL, json=payload, headers=headers
                    ) as resp:
                        response = await resp.json()
                        if "result" not in response or not response["result"]:
                            continue

                        flow_type_base = (
                            "DEPOSIT" if payload["id"] == 1 else "WITHDRAWAL"
                        )

                        for log in response["result"]:
                            event_id = (
                                f"{log['transactionHash']}_{log.get('logIndex', '0x0')}"
                            )
                            if event_id in seen_events:
                                continue
                            seen_events.append(event_id)

                            is_removed = log.get("removed", False)
                            flow_type = (
                                f"REMOVED_{flow_type_base}"
                                if is_removed
                                else flow_type_base
                            )
                            usdc_amount = int(log["data"], 16) / 1_000_000
                            if is_removed:
                                usdc_amount = -usdc_amount

                            user_addr_topic = 1 if flow_type_base == "DEPOSIT" else 2
                            user_address = "0x" + log["topics"][user_addr_topic][-40:]

                            buffer.append(
                                {
                                    "local_timestamp": int(time.time()),
                                    "flow_type": flow_type,
                                    "user_address": user_address,
                                    "usdc_amount": usdc_amount,
                                    "tx_hash": log["transactionHash"],
                                    "log_index": int(log.get("logIndex", "0x0"), 16),
                                    "block_number": int(
                                        log.get("blockNumber", "0x0"), 16
                                    ),
                                }
                            )
                            backfilled_count += 1

                # Update tracker so if we crash on the next chunk, we don't start from the beginning
                last_successful_block = current_to
                current_from = current_to + 1

        if backfilled_count > 0:
            print(f"✅ Backfill complete. Recovered {backfilled_count} missed flows.")

        return hex(last_successful_block)

    except Exception as e:
        print(f"⚠️ Backfill failed midway: {e}")
        # Return the furthest we got before failing so the next loop resumes properly
        return (
            hex(last_successful_block)
            if "last_successful_block" in locals()
            else last_seen_block_hex
        )


# ---------------------------------------------------------
# MAIN SCRAPER FUNCTION
# ---------------------------------------------------------
async def scrape_bridge_flows(max_lookback_blocks=None, min_print_value_usd=10000):
    backoff = 1
    buffer = []
    seen_events = deque(maxlen=20000)

    highest_block_seen_hex = load_last_block()

    BUFFER_SIZE_LIMIT = 1000
    BUFFER_TIME_LIMIT_SEC = 1800
    last_flush_time = time.time()

    deposit_sub = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_subscribe",
        "params": [
            "logs",
            {
                "address": USDC_ARBITRUM,
                "topics": [TRANSFER_EVENT_TOPIC, None, PADDED_BRIDGE_ADDRESS],
            },
        ],
    }

    withdrawal_sub = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "eth_subscribe",
        "params": [
            "logs",
            {
                "address": USDC_ARBITRUM,
                "topics": [TRANSFER_EVENT_TOPIC, PADDED_BRIDGE_ADDRESS],
            },
        ],
    }

    try:
        while True:
            try:
                async with websockets.connect(ALCHEMY_WS_URL) as ws:
                    print("🟢 Connected to Alchemy Arbitrum Node")
                    print(
                        f"Console will only print liquidations > ${min_print_value_usd:,.0f} to avoid spam."
                    )
                    backoff = 1
                    sub_map = {}

                    await ws.send(json.dumps(deposit_sub))
                    await ws.send(json.dumps(withdrawal_sub))

                    for _ in range(2):
                        response = json.loads(await ws.recv())
                        sub_id = response.get("result")
                        if response.get("id") == 1:
                            sub_map[sub_id] = "DEPOSIT"
                        elif response.get("id") == 2:
                            sub_map[sub_id] = "WITHDRAWAL"

                    # Trigger backfill if state exists or if CLI forced a max lookback
                    if highest_block_seen_hex or max_lookback_blocks is not None:
                        highest_block_seen_hex = await backfill_missed_blocks(
                            highest_block_seen_hex,
                            seen_events,
                            buffer,
                            max_lookback_blocks,
                        )

                    print("⏳ Listening for Two-Way Hyperliquid Bridge Flows...\n")

                    while True:
                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=10)
                            data = json.loads(message)

                            if "params" not in data or "result" not in data["params"]:
                                continue

                            event_sub_id = data["params"]["subscription"]
                            flow_type_base = sub_map.get(event_sub_id, "UNKNOWN")

                            log = data["params"]["result"]
                            tx_hash = log["transactionHash"]
                            log_index = log.get("logIndex", "0x0")

                            event_id = f"{tx_hash}_{log_index}"
                            if event_id in seen_events:
                                continue
                            seen_events.append(event_id)

                            highest_block_seen_hex = log.get(
                                "blockNumber", highest_block_seen_hex
                            )

                            is_removed = log.get("removed", False)
                            flow_type = (
                                f"REMOVED_{flow_type_base}"
                                if is_removed
                                else flow_type_base
                            )
                            usdc_amount = int(log["data"], 16) / 1_000_000
                            if is_removed:
                                usdc_amount = -usdc_amount

                            current_time = time.time()
                            timestamp_str = datetime.fromtimestamp(
                                current_time
                            ).strftime("%H:%M:%S")

                            user_address = None

                            if flow_type_base == "DEPOSIT":
                                emoji, label, topic_index = "🟢", "DEPOSIT   ", 1
                            elif flow_type_base == "WITHDRAWAL":
                                emoji, label, topic_index = "🔴", "WITHDRAWAL", 2
                            else:
                                return

                            user_address = "0x" + log["topics"][topic_index][-40:]

                            if not is_removed:
                                if usdc_amount >= min_print_value_usd:
                                    emoji = (
                                        "🟡"
                                        if usdc_amount >= 50 * min_print_value_usd
                                        else emoji
                                    )

                                    print(
                                        f"[{timestamp_str}] {emoji} {label} | ${usdc_amount:,.2f} | {user_address}"
                                    )

                            buffer.append(
                                {
                                    "local_timestamp": current_time,
                                    "flow_type": flow_type,
                                    "user_address": user_address,
                                    "usdc_amount": usdc_amount,
                                    "tx_hash": tx_hash,
                                    "log_index": int(log_index, 16),
                                    "block_number": int(highest_block_seen_hex, 16),
                                }
                            )

                        except asyncio.TimeoutError:
                            pass

                        time_since_flush = time.time() - last_flush_time
                        if len(buffer) >= BUFFER_SIZE_LIMIT or (
                            time_since_flush >= BUFFER_TIME_LIMIT_SEC
                            and len(buffer) > 0
                        ):
                            data_to_save = buffer.copy()
                            buffer.clear()
                            last_flush_time = time.time()

                            await asyncio.to_thread(
                                save_parquet_chunk,
                                data_to_save,
                                DATA_DIR,
                                highest_block_seen_hex,
                            )

            except websockets.exceptions.ConnectionClosed:
                print("⚠️ Alchemy Websocket closed. Reconnecting...")
            except Exception as e:
                print(f"❌ Connection lost: {e}. Retrying in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    finally:
        if buffer:
            print(
                f"\n🛑 Shutting down. Flushing remaining {len(buffer)} flows to disk..."
            )
            save_parquet_chunk(buffer, DATA_DIR, highest_block_seen_hex)
        else:
            save_last_block(highest_block_seen_hex)


if __name__ == "__main__":
    # Setup argparse for CLI customization
    parser = argparse.ArgumentParser(description="Hyperliquid Bridge WebSocket Scraper")
    parser.add_argument(
        "--lookback-days",
        type=float,
        help="Maximum days to backfill (e.g., 2.5)",
        default=None,
    )
    parser.add_argument(
        "--lookback-blocks",
        type=int,
        help="Maximum blocks to backfill (Overrides days)",
        default=None,
    )
    parser.add_argument(
        "--min_usd",
        type=float,
        default=10000.0,
        help="Minimum USD value to print to console (default: $10,000)",
    )
    args = parser.parse_args()

    # Resolve max block limit based on user inputs
    max_lookback_blocks = None
    if args.lookback_blocks is not None:
        max_lookback_blocks = args.lookback_blocks
    elif args.lookback_days is not None:
        # Arbitrum processes ~4 blocks per second.
        # Days * 24hrs * 60m * 60s * 4 blocks
        max_lookback_blocks = int(args.lookback_days * 24 * 60 * 60 * 4)

    try:
        asyncio.run(
            scrape_bridge_flows(max_lookback_blocks, min_print_value_usd=args.min_usd)
        )
    except KeyboardInterrupt:
        pass
