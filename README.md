# JARVIS — AI Trading Assistant on Discord

Personal trading copilot that lives in Discord. JARVIS watches crypto and stock markets 24/7, runs three independent strategy engines through a confluence scoring system, paper-trades an intraday engine, analyzes everything with a **local LLM (Ollama)** — and reports, logs and takes commands through Discord channels.

**Paper trading by default.** The live path (Bybit) sits behind a `PAPER_TRADING` flag and a production-grade risk manager.

## Architecture

```
main.py                 Discord orchestrator: channels (talk / logs / audit / paper),
                        command handling, scheduled loops
engine.py               top-level coordination
ollama_client.py        local LLM: market analysis + Pine Script generation
database.py             SQLite: reports, trade history

bots/
  crypto_bot / stocks_bot / news_bot / market_data_bot
                        multi-source data: Binance, Bybit, CoinGecko, CoinGlass,
                        Twelve Data, news feeds
  tradingview_bridge    real chart data from TradingView
  pc_monitor            host health (CPU/RAM/disk/internet) with Discord alerts
  indicadores / tendencias
                        technical indicators + trend aggregation

bots/trading_bot/
  engine.py             confluence engine: global scan loop, confirmation loop,
                        market-context refresh, scoring, position management
  zone_flip.py          structural S/R zones: real swing highs/lows, touch clusters,
                        S→R / R→S flips — no synthetic bands
  wave_hunt.py          Elliott-style wave tops via TradingView data (weeks/months horizon)
  smart_flow.py         order-flow: CVD, funding rate, OI, long/short ratio, taker ratio
  fast_paper.py         fast intraday engine — paper/shadow only, never sends real orders
  confirmacion.py       entry confirmation layer
  risk_manager.py       sizing v3: per-symbol qty precision, exposure/margin separation,
                        real monetary risk, defensive validations
  auditor.py            trade auditing
  bybit_client.py       exchange client (keys via .env only)
```

Every strategy returns a normalized packet (`score`, `direccion`, `leverage`, `razones`, indicators used/discarded) so the confluence engine can combine heterogeneous strategies under one scoring threshold before anything executes.

## The AI layer

Analysis runs through a local Ollama model — no API costs, no data leaving the machine. JARVIS uses it to explain market conditions, audit closed trades (post-trade analysis loop), and even **generate Pine Script indicators on demand** from Discord.

## Run

```bash
pip install -r requirements.txt
cp .env.example .env    # Discord token, channel ids, data keys; PAPER_TRADING=true
python main.py
```

## Stack

Python · asyncio · discord.py · Ollama (local LLM) · Bybit · Binance/CoinGecko/CoinGlass/Twelve Data · TradingView bridge · SQLite · numpy

> Related: [pumpfun-sniper](https://github.com/diegodiaz808/pumpfun-sniper) — an earlier, narrower bot focused on sniping pump.fun launches on Solana.
