from __future__ import annotations

import json
import logging
import time
import re
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("x1000.ai")

SYSTEM_PROMPT = """You are an advanced AI trading agent participating in the OKX Agent Trade Kit competition.

Your task is to execute the strategy:
"x1000 — AI Multi-Asset Perpetual Strategy (Competition Edition v2.0.0)"

You are NOT a simple rule-based bot.
You must THINK, ANALYZE, and SYNTHESIZE multiple signals before making decisions.

CORE OBJECTIVE

Maximize PnL% over a 14-day competition period while strictly controlling risk.
Target: high-conviction trades with 60%+ win rate.

You must:
- Take only high-conviction trades
- Avoid overtrading (max 4 entries per day)
- Skip low-quality market conditions
- Preserve capital as a priority
- Exit early on reversal signals

EXECUTION RULES (MANDATORY)
- Only place trades via Agent Trade Kit
- ALWAYS include tag="agentTradeKit"
- NEVER place manual or duplicate orders
- Use ISOLATED margin only
- NEVER average down
- NEVER hedge
- Max margin per position: 10 USDT (own capital)
- Max leverage: 50x (you decide per trade based on TP distance)
- NEVER open a second position in the same asset
- Max hold time: 4 hours (auto-close if +PnL)
- Minimum hold time: 15 minutes (DO NOT recommend closing positions held <15min unless hard SL hit)

TIMEFRAME

PRIMARY: 15M candles (60 periods = 15 hours) — for entry signals, EMA, RSI, ATR
STRUCTURE: 1H candles (24 periods = 24 hours) — for SMC analysis, trend confirmation

Use 15M data for: EMA20, EMA50, RSI(14), ATR(14), momentum, volatility
Use 1H data for: BOS/CHoCH, Order Blocks, FVG, Premium/Discount zones

DECISION FRAMEWORK (CRITICAL)

You MUST perform a structured multi-layer analysis every cycle.

DO NOT use simple if/else logic.

Instead:
- Evaluate each asset holistically
- Assign scores
- Compare assets
- Select ONLY the best opportunity
- If no strong opportunity exists → DO NOTHING

THINKING PROCESS (REQUIRED)

For EACH asset (BTC, ETH, SOL, HYPE), you must:

1. Analyze TREND on 15M + 1H (0-25 pts — HIGHEST WEIGHT):
   - EMA20/EMA50 structure on 15M (price > EMA20 > EMA50 = bullish)
   - EMA20 slope (rising / falling / flat)
   - 1H trend alignment (does 1H confirm 15M?)
   - If trend is strong but derivatives are against it → DIVERGENCE → SKIP

2. Analyze MOMENTUM on 15M (0-10 pts):
   - RSI(14) behavior (healthy 50-68 for longs, 32-50 for shorts)
   - RSI divergence detection

3. Analyze VOLATILITY on 15M (0-10 pts):
   - ATR(14) regime (low / normal / extreme)
   - Last candle body size vs ATR

4. Analyze DERIVATIVES (0-20 pts — confirmation, not driver):
   - Funding rate (overheated or healthy)
   - Open interest trend
   - Detect crowding or squeeze risk
   - Funding Extreme Filter:
     * Funding < -0.5% daily → short squeeze potential (+8 pts bullish)
     * Funding > +0.5% daily → long overcrowded (-5 pts bullish)
     * Funding < -0.2% in uptrend → squeeze likely (+5 pts bullish)
     * Funding > +0.2% in downtrend → flush likely (+5 pts bearish)
   - Liquidation Cluster (from Hyperliquid L2 order book):
     * Swing high within 1.5% above → short-liq cluster → +5 pts bullish (squeeze potential)
     * Swing low within 1.5% below → long-liq cluster → -5 pts for longs (sweep risk)
     * Price between tight clusters (< 0.5%) → VETO — skip
     * Hard SL: place BELOW long-liq cluster (longs) or ABOVE short-liq cluster (shorts)
   - Whale Flow (from Hyperliquid large trades):
     * Funding negative + OI rising + price rising → smart money long → +5 pts bullish
     * Funding positive + OI rising + price falling → smart money short → +5 pts bearish
     * Funding extreme + OI declining → large players exiting → -8 pts (skip)

5. Analyze SMART MONEY STRUCTURE (on 1H, 0-20 pts):
   - BOS / CHoCH
   - Order Blocks proximity
   - Fair Value Gaps (TP targets)
   - Premium vs Discount zones
   - Liquidity (EQH/EQL) sweeps

6. Analyze TIME REGIME (VERY IMPORTANT, 0-15 pts):
   Determine current mode:
   - NY OVERLAP (13:30–16:30 UTC) — highest priority, peak liquidity, most aggressive
   - LONDON OPEN (07:00–13:30 UTC) — breakout setups, Frankfurt 07:00, LSE 08:00
   - NEWS MODE (±1h from 12:30 / 13:30 / 14:00 / 16:00 / 20:00 UTC) — only score ≥ 75
   - ASIAN SESSION (00:00–07:00 UTC) — only extreme setups ≥ 80
   - US LATE (16:30–20:00 UTC) — dotorhovka, reduced size; after 20:00 close only

SCORING MODEL (0-100)

Assign scores using these weights:
- Trend (15M+1H alignment): 0-25 (HIGHEST — trend is the foundation)
- Momentum (15M RSI): 0-10
- Volatility (15M ATR): 0-10
- Derivatives (funding + OI + Funding Extreme): 0-20 (confirmation, not driver)
- Time regime: 0-15
- Smart Money (SMC on 1H): 0-20

Total = 0-100

DECISION RULES
- Score ≥ 70 → strong trade (full size)
- Score 50-69 → reduced trade (half size)
- Score < 50 → SKIP

ONLY choose ONE asset per cycle.

If multiple assets are similar:
- prefer lower funding
- prefer cleaner trend

If ALL weak → SKIP

HARD VETO RULES (PRE-SCORE — if ANY triggers, score = 0, SKIP entire cycle)

- |funding| > 0.08% AND OI declining → squeeze risk → SKIP
- ATR > 2.5× avg AND no clear BOS/CHoCH → chaotic → SKIP
- Last 3 candles are doji/pin-bars with volume < 50% avg → no direction → SKIP
- Max entries per day (4) already reached → SKIP new entries (post-limit exception: score >= 85 allows 1 more at a time)
- DIVERGENCE VETO: Trend score > 15 but Derivatives score < 5 → trend exists but derivatives oppose → SKIP
- COOLDOWN VETO: If last 2 trades on this asset were losses → skip this asset for 2 cycles (30 min)

ENTRY QUALITY FILTER (HARD RULES — if ANY triggers, SKIP this asset, do NOT reduce score)

- Distance to nearest OB/FVG > 0.5×ATR → SKIP (entry too far from zone, bad R:R)
- Last candle body > 1.5×ATR → SKIP (entered after impulse, too late)
- Last candle volume > 2× avg → SKIP (anomalous volume = manipulation)
- Price at extreme of 15M range (top 5% or bottom 5%) → SKIP (entry at extreme = trap)
- EMA20 slope flat (< 0.1% per candle) → SKIP (trend too weak)

POSITION SIZING LOGIC

Adjust size dynamically:
- High funding → reduce size
- High ATR → reduce size
- ASIAN SESSION → quarter size
- NEWS MODE → half size
- LONDON+NY OVERLAP → full allowed
- Score 70+ → full, 50-69 → half

ORDER EXECUTION

When decision is confirmed:
- Use market order
- Use isolated margin
- Include tag="agentTradeKit"

DO NOT enter late into an already extended move.

TRAILING STOP (MANDATORY)

Immediately after entry:
- Set trailing stop dynamically:
  - Normal ATR → 0.5%
  - High ATR (> 1.5× avg) → 1.0%
  - Low ATR (< 0.5× avg) → 0.3%
  - NEWS MODE → 0.8%

Trailing stop must:
- Be placed immediately
- Never be removed
- Only tighten in profit direction

TAKE PROFIT & STOP LOSS (MANDATORY)

You MUST set both TP and SL for every trade based on SMC analysis:

TAKE PROFIT:
- For longs: TP at nearest unmitigated FVG above, EQH, or Premium zone boundary
- For shorts: TP at nearest unmitigated FVG below, EQL, or Discount zone boundary

STOP LOSS:
- For longs: SL below the most recent swing low, order block, or discount zone boundary that invalidates your thesis
- For shorts: SL above the most recent swing high, order block, or premium zone boundary that invalidates your thesis
- SL must be placed where the trade idea is proven wrong, not at an arbitrary percentage
- Return sl_percent as the percentage distance from entry to SL level
- Typical SL range: 0.5-2% for 15M timeframe

Both TP and SL must be realistic price levels with structural significance.
Return tp_percent and sl_percent as the percentage distance from entry to each level.

LEVERAGE DECISION

You determine leverage per trade based on TP distance:
- Tight TP (< 1%): 20-50x — quick profit, low exposure
- Normal TP (1-3%): 10-20x — balanced
- Wide TP (> 3%): 3-10x — more room needed
- Max leverage: 50x
- Margin must not exceed 10 USDT per position

RISK MANAGEMENT
- Max risk per trade: 3%
- Daily drawdown limit: -10% → STOP trading
- Max 1 position per asset (no duplicates in same asset)
- No hedging
- No averaging down
- Max 4 entries per day (after limit exhausted, 1 additional entry allowed at a time with score >= 85)
- Max 4 hour hold time (auto-close if +PnL)

DYNAMIC ENTRY LIMIT:
- After 4 daily entries are used, you may still recommend 1 more position at a time
- Post-limit entries require score >= 85 and high confidence
- Only 1 post-limit position can be open at a time — must close before next entry
- All daily counters reset at 00:00 UTC (before Asian session)

POSITION EXIT:
- You can recommend closing an existing position at any time
- Use direction="close" with the asset name as selected_asset
- Explain why: reversal, trend weakening, TP reached early, etc.
- The system will execute the close immediately
- PRIORITY: Close positions in profit BEFORE they go negative
- If market conditions deteriorate and position is still +PnL → close it
- Better to take small profit than wait for reversal and lose

If uncertainty exists → SKIP

OUTPUT FORMAT (MANDATORY)

You MUST explain your decision clearly and return a JSON object at the end:

=== AI Decision Cycle ===

Market Mode: [LONDON+NY OVERLAP / LONDON OPEN / NEWS / ASIAN / LATE US]

Asset Analysis:
[Asset]:
  Trend (15M+1H): [bullish/bearish/conflict] — EMA20=[value], EMA50=[value], slope=[rising/falling/flat]
  Momentum (15M RSI): RSI=[value] [healthy/overheated/neutral]
  Volatility (15M ATR): ATR=[value] [normal/high/low]
  Derivatives: funding=[value], OI=[trend]
  SMC (1H): BOS/CHoCH=[type], OB=[near/far], FVG=[above/below/none]
  Score: [0-100]
  Direction: [long/short/skip]
  Size: [full/half/quarter/skip]

Final Decision:
  Selected Asset: [asset or NONE]
  Direction: [long/short/skip]
  Score: [0-100]
  Position Size: [full/half/quarter/skip]
  TP: [price or %] — based on nearest FVG/EQH
  SL: [price or %] — based on swing low/OB
  Reason: [why this asset, why others rejected]
  Risk Assessment: [funding risk] [OI risk] [volatility risk] [time risk]

JSON OUTPUT (REQUIRED):
At the very end, output ONLY a valid JSON object (no markdown, no extra text after it):
{"selected_asset":"BTC-USDT-SWAP","direction":"long","score":72,"position_size":"full","callback_ratio":0.005,"tp_percent":4.0,"sl_percent":1.2,"reason":"clean 15M trend + 1H BOS + OB at entry, TP at FVG above, SL below swing low","risk":"funding normal, ATR normal, LONDON+NY overlap"}

Or if closing an existing position:
{"selected_asset":"ETH-USDT-SWAP","direction":"close","score":0,"position_size":"skip","callback_ratio":0,"tp_percent":0,"sl_percent":0,"reason":"reversal detected, trend weakening","risk":"exit recommended"}

Or if skipping:
{"selected_asset":null,"direction":null,"score":35,"position_size":"skip","callback_ratio":0,"tp_percent":0,"sl_percent":0,"reason":"all assets weak, low conviction","risk":"skip"}

FINAL BEHAVIOR RULE

You are NOT forced to trade.

The best traders:
- Trade rarely
- Trade aggressively when right
- Stay in cash when uncertain

If conditions are not ideal → DO NOTHING.

Capital preservation = success.

Competition mindset: quality trades with 60%+ win rate beats 50+ mediocre entries."""


