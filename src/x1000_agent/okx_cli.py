from __future__ import annotations

import json
import subprocess
import shutil
from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class OkxCli:
    profile: str = "live"

    def _exe(self) -> str:
        for name in ("okx", "okx.cmd", "okx.exe"):
            p = shutil.which(name)
            if p:
                return p
        raise RuntimeError('OKX CLI not found in PATH. Install "@okx_ai/okx-trade-cli" and reopen the terminal.')

    def _run(self, args: Sequence[str]) -> str:
        cmd = [self._exe(), "--profile", self.profile, *args]
        try:
            proc = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            stdout = (e.stdout or "").strip()
            msg = stderr or stdout or "Unknown OKX CLI error"
            raise RuntimeError(msg) from e
        return (proc.stdout or "").strip()

    def _json(self, args: Sequence[str]) -> Any:
        out = self._run([*args, "--json"])
        return json.loads(out) if out else None

    # --- Preflight: verify credentials before authenticated operations ---
    def verify_credentials(self) -> bool:
        """Check if CLI is configured for this profile. Returns True if ok."""
        try:
            out = self._run(["config", "show"])
            return "error" not in out.lower()
        except Exception:
            return False

    # --- Market (read-only, no auth needed) ---
    def get_ticker(self, inst_id: str) -> dict[str, Any]:
        return self._json(["market", "ticker", inst_id])

    def get_instruments(self, inst_type: str, inst_id: str | None = None) -> list[dict]:
        """Get instrument details including ctVal (contract face value), minSz, lotSz."""
        args = ["market", "instruments", "--instType", inst_type]
        if inst_id:
            args += ["--instId", inst_id]
        data = self._json(args)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return []

    def get_ctval(self, inst_id: str) -> tuple[float, float, float]:
        """Get ctVal (contract face value), minSz, lotSz for a swap instrument."""
        instruments = self.get_instruments("SWAP", inst_id)
        if not instruments:
            raise RuntimeError(f"Instrument {inst_id} not found")
        info = instruments[0]
        ct_val = float(info.get("ctVal", 1) or 1)
        min_sz = float(info.get("minSz", 1) or 1)
        lot_sz = float(info.get("lotSz", 1) or 1)
        return ct_val, min_sz, lot_sz

    def get_candles(self, inst_id: str, bar: str = "1H", limit: int = 100) -> list[list]:
        """Get OHLCV candles. Bar values: 3m, 5m, 15m, 1H, 4H, 12Hutc, 1Dutc, etc."""
        data = self._json(["market", "candles", inst_id, "--bar", bar, "--limit", str(limit)])
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return []

    def get_indicator(self, indicator: str, inst_id: str, bar: str = "1H", params: str | None = None) -> Any:
        """Get technical indicator value (rsi, macd, ema, supertrend, bb, etc.)."""
        args = ["market", "indicator", indicator, inst_id, "--bar", bar]
        if params:
            args += ["--params", params]
        return self._json(args)

    def get_funding_rate(self, inst_id: str, history: bool = False, limit: int = 10) -> Any:
        args = ["market", "funding-rate", inst_id]
        if history:
            args += ["--history", "--limit", str(limit)]
        return self._json(args)

    def get_mark_price(self, inst_type: str, inst_id: str | None = None) -> Any:
        args = ["market", "mark-price", "--instType", inst_type]
        if inst_id:
            args += ["--instId", inst_id]
        return self._json(args)

    def get_orderbook(self, inst_id: str, depth: int = 20) -> dict:
        return self._json(["market", "orderbook", inst_id, "--sz", str(depth)])

    # --- Account ---
    def get_balance(self, ccy: str | None = None) -> dict[str, Any]:
        args = ["account", "balance"]
        if ccy:
            args.append(ccy)
        return self._json(args)

    def get_positions(self) -> list[dict]:
        data = self._json(["account", "positions"])
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return []

    # --- Swap (Perpetuals) ---
    def swap_positions(self, inst_id: str | None = None) -> list[dict]:
        args = ["swap", "positions"]
        if inst_id:
            args.append(inst_id)
        data = self._json(args)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return []

    def swap_place_order(
        self,
        inst_id: str,
        side: str,
        size: str,
        td_mode: str = "cross",
        order_type: str = "market",
        pos_side: str | None = None,
        tgt_ccy: str | None = None,
        price: str | None = None,
        leverage: int | None = None,
        tp_trigger_px: float | None = None,
        sl_trigger_px: float | None = None,
    ) -> dict:
        """
        Place swap order with OKX agent-skills patterns:
        - td_mode: cross/isolated (required)
        - pos_side: long/short (hedge mode)
        - tgt_ccy: base_ccy/quote_ccy/margin
        - tp_trigger_px/sl_trigger_px: attached TP/SL
        """
        args = [
            "swap", "place",
            "--instId", inst_id,
            "--side", side,
            "--ordType", order_type,
            "--sz", str(size),
            "--tdMode", td_mode,
        ]
        if pos_side:
            args += ["--posSide", pos_side]
        if tgt_ccy:
            args += ["--tgtCcy", tgt_ccy]
        if price:
            args += ["--px", price]
        if leverage:
            args += ["--leverage", str(leverage)]
        if tp_trigger_px:
            args += [f"--tpTriggerPx={tp_trigger_px}", "--tpOrdPx=-1"]
        if sl_trigger_px:
            args += [f"--slTriggerPx={sl_trigger_px}", "--slOrdPx=-1"]
        return self._json(args)

    def swap_close_position(self, inst_id: str, mgn_mode: str = "cross", pos_side: str | None = None) -> dict:
        args = ["swap", "close", "--instId", inst_id, "--mgnMode", mgn_mode]
        if pos_side:
            args += ["--posSide", pos_side]
        return self._json(args)

    def swap_order_cancel(self, inst_id: str, order_id: str | None = None) -> dict:
        args = ["swap", "cancel", "--instId", inst_id]
        if order_id:
            args += ["--ordId", order_id]
        return self._json(args)

    def swap_leverage_set(self, inst_id: str, leverage: int, mgn_mode: str = "cross", pos_side: str | None = None) -> dict:
        args = ["swap", "leverage", "--instId", inst_id, "--lever", str(leverage), "--mgnMode", mgn_mode]
        if pos_side:
            args += ["--posSide", pos_side]
        return self._json(args)

    def swap_leverage_get(self, inst_id: str, mgn_mode: str = "cross") -> dict:
        return self._json(["swap", "get-leverage", "--instId", inst_id, "--mgnMode", mgn_mode])

    def swap_orders(self, inst_id: str | None = None, history: bool = False) -> list[dict]:
        args = ["swap", "orders"]
        if inst_id:
            args.append(inst_id)
        if history:
            args.append("--history")
        data = self._json(args)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return []

    def swap_get_order(self, inst_id: str, order_id: str | None = None) -> dict:
        args = ["swap", "get", "--instId", inst_id]
        if order_id:
            args += ["--ordId", order_id]
        return self._json(args)

    # --- Algo orders: TP/SL, trailing stops ---
    def swap_algo_place(
        self,
        inst_id: str,
        side: str,
        order_type: str,  # oco, conditional, move_order_stop
        size: str,
        td_mode: str = "cross",
        pos_side: str | None = None,
        tgt_ccy: str | None = None,
        tp_trigger_px: float | None = None,
        sl_trigger_px: float | None = None,
        callback_ratio: float | None = None,
        active_px: float | None = None,
        reduce_only: bool = False,
    ) -> dict:
        args = [
            "swap", "algo", "place",
            "--instId", inst_id,
            "--side", side,
            "--ordType", order_type,
            "--sz", str(size),
            "--tdMode", td_mode,
        ]
        if pos_side:
            args += ["--posSide", pos_side]
        if tgt_ccy:
            args += ["--tgtCcy", tgt_ccy]
        if tp_trigger_px:
            args += [f"--tpTriggerPx={tp_trigger_px}", "--tpOrdPx=-1"]
        if sl_trigger_px:
            args += [f"--slTriggerPx={sl_trigger_px}", "--slOrdPx=-1"]
        if callback_ratio:
            args += ["--callbackRatio", str(callback_ratio)]
        if active_px:
            args += ["--activePx", str(active_px)]
        if reduce_only:
            args.append("--reduceOnly")
        return self._json(args)

    def swap_algo_trail(
        self,
        inst_id: str,
        side: str,
        size: str,
        td_mode: str = "cross",
        pos_side: str | None = None,
        callback_ratio: float | None = None,
        active_px: float | None = None,
        reduce_only: bool = False,
    ) -> dict:
        args = [
            "swap", "algo", "trail",
            "--instId", inst_id,
            "--side", side,
            "--sz", str(size),
            "--tdMode", td_mode,
        ]
        if pos_side:
            args += ["--posSide", pos_side]
        if callback_ratio:
            args += ["--callbackRatio", str(callback_ratio)]
        if active_px:
            args += ["--activePx", str(active_px)]
        if reduce_only:
            args.append("--reduceOnly")
        return self._json(args)

    def swap_algo_cancel(self, inst_id: str, algo_id: str) -> dict:
        return self._json(["swap", "algo", "cancel", "--instId", inst_id, "--algoId", algo_id])

    def swap_algo_orders(self, inst_id: str | None = None) -> list[dict]:
        args = ["swap", "algo", "orders"]
        if inst_id:
            args.append(inst_id)
        data = self._json(args)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return []

    # --- Portfolio helpers ---
    def account_bills(self, limit: int = 20) -> list[dict]:
        data = self._json(["account", "bills", "--limit", str(limit)])
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return []

    def positions_history(self, inst_id: str | None = None) -> list[dict]:
        args = ["account", "positions-history"]
        if inst_id:
            args += ["--instId", inst_id]
        data = self._json(args)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return []


def calc_contracts(usdt_amount: float, price: float, ct_val: float) -> int:
    """Convert USDT amount to contract count: floor(usdt / (price * ctVal))."""
    if price <= 0 or ct_val <= 0:
        return 0
    return int(usdt_amount / (price * ct_val))
