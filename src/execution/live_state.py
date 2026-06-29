"""
execution/live_state.py — Per-asset and portfolio-level state containers.

Extracted from live_engine.py so state data is decoupled from engine logic.
These are pure data holders with no side effects — easy to serialize,
inspect, and eventually replace with C++ structs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.models import Position, Trade
from risk.stops import StopLoss
from .base_executor_feed import BaseFeed, BaseBarBuilder


@dataclass
class _AssetLiveState:
    """Mutable per-(exchange, symbol) live state."""
    symbol: str = ""
    exchange: str = ""
    position: Position = field(default_factory=Position)
    open_trade: Trade | None = None
    stop_loss: StopLoss | None = None
    feed: BaseFeed | None = None
    bar_builder: BaseBarBuilder | None = None


@dataclass
class LiveState:
    """Portfolio-level bookkeeping for a running engine."""
    equity: float = 0.0
    peak_equity: float = 0.0
    starting_equity: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    closed_trades: list[Trade] = field(default_factory=list)
    daily_trades: int = 0
    daily_pnl: float = 0.0
    last_bar_idx: int = 0
    strategy_setup_done: bool = False
    kill_switch: bool = False

    @property
    def position(self) -> Position:
        """Convenience: first position (single-asset engines)."""
        if self.positions:
            return next(iter(self.positions.values()))
        return Position()

    # Backward-compat alias
    @property
    def signal_setup_done(self) -> bool:
        return self.strategy_setup_done

    @signal_setup_done.setter
    def signal_setup_done(self, v: bool):
        self.strategy_setup_done = v