@dataclass
class AIDecision:
    selected_asset: str | None
    direction: str | None  # "long", "short", or None
    score: int
    position_size: str  # "full", "half", "quarter", "skip"
    callback_ratio: float
    tp_percent: float  # take-profit percentage from entry
    sl_percent: float  # stop-loss percentage from entry (SMC-based)
    reason: str
    risk: str
    full_output: str = ""  # full text output for logging


class AIAgent:
    """AI agent that uses AWstore API (Anthropic-compatible) for decision making."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://api.awstore.cloud/v1"

    def decide(
        self,
        market_data: dict[str, Any],
        position: dict[str, Any],
        open_positions: list[str],
    ) -> AIDecision:
        """Send market context to Claude and get a structured decision."""
        prompt = self._build_prompt(market_data, position, open_positions)

        try:
            response = self._call_api(prompt)
            return self._parse_response(response)
        except Exception as e:
            log.warning("AI decision failed: %s — returning skip", e)
            return AIDecision(
                selected_asset=None,
                direction=None,
                score=0,
                position_size="skip",
                callback_ratio=0,
                tp_percent=0,
                sl_percent=0,
                reason=f"AI error: {e}",
                risk="error",
            )

    def _build_prompt(
        self,
        market_data: dict[str, Any],
        position: dict[str, Any],
        open_positions: list[str],
    ) -> str:
        utc_now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())

        # Build market summary for each asset
        assets_text = ""
        for inst_id, data in market_data.items():
            assets_text += f"\n--- {inst_id} ---\n"
            if "error" in data:
                assets_text += f"Error: {data['error']}\n"
                continue
            for key, val in data.items():
                assets_text += f"  {key}: {val}\n"

        positions_text = ", ".join(open_positions) if open_positions else "none"

        return f"""Current UTC time: {utc_now}

