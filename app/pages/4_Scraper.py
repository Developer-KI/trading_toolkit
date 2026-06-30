"""Data Scraper — launch and monitor WebSocket scrapers from the dashboard."""
import os
import sys
import threading
import subprocess
import time
from collections import deque
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in [str(_ROOT / "src"), str(_ROOT), str(_ROOT / "app")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import streamlit as st

from components.style import inject

st.set_page_config(page_title="Scraper", page_icon="📡", layout="wide")
inject()
st.title("Data Scraper")

# ── Presets ───────────────────────────────────────────────────────────────────
# (exchange, coin, market, streams, depth)
_PRESETS: dict[str, tuple] = {
    "ETH Full (HL)":         ("hyperliquid", "ETH",     "perpetuals", ["trades", "l2", "funding"], 1),
    "BTC Full (HL)":         ("hyperliquid", "BTC",     "perpetuals", ["trades", "l2", "funding"], 1),
    "ETH Trades Only (HL)":  ("hyperliquid", "ETH",     "perpetuals", ["trades"], 1),
    "ETH L2+Trades (HL)":    ("hyperliquid", "ETH",     "perpetuals", ["trades", "l2"], 1),
    "SOL Full (HL)":         ("hyperliquid", "SOL",     "perpetuals", ["trades", "l2", "funding"], 1),
    "ETH Full (Binance)":    ("binance",     "ETHUSDT", "futures",    ["trades", "l2", "funding"], 20),
    "BTC Full (Binance)":    ("binance",     "BTCUSDT", "futures",    ["trades", "l2", "funding"], 20),
    "BTC L2+Trades (BN)":    ("binance",     "BTCUSDT", "futures",    ["trades", "l2"], 20),
    "ETH Spot (BN)":         ("binance",     "ETHUSDT", "spot",       ["trades"], 1),
}

# Valid multi-stream --mode combos for the HL CLI
_HL_MODE: dict[frozenset, str] = {
    frozenset(["trades", "l2", "funding"]): "all",
    frozenset(["trades", "l2"]):            "l2/trades",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_hl_cmds(coin: str, streams: list[str], depth: int) -> list[list[str]]:
    key = frozenset(streams)
    if key in _HL_MODE:
        return [[
            sys.executable, "-m", "src.data.feeds.hyperliquid",
            "--coin", coin, "--mode", _HL_MODE[key], "--depth", str(depth),
        ]]
    # Fallback: one subprocess per stream for combinations not in the mode map
    return [
        [sys.executable, "-m", "src.data.feeds.hyperliquid",
         "--coin", coin, "--mode", m, "--depth", str(depth)]
        for m in streams
    ]


def _build_bn_cmd(coin: str, market: str, streams: list[str], depth: int) -> list[str]:
    return [
        sys.executable, "-m", "src.data.feeds.binance",
        "--coin", coin, "--market", market,
        "--streams", *streams,
        "--depth", str(depth),
    ]


def _scraper_key(exchange: str, coin: str, streams: list[str]) -> str:
    return f"{exchange[:2].upper()}_{coin}_{'_'.join(sorted(streams))}"


def _start(key: str, cmd: list[str]) -> None:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        bufsize=1,
        cwd=str(_ROOT),
        env=env,
    )
    lines: deque[str] = deque(maxlen=300)

    def _reader(p: subprocess.Popen, q: deque) -> None:
        for line in iter(p.stdout.readline, ""):
            q.append(line.rstrip())
        p.stdout.close()

    threading.Thread(target=_reader, args=(proc, lines), daemon=True).start()
    st.session_state["scrapers"][key] = {
        "proc": proc,
        "lines": lines,
        "cmd": cmd,
        "started_at": datetime.now(),
    }


def _stop(key: str) -> None:
    entry = st.session_state["scrapers"].get(key)
    if not entry:
        return
    proc: subprocess.Popen = entry["proc"]
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
    del st.session_state["scrapers"][key]


def _is_running(entry: dict) -> bool:
    return entry["proc"].poll() is None


def _count_saved_rows(lines: deque) -> int:
    total = 0
    for line in lines:
        if "Saved" in line:
            parts = line.split()
            for i, p in enumerate(parts):
                if p == "Saved" and i + 1 < len(parts):
                    try:
                        total += int(parts[i + 1])
                    except ValueError:
                        pass
    return total


# ── Session state init ────────────────────────────────────────────────────────

if "scrapers" not in st.session_state:
    st.session_state["scrapers"] = {}

# Config defaults — set only once on first load, then widgets own their state
_INIT = {
    "sc_exchange": "hyperliquid",
    "sc_coin":     "ETH",
    "sc_coin_bn":  "ETHUSDT",
    "sc_market":   "futures",
    "sc_streams":  ["trades"],
    "sc_depth":    1,
    "_sc_last_preset": "(custom)",
}
for k, v in _INIT.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ── Sidebar: configuration ────────────────────────────────────────────────────

with st.sidebar:
    st.header("Configure Scraper")

    preset_name = st.selectbox(
        "Preset", ["(custom)"] + list(_PRESETS.keys()), key="sc_preset",
    )

    # When preset changes, push values into session state so widgets update
    if preset_name != "(custom)" and st.session_state["_sc_last_preset"] != preset_name:
        _ex, _co, _mk, _st_list, _dp = _PRESETS[preset_name]
        st.session_state["sc_exchange"] = _ex
        st.session_state["sc_coin"]     = _co
        st.session_state["sc_coin_bn"]  = _co
        st.session_state["sc_market"]   = _mk
        st.session_state["sc_streams"]  = _st_list
        st.session_state["sc_depth"]    = _dp
        st.session_state["_sc_last_preset"] = preset_name

    exchange = st.selectbox("Exchange", ["hyperliquid", "binance"], key="sc_exchange")

    if exchange == "hyperliquid":
        coin = st.text_input("Coin", key="sc_coin").upper()
        market = "perpetuals"
        available_streams = ["trades", "l2", "funding", "wallet"]
    else:
        coin = st.text_input("Symbol", key="sc_coin_bn").upper()
        market = st.selectbox("Market", ["futures", "spot"], key="sc_market")
        available_streams = ["trades", "l2", "funding"]

    # Clip stored stream selection to valid options for the current exchange
    stored_streams = st.session_state.get("sc_streams", [])
    valid_stored = [s for s in stored_streams if s in available_streams]
    if valid_stored != stored_streams:
        st.session_state["sc_streams"] = valid_stored

    streams = st.multiselect("Streams", available_streams, key="sc_streams")

    depth = 1
    if "l2" in streams:
        depth = st.slider("L2 Depth", min_value=1, max_value=20, key="sc_depth")

    wallet = ""
    data_streams = [s for s in streams if s != "wallet"]
    if exchange == "hyperliquid" and "wallet" in streams:
        wallet = st.text_input("Wallet address (0x...)", key="sc_wallet")

    st.divider()

    launch_disabled = (not data_streams and not wallet) or not coin
    if st.button(
        "Start Scraper", key="sc_start_btn", type="primary",
        disabled=launch_disabled, use_container_width=True,
    ):
        started_any = False

        if data_streams:
            if exchange == "hyperliquid":
                cmds = _build_hl_cmds(coin, data_streams, depth)
                for i, cmd in enumerate(cmds):
                    key = (
                        _scraper_key("hyperliquid", coin, data_streams)
                        if len(cmds) == 1
                        else _scraper_key("hyperliquid", coin, [data_streams[i]])
                    )
                    if key in st.session_state["scrapers"]:
                        st.warning(f"`{key}` already running.")
                    else:
                        _start(key, cmd)
                        started_any = True
            else:
                key = _scraper_key("binance", coin, data_streams)
                if key in st.session_state["scrapers"]:
                    st.warning(f"`{key}` already running.")
                else:
                    _start(key, _build_bn_cmd(coin, market, data_streams, depth))
                    started_any = True

        if wallet:
            wkey = f"HL_WALLET_{wallet[:10]}"
            if wkey in st.session_state["scrapers"]:
                st.warning(f"`{wkey}` already running.")
            else:
                _start(wkey, [sys.executable, "-m", "src.data.feeds.hyperliquid",
                               "--wallet", wallet])
                started_any = True

        if started_any:
            st.rerun()

# ── Main area: running scrapers ───────────────────────────────────────────────

scrapers: dict = st.session_state["scrapers"]

if not scrapers:
    st.info("No scrapers running. Configure one in the sidebar and click **Start Scraper**.")
else:
    any_running = False

    for key, entry in list(scrapers.items()):
        running = _is_running(entry)
        if running:
            any_running = True

        status_label = "🟢 RUNNING" if running else "🔴 STOPPED"

        with st.expander(f"{key}  —  {status_label}", expanded=running):
            col_info, col_btn = st.columns([5, 1])

            with col_info:
                st.caption(f"Started: {entry['started_at'].strftime('%H:%M:%S')}")
                st.caption(f"Cmd: `{' '.join(entry['cmd'])}`")
                m1, m2 = st.columns(2)
                m1.metric("Status", "Running" if running else "Stopped")
                m2.metric("Rows Saved (est.)", _count_saved_rows(entry["lines"]))

            with col_btn:
                st.write("")
                if running:
                    if st.button("Stop", key=f"stop_{key}", type="secondary"):
                        _stop(key)
                        st.rerun()
                else:
                    if st.button("Remove", key=f"rm_{key}"):
                        del scrapers[key]
                        st.rerun()

            st.markdown("**Console** (last 50 lines)")
            recent = list(entry["lines"])[-50:]
            st.code("\n".join(recent) if recent else "(no output yet)", language=None)

    # Auto-refresh while scrapers are active
    if any_running:
        time.sleep(2)
        st.rerun()
