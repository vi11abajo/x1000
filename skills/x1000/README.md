# x1000 — AI Multi-Asset Perpetual Strategy

> Built for the OKX Agent Trade Kit Competition

## Overview

x1000 is an AI-driven perpetual futures trading agent that trades **BTC-USDT-SWAP**, **ETH-USDT-SWAP**, **SOL-USDT-SWAP**, and **HYPE-USDT-SWAP**. Uses Claude (via AWstore API) for decision-making with a 6-dimension scoring framework, Smart Money Concepts analysis, real-time liquidation/whale data from Hyperliquid, and a dual-loop architecture for entry and position monitoring.

### Key Features

- **AI Decision Layer** — Claude synthesizes 6 signal dimensions into a composite 0-100 score
- **Dual-Loop Architecture** — Entry Loop (adaptive 15-60min) + Monitoring Loop (every 5min) for early exit on reversal detection
- **Smart Money Concepts** — BOS/CHoCH, Order Blocks, Fair Value Gaps, Premium/Discount zones on 1H candles
- **Hyperliquid Data** — Real-time liquidation cluster zones (L2 order book depth) and whale flow (large trades >$100k)
- **Hard Veto Rules** — Divergence veto, cooldown after 2 losses, EMA20 flat slope filter
- **Entry Quality Filter** — 5 hard checks that must all pass before entry
- **Dynamic Entry Limit** — After 4 daily entries, allows 1 additional position at a time with score >= 85
- **Time-Aware Market Mode** — 6 session modes with adaptive sizing and entry intervals
- **Mandatory Trailing Stop + Hard SL** — Every position has both; trailing stop tightens on reversal signals

### Competition Criteria

| Requirement | Status |
|---|---|
| AI decision-making step | Step 3 (6-dimension scoring breakdown) |
| Orders via Agent Trade Kit | All orders use `tag=agentTradeKit` |
| Clear stop-loss rules | Step 6 (trailing stop + hard SL, ATR-adjusted) |
| Risk management | Defined limits, kill switch, position caps |
| Deep AI analysis | 6-dimension scoring, SMC, market mode detection |
| Multi-signal fusion | EMA + RSI + ATR + Funding + OI + SMC + Liquidation + Whale Flow |
| Adaptive logic | 6 market modes, dynamic sizing, progressive conviction |

## Installation

```bash
npm install -g @okx_ai/okx-trade-cli
okx config init
```

## Usage

```bash
# AI-driven mode (recommended)
python -m x1000_agent.main -v run --ai

# Rule-based mode (legacy)
python -m x1000_agent.main -v run
```

## Strategy Structure

```
Step 1    → Collect Market Data (15M + 1H candles, indicators)
Step 2    → Derivative Analysis (funding rate, open interest)
Step 2.4  → Liquidation Cluster Analysis (Hyperliquid L2 book)
Step 2.5  → Whale Flow Filter (Hyperliquid large trades)
Step 2.6  → Time & Market Mode (6 session modes)
Step 2.7  → Entry Quality Filter (5 hard checks)
Step 2.8  → Hard Veto Rules (divergence, cooldown, flat EMA)
Step 3    → AI Decision-Making (6-dimension scoring, 0-100)
Step 4    → Asset Selection (pick best, progressive conviction)
Step 5    → Order Execution (Agent Trade Kit only, isolated margin)
Step 6    → Trailing Stop + Hard SL (ATR-adjusted)
Step 7    → Dual-Loop: Entry + Monitoring (reversal detection, TP exit)
```

## Scoring Breakdown

| Dimension | Range | What It Measures |
|---|---|---|
| Trend Strength | 0-25 | EMA20/EMA50 alignment on 15M+1H, slope |
| Momentum | 0-10 | RSI(14) zone, divergence |
| Volatility | 0-10 | ATR(14) regime, candle body size |
| Derivatives | 0-20 | Funding alignment, OI trend, liquidation clusters, whale flow |
| Time Regime | 0-15 | Market mode + setup quality |
| Smart Money | 0-20 | BOS/CHoCH, Order Blocks, FVG, Premium/Discount |
| **Total** | **0-100** | |

## Risk Parameters

| Parameter | Value |
|---|---|
| Max margin per position | 10 USDT (isolated) |
| Max loss per trade | 3% of account |
| Daily kill switch | -10% |
| Max entries per day | 4 (+ dynamic post-limit with score >= 85) |
| Max positions per asset | 1 |
| Hedging | Disabled |
| Averaging down | Disabled |
| Max hold time | 4 hours (auto-close if +PnL) |
| Funding > 0.1% | Halve position size |

## License

MIT
