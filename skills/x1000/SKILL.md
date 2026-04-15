---
name: x1000
description: "AI-driven multi-asset perpetual futures strategy optimized for 14-day OKX Agent Trade Kit competition. Uses 15M candles as primary timeframe with 1H structure confirmation. Dual-loop architecture: Entry Loop (every 15 min) for new positions, Monitoring Loop (every 5 min) for early exit on reversal detection. Features Hard Veto Rules (Divergence + Cooldown), Entry Quality Filter (HARD SKIP), 6-dimension scoring (trend 0-25, momentum 0-10, ATR 0-10, derivatives 0-20, time regime 0-15, SMC 0-20), Smart Money Concepts analysis, Funding Extreme Filter, Liquidation Cluster Analysis (Hyperliquid order book depth), Whale Flow Filter (Hyperliquid large trades), adaptive time-aware market mode detection, mandatory trailing stop + hard SL, ATR-based leverage capping, progressive conviction threshold, and strict risk management. All orders placed via Agent Trade Kit with tag=agentTradeKit."
license: MIT
metadata:
  author: x1000
  version: "2.0.0"
  homepage: "https://www.okx.com"
  agent:
    requires:
      bins: ["okx"]
    install:
      - id: npm
        kind: node
        package: "@okx_ai/okx-trade-cli"
        bins: ["okx"]
        label: "Install OKX CLI (npm)"
---

# x1000 — AI Multi-Asset Perpetual Strategy (Competition Edition)

An autonomous trading strategy for the OKX Agent Trade Kit competition. Uses **15M candles** as the primary timeframe for timely signals, with **1H candles** for structural confirmation. Dual-loop architecture: **Entry Loop** (every 15 min) finds new positions, **Monitoring Loop** (every 5 min) detects reversals for early profit-taking.

**Goal: Maximize PnL% with strict risk control. Quality over quantity — high-conviction trades with 60%+ win rate.**

**All orders are placed automatically via Agent Trade Kit with `tag=agentTradeKit`.**

## Strategy Overview

| Parameter | Value |
|---|---|
| **Primary TF** | 15M candles (60 periods = 15 hours) |
| **Structure TF** | 1H candles (24 periods = 24 hours) |
| **Entry Loop** | Every 15 minutes |
| **Monitoring Loop** | Every 5 minutes (positions only) |
| **Assets** | BTC-USDT-SWAP, ETH-USDT-SWAP, SOL-USDT-SWAP, HYPE-USDT-SWAP |
| **Margin mode** | Isolated only |
| **Max margin per position** | 10 USDT (own capital) |
| **Max leverage** | 50x (agent decides per trade) |
| **Max loss per trade** | 3% of account capital |
| **Max entries per day** | 4 |
| **Max hold time** | 4 hours (auto-close if +PnL) |
| **Daily kill switch** | -10% daily drawdown |
| **Hedging** | Not allowed |
| **Averaging down** | Not allowed |
| **Duplicate entries** | Not allowed — no second position in the same asset |
| **Stop-loss** | Trailing stop (primary) + hard SL (emergency backup) |
| **Order tag** | `agentTradeKit` (required for competition ranking) |

## Core Principles

- **PnL% is the win metric** — fewer high-quality trades beat many mediocre ones
- **15M primary** — 1H is too slow; by the time a 1H candle closes, half the move is gone
- **Early exit on reversal** — don't wait for trailing stop if reversal signals are strong
- **Trade ONLY through Agent Trade Kit** — no manual orders
- **Never average down** losing positions
- **Isolated margin only** — each position's risk is contained
- **Progressive conviction** — each new position requires a higher score

---

## Step 1 · Collect Market Data

### 1.1 — Primary: 15M Candles (Entry Signals)

For each of the four assets, fetch 15M candles (last 60 = 15 hours):

```bash
okx market candles BTC-USDT-SWAP --bar 15m --limit 60 --json
okx market candles ETH-USDT-SWAP --bar 15m --limit 60 --json
okx market candles SOL-USDT-SWAP --bar 15m --limit 60 --json
okx market candles HYPE-USDT-SWAP --bar 15m --limit 60 --json
```

