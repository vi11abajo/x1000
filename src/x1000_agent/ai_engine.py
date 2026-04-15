from __future__ import annotations

import html
import logging
import re
import threading
import time
from typing import Any

from x1000_agent.config import AgentConfig
from x1000_agent.health import HealthMonitor
from x1000_agent.hyperliquid_client import HyperliquidClient
from x1000_agent.mcp_client import McpClient
from x1000_agent.okx_cli import calc_contracts
from x1000_agent.risk import RiskManager
from x1000_agent.strategy import Signal
from x1000_agent.telegram import TelegramNotifier
from x1000_agent.telegram_listener import TelegramListener
from x1000_agent.ai import AIAgent, AIDecision

log = logging.getLogger("x1000.ai_engine")


def _safe_tg_html(text: str) -> str:
    """Escape AI-generated text for Telegram HTML, preserving intentional tags."""
    # AI may output HTML like <code>, <b>, etc. Escape everything first,
    # then restore the tags we explicitly allow.
    escaped = html.escape(text, quote=False)
    # Restore common formatting tags the AI might use
    for tag in ("b", "i", "code", "pre", "u", "strike", "a", "em", "strong"):
        escaped = escaped.replace(f"&lt;{tag}&gt;", f"<{tag}>")
        escaped = escaped.replace(f"&lt;/{tag}&gt;", f"</{tag}>")
        # Also restore self-closing variants
        escaped = escaped.replace(f"&lt;{tag} /&gt;", f"<{tag} />")
        escaped = escaped.replace(f"&lt;{tag}/&gt;", f"<{tag}/>")
    # Restore attributes on allowed tags (e.g. <a href="...">)
    for tag in ("a",):
        escaped = re.sub(
            rf"&lt;{tag}\s+([^&]*)&gt;",
            rf"<{tag} \1>",
            escaped,
        )
    return escaped

ASSETS = [
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
    "HYPE-USDT-SWAP",
]


