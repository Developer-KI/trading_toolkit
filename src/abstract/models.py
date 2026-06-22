"""
models.py — Data models for the vectorized backtester.

Defines Trade, Position, OrderBookSnapshot, and configuration dataclasses
used throughout the system. All timestamps are expected as pandas Timestamps.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

import logging

import numpy as np
import pandas as pd


logger = logging.getLogger(__name__)

class Side(enum.Enum):
    LONG = 1
    SHORT = -1
    FLAT = 0


class OrderType(enum.Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


# ── Order Book ───────────────────────────────────────────────────────────────


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBookSnapshot:
    """Single L2 snapshot — lists of (price, size) per side, best → worst."""

    timestamp: pd.Timestamp
    bids: list[OrderBookLevel]  # best bid first
    asks: list[OrderBookLevel]  # best ask first

    @property
    def book_depth(self) -> int:
        # Treat the left over book levels, if there are any, as non-existent
        return min(len(self.bids), len(self.asks))

    @property
    def mid(self) -> float:
        if self.bids and self.asks:
            return (self.bids[0].price + self.asks[0].price) / 2.0
        return np.nan

    @property
    def spread(self) -> float:
        if self.bids and self.asks:
            return self.asks[0].price - self.bids[0].price
        return np.nan

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        if mid and mid > 0:
            return (self.spread / mid) * 1e4
        return np.nan

    def depth_at(self, pct_from_mid: float = 0.01) -> dict[str, float]:
        """Cumulative size within `pct_from_mid` of the midprice."""
        mid = self.mid
        bid_depth = sum(
            lvl.size for lvl in self.bids if lvl.price >= mid * (1 - pct_from_mid)
        )
        ask_depth = sum(
            lvl.size for lvl in self.asks if lvl.price <= mid * (1 + pct_from_mid)
        )
        return {"bid_depth": bid_depth, "ask_depth": ask_depth}

    def vwap_fill_price(self, size: float, side: Side) -> float:
        """Walk the book and return the VWAP fill price for a given size."""
        levels = self.asks if side == Side.LONG else self.bids
        remaining = abs(size)
        cost = 0.0
        for lvl in levels:
            take = min(remaining, lvl.size)
            cost += take * lvl.price
            remaining -= take
            if remaining <= 0:
                break
        filled = abs(size) - remaining
        if filled == 0:
            return np.nan
        return cost / filled
    
# ── Funding Rate Snapshot ─────────────────────────────────────────────────────


@dataclass
class FundingSnapshot:
    """
    Single funding rate observation for a perpetual swap.

    Exchanges publish funding rates at fixed intervals (typically 8h).
    Align these to your bar timestamps so the engine and cost models
    can look up the rate that was active on any given bar.

    Fields:
        timestamp:        when this rate was observed / published
        rate:             per-period funding rate (e.g. 0.0001 = 1 bps)
        rate_annualized:  convenience pre-computed annual rate in bps
        oracle_price:     spot / index price the exchange uses as reference (missing = 0.0)
        mark_price:       fair price used for funding & liquidation calcs (missing = 0.0)
    """
    timestamp: pd.Timestamp
    rate: float                  # per-period (e.g. per 8h)
    rate_annualized: float       # annualised, in basis points
    oracle_price: float = 0.0   # spot / index reference price
    mark_price: float = 0.0     # exchange mark price


# ── Trade / Position ─────────────────────────────────────────────────────────


@dataclass
class Trade:
    timestamp: pd.Timestamp
    side: Side
    size: float
    entry_price: float
    exit_price: float | None = None
    exit_timestamp: pd.Timestamp | None = None
    pnl: float = 0.0
    pnl_pct: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0
    reason_entry: str = ""
    reason_exit: str = ""
    signal_values: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "exit_timestamp": self.exit_timestamp,
            "side": self.side.name,
            "size": self.size,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "pnl": self.pnl,
            "pnl_pct": self.pnl_pct,
            "fees": self.fees,
            "slippage": self.slippage,
            "reason_entry": self.reason_entry,
            "reason_exit": self.reason_exit,
            **{f"sig_{k}": v for k, v in self.signal_values.items()},
            **{f"meta_{k}": v for k, v in self.meta.items()},
        }


@dataclass
class Position:
    side: Side = Side.FLAT
    size: float = 0.0
    entry_price: float = 0.0
    entry_timestamp: pd.Timestamp | None = None
    unrealized_pnl: float = 0.0


# ── Backtester Configuration ────────────────────────────────────────────────


@dataclass
class BacktestConfig:
    initial_capital: float = 100_000.0
    position_sizing: str = (
        "fixed_fractional"  # fixed_fractional | fixed_notional | kelly
    )
    risk_per_trade: float = 0.02  # fraction of equity risked per trade
    max_position_pct: float = 0.25  # max % of equity
    use_l2_fills: bool = False  # walk-the-book fill simulation
    maker_fee_bps: float = 2.0  # exchange maker fee
    taker_fee_bps: float = 5.0  # exchange taker fee
    funding_rate_annual_bps: float = 0.0  # perpetual funding rate (annualised)
    slippage_model: str = "fixed"  # fixed | proportional | l2_book
    slippage_bps: float = 1.0  # used by fixed/proportional models
    margin_type: str = "cross"  # cross | isolated
    leverage: float = 1.0
    export_path: str = "backtest_output"

    """
