from __future__ import annotations

import logging
import time
from typing import Any

from x1000_agent.config import AgentConfig
from x1000_agent.okx_cli import OkxCli, calc_contracts
from x1000_agent.risk import RiskManager
from x1000_agent.strategy import BaseStrategy, Signal
from x1000_agent.telegram import TelegramNotifier

log = logging.getLogger("x1000.engine")


class TradingEngine:
    def __init__(self, config: AgentConfig, strategy: BaseStrategy):
        self.config = config
        self.okx = OkxCli(profile=config.profile)
        self.risk = RiskManager(config)
        self.strategy = strategy
        self._running = False
        self.tg = TelegramNotifier(
            bot_token=config.telegram.bot_token,
            chat_id=config.telegram.chat_id,
            enabled=config.telegram.enabled,
        )
        self._ct_val: float = 1.0
        self._price: float = 0.0

    def run_once(self) -> None:
        market = self._fetch_market()
        position = self._fetch_position()
        signal = self.strategy.evaluate(market, position)
        log.info("Signal: %s", signal)

        ok, reason = self.risk.check(signal.side, signal.size_usd)
        if not ok:
            log.warning("Risk check failed: %s — skipping", reason)
            if "kill" in reason.lower():
                self.tg.notify_kill_switch(reason)
            return

        self._execute(signal, position)

    def run_loop(self) -> None:
        self._running = True
        log.info("Trading loop started (interval=%ds)", self.config.loop_interval_sec)
        self.tg.notify_startup(self.config.profile, self.config.inst_id)
        while self._running:
            try:
                self.run_once()
            except Exception as e:
                log.exception("Loop error: %s", e)
                self.tg.notify_error(str(e), "run_loop")
            time.sleep(self.config.loop_interval_sec)

    def stop(self, reason: str = "") -> None:
        self._running = False
        log.info("Trading loop stopped: %s", reason)
        self.tg.notify_shutdown(reason)

    def _fetch_market(self) -> dict[str, Any]:
        ticker = self.okx.get_ticker(self.config.inst_id)
        data = ticker if isinstance(ticker, list) else ticker.get("data", [ticker])
        last_px = float(data[0].get("last", 0)) if data else 0
        self._price = last_px
        return {"ticker": ticker, "inst_id": self.config.inst_id, "last_px": last_px}

    def _fetch_position(self) -> dict[str, Any]:
        positions = self.okx.swap_positions()
        my = [p for p in positions if p.get("instId") == self.config.inst_id]
        return {"positions": my, "inst_id": self.config.inst_id}

    def _get_ct_val(self) -> float:
        if self._ct_val <= 1.0:
            try:
                ct_val, _, _ = self.okx.get_ctval(self.config.inst_id)
                self._ct_val = ct_val
                log.info("ctVal for %s = %s", self.config.inst_id, ct_val)
            except Exception as e:
                log.warning("Failed to get ctVal: %s — using default 1.0", e)
        return self._ct_val

    def _calc_size(self, size_usd: float) -> str:
        """Convert USDT size to contracts using ctVal (OKX agent-skills pattern)."""
        ct_val = self._get_ct_val()
        contracts = calc_contracts(size_usd, self._price, ct_val)
        return str(max(contracts, 1))

    def _calc_leverage(self, callback_ratio: float) -> int:
        """Calculate leverage from trailing stop distance and risk limits.

        Formula: leverage = min(max_leverage, 0.03 / callback_ratio)
        Max 3% risk per trade divided by stop distance = safe leverage.
        e.g. callback=0.005 (0.5%) → 0.03/0.005 = 6x
        e.g. callback=0.010 (1.0%) → 0.03/0.010 = 3x
        e.g. callback=0.003 (0.3%) → 0.03/0.003 = 10x
        """
        if callback_ratio <= 0:
            return 1
        raw_leverage = 0.03 / callback_ratio
        lev = max(1, min(int(raw_leverage), self.config.risk.max_leverage))
        return lev

    def _execute(self, signal: Signal, position: dict) -> None:
        if signal.side is None:
            log.debug("No signal — flat")
            return

        leverage = self._calc_leverage(signal.callback_ratio)
        log.info("Executing: %s %.0f USD @ %dx (callback=%.4f)",
                 signal.side, signal.size_usd, leverage, signal.callback_ratio)

        td_mode = self.config.risk.td_mode
        pos_side = "long" if signal.side == "long" else "short"
        size_contracts = self._calc_size(signal.size_usd)

        # Use strategy-specific TP/SL if provided, fall back to config defaults
        tp_pct = signal.tp_percent if signal.tp_percent > 0 else self.config.risk.tp_percent
        sl_pct = signal.sl_percent if signal.sl_percent > 0 else self.config.risk.sl_percent
        trail = signal.callback_ratio if signal.callback_ratio > 0 else self.config.risk.trailing_callback

        # Calculate TP/SL prices from entry price
        tp_px = None
        sl_px = None
        if tp_pct > 0 and self._price > 0:
            tp_px = round(self._price * (1 + tp_pct / 100), 2) if signal.side == "long" else round(self._price * (1 - tp_pct / 100), 2)
        if sl_pct > 0 and self._price > 0:
            sl_px = round(self._price * (1 - sl_pct / 100), 2) if signal.side == "long" else round(self._price * (1 + sl_pct / 100), 2)

        try:
            self.okx.swap_leverage_set(self.config.inst_id, leverage, mgn_mode=td_mode, pos_side=pos_side)
        except Exception as e:
            log.warning("Failed to set leverage: %s", e)
            self.tg.notify_error(f"Leverage set failed: {e}", self.config.inst_id)

        try:
            result = self.okx.swap_place_order(
                inst_id=self.config.inst_id,
                side="buy" if signal.side == "long" else "sell",
                size=size_contracts,
                td_mode=td_mode,
                order_type="market",
                pos_side=pos_side,
                tp_trigger_px=tp_px,
                sl_trigger_px=sl_px,
            )
            log.info("Order placed: %s", result)
            self.risk.update_position(signal.size_usd)

            # Set trailing stop if configured
            if trail > 0:
                try:
                    self.okx.swap_algo_trail(
                        inst_id=self.config.inst_id,
                        side="sell" if signal.side == "long" else "buy",
                        size=size_contracts,
                        td_mode=td_mode,
                        pos_side=pos_side,
                        callback_ratio=trail,
                        reduce_only=True,
                    )
                    log.info("Trailing stop placed: %.2f%%", trail * 100)
                except Exception as e:
                    log.warning("Trailing stop failed: %s", e)

            self.tg.notify_order_filled(
                side=signal.side,
                inst_id=self.config.inst_id,
                size=signal.size_usd,
                price=self._price,
                leverage=leverage,
            )
            if tp_px:
                self.tg.notify_take_profit(self.config.inst_id, tp_px, 0)
            if sl_px:
                self.tg.notify_stop_loss(self.config.inst_id, sl_px, 0)
        except Exception as e:
            log.error("Order failed: %s", e)
            self.tg.notify_error(f"Order failed: {e}", self.config.inst_id)
