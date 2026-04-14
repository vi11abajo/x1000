from __future__ import annotations

import json
import logging
import time
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("x1000.tg_listener")


@dataclass
class TelegramListener:
    """Polls Telegram for incoming messages and dispatches commands."""
    bot_token: str
    chat_id: str
    enabled: bool = True
    _offset: int = 0
    _handlers: dict[str, Callable] = field(default_factory=dict)
    _chat_handler: Callable[[str], str] | None = None  # natural language handler

    def register(self, command: str, handler: Callable) -> None:
        """Register a command handler. Command without / prefix."""
        self._handlers[command] = handler

    def set_chat_handler(self, handler: Callable[[str], str]) -> None:
        """Set a handler for natural language messages (non-commands)."""
        self._chat_handler = handler

    def _send_reply(self, text: str, reply_to: int = 0) -> None:
        """Send a reply to a specific message."""
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = json.dumps({
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_to_message_id": reply_to,
        }).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                json.loads(resp.read().decode())
        except Exception as e:
            log.warning("Failed to send Telegram reply: %s", e)

    def _get_updates(self) -> list[dict]:
        """Poll for new updates. On 409, return empty and let the loop retry."""
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        params = urllib.parse.urlencode({
            "offset": self._offset,
            "timeout": 2,
            "allowed_updates": json.dumps(["message"]),
        })
        req = urllib.request.Request(f"{url}?{params}")
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
                if data.get("ok"):
                    return data.get("result", [])
        except urllib.error.HTTPError as e:
            if e.code == 409:
                log.debug("Telegram 409 — waiting for old session to expire")
                time.sleep(5)
                return []
            log.warning("Failed to get updates (HTTP %d): %s", e.code, e)
        except Exception as e:
            log.warning("Failed to get updates: %s", e)
        return []

    def _handle_message(self, update: dict) -> None:
        """Process a single update."""
        msg = update.get("message", {})
        chat = msg.get("chat", {})
        if str(chat.get("id", "")) != self.chat_id:
            return  # ignore messages from other chats

        text = msg.get("text", "").strip()
        msg_id = msg.get("message_id", 0)

        if not text:
            return

        # Parse command
        if text.startswith("/"):
            parts = text[1:].split(" ", 1)
            cmd = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""

            if cmd in self._handlers:
                log.info("Command: /%s arg='%s'", cmd, arg)
                response = self._handlers[cmd](arg)
                if response:
                    self._send_reply(response, msg_id)
            elif cmd == "help":
                self._send_reply(self._help_text(), msg_id)
            else:
                self._send_reply(
                    f"Unknown command: <code>/{cmd}</code>\n"
                    f"Send /help for available commands",
                    msg_id,
                )
        elif self._chat_handler:
            # Natural language message — route to AI
            log.info("Chat message: %s", text[:100])
            response = self._chat_handler(text)
            if response:
                self._send_reply(response, msg_id)

    def _help_text(self) -> str:
        commands = "\n".join(
            f"/{cmd} — {handler.__doc__ or 'No description'}"
            for cmd, handler in self._handlers.items()
        )
        return (
            f"<b>x1000 Agent Commands</b>\n\n"
            f"{commands}\n\n"
            f"Or just ask me anything about the market, positions, or strategy."
        )

    def run(self, stop_event: Callable[[], bool] = lambda: False) -> None:
        """Start polling loop."""
        # Drop all pending updates on startup to avoid stale messages
        try:
            self._get_updates()
        except Exception:
            pass
        log.info("Telegram listener started")
        while not stop_event():
            updates = self._get_updates()
            for update in updates:
                self._offset = update.get("update_id", 0) + 1
                self._handle_message(update)
            time.sleep(1)
        log.info("Telegram listener stopped")
