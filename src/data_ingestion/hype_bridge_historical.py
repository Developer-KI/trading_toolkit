import asyncio
import os
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import aiohttp
import json
from datetime import datetime
from dotenv import load_dotenv
import re
from pathlib import Path

# ---------------------------------------------------------
# CONSTANTS & CONFIGURATION
# ---------------------------------------------------------
load_dotenv()
ARBISCAN_API_KEY = os.getenv("ARBISCAN")

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

CHECKPOINT_FILE = os.path.join(DATA_DIR, "hist_scraper_checkpoint.json")
SAVE_THRESHOLD = 500000  # Save to disk every 500k rows

os.makedirs(DATA_DIR, exist_ok=True)


# ---------------------------------------------------------
# PERSISTENCE LOGIC
# ---------------------------------------------------------
def get_earliest_live_block(data_dir):
    """
    Scans the live scraper's timestamped parquet files to find the absolute
    earliest block it recorded, preventing the historical scraper from overlapping.
    """
    earliest_block = float("inf")

    # The live script saves files as exactly timestamped digits: e.g., 1713500000000.parquet
    # We use regex so we don't accidentally read the historical_total.parquet files
    pattern = re.compile(r"^\d+\.parquet$")

    if not os.path.exists(data_dir):
        return None

    live_files = [
        os.path.join(data_dir, f) for f in os.listdir(data_dir) if pattern.match(f)
    ]

    if not live_files:
        return None

    print(
        f"\n🔍 Analyzing {len(live_files)} live parquet files to prevent data overlap..."
    )

    for file in live_files:
        try:
            # We ONLY read the 'block_number' column to make this scan lightning-fast and use ~0 RAM
            df = pq.read_table(file, columns=["block_number"]).to_pandas()
            if not df.empty:
                min_val = df["block_number"].min()
                if min_val < earliest_block:
                    earliest_block = min_val
        except Exception as e:
            print(f"⚠️ Warning: Could not read {file} for overlap detection: {e}")

    return int(earliest_block) if earliest_block != float("inf") else None


def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, "r") as f:
            return json.load(f)
    return {"DEPOSIT": 100_000_000, "WITHDRAWAL": 100_000_000}


def save_checkpoint(flow_type, block):
    checkpoints = load_checkpoint()
    checkpoints[flow_type] = block
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(checkpoints, f, indent=4)


def append_to_parquet(data_list, flow_type):
    if not data_list:
        return

    file_path = os.path.join(DATA_DIR, f"historical_{flow_type.lower()}s_total.parquet")
    df_new = pd.DataFrame(data_list)

    if not os.path.exists(file_path):
        # Create new file, deduplicate just to be safe
        df_new.drop_duplicates(subset=["tx_hash", "log_index"], inplace=True)
        table = pa.Table.from_pandas(df_new)
        pq.write_table(table, file_path)
        print(f"💾 [{flow_type}] Created new file and saved {len(df_new):,} rows.")
    else:
        # Load existing, concat, and dedupe. This elegantly handles overlapping checkpoints!
        existing_df = pd.read_parquet(file_path)
        combined_df = pd.concat([existing_df, df_new], ignore_index=True)

        before_count = len(combined_df)
        combined_df.drop_duplicates(subset=["tx_hash", "log_index"], inplace=True)
        after_count = len(combined_df)

        table = pa.Table.from_pandas(combined_df)
        pq.write_table(table, file_path)

        dupes_removed = before_count - after_count
        dupe_msg = (
            f" (Removed {dupes_removed:,} overlapping dupes)"
            if dupes_removed > 0
            else ""
        )
        print(
            f"💾 [{flow_type}] Appended {len(df_new):,} rows.{dupe_msg} Total file rows: {after_count:,}"
        )


async def get_latest_block(session):
    """Fetches the absolute latest block number on the Arbitrum chain."""
    params = {
        "chainid": 42161,
        "module": "proxy",
        "action": "eth_blockNumber",
        "apikey": ARBISCAN_API_KEY,
    }
    async with session.get("https://api.etherscan.io/v2/api", params=params) as resp:
        data = await resp.json()
        return int(data.get("result", "0x0"), 16)


