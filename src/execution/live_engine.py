"""
execution/live_engine.py — Live execution engine (exchange-agnostic).

Drop-in replacement for the old Hyperliquid-only engine.  Now uses
BaseExecutor / BaseFeed / BaseBarBuilder so any exchange works.

Two engine classes:

  LiveEngine          — single-exchange (backward compat)
  MultiExchangeEngine — runs the same strategy across N exchanges,
                        with a shared MultiExchangePortfolio for
                        cross-exchange hedging and net-exposure tracking.
"""

from __future__ import annotations

import copy
import csv
import logging
import os
import select
import sys
import threading
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from abstract.models import (
    BacktestConfig,
    Position,
    Side,
    Trade,
    LiveConfig, 
    ExchangeCredentials
)
from strategy.sizing import Sizer, SizingContext, default_sizer
from strategy.stoploss import (
    StopLoss,
    StopContext,
    default_stop_loss,
)

from strategy.base import Signal, Strategy, StrategyContext, PortfolioTarget,  CrossExchangeStrategy, CrossExchangeContext, MultiExchangeTarget
from strategy.built_in import SingleSignalStrategy
from strategy.universe import Universe
from strategy.overlay import PortfolioOverlay

from .base_executor_feed import BaseExecutor, BaseFeed, BaseBarBuilder, FillResult, MultiExchangePortfolio
from .factory import create_executor, create_feed, create_bar_builder
logger = logging.getLogger(__name__)


# ── Manual kill switch (background stdin listener) ───────────────────────────

KILL_KEY = "q"  # press this key + Enter to trigger manual kill switch


class _ManualKillSwitch:
    """
    Background thread that listens for a keypress to trigger an
    emergency shutdown.  Press the KILL_KEY (default: 'q') then Enter.

    Works on Linux/macOS terminals.  On non-interactive environments
    (systemd, Docker without -it) the thread silently exits and
    the engine runs without manual kill capability.
    """

    def __init__(self, callback, key: str = KILL_KEY):
        self._callback = callback
        self._key = key
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self):
        if not sys.stdin.isatty():
            logger.info("Non-interactive terminal — manual kill switch disabled")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._listen, daemon=True, name="kill-switch",
        )
        self._thread.start()
        logger.info(
            "Manual kill switch active — press '%s' + Enter to flatten & shutdown",
            self._key,
        )

    def stop(self):
        self._running = False

    def _listen(self):
        try:
            while self._running:
                if select.select([sys.stdin], [], [], 1.0)[0]:
                    line = sys.stdin.readline().strip().lower()
                    if line == self._key:
                        logger.critical(
                            "MANUAL KILL — '%s' pressed, flattening all positions",
                            self._key,
                        )
                        self._callback()
                        return
        except Exception:
            pass  # stdin closed / not available


def _sizer_config_shim(config: LiveConfig, equity: float) -> BacktestConfig:
    return BacktestConfig(
        initial_capital=equity,
        risk_per_trade=config.risk_per_trade,
        max_position_pct=config.max_position_pct,
        leverage=config.leverage,
        margin_type=config.margin_type,
    )


# ── Per-asset state ──────────────────────────────────────────────────────────


@dataclass
class _AssetLiveState:
    symbol: str = ""
    exchange: str = ""
    position: Position = field(default_factory=Position)
    open_trade: Trade | None = None
    stop_loss: StopLoss | None = None
    feed: BaseFeed | None = None
    bar_builder: BaseBarBuilder | None = None


# ── Portfolio-level state ────────────────────────────────────────────────────


@dataclass
class LiveState:
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
        if self.positions:
            return next(iter(self.positions.values()))
        return Position()

    @property
    def signal_setup_done(self) -> bool:
        return self.strategy_setup_done

    @signal_setup_done.setter
    def signal_setup_done(self, v: bool):
        self.strategy_setup_done = v


# ══════════════════════════════════════════════════════════════════════════
#  LiveEngine — single-exchange (backward-compatible)
# ══════════════════════════════════════════════════════════════════════════