execution/models.py — Configuration for live execution (exchange-agnostic).

The old HyperliquidConfig is preserved as a factory method for backward
compatibility.  New code should use LiveConfig directly and pass exchange
credentials via ExchangeCredentials.
"""


@dataclass
class ExchangeCredentials:
    """
    Credentials for one exchange account.

    Each exchange has different auth requirements:
      • Hyperliquid: account_address + secret_key
      • Binance:     api_key + api_secret
      • Bybit:       api_key + api_secret
      • OKX:         api_key + api_secret + passphrase

    Put whatever you need in `extra` for exchange-specific fields.
    """
    exchange: str = ""             # "hyperliquid", "binance", "bybit", …
    api_key: str = ""              # Binance/Bybit/OKX: API key
    api_secret: str = ""           # Binance/Bybit/OKX: API secret
    account_address: str = ""      # Hyperliquid: wallet address
    secret_key: str = ""           # Hyperliquid: API wallet private key
    testnet: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    # Convenience: symbol map for this exchange (e.g. {"ETH": "ETHUSDT"})
    symbol_map: dict[str, str] = field(default_factory=dict)


@dataclass
class LiveConfig:
    """
    All settings needed to run a strategy live on one or more exchanges.

    Replaces the old Hyperliquid-specific config.  Fully backward-compatible:
    old code that sets account_address/secret_key/use_testnet still works.
    """

    # ── Exchange credentials ─────────────────────────────────────────────
    # Option A: single exchange (backward compat)
    exchange: str = "hyperliquid"
    account_address: str = ""
    secret_key: str = ""
    api_key: str = ""
    api_secret: str = ""
    use_testnet: bool = True

    # Option B: multi-exchange
    exchanges: list[ExchangeCredentials] | None = None

    # ── Market ───────────────────────────────────────────────────────────
    symbol: str = "ETH"
    symbols: list[str] | None = None
    bar_interval_s: int = 60

    # ── Warm-up ──────────────────────────────────────────────────────────
    warmup_bars: int = 200
    max_bars_in_memory: int = 2000

    # ── Position sizing / risk ───────────────────────────────────────────
    risk_per_trade: float = 0.02
    max_position_pct: float = 0.25
    leverage: float = 1.0
    margin_type: str = "cross"

    # ── Risk hard limits ─────────────────────────────────────────────────
    max_open_orders: int = 3
    max_daily_trades: int = 50
    max_daily_loss_pct: float = 5.0
    cooldown_after_loss_s: int = 0

    # ── Order execution ──────────────────────────────────────────────────
    order_type: str = "market"
    limit_chase_bps: float = 2.0
    cancel_stale_after_s: float = 10.0
    reduce_only_exits: bool = True

    # ── Logging ──────────────────────────────────────────────────────────
    log_dir: str = "logs/live"
    log_level: str = "INFO"
    trade_log_csv: str = "live_trades.csv"

    @property
    def active_symbols(self) -> list[str]:
        if self.symbols:
            return list(self.symbols)
        return [self.symbol]

    @property
    def is_multi_asset(self) -> bool:
        return len(self.active_symbols) > 1

    @property
    def is_multi_exchange(self) -> bool:
        return self.exchanges is not None and len(self.exchanges) > 1

    # ── Backward-compat URL helpers (single-exchange Hyperliquid) ────────

    @property
    def api_url(self) -> str:
        if self.exchange == "hyperliquid":
            if self.use_testnet:
                return "https://api.hyperliquid-testnet.xyz"
            return "https://api.hyperliquid.xyz"
        if self.exchange == "binance":
            if self.use_testnet:
                return "https://testnet.binancefuture.com"
            return "https://fapi.binance.com"
        return ""

    @property
    def ws_url(self) -> str:
        if self.exchange == "hyperliquid":
            if self.use_testnet:
                return "wss://api.hyperliquid-testnet.xyz/ws"
            return "wss://api.hyperliquid.xyz/ws"
        if self.exchange == "binance":
            if self.use_testnet:
                return "wss://fstream.binancefuture.com/ws"
            return "wss://fstream.binance.com/ws"
        return ""

    def get_credentials(self) -> list[ExchangeCredentials]:
        """
        Resolve credentials to a flat list.

        If self.exchanges is set, return that.
        Otherwise, build a single-entry list from the top-level fields.
        """
        if self.exchanges:
            return list(self.exchanges)

        cred = ExchangeCredentials(
            exchange=self.exchange,
            api_key=self.api_key,
            api_secret=self.api_secret,
            account_address=self.account_address,
            secret_key=self.secret_key,
            testnet=self.use_testnet,
        )
        return [cred]
    
    """
