import asyncio
import aiohttp
import time
import pandas as pd
import argparse
from datetime import datetime, timezone
from pathlib import Path

##############################################################################
# Macro & Alternative Data REST API Poller
# Periodically fetches structural market data and saves time-series snapshots
##############################################################################

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "cleaned" / "macro_snapshots"

DATA_DIR.mkdir(parents=True, exist_ok=True)

# %%


def append_to_csv(data_dict: dict, filename: str):
    """Appends a dictionary row to a CSV to maintain a time-series record."""
    file_path = DATA_DIR / filename
    df = pd.DataFrame([data_dict])

    # If file doesn't exist, write headers; otherwise append
    if not file_path.is_file():
        df.to_csv(file_path, index=False)
    else:
        df.to_csv(file_path, mode="a", header=False, index=False)


async def fetch_binance_open_interest(session: aiohttp.ClientSession, coin: str):
    """Fetches Total Open Interest from Binance Futures (Free, no API key)."""
    url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={coin.upper()}"
    try:
        async with session.get(url) as response:
            data = await response.json()
            row = {
                "timestamp": int(time.time() * 1000),
                "datetime": datetime.now(timezone.utc),
                "coin": coin.upper(),
                "open_interest_tokens": float(data.get("openInterest", 0)),
            }
            append_to_csv(row, f"open_interest_{coin.upper()}.csv")
            print(
                f"[OI] {coin.upper()} Open Interest: {row['open_interest_tokens']:,.2f} tokens"
            )
    except Exception as e:
        print(f"❌ Error fetching Open Interest for {coin}: {e}")


async def fetch_defillama_stablecoins(session: aiohttp.ClientSession):
    """Fetches total circulating stablecoin supply (Proxy for macro market liquidity)."""
    url = "https://stablecoins.llama.fi/stablecoins"
    try:
        async with session.get(url) as response:
            data = await response.json()
            pegged_assets = data.get("peggedAssets", [])

            # Find USDT and USDC data
            usdt_data = next(
                (item for item in pegged_assets if item["symbol"] == "USDT"), None
            )
            usdc_data = next(
                (item for item in pegged_assets if item["symbol"] == "USDC"), None
            )

            row = {
                "timestamp": int(time.time() * 1000),
                "datetime": datetime.now(timezone.utc),
                "usdt_circulating": usdt_data["circulating"]["peggedUSD"]
                if usdt_data
                else 0,
                "usdc_circulating": usdc_data["circulating"]["peggedUSD"]
                if usdc_data
                else 0,
            }
            append_to_csv(row, "macro_stablecoin_supply.csv")
            print(
                f"[MACRO] Stablecoin Supply - USDT: ${row['usdt_circulating'] / 1e9:.2f}B | USDC: ${row['usdc_circulating'] / 1e9:.2f}B"
            )
    except Exception as e:
        print(f"❌ Error fetching DefiLlama data: {e}")


async def fetch_deribit_volatility(session: aiohttp.ClientSession, currency: str):
    """Fetches Implied Volatility (DVOL) from Deribit Options (Free, no API key)."""
    # Deribit requires start_timestamp and end_timestamp in milliseconds
    end_timestamp = int(time.time() * 1000)
    # Pull the last 2 days of data to guarantee we get the latest daily candle
    start_timestamp = end_timestamp - (2 * 24 * 60 * 60 * 1000)

    url = f"https://www.deribit.com/api/v2/public/get_volatility_index_data?currency={currency.upper()}&start_timestamp={start_timestamp}&end_timestamp={end_timestamp}&resolution=1D"

    try:
        async with session.get(url) as response:
            data = await response.json()

            # 1. Catch API-level errors so it never fails silently again
            if "error" in data:
                print(
                    f"⚠️ Deribit API Error for {currency}: {data['error'].get('message')}"
                )
                return

            # 2. Process the data if successful
            if (
                "result" in data
                and "data" in data["result"]
                and len(data["result"]["data"]) > 0
            ):
                # The latest daily data point is the last in the array [timestamp, open, high, low, close]
                latest_data = data["result"]["data"][-1]

                row = {
                    "timestamp": int(time.time() * 1000),
                    # Added .isoformat() so it saves nicely in your CSV
                    "datetime": datetime.now(timezone.utc).isoformat(),
                    "currency": currency.upper(),
                    "dvol_close": float(latest_data[4]),
                }
                append_to_csv(row, f"deribit_dvol_{currency.upper()}.csv")
                print(
                    f"[VOLATILITY] {currency.upper()} DVOL Index: {row['dvol_close']:.2f}"
                )
            else:
                print(f"⚠️ No volatility data returned for {currency}.")

    except Exception as e:
        print(f"❌ Error fetching Deribit Volatility for {currency}: {e}")


async def polling_loop(interval_seconds: int, coins: list):
    """Runs continuously, fetching REST APIs at the given interval."""
    print(f"🚀 Starting REST API Poller. Interval: {interval_seconds} seconds.")

    async with aiohttp.ClientSession() as session:
        while True:
            tasks = []

            # 1. Fetch Open Interest for requested coins
            for coin in coins:
                tasks.append(fetch_binance_open_interest(session, coin))

            # 2. Fetch Macro Stablecoin Data
            tasks.append(fetch_defillama_stablecoins(session))

            # 3. Fetch Options Volatility for BTC and ETH
            tasks.append(fetch_deribit_volatility(session, "BTC"))
            tasks.append(fetch_deribit_volatility(session, "ETH"))

            # Execute all REST calls concurrently
            await asyncio.gather(*tasks)

            print(f"⏳ Sleeping for {interval_seconds} seconds...\n")
            await asyncio.sleep(interval_seconds)


def main():
    parser = argparse.ArgumentParser(description="Macro & Alternative Data REST Poller")
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Polling interval in seconds (default: 300 / 5 mins)",
    )
    parser.add_argument(
        "--coins",
        type=str,
        nargs="+",
        default=["BTCUSDT", "ETHUSDT"],
        help="List of Binance symbols for Open Interest",
    )
    args = parser.parse_args()

    try:
        asyncio.run(polling_loop(args.interval, args.coins))
    except KeyboardInterrupt:
        print("\n🛑 Poller stopped safely by user.")


if __name__ == "__main__":
    main()