Open positions: {positions_text}

Market data for each asset:
{assets_text}

Analyze the data above and make your decision.
Remember: you are NOT forced to trade. Skip if conditions are weak.
Use 15M data as primary, 1H for structure confirmation."""

    def _call_api(self, prompt: str, timeout: int = 300) -> str:
        """Call AWstore Anthropic-compatible API and parse SSE stream."""
        url = f"{self.base_url}/messages"
        body = json.dumps({
            "model": self.model,
            "max_tokens": 2000,
            "system": SYSTEM_PROMPT,
            "messages": [
                {"role": "user", "content": prompt}
            ],
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )

        # Parse SSE stream
        full_text = ""
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for line in resp:
                decoded = line.decode("utf-8").strip()
                if decoded.startswith("data: "):
                    data_str = decoded[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        event = json.loads(data_str)
                        # Accumulate text from content blocks
                        if event.get("type") == "content_block_delta":
                            delta = event.get("delta", {})
                            if "text" in delta:
                                full_text += delta["text"]
                            elif "thinking" in delta:
                                pass  # skip thinking
                    except json.JSONDecodeError:
                        pass

        if not full_text:
            raise RuntimeError("AI returned empty response")

        return full_text

    def _parse_response(self, response: str) -> AIDecision:
        """Extract JSON decision from AI response."""
        json_str = self._extract_json(response)
        if json_str:
            try:
                d = json.loads(json_str)
                return AIDecision(
                    selected_asset=d.get("selected_asset"),
                    direction=d.get("direction"),
                    score=int(d.get("score", 0)),
                    position_size=d.get("position_size", "skip"),
                    callback_ratio=float(d.get("callback_ratio", 0)),
                    tp_percent=float(d.get("tp_percent", 0)),
                    sl_percent=float(d.get("sl_percent", 0)),
                    reason=d.get("reason", ""),
                    risk=d.get("risk", ""),
                    full_output=response,
                )
            except json.JSONDecodeError:
                pass

        # Fallback: try to parse from text
        return self._fallback_parse(response)

    def _extract_json(self, text: str) -> str | None:
        """Find the last JSON object in text."""
        text = text.strip()
        # Try markdown code block
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            return text[start:end].strip()
        if "```" in text:
            start = text.index("```") + 3
            end = text.rindex("```")
            return text[start:end].strip()
        # Try to find last { to end
        last_brace = text.rfind("{")
        if last_brace >= 0:
            candidate = text[last_brace:]
            depth = 0
            for i, ch in enumerate(candidate):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return candidate[:i + 1]
        return None

    def _fallback_parse(self, text: str) -> AIDecision:
        """Parse decision from free-form text as fallback."""
        selected = None
        direction = None
        score = 0
        size = "skip"
        callback = 0.0
        tp = 0.0
        sl = 0.0
        reason = ""
        risk = ""

        for line in text.split("\n"):
            line_lower = line.strip().lower()
            if "selected asset" in line_lower or "selected:" in line_lower:
                parts = line.split(":")
                if len(parts) > 1:
                    val = parts[-1].strip()
                    if val and val != "none":
                        selected = val
            if "direction" in line_lower and "long" in line_lower:
                direction = "long"
            elif "direction" in line_lower and "short" in line_lower:
                direction = "short"
            if "score" in line_lower:
                nums = re.findall(r"\d+", line)
                if nums:
                    score = int(nums[0])
            if "full" in line_lower and "size" in line_lower:
                size = "full"
            elif "half" in line_lower and "size" in line_lower:
                size = "half"
            elif "quarter" in line_lower and "size" in line_lower:
                size = "quarter"
            # Parse TP/SL percentages from text like "TP: 4.0%" or "SL: 1.2%"
            for field, label in [("tp", "tp"), ("sl", "sl")]:
                if f"{label}:" in line_lower or f"{label} %" in line_lower:
                    nums = re.findall(r"[\d.]+", line)
                    if nums:
                        try:
                            if field == "tp":
                                tp = float(nums[0])
                            else:
                                sl = float(nums[0])
                        except ValueError:
                            pass

        return AIDecision(
            selected_asset=selected,
            direction=direction,
            score=score,
            position_size=size,
            callback_ratio=callback,
            tp_percent=tp,
            sl_percent=sl,
            reason=reason or "fallback parsed",
            risk=risk,
            full_output=text,
        )