class LiveEngine:
    """
    Single-exchange live engine.  API is unchanged from the old version.

    The only difference: it now instantiates executor/feed via the factory
    so you can set config.exchange = "binance" and it Just Works™.
    """

    def __init__(
        self,
        signal: Signal | None = None,
        strategy: Strategy | None = None,
        config: LiveConfig | None = None,
        sizer: Sizer | dict[str, Sizer] | None = None,
        stop_loss: StopLoss | dict[str, StopLoss] | None = None,
    ):
        if signal is None and strategy is None:
            raise ValueError("Provide either signal= or strategy=")
        if signal is not None and strategy is not None:
            raise ValueError("Provide signal= or strategy=, not both")

        self.config = config or LiveConfig()
        self._sizer_spec = sizer
        self._stop_loss_spec = stop_loss

        if signal is not None:
            self.strategy = SingleSignalStrategy(signal=signal, symbol=self.config.symbol)
        else:
            self.strategy = strategy

        self._symbols = self.config.active_symbols
        self._is_single = len(self._symbols) == 1

        self.executor: BaseExecutor | None = None
        self._assets: dict[str, _AssetLiveState] = {}
        self.state = LiveState()
        self._lock = threading.Lock()
        self._running = False
        self._last_processed_bar: dict[str, int] = {}
        self._kill_listener = _ManualKillSwitch(self._manual_kill)

        os.makedirs(self.config.log_dir, exist_ok=True)
        self._trade_log_path = os.path.join(self.config.log_dir, self.config.trade_log_csv)
        self._universe = Universe(symbols=self._symbols)

    # ── Component resolution ─────────────────────────────────────────────

    def _resolve_sizer(self, symbol: str) -> Sizer:
        if isinstance(self._sizer_spec, dict):
            return copy.deepcopy(self._sizer_spec.get(symbol, default_sizer()))
        elif self._sizer_spec is not None:
            return copy.deepcopy(self._sizer_spec)
        return default_sizer()

    def _resolve_stop_loss(self, symbol: str) -> StopLoss:
        if isinstance(self._stop_loss_spec, dict):
            return copy.deepcopy(self._stop_loss_spec.get(symbol, default_stop_loss()))
        elif self._stop_loss_spec is not None:
            return copy.deepcopy(self._stop_loss_spec)
        return default_stop_loss()

    def _get_exchange_cred(self) -> ExchangeCredentials:
        """Build ExchangeCredentials from config (backward compat)."""
        creds = self.config.get_credentials()
        return creds[0]

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self):
        self._setup_logging()
        logger.info("=" * 60)
        logger.info(
            "LIVE ENGINE STARTING — %s on %s (%s)",
            self.strategy.__class__.__name__,
            ", ".join(self._symbols),
            self.config.exchange,
        )
        logger.info("=" * 60)

        # 1. Create executor via factory
        cred = self._get_exchange_cred()
        self.executor = create_executor(cred)

        # 2. Set leverage
        for sym in self._symbols:
            self.executor.set_leverage(
                sym, int(self.config.leverage),
                cross=self.config.margin_type == "cross",
            )

        # 3. Sync account state
        self.state.equity = self.executor.get_equity()
        self.state.starting_equity = self.state.equity
        self.state.peak_equity = self.state.equity

        for sym in self._symbols:
            pos = self.executor.get_position(sym)
            self.state.positions[sym] = pos
            logger.info("  %s — %s %.4f @ %.4f", sym, pos.side.name, pos.size, pos.entry_price)

        logger.info("Account equity: $%.2f", self.state.equity)

        # 4. Per-asset: bar builder, feed, stop-loss
        for sym in self._symbols:
            ast = _AssetLiveState(
                symbol=sym,
                exchange=cred.exchange,
                position=self.state.positions[sym],
                stop_loss=self._resolve_stop_loss(sym),
            )
            ast.bar_builder = create_bar_builder(
                interval_s=self.config.bar_interval_s,
                max_bars=self.config.max_bars_in_memory,
                on_bar_close=lambda data, s=sym: self._on_new_bar(s, data),
            )
            self._assets[sym] = ast

        # 5. Warm up
        self._warmup(cred)

        # 6. Start feeds
        self._running = True
        for sym in self._symbols:
            ast = self._assets[sym]
            ast.feed = create_feed(
                exchange=cred.exchange,
                symbol=sym,
                testnet=cred.testnet,
                symbol_map=cred.symbol_map,
            )
            ast.feed.start(
                on_trade=ast.bar_builder.on_trade,
                on_candle=ast.bar_builder.on_candle,
                on_l2=None,
            )

        logger.info("Live engine running on %d symbol(s)", len(self._symbols))

        # Start manual kill switch listener
        self._kill_listener.start()

        # Main heartbeat loop
        self._main_loop()

    def _main_loop(self):
        heartbeat_interval = 60
        last_heartbeat = time.time()
        last_bar_counts: dict[str, int] = {s: self._assets[s].bar_builder.bar_count for s in self._symbols}
        stall_ticks: dict[str, int] = {s: 0 for s in self._symbols}

        try:
            while self._running and not self.state.kill_switch:
                time.sleep(1)
                now = time.time()
                if now - last_heartbeat < heartbeat_interval:
                    continue
                last_heartbeat = now

                parts = []
                for sym in self._symbols:
                    ast = self._assets[sym]
                    bc = ast.bar_builder.bar_count
                    pos = self.state.positions.get(sym, Position())
                    parts.append(f"{sym}: {bc} bars, pos={pos.side.name} {pos.size:.4f}")

                    if bc == last_bar_counts.get(sym, 0):
                        stall_ticks[sym] += 1
                    else:
                        stall_ticks[sym] = 0
                    last_bar_counts[sym] = bc

                    if stall_ticks[sym] >= 3:
                        logger.warning(
                            "WATCHDOG: %s stalled for %d heartbeats",
                            sym, stall_ticks[sym],
                        )

                logger.info("Heartbeat | equity=$%.2f | %s", self.state.equity, " | ".join(parts))
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt — shutting down")
        finally:
            self.stop()

    def stop(self):
        self._running = False
        self._kill_listener.stop()
        for ast in self._assets.values():
            if ast.feed:
                ast.feed.stop()
        logger.info(
            "Engine stopped | trades=%d | closed=%d",
            len(self.state.trades), len(self.state.closed_trades),
        )

    def _manual_kill(self):
        """Called by _ManualKillSwitch when the user presses the kill key."""
        logger.critical("MANUAL KILL SWITCH ACTIVATED — flattening all positions")
        for sym in self._symbols:
            try:
                self.executor.close_position(sym)
                self.executor.cancel_all(sym)
            except Exception as e:
                logger.error("Failed to flatten %s: %s", sym, e)
        self.state.kill_switch = True

    # ── Core bar processing (unchanged logic) ────────────────────────────

    def _on_new_bar(self, trigger_symbol: str, data: pd.DataFrame):
        if not self._running or self.state.kill_switch:
            return
        bar_count = self._assets[trigger_symbol].bar_builder.bar_count
        if bar_count == self._last_processed_bar.get(trigger_symbol, -1):
            return
        self._last_processed_bar[trigger_symbol] = bar_count
        threading.Thread(
            target=self._safe_process_bar,
            args=(trigger_symbol,),
            daemon=True,
            name=f"bar-proc-{trigger_symbol}",
        ).start()

    def _safe_process_bar(self, trigger_symbol: str):
        with self._lock:
            try:
                self._process_bar(trigger_symbol)
            except Exception as e:
                logger.error("Bar processing error [%s]: %s", trigger_symbol, e, exc_info=True)

    def _process_bar(self, trigger_symbol: str):
        # Build universe
        for sym in self._symbols:
            df = self._assets[sym].bar_builder.to_dataframe()
            if len(df) >= 2:
                self._universe.update_asset_bars(sym, df)

        trigger_df = self._assets[trigger_symbol].bar_builder.to_dataframe()
        if len(trigger_df) < 2:
            return

        ts = trigger_df.index[-1] if isinstance(trigger_df.index, pd.DatetimeIndex) else pd.Timestamp.now()

        # Sync equity
        self.state.equity = self.executor.get_equity()
        self.state.peak_equity = max(self.state.peak_equity, self.state.equity)

        if self._check_kill_switch():
            return

        # Sync positions
        for sym in self._symbols:
            exchange_pos = self.executor.get_position(sym)
            local_pos = self.state.positions.get(sym, Position())
            if exchange_pos.side != local_pos.side or abs(exchange_pos.size - local_pos.size) > 1e-8:
                logger.warning(
                    "%s position mismatch — local: %s %.4f, exchange: %s %.4f",
                    sym, local_pos.side.name, local_pos.size, exchange_pos.side.name, exchange_pos.size,
                )
                self.state.positions[sym] = exchange_pos
                self._assets[sym].position = exchange_pos
            else:
                self._assets[sym].position = local_pos

        # Mark-to-market
        for sym in self._symbols:
            ast = self._assets[sym]
            pos = ast.position
            if pos.side != Side.FLAT and pos.size > 0:
                price = ast.bar_builder.last_close
                if not np.isnan(price):
                    direction = 1 if pos.side == Side.LONG else -1
                    pos.unrealized_pnl = (price - pos.entry_price) * pos.size * direction

        # Stop-loss checks
        for sym in self._symbols:
            ast = self._assets[sym]
            pos = ast.position
            if pos.side == Side.FLAT:
                continue
            df = self._universe.ohlcv(sym) if sym in self._universe._assets else None
            if df is None or len(df) < 2:
                continue
            idx = len(df) - 1
            price = df["close"].iat[idx]
            l2_snap = ast.feed.latest_l2 if ast.feed else None
            bar_dict = {c: df[c].iat[idx] for c in df.columns if np.isscalar(df[c].iat[idx])}

            # Inject live funding rate
            funding_snap = self.executor.fetch_funding_rate(sym)
            if funding_snap is not None:
                bar_dict["funding_rate"] = funding_snap.rate
                bar_dict["funding_rate_ann_bps"] = funding_snap.rate_annualized
                if funding_snap.oracle_price > 0:
                    bar_dict["oracle_price"] = funding_snap.oracle_price
                if funding_snap.mark_price > 0:
                    bar_dict["mark_price"] = funding_snap.mark_price

            stop_ctx = StopContext(
                position=pos, bar_idx=idx,
                open=df["open"].iat[idx], high=df["high"].iat[idx],
                low=df["low"].iat[idx], close=price,
                data=df, l2=l2_snap, bar_data=bar_dict,
            )
            ast.stop_loss.update(stop_ctx)
            stop_result = ast.stop_loss.check(stop_ctx)
            if stop_result.triggered:
                logger.info("STOP TRIGGERED on %s: %s", sym, stop_result.reason)
                fill = self._execute_close(sym, pos, stop_result.reason)
                if fill.success:
                    self._record_trade(sym, pos, fill, ts, stop_result.reason)
                    self.state.positions[sym] = Position()
                    ast.position = Position()
                    ast.open_trade = None
                    ast.stop_loss.reset()

        # Strategy setup + generate
        try:
            self.strategy.setup(self._universe)
        except Exception as e:
            logger.error("Strategy setup failed: %s", e)
            return
        self.state.strategy_setup_done = True

        bar_idx = len(self._universe.ohlcv(trigger_symbol)) - 1 if trigger_symbol in self._universe._assets else 0
        ctx = StrategyContext(
            universe=self._universe, bar_idx=bar_idx, timestamp=ts,
            equity=self.state.equity,
            positions={s: self._assets[s].position for s in self._symbols},
            trade_history=self.state.closed_trades,
        )
        target = self.strategy.generate(ctx)

        # Close positions that should be flat/flipped
        for sym in self._symbols:
            ast = self._assets[sym]
            pos = ast.position
            if pos.side == Side.FLAT:
                continue
            desired = target[sym]
            if desired.side == Side.FLAT or desired.side != pos.side:
                reason = desired.reason or "target_flat"
                fill = self._execute_close(sym, pos, reason)
                if fill.success:
                    self._record_trade(sym, pos, fill, ts, reason)
                    self.state.positions[sym] = Position()
                    ast.position = Position()
                    ast.open_trade = None
                    ast.stop_loss.reset()

        # Open new positions
        for sym in self._symbols:
            ast = self._assets[sym]
            pos = ast.position
            if pos.side != Side.FLAT:
                continue
            alloc = target[sym]
            if alloc.side == Side.FLAT or alloc.weight <= 0:
                continue
            if self.state.daily_trades >= self.config.max_daily_trades:
                continue

            price = ast.bar_builder.last_close
            if np.isnan(price) or price <= 0:
                continue
            df = self._universe.ohlcv(sym) if sym in self._universe._assets else None
            if df is None or len(df) < 2:
                continue
            idx = len(df) - 1
            l2_snap = ast.feed.latest_l2 if ast.feed else None
            bar_dict = {c: df[c].iat[idx] for c in df.columns if np.isscalar(df[c].iat[idx])}

            # Inject live funding rate
            funding_snap = self.executor.fetch_funding_rate(sym)
            if funding_snap is not None:
                bar_dict["funding_rate"] = funding_snap.rate
                bar_dict["funding_rate_ann_bps"] = funding_snap.rate_annualized
                if funding_snap.oracle_price > 0:
                    bar_dict["oracle_price"] = funding_snap.oracle_price
                if funding_snap.mark_price > 0:
                    bar_dict["mark_price"] = funding_snap.mark_price

            sizer_cfg = _sizer_config_shim(self.config, self.state.equity)
            sizing_ctx = SizingContext(
                equity=self.state.equity, price=price,
                signal=alloc.to_signal_result(), config=sizer_cfg,
                position=pos, data=df, bar_idx=idx,
                trade_history=self.state.closed_trades,
                l2=l2_snap, bar_data=bar_dict,
            )
            sizer = self._resolve_sizer(sym)
            size = sizer.compute(sizing_ctx)

            if self._is_single:
                max_notional = self.state.equity * self.config.max_position_pct * self.config.leverage
            else:
                max_notional = self.state.equity * alloc.weight * self.config.leverage
            max_size = max_notional / price if price > 0 else 0
            size = min(size, max_size)
            if size <= 0:
                continue

            logger.info("OPENING %s %.4f %s @ ~%.4f", alloc.side.name, size, sym, price)
            fill = self._execute_open(sym, alloc.side, size)
            if fill.success:
                new_pos = Position(
                    side=alloc.side,
                    size=fill.filled_size or size,
                    entry_price=fill.fill_price or price,
                    entry_timestamp=ts,
                )
                self.state.positions[sym] = new_pos
                ast.position = new_pos

                stop_ctx = StopContext(
                    position=new_pos, bar_idx=idx,
                    open=df["open"].iat[idx], high=df["high"].iat[idx],
                    low=df["low"].iat[idx], close=price,
                    data=df, l2=l2_snap, bar_data=bar_dict,
                )
                ast.stop_loss.on_entry(new_pos, stop_ctx)

                trade = Trade(
                    timestamp=ts, side=alloc.side,
                    size=fill.filled_size or size,
                    entry_price=fill.fill_price or price,
                    reason_entry=alloc.reason,
                    signal_values=alloc.meta,
                    meta={"symbol": sym, "exchange": self.config.exchange},
                )
                self.state.trades.append(trade)
                ast.open_trade = trade
                self.state.daily_trades += 1
                self.strategy.on_fill(sym, alloc.side, fill.filled_size or size, fill.fill_price or price)

    # ── Execution helpers ────────────────────────────────────────────────

    def _execute_open(self, symbol, side, size) -> FillResult:
        if self.config.order_type == "limit":
            mid = self.executor.get_mid_price(symbol)
            offset = mid * self.config.limit_chase_bps / 1e4
            px = mid + offset if side == Side.LONG else mid - offset
            return self.executor.limit_order(symbol, side, size, px)
        return self.executor.market_order(symbol, side, size)

    def _execute_close(self, symbol, pos, reason) -> FillResult:
        close_side = Side.SHORT if pos.side == Side.LONG else Side.LONG
        return self.executor.market_order(
            symbol, close_side, pos.size,
            reduce_only=self.config.reduce_only_exits,
        )

    # ── Trade recording ──────────────────────────────────────────────────

    def _record_trade(self, symbol, pos, fill, ts, reason):
        exit_price = fill.fill_price if fill.fill_price > 0 else self._assets[symbol].bar_builder.last_close
        direction = 1 if pos.side == Side.LONG else -1
        pnl = (exit_price - pos.entry_price) * pos.size * direction

        ast = self._assets[symbol]
        if ast.open_trade is not None and ast.open_trade.exit_price is None:
            t = ast.open_trade
            t.exit_price = exit_price
            t.exit_timestamp = ts
            t.pnl = pnl
            t.pnl_pct = pnl / (pos.entry_price * pos.size) if pos.entry_price * pos.size > 0 else 0
            t.fees = 0.0
            t.reason_exit = reason
            t.meta["symbol"] = symbol
            t.meta["exchange"] = self.config.exchange
            self.state.closed_trades.append(t)

        self.state.daily_pnl += pnl
        self.state.equity = self.executor.get_equity()

        logger.info(
            "TRADE CLOSED: %s %s %.4f | entry=%.4f exit=%.4f | pnl=$%.2f | reason=%s",
            pos.side.name, symbol, pos.size, pos.entry_price, exit_price, pnl, reason,
        )
        self._write_trade_csv(self.state.closed_trades[-1] if self.state.closed_trades else None)

    def _write_trade_csv(self, trade: Trade | None):
        if trade is None:
            return
        file_exists = os.path.exists(self._trade_log_path)
        row = trade.to_dict()
        with open(self._trade_log_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=row.keys())
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)

    # ── Risk management ──────────────────────────────────────────────────

    def _check_kill_switch(self) -> bool:
        if self.state.starting_equity <= 0:
            return False
        daily_loss_pct = abs(self.state.daily_pnl) / self.state.starting_equity * 100
        if self.state.daily_pnl < 0 and daily_loss_pct >= self.config.max_daily_loss_pct:
            logger.critical("KILL SWITCH — daily loss %.2f%% exceeds limit", daily_loss_pct)
            for sym in self._symbols:
                self.executor.close_position(sym)
                self.executor.cancel_all(sym)
            self.state.kill_switch = True
            return True
        return False

    # ── Warm-up ──────────────────────────────────────────────────────────

    def _warmup(self, cred: ExchangeCredentials):
        for sym in self._symbols:
            logger.info("Fetching %d warmup bars for %s...", self.config.warmup_bars, sym)
            try:
                now_ms = int(time.time() * 1000)
                start_ms = now_ms - self.config.warmup_bars * self.config.bar_interval_s * 1000
                rows = self.executor.fetch_historical_candles(sym, "1m", start_ms, now_ms)
                if rows:
                    df = pd.DataFrame(rows).set_index("timestamp")
                    self._assets[sym].bar_builder.seed(df)
                    self._universe.update_asset_bars(sym, df)
                    logger.info("  %s warmup: %d bars", sym, len(df))
                else:
                    logger.warning("  %s: no warmup candles", sym)
            except Exception as e:
                logger.warning("  %s warmup failed: %s", sym, e)

    def _setup_logging(self):
        log_fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        logging.basicConfig(
            level=getattr(logging, self.config.log_level.upper(), logging.INFO),
            format=log_fmt,
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler(os.path.join(self.config.log_dir, "live_engine.log"), mode="a"),
            ],
        )


