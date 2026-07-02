# %%
import boto3
from botocore import UNSIGNED
from botocore.config import Config
import argparse
from datetime import datetime, timedelta, timezone
import asyncio
import lz4.frame
from pathlib import Path
import csv
import json
import time
import urllib.request

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

# %%
DIR_PATH = Path(__file__).parent
BUCKET = "hyperliquid-archive"
CSV_HEADER = ["datetime", "timestamp", "level", "price", "size", "number"]

HL_REST_URL = "https://api.hyperliquid.xyz/info"

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
OHLCV_DIR = PROJECT_ROOT / "data" / "cleaned" / "historical" / "hyperliquid"

_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
    "3d": 259_200_000, "1w": 604_800_000,
}

# %%


def get_args():
    parser = argparse.ArgumentParser(
        description="Retrieve historical market data from Hyperliquid exchange"
    )
    subparser = parser.add_subparsers(
        dest="tool", required=True, help="tool: download, decompress, to_csv, ohlcv"
    )

    global_parser = subparser.add_parser("global_settings", add_help=False)
    global_parser.add_argument(
        "t",
        metavar="Tickers",
        help="Tickers of assets to be downloaded separated by spaces. e.g. BTC ETH",
        nargs="+",
    )
    global_parser.add_argument(
        "--all",
        help="Apply action to all available dates and times.",
        action="store_true",
        default=False,
    )
    global_parser.add_argument(
        "-sd",
        metavar="Start date",
        help="Starting date as one unbroken string formatted: YYYYMMDD.  e.g. 20230916",
    )
    global_parser.add_argument(
        "-sh",
        metavar="Start hour",
        help="Hour of the starting day as an integer between 0 and 23. e.g. 9  Default: 0",
        type=int,
        default=0,
    )
    global_parser.add_argument(
        "-ed",
        metavar="End date",
        help="Ending date as one unbroken string formatted: YYYYMMDD.  e.g. 20230916",
    )
    global_parser.add_argument(
        "-eh",
        metavar="End hour",
        help="Hour of the ending day as an integer between 0 and 23. e.g. 9  Default: 23",
        type=int,
        default=23,
    )

    subparser.add_parser(
        "download", help="Download historical market data", parents=[global_parser]
    )
    subparser.add_parser(
        "decompress", help="Decompress downloaded lz4 data", parents=[global_parser]
    )
    subparser.add_parser(
        "to_csv",
        help="Convert decompressed downloads into formatted CSV",
        parents=[global_parser],
    )

    ohlcv_parser = subparser.add_parser(
        "ohlcv", help="Download OHLCV candles via Hyperliquid REST API (candleSnapshot)"
    )
    ohlcv_parser.add_argument("--coin", required=True, help="Coin e.g. ETH BTC SOL")
    ohlcv_parser.add_argument(
        "--interval", default="1m",
        help="Candle interval: 1m 3m 5m 15m 30m 1h 2h 4h 8h 12h 1d 3d 1w (default: 1m)",
    )
    ohlcv_parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    ohlcv_parser.add_argument("--end", required=True, help="End date YYYY-MM-DD (exclusive)")

    return parser.parse_args()


def make_date_list(start_date, end_date):
    start_date = datetime.strptime(start_date, "%Y%m%d")
    end_date = datetime.strptime(end_date, "%Y%m%d")

    date_list = []

    current_date = start_date
    while current_date <= end_date:
        date_list.append(current_date.strftime("%Y%m%d"))
        current_date += timedelta(days=1)

    return date_list


def make_date_hour_list(date_list, start_hour, end_hour, delimiter="/"):
    date_hour_list = []
    end_date = date_list[-1]
    hour = start_hour
    end = 23
    for date in date_list:
        if date == end_date:
            end = end_hour

        while hour <= end:
            date_hour = date + delimiter + str(hour)
            date_hour_list.append(date_hour)
            hour += 1

        hour = 0

    return date_hour_list


async def download_object(s3, asset, date_hour):
    date_and_hour = date_hour.split("/")
    s3.download_file(
        BUCKET,
        f"market_data/{date_hour}/l2Book/{asset}.lz4",
        f"{DIR_PATH}/downloads/{asset}/{date_and_hour[0]}-{date_and_hour[1]}.lz4",
    )


async def download_objects(s3, assets, date_hour_list):
    print(f"Downloading {len(date_hour_list)} objects...")
    for asset in assets:
        await asyncio.gather(
            *[download_object(s3, asset, date_hour) for date_hour in date_hour_list]
        )


async def decompress_file(asset, date_hour):
    lz_file_path = DIR_PATH / "downloads" / asset / f"{date_hour}.lz4"
    file_path = DIR_PATH / "downloads" / asset / date_hour

    if not lz_file_path.is_file():
        print(f"decompress_file: file not found: {lz_file_path}")
        return

    with lz4.frame.open(lz_file_path, mode="r") as lzfile:
        data = lzfile.read()
        with open(file_path, "wb") as file:
            file.write(data)


