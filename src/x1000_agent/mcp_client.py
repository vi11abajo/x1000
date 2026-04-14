from __future__ import annotations

import json
import logging
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("x1000.mcp")


@dataclass
class McpClient:
    """JSON-RPC client for the OKX Trade MCP server over stdio."""
    profile: str = "live"
    modules: str = "market,swap,account"
    _process: subprocess.Popen | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _id: int = 0
    _reader_thread: threading.Thread | None = field(default=None, repr=False)
    _responses: dict[int, dict] = field(default_factory=dict, repr=False)
    _ready: threading.Event = field(default_factory=threading.Event, repr=False)

    def start(self) -> None:
        """Spawn the MCP server subprocess."""
        cmd = " ".join(["okx-trade-mcp", "--profile", self.profile, "--modules", self.modules])
        log.info("Starting MCP server: %s", cmd)
        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            shell=True,
        )
        self._reader_thread = threading.Thread(
            target=self._read_loop, daemon=True, name="mcp-reader"
        )
        self._reader_thread.start()
        # Initialize MCP session
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "x1000-agent", "version": "0.1.0"},
        })
        self._ready.wait(timeout=10)
        log.info("MCP server initialized")

    def stop(self) -> None:
        """Terminate the MCP server subprocess."""
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            log.info("MCP server stopped")

    def call(self, tool_name: str, args: dict[str, Any] | None = None) -> dict:
        """Call an MCP tool and return the parsed result.

        MCP wraps OKX responses as: {"tool": "...", "ok": true, "data": {"endpoint": "...", "data": [...]}}
        We unwrap to return just the inner "data" array (matching the old OkxCli interface).
        """
        response = self._rpc("tools/call", {
            "name": tool_name,
            "arguments": args or {},
        })
        result = response.get("result", response)
        # Prefer structuredContent if available (parsed JSON)
        structured = result.get("structuredContent")
        if structured:
            return self._unwrap(structured)
        # Fall back to parsing text content
        content = result.get("content", [])
        if isinstance(content, list) and content:
            text = content[0].get("text", "") if isinstance(content[0], dict) else str(content[0])
            try:
                parsed = json.loads(text)
                return self._unwrap(parsed)
            except (json.JSONDecodeError, TypeError):
                return {"raw": text}
        return result

    def _unwrap(self, data: dict) -> dict | list:
        """Unwrap MCP response to return the inner OKX data.

        MCP wraps responses in two levels:
        Level 1: {"tool": "...", "ok": true, "data": {"endpoint": "...", "data": [...]}}
        Level 2: {"endpoint": "...", "data": [...]}
        We unwrap both levels and return the innermost list/dict.
        """
        if isinstance(data, dict):
            # Level 1: MCP tool wrapper {"tool": "...", "data": {...}}
            inner = data.get("data")
            if isinstance(inner, dict):
                # Level 2: OKX API wrapper {"endpoint": "...", "data": [...]}
                deeper = inner.get("data")
                if isinstance(deeper, list):
                    return deeper
                if isinstance(deeper, dict):
                    return deeper
                return inner
            if isinstance(inner, list):
                return inner
            return data
        return data

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        """Send a JSON-RPC request and wait for the response."""
        with self._lock:
            self._id += 1
            req_id = self._id
            req = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params or {},
            }
            self._send(req)
            # Wait for response
            event = threading.Event()
            self._responses[f"wait:{req_id}"] = event
            event.wait(timeout=120)
            resp = self._responses.pop(str(req_id), None)
            if resp is None:
                raise TimeoutError(f"MCP request timeout for {method} (id={req_id})")
            if "error" in resp:
                raise RuntimeError(f"MCP error on {method}: {resp['error']}")
            return resp

    def _send(self, req: dict) -> None:
        """Write a JSON-RPC request to the subprocess stdin."""
        if not self._process or not self._process.stdin:
            raise RuntimeError("MCP server not started")
        line = json.dumps(req, separators=(",", ":")) + "\n"
        self._process.stdin.write(line)
        self._process.stdin.flush()

    def _read_loop(self) -> None:
        """Read JSON-RPC responses from the subprocess stdout."""
        if not self._process or not self._process.stdout:
            return
        for line in self._process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg_id = msg.get("id")
            if msg_id is not None:
                key = str(msg_id)
                # Store the response
                self._responses[key] = msg
                # Signal the waiting thread
                event = self._responses.pop(f"wait:{msg_id}", None)
                if event:
                    event.set()
                # Handle initialize response
                if msg.get("result") and msg.get("result", {}).get("serverInfo"):
                    self._ready.set()
            # Log errors
            if "error" in msg:
                log.warning("MCP error: %s", msg["error"])
