"""Motor intradía rápido en paper/shadow. Nunca envía órdenes reales."""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

from bots import data_provider as dp

log = logging.getLogger("fast_paper")

STATE_PATH = Path("fast_paper_state.json")
EVENT_LOG_PATH = Path("fast_paper_events.jsonl")
OFFICIAL_TRADES_PATH = Path("trades.json")
ASSETS = ("BTC", "ETH")
STARTING_EQUITY = 500.0
RISK_PCT = 0.005
DAILY_LOSS_PCT = 0.02
# v1.1: entrada como orden límite post-only (maker) → fee 0.02%.
# La salida sigue siendo market/stop (taker 0.055%). Con targets de ~0.5%,
# las fees round-trip taker/taker se comían el 21% del target.
FEE_RATE = 0.00055          # taker — se usa en salidas
FEE_MAKER = 0.0002          # maker — se usa en entradas (limit post-only)
SLIPPAGE_RATE = 0.0002
# v1.1: RR 1.6 → 2.0. Con RR neto realizado 0.80 el sistema necesitaba 55% WR.
RR_TARGET = 2.0
# v1.1: breakeven automático — 13/32 pérdidas habían llegado a +0.25% MFE.
BREAKEVEN_TRIGGER_PCT = 0.25   # % a favor para mover stop a breakeven
BREAKEVEN_FEE_BUFFER = 0.0012  # el BE cubre fees + slippage del round-trip
MAX_OPEN = 5
COOLDOWN_MINUTES = 15
PAPER_LEVERAGE = 5
MARGIN_TOTAL_MAX_PCT = 0.70
STRATEGY_TAG = "fast-paper-v1.1"   # para segmentar resultados pre/post cambios
_stop_hook_registered = False


