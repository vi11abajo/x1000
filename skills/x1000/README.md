# x1000 — AI Multi-Asset Perpetual Strategy

> Built for the OKX Agent Trade Kit Competition

## Overview

x1000 is an AI-driven perpetual futures trading strategy that selects the single best trade among **BTC-USDT-SWAP**, **ETH-USDT-SWAP**, **SOL-USDT-SWAP**, and **HYPE-USDT-SWAP** every cycle.

### Key Features

- **Deep AI Decision Layer** — Not if/else rules. The AI synthesizes 5 signal dimensions (trend, momentum, volatility, derivatives, time regime) into a composite 0-100 score
- **Time-Aware Market Mode** — Detects US High Vol, News/Macro, Low Liquidity, and Normal modes, adjusting position sizing and thresholds accordingly
- **Multi-Signal Fusion** — EMA structure + RSI momentum + ATR regime + funding rate + open interest flow
- **Adaptive Logic** — Dynamically adjusts position size, skips cycles, and modifies risk based on real-time market conditions
- **Mandatory Stop-Loss** — Every position has an immediate SL order; no exceptions
- **Strict Risk Management** — 3% max risk per trade, 8% daily kill switch, max 2 concurrent positions

### Competition Criteria

| Requirement | Status |
|---|---|
| AI decision-making step | Step 3 (explicit scoring breakdown) |
| Orders via Agent Trade Kit | All orders use `tag=agentTradeKit` |
| Clear stop-loss rules | Step 6 (mandatory, ATR-adjusted) |
| Risk management | Defined limits, kill switch, position caps |
| Deep AI analysis | 5-dimension scoring, market mode detection |
| Multi-signal fusion | EMA + RSI + ATR + Funding + OI + Time |
| Adaptive logic | 4 market modes, dynamic sizing |

## Installation

```bash
npm install -g @okx_ai/okx-trade-cli
okx config init
```

## Usage

```bash
# Run the strategy (every 4 hours)
okx --profile live swap place --instId BTC-USDT-SWAP --side buy --ordType market --sz 1 --tdMode cross --posSide long --tag agentTradeKit
```

## Strategy Structure

```
Step 1  → Collect Market Data (candles, EMA, RSI, ATR)
Step 2  → Derivative Analysis (funding rate, open interest)
Step 2.5 → Time & Market Mode (US High Vol / News / Low Liq / Normal)
Step 3  → AI Decision-Making (5-dimension scoring, 0-100)
Step 4  → Asset Selection (pick best, skip if all < 50)
Step 5  → Order Execution (Agent Trade Kit only)
Step 6  → Mandatory Stop-Loss (ATR-adjusted)
```

## Scoring Breakdown

| Dimension | Range | What It Measures |
|---|---|---|
| Trend Strength | 0-30 | EMA alignment, structure cleanliness |
| Momentum | 0-20 | RSI zone, movement sustainability |
| Volatility | 0-15 | ATR regime (normal/low/high) |
| Derivatives | 0-20 | Funding alignment, OI confirmation |
| Time Regime | 0-15 | Market mode + setup quality |
| **Total** | **0-100** | |

## Risk Parameters

| Parameter | Value |
|---|---|
| Max loss per trade | 3% of account |
| Daily kill switch | -8% |
| Max concurrent positions | 2 |
| Hedging | Disabled |
| Averaging down | Disabled |
| Funding > 0.1% | Halve position |

## License

MIT
