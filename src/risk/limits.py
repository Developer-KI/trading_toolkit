"""
risk/limits.py — Hard risk limits enforced at runtime.

Extracted from execution/single_exchange_engine.py so risk rules live in one place
and can be tested independently of the execution engine.

Dependency: core/ only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core.models import LiveConfig

logger = logging.getLogger(__name__)


@dataclass
class DailyLimitState:
    """Mutable state tracked per trading day."""
    daily_pnl: float = 0.0
    daily_trades: int = 0
    starting_equity: float = 0.0


def check_daily_loss_limit(state: DailyLimitState, config: LiveConfig) -> bool:
    """
    Return True if the daily loss limit has been breached.

    Callers should flatten all positions and set kill_switch = True when
    this returns True.
    """
    if state.starting_equity <= 0:
        return False
    daily_loss_pct = abs(state.daily_pnl) / state.starting_equity * 100
    if state.daily_pnl < 0 and daily_loss_pct >= config.max_daily_loss_pct:
        logger.critical(
            "KILL SWITCH — daily loss %.2f%% exceeds limit %.2f%%",
            daily_loss_pct,
            config.max_daily_loss_pct,
        )
        return True
    return False
