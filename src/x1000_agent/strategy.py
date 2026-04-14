from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from x1000_agent.okx_cli import OkxCli

log = logging.getLogger("x1000.strategy")


@dataclass
class Signal:
    side: str | None  # "long", "short", or None (flat)
    size_usd: float
    callback_ratio: float  # trailing stop callback ratio (e.g. 0.005 = 0.5%)
    reason: str = ""
    tp_percent: float = 0.0
    sl_percent: float = 0.0


class BaseStrategy:
    def evaluate(self, market_data: dict[str, Any], position: dict[str, Any]) -> Signal:
        raise NotImplementedError


# --- Pair-specific configs from backtest results ---
PAIR_CONFIG = {
    "BTC-USDT-SWAP": {"tp": 4.0, "sl": 5.0, "trail": 0.005, "htf_min": 35, "htf_max": 70},
    "ETH-USDT-SWAP": {"tp": 2.5, "sl": 4.0, "trail": 0.005, "htf_min": 40, "htf_max": 75},
    "SOL-USDT-SWAP": {"tp": 3.0, "sl": 6.0, "trail": 0.010, "htf_min": 40, "htf_max": 75},
}


def _get_rsi(okx: OkxCli, inst_id: str, bar: str = "1H") -> float | None:
    """Fetch RSI(14) value. Returns float or None on error."""
    try:
        data = okx.get_indicator("rsi", inst_id, bar=bar, params="14")
        if isinstance(data, list) and data:
            item = data[0]
            tf_data = item.get("data", [{}])[0]
            timeframes = tf_data.get("timeframes", {})
            tf = timeframes.get(bar, {})
            indicators = tf.get("indicators", {})
            rsi_list = indicators.get("RSI", [])
            if rsi_list:
                values = rsi_list[0].get("values", {})
                return float(values.get("14", 0))
    except Exception as e:
        log.warning("RSI fetch failed: %s", e)
    return None


def _get_candles(okx: OkxCli, inst_id: str, bar: str = "1H", limit: int = 10) -> list[list]:
    """Fetch OHLCV candles."""
    try:
        return okx.get_candles(inst_id, bar=bar, limit=limit)
    except Exception as e:
        log.warning("Candles fetch failed: %s", e)
        return []


def _count_green_candles(candles: list[list], lookback: int = 5) -> int:
    """Count green (bullish) candles in the last N candles."""
    if not candles:
        return 0
    recent = candles[:lookback]
    green = 0
    for c in recent:
        if len(c) >= 5:
            o, c_close = float(c[1]), float(c[4])
            if c_close > o:
                green += 1
    return green


def _count_red_candles(candles: list[list], lookback: int = 5) -> int:
    """Count red (bearish) candles in the last N candles."""
    if not candles:
        return 0
    recent = candles[:lookback]
    red = 0
    for c in recent:
        if len(c) >= 5:
            o, c_close = float(c[1]), float(c[4])
            if c_close < o:
                red += 1
    return red


def _check_volume_capitulation(candles: list[list], mult: float = 2.0) -> bool:
    """Check if latest volume > average x mult (capitulation spike)."""
    if not candles or len(candles) < 21:
        return False
    vols = [float(c[5]) for c in candles if len(c) > 5]
    if len(vols) < 21:
        return False
    avg_vol = sum(vols[1:]) / len(vols[1:])
    latest_vol = vols[0]
    return latest_vol > avg_vol * mult