Calculate for each asset on 15M:
- **EMA20** — 20-period EMA (fast trend on 15M)
- **EMA50** — 50-period EMA (medium trend on 15M)
- **RSI(14)** — momentum oscillator
- **ATR(14)** — average true range (volatility)
- **Volume vs 20-period average**

### 1.2 — Structure: 1H Candles (SMC Context)

For each asset, fetch 1H candles (last 24 = 24 hours):

```bash
okx market candles BTC-USDT-SWAP --bar 1H --limit 24 --json
okx market candles ETH-USDT-SWAP --bar 1H --limit 24 --json
okx market candles SOL-USDT-SWAP --bar 1H --limit 24 --json
okx market candles HYPE-USDT-SWAP --bar 1H --limit 24 --json
```

Use 1H data for:
- **SMC structure** (BOS/CHoCH, Order Blocks, FVG) — more reliable on higher TF
- **Trend confirmation** — does 1H trend align with 15M signal?
- **Premium/Discount zones** — calculated from 1H swing range

### 1.3 — Derive from 15M Candles

- Price position relative to EMA20 and EMA50
- EMA20 slope (rising / falling / flat)
- EMA20/EMA50 crossover status
- Last candle range (high - low) vs ATR
- Last candle body size (|close - open|) vs ATR
- Volume vs 20-period average
- Local high/low of the last 8-12 candles

---

## Step 2 · Derivative Analysis

### 2.1 — Funding Rate

```bash
okx market funding-rate BTC-USDT-SWAP --json
okx market funding-rate ETH-USDT-SWAP --json
okx market funding-rate SOL-USDT-SWAP --json
okx market funding-rate HYPE-USDT-SWAP --json
```

For each asset, assess:
- **Funding overheating**: |funding| approaching or exceeding 0.1% → caution, halve position
- **OI growth**: Is open interest increasing? (confirms trend participation)
- **Price-OI alignment**: Does OI confirm the price move, or signal a potential squeeze?
- **Squeeze signs**: OI rising but price stalling = crowded trade, potential reversal

### 2.2 — Open Interest

```bash
okx market open-interest --instType SWAP --instId BTC-USDT-SWAP --json
okx market open-interest --instType SWAP --instId ETH-USDT-SWAP --json
okx market open-interest --instType SWAP --instId SOL-USDT-SWAP --json
okx market open-interest --instType SWAP --instId HYPE-USDT-SWAP --json
```

### 2.3 — Funding Extreme Filter (NEW)

Extreme funding rates are powerful contrarian signals:

| Condition | Signal | Score Adjustment |
|---|---|---|
| Funding < -0.5% (daily) | Short squeeze potential — strong bullish | +8 pts to bullish score |
| Funding > +0.5% (daily) | Long overcrowded — caution | -5 pts to bullish score |
| Funding < -0.2% in uptrend | Shorts trapped, squeeze likely | +5 pts to bullish score |
| Funding > +0.2% in downtrend | Longs trapped, flush likely | +5 pts to bearish score |
| Raw vs OI-weighted divergence > 0.3% | Large players positioning opposite retail | +3 pts caution |

### 2.4 — Liquidation Cluster Analysis

Real-time liquidation cluster data is fetched from Hyperliquid L2 order book depth.
Large walls in the order book represent potential liquidation zones.

| Condition | Signal | Adjustment |
|---|---|---|
| Long-liq cluster within 1.5% below price | Price may sweep before rally | -5 pts for longs (sweep risk) |
| Short-liq cluster within 1.5% above price | Price may sweep before drop | +5 pts for longs (short squeeze potential) |
| Price between tight clusters (< 0.5% apart) | Trapped range | VETO — skip, wait for breakout |
| Hard SL placement for LONG | Place BELOW long-liq cluster | Not above it — avoid sweep |
| Hard SL placement for SHORT | Place ABOVE short-liq cluster | Not below it — avoid sweep |

### 2.5 — Whale Flow Filter

