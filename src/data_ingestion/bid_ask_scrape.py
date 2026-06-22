import asyncio
import websockets
import json
import urllib.request
import pandas as pd
import time
import argparse
import os

URL = "wss://api.hyperliquid.xyz/ws"
REST_URL = "https://api.hyperliquid.xyz/info"
CHECKPOINT_FILE = "scraper_checkpoint.json"


def fetch_market_data():
    """Fetches universe metadata and context for all Perps and Spots."""
    headers = {"Content-Type": "application/json"}

    def post_api(payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(REST_URL, data=data, headers=headers)
        with urllib.request.urlopen(req) as res:
            return json.loads(res.read().decode("utf-8"))

    assets = {}

    print("🔍 Fetching Perp metadata...")
    try:
        perp_data = post_api({"type": "metaAndAssetCtxs"})
        perp_universe = perp_data[0]["universe"]
        perp_ctxs = perp_data[1]

        for i, pair in enumerate(perp_universe):
            coin = pair["name"]
            ctx = perp_ctxs[i]
            vol = float(ctx["dayNtlVlm"])

            if vol < 100:  # Skip dead/delisted perps
                continue

            oi = float(ctx["openInterest"])
            px = float(ctx["markPx"])
            oi_usd = oi * px

            prev_px = float(ctx.get("prevDayPx", px))
            vol_pct = abs(px - prev_px) / prev_px if prev_px > 0 else 0
            funding_rate = float(ctx.get("funding", 0)) * 10000

            assets[coin] = {
                "Coin": coin,
                "Type": "Perp",
                "Price": px,
                "24h_Vol": vol,
                "OI_or_MCap": oi_usd,
                "Vol/OI_Ratio": vol / oi_usd if oi_usd > 0 else 0,
                "24h_Volatility(%)": vol_pct * 100,
                "Funding_Rate(bps)": funding_rate,
            }
    except Exception as e:
        print(f"⚠️ Error fetching perps: {e}")

    dexes = ["", "vntl"]
    for dex in dexes:
        dex_label = f" (Dex: {dex})" if dex else " (Main L1)"
        print(f"🔍 Fetching Spot metadata{dex_label}...")
        try:
            spot_data = post_api({"type": "spotMetaAndAssetCtxs", "dex": dex})
            spot_universe = spot_data[0]["universe"]
            spot_tokens = spot_data[0]["tokens"]
            spot_ctxs = spot_data[1]

            for i, pair in enumerate(spot_universe):
                base_idx = pair["tokens"][0]
                raw_coin = spot_tokens[base_idx]["name"]
                ws_coin = f"{dex}:{raw_coin}" if dex else raw_coin
                ctx = spot_ctxs[i]
                vol = float(ctx["dayNtlVlm"])

                if vol < 100:
                    continue

                supply = float(ctx.get("circulatingSupply", 0))
                px = float(ctx["markPx"])
                mc_usd = supply * px

                prev_px = float(ctx.get("prevDayPx", px))
                vol_pct = abs(px - prev_px) / prev_px if prev_px > 0 else 0

                assets[ws_coin] = {
                    "Coin": ws_coin,
                    "Type": "Spot",
                    "Price": px,
                    "24h_Vol": vol,
                    "OI_or_MCap": mc_usd,
                    "Vol/OI_Ratio": vol / mc_usd if mc_usd > 0 else 0,
                    "24h_Volatility(%)": vol_pct * 100,
                    "Funding_Rate(bps)": 0.0,
                }
        except Exception as e:
            print(f"⚠️ Error fetching spots for dex '{dex}': {e}")

    print(f"✅ Found {len(assets)} total assets across Perps and Spot.")
    return assets


def save_checkpoint(stats):
    """Saves the current dictionary of stats to disk to allow seamless resuming."""
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(stats, f)


async def collect_spreads(assets, target_ticks=15, batch_size=8, max_timeout=45):
    # 1. State Management
    reconnect_delay = 5  # Start higher if you're already being flagged
    fail_count = 0

    spread_stats = {
        coin: {
            "ticks": 0,
            "sum_bps": 0.0,
            "sum_usd": 0.0,
            "sum_bbo_depth": 0.0,
            "tick_rate": 0.0,
            "timed_out": False,
        }
        for coin in assets
    }

    completed_coins = set()
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE, "r") as f:
                saved_data = json.load(f)
                for coin, stats in saved_data.items():
                    if coin in spread_stats:
                        spread_stats[coin].update(stats)
                        if stats["ticks"] >= target_ticks or stats.get(
                            "timed_out", False
                        ):
                            completed_coins.add(coin)
            print(f"✅ Loaded checkpoint: {len(completed_coins)}/{len(assets)} done.")
        except Exception as e:
            print(f"⚠️ Checkpoint error: {e}")

    unprocessed_coins = [c for c in assets.keys() if c not in completed_coins]
    active_coins = {}
    total_coins = len(assets)

    while len(completed_coins) < total_coins:
        try:
            print(
                f"🌐 Connecting... (Backoff: {reconnect_delay}s | Remaining: {len(unprocessed_coins)})"
            )

            # Use a slightly different endpoint if the main one is sticky
            # URL = "wss://api-ui.hyperliquid.xyz/ws" # Alternative if still blocked

            async with websockets.connect(
                URL, ping_interval=20, ping_timeout=20, close_timeout=5
            ) as ws:
                # IMPORTANT: Wait a moment after handshake before spamming subscriptions
                await asyncio.sleep(2)

                processed_this_conn = 0

                # Fill the initial batch slowly
                while len(active_coins) < batch_size and unprocessed_coins:
                    coin = unprocessed_coins.pop(0)
                    active_coins[coin] = time.time()
                    await ws.send(
                        json.dumps(
                            {
                                "method": "subscribe",
                                "subscription": {"type": "l2Book", "coin": coin},
                            }
                        )
                    )
                    await asyncio.sleep(0.5)  # Staggered subscription

                while len(completed_coins) < total_coins:
                    # Connection Health Check
                    if processed_this_conn >= 30:  # Rotate even more frequently
                        print("♻️ Reaching session limit, rotating connection...")
                        break

                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=10.0)
                        data = json.loads(message)

                        # If we get ANY valid data, we reset our failure count
                        fail_count = 0
                        reconnect_delay = 5

                        if data.get("channel") == "l2Book" and "data" in data:
                            payload = data["data"]
                            raw_coin = payload["coin"]
                            actual_coin = (
                                raw_coin
                                if raw_coin in active_coins
                                else f"vntl:{raw_coin}"
                            )

                            if actual_coin in active_coins:
                                # ... [YOUR EXISTING DATA PROCESSING LOGIC HERE] ...
                                # (Assuming you keep the spread/depth calculations from your original script)

                                # If finished with a coin:
                                if spread_stats[actual_coin]["ticks"] >= target_ticks:
                                    completed_coins.add(actual_coin)
                                    del active_coins[actual_coin]
                                    processed_this_conn += 1
                                    save_checkpoint(spread_stats)

                                    # Subscribe to a new one to keep the pipe full
                                    if unprocessed_coins:
                                        new_coin = unprocessed_coins.pop(0)
                                        active_coins[new_coin] = time.time()
                                        await ws.send(
                                            json.dumps(
                                                {
                                                    "method": "subscribe",
                                                    "subscription": {
                                                        "type": "l2Book",
                                                        "coin": new_coin,
                                                    },
                                                }
                                            )
                                        )
                                        await asyncio.sleep(0.2)

                    except asyncio.TimeoutError:
                        await ws.send(json.dumps({"method": "ping"}))
                        continue

        except Exception as e:
            fail_count += 1
            # Put active coins back in the queue
            for c in list(active_coins.keys()):
                unprocessed_coins.insert(0, c)
            active_coins.clear()

            # Exponential backoff logic
            wait_time = reconnect_delay
            if fail_count > 3:
                print("🚨 Persistent rejection detected. Entering 60s Cool Down...")
                wait_time = 60

            print(f"⚠️ Connection dropped ({e}). Retrying in {wait_time}s...")
            await asyncio.sleep(wait_time)

            # Increase backoff for next time
            reconnect_delay = min(reconnect_delay * 2, 120)

    return spread_stats


