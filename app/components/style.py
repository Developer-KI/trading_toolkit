"""Terminal-style CSS shared across all app pages."""
import streamlit as st

_CSS = """
<style>
    .block-container {
        padding-top: 0.5rem;
        padding-bottom: 0rem;
        padding-left: 1.5rem;
        padding-right: 1.5rem;
    }
    button[data-baseweb="tab"] {
        font-size: 12px !important;
        height: 30px !important;
        padding: 0px 12px !important;
        font-weight: 600 !important;
    }
    [data-testid="stMetricLabel"] {
        font-size: 11px !important;
        font-weight: 500 !important;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: #9ba3b8;
    }
    [data-testid="stMetricValue"] {
        font-size: 17px !important;
        font-weight: 700 !important;
        font-family: 'Courier New', Courier, monospace;
    }
    [data-testid="stMetricDelta"] {
        font-size: 12px !important;
        font-weight: 500 !important;
    }
    h3 {
        font-weight: 600 !important;
    }
    section[data-testid="stSidebar"][aria-expanded="true"] {
        width: 290px !important;
    }
    .stMainBlockContainer {
        transition: margin-left 0.3s ease-in-out;
    }

    /* ── Section labels in the main body ──────────────────────────────── */
    .sec-label {
        font-size: 11px !important;
        font-weight: 800 !important;
        text-transform: uppercase;
        letter-spacing: 0.14em;
        color: #9ba3b8;
        margin: 2px 0 6px 0 !important;
        padding: 0 !important;
        line-height: 1 !important;
        display: block;
    }

    /* ── Control panels (st.container(border=True)) ───────────────────── */
    [data-testid="stVerticalBlockBorderWrapper"] {
        background: rgba(255, 255, 255, 0.015);
        border-color: #2a2f4a !important;
        border-radius: 6px !important;
    }

    /* Quieter dividers — they separate controls from results, not shout. */
    hr {
        margin: 0.9rem 0 !important;
        border-color: #2a2f4a !important;
    }

    /* Callouts: flatter, so charts stay the loudest thing on the page. */
    [data-testid="stAlert"] {
        border-radius: 6px;
        font-size: 13px;
        padding: 0.55rem 0.8rem;
    }

    /* Expanders hold raw tables — recede until opened. */
    details[data-testid="stExpander"] summary p {
        font-size: 12px !important;
        font-weight: 600 !important;
        letter-spacing: 0.02em;
    }

    /* ── Sidebar section labels ───────────────────────────────────────── */
    .sb-label {
        font-size: 10px !important;
        font-weight: 800 !important;
        text-transform: uppercase;
        letter-spacing: 0.12em;
        color: #6b7280;
        margin: 10px 0 2px 0 !important;
        padding: 0 !important;
        line-height: 1 !important;
        display: block;
    }

    /* ── Universe asset chip ─────────────────────────────────────────── */
    .uni-chip {
        background: #161929;
        border: 1px solid #2a2f4a;
        border-radius: 4px;
        padding: 4px 10px;
        font-size: 13px;
        font-weight: 700;
        color: #cdd5f0;
        font-family: 'Courier New', Courier, monospace;
        letter-spacing: 0.05em;
        display: block;
        line-height: 1.4;
        margin-bottom: 2px;
    }
</style>
"""


def inject() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)