Real-time whale flow data is fetched from Hyperliquid recent large trades (>$100k).
Buy/sell ratio of institutional trades shows directional consensus.

| Condition | Signal | Adjustment |
|---|---|---|
| Whale buy_pct >= 70% | Strong bullish institutional consensus | +5 pts bullish |
| Whale sell_pct >= 70% | Strong bearish institutional consensus | +5 pts bearish |
| Whale buy_pct 40-60% (split) | Uncertainty — institutions disagree | -3 pts caution |
| Funding negative + OI rising + price rising | Smart money long, retail short | +5 pts bullish (short squeeze building) |
| Funding positive + OI rising + price falling | Smart money short, retail long | +5 pts bearish (long flush building) |
| Funding extreme + OI declining | Large players exiting, retail trapped | -8 pts (uncertainty — reduce or skip) |
| OI surging (> 5% in 1h) without price move | Large positions building, breakout imminent | +3 pts caution — wait for direction |

---

## Step 2.6 · Time & Market Mode Analysis

The AI determines the current market mode based on UTC time:

### Mode 1: NY OVERLAP (13:30–16:30 UTC) — HIGHEST PRIORITY
- **Characteristics**: NYSE opens at 13:30, London still active — peak global liquidity, 2-3× average volume, cleanest trends
- **Key window**: 13:30–15:30 UTC = NY Killzone, most reliable SMC structures
- **Action**: Most aggressive — prioritize breakout and trend-following setups; 1.5x size allowed
- **Entry interval**: Every 15 minutes

### Mode 2: LONDON OPEN (07:00–13:30 UTC)
- **Characteristics**: Frankfurt opens at 07:00 (pre-market, potential Judas Swing false moves), LSE opens at 08:00 with main volume. London Killzone 08:00–10:00 UTC forms most reliable 1H Order Blocks and FVGs.
- **Action**: Prioritize breakout setups from Asian range; full size after 08:00
- **Entry interval**: Every 15 minutes

### Mode 3: NEWS / MACRO MODE (±1 hour from 12:30 / 13:30 / 14:00 / 16:00 / 20:00 UTC)
- **Characteristics**: US macro data (CPI, NFP) released at 12:30 or 14:00 UTC depending on DST. FOMC/NFP at 13:30-14:30 UTC.
- **Action**: Trade ONLY strongest setups (score ≥ 75); half size
- **Entry interval**: Every 5 minutes (monitoring only, entries rare)

### Mode 4: ASIAN SESSION (00:00–07:00 UTC)
- **Characteristics**: Tokyo opens 23:00 UTC (prev day), HK/Singapore 01:00 UTC. Critical window 00:30–03:00 UTC (CNY fixing, Chinese exchanges open). Thin order books, wider spreads, false breakouts. 07:00 UTC = dead zone before London.
- **Action**: Only extreme setups (score ≥ 80); quarter size
- **Entry interval**: Every 60 minutes

### Mode 5: US LATE (16:30–20:00 UTC)
- **Characteristics**: European traders leave after 16:30, liquidity drops. US trends continue but with less conviction. After 20:00 UTC = flat mode (London closed, fixings settled).
- **Action**: 16:30–20:00: dotorhovka US trends, 0.75x size. After 20:00: close only, no new entries.
- **Entry interval**: Every 30 minutes (16:30–20:00), disabled after 20:00

---

## Step 2.6 · Smart Money Concepts (SMC) Analysis

SMC is computed from **1H candle data** (Step 1.2) for reliability.

### 2.6.1 — Swing Structure & BOS / CHoCH
- **BOS**: Price breaks swing high/low confirming trend continuation
- **CHoCH**: Price breaks last swing point defining current trend → potential reversal

### 2.6.2 — Order Blocks (OB)
- **Bullish OB**: Last bearish candle before bullish BOS — institutional buying zone
- **Bearish OB**: Last bullish candle before bearish BOS — institutional selling zone

