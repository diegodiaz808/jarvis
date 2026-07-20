"""
Data Provider — hub central de datos de mercado con fallback automático.
Crypto: BTC, ETH, SOL (Binance/Bybit/CoinGecko).
Acciones: AAPL, NVDA, TSLA, META, MSFT, AMZN, GOOGL (Twelve Data → Yahoo).

Fuentes por tipo de dato:
  Precio / OHLCV : Binance spot → Bybit → CoinGecko
  RSI / MACD     : Calculados desde OHLCV — sin APIs externas
  CVD            : Binance Futures taker volume → inferido desde OHLCV
  Funding Rate   : Binance Futures → Bybit → CoinGlass (público)
  Open Interest  : Binance Futures → Bybit
  L/S Ratio      : Binance Futures → CoinGlass (público)
  Taker Ratio    : Binance Futures → CoinGlass (público)
"""
from __future__ import annotations

import asyncio
import os
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp

log = logging.getLogger("data_provider")

# ─── URLs base ────────────────────────────────────────────────────────────────

BINANCE_SPOT    = "https://api.binance.com"
BINANCE_FUTURES = "https://fapi.binance.com"
BYBIT_BASE      = "https://api.bybit.com"
COINGECKO_BASE  = "https://api.coingecko.com/api/v3"
COINGLASS_BASE  = "https://open-api.coinglass.com/public/v2"
TWELVE_DATA_BASE = "https://api.twelvedata.com"

COINGECKO_IDS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}

# ─── Acciones — datos vía Yahoo Finance ───────────────────────────────────────
# Las acciones se operan como perpetuos linear en Bybit, pero su precio/OHLCV
# viene de Yahoo Finance (gratis, sin API key ni rate limit). No tienen
# funding/OI/CVD on-chain → esas métricas se reportan como "no disponible".
STOCKS = {"AAPL", "NVDA", "TSLA", "META", "MSFT", "AMZN", "GOOGL"}

YAHOO_CHARTS = (
    "https://query1.finance.yahoo.com/v8/finance/chart",
    "https://query2.finance.yahoo.com/v8/finance/chart",
)

# timeframe interno → (intervalo Yahoo, rango Yahoo, factor de agregación)
# Yahoo no expone 2h/4h: se piden velas de 60m y se agregan en bloques.
_YAHOO_TF = {
    "5m":  ("5m",  "5d",  1),
    "15m": ("15m", "1mo", 1),
    "30m": ("30m", "1mo", 1),
    "1h":  ("60m", "3mo", 1),
    "2h":  ("60m", "6mo", 2),
    "4h":  ("60m", "1y",  4),
    "1d":  ("1d",  "2y",  1),
    "1w":  ("1wk", "5y",  1),
}

_TWELVE_TF = {
    "1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
    "1h": "1h", "2h": "2h", "4h": "4h", "1d": "1day", "1w": "1week",
}


def es_stock(simbolo: str) -> bool:
    return simbolo.split("/")[0].upper() in STOCKS


def _agregar_velas(bars: list[dict], factor: int) -> list[dict]:
    """Agrupa velas consecutivas en bloques de `factor` (para 2h/4h desde 1h)."""
    if factor <= 1:
        return bars
    out = []
    for i in range(0, len(bars), factor):
        bloque = bars[i:i + factor]
        if not bloque:
            continue
        out.append({
            "time":   bloque[0]["time"],
            "open":   bloque[0]["open"],
            "high":   max(b["high"] for b in bloque),
            "low":    min(b["low"]  for b in bloque),
            "close":  bloque[-1]["close"],
            "volume": sum(b["volume"] for b in bloque),
        })
    return out