class AIEngine:
    """AI-driven trading engine that uses Claude (via AWstore) for decisions."""

    def __init__(self, config: AgentConfig, ai: AIAgent):
        self.config = config
        self.mcp = McpClient(profile=config.profile, modules="market,swap,account")
        self.mcp.start()
        self.risk = RiskManager(config)
        self.ai = ai
        self._running = False
        self.hl = HyperliquidClient()
        self.tg = TelegramNotifier(
            bot_token=config.telegram.bot_token,
            chat_id=config.telegram.chat_id,
            enabled=config.telegram.enabled,
        )
        self._ct_vals: dict[str, float] = {}
        self._price: float = 0.0
        self._last_decision: AIDecision | None = None
        self._last_market: dict[str, dict[str, Any]] = {}
        self._last_entry_time: float = 0.0  # timestamp of last trade entry
        self._entry_date: str = ""  # UTC date string for daily counter
        self._entry_count: int = 0  # entries today
        self._entry_times: dict[str, float] = {}  # instId -> entry timestamp for hold time
        self._entry_prices: dict[str, float] = {}  # instId -> entry price for TP calculation
        self._tp_levels: dict[str, float] = {}  # instId -> TP price level from AI decision
        self._reversal_pending: dict[str, int] = {}  # instId -> reversal_score (waiting for confirmation)
        self._conviction_threshold: int = 50  # progressive: 50 → 60 → 70 → 80
        self._total_entries: int = 0  # lifetime entry counter for progressive conviction
        self._asset_trade_results: dict[str, list[bool]] = {}  # instId -> [True=win, False=loss]

        # Dynamic entry limit: after 4 daily entries exhausted, allow 1 more at a time with score >= 85
        self._post_limit_asset: str | None = None  # instId of post-limit position (None = no post-limit active)

        # Minimum interval between new entries (seconds) by market mode
        self._entry_intervals = {
            "NY OVERLAP": 900,         # 15 min
            "LONDON OPEN": 900,        # 15 min
            "NEWS MODE": 300,          # 5 min
            "ASIAN SESSION": 3600,     # 60 min
            "US LATE": 1800,           # 30 min
            "PACIFIC/CLOSE": 999999,   # disabled — close only
            "NORMAL": 900,             # 15 min default
        }

        # Telegram command listener
        self._tg_listener = TelegramListener(
            bot_token=config.telegram.bot_token,
            chat_id=config.telegram.chat_id,
            enabled=config.telegram.enabled,
        )
        self._tg_thread: threading.Thread | None = None
        self._register_commands()

        # Health monitor
        self._health = HealthMonitor(check_interval_sec=60)
        self._health.register_mcp_restart(
            restart_fn=self._restart_mcp,
            health_fn=self._check_mcp_health,
        )
        self._health.register_telegram_restart(self._restart_tg_listener)
        self._health.register_notifier(self.tg.notify_error)

    def _chat_handler(self, text: str) -> str:
        """Handle natural language messages via AI."""
        try:
            positions = self._fetch_all_positions()
            open_assets = [p["instId"] for p in positions]

            # Build context
            ctx = self._build_chat_context(positions)

            prompt = (
                f"User message: {text}\n\n"
                f"Current context:\n{ctx}\n\n"
                f"Respond to the user's message naturally. "
                f"If they ask about trading, analyze the market and give your honest assessment. "
                f"If they ask about positions, explain what's open. "
                f"Keep responses concise (under 500 chars). "
                f"Use HTML formatting for Telegram (use <code>, <b>, etc.)."
            )

            response = self.ai._call_api(prompt, timeout=30)
            # Sanitize AI response for Telegram HTML safety
            response = _safe_tg_html(response)
            # Truncate if too long
            if len(response) > 4000:
                response = response[:3997] + "..."
            return response
        except Exception as e:
            log.warning("Chat handler error: %s", e)
            return f"Sorry, I couldn't process that: {e}"

    def _build_chat_context(self, positions: list[dict]) -> str:
        """Build context string for chat responses."""
        utc_now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        lines = [f"Time: {utc_now}"]

        # Positions
        if positions:
            lines.append("\nOpen positions:")
            for p in positions:
                lines.append(
                    f"  {p.get('instId')} {p.get('posSide')} "
                    f"@ {p.get('avgPx')} | PnL: {p.get('upl')} | Lev: {p.get('lever')}x"
                )
        else:
            lines.append("\nNo open positions")

        # Last AI decision
        if self._last_decision:
            d = self._last_decision
            lines.append(
                f"\nLast decision: {d.selected_asset or 'skip'} "
                f"{d.direction or ''} score={d.score} — {d.reason}"
            )

        # Market snapshot (simplified)
        if self._last_market:
            lines.append("\nMarket snapshot:")
            for inst_id, data in self._last_market.items():
                parts = []
                if "last_price" in data:
                    parts.append(f"price={data['last_price']}")
                if "rsi_15m" in data:
                    parts.append(f"RSI(15M)={data['rsi_15m']}")
                if "ema20_15m" in data and "ema50_15m" in data:
                    parts.append(f"EMA20={data['ema20_15m']} EMA50={data['ema50_15m']}")
                if "funding_rate" in data:
                    parts.append(f"funding={data['funding_rate']}")
                lines.append(f"  {inst_id}: {', '.join(parts)}")

        # Risk state
        lines.append(
            f"\nRisk: daily_pnl={self.risk.daily_pnl_usd:+.2f}, "
            f"position_usd={self.risk.current_position_usd:.2f}, "
            f"kill_switch={'ON' if self.risk.killed else 'OFF'}"
        )

        return "\n".join(lines)

    def _register_commands(self) -> None:
        """Register Telegram command handlers."""
        self._tg_listener.register("status", self._cmd_status)
        self._tg_listener.register("positions", self._cmd_positions)
        self._tg_listener.register("close", self._cmd_close)
        self._tg_listener.register("pause", self._cmd_pause)
        self._tg_listener.register("resume", self._cmd_resume)
        self._tg_listener.register("balance", self._cmd_balance)
        self._tg_listener.register("pnl", self._cmd_pnl)
        self._tg_listener.register("kill", self._cmd_kill)
        self._tg_listener.register("health", self._cmd_health)
        self._tg_listener.set_chat_handler(self._chat_handler)

    def _cmd_status(self, arg: str) -> str:
        """Show current AI decision and market mode"""
        if self._last_decision:
            d = self._last_decision
            return (
                f"<b>Last AI Decision</b>\n"
                f"Asset: <code>{d.selected_asset or 'NONE'}</code>\n"
                f"Direction: <code>{d.direction or 'skip'}</code>\n"
                f"Score: <code>{d.score}/100</code>\n"
                f"Size: <code>{d.position_size}</code>\n"
                f"Reason: {d.reason}"
            )
        return "No decisions yet"

    def _cmd_positions(self, arg: str) -> str:
        """Show all open positions"""
        positions = self.mcp.call("swap_get_positions", {"instType": "SWAP"})
        if not positions:
            return "No open positions"
        lines = []
        for p in positions:
            inst = p.get("instId", "?")
            side = p.get("posSide", "?")
            avg = p.get("avgPx", "?")
            pnl = p.get("upl", "0")
            lev = p.get("lever", "?")
            mgn = p.get("mgnMode", "?")
            lines.append(
                f"<code>{inst}</code> {side} @ {avg}\n"
                f"  Leverage: {lev}x | Margin: {mgn}\n"
                f"  PnL: <code>${float(pnl):+.2f}</code>"
            )
        return "<b>Open Positions</b>\n\n" + "\n\n".join(lines)

    def _cmd_close(self, arg: str) -> str:
        """Close a position. Usage: /close BTC-USDT-SWAP or /close all"""
        positions = self.mcp.call("swap_get_positions", {"instType": "SWAP"})
        if not positions:
            return "No open positions to close"
        if arg.upper() == "ALL":
            results = []
            for p in positions:
                inst = p.get("instId")
                side = p.get("posSide")
                mgn = p.get("mgnMode", "isolated")
                try:
                    self._close_position(inst, side, mgn, reason="manual_close_all")
                    results.append(f"[OK] {inst} closed")
                except Exception as e:
                    results.append(f"[FAIL] {inst}: {e}")
            return "<b>Close All</b>\n\n" + "\n".join(results)
        # Close specific asset
        for p in positions:
            if p.get("instId", "").upper() == arg.upper():
                mgn = p.get("mgnMode", "isolated")
                side = p.get("posSide")
                try:
                    self._close_position(arg.upper(), side, mgn, reason="manual_close")
                    return f"<code>{arg.upper()}</code> closed"
                except Exception as e:
                    return f"Failed: {e}"
        return f"Position <code>{arg.upper()}</code> not found"

    def _cmd_pause(self, arg: str) -> str:
        """Pause trading loop"""
        self._running = False
        return "Trading paused"

    def _cmd_resume(self, arg: str) -> str:
        """Resume trading loop"""
        self._running = True
        return "Trading resumed"

    def _cmd_balance(self, arg: str) -> str:
        """Show account balance"""
        try:
            data = self.mcp.call("account_get_balance")
            details = data if isinstance(data, dict) else (data[0] if isinstance(data, list) and data else {})
            total_eq = details.get("totalEq", "?")
            avail_eq = details.get("availEq", "?")
            return (
                f"<b>Balance</b>\n"
                f"Total Equity: <code>${total_eq}</code>\n"
                f"Available: <code>${avail_eq}</code>"
            )
        except Exception as e:
            return f"Failed: {e}"

    def _cmd_pnl(self, arg: str) -> str:
        """Show daily PnL"""
        status = "ACTIVE" if self.risk.killed else "OFF"
        return (
            f"<b>PnL</b>\n"
            f"Daily: <code>${self.risk.daily_pnl_usd:+.2f}</code>\n"
            f"Current positions: <code>${self.risk.current_position_usd:.2f}</code>\n"
            f"Kill switch: {status}"
        )

    def _cmd_kill(self, arg: str) -> str:
        """Emergency kill switch - close all and stop"""
        self.risk.killed = True
        self._running = False
        # Close all positions
        positions = self.mcp.call("swap_get_positions", {"instType": "SWAP"})
        closed = []
        for p in positions:
            try:
                inst = p.get("instId")
                mgn = p.get("mgnMode", "isolated")
                side = p.get("posSide")
                self.mcp.call("swap_close_position", {"instId": inst, "posSide": side, "mgnMode": mgn})
                closed.append(inst)
            except Exception:
                pass
        closed_str = ", ".join(closed) if closed else "none"
        return (
            f"<b>KILL SWITCH ACTIVATED</b>\n"
            f"Trading stopped\n"
            f"Positions closed: {closed_str}"
        )

    def _cmd_health(self, arg: str) -> str:
        """Show system health status"""
        lines = ["<b>System Health</b>"]

        # MCP server
        mcp_ok = self._check_mcp_health()
        lines.append(f"MCP Server: {'<code>OK</code>' if mcp_ok else '<code>DOWN</code>'}")

        # Telegram listener
        tg_alive = self._tg_thread is not None and self._tg_thread.is_alive()
        lines.append(f"Telegram: {'<code>OK</code>' if tg_alive else '<code>DOWN</code>'}")

        # Trading loop
        lines.append(f"Trading: {'<code>RUNNING</code>' if self._running else '<code>STOPPED</code>'}")

        # Kill switch
        lines.append(f"Kill switch: {'<code>ON</code>' if self.risk.killed else '<code>OFF</code>'}")

        # PnL
        lines.append(f"Daily PnL: <code>${self.risk.daily_pnl_usd:+.2f}</code>")

        # Last decision
        if self._last_decision:
            d = self._last_decision
            lines.append(f"Last decision: <code>{d.selected_asset or 'skip'}</code> score={d.score}")

        return "\n".join(lines)

    def run_once(self) -> None:
        # Refresh real PnL from exchange before each cycle
        self.risk.refresh_pnl(mcp=self.mcp)

        market = self._fetch_all_market()
        positions = self._fetch_all_positions()
        open_assets = [p["instId"] for p in positions]

        self._last_market = market
        decision = self.ai.decide(market, {"positions": positions}, open_assets)
        self._last_decision = decision

        log.info("AI Decision: %s", decision.reason)
        log.info("Full output:\n%s", decision.full_output)

        if decision.selected_asset is None or decision.direction is None:
            log.info("AI decided to skip — score=%d, reason=%s", decision.score, decision.reason)
            self._send_cycle_report(decision, positions)
            return

        # AI may recommend closing an existing position
        if decision.direction == "close" and decision.selected_asset in open_assets:
            # Minimum hold time: prevent premature exits
            entry_time = self._entry_times.get(decision.selected_asset, 0)
            hold_seconds = time.time() - entry_time
            min_hold = 15 * 60  # 15 minutes
            if hold_seconds < min_hold:
                hold_min = hold_seconds / 60
                log.info("AI close vetoed: %s held %.0fm < %.0fm minimum — letting trade develop",
                         decision.selected_asset, hold_min, min_hold / 60)
                self._send_cycle_report(decision, positions)
                return

            log.info("AI recommends closing %s — reason=%s", decision.selected_asset, decision.reason)
            for p in positions:
                if p.get("instId") == decision.selected_asset:
                    pnl = float(p.get("upl", 0) or 0)
                    # Hard guard: never close at a loss (except max hold time)
                    if pnl < 0:
                        hold_seconds = time.time() - self._entry_times.get(decision.selected_asset, 0)
                        max_hold = 4 * 3600
                        if hold_seconds < max_hold:
                            log.info("AI close vetoed (negative PnL): %s PnL=$%.2f — waiting for recovery or SL",
                                     decision.selected_asset, pnl)
                            self._send_cycle_report(decision, positions)
                            return
                        # Max hold reached with negative PnL — still close to prevent further loss
                        log.info("Max hold time reached with negative PnL: %s — closing anyway", decision.selected_asset)
                    self._close_position(decision.selected_asset, p.get("posSide"),
                                         p.get("mgnMode", "isolated"),
                                         reason=f"AI_recommend:{decision.reason}", pnl=pnl)
                    self._send_cycle_report(decision, positions)
                    return
            return

        # Progressive conviction threshold — each new entry requires higher score
        if decision.score < self._conviction_threshold:
            log.info("Score %d below conviction threshold %d — skipping",
                     decision.score, self._conviction_threshold)
            self._send_cycle_report(decision, positions)
            return

        # Daily entry limit — with dynamic post-limit exception
        if not self._check_daily_entries():
            # Post-limit mode: allow 1 more position at a time with score >= 85
            if self._post_limit_asset:
                log.info("Post-limit position still open (%s) — cannot open another (score=%d)",
                         self._post_limit_asset, decision.score)
                return
            if decision.score < 85:
                log.info("Daily limit exhausted, score=%d < 85 — post-limit entry requires high confidence", decision.score)
                return
            log.info("Post-limit entry allowed: score=%d >= 85, no post-limit position open", decision.score)

        # Cooldown after 2 losses on same asset
        asset_results = self._asset_trade_results.get(decision.selected_asset, [])
        if len(asset_results) >= 2 and not asset_results[-2] and not asset_results[-1]:
            log.info("Cooldown: last 2 trades on %s were losses — skipping", decision.selected_asset)
            return

        # Entry cooldown — prevent overtrading
        elapsed = time.time() - self._last_entry_time
        min_interval = self._get_entry_interval(decision.full_output)
        if elapsed < min_interval:
            log.info("Entry cooldown active (%.0fs < %ds) — skipping", elapsed, min_interval)
            return

        # Convert AI decision to Signal
        signal = Signal(
            side=decision.direction,
            size_usd=self._calc_size_usd(decision.position_size),
            callback_ratio=decision.callback_ratio,
            reason=f"AI: {decision.reason} (score={decision.score}, mode={decision.risk})",
            tp_percent=decision.tp_percent,
            sl_percent=decision.sl_percent,
        )

        ok, reason = self.risk.check(signal.side, signal.size_usd)
        if not ok:
            log.warning("Risk check failed: %s — skipping", reason)
            if "kill" in reason.lower():
                self.tg.notify_kill_switch(reason)
            return

        self._execute(signal, decision)
        self._last_entry_time = time.time()

    def _send_cycle_report(self, decision: AIDecision, positions: list[dict]) -> None:
        """Send activity report to Telegram after each cycle."""
        utc_now = time.strftime("%H:%M:%S UTC", time.gmtime())

        # Build positions summary
        if positions:
            pos_lines = []
            for p in positions:
                inst = p.get("instId", "?")
                side = p.get("posSide", "?")
                pnl = float(p.get("upl", 0) or 0)
                pos_lines.append(f"{inst} {side}: ${pnl:+.2f}")
            pos_text = "\n".join(pos_lines)
        else:
            pos_text = "No open positions"

        if decision.selected_asset and decision.direction:
            action = f"<b>ENTRY</b> {decision.selected_asset} {decision.direction.upper()}"
            details = (
                f"Score: {decision.score}/100\n"
                f"Size: {decision.position_size}\n"
                f"TP: {decision.tp_percent}% | SL: {decision.sl_percent}%\n"
                f"Trailing: {decision.callback_ratio*100:.1f}%\n"
                f"Reason: {decision.reason}"
            )
        else:
            action = "<b>SKIP</b>"
            details = (
                f"Score: {decision.score}/100\n"
                f"Reason: {decision.reason}"
            )

        # Risk state
        risk_text = (
            f"Daily PnL: ${self.risk.daily_pnl_usd:+.2f}\n"
            f"Unrealized: ${self.risk.unrealized_pnl_usd:+.2f}\n"
            f"Kill switch: {'ON' if self.risk.killed else 'OFF'}\n"
            f"Entries today: {self._entry_count}/4"
        )

        report = (
            f"<b>x1000 Cycle Report</b> [{utc_now}]\n\n"
            f"{action}\n\n"
            f"{details}\n\n"
            f"<b>Positions:</b>\n{pos_text}\n\n"
            f"<b>Risk:</b>\n{risk_text}"
        )

        # Truncate if too long for Telegram
        if len(report) > 4000:
            report = report[:3997] + "..."

        self.tg.notify(report)

    def _send_monitoring_report(self, positions: list[dict]) -> None:
        """Send position monitoring report to Telegram."""
        if not positions:
            return

        utc_now = time.strftime("%H:%M:%S UTC", time.gmtime())
        lines = [f"<b>Position Monitor</b> [{utc_now}]"]
        for p in positions:
            inst = p.get("instId", "?")
            side = p.get("posSide", "?")
            avg = float(p.get("avgPx", 0))
            pnl = float(p.get("upl", 0) or 0)
            mgn = float(p.get("margin", 10))
            pnl_pct = (pnl / mgn * 100) if mgn > 0 else 0
            entry_time = self._entry_times.get(inst)
            if entry_time:
                hold_min = (time.time() - entry_time) / 60
                hold_str = f"{hold_min:.0f}m"
            else:
                hold_str = "unknown"

            tp = self._tp_levels.get(inst)
            tp_str = f"TP: {tp}" if tp else "TP: N/A"
            pending = self._reversal_pending.get(inst, 0)
            pending_str = f"Reversal pending: {pending}" if pending > 0 else ""

            lines.append(
                f"\n<code>{inst}</code> {side} @ {avg}\n"
                f"  PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%) | Hold: {hold_str}\n"
                f"  {tp_str}"
                + (f"\n  {pending_str}" if pending_str else "")
            )

        lines.append(f"\nDaily PnL: ${self.risk.daily_pnl_usd:+.2f}")

        report = "\n".join(lines)
        if len(report) > 4000:
            report = report[:3997] + "..."

        self.tg.notify(report)

    def run_loop(self) -> None:
        """Dual-loop architecture: Entry Loop (15min) + Monitoring Loop (5min)."""
        self._running = True
        log.info("AI Trading dual-loop started: Entry=15min, Monitor=5min")
        self.tg.notify_startup(self.config.profile, ", ".join(ASSETS))

        # Start Telegram listener in background thread
        self._start_tg_listener()

        # Start health monitor
        self._health.start()

        last_entry = 0.0
        last_monitor = 0.0
        monitor_interval = 5 * 60  # 5 minutes

        while self._running:
            now = time.time()

            # Monitoring Loop — every 5 minutes, only if positions open
            if now - last_monitor >= monitor_interval:
                last_monitor = now
                try:
                    self._monitoring_loop()
                except Exception as e:
                    log.exception("Monitoring loop error: %s", e)
                    self.tg.notify_error(str(e), "monitoring_loop")

            # Entry Loop — adaptive interval based on last decision's market mode
            entry_interval = self._get_entry_interval(
                self._last_decision.full_output if self._last_decision else ""
            )
            if now - last_entry >= entry_interval:
                last_entry = now
                try:
                    self.run_once()
                except Exception as e:
                    log.exception("Entry loop error: %s", e)
                    self.tg.notify_error(str(e), "entry_loop")

            # Sleep 30 seconds between checks
            time.sleep(30)

    def _monitoring_loop(self) -> None:
        """Monitor open positions for reversal signals, TP reach, and early exit."""
        positions = self._fetch_all_positions()
        if not positions:
            return

        # Check max hold time
        to_close = self._check_hold_time(positions)
        for p in to_close:
            self._close_position(p.get("instId"), p.get("posSide"), p.get("mgnMode", "isolated"),
                                 reason="max_hold_time", pnl=float(p.get("upl", 0) or 0))

        for pos in positions:
            inst_id = pos.get("instId", "")
            pos_side = pos.get("posSide", "")
            avg_px = float(pos.get("avgPx", 0))
            upl = float(pos.get("upl", 0))
            margin = float(pos.get("margin", 10))
            pnl_pct = (upl / margin * 100) if margin > 0 else 0
            time_in_trade = (time.time() - self._entry_times.get(inst_id, time.time())) / 60

            # Skip if position just opened (< 5 min)
            if time_in_trade < 5:
                continue

            # Fetch latest 15M candles
            try:
                candles = self.mcp.call("market_get_candles", {"instId": inst_id, "bar": "15m", "limit": 24})
                if not candles:
                    continue
                closes = [float(c[4]) for c in candles if len(c) > 4]
                highs = [float(c[2]) for c in candles if len(c) > 2]
                lows = [float(c[3]) for c in candles if len(c) > 3]
                opens = [float(c[1]) for c in candles if len(c) > 1]
                volumes = [float(c[5]) for c in candles if len(c) > 5]
                if len(closes) < 15:
                    continue

                current_price = closes[0]

                # === TP-based Exit ===
                tp_level = self._tp_levels.get(inst_id)
                if tp_level and tp_level > 0:
                    atr = self._calc_atr([[0, opens[i], highs[i], lows[i], closes[i]]
                                          for i in range(min(15, len(closes)))], 14) or 0
                    if pos_side == "long":
                        tp_threshold = tp_level - 0.001 * atr if atr else tp_level * 0.999
                        if current_price >= tp_threshold:
                            self._close_position(inst_id, pos_side, pos.get("mgnMode", "isolated"),
                                                 reason=f"TP_reached@{tp_level},PnL={pnl_pct:.1f}%", pnl=upl)
                            continue
                    else:  # short
                        tp_threshold = tp_level + 0.001 * atr if atr else tp_level * 1.001
                        if current_price <= tp_threshold:
                            self._close_position(inst_id, pos_side, pos.get("mgnMode", "isolated"),
                                                 reason=f"TP_reached@{tp_level},PnL={pnl_pct:.1f}%", pnl=upl)
                            continue

                # === Reversal Detection ===
                reversal_score, signals = self._detect_reversal(
                    closes, highs, lows, opens, volumes, pos_side
                )

                # === Aggressive Early Exit (Step 7.7) ===
                # Don't wait for confirmation if PnL is already good
                if reversal_score >= 1 and pnl_pct > 1.0 and time_in_trade > 8:
                    self._close_position(inst_id, pos_side, pos.get("mgnMode", "isolated"),
                                         reason=f"aggressive_exit(score=1,PnL={pnl_pct:.1f}%,time={time_in_trade:.0f}m)", pnl=upl)
                    continue
                if reversal_score >= 2 and pnl_pct > 0.5 and time_in_trade > 5:
                    self._close_position(inst_id, pos_side, pos.get("mgnMode", "isolated"),
                                         reason=f"aggressive_exit(score=2,PnL={pnl_pct:.1f}%,time={time_in_trade:.0f}m)", pnl=upl)
                    continue

                # === Confirmation Candle logic ===
                # Don't close immediately on score >= 2 — wait for next candle to confirm
                pending_score = self._reversal_pending.get(inst_id, 0)
                if pending_score >= 2:
                    # Previous cycle had reversal signal — check if confirmed
                    if reversal_score >= pending_score:
                        # Confirmed — act on it
                        action = self._reversal_action(pending_score, pnl_pct, time_in_trade)
                        if action == "CLOSE":
                            self._close_position(inst_id, pos_side, pos.get("mgnMode", "isolated"),
                                                 reason=f"reversal_confirmed(score={pending_score}),PnL={pnl_pct:.1f}%", pnl=upl)
                            self._reversal_pending.pop(inst_id, None)
                            continue
                        elif action == "TIGHTEN":
                            self._tighten_trailing(inst_id, pos)
                    else:
                        # Signal weakened — false alarm
                        log.info("Reversal signal faded for %s (was %d, now %d) — HOLD",
                                 inst_id, pending_score, reversal_score)
                    self._reversal_pending.pop(inst_id, None)
                elif reversal_score >= 2:
                    # New reversal signal — set pending, wait for confirmation
                    self._reversal_pending[inst_id] = reversal_score
                    log.info("Reversal pending for %s: score=%d, PnL=%.2f%% — waiting for confirmation",
                             inst_id, reversal_score, pnl_pct)
                elif reversal_score >= 1:
                    # Weak reversal — apply early exit rules
                    action = self._reversal_action(reversal_score, pnl_pct, time_in_trade)
                    if action == "CLOSE":
                        self._close_position(inst_id, pos_side, pos.get("mgnMode", "isolated"),
                                             reason=f"weak_reversal(score=1),PnL={pnl_pct:.1f}%", pnl=upl)
                        continue

                log.info("Monitor %s: PnL=%.2f%%, reversal_score=%d, pending=%d, signals=%s → HOLD",
                         inst_id, pnl_pct, reversal_score, pending_score, signals)

            except Exception as e:
                log.warning("Monitoring error for %s: %s", inst_id, e)

        # Send monitoring report
        self._send_monitoring_report(positions)

    def _reversal_action(self, score: int, pnl_pct: float, time_in_trade: float) -> str:
        """Determine action based on reversal score, PnL, and time in trade.

        Weak Reversal Early Exit rules:
        - score >= 3 + PnL > 0% → CLOSE immediately
        - score >= 2 + PnL > 0.3% + time > 5min → CLOSE
        - score >= 1 + PnL > 0.8% + time > 10min → CLOSE
        - score >= 2 + PnL <= 0.3% → TIGHTEN
        - score == 1 + PnL <= 0.8% → MONITOR
        """
        if score >= 3 and pnl_pct > 0:
            return "CLOSE"
        if score >= 2 and pnl_pct > 0.3 and time_in_trade > 5:
            return "CLOSE"
        if score >= 1 and pnl_pct > 0.8 and time_in_trade > 10:
            return "CLOSE"
        if score >= 2 and pnl_pct <= 0.3:
            return "TIGHTEN"
        return "MONITOR"

    def _tighten_trailing(self, inst_id: str, pos: dict) -> None:
        """Tighten trailing stop for a position. Cancels existing trailing stops first."""
        try:
            # Cancel existing trailing stops (move_order_stop) for this asset
            algo_orders = self.mcp.call("swap_get_algo_orders", {"instType": "SWAP"})
            if isinstance(algo_orders, list):
                for algo in algo_orders:
                    if algo.get("instId") == inst_id and algo.get("ordType") == "move_order_stop":
                        algo_id = algo.get("algoId")
                        if algo_id:
                            self.mcp.call("swap_cancel_algo_order", {
                                "instId": inst_id,
                                "algoId": algo_id,
                            })
                            log.info("Cancelled old trailing stop %s for %s before tightening", algo_id, inst_id)

            self.mcp.call("swap_place_algo_order", {
                "instId": inst_id,
                "posSide": pos.get("posSide", ""),
                "tdMode": pos.get("mgnMode", "isolated"),
                "sz": pos.get("availPos", pos.get("pos", "1")),
                "side": "sell" if pos.get("posSide") == "long" else "buy",
                "ordType": "move_order_stop",
                "callbackRatio": "0.25",  # tighten to 0.25%
                "reduceOnly": True,
            })
            log.info("Tightened trailing stop for %s to 0.25%%", inst_id)
        except Exception as e:
            log.warning("Failed to tighten trailing stop for %s: %s", inst_id, e)

    def _detect_reversal(self, closes, highs, lows, opens, volumes, pos_side: str) -> tuple[int, dict]:
        """Detect reversal signals on 15M candles. Returns (score, signal_details)."""
        score = 0
        signals = {"rsi_div": False, "ema_cross": False, "choch": False, "vol_collapse": False}

        # === Type 1: RSI Divergence ===
        # Calculate RSI series once on full dataset, compare RSI at price peaks
        if len(closes) >= 20:
            rsi_series = self._calc_rsi_series(closes, 14)
            if rsi_series and len(rsi_series) >= 20:
                if pos_side == "long":
                    # Bearish divergence: price higher high, RSI lower high
                    idx_recent, price_high_recent = max(range(6), key=lambda i: closes[i]), max(closes[:6])
                    idx_older, price_high_older = max(range(6, 12), key=lambda i: closes[i]), max(closes[6:12])
                    if price_high_recent > price_high_older and rsi_series[idx_recent] < rsi_series[idx_older]:
                        score += 1
                        signals["rsi_div"] = True
                else:  # short
                    # Bullish divergence: price lower low, RSI higher low
                    idx_recent, price_low_recent = min(range(6), key=lambda i: closes[i]), min(closes[:6])
                    idx_older, price_low_older = min(range(6, 12), key=lambda i: closes[i]), min(closes[6:12])
                    if price_low_recent < price_low_older and rsi_series[idx_recent] > rsi_series[idx_older]:
                        score += 1
                        signals["rsi_div"] = True

        # === Type 2: EMA Crossover ===
        ema20 = self._calc_ema(closes, 20)
        ema50 = self._calc_ema(closes, 50)
        if ema20 is not None and ema50 is not None:
            price = closes[0]
            if pos_side == "long":
                if price < ema20:
                    score += 1
                    signals["ema_cross"] = True
                if price < ema50:
                    score += 1
            else:  # short
                if price > ema20:
                    score += 1
                    signals["ema_cross"] = True
                if price > ema50:
                    score += 1

        # === Type 3: SMC CHoCH ===
        if len(closes) >= 12:
            if pos_side == "long":
                swing_low = min(lows[6:12])
                if closes[0] < swing_low:
                    score += 2
                    signals["choch"] = True
            else:  # short
                swing_high = max(highs[6:12])
                if closes[0] > swing_high:
                    score += 2
                    signals["choch"] = True

        # === Type 4: Volume Collapse + Price Stall ===
        if len(volumes) >= 20:
            avg_vol = sum(volumes[-20:]) / 20
            last_vol = volumes[0]
            last_range = abs(closes[0] - opens[0]) if opens else 0
            # Compute ATR from the candles we have
            atr_candles = []
            for i in range(len(closes)):
                atr_candles.append([0, opens[i] if i < len(opens) else closes[i],
                                    highs[i], lows[i], closes[i]])
            atr = self._calc_atr(atr_candles, 14) if len(atr_candles) >= 15 else None
            if atr and avg_vol > 0:
                if last_vol < 0.3 * avg_vol and last_range < 0.5 * atr:
                    score += 1
                    signals["vol_collapse"] = True

        return score, signals

    def _close_position(self, inst_id: str, pos_side: str, mgn_mode: str, reason: str = "", pnl: float | None = None) -> None:
        """Close a position via MCP. PnL can be passed from caller to avoid extra API call."""
        try:
            # Get PnL before close if not provided
            if pnl is None:
                positions = self.mcp.call("swap_get_positions", {"instType": "SWAP"})
                pnl = 0.0
                for p in positions:
                    if p.get("instId") == inst_id:
                        pnl = float(p.get("upl", 0) or 0)
                        break

            result = self.mcp.call("swap_close_position", {
                "instId": inst_id,
                "posSide": pos_side,
                "mgnMode": mgn_mode,
            })
            log.info("Auto-close %s (%s): PnL=%.2f, response=%s", inst_id, reason, pnl, result)

            # Cancel all pending algo orders for this position (trailing stop, TP, SL)
            try:
                algo_orders = self.mcp.call("swap_get_algo_orders", {"instType": "SWAP"})
                if isinstance(algo_orders, list):
                    for algo in algo_orders:
                        if algo.get("instId") == inst_id:
                            algo_id = algo.get("algoId")
                            if algo_id:
                                self.mcp.call("swap_cancel_algo_order", {
                                    "instId": inst_id,
                                    "algoId": algo_id,
                                })
                                log.info("Cancelled algo order %s for %s", algo_id, inst_id)
            except Exception as e:
                log.warning("Failed to cancel algo orders for %s: %s", inst_id, e)

            # Cancel all pending regular orders (limit, etc.) for this instrument
            try:
                open_orders = self.mcp.call("swap_get_orders", {"instType": "SWAP", "instId": inst_id, "status": "open"})
                if isinstance(open_orders, list):
                    for order in open_orders:
                        if order.get("instId") == inst_id:
                            ord_id = order.get("ordId")
                            if ord_id:
                                self.mcp.call("swap_cancel_order", {
                                    "instId": inst_id,
                                    "ordId": ord_id,
                                })
                                log.info("Cancelled pending order %s for %s", ord_id, inst_id)
            except Exception as e:
                log.warning("Failed to cancel pending orders for %s: %s", inst_id, e)

            self.tg.notify_error(f"Auto-closed {inst_id}: {reason} (PnL=${pnl:+.2f})", inst_id)

            # Record trade result for cooldown tracking
            is_win = pnl >= 0
            if inst_id not in self._asset_trade_results:
                self._asset_trade_results[inst_id] = []
            self._asset_trade_results[inst_id].append(is_win)
            # Keep only last 5 results
            if len(self._asset_trade_results[inst_id]) > 5:
                self._asset_trade_results[inst_id] = self._asset_trade_results[inst_id][-5:]

            # Record realized PnL for risk manager
            self.risk.record_realized_pnl(pnl)

            # Clean up tracking
            self._entry_times.pop(inst_id, None)
            self._entry_prices.pop(inst_id, None)
            self._tp_levels.pop(inst_id, None)
            self._reversal_pending.pop(inst_id, None)
            # Clear post-limit flag if this was the post-limit position
            if self._post_limit_asset == inst_id:
                self._post_limit_asset = None
                log.info("Post-limit position closed — another high-confidence entry is now allowed")
        except Exception as e:
            log.error("Failed to close %s: %s", inst_id, e)

    def stop(self, reason: str = "") -> None:
        self._running = False
        self._health.stop()
        self.mcp.stop()
        log.info("AI Trading loop stopped: %s", reason)
        self.tg.notify_shutdown(reason)

    # --- Health & restart helpers ---

    def _start_tg_listener(self) -> None:
        if self._tg_listener.enabled:
            self._tg_thread = threading.Thread(
                target=self._tg_listener.run,
                kwargs={"stop_event": lambda: not self._running},
                daemon=True,
            )
            self._tg_thread.start()
            log.info("Telegram listener started in background")

    def _restart_tg_listener(self) -> None:
        log.warning("Restarting Telegram listener")
        # Reset offset to avoid missing messages
        self._tg_listener._offset = 0
        self._start_tg_listener()

    def _restart_mcp(self) -> None:
        log.warning("Restarting MCP server")
        self.mcp.stop()
        time.sleep(2)
        self.mcp = McpClient(profile=self.config.profile, modules="market,swap,account")
        self.mcp.start()
        log.info("MCP server restarted")

    def _check_mcp_health(self) -> bool:
        """Quick health check — fetch ticker, should respond within 10s."""
        try:
            result = self.mcp.call("market_get_ticker", {"instId": "BTC-USDT-SWAP"})
            return bool(result)
        except Exception:
            return False

    def _fetch_all_market(self) -> dict[str, dict[str, Any]]:
        """Collect full market data for all 4 assets via MCP.

        15M candles (60 periods) as primary timeframe.
        1H candles (24 periods) for structure / SMC context.
        """
        result = {}
        for inst_id in ASSETS:
            data = {}
            try:
                # Ticker
                ticker = self.mcp.call("market_get_ticker", {"instId": inst_id})
                tdata = ticker if isinstance(ticker, list) else ticker.get("data", [ticker])
                last_px = float(tdata[0].get("last", 0)) if tdata else 0
                data["last_price"] = last_px
                if inst_id == self.config.inst_id:
                    self._price = last_px

                # === 15M candles (primary, 60 periods = 15 hours) ===
                candles_15m = self.mcp.call("market_get_candles", {"instId": inst_id, "bar": "15m", "limit": 60})
                if candles_15m:
                    closes_15m = [float(c[4]) for c in candles_15m if len(c) > 4]
                    highs_15m = [float(c[2]) for c in candles_15m if len(c) > 2]
                    lows_15m = [float(c[3]) for c in candles_15m if len(c) > 2]
                    opens_15m = [float(c[1]) for c in candles_15m if len(c) > 1]
                    volumes_15m = [float(c[5]) for c in candles_15m if len(c) > 5]
                    data["candles_15m_count"] = len(candles_15m)
                    if closes_15m:
                        data["last_close_15m"] = closes_15m[0]
                        data["last_high_15m"] = highs_15m[0]
                        data["last_low_15m"] = lows_15m[0]
                        data["last_open_15m"] = opens_15m[0] if opens_15m else 0
                        data["last_volume_15m"] = volumes_15m[0] if volumes_15m else 0
                        data["avg_volume_15m_20"] = sum(volumes_15m[-20:]) / 20 if len(volumes_15m) >= 20 else 0
                        data["price_change_15m"] = round((closes_15m[0] - closes_15m[1]) / closes_15m[1] * 100, 3) if len(closes_15m) > 1 else 0
                        data["price_change_1h_15m"] = round((closes_15m[0] - closes_15m[3]) / closes_15m[3] * 100, 3) if len(closes_15m) > 3 else 0
                        data["price_change_4h_15m"] = round((closes_15m[0] - closes_15m[15]) / closes_15m[15] * 100, 3) if len(closes_15m) > 15 else 0
                        # EMA20 on 15M
                        data["ema20_15m"] = self._calc_ema(closes_15m, 20)
                        # EMA50 on 15M
                        data["ema50_15m"] = self._calc_ema(closes_15m, 50)
                        # RSI14 on 15M
                        data["rsi_15m"] = self._calc_rsi(closes_15m, 14)
                        # ATR14 on 15M
                        data["atr_15m"] = self._calc_atr(candles_15m, 14)
                        # EMA20 slope (compare current vs 5 bars ago)
                        if len(closes_15m) >= 25:
                            ema20_prev = self._calc_ema(closes_15m[:-5], 20)
                            if ema20_prev and data["ema20_15m"]:
                                slope = (data["ema20_15m"] - ema20_prev) / ema20_prev * 100
                                if slope > 0.05:
                                    data["ema20_slope_15m"] = "rising"
                                elif slope < -0.05:
                                    data["ema20_slope_15m"] = "falling"
                                else:
                                    data["ema20_slope_15m"] = "flat"
                        # Last candle body size vs ATR
                        if opens_15m and closes_15m and data.get("atr_15m"):
                            body = abs(closes_15m[0] - opens_15m[0])
                            atr = data["atr_15m"]
                            if atr > 0:
                                data["body_vs_atr"] = round(body / atr, 2)
                        # Volume anomaly
                        if volumes_15m and data.get("avg_volume_15m_20"):
                            avg_vol = data["avg_volume_15m_20"]
                            if avg_vol > 0:
                                data["volume_ratio"] = round(volumes_15m[0] / avg_vol, 2)

                # === 1H candles (structure, 24 periods = 24 hours) ===
                candles_1h = self.mcp.call("market_get_candles", {"instId": inst_id, "bar": "1H", "limit": 24})
                if candles_1h:
                    closes_1h = [float(c[4]) for c in candles_1h if len(c) > 4]
                    highs_1h = [float(c[2]) for c in candles_1h if len(c) > 2]
                    lows_1h = [float(c[3]) for c in candles_1h if len(c) > 2]
                    data["candles_1h_count"] = len(candles_1h)
                    if closes_1h:
                        data["last_close_1h"] = closes_1h[0]
                        data["last_high_1h"] = highs_1h[0]
                        data["last_low_1h"] = lows_1h[0]
                        data["price_change_1h"] = round((closes_1h[0] - closes_1h[1]) / closes_1h[1] * 100, 3) if len(closes_1h) > 1 else 0
                        data["price_change_4h"] = round((closes_1h[0] - closes_1h[3]) / closes_1h[3] * 100, 3) if len(closes_1h) > 3 else 0
                        data["price_change_12h"] = round((closes_1h[0] - closes_1h[11]) / closes_1h[11] * 100, 3) if len(closes_1h) > 11 else 0
                        # EMA20 on 1H (for SMC context)
                        data["ema20_1h"] = self._calc_ema(closes_1h, 20)
                        # RSI14 on 1H
                        data["rsi_1h"] = self._calc_rsi(closes_1h, 14)
                        # 1H swing high/low for SMC
                        if len(highs_1h) >= 12:
                            data["swing_high_1h"] = max(highs_1h[:12])
                            data["swing_low_1h"] = min(lows_1h[:12])

                # Funding rate
                try:
                    funding = self.mcp.call("market_get_funding_rate", {"instId": inst_id})
                    if isinstance(funding, list) and funding:
                        data["funding_rate"] = float(funding[0].get("fundingRate", 0))
                        data["next_funding"] = funding[0].get("nextFundingTime", "")
                except Exception as e:
                    data["funding_error"] = str(e)

                # Open Interest
                try:
                    oi = self.mcp.call("market_get_open_interest", {"instType": "SWAP", "instId": inst_id})
                    if isinstance(oi, list) and oi:
                        data["oi"] = oi[0].get("oi", "")
                except Exception:
                    pass

            except Exception as e:
                data["error"] = str(e)

            result[inst_id] = data

        # === Hyperliquid: Liquidation Clusters + Whale Flow ===
        try:
            prices = {inst_id: data.get("last_price", 0) for inst_id, data in result.items()}
            hl_data = self.hl.get_all_data(ASSETS, prices)
            for inst_id, hl_info in hl_data.items():
                if inst_id in result:
                    result[inst_id].update(hl_info)
        except Exception as e:
            log.warning("Hyperliquid data fetch failed: %s", e)

        return result

    @staticmethod
    def _calc_ema(closes: list[float], period: int) -> float | None:
        """Calculate EMA for the last close."""
        if len(closes) < period:
            return None
        # Use simplified EMA: start with SMA, then apply EMA formula
        multiplier = 2 / (period + 1)
        sma = sum(closes[-period:]) / period
        ema = sma
        # For better accuracy, iterate backwards
        data = closes[-(period * 2):]  # use extra data if available
        if len(data) > period:
            ema = sum(data[:period]) / period
            for price in data[period:]:
                ema = (price - ema) * multiplier + ema
        else:
            ema = sma
        return round(ema, 6)

    @staticmethod
    def _calc_rsi(closes: list[float], period: int = 14) -> float | None:
        """Calculate RSI using Wilder smoothing (standard method)."""
        if len(closes) < period + 1:
            return None
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        # Wilder smoothing: start with SMA of first `period` gains/losses
        first_gains = [d for d in deltas[:period] if d > 0]
        first_losses = [-d for d in deltas[:period] if d < 0]
        avg_gain = sum(first_gains) / period
        avg_loss = sum(first_losses) / period
        # Smooth remaining deltas
        for d in deltas[period:]:
            gain = d if d > 0 else 0
            loss = -d if d < 0 else 0
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 2)

    @staticmethod
    def _calc_rsi_series(closes: list[float], period: int = 14) -> list[float] | None:
        """Calculate RSI for each candle, returning a list aligned with closes."""
        if len(closes) < period + 1:
            return None
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        first_gains = [d for d in deltas[:period] if d > 0]
        first_losses = [-d for d in deltas[:period] if d < 0]
        avg_gain = sum(first_gains) / period
        avg_loss = sum(first_losses) / period
        rsi_values: list[float] = []
        # First RSI value
        if avg_loss == 0:
            rsi_values.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_values.append(round(100 - (100 / (1 + rs)), 2))
        # Smooth remaining
        for d in deltas[period:]:
            gain = d if d > 0 else 0
            loss = -d if d < 0 else 0
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
            if avg_loss == 0:
                rsi_values.append(100.0)
            else:
                rs = avg_gain / avg_loss
                rsi_values.append(round(100 - (100 / (1 + rs)), 2))
        return rsi_values

    @staticmethod
    def _calc_atr(candles: list, period: int = 14) -> float | None:
        """Calculate ATR from OHLC candles [ts, open, high, low, close, vol, ...]."""
        if len(candles) < period + 1:
            return None
        trs = []
        for i in range(1, len(candles)):
            high = float(candles[i][2])
            low = float(candles[i][3])
            prev_close = float(candles[i - 1][4])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
        return round(sum(trs[-period:]) / period, 6)

    def _fetch_all_positions(self) -> list[dict]:
        """Get all open swap positions via MCP."""
        return self.mcp.call("swap_get_positions", {"instType": "SWAP"})

    def _get_entry_interval(self, full_output: str) -> int:
        """Determine minimum interval between new entries based on market mode."""
        output_upper = full_output.upper()
        if "NY OVERLAP" in output_upper:
            return self._entry_intervals["NY OVERLAP"]
        if "LONDON OPEN" in output_upper:
            return self._entry_intervals["LONDON OPEN"]
        if "NEWS MODE" in output_upper or "NEWS/" in output_upper:
            return self._entry_intervals["NEWS MODE"]
        if "ASIAN SESSION" in output_upper or "ASIAN" in output_upper:
            return self._entry_intervals["ASIAN SESSION"]
        if "US LATE" in output_upper or "LATE US" in output_upper:
            return self._entry_intervals["US LATE"]
        if "PACIFIC" in output_upper or "CLOSE" in output_upper:
            return self._entry_intervals["PACIFIC/CLOSE"]
        return self._entry_intervals["NORMAL"]

    def _check_daily_entries(self) -> bool:
        """Check if we've hit max 4 entries today. Reset counter at UTC midnight."""
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if today != self._entry_date:
            self._entry_date = today
            self._entry_count = 0
            self._post_limit_asset = None
            log.info("Daily entry counter reset: new day %s, post-limit flag cleared", today)
        return self._entry_count < 4

    def _record_entry(self) -> None:
        """Record a new entry for daily counter and progressive conviction."""
        self._check_daily_entries()  # ensure date is current
        self._entry_count += 1
        self._total_entries += 1
        # Progressive conviction: 50 → 60 → 70 → 80
        self._conviction_threshold = min(80, 50 + self._total_entries * 10)
        log.info("Conviction threshold raised to %d (entry #%d)",
                 self._conviction_threshold, self._total_entries)

    def _check_hold_time(self, positions: list[dict]) -> list[dict]:
        """Check max 4h hold time. Return list of positions to auto-close.

        If position open > 4h AND PnL > 0.5% → close automatically.
        Uses OKX cTime (creation time) for legacy positions we didn't open.
        """
        to_close = []
        now = time.time()
        for p in positions:
            inst_id = p.get("instId", "")
            entry_time = self._entry_times.get(inst_id)

            # If we didn't open this position, try OKX cTime (milliseconds)
            if entry_time is None:
                c_time_ms = p.get("cTime")
                if c_time_ms:
                    try:
                        entry_time = float(c_time_ms) / 1000.0
                        self._entry_times[inst_id] = entry_time
                    except (ValueError, TypeError):
                        self._entry_times[inst_id] = now
                        continue
                else:
                    # Fallback: assume just opened
                    self._entry_times[inst_id] = now
                    continue

            hold_hours = (now - entry_time) / 3600
            if hold_hours > 4.0:
                upl = float(p.get("upl", 0))
                mgn = float(p.get("margin", 10))
                if mgn > 0:
                    pnl_pct = (upl / mgn) * 100
                else:
                    pnl_pct = 0
                if pnl_pct > 0.5:
                    log.info("Max hold time reached: %s held %.1fh, PnL=%.2f%% → auto-close",
                             inst_id, hold_hours, pnl_pct)
                    to_close.append(p)
        return to_close

    def _calc_size_usd(self, position_size: str) -> float:
        """Convert AI position size to USD amount."""
        base = self.config.risk.max_position_usd
        if position_size == "full":
            return base
        elif position_size == "half":
            return base / 2
        elif position_size == "quarter":
            return base / 4
        return base

    def _calc_leverage(self, tp_percent: float, notional_usd: float, atr_pct: float | None = None) -> int:
        """Calculate leverage based on TP target, ATR volatility, and max margin constraint.

        ATR-based cap: max_leverage = 0.5% / (ATR_pct * 2)
        This ensures a 2*ATR move against us doesn't liquidate the position.
        """
        max_margin = self.config.risk.max_margin_usd
        max_lev = self.config.risk.max_leverage

        # Ensure margin doesn't exceed max_margin
        margin_based_lev = int(notional_usd / max_margin) if max_margin > 0 else 1
        margin_based_lev = max(1, margin_based_lev)

        # TP-based leverage
        if tp_percent <= 0:
            tp_based_lev = 10  # default
        elif tp_percent < 1.0:
            tp_based_lev = 50  # tight TP, high leverage
        elif tp_percent < 3.0:
            tp_based_lev = 15  # normal TP, medium leverage
        else:
            tp_based_lev = 5   # wide TP, low leverage

        # ATR-based leverage cap: 0.5% / (ATR_pct * 2)
        # e.g. ATR 1% → max 25x; ATR 2% → max 12.5x; ATR 0.5% → max 50x
        if atr_pct and atr_pct > 0:
            atr_based_lev = int(0.5 / (atr_pct * 2))
        else:
            atr_based_lev = max_lev  # no ATR data, no cap

        # Use the lowest of all constraints to stay safe
        lev = min(margin_based_lev, tp_based_lev, atr_based_lev, max_lev)
        return max(1, lev)

    def _execute(self, signal: Signal, decision: AIDecision) -> None:
        if signal.side is None:
            log.debug("No signal — flat")
            return

        td_mode = self.config.risk.td_mode
        pos_side = "long" if signal.side == "long" else "short"
        inst_id = decision.selected_asset

        # Get price for the SELECTED asset via MCP
        try:
            ticker = self.mcp.call("market_get_ticker", {"instId": inst_id})
            tdata = ticker if isinstance(ticker, list) else ticker.get("data", [ticker])
            entry_price = float(tdata[0].get("last", 0)) if tdata else 0
        except Exception:
            entry_price = self._last_market.get(inst_id, {}).get("last_price", 0)

        # Get ATR for the selected asset (for leverage calculation)
        atr_abs = self._last_market.get(inst_id, {}).get("atr_15m")
        if atr_abs and entry_price > 0:
            atr_pct = atr_abs / entry_price  # convert absolute ATR to percentage
        else:
            atr_pct = None

        leverage = self._calc_leverage(signal.tp_percent, signal.size_usd, atr_pct)

        # Get ctVal for the selected asset via MCP
        try:
            inst_data = self.mcp.call("market_get_instruments", {"instType": "SWAP", "instId": inst_id})
            if isinstance(inst_data, list) and inst_data:
                ct_val = float(inst_data[0].get("ctVal", 1) or 1)
            else:
                ct_val = 1.0
        except Exception:
            ct_val = 1.0

        size_contracts = str(max(calc_contracts(signal.size_usd, entry_price, ct_val), 1))

        trail = signal.callback_ratio if signal.callback_ratio > 0 else 0.005

        # Calculate TP and SL prices from entry price (AI determines levels via SMC)
        tp_px = None
        sl_px = None
        if signal.tp_percent > 0 and entry_price > 0:
            tp_px = round(entry_price * (1 + signal.tp_percent / 100), 2) if signal.side == "long" else round(entry_price * (1 - signal.tp_percent / 100), 2)
        if signal.sl_percent > 0 and entry_price > 0:
            sl_px = round(entry_price * (1 - signal.sl_percent / 100), 2) if signal.side == "long" else round(entry_price * (1 + signal.sl_percent / 100), 2)

        # Set leverage via MCP (posSide only needed for isolated margin hedge mode)
        try:
            lev_args = {"instId": inst_id, "lever": str(leverage), "mgnMode": td_mode}
            if td_mode == "isolated":
                lev_args["posSide"] = pos_side
            lev_result = self.mcp.call("swap_set_leverage", lev_args)
            log.info("Leverage set: %dx, response=%s", leverage, lev_result)
            # Check for errors
            if isinstance(lev_result, list):
                for item in lev_result:
                    code = item.get("sCode", "0")
                    if code and code != "0":
                        msg = item.get("sMsg", "Unknown error")
                        log.warning("Leverage rejected: sCode=%s, %s", code, msg)
        except Exception as e:
            log.warning("Failed to set leverage: %s", e)
            self.tg.notify_error(f"Leverage set failed: {e}", inst_id)

        # Place order via MCP (market order — no TP/SL attached)
        try:
            order_args = {
                "instId": inst_id,
                "side": "buy" if signal.side == "long" else "sell",
                "sz": size_contracts,
                "tdMode": td_mode,
                "ordType": "market",
                "posSide": pos_side,
                "tag": "agentTradeKit",
            }
            result = self.mcp.call("swap_place_order", order_args)
            log.info("Order placed: %s", result)

            # Check for MCP-level errors (can be dict or list)
            if isinstance(result, dict) and result.get("error"):
                err_msg = result.get("message", "Unknown MCP error")
                raise RuntimeError(f"MCP order failed: {err_msg}")
            if isinstance(result, list):
                for item in result:
                    code = item.get("sCode", "0")
                    if code and code != "0":
                        msg = item.get("sMsg", "Unknown error")
                        raise RuntimeError(f"Order rejected: sCode={code}, {msg}")

            self.risk.update_position(signal.size_usd)
            # Check post-limit BEFORE incrementing counter (order-independent)
            is_post_limit = self._entry_count >= 4
            self._record_entry()
            if is_post_limit:
                self._post_limit_asset = inst_id
                log.info("Post-limit position opened: %s, score=%d, must close before next entry", inst_id, decision.score)
            self._entry_times[inst_id] = time.time()
            self._entry_prices[inst_id] = entry_price
            # Store TP level for monitoring loop TP-based exit
            if tp_px:
                self._tp_levels[inst_id] = tp_px

            # Set trailing stop via MCP algo order
            if trail > 0:
                try:
                    self.mcp.call("swap_place_algo_order", {
                        "instId": inst_id,
                        "posSide": pos_side,
                        "tdMode": td_mode,
                        "sz": size_contracts,
                        "side": "sell" if signal.side == "long" else "buy",
                        "ordType": "move_order_stop",
                        "callbackRatio": str(round(trail * 100, 2)),
                        "reduceOnly": True,
                    })
                    log.info("Trailing stop placed: %.2f%%", trail * 100)
                except Exception as e:
                    log.warning("Trailing stop failed: %s", e)

            # Set hard SL as separate algo order (emergency backup)
            if sl_px:
                try:
                    self.mcp.call("swap_place_algo_order", {
                        "instId": inst_id,
                        "posSide": pos_side,
                        "tdMode": td_mode,
                        "sz": size_contracts,
                        "side": "sell" if signal.side == "long" else "buy",
                        "ordType": "conditional",
                        "slTriggerPx": str(sl_px),
                        "slOrdPx": "-1",
                        "reduceOnly": True,
                    })
                    log.info("Hard SL placed at %s", sl_px)
                except Exception as e:
                    log.warning("Hard SL placement failed: %s", e)

            # Set TP as separate algo order
            if tp_px:
                try:
                    self.mcp.call("swap_place_algo_order", {
                        "instId": inst_id,
                        "posSide": pos_side,
                        "tdMode": td_mode,
                        "sz": size_contracts,
                        "side": "sell" if signal.side == "long" else "buy",
                        "ordType": "conditional",
                        "tpTriggerPx": str(tp_px),
                        "tpOrdPx": "-1",
                        "reduceOnly": True,
                    })
                    log.info("TP placed at %s", tp_px)
                except Exception as e:
                    log.warning("TP placement failed: %s", e)

            self.tg.notify_order_filled(
                side=signal.side,
                inst_id=inst_id,
                size=signal.size_usd,
                price=entry_price,
                leverage=leverage,
                tp_px=tp_px,
                sl_px=sl_px,
            )
        except Exception as e:
            log.error("Order failed: %s", e)
            self.tg.notify_error(f"Order failed: {e}", inst_id)