### 2.6.3 — Fair Value Gaps (FVG)
- **Bullish FVG**: `low[i] > high[i-2]` — aggressive buying left unfilled orders
- **Bearish FVG**: `high[i] < low[i-2]` — aggressive selling
- FVGs act as **price magnets** — TP targets

### 2.6.4 — Premium & Discount Zones
- **Premium**: Top 50% of swing range — expensive, prefer shorts
- **Discount**: Bottom 50% of swing range — cheap, prefer longs

### 2.6.5 — SMC Composite Score (0–20 points)

| SMC Condition | Points |
|---|---|
| BOS confirms trend + OB at entry zone + FVG as target | 16-20 |
| BOS confirms trend but no nearby OB | 11-15 |
| CHoCH detected (reversal) + OB confirmation | 11-15 |
| CHoCH without OB confirmation | 6-10 |
| No clear SMC structure / conflict with indicators | 0-5 |
| Price in Discount zone (for longs) or Premium zone (for shorts) | +2 bonus |
| EQH/EQL liquidity sweep detected nearby | +2 bonus |

---

## Step 2.7 · Entry Quality Filter (HARD RULES)

> **Even a strong trend can be a bad entry if price is too far from the trigger zone.**
> **If ANY check fails → SKIP this asset entirely. Do NOT reduce score and trade anyway.**

| Check | Pass | Fail → SKIP |
|---|---|---|
| Distance to nearest OB/FVG | ≤ 0.5×ATR from entry | > 0.5×ATR (too far, bad R:R) |
| Last candle body size | ≤ 1.5×ATR (normal) | > 1.5×ATR (entered after impulse, too late) |
| Last candle volume | ≤ 2× 20-period avg | > 2× avg (anomalous volume = manipulation) |
| Price position in 15M range | Not in top/bottom 5% | At extreme (trap zone) |
| EMA20 slope | ≥ 0.1% per candle | Flat (< 0.1% — trend too weak) |

---

## Step 2.8 · Hard Veto Rules (PRE-SCORE)

> **Mandatory veto conditions. If ANY veto triggers, the cycle MUST be skipped. Score = 0.**

| Veto Condition | Action |
|---|---|
| \|funding\| > 0.08% AND OI declining → squeeze risk | SKIP entire cycle |
| ATR > 2.5× avg AND no clear BOS/CHoCH → chaotic market | SKIP entire cycle |
| Last 3 candles are doji/pin-bars with volume < 50% avg → no direction | SKIP entire cycle |
| Max entries per day (4) already reached | SKIP new entries, continue monitoring |
| **DIVERGENCE VETO**: Trend score > 15 but Derivatives score < 5 → trend exists but derivatives oppose | SKIP entire cycle |
| **COOLDOWN VETO**: Last 2 trades on this asset were losses → skip for 2 cycles (30 min) | SKIP this asset |

---

## Step 3 · AI Decision-Making (PRIMARY STEP)

### 3.0 — Execution Flow

The decision pipeline runs in this order:
1. **Data Collection** (Step 1-2)
2. **Hard Veto Check** (Step 2.8) — if veto triggers → SKIP
3. **Entry Quality Filter** (Step 2.7) — if fails → reduce score or SKIP
4. **Scoring** (Step 3.1 below) — only for assets that passed veto + entry filter
5. **Asset Selection** (Step 4) — pick best or skip

### 3.1 — Comprehensive Evaluation

For each asset that passed veto checks, evaluate six dimensions:

#### Dimension 1: Trend Strength (0–25 points) — 15M + 1H alignment
| Condition | Points |
|---|---|
| 15M AND 1H both bullish (price > EMA20 > EMA50, EMA20 rising) | 16-20 |
| 15M AND 1H both bearish (price < EMA20 < EMA50, EMA20 falling) | 16-20 |
| 15M bullish but 1H flat/neutral | 10-15 |
| 15M bearish but 1H flat/neutral | 10-15 |
| 15M and 1H conflict (opposite directions) | 0-5 |
| Both flat / EMAs tangled | 0-5 |