async def _yahoo_ohlcv(simbolo: str, timeframe: str, bars: int) -> list[dict]:
    """OHLCV de acciones desde Yahoo Finance. Formato estándar del provider."""
    intervalo, rango, factor = _YAHOO_TF.get(timeframe, ("60m", "3mo", 1))
    sym = simbolo.split("/")[0].upper()
    cache_key = (sym, timeframe, bars)
    params = {"interval": intervalo, "range": rango}
    headers = {"User-Agent": "Mozilla/5.0"}

    for intento in range(2):
        for base_url in YAHOO_CHARTS:
            data = await _get(f"{base_url}/{sym}", params, headers=headers)
            try:
                res = data["chart"]["result"][0]
                ts  = res["timestamp"]
                q   = res["indicators"]["quote"][0]
            except (KeyError, IndexError, TypeError):
                continue

            crudas = []
            for i, t in enumerate(ts):
                o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
                if None in (o, h, l, c):
                    continue
                crudas.append({"time": int(t), "open": float(o), "high": float(h),
                               "low": float(l), "close": float(c),
                               "volume": float(q["volume"][i] or 0)})

            agregadas = _agregar_velas(crudas, factor)[-bars:]
            if agregadas:
                _STOCK_OHLCV_CACHE[cache_key] = (time.time(), agregadas)
                return agregadas

        if intento == 0:
            await asyncio.sleep(0.4)

    cached = _STOCK_OHLCV_CACHE.get(cache_key)
    if cached and time.time() - cached[0] <= _STOCK_CACHE_TTL:
        return cached[1]
    return []


async def _twelve_ohlcv(simbolo: str, timeframe: str, bars: int, warn: bool = True) -> list[dict]:
    """OHLCV de acciones o cripto desde Twelve Data; [] activa el fallback."""
    api_key = os.getenv("TWELVE_DATA_API_KEY", "").strip()
    interval = _TWELVE_TF.get(timeframe)
    if not api_key or not interval:
        return []

    base = simbolo.split("/")[0].upper()
    sym = base if es_stock(simbolo) else f"{base}/USD"
    cache_key = (sym, timeframe, bars)
    cached = _TWELVE_OHLCV_CACHE.get(cache_key)
    if cached and time.time() - cached[0] <= _TWELVE_CACHE_TTL:
        return cached[1]

    data = await _get(
        f"{TWELVE_DATA_BASE}/time_series",
        {"symbol": sym, "interval": interval, "outputsize": min(max(bars, 1), 5000),
         "order": "asc", "timezone": "UTC", "apikey": api_key},
    )
    if not isinstance(data, dict) or data.get("status") == "error":
        message = data.get("message", "respuesta inválida") if isinstance(data, dict) else "sin respuesta"
        if warn:
            _warn_once(f"twelve:{sym}:{timeframe}", f"Twelve Data {sym} {timeframe}: {message}")
        return []

    result = []
    for value in data.get("values", []):
        try:
            dt = datetime.fromisoformat(value["datetime"]).replace(tzinfo=timezone.utc)
            result.append({
                "time": int(dt.timestamp()), "open": float(value["open"]),
                "high": float(value["high"]), "low": float(value["low"]),
                "close": float(value["close"]), "volume": float(value.get("volume") or 0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    result.sort(key=lambda bar: bar["time"])
    result = result[-bars:]
    if result:
        _TWELVE_OHLCV_CACHE[cache_key] = (time.time(), result)
    return result

BINANCE_TF = {
    "1m": "1m",  "5m": "5m",  "15m": "15m", "30m": "30m",
    "1h": "1h",  "2h": "2h",  "4h": "4h",   "6h": "6h",
    "12h": "12h", "1d": "1d", "1w": "1w",
}

BYBIT_TF = {
    "1m": "1",   "5m": "5",   "15m": "15",  "30m": "30",
    "1h": "60",  "2h": "120", "4h": "240",  "6h": "360",
    "12h": "720", "1d": "D",  "1w": "W",
}

_TIMEOUT = aiohttp.ClientTimeout(total=10)
_STOCK_CACHE_TTL = 60 * 60 * 12
_TWELVE_CACHE_TTL = 60 * 5
_WARN_COOLDOWN = 60 * 10

_STOCK_OHLCV_CACHE: dict[tuple[str, str, int], tuple[float, list[dict]]] = {}
_TWELVE_OHLCV_CACHE: dict[tuple[str, str, int], tuple[float, list[dict]]] = {}
_LAST_WARN: dict[str, float] = {}


# ─── HTTP helper ──────────────────────────────────────────────────────────────

async def _get(url: str, params: dict = None, headers: dict = None) -> Optional[dict | list]:
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
            async with s.get(url, params=params, headers=headers) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
    except Exception as e:
        log.debug(f"HTTP {url}: {e}")
    return None


def _warn_once(key: str, msg: str, cooldown: int = _WARN_COOLDOWN) -> None:
    now = time.time()
    last = _LAST_WARN.get(key, 0)
    if now - last >= cooldown:
        _LAST_WARN[key] = now
        log.warning(msg)


# ─── Cálculo de indicadores (sin APIs) ───────────────────────────────────────

def _calc_rsi(closes: list, periodo: int = 14) -> Optional[float]:
    if len(closes) < periodo + 1:
        return None
    deltas   = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    ganancias = [d if d > 0 else 0 for d in deltas]
    perdidas  = [abs(d) if d < 0 else 0 for d in deltas]
    avg_g = sum(ganancias[-periodo:]) / periodo
    avg_p = sum(perdidas[-periodo:]) / periodo
    if avg_p == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_g / avg_p)), 2)