async def decompress_files(assets, date_hour_list):
    print(f"Decompressing {len(date_hour_list)} files...")
    for asset in assets:
        await asyncio.gather(
            *[decompress_file(asset, date_hour) for date_hour in date_hour_list]
        )


def write_rows(csv_writer, line):
    rows = []
    entry = json.loads(line)
    date_time = entry["time"]
    timestamp = str(entry["raw"]["data"]["time"])
    all_orders = entry["raw"]["data"]["levels"]

    for i, order_level in enumerate(all_orders):
        level = str(i + 1)
        for order in order_level:
            price = order["px"]
            size = order["sz"]
            number = str(order["n"])

            rows.append([date_time, timestamp, level, price, size, number])

    for row in rows:
        csv_writer.writerow(row)


async def convert_file(asset, date_hour):
    file_path = DIR_PATH / "downloads" / asset / date_hour
    csv_path = DIR_PATH / "csv" / asset / f"{date_hour}.csv"

    with open(csv_path, "w", newline="") as csv_file:
        csv_writer = csv.writer(csv_file, dialect="excel")
        csv_writer.writerow(CSV_HEADER)

        with open(file_path) as file:
            for line in file:
                write_rows(csv_writer, line)


async def files_to_csv(assets, date_hour_list):
    print(f"Converting {len(date_hour_list)} files to CSV...")
    for asset in assets:
        await asyncio.gather(
            *[convert_file(asset, date_hour) for date_hour in date_hour_list]
        )


# ── OHLCV REST downloader ─────────────────────────────────────────────────────

def fetch_ohlcv_candles(coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    """Fetch OHLCV candles from Hyperliquid REST candleSnapshot endpoint with pagination."""
    candle_ms = _INTERVAL_MS.get(interval, 60_000)
    records: list[dict] = []
    current_start = start_ms

    while current_start < end_ms:
        payload = json.dumps({
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": interval, "startTime": current_start, "endTime": end_ms},
        }).encode("utf-8")
        req = urllib.request.Request(
            HL_REST_URL, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if not data:
            break
        records.extend(data)
        last_t = data[-1]["t"]
        current_start = last_t + candle_ms
        if len(data) < 5000:
            break
        time.sleep(0.05)

    return records


def save_ohlcv_parquet(coin: str, records: list[dict]) -> Path:
    df = pd.DataFrame([
        {
            "timestamp": pd.Timestamp(r["t"], unit="ms", tz="UTC"),
            "open":   float(r["o"]),
            "high":   float(r["h"]),
            "low":    float(r["l"]),
            "close":  float(r["c"]),
            "volume": float(r["v"]),
            "vwap":   float(r.get("vw", 0) or 0),
            "trades": int(r.get("n", 0) or 0),
        }
        for r in records
    ])
    out_dir = OHLCV_DIR / coin
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{int(time.time() * 1000)}.parquet"
    pq.write_table(pa.Table.from_pandas(df), out_path)
    return out_path


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(DIR_PATH)
    args = get_args()

    if args.tool == "ohlcv":
        start_ms = int(
            datetime.strptime(args.start, "%Y-%m-%d")
            .replace(tzinfo=timezone.utc)
            .timestamp() * 1000
        )
        end_ms = int(
            datetime.strptime(args.end, "%Y-%m-%d")
            .replace(tzinfo=timezone.utc)
            .timestamp() * 1000
        )
        print(f"Fetching {args.coin} {args.interval} candles {args.start} → {args.end} …")
        records = fetch_ohlcv_candles(args.coin, args.interval, start_ms, end_ms)
        print(f"Fetched {len(records):,} candles")
        if records:
            out = save_ohlcv_parquet(args.coin, records)
            print(f"Saved → {out}")
        else:
            print("No candles returned.")
        return

    s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED))

    downloads_path = DIR_PATH / "downloads"
    downloads_path.mkdir(exist_ok=True)

    csv_path = DIR_PATH / "csv"
    csv_path.mkdir(exist_ok=True)

    for asset in args.t:
        downloads_asset_path = downloads_path / asset
        downloads_asset_path.mkdir(exist_ok=True)
        csv_asset_path = csv_path / asset
        csv_asset_path.mkdir(exist_ok=True)

    date_list = make_date_list(args.sd, args.ed)
    loop = asyncio.new_event_loop()

    if args.tool == "download":
        date_hour_list = make_date_hour_list(date_list, args.sh, args.eh)
        loop.run_until_complete(download_objects(s3, args.t, date_hour_list))
        loop.close()

    if args.tool == "decompress":
        date_hour_list = make_date_hour_list(date_list, args.sh, args.eh, delimiter="-")
        loop.run_until_complete(decompress_files(args.t, date_hour_list))
        loop.close()

    if args.tool == "to_csv":
        date_hour_list = make_date_hour_list(date_list, args.sh, args.eh, delimiter="-")
        loop.run_until_complete(files_to_csv(args.t, date_hour_list))
        loop.close()

    print("Done")


if __name__ == "__main__":
    main()