#### Dimension 2: Momentum (0–10 points) — 15M RSI
| RSI Condition | Points |
|---|---|
| RSI 50-68 in uptrend (healthy momentum) | 6-8 |
| RSI 32-50 in downtrend (healthy momentum) | 6-8 |
| RSI 40-60 with flat EMAs (range-bound) | 0-2 |
| RSI > 75 or < 25 (overheated) | 0-4 (caution penalty) |

#### Dimension 3: Volatility Regime (0–10 points) — 15M ATR
| ATR Condition | Points |
|---|---|
| ATR near or above 20-period average (sufficient movement) | 9-12 |
| ATR 0.5-1.0× average (moderate) | 5-8 |
| ATR < 0.5× average (too quiet — skip) | 0-4 |
| ATR > 2× average (extreme — caution) | 2-6 |

#### Dimension 4: Derivative Confirmation (0–20 points)
| Signal | Points |
|---|---|
| Funding moderate AND aligned with trend, OI growing with trend | 20-25 |
| Funding moderate but OI flat | 12-19 |
| \|funding\| > 0.08% (overheated) | 0-5 (penalty) |
| OI rising but price stalling (divergence) | 0-5 (penalty) |
| OI declining against trend (weakening) | 0-10 |
| Funding negative in uptrend (short squeeze signal) | +3 bonus |
| Funding extreme filter triggered (Step 2.3) | Apply adjustment |

#### Dimension 5: Time / Market Mode (0–15 points)
| Mode + Setup Quality | Points |
|---|---|
| NY OVERLAP (13:30–16:30) + clean trend | 13-15 |
| LONDON OPEN (07:00–13:30) + breakout setup | 11-14 |
| NY OVERLAP + moderate setup | 10-12 |
| NEWS MODE + strongest setup (score ≥ 75 from other dims) | 7-12 |
| US LATE (16:30–20:00) + clean setup | 6-10 |
| ASIAN SESSION (00:00–07:00) + extreme setup (≥ 80) | 3-7 |
| PACIFIC/CLOSE (20:00–00:00) | 0-2 (close only) |
| Any mode + weak setup | 0-5 |

#### Dimension 6: Smart Money Concepts (0–20 points) — 1H
| SMC Condition | Points |
|---|---|
| BOS confirms trend + OB at entry zone + FVG as target | 16-20 |
| BOS confirms trend but no nearby OB | 11-15 |
| CHoCH detected (reversal) + OB confirmation | 11-15 |
| CHoCH without OB confirmation | 6-10 |
| No clear SMC structure / conflict with indicators | 0-5 |
| Price in Discount zone (for longs) or Premium zone (for shorts) | +2 bonus |
| EQH/EQL liquidity sweep detected nearby | +2 bonus |

### 3.2 — Composite Score & Interpretation

**Total: 0–100 points**

| Score Range | Action | Position Size |
|---|---|---|
| 70–100 | Strong trade | Full (up to 3% risk) |
| 50–69 | Reduced trade | Half (up to 1.5% risk) |
| Below 50 | Skip cycle | None |

### 3.3 — AI Output Format

The AI MUST produce a structured decision summary:

```
=== AI Decision Cycle [UTC timestamp] ===

Market Mode: [LONDON+NY OVERLAP / LONDON OPEN / NEWS / ASIAN / LATE US]

Asset Analysis:
  BTC-USDT-SWAP:
    Trend (15M+1H): [bullish/bearish/conflict] — [0-20] pts
    Momentum (15M RSI): RSI=[value] [healthy/overheated/neutral] — [0-8] pts
    Volatility (15M ATR): ATR=[value] [normal/high/low] — [0-12] pts
    Derivatives: funding=[value], OI=[trend] — [0-25] pts
    Time Regime: [mode] — [0-15] pts
    SMC (1H): BOS/CHoCH=[type], OB=[near/far], FVG=[above/below/none] — [0-20] pts
    TOTAL SCORE: [0-100]
    Direction: [long/short/skip]
    Position Size: [full/half/quarter/skip]

Final Decision:
  Selected Asset: [asset or NONE]
  Direction: [long/short/skip]
  Confidence Score: [0-100]
  Position Size: [full/half/quarter/skip]
  TP: [price or %] — based on nearest FVG/EQH
  SL: [price or %] — based on swing low/OB
  Reason: [why this asset, why others rejected]
  Risk Assessment: [funding risk] [OI risk] [volatility risk] [time risk]
```