def _calc_macd(closes: list, rapida: int = 12, lenta: int = 26, senal: int = 9) -> dict:
    def ema(datos, periodo):
        k = 2 / (periodo + 1)
        e = [datos[0]]
        for p in datos[1:]:
            e.append(p * k + e[-1] * (1 - k))
        return e

    if len(closes) < lenta + senal:
        return {}
    ema_r     = ema(closes, rapida)
    ema_l     = ema(closes, lenta)
    macd_line = [ema_r[i] - ema_l[i] for i in range(len(ema_l))]
    sig_line  = ema(macd_line[-(senal * 3):], senal)
    macd_val  = round(macd_line[-1], 6)
    sig_val   = round(sig_line[-1], 6)
    return {"macd": macd_val, "signal": sig_val, "histogram": round(macd_val - sig_val, 6)}


def _cvd_inferido(bars: list) -> dict:
    """CVD inferido: divergencia precio/volumen."""
    if len(bars) < 8:
        return {"divergente": False, "disponible": False, "fuente": "inferido"}
    closes  = [b["close"] for b in bars]
    vol_rec = sum(b["volume"] for b in bars[-3:]) / 3
    vol_ant = sum(b["volume"] for b in bars[-8:-3]) / 5
    precio_sube = closes[-1] > closes[-5] if len(closes) >= 5 else False
    vol_cae     = vol_rec < vol_ant * 0.85
    divergente  = (precio_sube and vol_cae) or (not precio_sube and not vol_cae)
    return {"divergente": divergente, "disponible": True, "fuente": "inferido"}


# ─── PRECIO ───────────────────────────────────────────────────────────────────