class S6ConfluencePullback(BaseStrategy):
    """
    Strategy 6: Confluence Pullback - 5-layer confirmation.
    Best for: BTC, ETH, AVAX, LINK on 1H timeframe.
    NOT for: DOGE, meme coins.
    Supports both long and short entries.
    """

    def __init__(self, okx: OkxCli, inst_id: str, size_usd: float = 100):
        self.okx = okx
        self.inst_id = inst_id
        self.size_usd = size_usd
        cfg = PAIR_CONFIG.get(inst_id, {"tp": 2.5, "sl": 4.0, "trail": 0.005, "htf_min": 40, "htf_max": 75})
        self.tp = cfg["tp"]
        self.sl = cfg["sl"]
        self.trail = cfg["trail"]
        self.htf_min = cfg["htf_min"]
        self.htf_max = cfg["htf_max"]

    def evaluate(self, market_data: dict[str, Any], position: dict[str, Any]) -> Signal:
        if position.get("positions"):
            return Signal(side=None, size_usd=0, callback_ratio=0, reason="already in position")

        htf_rsi = _get_rsi(self.okx, self.inst_id, bar="4H")
        if htf_rsi is None:
            return Signal(side=None, size_usd=0, callback_ratio=0, reason="HTF RSI unavailable")
        if not (self.htf_min <= htf_rsi <= self.htf_max):
            return Signal(side=None, size_usd=0, callback_ratio=0,
                          reason=f"HTF RSI {htf_rsi:.1f} outside [{self.htf_min}, {self.htf_max}]")

        rsi = _get_rsi(self.okx, self.inst_id, bar="1H")
        if rsi is None:
            return Signal(side=None, size_usd=0, callback_ratio=0, reason="RSI unavailable")

        candles = _get_candles(self.okx, self.inst_id, bar="1H", limit=10)

        # --- LONG: RSI pulled back below 35, recovery starting ---
        if rsi <= 35:
            green = _count_green_candles(candles, lookback=5)
            if green < 1:
                return Signal(side=None, size_usd=0, callback_ratio=0, reason=f"Long: only {green} green candles")
            cap = _check_volume_capitulation(candles, mult=2.0)
            if cap:
                return Signal(side=None, size_usd=0, callback_ratio=0, reason="Long: volume capitulation - waiting")
            return Signal(
                side="long",
                size_usd=self.size_usd,
                callback_ratio=self.trail,
                reason=f"S6 long: HTF RSI={htf_rsi:.1f}, RSI={rsi:.1f}, green={green}",
                tp_percent=self.tp,
                sl_percent=self.sl,
            )

        # --- SHORT: RSI pushed above 65, rejection starting ---
        if rsi >= 65:
            red = _count_red_candles(candles, lookback=5)
            if red < 1:
                return Signal(side=None, size_usd=0, callback_ratio=0, reason=f"Short: only {red} red candles")
            cap = _check_volume_capitulation(candles, mult=2.0)
            if cap:
                return Signal(side=None, size_usd=0, callback_ratio=0, reason="Short: volume capitulation - waiting")
            return Signal(
                side="short",
                size_usd=self.size_usd,
                callback_ratio=self.trail,
                reason=f"S6 short: HTF RSI={htf_rsi:.1f}, RSI={rsi:.1f}, red={red}",
                tp_percent=self.tp,
                sl_percent=self.sl,
            )

        return Signal(side=None, size_usd=0, callback_ratio=0, reason=f"S6: RSI {rsi:.1f} neutral zone")


class S2ConfirmedReversal(BaseStrategy):
    """
    Strategy 2: RSI Confirmed Reversal.
    Best for: SOL, NEAR, SUI, UNI, DOGE on 1H.
    NOT for: BTC, ETH, AVAX, LINK.
    Supports both long and short reversals.
    Tracks candle timestamps to avoid counting the same candle multiple times.
    """

    def __init__(self, okx: OkxCli, inst_id: str, size_usd: float = 100,
                 threshold: float = 30, min_candles: int = 7, max_gap: int = 1):
        self.okx = okx
        self.inst_id = inst_id
        self.size_usd = size_usd
        self.threshold = threshold
        self.min_candles = min_candles
        self.max_gap = max_gap
        self._below_count: int = 0
        self._gap_count: int = 0
        self._above_count: int = 0
        self._gap_count_short: int = 0
        self._last_candle_ts: str = ""  # track last processed candle timestamp

    def _get_latest_candle_ts(self) -> str:
        """Fetch the latest 1H candle timestamp to detect new candles."""
        try:
            candles = self.okx.get_candles(self.inst_id, bar="1H", limit=1)
            if candles:
                return candles[0][0]  # timestamp is first element
        except Exception:
            pass
        return ""

    def evaluate(self, market_data: dict[str, Any], position: dict[str, Any]) -> Signal:
        if position.get("positions"):
            return Signal(side=None, size_usd=0, callback_ratio=0, reason="already in position")

        # Only process once per new candle
        current_ts = self._get_latest_candle_ts()
        if current_ts == self._last_candle_ts:
            return Signal(side=None, size_usd=0, callback_ratio=0, reason="S2: same candle, waiting")
        self._last_candle_ts = current_ts

        rsi = _get_rsi(self.okx, self.inst_id, bar="1H")
        if rsi is None:
            return Signal(side=None, size_usd=0, callback_ratio=0, reason="RSI unavailable")

        short_threshold = 100 - self.threshold  # e.g. 70 if threshold=30

        # --- LONG setup: RSI below threshold for min_candles ---
        if rsi < self.threshold:
            self._below_count += 1
            self._gap_count = 0
        elif rsi < self.threshold + 5 and self._below_count > 0:
            self._gap_count += 1
            if self._gap_count > self.max_gap:
                self._below_count = 0
                self._gap_count = 0
        else:
            if self._below_count >= self.min_candles:
                count = self._below_count
                self._below_count = 0
                self._gap_count = 0
                cfg = PAIR_CONFIG.get(self.inst_id, {"tp": 2.5, "sl": 4.0, "trail": 0.008})
                return Signal(
                    side="long",
                    size_usd=self.size_usd,
                    callback_ratio=cfg["trail"],
                    reason=f"S2 long: {count} candles below {self.threshold}, RSI now {rsi:.1f}",
                    tp_percent=cfg["tp"],
                    sl_percent=cfg["sl"],
                )
            self._below_count = 0
            self._gap_count = 0

        # --- SHORT setup: RSI above short_threshold for min_candles ---
        if rsi > short_threshold:
            self._above_count += 1
            self._gap_count_short = 0
        elif rsi > short_threshold - 5 and self._above_count > 0:
            self._gap_count_short += 1
            if self._gap_count_short > self.max_gap:
                self._above_count = 0
                self._gap_count_short = 0
        else:
            if self._above_count >= self.min_candles:
                count = self._above_count
                self._above_count = 0
                self._gap_count_short = 0
                cfg = PAIR_CONFIG.get(self.inst_id, {"tp": 2.5, "sl": 4.0, "trail": 0.008})
                return Signal(
                    side="short",
                    size_usd=self.size_usd,
                    callback_ratio=cfg["trail"],
                    reason=f"S2 short: {count} candles above {short_threshold}, RSI now {rsi:.1f}",
                    tp_percent=cfg["tp"],
                    sl_percent=cfg["sl"],
                )
            self._above_count = 0
            self._gap_count_short = 0

        return Signal(side=None, size_usd=0, callback_ratio=0,
                      reason=f"S2: below={self._below_count}/{self.min_candles}, above={self._above_count}/{self.min_candles}, RSI={rsi:.1f}")


