"""
Market Context — estado macro del mercado por activo.

Fuentes:
  Daily (1d, 100 barras): tendencia EMA20/EMA50, RSI macro
  4H   (4h,  50 barras): zonas estructurales de soporte/resistencia

Cache en memoria con TTL de 1 hora.
El engine llama a actualizar_todos() cada hora y a get_contexto()
antes de validar si una señal está alineada con el sesgo macro.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

log = logging.getLogger("market_context")

# ─── Cache ────────────────────────────────────────────────────────────────────

_cache: dict[str, dict] = {}
CACHE_TTL = 3600  # 1 hora en segundos


# ─── Helpers de indicadores (sin dependencias externas) ───────────────────────

def _ema(closes: list, periodo: int) -> list:
    if len(closes) < periodo:
        return []
    k = 2 / (periodo + 1)
    e = [closes[0]]
    for p in closes[1:]:
        e.append(p * k + e[-1] * (1 - k))
    return e


def _rsi(closes: list, periodo: int = 14) -> Optional[float]:
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


def _tendencia_desde_ema(closes_d: list) -> str:
    """
    Calcula tendencia macro usando EMA20 vs EMA50 en Daily.
    Retorna 'bullish', 'bearish' o 'lateral'.
    """
    if len(closes_d) < 52:
        return "lateral"
    ema20 = _ema(closes_d, 20)
    ema50 = _ema(closes_d, 50)
    if len(ema20) < 5 or len(ema50) < 5:
        return "lateral"

    # Confirmar con los últimos 3 cruces para evitar falsos positivos
    bullish_count = sum(1 for i in (-1, -2, -3) if ema20[i] > ema50[i])
    bearish_count = sum(1 for i in (-1, -2, -3) if ema20[i] < ema50[i])

    if bullish_count == 3:
        return "bullish"
    elif bearish_count == 3:
        return "bearish"
    return "lateral"


def _sesgo_desde_tendencia(tendencia: str, rsi_d: Optional[float]) -> str:
    """
    Deriva el sesgo operativo desde tendencia macro.

    Doctrina: NO se opera contra el macro, incluso si el RSI está extremo.
    "Los rebotes en bearish son oportunidades de SHORT, no de LONG"
    "Los pullbacks en bullish son oportunidades de LONG, no de SHORT"

    bullish  → LONG (siempre)
    bearish  → SHORT (siempre, incluso con RSI sobrevendido)
    lateral  → NEUTRAL
    """
    if tendencia == "bullish":
        return "LONG"
    elif tendencia == "bearish":
        return "SHORT"
    return "NEUTRAL"


# ─── Cálculo de contexto ──────────────────────────────────────────────────────

async def calcular_contexto(simbolo: str) -> dict:
    """
    Calcula el contexto macro de un activo.
    Daily → tendencia, RSI macro.
    4H   → zonas estructurales (soporte/resistencia de las últimas 20 velas).
    """
    from bots import data_provider as dp

    bars_d, bars_4h = await asyncio.gather(
        dp.get_ohlcv(simbolo, "1d", 100),
        dp.get_ohlcv(simbolo, "4h", 50),
    )

    if not bars_d:
        return {
            "simbolo":    simbolo,
            "tendencia":  "lateral",
            "sesgo":      "NEUTRAL",
            "rsi_diario": None,
            "estructura": {},
            "disponible": False,
            "error":      "Sin datos diarios",
        }

    closes_d  = [b["close"] for b in bars_d]
    tendencia = _tendencia_desde_ema(closes_d)
    rsi_d     = _rsi(closes_d)
    sesgo     = _sesgo_desde_tendencia(tendencia, rsi_d)

    # Zonas estructurales desde 4H
    estructura = {}
    if bars_4h:
        recientes = bars_4h[-20:] if len(bars_4h) >= 20 else bars_4h
        highs = [b["high"] for b in recientes]
        lows  = [b["low"]  for b in recientes]
        estructura = {
            "resistencia": round(max(highs), 2),
            "soporte":     round(min(lows), 2),
        }

    return {
        "simbolo":    simbolo.upper(),
        "tendencia":  tendencia,   # bullish / bearish / lateral
        "sesgo":      sesgo,       # LONG / SHORT / NEUTRAL
        "rsi_diario": rsi_d,
        "estructura": estructura,
        "disponible": True,
        "error":      None,
    }


# ─── API pública ──────────────────────────────────────────────────────────────

async def get_contexto(simbolo: str) -> dict:
    """Retorna contexto cacheado. Lo recalcula si el TTL expiró."""
    sym   = simbolo.upper()
    ahora = time.time()

    cached = _cache.get(sym)
    if cached and (ahora - cached["ts"]) < CACHE_TTL:
        return cached["data"]

    ctx = await calcular_contexto(sym)
    _cache[sym] = {"data": ctx, "ts": ahora}
    log.info(
        f"[ctx] {sym}: tendencia={ctx['tendencia']} "
        f"sesgo={ctx['sesgo']} RSI_d={ctx.get('rsi_diario')}"
    )
    return ctx


async def actualizar_todos(activos: list[str]) -> dict[str, dict]:
    """Fuerza actualización del contexto de todos los activos en paralelo."""
    ahora     = time.time()
    resultados = await asyncio.gather(*[calcular_contexto(a) for a in activos])
    out = {}
    for activo, ctx in zip(activos, resultados):
        sym = activo.upper()
        _cache[sym] = {"data": ctx, "ts": ahora}
        out[sym] = ctx
        log.info(
            f"[ctx] {sym}: tendencia={ctx['tendencia']} "
            f"sesgo={ctx['sesgo']} RSI_d={ctx.get('rsi_diario')}"
        )
    return out