async def main():
    parser = argparse.ArgumentParser(description="Hyperliquid Quant MM Scraper")
    parser.add_argument(
        "--ticks", type=int, default=20, help="Number of ticks required per coin"
    )
    parser.add_argument(
        "--batch", type=int, default=8, help="Concurrent coins per WebSocket"
    )
    parser.add_argument(
        "--timeout", type=int, default=45, help="Max wait time for illiquid coins"
    )
    args = parser.parse_args()

    assets = fetch_market_data()
    if not assets:
        return

    spread_stats = await collect_spreads(
        assets, target_ticks=args.ticks, batch_size=args.batch, max_timeout=args.timeout
    )

    print("\n📊 Processing quantitative metrics...")
    final_data = []
    for coin, info in assets.items():
        stats = spread_stats.get(coin, {})
        ticks = stats.get("ticks", 0)

        info["Ticks_Caught"] = ticks
        info["Avg_Spread(bps)"] = (stats["sum_bps"] / ticks) if ticks > 0 else 0
        info["Avg_BBO_Depth($)"] = (stats["sum_bbo_depth"] / ticks) if ticks > 0 else 0
        info["Tick_Rate(Hz)"] = stats.get("tick_rate", 0)

        final_data.append(info)

    df = pd.DataFrame(final_data)

    df["Spread_to_Vol_Ratio"] = df["Avg_Spread(bps)"] / (
        df["24h_Volatility(%)"] + 0.001
    )
    df["MM_Score"] = (
        df["Avg_Spread(bps)"] * df["Tick_Rate(Hz)"] * df["Avg_BBO_Depth($)"]
    ) / (df["24h_Volatility(%)"] + 1)
    df = df.sort_values(by="MM_Score", ascending=False).reset_index(drop=True)

    pd.set_option("display.max_rows", 150)
    pd.set_option("display.float_format", lambda x: f"{x:,.2f}")

    display_df = df[
        [
            "Coin",
            "Type",
            "24h_Volatility(%)",
            "Funding_Rate(bps)",
            "Vol/OI_Ratio",
            "Avg_Spread(bps)",
            "Avg_BBO_Depth($)",
            "Tick_Rate(Hz)",
            "Spread_to_Vol_Ratio",
            "MM_Score",
        ]
    ]

    print("\n" + "=" * 125)
    print(display_df.to_string())
    print("=" * 125)

    filename = f"hyperliquid_quant_metrics_{int(time.time())}.csv"
    df.to_csv(filename, index=False)
    print(f"\n💾 Saved full dataset to {filename}")

    # Clean up the checkpoint since we finished successfully!
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)
        print("🧹 Cleaned up checkpoint file.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 KeyboardInterrupt received. Progress is saved in checkpoint file.")