---

## Step 4 · Asset Selection

**Rules:**
1. Select the single asset with the maximum composite score
2. If two assets are close in score (within 5 points), prefer the one with:
   - Lower absolute funding rate
   - Cleaner EMA structure (less intertwining)
3. **Progressive conviction threshold** — each new position requires a higher score:
   - 1st position: minimum score 50
   - 2nd position: minimum score 60 (previous + 10)
   - 3rd position: minimum score 70 (previous + 10)
   - Nth position: minimum score = previous threshold + 10
4. If the best asset's score is below the current threshold → skip the entire cycle
5. **No duplicate entries** — never open a second position in the same asset
6. **Max 4 entries per day** — standard daily limit
7. **DYNAMIC ENTRY LIMIT (post-limit exception):**
   - After 4 daily entries are exhausted, allow **1 additional position** at a time
   - Post-limit entry requires **score >= 85** and high confidence
   - Cannot open another position until the post-limit position is fully closed
   - Once closed, another post-limit entry is allowed (still requires score >= 85)
   - All counters reset before Asian session (00:00 UTC)

---

## Step 5 · Order Execution

> **Only execute when the AI confirms an entry with score ≥ 50.**

```bash
okx swap place --instId <selected_asset> \
  --side <buy|sell> \
  --ordType market \
  --sz <calculated_size> \
  --tdMode isolated \
  --posSide <long|short> \
  --tag agentTradeKit
```

**Execution rules:**
- Open trade ONLY after AI has finalized the Step 3 decision
- Do NOT place manual duplicate orders outside Agent Trade Kit
- Do NOT open a second position in the same asset (one position per asset max)
- If the market has already moved significantly since the AI decision → skip the cycle

### Position Sizing Adjustments

| Condition | Adjustment |
|---|---|
| Score 70+ | Full size (baseline) |
| Score 50-69 | Half size |
| \|funding\| > 0.1% | Halve the calculated size |
| ATR > 2× average | Quarter size |
| ASIAN SESSION | Quarter size |
| NEWS MODE | Half size |
| LONDON+NY OVERLAP | Allowed full size (no reduction) |

---

## Step 6 · Mandatory Trailing Stop + Hard SL

> **A trailing stop order must be placed immediately after the position is opened. Additionally, a hard stop-loss must be attached to the entry order as an emergency backup.**

### Trailing Stop Calculation

The AI selects the `callbackRatio` based on current market conditions:

| Condition | callbackRatio | Effective Distance |
|---|---|---|
| Normal ATR | 0.005 (0.5%) | Tight — locks in gains quickly |
| High ATR (> 1.5× avg) | 0.010 (1.0%) | Wider — avoids noise stop-outs |
| Low ATR (< 0.5× avg) | 0.003 (0.3%) | Very tight — low volatility allows it |
| NEWS MODE | 0.008 (0.8%) | Wider — accounts for spike wicks |
| US HIGH VOL | 0.005 (0.5%) | Standard — clean trends |

**Trailing stop rules:**
- Trailing stop must be set IMMEDIATELY after entry
- The AI determines the initial `callbackRatio` — no hardcoded single value
- Do NOT cancel trailing stop without a new AI assessment
- Trailing stop only moves in profit direction — never widens on losses

**Hard stop-loss (emergency backup):**
- Attach `slTriggerPx` to the entry order alongside `tpTriggerPx`
- SL level determined by SMC analysis: below swing low / order block for longs, above swing high / order block for shorts
- Hard SL protects against news gaps and exchange latency where trailing stop may not execute

---

## Step 7 · Dual-Loop Architecture (CRITICAL FOR COMPETITION)

### 7.1 — Entry Loop (Every 15 Minutes)