class S1SimpleRSI(BaseStrategy):
    """
    Strategy 1: RSI Simple Entry - buy when RSI drops below threshold,
    short when RSI goes above 100-threshold.
    Fallback strategy, best for BTC in ranging markets.
    """

    def __init__(self, okx: OkxCli, inst_id: str, size_usd: float = 100, threshold: float = 30):
        self.okx = okx
        self.inst_id = inst_id
        self.size_usd = size_usd
        self.threshold = threshold
        self._cooldown_long: int = 0
        self._cooldown_short: int = 0

    def evaluate(self, market_data: dict[str, Any], position: dict[str, Any]) -> Signal:
        if position.get("positions"):
            return Signal(side=None, size_usd=0, callback_ratio=0, reason="already in position")

        rsi = _get_rsi(self.okx, self.inst_id, bar="1H")
        if rsi is None:
            return Signal(side=None, size_usd=0, callback_ratio=0, reason="RSI unavailable")

        short_threshold = 100 - self.threshold  # e.g. 70

        # --- LONG ---
        if self._cooldown_long > 0:
            self._cooldown_long -= 1
        elif rsi < self.threshold:
            self._cooldown_long = 12
            return Signal(
                side="long",
                size_usd=self.size_usd,
                callback_ratio=0.005,
                reason=f"S1 long: RSI {rsi:.1f} < {self.threshold}",
                tp_percent=2.0,
                sl_percent=3.0,
            )

        # --- SHORT ---
        if self._cooldown_short > 0:
            self._cooldown_short -= 1
        elif rsi > short_threshold:
            self._cooldown_short = 12
            return Signal(
                side="short",
                size_usd=self.size_usd,
                callback_ratio=0.005,
                reason=f"S1 short: RSI {rsi:.1f} > {short_threshold}",
                tp_percent=2.0,
                sl_percent=3.0,
            )

        return Signal(side=None, size_usd=0, callback_ratio=0, reason=f"S1: RSI {rsi:.1f} neutral")


class CompositeStrategy(BaseStrategy):
    """
    Runs S6 -> S2 -> S1 in priority order.
    First strategy with a valid signal wins.
    """

    def __init__(self, okx: OkxCli, inst_id: str, size_usd: float = 100):
        self.s6 = S6ConfluencePullback(okx, inst_id, size_usd)
        self.s2 = S2ConfirmedReversal(okx, inst_id, size_usd)
        self.s1 = S1SimpleRSI(okx, inst_id, size_usd)

    def evaluate(self, market_data: dict[str, Any], position: dict[str, Any]) -> Signal:
        sig = self.s6.evaluate(market_data, position)
        if sig.side is not None:
            log.info("S6 signal: %s", sig.reason)
            return sig

        sig = self.s2.evaluate(market_data, position)
        if sig.side is not None:
            log.info("S2 signal: %s", sig.reason)
            return sig

        sig = self.s1.evaluate(market_data, position)
        if sig.side is not None:
            log.info("S1 signal: %s", sig.reason)
            return sig

        log.debug("No signal from any strategy")
        return sig
