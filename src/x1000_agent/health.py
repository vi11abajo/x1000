from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("x1000.health")


@dataclass
class HealthMonitor:
    """Watches critical subsystems and restarts them if they die."""

    check_interval_sec: int = 60
    _running: bool = False
    _tg_restart: Callable[[], None] | None = None
    _mcp_restart: Callable[[], None] | None = None
    _mcp_health: Callable[[], bool] | None = None
    _notify: Callable[[str], None] | None = None  # send alert

    def register_telegram_restart(self, fn: Callable[[], None]) -> None:
        """Register a function that restarts the Telegram listener."""
        self._tg_restart = fn

    def register_mcp_restart(self, restart_fn: Callable[[], None], health_fn: Callable[[], bool]) -> None:
        """Register functions to restart and check MCP server health."""
        self._mcp_restart = restart_fn
        self._mcp_health = health_fn

    def register_notifier(self, fn: Callable[[str], None]) -> None:
        """Register a function to send alerts (e.g. Telegram notification)."""
        self._notify = fn

    def start(self) -> None:
        """Start the health monitor in a background daemon thread."""
        self._running = True
        t = threading.Thread(target=self._loop, daemon=True, name="health-monitor")
        t.start()
        log.info("Health monitor started (interval=%ds)", self.check_interval_sec)

    def stop(self) -> None:
        self._running = False
        log.info("Health monitor stopped")

    def _loop(self) -> None:
        while self._running:
            time.sleep(self.check_interval_sec)
            if not self._running:
                break
            try:
                self._check_mcp()
            except Exception as e:
                log.warning("Health check error: %s", e)

    def _check_mcp(self) -> None:
        """Check if MCP server is alive, restart if not."""
        if not self._mcp_health or not self._mcp_restart:
            return
        if not self._mcp_health():
            log.warning("MCP server unhealthy — restarting")
            if self._notify:
                self._notify("MCP server restarted (health check failed)")
            try:
                self._mcp_restart()
            except Exception as e:
                log.error("MCP restart failed: %s", e)
                if self._notify:
                    self._notify(f"MCP restart FAILED: {e}")