execution/portfolio.py — Cross-exchange portfolio aggregator.

Tracks positions and equity across multiple exchanges simultaneously.
The engine queries this instead of individual executors when it needs
portfolio-level data (total equity, net exposure, combined positions).

This is what enables hedging across exchanges — for example, going long
ETH on Hyperliquid and short ETH on Binance, and seeing the net position
as flat from the portfolio's perspective.

Usage:
    portfolio = MultiExchangePortfolio()
    portfolio.register(hl_executor)
    portfolio.register(bn_executor)

    # Get combined view
    total_equity = portfolio.total_equity()
    net_pos = portfolio.net_position("ETH")           # aggregated
    positions = portfolio.all_positions()              # per-exchange breakdown
    exposure = portfolio.net_exposure_pct()             # net $ as % of equity
"""


@dataclass
class ExchangePosition:
    """Position on a specific exchange."""
    exchange: str
    symbol: str
    side: Side = Side.FLAT
    size: float = 0.0
    entry_price: float = 0.0
    unrealized_pnl: float = 0.0


@dataclass
class AggregatedPosition:
    """
    Net position across all exchanges for one symbol.

    If you're long 10 ETH on Hyperliquid and short 6 ETH on Binance,
    the net is long 4 ETH.
    """
    symbol: str
    net_size: float = 0.0          # positive = net long, negative = net short
    net_side: Side = Side.FLAT
    gross_long: float = 0.0        # total long size across exchanges
    gross_short: float = 0.0       # total short size across exchanges
    per_exchange: list[ExchangePosition] = field(default_factory=list)

    @property
    def is_hedged(self) -> bool:
        """True if we have opposing positions on different exchanges."""
        return self.gross_long > 0 and self.gross_short > 0

    @property
    def gross_exposure(self) -> float:
        return self.gross_long + self.gross_short