async def fetch_flows(session, flow_type):
    checkpoints = load_checkpoint()
    current_block = checkpoints.get(flow_type, 100_000_000)

    if current_block == "COMPLETED":
        print(f"✅ [{flow_type}] is already fully completed. Skipping...")
        return 0

    current_block = int(current_block)

    latest_chain_block = await get_latest_block(session)
    if latest_chain_block == 0:
        print(f"❌[{flow_type}] Failed to fetch latest block from Arbiscan. Aborting.")
        return 0

    # -------------------------------------------------------------
    # NEW: OVERLAP PREVENTION LOGIC
    # -------------------------------------------------------------
    earliest_live_block = get_earliest_live_block(DATA_DIR)

    if earliest_live_block:
        target_max_block = earliest_live_block - 1

        # If the historical scraper has already reached the start of the live data
        if current_block > target_max_block:
            print(
                f"✅ [{flow_type}] Historical scrape seamlessly connected to live data at block {earliest_live_block}. Marking COMPLETED."
            )
            save_checkpoint(flow_type, "COMPLETED")
            return 0

        print(
            f"🎯 Live data detected! Capping historical target at block {target_max_block:,} to prevent duplicate data."
        )
        latest_chain_block = min(latest_chain_block, target_max_block)
    # -------------------------------------------------------------

    buffer = []
    seen_events = set()
    total_recovered = 0
    is_deposit = flow_type == "DEPOSIT"
    is_completed = False
    current_page = 1

    CHUNK_SIZE = 1_000_000

    print(
        f"\n🚀[{flow_type}] Fetching from block {current_block} to {latest_chain_block:,}..."
    )

    try:
        while current_block <= latest_chain_block:
            # Create a strict window so we know exactly what Etherscan is searching
            target_to_block = min(current_block + CHUNK_SIZE, latest_chain_block)

            params = {
                "chainid": 42161,
                "module": "logs",
                "action": "getLogs",
                "fromBlock": current_block,
                "toBlock": target_to_block,
                "address": USDC_ARBITRUM,
                "topic0": TRANSFER_EVENT_TOPIC,
                "page": current_page,
                "offset": 1000,
                "apikey": ARBISCAN_API_KEY,
            }

            if is_deposit:
                params.update({"topic0_2_opr": "and", "topic2": PADDED_BRIDGE_ADDRESS})
            else:
                params.update({"topic0_1_opr": "and", "topic1": PADDED_BRIDGE_ADDRESS})

            async with session.get(
                "https://api.etherscan.io/v2/api", params=params
            ) as resp:
                data = await resp.json()

            if data.get("status") == "0":
                message = data.get("message") or ""
                result = data.get("result") or ""

                # If chunk is still too large, dynamically shrink it
                if (
                    "Log response size exceeded" in message
                    or "smaller block range" in result
                ):
                    CHUNK_SIZE = max(100_000, CHUNK_SIZE // 2)
                    print(
                        f"⚠️ [{flow_type}] Range too large. Reducing chunk size to {CHUNK_SIZE:,}"
                    )
                    continue

                if "No records found" in message or "No records found" in result:
                    # The chunk is perfectly empty. Advance safely to the NEXT chunk!
                    current_block = target_to_block + 1
                    current_page = 1
                    continue

                print(
                    f"⚠️ [{flow_type}] API Notice: {message} | {result}. Retrying in 2s..."
                )
                await asyncio.sleep(2)
                continue

            logs = data.get("result") or []
            if not logs:
                current_block = target_to_block + 1
                current_page = 1
                continue

            for log in logs:
                tx_hash = log["transactionHash"]
                raw_log_idx = log.get("logIndex", "0x0")
                log_idx = (
                    int(raw_log_idx, 16) if raw_log_idx and raw_log_idx != "0x" else 0
                )

                raw_block = log.get("blockNumber", "0x0")
                block_num = int(raw_block, 16) if raw_block and raw_block != "0x" else 0

                raw_ts = log.get("timeStamp")
                ts_value = (
                    int(raw_ts, 16) if str(raw_ts).startswith("0x") else int(raw_ts)
                )

                event_id = f"{tx_hash}_{log_idx}"
                if event_id in seen_events:
                    continue
                seen_events.add(event_id)

                buffer.append(
                    {
                        "local_timestamp": ts_value,
                        "flow_type": flow_type,
                        "user_address": "0x"
                        + log["topics"][1 if is_deposit else 2][-40:],
                        "usdc_amount": int(log["data"], 16) / 1_000_000,
                        "tx_hash": tx_hash,
                        "log_index": log_idx,
                        "block_number": block_num,
                    }
                )
                total_recovered += 1

            last_block_seen = int(logs[-1]["blockNumber"], 16)
            latest_time = datetime.fromtimestamp(
                buffer[-1]["local_timestamp"]
            ).strftime("%Y-%m-%d %H:%M:%S")

            print(
                f"🔄 [{flow_type}] Logs: {len(logs)} | Buffer: {len(buffer):,} | Block {last_block_seen} ({latest_time}) | Target: {target_to_block}"
            )

            if len(buffer) >= SAVE_THRESHOLD:
                append_to_parquet(buffer, flow_type)
                buffer.clear()
                save_checkpoint(flow_type, last_block_seen)

            if len(logs) == 1000:
                if current_block == last_block_seen:
                    current_page += 1
                else:
                    current_block = last_block_seen
                    current_page = 1
            else:
                # We definitively consumed ALL logs in this chunk window. Advance past it.
                current_block = target_to_block + 1
                current_page = 1

            await asyncio.sleep(0.25)

        is_completed = True  # Successfully traversed all the way to the live chain tip

    except asyncio.CancelledError:
        print(f"\n⚠️ [{flow_type}] Interrupted by user! Flushing progress...")
        raise
    except Exception as e:
        print(f"\n❌[{flow_type}] Unexpected Error: {e}")
        raise
    finally:
        if buffer:
            append_to_parquet(buffer, flow_type)

        if is_completed:
            save_checkpoint(flow_type, "COMPLETED")
            print(
                f"✅ [{flow_type}] Fully completed! Recovered {total_recovered:,} new records."
            )
        else:
            save_checkpoint(flow_type, current_block)
            print(f"⏸️[{flow_type}] Successfully checkpointed at block {current_block}.")

    return total_recovered


async def main():
    async with aiohttp.ClientSession() as session:
        await fetch_flows(session, "DEPOSIT")
        await fetch_flows(session, "WITHDRAWAL")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Catching it at the very top level prevents giant ugly Traceback logs
        # when a user forces an exit via Ctrl+C.
        print("\n🛑 Program abruptly halted via Keyboard Interrupt. Exited cleanly.")
