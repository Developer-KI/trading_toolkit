"""
Options Analytics — reconstruct the IV surface and Greeks for an LSE-listed underlying.

Given the live option chain from the LSE provider, this page inverts each contract's
bid/ask mid under Black-Scholes to an implied volatility, then shows the smile, term
structure, 3-D surface, and Greeks. Mid is used rather than the last traded price
because a last print can be hours stale against a live spot, which pushes deep-ITM
quotes below intrinsic and leaves holes in the surface.

Launch from the project root:
    streamlit run app/Strategy_Explorer.py
then open "Options" in the sidebar page list.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

_APP = Path(__file__).resolve().parent.parent
_ROOT = _APP.parent
_SRC = _ROOT / "src"
for _p in [str(_SRC), str(_ROOT), str(_APP)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd
import streamlit as st

from components.lse_data import fetch_option_underlyings, get_api_key, load_chain_cached
from components.charts import (
    greeks_chart, iv_smile_chart, iv_surface_chart, term_structure_chart,
)
from components import ui
from components.style import inject
from core.derivatives import IVSurface

st.set_page_config(page_title="Options Analytics", page_icon="🧮", layout="wide")
inject()
st.title("Options Analytics")


def _sb(text: str) -> None:
    st.markdown(f'<p class="sb-label">{text}</p>', unsafe_allow_html=True)


# ── Mandatory, preloaded LSE credentials ──────────────────────────────────────

api_key = get_api_key()
if not api_key:
    st.error(
        "LSE API key is required. Add `LSE_DATA=your_key` to the project `.env` file, "
        "then reload this page."
    )
    st.stop()


@st.cache_data(show_spinner="Loading LSE options catalogue…")
def _underlyings() -> list[dict]:
    return fetch_option_underlyings(api_key) or []


catalogue = _underlyings()
if not catalogue:
    st.error("Could not load the LSE options catalogue. Check the API key and connectivity.")
    st.stop()

# Map "SYMBOL — Name" label → symbol.
_label_to_sym = {
    f"{row.get('symbol', '')} — {row.get('name', '')}".strip(" —"): row.get("symbol", "")
    for row in catalogue if row.get("symbol")
}
_labels = sorted(_label_to_sym)


# ── Sidebar controls ──────────────────────────────────────────────────────────

with st.sidebar:
    _sb("Underlying (LSE options catalogue)")
    default_idx = next((i for i, l in enumerate(_labels) if l.startswith("AAPL ")), 0)
    label = st.selectbox("Underlying", _labels, index=default_idx,
                         label_visibility="collapsed")
    underlying = _label_to_sym[label]

    _sb("Expiry window")
    today = date.today()
    exp_range = st.date_input(
        "Expiry window",
        value=(today, today + timedelta(days=180)),
        min_value=today,
        label_visibility="collapsed",
    )
    if isinstance(exp_range, (list, tuple)) and len(exp_range) == 2:
        d_from, d_to = exp_range
    else:  # user mid-selection: single date → treat as the upper bound
        d_from, d_to = today, (exp_range if isinstance(exp_range, date) else today + timedelta(days=180))
    min_dte = max((d_from - today).days, 0)
    max_dte = max((d_to - today).days, min_dte)

    _sb("Rates (bps)")
    r_bps = st.number_input("Risk-free rate (bps)", value=400, step=25, min_value=0)
    q_bps = st.number_input("Dividend yield (bps)", value=0, step=25, min_value=0)
    r = r_bps / 1e4
    q = q_bps / 1e4

    _sb("Smile x-axis")
    x_axis = st.radio("Smile x-axis", ["moneyness", "strike"], horizontal=True,
                      label_visibility="collapsed")
    use_moneyness = x_axis == "moneyness"

    load = st.button("Load chain", type="primary", use_container_width=True)


# ── Chain acquisition ─────────────────────────────────────────────────────────

ui.intro(
    "Black-Scholes implied volatility inverted from the bid/ask mid of every live "
    "contract — the smile, term structure and Greeks all read off that surface."
)

if load:
    chain = load_chain_cached(underlying, min_dte=min_dte, max_dte=max_dte, api_key=api_key)
    st.session_state["_options_chain"] = chain
chain = st.session_state.get("_options_chain")

if chain is None or not chain.contracts:
    ui.empty_state("Pick an underlying and expiry window, then press **Load chain**.")
    st.stop()

# Providers keep expired contracts in the "current" chain, frozen at their last traded
# state. Time to expiry is zero, so nothing can be reconstructed from them — drop them
# rather than rendering rows of blank IV and Greeks.
n_expired = chain.n_expired
if n_expired:
    chain = chain.drop_expired()
if not chain.contracts:
    ui.verdict("warn", f"All {n_expired} contracts in this chain have already expired — "
                       "the provider has no live chain for this underlying.")
    st.stop()


# ── Reconstruct surface (cached by parameter signature) ───────────────────────

surf_key = (
    f"_surf_{chain.underlying}_{min_dte}_{max_dte}"
    f"_{r_bps}_{q_bps}_{use_moneyness}"
)
if surf_key in st.session_state:
    surface = st.session_state[surf_key]
else:
    try:
        with st.spinner("Reconstructing IV surface…"):
            # Black-Scholes inversion of the bid/ask mid — the only mode the page
            # offers, so the surface always reflects market quotes rather than a
            # fitted model, and never a stale last-traded print.
            surface = IVSurface.from_chain(
                chain, r=r, q=q, moneyness=use_moneyness, model="bs", price="mid",
            )
        st.session_state[surf_key] = surface
    except (ValueError, RuntimeError) as e:
        st.error(f"Could not build the IV surface: {e}")
        st.stop()

gdf = surface.iv_df
n_total = len(chain.contracts)
n_valid = int(gdf["iv"].notna().sum())

ui.metric_row([
    {"label": "Underlying", "value": chain.underlying},
    {"label": "Spot", "value": f"{chain.spot:,.2f}" if pd.notna(chain.spot) else "—"},
    {"label": "Contracts", "value": f"{n_total:,}",
     "delta": f"−{n_expired} expired" if n_expired else None,
     "help": "Live contracts in the requested expiry window."},
    {"label": "Solved IV", "value": f"{n_valid}/{n_total}",
     "help": "Contracts whose quote could be inverted to an implied volatility."},
    {"label": "Expiries", "value": f"{len(surface.expiries)}"},
])

# Every blank IV has a reason — show the breakdown instead of leaving gaps unexplained.
if "iv_status" in gdf.columns:
    _unsolved = gdf.loc[gdf["iv"].isna(), "iv_status"]
    if len(_unsolved):
        _reasons = ", ".join(f"{n} {why}" for why, n in _unsolved.value_counts().items())
        st.caption(
            f"{len(_unsolved)} contract(s) could not be inverted — {_reasons}. "
            "See the `iv_status` column in the chain table."
        )

st.divider()

# ── Surface + term structure ──────────────────────────────────────────────────

ui.section("Volatility surface")
left, right = st.columns([3, 2])
with left:
    st.plotly_chart(iv_surface_chart(surface), use_container_width=True)
with right:
    st.plotly_chart(term_structure_chart(surface), use_container_width=True)
    if surface.expiries:
        atm = surface.atm_vol(surface.expiries[0])
        skew = surface.skew(surface.expiries[0])
        ui.metric_row([
            {"label": "Front ATM IV", "value": f"{atm * 100:.1f}%" if pd.notna(atm) else "—",
             "help": "At-the-money implied vol of the nearest expiry."},
            {"label": "Front skew 90/110",
             "value": f"{skew * 100:.1f}%" if pd.notna(skew) else "—",
             "help": "Put-side minus call-side IV. Positive means downside is bid."},
        ])

ui.section("Smile")
st.plotly_chart(iv_smile_chart(surface), use_container_width=True)

# ── Greeks per expiry ─────────────────────────────────────────────────────────

ui.section("Greeks")
# Only offer expiries with reconstructed data (skips e.g. 0-DTE rows that don't solve).
solved_expiries = surface.expiries or []
if solved_expiries:
    exp_labels = [pd.Timestamp(e).strftime("%Y-%m-%d") for e in solved_expiries]
    sel = st.selectbox("Expiry", exp_labels, index=0)
    st.plotly_chart(greeks_chart(gdf, expiry=pd.Timestamp(sel)), use_container_width=True)
else:
    ui.empty_state("No expiries with solvable IV in this window.")

# ── Full chain table ──────────────────────────────────────────────────────────

with st.expander("Chain table (with reconstructed IV & Greeks)"):
    show = gdf.copy()
    show["iv_%"] = show["iv"] * 100.0
    cols = ["expiry", "strike", "option_type", "mid", "last", "underlying_price",
            "T", "iv_%", "iv_status", "delta", "gamma", "vega", "theta", "rho",
            "volume", "open_interest"]
    cols = [c for c in cols if c in show.columns]
    st.dataframe(show[cols], use_container_width=True, height=420)
    st.download_button(
        "Download chain CSV", show[cols].to_csv(index=False).encode(),
        file_name=f"{chain.underlying}_chain.csv", mime="text/csv",
    )