async def get_precio(simbolo: str) -> Optional[float]:
    """Binance spot → Bybit → CoinGecko (cripto) · Twelve Data → Yahoo (acciones)."""
    if es_stock(simbolo):
        bars = await _twelve_ohlcv(simbolo, "1h", 1, warn=False) or await _yahoo_ohlcv(simbolo, "1h", 1)
        if bars:
            return bars[-1]["close"]
        _warn_once(
            f"stock-price:{simbolo}",
            f"[data_provider] Sin precio (stock) para {simbolo}",
        )
        return None

    sym = simbolo.upper() + "USDT"

    data = await _get(f"{BINANCE_SPOT}/api/v3/ticker/price", {"symbol": sym})
    if data and data.get("price"):
        return float(data["price"])

    data = await _get(f"{BYBIT_BASE}/v5/market/tickers",
                      {"category": "linear", "symbol": sym})
    try:
        return float(data["result"]["list"][0]["lastPrice"])
    except Exception:
        pass

    bars = await _twelve_ohlcv(simbolo, "1h", 1)
    if bars:
        return bars[-1]["close"]

    cg_id = COINGECKO_IDS.get(simbolo.upper())
    if cg_id:
        data = await _get(f"{COINGECKO_BASE}/simple/price",
                          {"ids": cg_id, "vs_currencies": "usd"})
        try:
            return float(data[cg_id]["usd"])
        except Exception:
            pass

    log.warning(f"[data_provider] Sin precio para {simbolo}")
    return None


# ─── OHLCV ────────────────────────────────────────────────────────────────────

async def get_ohlcv(simbolo: str, timeframe: str = "4h", bars: int = 100) -> list[dict]:
    """Binance → Bybit → CoinGecko (cripto) · Twelve Data → Yahoo (acciones).
    Formato: [{time,open,high,low,close,volume}]."""
    if es_stock(simbolo):
        bars_out = await _twelve_ohlcv(simbolo, timeframe, bars, warn=False)
        if not bars_out:
            bars_out = await _yahoo_ohlcv(simbolo, timeframe, bars)
        if not bars_out:
            _warn_once(
                f"stock-ohlcv:{simbolo}:{timeframe}",
                f"[data_provider] Sin OHLCV (stock) para {simbolo} {timeframe}",
            )
        return bars_out

    sym  = simbolo.upper() + "USDT"
    tf_b = BINANCE_TF.get(timeframe, "4h")
    tf_y = BYBIT_TF.get(timeframe, "240")

    # 1. Binance
    data = await _get(f"{BINANCE_SPOT}/api/v3/klines",
                      {"symbol": sym, "interval": tf_b, "limit": bars})
    if isinstance(data, list) and data:
        return [
            {"time": int(k[0]) // 1000, "open": float(k[1]),
             "high": float(k[2]), "low": float(k[3]),
             "close": float(k[4]), "volume": float(k[5])}
            for k in data
        ]

    # 2. Bybit
    data = await _get(f"{BYBIT_BASE}/v5/market/kline",
                      {"category": "linear", "symbol": sym,
                       "interval": tf_y, "limit": bars})
    try:
        raw = data["result"]["list"]
        result = [
            {"time": int(k[0]) // 1000, "open": float(k[1]),
             "high": float(k[2]), "low": float(k[3]),
             "close": float(k[4]), "volume": float(k[5])}
            for k in raw
        ]
        result.sort(key=lambda b: b["time"])
        return result
    except Exception:
        pass

    # 3. Twelve Data: respaldo normalizado para cripto
    twelve_bars = await _twelve_ohlcv(simbolo, timeframe, bars)
    if twelve_bars:
        return twelve_bars

    # 4. CoinGecko (solo diario/semanal)
    if timeframe in ("1d", "1w"):
        cg_id = COINGECKO_IDS.get(simbolo.upper())
        days  = bars if timeframe == "1d" else bars * 7
        if cg_id:
            data = await _get(f"{COINGECKO_BASE}/coins/{cg_id}/ohlc",
                              {"vs_currency": "usd", "days": min(days, 365)})
            if isinstance(data, list):
                return [
                    {"time": int(k[0]) // 1000, "open": float(k[1]),
                     "high": float(k[2]), "low": float(k[3]),
                     "close": float(k[4]), "volume": 0}
                    for k in data[-bars:]
                ]

    log.warning(f"[data_provider] Sin OHLCV para {simbolo} {timeframe}")
    return []


# ─── INDICADORES (RSI + MACD desde OHLCV) ────────────────────────────────────