**Purpose**: Find and enter new high-conviction positions.

**Trigger**: Every 15 minutes (adjusted by market mode per Step 2.5)

**Condition**: Only if no position currently open in the selected asset AND daily entry count < 4

**Flow**:
1. Fetch 15M candles (60 periods) + 1H candles (24 periods) for all assets
2. Run full analysis: Steps 1-6
3. If composite score ≥ current threshold → place entry order
4. Log entry time and price

### 7.2 — Monitoring Loop (Every 5 Minutes)

**Purpose**: Detect early reversal signals and close positions with profit BEFORE trailing stop is hit.

**Trigger**: Every 5 minutes

**Condition**: Only if position is currently open

**Flow**:
1. Fetch latest 15M candles (last 12 = 3 hours) for the asset with open position
2. Calculate current PnL%, time in trade
3. Run Reversal Detection Algorithm (Step 7.3)
4. Decision: HOLD / TIGHTEN SL / CLOSE EARLY

### 7.3 — Reversal Detection Algorithm

The AI must detect 4 types of reversals on 15M candles:

**Type 1: RSI Divergence (Most Reliable)**
- **Bearish divergence** (for LONG positions): Price makes higher high, RSI makes lower high → momentum weakening
- **Bullish divergence** (for SHORT positions): Price makes lower low, RSI makes higher low → momentum weakening
- Score: +1 reversal point

**Type 2: EMA Crossover (Fast Reversal Signal)**
- **For LONG**: Price crosses BELOW EMA20 → bearish signal
  - Severity HIGH if price also below EMA50
- **For SHORT**: Price crosses ABOVE EMA20 → bullish signal
  - Severity HIGH if price also above EMA50
- Score: EMA20 crossover = +1, EMA50 crossover = +2

**Type 3: SMC CHoCH (Change of Character)**
- **For LONG**: Price closes below last swing low that defined the uptrend → CHoCH bearish
- **For SHORT**: Price closes above last swing high that defined the downtrend → CHoCH bullish
- Score: +2 reversal points

**Type 4: Volume Collapse + Price Stall**
- Last candle volume < 0.3× avg_volume_20 AND last candle range < 0.5× ATR
- Score: +1 reversal point

### 7.4 — Reversal Score → Action

**Confirmation Candle Rule:** When reversal_score ≥ 2, do NOT close immediately. Set flag and wait for next 15M candle. If score stays same or increases → confirmed → act. If score drops → false alarm → HOLD.

| Reversal Score | PnL Condition | Time in Trade | Action |
|---|---|---|---|
| ≥ 3 (confirmed) | > 0% | Any | **CLOSE immediately** — strong reversal, exit now |
| ≥ 2 (confirmed) | > 0.3% | > 5 min | **CLOSE** — moderate reversal + profit locked |
| ≥ 2 (confirmed) | ≤ 0.3% | Any | **TIGHTEN trailing stop** to 0.25% |
| 1 | > 0.8% | > 10 min | **CLOSE** — weak reversal + good profit + time decay |
| 1 | ≤ 0.8% | Any | **MONITOR** — weak signal, let trailing stop work |
| 0 | Any | Any | **HOLD** — no reversal, trailing stop active |

### 7.6 — Profit Target Exit (NEW)

After entry, the AI calculates TP level based on nearest FVG / EQH / OB. During monitoring:

| Condition | Action |
|---|---|
| Current price ≥ TP_level - 0.1%×ATR (for long) | **CLOSE** — TP reached |
| Current price ≤ TP_level + 0.1%×ATR (for short) | **CLOSE** — TP reached |

This ensures you don't leave money on the table waiting for trailing stop.

### 7.5 — Position Holding Time Limits

| Rule | Limit |
|---|---|
| Max hold time | 4 hours — if open > 4h AND PnL > 0.5% → close automatically |
| Min hold time | 5 minutes — do NOT close within 5 min of entry |
| Daily entry limit | 4 new positions per day |

---

## Risk Management