def _event(event_type: str, **payload) -> None:
    """Ledger append-only: cada línea es un evento independiente e inmutable."""
    record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event_type,
              "engine": "fast-paper-v1", **payload}
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd = os.open(EVENT_LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _quantize(value: float, step: float, rounding=ROUND_DOWN) -> float:
    if not step:
        return value
    d_value, d_step = Decimal(str(value)), Decimal(str(step))
    return float((d_value / d_step).to_integral_value(rounding=rounding) * d_step)


async def _instrument_rules(asset: str) -> dict:
    data = await dp._get(f"{dp.BYBIT_BASE}/v5/market/instruments-info",
                         {"category": "linear", "symbol": f"{asset}USDT"})
    try:
        row = data["result"]["list"][0]
        lot, price, leverage = row["lotSizeFilter"], row["priceFilter"], row["leverageFilter"]
        return {
            "status": row["status"], "contract_type": row["contractType"],
            "qty_step": float(lot["qtyStep"]), "min_qty": float(lot["minOrderQty"]),
            "min_notional": float(lot.get("minNotionalValue") or 0),
            "max_market_qty": float(lot.get("maxMktOrderQty") or lot.get("maxMarketOrderQty") or 0),
            "tick_size": float(price["tickSize"]),
            "min_leverage": float(leverage["minLeverage"]),
            "max_leverage": float(leverage["maxLeverage"]),
            "leverage_step": float(leverage["leverageStep"]),
            "funding_interval_minutes": int(row.get("fundingInterval") or 480),
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        _event("INSTRUMENT_RULES_ERROR", asset=asset, error=str(exc))
        return {}


async def _funding_cost(pos: dict, closed_at: str) -> tuple[float, list[dict]]:
    start_ms = int(datetime.fromisoformat(pos["opened_at"]).timestamp() * 1000)
    end_ms = int(datetime.fromisoformat(closed_at).timestamp() * 1000)
    data = await dp._get(f"{dp.BYBIT_BASE}/v5/market/funding/history", {
        "category": "linear", "symbol": f"{pos['asset']}USDT",
        "startTime": start_ms, "endTime": end_ms, "limit": 200,
    })
    rows = []
    try:
        for row in data["result"]["list"]:
            ts = int(row["fundingRateTimestamp"])
            if start_ms < ts <= end_ms:
                rows.append({"timestamp": ts, "rate": float(row["fundingRate"])})
    except (KeyError, TypeError, ValueError):
        return 0.0, []
    notional = pos["entry"] * pos["qty"]
    signed = -1 if pos["direction"] == "LONG" else 1
    return signed * notional * sum(row["rate"] for row in rows), rows


async def _market_snapshot(asset: str) -> dict:
    """Foto de mercado multi-fuente para auditar si el fill paper era ejecutable."""
    symbol = f"{asset}USDT"
    bybit, binance, twelve = await asyncio.gather(
        dp._get(f"{dp.BYBIT_BASE}/v5/market/tickers", {"category": "linear", "symbol": symbol}),
        dp._get(f"{dp.BINANCE_SPOT}/api/v3/ticker/bookTicker", {"symbol": symbol}),
        dp._twelve_ohlcv(asset, "1m", 1),
    )
    snap = {"captured_at": datetime.now(timezone.utc).isoformat()}
    try:
        row = bybit["result"]["list"][0]
        snap.update({
            "bybit_last": float(row["lastPrice"]), "bybit_bid": float(row["bid1Price"]),
            "bybit_ask": float(row["ask1Price"]), "mark": float(row["markPrice"]),
            "index": float(row["indexPrice"]), "funding": float(row.get("fundingRate") or 0),
        })
        mid = (snap["bybit_bid"] + snap["bybit_ask"]) / 2
        snap["spread_bps"] = (snap["bybit_ask"] - snap["bybit_bid"]) / mid * 10_000
    except (KeyError, IndexError, TypeError, ValueError):
        snap["bybit_error"] = True
    try:
        snap.update({"binance_bid": float(binance["bidPrice"]), "binance_ask": float(binance["askPrice"])})
    except (KeyError, TypeError, ValueError):
        snap["binance_error"] = True
    if twelve:
        snap["twelve_close"] = float(twelve[-1]["close"])
    reference = snap.get("bybit_last")
    if reference:
        if snap.get("twelve_close"):
            snap["twelve_deviation_bps"] = (snap["twelve_close"] - reference) / reference * 10_000
        if snap.get("binance_bid") and snap.get("binance_ask"):
            binance_mid = (snap["binance_bid"] + snap["binance_ask"]) / 2
            snap["binance_deviation_bps"] = (binance_mid - reference) / reference * 10_000
    snap["sources_ok"] = sum(not snap.get(f"{name}_error") for name in ("bybit", "binance")) + int(bool(twelve))
    return snap


def _default_state() -> dict:
    return {
        "version": "fast-paper-v1",
        "equity": STARTING_EQUITY,
        "peak_equity": STARTING_EQUITY,
        "open": {},
        "closed": [],
        "last_entry_at": {},
        "last_evaluated_candle": {},
        "last_trade_id": 0,
        "experiment_started_at": datetime.now(timezone.utc).isoformat(),
        "last_cycle_at": None,
        "cycles_executed": 0,
        "cycles_missed": 0,
        "online_seconds": 0.0,
        "max_drawdown_pct": 0.0,
        "last_weekly_report_at": None,
    }


def _load() -> dict:
    if not STATE_PATH.exists():
        return _default_state()
    try:
        data = json.loads(STATE_PATH.read_text())
        base = _default_state()
        base.update(data)
        migrated = False
        for trade in [*base["closed"], *base["open"].values()]:
            if not trade.get("trade_id"):
                base["last_trade_id"] += 1
                trade["trade_id"] = f"#FP{base['last_trade_id']:04d}"
                migrated = True
            if "audit_level" not in trade:
                trade["audit_level"] = "full" if trade.get("instrument_rules") else "legacy_pre_audit"
                migrated = True
        if migrated:
            _save(base)
        return base
    except Exception as exc:
        log.error("No se pudo cargar fast paper: %s", exc)
        return _default_state()


def _save(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(STATE_PATH)


def _next_trade_id(state: dict) -> str:
    state["last_trade_id"] = int(state.get("last_trade_id", 0)) + 1
    return f"#FP{state['last_trade_id']:04d}"


def mark_engine_start() -> None:
    global _stop_hook_registered
    state = _load()
    state["session_started_at"] = datetime.now(timezone.utc).isoformat()
    _save(state)
    _event("ENGINE_STARTED", open_trades=list(state["open"]), equity=state["equity"])
    if not _stop_hook_registered:
        atexit.register(_mark_engine_stop)
        _stop_hook_registered = True


def _mark_engine_stop() -> None:
    try:
        state = _load()
        _event("ENGINE_STOPPED", cycles_executed=state.get("cycles_executed", 0),
               equity=state.get("equity"), open_trades=list(state.get("open", {})))
    except Exception:
        pass


def record_engine_error(error: str) -> None:
    _event("ENGINE_ERROR", error=error)


def _heartbeat(state: dict) -> None:
    now = datetime.now(timezone.utc)
    last = state.get("last_cycle_at")
    if last:
        delta = max(0, (now - datetime.fromisoformat(last)).total_seconds())
        state["online_seconds"] = float(state.get("online_seconds", 0)) + min(delta, 75)
        state["cycles_missed"] = int(state.get("cycles_missed", 0)) + max(0, int(delta // 60) - 1)
    state["last_cycle_at"] = now.isoformat()
    state["cycles_executed"] = int(state.get("cycles_executed", 0)) + 1


def _ema(values: list[float], period: int) -> float:
    seed = sum(values[:period]) / period
    k = 2 / (period + 1)
    out = seed
    for value in values[period:]:
        out = value * k + out * (1 - k)
    return out


def _rsi(values: list[float], period: int = 14) -> float:
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(delta, 0) for delta in deltas[-period:]]
    losses = [abs(min(delta, 0)) for delta in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    return 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)


def _atr(bars: list[dict], period: int = 14) -> float:
    ranges = []
    for i in range(1, len(bars)):
        high, low, previous = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        ranges.append(max(high - low, abs(high - previous), abs(low - previous)))
    return sum(ranges[-period:]) / period


def _today_pnl(state: dict) -> float:
    today = datetime.now(timezone.utc).date().isoformat()
    return sum(t.get("pnl_net", 0) for t in state["closed"] if t.get("closed_at", "")[:10] == today)


def _signal(asset: str, bars_5m: list[dict], bars_15m: list[dict], bars_1h: list[dict]) -> dict | None:
    # Sólo velas cerradas: la última vela de Binance/Bybit puede seguir formándose.
    b5, b15, b1h = bars_5m[:-1], bars_15m[:-1], bars_1h[:-1]
    if min(len(b5), len(b15), len(b1h)) < 55:
        return None

    c5 = [b["close"] for b in b5]
    c15 = [b["close"] for b in b15]
    c1h = [b["close"] for b in b1h]
    price = c5[-1]
    ema20_1h, ema50_1h = _ema(c1h, 20), _ema(c1h, 50)
    ema9_15, ema21_15 = _ema(c15, 9), _ema(c15, 21)
    ema9_5, ema21_5 = _ema(c5, 9), _ema(c5, 21)
    rsi5 = _rsi(c5)
    avg_volume = sum(b["volume"] for b in b5[-21:-1]) / 20
    volume_ratio = b5[-1]["volume"] / avg_volume if avg_volume else 0

    # v1.1: la alineación con la tendencia 1h dejó de puntuar. En 52 trades
    # medidos, ir "a favor" del 1h rindió 29% WR y contra/neutro 50% WR:
    # en 5m, cuando el 1h ya confirmó, el movimiento está extendido y
    # revierte. Se registra como dato informativo, no como filtro.
    long_core = [
        ema9_15 > ema21_15,
        price > ema9_5 > ema21_5,
        52 <= rsi5 <= 72,
        volume_ratio >= 0.80,
    ]
    short_core = [
        ema9_15 < ema21_15,
        price < ema9_5 < ema21_5,
        28 <= rsi5 <= 48,
        volume_ratio >= 0.80,
    ]
    direction, checks = ("LONG", long_core) if sum(long_core) >= sum(short_core) else ("SHORT", short_core)
    score = sum(checks)
    trend_alineada = (ema20_1h > ema50_1h) if direction == "LONG" else (ema20_1h < ema50_1h)
    atr = _atr(b5)
    stop_distance = max(atr * 1.3, price * 0.0035)
    stop = price - stop_distance if direction == "LONG" else price + stop_distance
    target = price + stop_distance * RR_TARGET if direction == "LONG" else price - stop_distance * RR_TARGET
    return {
        "asset": asset, "direction": direction, "signal_price": price,
        "stop": stop, "target": target, "atr": atr, "score": score * 25,
        "eligible": score >= 4,   # los 4 checks core deben confirmar
        "checks": {
            "trend_1h": trend_alineada, "momentum_15m": checks[0],
            "structure_5m": checks[1], "rsi_5m": checks[2], "volume": checks[3],
        },
        "rsi5": rsi5, "volume_ratio": volume_ratio, "candle_time": b5[-1]["time"],
        "trend_1h": "alcista" if ema20_1h > ema50_1h else "bajista",
        "indicators": {"ema9_5m": ema9_5, "ema21_5m": ema21_5,
                       "ema9_15m": ema9_15, "ema21_15m": ema21_15,
                       "ema20_1h": ema20_1h, "ema50_1h": ema50_1h},
        "signal_candle": {k: b5[-1][k] for k in ("time", "open", "high", "low", "close", "volume")},
    }


async def _close_position(state: dict, asset: str, raw_exit: float, reason: str) -> str:
    pos = state["open"].pop(asset)
    snapshot = await _market_snapshot(asset)
    executable = snapshot.get("bybit_bid") if pos["direction"] == "LONG" else snapshot.get("bybit_ask")
    base_exit = executable or raw_exit
    adverse = 1 - SLIPPAGE_RATE if pos["direction"] == "LONG" else 1 + SLIPPAGE_RATE
    exit_price = base_exit * adverse
    gross = ((exit_price - pos["entry"]) if pos["direction"] == "LONG" else (pos["entry"] - exit_price)) * pos["qty"]
    exit_fee = exit_price * pos["qty"] * FEE_RATE
    closed_at = datetime.now(timezone.utc).isoformat()
    funding_pnl, funding_events = await _funding_cost(pos, closed_at)
    net = gross - pos["entry_fee"] - exit_fee + funding_pnl
    opened_at = datetime.fromisoformat(pos["opened_at"])
    duration_minutes = (datetime.fromisoformat(closed_at) - opened_at).total_seconds() / 60
    trade = {**pos, "trigger_price": raw_exit, "exit": exit_price, "exit_fee": exit_fee,
             "exit_snapshot": snapshot, "pnl_gross": gross, "pnl_net": net,
             "funding_pnl": funding_pnl, "funding_events": funding_events,
             "reason": reason, "closed_at": closed_at, "duration_minutes": duration_minutes,
             "exit_deviation_bps": (exit_price - raw_exit) / raw_exit * 10_000}
    state["closed"].append(trade)
    state["equity"] += net
    state["peak_equity"] = max(state["peak_equity"], state["equity"])
    current_dd = ((state["peak_equity"] - state["equity"]) / state["peak_equity"] * 100
                  if state["peak_equity"] else 0)
    state["max_drawdown_pct"] = max(float(state.get("max_drawdown_pct", 0)), current_dd)
    state["last_entry_at"][asset] = closed_at
    _save(state)
    _event("TRADE_CLOSED", trade_id=trade.get("trade_id"), asset=asset,
           direction=trade["direction"], reason=reason, trigger_price=raw_exit,
           exit=exit_price, pnl_gross=gross, fees=pos["entry_fee"] + exit_fee,
           funding_pnl=funding_pnl, pnl_net=net, duration_minutes=duration_minutes,
           mfe_pct=pos.get("mfe_pct", 0), mae_pct=pos.get("mae_pct", 0), snapshot=snapshot)
    icon = "✅" if net > 0 else "🛑"
    return (
        f"{icon} **PAPER {pos.get('trade_id', '#FP????')} CERRADO — {asset} {pos['direction']}**\n"
        f"Salida: `${exit_price:,.2f}` · Motivo: `{reason}`\n"
        f"PnL neto: `{net:+.4f} USDT` · Fees totales: `${pos['entry_fee'] + exit_fee:.4f}`\n"
        f"Funding: `{funding_pnl:+.4f} USDT` ({len(funding_events)} liquidaciones)\n"
        f"Duración: `{duration_minutes:.1f} min` · MFE: `{pos.get('mfe_pct', 0):.3f}%` · MAE: `{pos.get('mae_pct', 0):.3f}%`\n"
        f"Spread salida: `{snapshot.get('spread_bps', 0):.2f} bps` · Desvío fill/trigger: `{trade['exit_deviation_bps']:+.2f} bps`\n"
        f"Equity paper: `${state['equity']:.2f}`"
    )


async def run_cycle() -> list[str]:
    """Gestiona posiciones y evalúa una señal por vela cerrada de 5 minutos."""
    state = _load()
    _heartbeat(state)
    _save(state)
    messages: list[str] = []

    # Gestión conservadora con high/low de la última vela de 1 minuto cerrada.
    for asset in list(state["open"]):
        bars_1m = await dp.get_ohlcv(asset, "1m", 3)
        if len(bars_1m) < 2:
            _event("DATA_ERROR", asset=asset, timeframe="1m", reason="insufficient_bars")
            continue
        candle = bars_1m[-2]
        pos = state["open"][asset]
        if pos["direction"] == "LONG":
            favorable = (candle["high"] - pos["entry"]) / pos["entry"] * 100
            adverse_move = (pos["entry"] - candle["low"]) / pos["entry"] * 100
        else:
            favorable = (pos["entry"] - candle["low"]) / pos["entry"] * 100
            adverse_move = (candle["high"] - pos["entry"]) / pos["entry"] * 100
        pos["mfe_pct"] = max(pos.get("mfe_pct", 0), favorable)
        pos["mae_pct"] = max(pos.get("mae_pct", 0), adverse_move)
        pos["last_mark"] = candle["close"]
        pos["last_managed_candle"] = candle["time"]

        # v1.1: breakeven automático. Si el trade llegó a +BREAKEVEN_TRIGGER_PCT
        # a favor, el stop sube a entrada + fees (LONG) o baja a entrada - fees
        # (SHORT). En la muestra v1, 13 de 32 pérdidas habían estado +0.25% a
        # favor antes de morir en el stop original.
        if (not pos.get("breakeven_applied")
                and pos["mfe_pct"] >= BREAKEVEN_TRIGGER_PCT):
            if pos["direction"] == "LONG":
                be = pos["entry"] * (1 + BREAKEVEN_FEE_BUFFER)
                mejora = be > pos["stop"]
            else:
                be = pos["entry"] * (1 - BREAKEVEN_FEE_BUFFER)
                mejora = be < pos["stop"]
            if mejora:
                stop_anterior = pos["stop"]
                pos["stop"] = be
                pos["breakeven_applied"] = True
                _event("STOP_MOVED_BREAKEVEN", trade_id=pos.get("trade_id"), asset=asset,
                       old_stop=stop_anterior, new_stop=be, mfe_pct=pos["mfe_pct"])

        _save(state)
        stop_reason = "BREAKEVEN" if pos.get("breakeven_applied") else "STOP"
        if pos["direction"] == "LONG":
            if candle["low"] <= pos["stop"]:
                _event("STOP_TOUCHED", trade_id=pos.get("trade_id"), asset=asset, candle=candle)
                messages.append(await _close_position(state, asset, pos["stop"], stop_reason))
            elif candle["high"] >= pos["target"]:
                _event("TARGET_TOUCHED", trade_id=pos.get("trade_id"), asset=asset, candle=candle)
                messages.append(await _close_position(state, asset, pos["target"], "TARGET"))
        else:
            if candle["high"] >= pos["stop"]:
                _event("STOP_TOUCHED", trade_id=pos.get("trade_id"), asset=asset, candle=candle)
                messages.append(await _close_position(state, asset, pos["stop"], stop_reason))
            elif candle["low"] <= pos["target"]:
                _event("TARGET_TOUCHED", trade_id=pos.get("trade_id"), asset=asset, candle=candle)
                messages.append(await _close_position(state, asset, pos["target"], "TARGET"))

    if len(state["open"]) >= MAX_OPEN:
        _event("CYCLE_BLOCKED", reason="max_open", open_count=len(state["open"]))
        return messages
    if _today_pnl(state) <= -STARTING_EQUITY * DAILY_LOSS_PCT:
        _event("CYCLE_BLOCKED", reason="daily_loss_limit", today_pnl=_today_pnl(state))
        return messages

    for asset in ASSETS:
        if asset in state["open"] or len(state["open"]) >= MAX_OPEN:
            continue
        last_entry = state["last_entry_at"].get(asset)
        if last_entry:
            elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last_entry)
            if elapsed.total_seconds() < COOLDOWN_MINUTES * 60:
                continue

        bars_5m, bars_15m, bars_1h = await asyncio.gather(
            dp.get_ohlcv(asset, "5m", 120), dp.get_ohlcv(asset, "15m", 100), dp.get_ohlcv(asset, "1h", 100)
        )
        if min(len(bars_5m), len(bars_15m), len(bars_1h)) < 56:
            _event("DATA_ERROR", asset=asset, reason="insufficient_signal_bars",
                   counts={"5m": len(bars_5m), "15m": len(bars_15m), "1h": len(bars_1h)})
            continue
        candle_key = str(bars_5m[-2]["time"])
        if state["last_evaluated_candle"].get(asset) == candle_key:
            continue
        state["last_evaluated_candle"][asset] = candle_key
        signal = _signal(asset, bars_5m, bars_15m, bars_1h)
        if not signal:
            _event("DATA_ERROR", asset=asset, reason="signal_calculation_failed")
            continue
        _event("SIGNAL_EVALUATED", asset=asset, candle_time=signal["candle_time"],
               direction=signal["direction"], score=signal["score"], eligible=signal["eligible"],
               checks=signal["checks"], rsi5=signal["rsi5"], volume_ratio=signal["volume_ratio"])
        if not signal["eligible"]:
            _event("SIGNAL_REJECTED", asset=asset, candle_time=signal["candle_time"],
                   score=signal["score"], failed=[name for name, ok in signal["checks"].items() if not ok])
            _save(state)
            continue

        direction = signal["direction"]
        snapshot, rules = await asyncio.gather(_market_snapshot(asset), _instrument_rules(asset))
        if not rules or rules.get("status") != "Trading":
            _event("ORDER_REJECTED", asset=asset, reason="instrument_not_tradeable", rules=rules)
            _save(state)
            continue
        executable = snapshot.get("bybit_ask") if direction == "LONG" else snapshot.get("bybit_bid")
        quoted_entry = executable or signal["signal_price"]
        entry_raw = quoted_entry * (1 + SLIPPAGE_RATE if direction == "LONG" else 1 - SLIPPAGE_RATE)
        entry = _quantize(entry_raw, rules["tick_size"], ROUND_HALF_UP)
        # v1.1 BUGFIX: SL/TP se re-anclan a la ENTRADA REAL, no al signal_price.
        # Antes, un fill 15-30 bps peor que la señal dejaba el target más cerca
        # y el stop más lejos: el RR planteado de 1.6 colapsaba a 0.4-0.9 en
        # la mitad de los trades. La geometría debe medirse desde donde entrás.
        stop_distance = max(signal["atr"] * 1.3, entry * 0.0035)
        if direction == "LONG":
            signal["stop"] = _quantize(entry - stop_distance, rules["tick_size"], ROUND_HALF_UP)
            signal["target"] = _quantize(entry + stop_distance * RR_TARGET, rules["tick_size"], ROUND_HALF_UP)
        else:
            signal["stop"] = _quantize(entry + stop_distance, rules["tick_size"], ROUND_HALF_UP)
            signal["target"] = _quantize(entry - stop_distance * RR_TARGET, rules["tick_size"], ROUND_HALF_UP)
        stop_distance = abs(entry - signal["stop"])
        risk_usdt = state["equity"] * RISK_PCT
        qty_raw = risk_usdt / stop_distance
        qty = _quantize(qty_raw, rules["qty_step"], ROUND_DOWN)
        notional = entry * qty
        leverage = min(PAPER_LEVERAGE, int(rules["max_leverage"]))
        margin = notional / leverage if leverage else notional
        used_margin = sum(p.get("estimated_margin", 0) for p in state["open"].values())
        rejection = None
        if qty < rules["min_qty"]:
            rejection = "below_min_qty"
        elif rules["min_notional"] and notional < rules["min_notional"]:
            rejection = "below_min_notional"
        elif rules["max_market_qty"] and qty > rules["max_market_qty"]:
            rejection = "above_max_market_qty"
        elif used_margin + margin > state["equity"] * MARGIN_TOTAL_MAX_PCT:
            rejection = "portfolio_margin_limit"
        if rejection:
            _event("ORDER_REJECTED", asset=asset, reason=rejection, qty_raw=qty_raw, qty=qty,
                   notional=notional, margin=margin, used_margin=used_margin, rules=rules)
            _save(state)
            continue
        entry_fee = entry * qty * FEE_MAKER   # v1.1: entrada limit post-only
        position = {
            **signal, "trade_id": _next_trade_id(state),
            "entry": entry, "qty": qty, "risk_usdt": risk_usdt,
            "entry_fee": entry_fee, "opened_at": datetime.now(timezone.utc).isoformat(),
            "entry_snapshot": snapshot, "quoted_entry": quoted_entry,
            "instrument_rules": rules, "qty_raw": qty_raw, "notional": notional,
            "leverage": leverage, "estimated_margin": margin,
            "signal_to_fill_bps": (entry - signal["signal_price"]) / signal["signal_price"] * 10_000,
            "mfe_pct": 0, "mae_pct": 0, "mode": "SHADOW", "strategy": STRATEGY_TAG,
            "audit_level": "full",
        }
        state["open"][asset] = position
        state["last_entry_at"][asset] = position["opened_at"]
        _save(state)
        _event("TRADE_OPENED", trade_id=position["trade_id"], asset=asset, direction=direction,
               entry=entry, qty=qty, notional=notional, leverage=leverage, margin=margin,
               stop=signal["stop"], target=signal["target"], score=signal["score"], rules=rules,
               snapshot=snapshot)
        messages.append(
            f"⚡ **PAPER {position['trade_id']} ABIERTO — {asset} {direction}**\n"
            f"Entrada simulada: `${entry:,.2f}` · Stop: `${signal['stop']:,.2f}` · TP: `${signal['target']:,.2f}`\n"
            f"Score: `{signal['score']}%` · RSI 5m: `{signal['rsi5']:.1f}` · Volumen: `{signal['volume_ratio']:.2f}x`\n"
            f"Bybit bid/ask: `${snapshot.get('bybit_bid', 0):,.2f}` / `${snapshot.get('bybit_ask', 0):,.2f}` · Spread: `{snapshot.get('spread_bps', 0):.2f} bps`\n"
            f"Desvío señal→fill: `{position['signal_to_fill_bps']:+.2f} bps` · Fuentes válidas: `{snapshot.get('sources_ok', 0)}/3`\n"
            f"Δ Binance/Bybit: `{snapshot.get('binance_deviation_bps', 0):+.2f} bps` · Δ Twelve/Bybit: `{snapshot.get('twelve_deviation_bps', 0):+.2f} bps`\n"
            f"Tendencia 1h: `{signal['trend_1h']}` · Riesgo: `${risk_usdt:.3f}` · Modo: `SHADOW`"
            f"\nQty Bybit: `{qty}` · Leverage: `{leverage}x` · Margen estimado: `${margin:.2f}`"
        )

    return messages


def summary() -> str:
    state = _load()
    closed = state["closed"]
    wins = [t for t in closed if t.get("pnl_net", 0) > 0]
    losses = [t for t in closed if t.get("pnl_net", 0) <= 0]
    gross_win = sum(t["pnl_net"] for t in wins)
    gross_loss = abs(sum(t["pnl_net"] for t in losses))
    fees = sum(t.get("entry_fee", 0) + t.get("exit_fee", 0) for t in closed)
    pnl = sum(t.get("pnl_net", 0) for t in closed)
    avg_duration = sum(t.get("duration_minutes", 0) for t in closed) / len(closed) if closed else 0
    avg_mfe = sum(t.get("mfe_pct", 0) for t in closed) / len(closed) if closed else 0
    avg_mae = sum(t.get("mae_pct", 0) for t in closed) / len(closed) if closed else 0
    avg_fill_deviation = sum(abs(t.get("signal_to_fill_bps", 0)) for t in closed) / len(closed) if closed else 0
    win_rate = len(wins) / len(closed) * 100 if closed else 0
    profit_factor = gross_win / gross_loss if gross_loss else (float("inf") if gross_win else 0)
    drawdown = (state["peak_equity"] - state["equity"]) / state["peak_equity"] * 100 if state["peak_equity"] else 0
    elapsed = max(1, (datetime.now(timezone.utc) - datetime.fromisoformat(state["experiment_started_at"])).total_seconds())
    uptime_pct = min(100, float(state.get("online_seconds", 0)) / elapsed * 100)
    pf_text = "∞" if profit_factor == float("inf") else f"{profit_factor:.2f}"
    return (
        "📊 **FAST PAPER v1 — RESUMEN**\n"
        f"Trades cerrados: `{len(closed)}` · Abiertos: `{len(state['open'])}`\n"
        f"Win rate: `{win_rate:.1f}%` · Profit factor: `{pf_text}`\n"
        f"PnL neto: `{pnl:+.4f} USDT` · Comisiones: `${fees:.4f}`\n"
        f"Duración media: `{avg_duration:.1f} min` · MFE/MAE medio: `{avg_mfe:.3f}% / {avg_mae:.3f}%`\n"
        f"Desvío señal→fill medio: `{avg_fill_deviation:.2f} bps`\n"
        f"Equity: `${state['equity']:.2f}` · DD actual/máximo: `{drawdown:.2f}% / {state.get('max_drawdown_pct', 0):.2f}%`\n"
        f"Uptime: `{uptime_pct:.1f}%` · Ciclos: `{state.get('cycles_executed', 0)}` · Perdidos: `{state.get('cycles_missed', 0)}`\n"
        f"PnL de hoy: `{_today_pnl(state):+.4f} USDT` · Modo: `SHADOW`"
    )


def _official_week_pnl(start: datetime) -> tuple[int, float]:
    if not OFFICIAL_TRADES_PATH.exists():
        return 0, 0.0
    try:
        trades = json.loads(OFFICIAL_TRADES_PATH.read_text())
    except Exception:
        return 0, 0.0
    local_tz = ZoneInfo("America/Argentina/Buenos_Aires")
    selected = []
    for trade in trades:
        ts = trade.get("timestamp")
        if not ts or trade.get("estado") not in ("CERRADO", "CERRADO_MANUAL"):
            continue
        try:
            dt = datetime.fromisoformat(ts)
            dt = dt.replace(tzinfo=local_tz).astimezone(timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except ValueError:
            continue
        if dt >= start:
            selected.append(trade)
    return len(selected), sum(float(t.get("pnl_usdt") or 0) for t in selected)


async def weekly_report(force: bool = False) -> str | None:
    state = _load()
    now = datetime.now(timezone.utc)
    last = state.get("last_weekly_report_at")
    experiment_start = datetime.fromisoformat(state["experiment_started_at"])
    if not force:
        anchor = datetime.fromisoformat(last) if last else experiment_start
        if now - anchor < timedelta(days=7):
            return None
    start = max(experiment_start, now - timedelta(days=7))
    fast = [t for t in state["closed"] if t.get("closed_at") and datetime.fromisoformat(t["closed_at"]) >= start]
    fast_pnl = sum(t.get("pnl_net", 0) for t in fast)
    fast_wins = sum(t.get("pnl_net", 0) > 0 for t in fast)
    fully_audited = sum(t.get("audit_level") == "full" for t in fast)
    official_n, official_pnl = _official_week_pnl(start)

    benchmark_parts = []
    benchmark_total = 0.0
    for asset in ASSETS:
        bars, current = await asyncio.gather(dp.get_ohlcv(asset, "1h", 170), dp.get_precio(asset))
        start_bar = min(bars, key=lambda b: abs(b["time"] - int(start.timestamp()))) if bars else None
        if start_bar and current:
            pnl = (current / start_bar["close"] - 1) * (STARTING_EQUITY / len(ASSETS))
            benchmark_total += pnl
            benchmark_parts.append(f"{asset} `{pnl:+.3f}`")

    elapsed = max(1, (now - experiment_start).total_seconds())
    uptime_pct = min(100, float(state.get("online_seconds", 0)) / elapsed * 100)
    state["last_weekly_report_at"] = now.isoformat()
    _save(state)
    _event("WEEKLY_REPORT", period_start=start.isoformat(), fast_trades=len(fast),
           fast_pnl=fast_pnl, official_trades=official_n, official_pnl=official_pnl,
           benchmark_pnl=benchmark_total, uptime_pct=uptime_pct)
    period_days = max(1, math.ceil((now - start).total_seconds() / 86400))
    return (
        f"📅 **COMPARATIVO SEMANAL — {period_days} DÍAS**\n"
        f"Fast Paper: `{len(fast)} trades` · `{fast_pnl:+.4f} USDT` · WR `{(fast_wins / len(fast) * 100 if fast else 0):.1f}%`\n"
        f"Muestra con auditoría completa: `{fully_audited}/{len(fast)}`\n"
        f"JARVIS oficial: `{official_n} trades` · `{official_pnl:+.4f} USDT`\n"
        f"Buy & Hold 50/50: `{benchmark_total:+.4f} USDT` ({' · '.join(benchmark_parts) or 'sin datos'})\n"
        f"Ventaja Fast vs Hold: `{fast_pnl - benchmark_total:+.4f} USDT`\n"
        f"Uptime del experimento: `{uptime_pct:.1f}%` · Ciclos perdidos: `{state.get('cycles_missed', 0)}`\n"
        f"DD máximo Fast: `{state.get('max_drawdown_pct', 0):.2f}%` · Modo: `SHADOW`"
    )
