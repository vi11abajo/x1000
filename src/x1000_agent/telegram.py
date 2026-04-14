from __future__ import annotations

import html
import logging
import urllib.request
import urllib.error
import json
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("x1000.telegram")


def _safe(text: str) -> str:
    """Escape HTML special characters for safe Telegram rendering."""
    if text is None:
        return ""
    return html.escape(str(text))


@dataclass(frozen=True)
class TelegramNotifier:
    bot_token: str
    chat_id: str
    enabled: bool = True
    api_url: str = "https://api.telegram.org/bot{token}/sendMessage"

    def send(self, text: str, parse_mode: str = "HTML") -> bool:
        if not self.enabled:
            return False
        url = self.api_url.format(token=self.bot_token)
        payload = json.dumps({
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                if data.get("ok"):
                    log.debug("Telegram notification sent")
                    return True
                log.warning("Telegram API error: %s", data)
                return False
        except Exception as e:
            log.error("Failed to send Telegram notification: %s", e)
            return False

    # --- Event templates ---

    def notify_order_filled(self, side: str, inst_id: str, size: float, price: float | None, leverage: int, tp_px: float | None = None, sl_px: float | None = None) -> None:
        label = "LONG" if side == "long" else "SHORT"
        text = (
            f"<b>[{label}] ORDER FILLED</b>\n"
            f"Instrument: <code>{_safe(inst_id)}</code>\n"
            f"Size: <code>${size:.2f}</code>\n"
            f"Leverage: <code>{leverage}x</code>\n"
            f"Price: <code>{price or 'market'}</code>"
        )
        if tp_px:
            text += f"\nTP: <code>{tp_px}</code>"
        if sl_px:
            text += f"\nSL: <code>{sl_px}</code>"
        self.send(text)

    def notify_order_closed(self, inst_id: str, pnl: float, reason: str = "") -> None:
        emoji = "OK" if pnl >= 0 else "LOSS"
        text = (
            f"<b>[{emoji}] POSITION CLOSED</b>\n"
            f"Instrument: <code>{_safe(inst_id)}</code>\n"
            f"PnL: <code>${pnl:+.2f}</code>\n"
            f"Reason: <code>{_safe(reason or 'manual')}</code>"
        )
        self.send(text)

    def notify_stop_loss(self, inst_id: str, price: float | None, loss: float) -> None:
        text = (
            f"<b>[STOP LOSS HIT]</b>\n"
            f"Instrument: <code>{_safe(inst_id)}</code>\n"
            f"Price: <code>{price or 'unknown'}</code>\n"
            f"Loss: <code>${loss:.2f}</code>"
        )
        self.send(text)

    def notify_take_profit(self, inst_id: str, price: float | None, profit: float) -> None:
        text = (
            f"<b>[TAKE PROFIT HIT]</b>\n"
            f"Instrument: <code>{_safe(inst_id)}</code>\n"
            f"Price: <code>{price or 'unknown'}</code>\n"
            f"Profit: <code>${profit:.2f}</code>"
        )
        self.send(text)

    def notify_kill_switch(self, reason: str) -> None:
        text = (
            f"<b>[KILL SWITCH ACTIVATED]</b>\n"
            f"Reason: <code>{_safe(reason)}</code>\n"
            f"All trading stopped"
        )
        self.send(text)

    def notify_error(self, error: str, context: str = "") -> None:
        text = (
            f"<b>[ERROR]</b>\n"
            f"{_safe(error)}\n"
            f"Context: <code>{_safe(context)}</code>"
        )
        self.send(text)

    def notify_startup(self, profile: str, inst_id: str) -> None:
        text = (
            f"<b>Agent Started</b>\n"
            f"Profile: <code>{_safe(profile)}</code>\n"
            f"Instrument: <code>{_safe(inst_id)}</code>\n"
            f"Ready to trade"
        )
        self.send(text)

    def notify_shutdown(self, reason: str = "") -> None:
        text = (
            f"<b>Agent Stopped</b>\n"
            f"Reason: <code>{_safe(reason or 'manual')}</code>"
        )
        self.send(text)