| Rule | Limit |
|---|---|
| Max margin per position | 10 USDT (own capital in isolated mode) |
| Max leverage | 50x (agent decides per trade based on TP distance) |
| Max loss per trade | 3% of account capital |
| Daily kill switch | Stop opening positions if daily PnL < -10% |
| Margin mode | Isolated only |
| Hedging | Not allowed |
| Averaging down | Not allowed |
| Duplicate entries | Not allowed — no second position in same asset |
| Stop-loss | Trailing stop (primary) + hard SL (emergency backup) |
| Funding > 0.1% (absolute) | Halve position size |
| High volatility | Reduce position size only, never increase risk |
| Conflicting signals / insufficient data | Skip cycle |
| Max entries per day | 4 |
| Max hold time | 4 hours (auto-close if +PnL) |

### Leverage Decision Logic

The agent determines leverage per position based on TP distance AND ATR volatility:

- **Tight TP (< 1%)**: Higher leverage (20-50x) — quick profit, low exposure time
- **Normal TP (1-3%)**: Medium leverage (10-20x) — balanced risk
- **Wide TP (> 3%)**: Lower leverage (3-10x) — more room for price movement
- **ATR-based cap**: `max_leverage = 0.5% / (ATR_pct × 2)` — ensures a 2×ATR move doesn't liquidate
  - e.g. ATR 1% → max 25x; ATR 2% → max 12.5x; ATR 0.5% → max 50x

Final leverage = min(50, TP-based, ATR-based, notional/10)

---

## Adaptive Logic

| Market Condition | AI Adaptation |
|---|---|
| **Strong trend (15M+1H aligned)** | Prefer asset with best EMA + RSI + OI combination |
| **NY OVERLAP (13:30–16:30)** | Most aggressive; 1.5x size; standard trailing (0.5%); entries every 15 min |
| **LONDON OPEN (07:00–13:30)** | Aggressive; full size; watch for Asian range breakout; entries every 15 min |
| **NEWS / MACRO mode** | Trade only top signals (score ≥ 75); half size; wider trailing (0.8%); entries every 5 min |
| **ASIAN SESSION (00:00–07:00)** | Reduce risk to quarter; only extreme setups (≥ 80); entries every 60 min |
| **US LATE (16:30–20:00)** | Reduced size (0.75x); prefer closing positions; entries every 30 min |
| **PACIFIC/CLOSE (20:00–00:00)** | Close only, no new entries |
| **Funding extreme (< -0.5%)** | Strong contrarian bullish signal — consider short squeeze setup |
| **Funding extreme (> +0.5%)** | Overheated longs — caution, reduce size or skip |
| **Low volatility** | Skip trades entirely; tight trailing (0.3%) |
| **Sharp impulse / spike** | Only highest-conviction trades (score ≥ 70) |
| **Conflicting signals** | Skip — capital preservation |

---

## Decision Journal

After each cycle, the AI must log a brief journal entry:

```
=== Decision Journal [UTC timestamp] ===
Market Mode: [mode]
Selected: [asset] / [direction] / [score]
Rejected: [asset] because [reason]
Rejected: [asset] because [reason]
Risk: [funding risk] [OI risk] [volatility risk] [time risk]
Action: [opened position at X / skipped cycle]
```

For monitoring cycles:
```
=== Monitoring Cycle [UTC timestamp] ===
Position: [asset] [long/short] | Entry: [price] | Current: [price]
PnL: [+X.XX%] | Time in trade: [X minutes]
Reversal Signals: RSI div=[Y/N] EMA cross=[Y/N] CHoCH=[Y/N] Vol collapse=[Y/N]
Reversal Score: [0-5]
Action: [HOLD / TIGHTEN SL / CLOSE]
Reason: [specific reason]
```

---

## Final Rule

**If there is no strong signal across the combination of EMA structure, RSI momentum, ATR regime, funding rate, open interest flow, AND the time regime is not favorable — DO NOT TRADE.**

Capital preservation is the highest priority. A skipped cycle is a successful risk management decision.

**Competition mindset: quality trades with 60%+ win rate beats 50+ mediocre entries.**
