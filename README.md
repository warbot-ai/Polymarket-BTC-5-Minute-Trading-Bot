# 🤖 Polymarket BTC 5-Minute Trading Bot

[![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/downloads/)
[![NautilusTrader](https://img.shields.io/badge/nautilus-1.222.0-green.svg)](https://nautilustrader.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Polymarket](https://img.shields.io/badge/Polymarket-CLOB-purple)](https://polymarket.com)
[![Redis](https://img.shields.io/badge/Redis-powered-red.svg)](https://redis.io/)
[![Grafana](https://img.shields.io/badge/Grafana-dashboard-orange)](https://grafana.com/)

A production-grade algorithmic trading bot for **Polymarket's 5-minute BTC price prediction markets**. Built with a 7-phase architecture combining multiple signal sources, professional risk management, and self-learning capabilities.

> **Migrated from 15-minute to 5-minute markets.** See the [Migration Notes](#migration-from-15-minute-bot) section for a full breakdown of every change and the reasoning behind each one.

---

## 📋 Table of Contents
- [What Changed — 15m → 5m](#migration-from-15-minute-bot)
- [Features](#features)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Running the Bot](#running-the-bot)
- [Signal Strategy](#signal-strategy)
- [Monitoring](#monitoring)
- [Project Structure](#project-structure)
- [Testing](#testing)
- [FAQ](#faq)
- [Disclaimer](#disclaimer)

---

## 🔄 Migration from 15-Minute Bot

This bot is a full rewrite of the original 15-minute version, adapted for the faster cadence and different characteristics of 5-minute markets. Every change is documented here with the reasoning.

### Core Timing Changes

| Parameter | 15-min Bot | 5-min Bot | Why |
|-----------|-----------|-----------|-----|
| `MARKET_INTERVAL_SECONDS` | 900 | 300 | The fundamental market duration |
| Slug pattern | `btc-updown-15m-*` | `btc-updown-5m-*` | Different Polymarket market family |
| Slug count loaded | 97 (24h) | 289 (24h) | 12 markets/hour × 24h + 1 prior |
| Filter `limit` | 100 | 300 | Must accommodate 289 slugs |
| Auto-restart | 90 min | 60 min | 5m filter lists go stale faster |
| Timer polling | 10s | 5s | Finer granularity for shorter markets |

### Trade Window

| | 15-min Bot | 5-min Bot | Reasoning |
|--|-----------|-----------|-----------|
| Window start | 780s (13:00) | 210s (3:30) | 70% through the market |
| Window end | 840s (14:00) | 255s (4:15) | 85% through the market |
| Window width | 60s | 45s | Proportionally equivalent |

**Why 70–85% into the market?**  
The same logic applies to both timeframes — we want to trade when the market has *already decided* which direction BTC went, not predict it. At minute 13 of 15, the outcome is nearly settled. The equivalent in a 5-min market is 3:30 in. Trading earlier means the price is still near 0.50 (coin-flip). Trading after 4:30 risks liquidity drying up near settlement.

### Trend Filter Thresholds

| | 15-min Bot | 5-min Bot | Reasoning |
|--|-----------|-----------|-----------|
| UP threshold | `> 0.60` | `> 0.62` | |
| DOWN threshold | `< 0.40` | `< 0.38` | |

**Why tighter for 5m?**  
A probability of 0.61 at minute 13 of a 15-minute market is well-settled — BTC has had 13 minutes to move. The same reading at 3:30 of a 5-minute market is much noisier because the market has only had 3.5 minutes to price the move. Raising the threshold to 0.62/0.38 means we only trade when the market has strong conviction, reducing false signals in exchange for slightly fewer trades.

### Signal Processor Changes

#### Spike Detector
| Parameter | 15-min | 5-min | Reasoning |
|-----------|--------|-------|-----------|
| `spike_threshold` | 0.05 | 0.04 | 5m prices move faster; 4% deviation is already extreme |
| `lookback_periods` | 20 | 15 | Shorter price history for a shorter market |
| `velocity_threshold` | 0.03 | 0.04 | Needs a bigger burst to distinguish signal from noise |

#### Tick Velocity Processor (most significant change)
| Parameter | 15-min | 5-min | Reasoning |
|-----------|--------|-------|-----------|
| Primary window | 30s | 15s | *New* — ultra-short momentum just before trade |
| Secondary window | 60s | 30s | Was primary for 15m |
| `velocity_threshold_30s` | 0.010 | 0.008 | 5m prices move faster; 0.8% in 30s is significant |
| `velocity_threshold_15s` | N/A | 0.005 | New threshold for the new 15s window |
| Tick tolerance | ±15s | ±10s | Tighter matching for the shorter windows |

**Why shorter windows for 5m?**  
At the 3:30 trade window in a 5-min market, "60 seconds ago" was the 2:30 mark — still in the noisy opening phase where nobody knows which way BTC will move. "30 seconds ago" is the 3:00 mark — price is settling. "15 seconds ago" is the 3:15 mark — almost exactly where we're trading. The 15s/30s windows measure *the settling phase*, which is far more predictive than the *opening noise phase*.

#### Order Book Imbalance
| Parameter | 15-min | 5-min | Reasoning |
|-----------|--------|-------|-----------|
| `min_book_volume` | $50 | $100 | 5m markets open thinner; filter noise from illiquid early books |

#### Divergence Processor
| Parameter | 15-min | 5-min | Reasoning |
|-----------|--------|-------|-----------|
| `momentum_threshold` | 0.003 | 0.002 | BTC moves faster relative to 5m intervals |
| `extreme_prob_threshold` | 0.68 | 0.72 | 5m markets reach 68%+ more often on BTC moves — only fade very extreme readings |
| `low_prob_threshold` | 0.32 | 0.28 | Symmetric |

#### Sentiment (Fear & Greed Index)
| Parameter | 15-min | 5-min | Reasoning |
|-----------|--------|-------|-----------|
| `extreme_fear_threshold` | 25 | 20 | Only very extreme readings matter |
| `extreme_greed_threshold` | 75 | 80 | Only very extreme readings matter |

#### Deribit PCR
| Parameter | 15-min | 5-min | Reasoning |
|-----------|--------|-------|-----------|
| `bullish_pcr_threshold` | 1.20 | 1.30 | Only very extreme PCR readings matter for 5m |
| `bearish_pcr_threshold` | 0.70 | 0.60 | Symmetric |
| `max_days_to_expiry` | 2 | 1 | Only intraday options are remotely relevant |
| `cache_seconds` | 300 | 600 | Refresh less often — data is barely useful anyway |

### Signal Fusion Weights (the biggest strategic rethink)

| Signal | 15-min Weight | 5-min Weight | Reasoning |
|--------|--------------|--------------|-----------|
| `OrderBookImbalance` | 0.30 | **0.38** | Best real-time signal; large players show hand before settlement |
| `TickVelocity` | 0.25 | **0.32** | Probability momentum is directly on-market and fast |
| `PriceDivergence` | 0.18 | 0.15 | Spot BTC momentum still valid but slightly less so |
| `SpikeDetection` | 0.12 | 0.10 | Mean reversion still fires occasionally |
| `SentimentAnalysis` | 0.05 | **0.03** | **Daily metric — nearly useless for 5m** |
| `DeribitPCR` | 0.10 | **0.02** | **Options data is hours old — nearly useless for 5m** |

**The core philosophy shift:**  
For 15-minute markets, Deribit PCR (institutional sentiment) carried 10% weight because institutional options positions can influence BTC direction over 15 minutes. For 5-minute markets, those positions are irrelevant — they don't react to 5-minute BTC moves. Similarly, the Fear & Greed index is a *daily* reading that tells us nothing about the next 300 seconds. The 5-minute bot is dominated by real-time, on-market signals: order book depth and tick velocity.

### Other Changes
- `price_history` max length: 100 → 60 (shorter horizon needs less history)
- `price_history` minimum for trading: 20 → 15 (faster startup)
- SMA lookback in `_fetch_market_context`: 20 periods → 15
- Synthetic history random walk: ±3% → ±2% (smaller moves for 5m)
- Paper trade output file: `paper_trades.json` → `paper_trades_5m.json`
- Paper trade simulated movement: ±8%/2% → ±6%/1.5% (tighter for 5m)
- Runner script: `15m_bot_runner.py` → `5m_bot_runner.py`
- `trader_id`: `BTC-15MIN-INTEGRATED-001` → `BTC-5MIN-INTEGRATED-001`

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| **7-Phase Architecture** | Modular, testable, production-ready design |
| **Real-Time Signals** | Order Book Imbalance + Tick Velocity dominate (38%/32%) |
| **Risk-First Design** | $1 max per trade, liquidity guard, exposure limits |
| **Dual-Mode Operation** | Toggle simulation ↔ live via Redis without restart |
| **Real-Time Monitoring** | Grafana dashboards + Prometheus metrics |
| **Self-Learning** | Signal weight optimization based on performance |
| **Auto-Recovery** | Market switching, FAK retry, WebSocket reconnection |
| **Paper Trading** | Full P&L tracking in simulation mode |

---

## 🏗️ Architecture

### 7-Phase Overview

```
External Data → Ingestion → Nautilus Core → Signal Processors → Fusion Engine → Risk Engine → Execution → Monitoring → Learning
                                                                                                               ↑
                                                                                              (feedback loop)
```

### Signal Processor Weights (5-min)

```
OrderBookImbalance  ████████████████████████████████████████ 38%  ← real-time CLOB depth
TickVelocity        ████████████████████████████████         32%  ← probability momentum
PriceDivergence     ███████████████                          15%  ← spot BTC momentum
SpikeDetection      ██████████                               10%  ← mean reversion
SentimentAnalysis   ███                                       3%  ← daily F&G (low weight)
DeribitPCR          ██                                        2%  ← options data (low weight)
```

---

## Prerequisites

- Python 3.14+
- Redis (for live/sim mode switching)
- Polymarket account with API credentials
- Git

---

## 🚀 Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/aulekator/Polymarket-BTC-15-Minute-Trading-Bot.git
cd Polymarket-BTC-15-Minute-Trading-Bot
```

### 2. Set Up Virtual Environment

```bash
# macOS / Linux
python -m venv venv
source venv/bin/activate

# Windows
python -m venv venv
venv\Scripts\activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Environment Variables

```bash
cp .env.example .env
```

Edit `.env`:

```env
# Polymarket API Credentials
POLYMARKET_PK=your_private_key_here
POLYMARKET_API_KEY=your_api_key_here
POLYMARKET_API_SECRET=your_api_secret_here
POLYMARKET_PASSPHRASE=your_passphrase_here

# Redis Configuration
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=2

# Trading Parameters
MARKET_BUY_USD=1.00
```

### 5. Start Redis

```bash
# macOS
brew install redis && redis-server

# Linux
sudo apt install redis-server && redis-server

# Windows — download from redis.io
redis-server
```

### 6. Run the Bot

```bash
# Simulation mode (default — no real money)
python bot.py

# Test mode (faster clock for debugging)
python bot.py --test-mode

# Live trading (REAL MONEY — read the disclaimer first!)
python 5m_bot_runner.py --live
```

---

## ⚙️ Configuration

| Argument | Description | Default |
|----------|-------------|---------|
| `--live` | Enable live trading (real money) | False (simulation) |
| `--test-mode` | Trade every minute for testing | False |
| `--no-grafana` | Disable Grafana metrics | False |

### Key Constants (in `bot.py`)

```python
MARKET_INTERVAL_SECONDS = 300    # 5-minute markets
TRADE_WINDOW_START      = 210    # 3m30s into market (70%)
TRADE_WINDOW_END        = 255    # 4m15s into market (85%)
TREND_UP_THRESHOLD      = 0.62   # buy YES if price > this
TREND_DOWN_THRESHOLD    = 0.38   # buy NO if price < this
RESTART_AFTER_MINUTES   = 60     # auto-restart to refresh market filters
```

---

## 🚦 Running the Bot

### Simulation Mode (Recommended First)

```bash
python bot.py
```

Monitor paper trades:
```bash
cat paper_trades_5m.json
```

### Live Mode

```bash
python 5m_bot_runner.py --live
```

The runner script wraps `bot.py` with automatic restart on exit (normal or error).

### Switch Mode Without Restarting (Redis)

```bash
python redis_control.py sim    # switch to simulation
python redis_control.py live   # switch to live trading
```

---

## 📊 Signal Strategy

### How the Bot Decides to Trade

1. **Load markets** — at startup, loads all `btc-updown-5m-*` slugs for the next 24 hours
2. **Subscribe** — subscribes to quote ticks for the currently active market
3. **Wait for window** — buffers ticks until 3:30 into the market (210 seconds)
4. **Trend filter** — if price `> 0.62` → buy YES; if `< 0.38` → buy NO; else skip
5. **Signal confirmation** — order book, tick velocity, spot momentum all vote
6. **Fusion** — weighted vote produces a fused signal with score and confidence
7. **Execute** — $1 market buy of YES or NO token via Polymarket CLOB

### Why Late in the Market?

At 3:30 into a 5-minute BTC market, the probability price *is* the market's verdict on which way BTC went. If the YES price is at $0.72, it means 72% of the money bet thinks BTC went up during this interval — and it probably did. We don't predict; we read a nearly-resolved outcome.

Trading at 0:30 in means the price is still near $0.50 because nobody knows yet. That's where most losses come from.

### Why Tighter Thresholds Than the 15m Bot?

| Price | 15m bot | 5m bot |
|-------|---------|--------|
| 0.61 at 13:00 of 15 | Trades | — |
| 0.61 at 3:30 of 5 | — | Skips |
| 0.63 at 3:30 of 5 | — | Trades |

A 0.61 reading at minute 13 of a 15-minute market has had 13 minutes of price discovery. The same reading at 3:30 of a 5-minute market has had 3.5 minutes — it's noisier and less reliable. The tighter threshold compensates.

---

## 📁 Project Structure

```
polymarket-btc-5m-bot/
├── bot.py                           # Main bot — all strategy logic
├── 5m_bot_runner.py                 # Auto-restart wrapper
├── 15m_bot_runner.py                # Legacy (kept for reference)
├── redis_control.py                 # Toggle sim/live mode
│
├── core/
│   ├── ingestion/                   # Data validation & ingestion
│   ├── nautilus_core/               # NautilusTrader integration
│   │   ├── instruments/btc_instruments.py
│   │   ├── data_engine/engine_wrapper.py
│   │   ├── event_dispatcher/dispatcher.py
│   │   └── providers/custom_data_provider.py
│   └── strategy_brain/
│       ├── fusion_engine/
│       │   └── signal_fusion.py     # Weighted voting fusion engine
│       ├── signal_processors/
│       │   ├── spike_detector.py    # MA deviation + velocity spikes
│       │   ├── tick_velocity_processor.py  # 15s/30s probability momentum ← updated
│       │   ├── orderbook_processor.py      # CLOB bid/ask imbalance
│       │   ├── divergence_processor.py     # Spot BTC momentum vs poly prob
│       │   ├── sentiment_processor.py      # Fear & Greed (low weight for 5m)
│       │   ├── deribit_pcr_processor.py    # Options PCR (low weight for 5m)
│       │   └── base_processor.py
│       └── strategies/
│           └── btc_15min_strategy.py  # Now implements BTCStrategy5Min
│
├── data_sources/
│   ├── binance/websocket.py
│   ├── coinbase/adapter.py
│   └── news_social/adapter.py
│
├── execution/
│   ├── polymarket_client.py
│   ├── execution_engine.py
│   └── risk_engine.py
│
├── monitoring/
│   ├── grafana_exporter.py
│   └── performance_tracker.py
│
├── feedback/
│   └── learning_engine.py
│
├── grafana/
│   ├── dashboard.json
│   └── import_dashboard.py
│
├── patch_gamma_markets.py
├── patch_market_orders.py
├── paper_trades_5m.json             # Simulation trade log (5m)
└── README.md
```

---

## 🧪 Testing

```bash
# Test individual phases
python core/nautilus_core/test_nautilus.py
python core/strategy_brain/test_strategy.py
python execution/test_execution.py

# Quick smoke test (simulation mode, fast clock)
python bot.py --test-mode
```

---

## ❓ FAQ

**Q: How much money do I need to start?**  
A: The bot caps each trade at $1, so you can start with as little as $10–20.

**Q: How often does it trade?**  
A: One trade per 5-minute market when the trend filter passes. That's up to 12 potential trades/hour, but roughly 25–35% of markets are skipped (price near 0.50), so expect 8–9 actual trades/hour maximum.

**Q: Why did you reduce the Deribit PCR weight so much (0.10 → 0.02)?**  
A: Deribit options data represents multi-hour to multi-day institutional positioning. It simply does not contain information about the next 5 minutes. Giving it 10% weight in the 15m bot was already borderline; for 5m it's essentially noise. Keeping a tiny 2% weight means it can still contribute a tiny nudge in genuinely extreme market conditions (PCR > 1.30 or < 0.60).

**Q: Why is the trade window 3:30–4:15 and not earlier?**  
A: See [Signal Strategy](#signal-strategy). The short version: trading at 0:30–2:00 means the price is still near 0.50 because BTC hasn't had time to move enough for the market to price it. The loss rate in that window is close to a coin flip.

**Q: Can I adjust the trade window?**  
A: Yes — edit `TRADE_WINDOW_START` and `TRADE_WINDOW_END` in `bot.py`. If you want to experiment with earlier trading (more trades, lower win rate), try 180–240. Later (higher win rate, fewer trades, liquidity risk): 240–270.

**Q: Can I run this 24/7?**  
A: Yes. The bot auto-restarts every 60 minutes to refresh market filters. The `5m_bot_runner.py` wrapper handles crash recovery.

---

## Disclaimer

**TRADING CRYPTOCURRENCIES AND PREDICTION MARKETS CARRIES SIGNIFICANT RISK.**

- This software is for educational purposes only
- Past performance does not guarantee future results
- Always test thoroughly in simulation mode before trading real money
- Start with the smallest possible amounts
- The developers are not responsible for any financial losses
- You are solely responsible for compliance with your local laws and regulations

---

## Contact & Community

- GitHub Issues: For bugs and feature requests
- Twitter: [@Kator07](https://twitter.com/Kator07)
- Discord: [Join our community](https://discord.gg/tafKjBnPEQ)
- Telegram: [![Telegram](https://img.shields.io/badge/Telegram-%230088cc.svg?style=flat&logo=telegram&logoColor=white)](https://t.me/Bigg_O7)

## ⭐ Show Your Support

If you find this project useful, please star the repo — it helps others discover it!

---

## Acknowledgments

- [NautilusTrader](https://nautilustrader.io/) — Professional trading framework
- [Polymarket](https://polymarket.com) — Prediction market platform
- All contributors and users of this project
