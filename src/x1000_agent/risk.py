from __future__ import annotations

import logging
from dataclasses import dataclass

from x1000_agent.config import AgentConfig, RiskLimits
from x1000_agent.okx_cli import OkxCli

log = logging.getLogger("x1000.risk")


@dataclass
class RiskManager:
    config: AgentConfig
    daily_pnl_usd: float = 0.0  # realized PnL (from closed trades today)
    unrealized_pnl_usd: float = 0.0  # current open position PnL
    current_position_usd: float = 0.0
    killed: bool = False
    _last_realized: float = 0.0  # cumulative realized PnL from exchange

    def check(self, signal_side: str | None, size_usd: float) -> tuple[bool, str]:
        if self.config.risk.kill_switch_enabled or self.killed:
            return False, "kill switch active"

        if signal_side and size_usd > self.config.risk.max_position_usd:
            return False, f"position ${size_usd:.0f} > max ${self.config.risk.max_position_usd:.0f}"

        # Kill switch checks realized PnL only, not unrealized
        if self.daily_pnl_usd < -self.config.risk.max_daily_loss_usd:
            self.killed = True
            log.critical("Daily loss limit hit: %.2f USD (realized) - KILL SWITCH", self.daily_pnl_usd)
            return False, f"daily loss hit: {self.daily_pnl_usd:.2f} USD (realized)"

        return True, "ok"

    def refresh_pnl(self, okx: OkxCli | None = None, mcp=None) -> None:
        """Fetch PnL from all open positions.

        daily_pnl_usd = cumulative realized PnL from closed trades today
        unrealized_pnl_usd = current open position unrealized PnL
        """
        try:
            if mcp:
                positions = mcp.call("swap_get_positions", {"instType": "SWAP"})
            else:
                positions = okx.swap_positions()

            # Unrealized PnL from open positions
            total_upl = 0.0
            for p in positions:
                upl = float(p.get("upl", 0) or 0)
                total_upl += upl
            self.unrealized_pnl_usd = total_upl

            # Realized PnL: track cumulative from exchange position data
            # OKX provides realized_pnl per position (accumulated since open)
            total_realized = 0.0
            for p in positions:
                rpnl = float(p.get("realizedPnl", 0) or 0)
                total_realized += rpnl

            # Detect change in cumulative realized PnL (from closed/algo-filled orders)
            delta = total_realized - self._last_realized
            if delta != 0:
                self.daily_pnl_usd += delta
                self._last_realized = total_realized

            log.debug("PnL refresh: unrealized=%.2f, realized_delta=%.2f, daily_total=%.2f",
                      total_upl, delta, self.daily_pnl_usd)
        except Exception as e:
            log.warning("Failed to refresh PnL: %s", e)

    def update_position(self, size_usd: float) -> None:
        self.current_position_usd = size_usd

    def record_realized_pnl(self, pnl_usd: float) -> None:
        """Manually record realized PnL when a position is closed."""
        self.daily_pnl_usd += pnl_usd
        log.info("Realized PnL recorded: %.2f, daily total: %.2f", pnl_usd, self.daily_pnl_usd)