async def get_indicadores(simbolo: str, timeframe: str = "4h",
                          bars: int = 100) -> dict:
    """RSI y MACD calculados localmente. Nunca falla por API externa."""
    ohlcv = await get_ohlcv(simbolo, timeframe, bars)
    if not ohlcv:
        return {"rsi": None, "macd": {}, "bars": [], "precio": None}
    closes = [b["close"] for b in ohlcv]
    return {
        "rsi":    _calc_rsi(closes),
        "macd":   _calc_macd(closes),
        "bars":   ohlcv,
        "precio": closes[-1] if closes else None,
    }


# ─── CVD ──────────────────────────────────────────────────────────────────────

async def get_cvd(simbolo: str, timeframe: str = "4h", bars: int = 20) -> dict:
    """
    Binance Futures taker volume (real) → inferido desde OHLCV.
    Retorna: {divergente, disponible, fuente, sesgo?, buy_ratio?}
    """
    # Acciones: sin taker-volume on-chain → CVD inferido desde velas.
    if es_stock(simbolo):
        ohlcv = await get_ohlcv(simbolo, timeframe, max(bars, 10))
        return _cvd_inferido(ohlcv)

    sym = simbolo.upper() + "USDT"

    data = await _get(f"{BINANCE_FUTURES}/futures/data/takerbuyselledVolume",
                      {"symbol": sym, "period": "1h", "limit": 5})
    if isinstance(data, list) and len(data) >= 2:
        try:
            buy_vol  = sum(float(d.get("buyVol",  0)) for d in data)
            sell_vol = sum(float(d.get("sellVol", 0)) for d in data)
            total    = buy_vol + sell_vol
            if total > 0:
                buy_ratio = buy_vol / total
                if buy_ratio > 0.60:
                    return {"divergente": True, "disponible": True,
                            "fuente": "binance_taker", "sesgo": "LONG",
                            "buy_ratio": round(buy_ratio, 3)}
                elif buy_ratio < 0.40:
                    return {"divergente": True, "disponible": True,
                            "fuente": "binance_taker", "sesgo": "SHORT",
                            "buy_ratio": round(buy_ratio, 3)}
                else:
                    return {"divergente": False, "disponible": True,
                            "fuente": "binance_taker", "sesgo": "neutro",
                            "buy_ratio": round(buy_ratio, 3)}
        except Exception:
            pass

    ohlcv = await get_ohlcv(simbolo, timeframe, max(bars, 10))
    return _cvd_inferido(ohlcv)


# ─── FUNDING RATE ─────────────────────────────────────────────────────────────

async def get_funding_rate(simbolo: str) -> dict:
    """Binance Futures → Bybit → CoinGlass. (Acciones: no aplica.)"""
    if es_stock(simbolo):
        return {"rate": 0, "disponible": False, "fuente": None}
    sym = simbolo.upper() + "USDT"

    data = await _get(f"{BINANCE_FUTURES}/fapi/v1/fundingRate",
                      {"symbol": sym, "limit": 1})
    if isinstance(data, list) and data:
        rate = float(data[0].get("fundingRate", 0)) * 100
        return {"rate": rate, "disponible": True, "fuente": "binance"}

    data = await _get(f"{BYBIT_BASE}/v5/market/funding/history",
                      {"category": "linear", "symbol": sym, "limit": 1})
    try:
        rate = float(data["result"]["list"][0]["fundingRate"]) * 100
        return {"rate": rate, "disponible": True, "fuente": "bybit"}
    except Exception:
        pass

    data = await _get(f"{COINGLASS_BASE}/funding_usd_rate",
                      {"symbol": simbolo.upper()})
    try:
        rate = float(data["data"][0]["rate"])
        return {"rate": rate, "disponible": True, "fuente": "coinglass"}
    except Exception:
        pass

    return {"rate": 0, "disponible": False, "fuente": None}


# ─── OPEN INTEREST ────────────────────────────────────────────────────────────

