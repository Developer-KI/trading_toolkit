"""
Layout primitives shared by every page, so the app reads as one system.

The pattern each screen follows:

    intro("What question this screen answers.")
    with panel():           # bordered block: inputs + the primary action
        ...
    st.divider()
    section("Results", "One line of context.")
    metric_row([...])       # headline numbers
    verdict("good", "…")    # the plain-language conclusion
    ...charts...
    with st.expander(...):  # raw tables and downloads last

Keeping these here means a change to the look lands everywhere at once.
"""
from __future__ import annotations

from contextlib import contextmanager

import streamlit as st

# Verdict kinds map to the same status vocabulary the charts use.
_VERDICT = {
    "good": st.success,
    "warn": st.warning,
    "bad": st.error,
    "info": st.info,
}


def intro(text: str) -> None:
    """One-line statement of what a screen is for. Always the first thing on a tab."""
    st.caption(text)


def section(title: str, help_text: str | None = None) -> None:
    """A small uppercase section label — quieter than st.subheader, used everywhere."""
    st.markdown(f'<p class="sec-label">{title}</p>', unsafe_allow_html=True)
    if help_text:
        st.caption(help_text)


@contextmanager
def panel(title: str | None = None):
    """Bordered block holding a screen's inputs and its primary action."""
    with st.container(border=True):
        if title:
            st.markdown(f'<p class="sec-label">{title}</p>', unsafe_allow_html=True)
        yield


def run_button(label: str, key: str) -> bool:
    """The primary action of a panel — full width, so every screen's CTA sits alike."""
    return st.button(label, type="primary", key=key, use_container_width=True)


def metric_row(specs: list[dict]) -> None:
    """
    Render one row of metrics from `[{"label", "value", "delta"?, "help"?}, …]`.

    Uniform across screens: same column split, same optional help affordance, and
    deltas default to neutral colouring so only real good/bad reads as green/red.
    """
    specs = [s for s in specs if s]
    if not specs:
        return
    for col, spec in zip(st.columns(len(specs)), specs):
        col.metric(
            spec["label"],
            spec["value"],
            delta=spec.get("delta"),
            delta_color=spec.get("delta_color", "off" if spec.get("delta") else "normal"),
            help=spec.get("help"),
        )


def verdict(kind: str, text: str) -> None:
    """The one-sentence conclusion for a screen. `kind`: good | warn | bad | info."""
    _VERDICT.get(kind, st.info)(text)


def empty_state(text: str) -> None:
    """Consistent 'nothing to show yet' message."""
    st.info(text)