# ══════════════════════════════════════════════════════════════════════════
#  MultiExchangeEngine
# ══════════════════════════════════════════════════════════════════════════


class MultiExchangeEngine:
    """
    Run strategies across multiple exchanges with a shared portfolio.

    Supports three modes:

    Mode A — Cross-exchange strategy (funding arb, stat arb, hedging):
        One CrossExchangeStrategy sees all exchanges and generates a
        MultiExchangeTarget that explicitly routes each leg.

        engine = MultiExchangeEngine(
            cross_strategy=FundingArbStrategy(...),
            config=config,
        )

    Mode B — Per-exchange strategies (independent signals):
        Each exchange runs its own Strategy independently.  A shared
        MultiExchangePortfolio provides cross-exchange awareness, and
        an optional PortfolioOverlay can enforce portfolio-level limits.

        engine = MultiExchangeEngine(
            per_exchange_strategies={
                "hyperliquid": MomentumStrategy(...),
                "binance":     MeanReversionStrategy(...),
            },
            config=config,
            overlay=NetExposureOverlay(max_net_weight=0.3),
        )

    Mode C — Single strategy on all exchanges:
        Like the old API.  One Strategy runs on the primary exchange's
        data, and allocations default to the primary exchange unless
        a PortfolioOverlay redirects them.

        engine = MultiExchangeEngine(
            strategy=MyStrategy(...),
            config=config,
        )

    Architecture:
        Per-exchange:
            executor → set_leverage, get_position, orders
            per-symbol feed → bar_builder → on_bar_close
            universe (built from bar builders)

        Shared:
            MultiExchangePortfolio (aggregated equity + positions)
            LiveState (trades, PnL, kill switch)
            PortfolioOverlay (optional risk filter)

        Bar processing:
            Any bar close on any exchange triggers processing.
            Cross-exchange: re-run strategy with all universes.
            Per-exchange: re-run only that exchange's strategy.
            Overlay: applied after strategy, before execution.
    """

    def __init__(
        self,
        # Mode A: cross-exchange strategy
        cross_strategy: CrossExchangeStrategy | None = None,
        # Mode B: per-exchange strategies
        per_exchange_strategies: dict[str, Strategy] | None = None,
        # Optional cross-exchange risk overlay
        overlay: PortfolioOverlay | None = None,
        config: LiveConfig | None = None,
        sizer: Sizer | dict[str, Sizer] | None = None,
        stop_loss: StopLoss | dict[str, StopLoss] | None = None,
    ):
        modes = sum(x is not None for x in [
            cross_strategy, per_exchange_strategies
        ])
        if modes == 0:
            raise ValueError(
                "Provide one of: cross_strategy=, per_exchange_strategies=, or strategy="
            )
        if modes > 1:
            raise ValueError(
                "Provide only one of: cross_strategy=, per_exchange_strategies="
            )

        self.config = config or LiveConfig()
        self._sizer_spec = sizer
        self._stop_loss_spec = stop_loss
        self.overlay = overlay

        self._creds = self.config.get_credentials()
        if len(self._creds) < 2:
            raise ValueError(
                "MultiExchangeEngine requires >= 2 exchanges in config.exchanges"
            )

        self._symbols = self.config.active_symbols
        self._exchange_names = [c.exchange for c in self._creds]

        # Determine mode
        if cross_strategy is not None:
            self._mode = "cross"
            self.cross_strategy = cross_strategy
            self._per_exchange_strategies: dict[str, Strategy] = {}
        elif per_exchange_strategies is not None:
            self._mode = "per_exchange"
            self.cross_strategy = None
            self._per_exchange_strategies = per_exchange_strategies

        # Per-exchange executors
        self._executors: dict[str, BaseExecutor] = {}
        # Per (exchange, symbol) → asset state
        self._assets: dict[tuple[str, str], _AssetLiveState] = {}
        # Per-exchange universes
        self._universes: dict[str, Universe] = {}

        self.portfolio = MultiExchangePortfolio()
        self.state = LiveState()

        self._lock = threading.Lock()
        self._running = False
        self._last_processed_bar: dict[str, int] = {}
        self._kill_listener = _ManualKillSwitch(self._manual_kill)

        os.makedirs(self.config.log_dir, exist_ok=True)
        self._trade_log_path = os.path.join(
            self.config.log_dir, self.config.trade_log_csv,
        )

    @property
    def primary_exchange(self) -> str:
        return self._exchange_names[0]

    # ── Component resolution ─────────────────────────────────────────

    def _resolve_sizer(self, symbol: str) -> Sizer:
        if isinstance(self._sizer_spec, dict):
            return copy.deepcopy(self._sizer_spec.get(symbol, default_sizer()))
        elif self._sizer_spec is not None:
            return copy.deepcopy(self._sizer_spec)
        return default_sizer()

    def _resolve_stop_loss(self, symbol: str) -> StopLoss:
        if isinstance(self._stop_loss_spec, dict):
            return copy.deepcopy(
                self._stop_loss_spec.get(symbol, default_stop_loss())
            )
        elif self._stop_loss_spec is not None:
            return copy.deepcopy(self._stop_loss_spec)
        return default_stop_loss()

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self):
        self._setup_logging()

        mode_label = {
            "cross": f"CrossExchange: {self.cross_strategy.__class__.__name__}",
            "per_exchange": "PerExchange: " + ", ".join(
                f"{ex}={s.__class__.__name__}"
                for ex, s in self._per_exchange_strategies.items()
            )
        }[self._mode]

        logger.info("=" * 60)
        logger.info(
            "MULTI-EXCHANGE ENGINE — %s", mode_label,
        )
        logger.info(
            "  Symbols: %s | Exchanges: %s",
            ", ".join(self._symbols),
            ", ".join(self._exchange_names),
        )
        if self.overlay:
            logger.info("  Overlay: %s", self.overlay.__class__.__name__)
        logger.info("=" * 60)

        # 1. Create executors + portfolios
        for cred in self._creds:
            ex = create_executor(cred)
            self._executors[cred.exchange] = ex
            self.portfolio.register(ex)

            for sym in self._symbols:
                ex.set_leverage(
                    sym, int(self.config.leverage),
                    cross=self.config.margin_type == "cross",
                )

        # 2. Sync equity
        self.portfolio.refresh_equity()
        self.state.equity = self.portfolio.total_equity()
        self.state.starting_equity = self.state.equity
        self.state.peak_equity = self.state.equity
        logger.info("Total equity: $%.2f", self.state.equity)

        for name, eq in self.portfolio.equity_breakdown().items():
            logger.info("  %s: $%.2f", name, eq)

        # 3. Per-exchange: universe, bar builders, feeds
        for cred in self._creds:
            universe = Universe(symbols=self._symbols)
            self._universes[cred.exchange] = universe

            for sym in self._symbols:
                key = (cred.exchange, sym)
                ast = _AssetLiveState(
                    symbol=sym,
                    exchange=cred.exchange,
                    position=self._executors[cred.exchange].get_position(sym),
                    stop_loss=self._resolve_stop_loss(sym),
                )
                ast.bar_builder = create_bar_builder(
                    interval_s=self.config.bar_interval_s,
                    max_bars=self.config.max_bars_in_memory,
                    on_bar_close=lambda data, e=cred.exchange, s=sym: (
                        self._on_new_bar(e, s, data)
                    ),
                )
                self._assets[key] = ast

        # 4. Sync positions
        for cred in self._creds:
            for sym in self._symbols:
                pos = self._executors[cred.exchange].get_position(sym)
                logger.info(
                    "  %s/%s: %s %.4f @ %.4f",
                    cred.exchange, sym, pos.side.name, pos.size, pos.entry_price,
                )

        # 5. Warm up
        self._warmup()

        # 6. Start feeds
        self._running = True
        for cred in self._creds:
            for sym in self._symbols:
                key = (cred.exchange, sym)
                ast = self._assets[key]
                ast.feed = create_feed(
                    exchange=cred.exchange,
                    symbol=sym,
                    testnet=cred.testnet,
                    symbol_map=cred.symbol_map,
                )
                ast.feed.start(
                    on_trade=ast.bar_builder.on_trade,
                    on_candle=ast.bar_builder.on_candle,
                )

        logger.info(
            "Multi-exchange engine running | %d exchange(s) × %d symbol(s)",
            len(self._exchange_names), len(self._symbols),
        )

        # Start manual kill switch listener
        self._kill_listener.start()

        # Main loop
        self._main_loop()

    def stop(self):
        self._running = False
        self._kill_listener.stop()
        for ast in self._assets.values():
            if ast.feed:
                ast.feed.stop()
        logger.info(
            "Multi-exchange engine stopped | trades=%d | closed=%d",
            len(self.state.trades), len(self.state.closed_trades),
        )

    def _manual_kill(self):
        """Called by _ManualKillSwitch when the user presses the kill key."""
        logger.critical("MANUAL KILL SWITCH ACTIVATED — flattening all positions")
        self.portfolio.flatten_all(self._symbols)
        self.state.kill_switch = True

    def _main_loop(self):
        heartbeat_interval = 60
        last_heartbeat = time.time()
        try:
            while self._running and not self.state.kill_switch:
                time.sleep(1)
                now = time.time()
                if now - last_heartbeat < heartbeat_interval:
                    continue
                last_heartbeat = now

                self.portfolio.refresh_equity()
                self.state.equity = self.portfolio.total_equity()

                parts = []
                for ex_name in self._exchange_names:
                    for sym in self._symbols:
                        key = (ex_name, sym)
                        ast = self._assets.get(key)
                        bc = ast.bar_builder.bar_count if ast else 0
                        pos = ast.position if ast else Position()
                        parts.append(
                            f"{ex_name}/{sym}: {bc}bars "
                            f"{pos.side.name} {pos.size:.4f}"
                        )

                logger.info(
                    "Heartbeat | equity=$%.2f | %s",
                    self.state.equity, " | ".join(parts),
                )
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt")
        finally:
            self.stop()

    # ── Bar processing ───────────────────────────────────────────────

    def _on_new_bar(
        self,
        exchange: str,
        trigger_symbol: str,
        data: pd.DataFrame,
    ):
        """Called by bar builder on any exchange when a bar closes."""
        if not self._running or self.state.kill_switch:
            return
        key = (exchange, trigger_symbol)
        ast = self._assets.get(key)
        if ast is None:
            return
        bc = ast.bar_builder.bar_count
        dedup_key = f"{exchange}:{trigger_symbol}"
        if bc == self._last_processed_bar.get(dedup_key, -1):
            return
        self._last_processed_bar[dedup_key] = bc

        threading.Thread(
            target=self._safe_process_bar,
            args=(exchange, trigger_symbol),
            daemon=True,
            name=f"mxe-{exchange}-{trigger_symbol}",
        ).start()

    def _safe_process_bar(self, exchange: str, trigger_symbol: str):
        with self._lock:
            try:
                self._process_bar(exchange, trigger_symbol)
            except Exception as e:
                logger.error(
                    "Bar processing error [%s/%s]: %s",
                    exchange, trigger_symbol, e, exc_info=True,
                )

    def _process_bar(self, trigger_exchange: str, trigger_symbol: str):
        # ── 1. Update all universes from bar builders ────────────────
        for ex_name in self._exchange_names:
            for sym in self._symbols:
                key = (ex_name, sym)
                ast = self._assets.get(key)
                if ast:
                    df = ast.bar_builder.to_dataframe()
                    if len(df) >= 2:
                        self._universes[ex_name].update_asset_bars(sym, df)

        # Need at least 2 bars on trigger
        trigger_key = (trigger_exchange, trigger_symbol)
        trigger_df = self._assets[trigger_key].bar_builder.to_dataframe()
        if len(trigger_df) < 2:
            return
        ts = (
            trigger_df.index[-1]
            if isinstance(trigger_df.index, pd.DatetimeIndex)
            else pd.Timestamp.now()
        )

        # ── 2. Refresh portfolio equity ──────────────────────────────
        self.portfolio.refresh_equity()
        self.state.equity = self.portfolio.total_equity()
        self.state.peak_equity = max(self.state.peak_equity, self.state.equity)

        # ── 3. Kill switch check ─────────────────────────────────────
        if self._check_kill_switch():
            return

        # ── 4. Sync positions from all exchanges ────────────────────
        all_positions: dict[str, dict[str, Position]] = {}
        for ex_name in self._exchange_names:
            ex = self._executors[ex_name]
            ex_pos = {}
            for sym in self._symbols:
                pos = ex.get_position(sym)
                ex_pos[sym] = pos
                key = (ex_name, sym)
                if key in self._assets:
                    self._assets[key].position = pos
            all_positions[ex_name] = ex_pos

        # Also update state.positions (primary exchange for backward compat)
        self.state.positions = all_positions.get(self.primary_exchange, {})

        # ── 5. Stop-loss checks (per exchange, per symbol) ───────────
        for ex_name in self._exchange_names:
            for sym in self._symbols:
                key = (ex_name, sym)
                ast = self._assets.get(key)
                if ast is None:
                    continue
                pos = ast.position
                if pos.side == Side.FLAT:
                    continue

                universe = self._universes[ex_name]
                try:
                    df = universe.ohlcv(sym)
                except KeyError:
                    continue
                if len(df) < 2:
                    continue
                idx = len(df) - 1
                price = df["close"].iat[idx]
                l2_snap = ast.feed.latest_l2 if ast.feed else None
                bar_dict = {
                    c: df[c].iat[idx]
                    for c in df.columns
                    if np.isscalar(df[c].iat[idx])
                }

                # Inject live funding rate
                try:
                    executor = self._executors[ex_name]
                    funding_snap = executor.fetch_funding_rate(sym)
                    if funding_snap is not None:
                        bar_dict["funding_rate"] = funding_snap.rate
                        bar_dict["funding_rate_ann_bps"] = funding_snap.rate_annualized
                        if funding_snap.oracle_price > 0:
                            bar_dict["oracle_price"] = funding_snap.oracle_price
                        if funding_snap.mark_price > 0:
                            bar_dict["mark_price"] = funding_snap.mark_price
                except Exception:
                    pass

                stop_ctx = StopContext(
                    position=pos, bar_idx=idx,
                    open=df["open"].iat[idx], high=df["high"].iat[idx],
                    low=df["low"].iat[idx], close=price,
                    data=df, l2=l2_snap, bar_data=bar_dict,
                )
                ast.stop_loss.update(stop_ctx)
                stop_result = ast.stop_loss.check(stop_ctx)

                if stop_result.triggered:
                    logger.info(
                        "STOP on %s/%s: %s", ex_name, sym, stop_result.reason,
                    )
                    executor = self._executors[ex_name]
                    close_side = (
                        Side.SHORT if pos.side == Side.LONG else Side.LONG
                    )
                    fill = executor.market_order(
                        sym, close_side, pos.size, reduce_only=True,
                    )
                    if fill.success:
                        self._record_trade(
                            ex_name, sym, pos, fill, ts, stop_result.reason,
                        )
                        ast.position = Position()
                        ast.open_trade = None
                        ast.stop_loss.reset()

        # ── 6. Generate targets (mode-dependent) ────────────────────
        merged_target = self._generate_targets(
            trigger_exchange, trigger_symbol, ts, all_positions,
        )
        if merged_target is None:
            return

        # ── 7. Apply overlay (if present) ────────────────────────────
        if self.overlay:
            cross_ctx = self._build_cross_ctx(ts, all_positions)
            merged_target = self.overlay.adjust(merged_target, cross_ctx)

        # ── 8. Execute on each exchange ──────────────────────────────
        self._execute_target(merged_target, ts, all_positions)

    # ── Target generation (per mode) ─────────────────────────────────

    def _generate_targets(
        self,
        trigger_exchange: str,
        trigger_symbol: str,
        ts: pd.Timestamp,
        all_positions: dict[str, dict[str, Position]],
    ) -> MultiExchangeTarget | None:
        """
        Generate a MultiExchangeTarget according to the engine's mode.

        Returns None if strategies can't run yet.
        """

        if self._mode == "cross":
            return self._generate_cross(ts, all_positions)

        elif self._mode == "per_exchange":
            return self._generate_per_exchange(
                trigger_exchange, ts, all_positions,
            )

        elif self._mode == "single":
            return self._generate_per_exchange(
                trigger_exchange, ts, all_positions,
            )

        return None

    def _generate_cross(
        self,
        ts: pd.Timestamp,
        all_positions: dict[str, dict[str, Position]],
    ) -> MultiExchangeTarget | None:
        """Mode A: one CrossExchangeStrategy sees everything."""
        try:
            self.cross_strategy.setup(self._universes)
        except Exception as e:
            logger.error("CrossStrategy setup failed: %s", e)
            return None

        ctx = self._build_cross_ctx(ts, all_positions)
        target = self.cross_strategy.generate(ctx)
        return target

    def _generate_per_exchange(
        self,
        trigger_exchange: str,
        ts: pd.Timestamp,
        all_positions: dict[str, dict[str, Position]],
    ) -> MultiExchangeTarget | None:
        """Mode B/C: run each exchange's strategy independently, merge."""
        per_exchange_targets: dict[str, PortfolioTarget] = {}

        for ex_name, strat in self._per_exchange_strategies.items():
            universe = self._universes.get(ex_name)
            if universe is None:
                continue

            try:
                strat.setup(universe)
            except Exception as e:
                logger.error("%s strategy setup failed: %s", ex_name, e)
                continue

            # Build StrategyContext for this exchange
            bar_idx = 0
            for sym in self._symbols:
                try:
                    bar_idx = max(
                        bar_idx, len(universe.ohlcv(sym)) - 1,
                    )
                except KeyError:
                    pass

            ctx = StrategyContext(
                universe=universe,
                bar_idx=bar_idx,
                timestamp=ts,
                equity=self.portfolio.total_equity(),
                positions=all_positions.get(ex_name, {}),
                trade_history=self.state.closed_trades,
            )
            pt = strat.generate(ctx)
            per_exchange_targets[ex_name] = pt

        # Merge into MultiExchangeTarget
        return MultiExchangeTarget.from_per_exchange(per_exchange_targets)

    def _build_cross_ctx(
        self,
        ts: pd.Timestamp,
        all_positions: dict[str, dict[str, Position]],
    ) -> CrossExchangeContext:
        """Build the context object for CrossExchangeStrategy / PortfolioOverlay."""
        # Bar index: use the minimum across all exchanges to stay safe
        bar_idx = 0
        for u in self._universes.values():
            for sym in self._symbols:
                try:
                    bar_idx = max(bar_idx, len(u.ohlcv(sym)) - 1)
                except KeyError:
                    pass

        return CrossExchangeContext(
            universes=self._universes,
            bar_idx=bar_idx,
            timestamp=ts,
            total_equity=self.portfolio.total_equity(),
            equity_by_exchange=self.portfolio.equity_breakdown(),
            positions=all_positions,
            portfolio=self.portfolio,
            trade_history=self.state.closed_trades,
        )

    # ── Execution ────────────────────────────────────────────────────

    def _execute_target(
        self,
        target: MultiExchangeTarget,
        ts: pd.Timestamp,
        all_positions: dict[str, dict[str, Position]],
    ):
        """
        Execute a MultiExchangeTarget on all exchanges.

        Phase 1: close positions that should be flat or flipped.
        Phase 2: open new positions.
        """
        # Phase 1: closes
        for ex_name in self._exchange_names:
            executor = self._executors[ex_name]
            for sym in self._symbols:
                key = (ex_name, sym)
                ast = self._assets.get(key)
                if ast is None:
                    continue
                pos = ast.position
                if pos.side == Side.FLAT:
                    continue

                desired = target[(ex_name, sym)]
                should_close = (
                    desired.side == Side.FLAT or desired.side != pos.side
                )
                if not should_close:
                    continue

                reason = desired.reason or "target_flat"
                close_side = (
                    Side.SHORT if pos.side == Side.LONG else Side.LONG
                )
                logger.info(
                    "CLOSING %s %s %.4f on %s — %s",
                    pos.side.name, sym, pos.size, ex_name, reason,
                )
                fill = executor.market_order(
                    sym, close_side, pos.size, reduce_only=True,
                )
                if fill.success:
                    self._record_trade(ex_name, sym, pos, fill, ts, reason)
                    ast.position = Position()
                    ast.open_trade = None
                    ast.stop_loss.reset()

        # Phase 2: opens
        for ex_name in self._exchange_names:
            executor = self._executors[ex_name]
            for sym in self._symbols:
                key = (ex_name, sym)
                ast = self._assets.get(key)
                if ast is None:
                    continue
                pos = ast.position
                if pos.side != Side.FLAT:
                    continue

                alloc = target[(ex_name, sym)]
                if alloc.side == Side.FLAT or alloc.weight <= 0:
                    continue

                if self.state.daily_trades >= self.config.max_daily_trades:
                    logger.warning("Daily trade limit reached")
                    continue

                price = ast.bar_builder.last_close
                if np.isnan(price) or price <= 0:
                    continue

                # Size from weight
                max_notional = (
                    self.state.equity * alloc.weight * self.config.leverage
                )
                size = max_notional / price if price > 0 else 0

                # Also run through sizer if data available
                universe = self._universes.get(ex_name)
                if universe:
                    try:
                        df = universe.ohlcv(sym)
                        idx = len(df) - 1
                        l2_snap = ast.feed.latest_l2 if ast.feed else None
                        bar_dict = {
                            c: df[c].iat[idx]
                            for c in df.columns
                            if np.isscalar(df[c].iat[idx])
                        }

                        # Inject live funding rate
                        try:
                            _ex = self._executors[ex_name]
                            _fs = _ex.fetch_funding_rate(sym)
                            if _fs is not None:
                                bar_dict["funding_rate"] = _fs.rate
                                bar_dict["funding_rate_ann_bps"] = _fs.rate_annualized
                                if _fs.oracle_price > 0:
                                    bar_dict["oracle_price"] = _fs.oracle_price
                                if _fs.mark_price > 0:
                                    bar_dict["mark_price"] = _fs.mark_price
                        except Exception:
                            pass

                        sizer_cfg = _sizer_config_shim(
                            self.config, self.state.equity,
                        )
                        sizing_ctx = SizingContext(
                            equity=self.state.equity,
                            price=price,
                            signal=alloc.to_signal_result(),
                            config=sizer_cfg,
                            position=pos,
                            data=df,
                            bar_idx=idx,
                            trade_history=self.state.closed_trades,
                            l2=l2_snap,
                            bar_data=bar_dict,
                        )
                        sizer = self._resolve_sizer(sym)
                        sizer_size = sizer.compute(sizing_ctx)
                        size = min(size, sizer_size)
                    except Exception:
                        pass  # Fall back to weight-based sizing

                if size <= 0:
                    continue

                logger.info(
                    "OPENING %s %.4f %s on %s @ ~%.4f (w=%.2f)",
                    alloc.side.name, size, sym, ex_name, price, alloc.weight,
                )
                fill = executor.market_order(sym, alloc.side, size)
                if fill.success:
                    new_pos = Position(
                        side=alloc.side,
                        size=fill.filled_size or size,
                        entry_price=fill.fill_price or price,
                        entry_timestamp=ts,
                    )
                    ast.position = new_pos

                    # Stop-loss init
                    try:
                        df = self._universes[ex_name].ohlcv(sym)
                        idx = len(df) - 1
                        l2_snap = ast.feed.latest_l2 if ast.feed else None
                        bar_dict = {
                            c: df[c].iat[idx]
                            for c in df.columns
                            if np.isscalar(df[c].iat[idx])
                        }

                        # Inject live funding rate
                        try:
                            _fs = self._executors[ex_name].fetch_funding_rate(sym)
                            if _fs is not None:
                                bar_dict["funding_rate"] = _fs.rate
                                bar_dict["funding_rate_ann_bps"] = _fs.rate_annualized
                                if _fs.oracle_price > 0:
                                    bar_dict["oracle_price"] = _fs.oracle_price
                                if _fs.mark_price > 0:
                                    bar_dict["mark_price"] = _fs.mark_price
                        except Exception:
                            pass

                        stop_ctx = StopContext(
                            position=new_pos, bar_idx=idx,
                            open=df["open"].iat[idx],
                            high=df["high"].iat[idx],
                            low=df["low"].iat[idx],
                            close=price,
                            data=df, l2=l2_snap, bar_data=bar_dict,
                        )
                        ast.stop_loss.on_entry(new_pos, stop_ctx)
                    except Exception:
                        pass

                    trade = Trade(
                        timestamp=ts, side=alloc.side,
                        size=fill.filled_size or size,
                        entry_price=fill.fill_price or price,
                        reason_entry=alloc.reason,
                        signal_values=alloc.meta,
                        meta={"symbol": sym, "exchange": ex_name},
                    )
                    self.state.trades.append(trade)
                    ast.open_trade = trade
                    self.state.daily_trades += 1

                    # Callback
                    if self._mode == "cross" and self.cross_strategy:
                        self.cross_strategy.on_fill(
                            ex_name, sym, alloc.side,
                            fill.filled_size or size,
                            fill.fill_price or price,
                        )
                    elif ex_name in self._per_exchange_strategies:
                        self._per_exchange_strategies[ex_name].on_fill(
                            sym, alloc.side,
                            fill.filled_size or size,
                            fill.fill_price or price,
                        )

    # ── Trade recording ──────────────────────────────────────────────

    def _record_trade(
        self,
        exchange: str,
        symbol: str,
        pos: Position,
        fill: FillResult,
        ts: pd.Timestamp,
        reason: str,
    ):
        key = (exchange, symbol)
        ast = self._assets.get(key)
        exit_price = (
            fill.fill_price
            if fill.fill_price > 0
            else (ast.bar_builder.last_close if ast else 0.0)
        )
        direction = 1 if pos.side == Side.LONG else -1
        pnl = (exit_price - pos.entry_price) * pos.size * direction

        if ast and ast.open_trade and ast.open_trade.exit_price is None:
            t = ast.open_trade
            t.exit_price = exit_price
            t.exit_timestamp = ts
            t.pnl = pnl
            t.pnl_pct = (
                pnl / (pos.entry_price * pos.size)
                if pos.entry_price * pos.size > 0 else 0
            )
            t.fees = 0.0
            t.reason_exit = reason
            t.meta["symbol"] = symbol
            t.meta["exchange"] = exchange
            self.state.closed_trades.append(t)

        self.state.daily_pnl += pnl
        logger.info(
            "TRADE CLOSED: %s %s %.4f on %s | "
            "entry=%.4f exit=%.4f | pnl=$%.2f | %s",
            pos.side.name, symbol, pos.size, exchange,
            pos.entry_price, exit_price, pnl, reason,
        )

        self._write_trade_csv(
            self.state.closed_trades[-1]
            if self.state.closed_trades else None,
        )

    def _write_trade_csv(self, trade: Trade | None):
        if trade is None:
            return
        file_exists = os.path.exists(self._trade_log_path)
        row = trade.to_dict()
        with open(self._trade_log_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=row.keys())
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)

    # ── Risk management ──────────────────────────────────────────────

    def _check_kill_switch(self) -> bool:
        if self.state.starting_equity <= 0:
            return False
        daily_loss_pct = (
            abs(self.state.daily_pnl) / self.state.starting_equity * 100
        )
        if (
            self.state.daily_pnl < 0
            and daily_loss_pct >= self.config.max_daily_loss_pct
        ):
            logger.critical(
                "KILL SWITCH — daily loss %.2f%% exceeds limit",
                daily_loss_pct,
            )
            self.portfolio.flatten_all(self._symbols)
            self.state.kill_switch = True
            return True
        return False

    # ── Warm-up ──────────────────────────────────────────────────────

    def _warmup(self):
        """Fetch historical candles for all exchanges and seed bar builders."""
        for cred in self._creds:
            executor = self._executors[cred.exchange]
            for sym in self._symbols:
                try:
                    now_ms = int(time.time() * 1000)
                    start_ms = (
                        now_ms
                        - self.config.warmup_bars
                        * self.config.bar_interval_s
                        * 1000
                    )
                    rows = executor.fetch_historical_candles(
                        sym, "1m", start_ms, now_ms,
                    )
                    if rows:
                        df = pd.DataFrame(rows).set_index("timestamp")
                        key = (cred.exchange, sym)
                        if key in self._assets:
                            self._assets[key].bar_builder.seed(df)
                        self._universes[cred.exchange].update_asset_bars(
                            sym, df,
                        )
                        logger.info(
                            "  %s/%s warmup: %d bars",
                            cred.exchange, sym, len(df),
                        )
                    else:
                        logger.warning(
                            "  %s/%s: no warmup candles", cred.exchange, sym,
                        )
                except Exception as e:
                    logger.warning(
                        "  %s/%s warmup failed: %s", cred.exchange, sym, e,
                    )

    # ── Logging ──────────────────────────────────────────────────────

    def _setup_logging(self):
        log_fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        logging.basicConfig(
            level=getattr(
                logging, self.config.log_level.upper(), logging.INFO,
            ),
            format=log_fmt,
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler(
                    os.path.join(
                        self.config.log_dir, "multi_exchange.log",
                    ),
                    mode="a",
                ),
            ],
        )