async def get_open_interest(simbolo: str) -> dict:
    """Binance Futures → Bybit. (Acciones: no aplica.)"""
    if es_stock(simbolo):
        return {"subiendo": None, "disponible": False, "fuente": None}
    sym = simbolo.upper() + "USDT"

    hist = await _get(f"{BINANCE_FUTURES}/futures/data/openInterestHist",
                      {"symbol": sym, "period": "1h", "limit": 3})
    if isinstance(hist, list) and len(hist) >= 2:
        try:
            oi_now  = float(hist[-1].get("sumOpenInterest", 0))
            oi_prev = float(hist[-2].get("sumOpenInterest", 0))
            return {"subiendo": oi_now > oi_prev, "valor": oi_now,
                    "disponible": True, "fuente": "binance"}
        except Exception:
            pass

    data = await _get(f"{BYBIT_BASE}/v5/market/open-interest",
                      {"category": "linear", "symbol": sym,
                       "intervalTime": "4h", "limit": 2})
    try:
        lista   = data["result"]["list"]
        oi_now  = float(lista[0]["openInterest"])
        oi_prev = float(lista[1]["openInterest"])
        return {"subiendo": oi_now > oi_prev, "valor": oi_now,
                "disponible": True, "fuente": "bybit"}
    except Exception:
        pass

    return {"subiendo": None, "disponible": False, "fuente": None}


# ─── LONG / SHORT RATIO ───────────────────────────────────────────────────────

async def get_long_short_ratio(simbolo: str) -> dict:
    """Binance Futures → CoinGlass. (Acciones: no aplica.)"""
    if es_stock(simbolo):
        return {"long_pct": 50, "disponible": False, "fuente": None}
    sym = simbolo.upper() + "USDT"

    data = await _get(f"{BINANCE_FUTURES}/futures/data/globalLongShortAccountRatio",
                      {"symbol": sym, "period": "1h", "limit": 1})
    if isinstance(data, list) and data:
        long_pct = float(data[0].get("longAccount", 0.5)) * 100
        return {"long_pct": long_pct, "disponible": True, "fuente": "binance"}

    data = await _get(f"{COINGLASS_BASE}/global_long_short_account_ratio",
                      {"symbol": simbolo.upper(), "interval": "h1", "limit": 1})
    try:
        ratio = float(data["data"][-1].get("longRatio", 0.5)) * 100
        return {"long_pct": ratio, "disponible": True, "fuente": "coinglass"}
    except Exception:
        pass

    return {"long_pct": 50, "disponible": False, "fuente": None}


# ─── TAKER BUY / SELL RATIO ──────────────────────────────────────────────────

async def get_taker_ratio(simbolo: str) -> dict:
    """Binance Futures → CoinGlass. (Acciones: no aplica.)"""
    if es_stock(simbolo):
        return {"ratio": 1.0, "disponible": False, "fuente": None}
    sym = simbolo.upper() + "USDT"

    data = await _get(f"{BINANCE_FUTURES}/futures/data/takerbuyselledVolume",
                      {"symbol": sym, "period": "1h", "limit": 1})
    if isinstance(data, list) and data:
        try:
            buy_vol  = float(data[0].get("buyVol",  0))
            sell_vol = float(data[0].get("sellVol", 0))
            ratio    = (buy_vol / sell_vol) if sell_vol > 0 else 1.0
            return {"ratio": round(ratio, 3), "disponible": True, "fuente": "binance"}
        except Exception:
            pass

    data = await _get(f"{COINGLASS_BASE}/taker_buy_sell_ratio",
                      {"symbol": simbolo.upper(), "interval": "h1", "limit": 1})
    try:
        ratio = float(data["data"][-1].get("ratio", 1.0))
        return {"ratio": ratio, "disponible": True, "fuente": "coinglass"}
    except Exception:
        pass

    return {"ratio": 1.0, "disponible": False, "fuente": None}
