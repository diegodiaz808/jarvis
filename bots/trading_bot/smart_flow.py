"""
Smart Flow Strategy — datos desde data_provider (Binance/Bybit/CoinGecko/CoinGlass).
Crypto: CVD, Funding Rate, OI, L/S Ratio, Taker Ratio, RSI, Net Flow.

Retorna formato normalizado para el engine de confluencia:
  puntuacion, direccion, score, score_max, score_raw, score_pct,
  leverage, razones, indicadores_usados, indicadores_descartados,
  error, operable
"""
import asyncio
import logging

log = logging.getLogger("smart_flow")

# Peso de cada indicador — crypto (6 indicadores, 100 pts posibles)
# RSI eliminado: no es indicador de flujo (es precio). Redundante con Zone Flip.
_PESO_CRYPTO = {
    "cvd":     25,
    "funding": 25,
    "oi":      15,
    "ls":      15,
    "taker":   10,
    "netflow": 10,
}

# Validación profesional
MIN_CRYPTO = 4                              # mínimo 4 de 6 indicadores
INDICADORES_CRITICOS_CRYPTO = {"cvd", "funding"}  # ideales: ambos
CRITICOS_MINIMO_CRYPTO = 1                 # pero al menos 1 de los 2
SCORE_MINIMO_OPERACION_CRYPTO = 70         # score mínimo 70%
RATIO_MINIMO_PUNTOS_CRYPTO = 0.65          # al menos 65% de puntos


# ── Resultado vacío estandarizado ────────────────────────────────────────────

def _resultado_vacio(simbolo: str, error: str = None, razones: list = None) -> dict:
    return {
        "puntuacion":              0,
        "direccion":               None,
        "estrategia":              "smart_flow",
        "simbolo":                 simbolo,
        "score":                   0,
        "score_max":               0,
        "score_raw":               0,
        "score_pct":               0,
        "leverage":                2,
        "razones":                 razones or [],
        "indicadores_usados":      [],
        "indicadores_descartados": [],
        "error":                   error,
        "operable":                False,
    }


# ── Leverage desde score_pct ──────────────────────────────────────────────────

def _leverage_desde_score(score_pct: float) -> int:
    if score_pct >= 90:
        return 10
    elif score_pct >= 75:
        return 7
    elif score_pct >= 60:
        return 5
    elif score_pct >= 45:
        return 3
    else:
        return 2


# ── Net flow inferido desde OHLCV ─────────────────────────────────────────────

def _netflow_desde_ohlcv(bars: list) -> dict:
    if len(bars) < 3:
        return {"saliendo": None, "disponible": False}
    closes = [b["close"] for b in bars]
    return {"saliendo": closes[-1] > closes[-3], "disponible": True, "fuente": "inferido"}


# ── Evaluadores individuales CRYPTO ──────────────────────────────────────────
# Cada función retorna: {"valido": bool, "puntos": int, "razon": str, "dir": "LONG"/"SHORT"/None}

def _ind_cvd(cvd_data: dict, bars: list) -> dict:
    """CVD desde data_provider. Soporta sesgo explícito (taker) e inferido."""
    if not cvd_data.get("disponible"):
        return {"valido": False, "puntos": 0, "razon": "⚠️ CVD no disponible — descartado", "dir": None}

    fuente = cvd_data.get("fuente", "?")
    sesgo  = cvd_data.get("sesgo")  # "LONG", "SHORT", "neutro" — solo Binance taker

    # Sesgo explícito (Binance taker buy/sell ratio)
    if sesgo in ("LONG", "SHORT"):
        br = cvd_data.get("buy_ratio", 0)
        return {"valido": True, "puntos": _PESO_CRYPTO["cvd"],
                "razon": f"✅ CVD taker sesgo {sesgo} ({br:.0%}) [{fuente}]",
                "dir": sesgo}

    # Divergencia precio/volumen (binance_taker neutro o inferido)
    if cvd_data.get("divergente"):
        closes = [b["close"] for b in bars] if bars else []
        if len(closes) >= 5:
            if closes[-1] > closes[-5]:
                return {"valido": True, "puntos": _PESO_CRYPTO["cvd"],
                        "razon": f"✅ CVD divergente en máximos → distribución → SHORT [{fuente}]",
                        "dir": "SHORT"}
            else:
                return {"valido": True, "puntos": _PESO_CRYPTO["cvd"],
                        "razon": f"✅ CVD divergente en mínimos → acumulación → LONG [{fuente}]",
                        "dir": "LONG"}

    return {"valido": True, "puntos": 0, "razon": f"❌ CVD sin señal clara [{fuente}]", "dir": None}


def _ind_funding(fr_data: dict) -> dict:
    """Funding Rate CONTINUO: cuanto más extremo, mejor."""
    if not fr_data.get("disponible"):
        return {"valido": False, "puntos": 0, "razon": "⚠️ Funding no disponible", "dir": None}

    rate   = fr_data["rate"]
    fuente = fr_data.get("fuente", "?")
    # |rate| 0.01 → fuerza 0 | 0.05 → 0.5 | 0.10+ → 1.0
    abs_rate = abs(rate)
    if abs_rate > 0.01:
        fuerza = max(0, min((abs_rate - 0.01) / 0.09, 1.0))
        pts = round(_PESO_CRYPTO["funding"] * fuerza, 1)
        direccion = "SHORT" if rate > 0 else "LONG"
        return {"valido": True, "puntos": pts,
                "razon": f"{'✅' if fuerza >= 0.4 else '⚠️'} Funding {rate:.4f}% "
                         f"(fuerza {fuerza:.0%}) [{fuente}] → {direccion} · "
                         f"{pts}/{_PESO_CRYPTO['funding']}pts",
                "dir": direccion}
    return {"valido": True, "puntos": 0,
            "razon": f"❌ Funding {rate:.4f}% [{fuente}] — neutro", "dir": None}


def _ind_oi(oi_data: dict, dir_tentativa: str | None) -> dict:
    """OI CONTINUO según delta de cambio."""
    if not oi_data.get("disponible"):
        return {"valido": False, "puntos": 0, "razon": "⚠️ OI no disponible", "dir": None}

    fuente   = oi_data.get("fuente", "?")
    subiendo = oi_data.get("subiendo")
    delta    = abs(oi_data.get("delta_pct", 0.5) or 0.5)
    # delta 0% → 0 | 2% → 0.5 | 5%+ → 1.0
    fuerza = max(0, min(delta / 5.0, 1.0))
    base = 0.5  # mínimo si confirma

    if subiendo and dir_tentativa == "SHORT":
        pts = round(_PESO_CRYPTO["oi"] * (base + (1-base) * fuerza), 1)
        return {"valido": True, "puntos": pts,
                "razon": f"✅ OI subiendo ({delta:.1f}%) — longs frágiles → SHORT [{fuente}] · "
                         f"{pts}/{_PESO_CRYPTO['oi']}pts", "dir": "SHORT"}
    elif subiendo is False and dir_tentativa == "LONG":
        pts = round(_PESO_CRYPTO["oi"] * (base + (1-base) * fuerza), 1)
        return {"valido": True, "puntos": pts,
                "razon": f"✅ OI bajando ({delta:.1f}%) — capitulación → LONG [{fuente}] · "
                         f"{pts}/{_PESO_CRYPTO['oi']}pts", "dir": "LONG"}
    dir_str = dir_tentativa or "?"
    return {"valido": True, "puntos": 0,
            "razon": f"❌ OI no confirma {dir_str} [{fuente}]", "dir": None}


def _ind_ls(ls_data: dict, dir_tentativa: str | None) -> dict:
    """L/S Ratio CONTINUO según extremo del posicionamiento."""
    if not ls_data.get("disponible"):
        return {"valido": False, "puntos": 0, "razon": "⚠️ L/S no disponible", "dir": None}
    lp = ls_data["long_pct"]
    # SHORT: longs > 60 → fuerza 0 | 75 → 0.5 | 90+ → 1.0
    # LONG:  longs < 40 → fuerza 0 | 25 → 0.5 | 10- → 1.0
    if lp > 60 and dir_tentativa == "SHORT":
        fuerza = max(0, min((lp - 60) / 30, 1.0))
        pts = round(_PESO_CRYPTO["ls"] * fuerza, 1)
        return {"valido": True, "puntos": pts,
                "razon": f"{'✅' if fuerza >= 0.4 else '⚠️'} {lp:.0f}% longs "
                         f"(fuerza {fuerza:.0%}) → SHORT · {pts}/{_PESO_CRYPTO['ls']}pts",
                "dir": "SHORT"}
    elif lp < 40 and dir_tentativa == "LONG":
        fuerza = max(0, min((40 - lp) / 30, 1.0))
        pts = round(_PESO_CRYPTO["ls"] * fuerza, 1)
        return {"valido": True, "puntos": pts,
                "razon": f"{'✅' if fuerza >= 0.4 else '⚠️'} {lp:.0f}% longs "
                         f"(fuerza {fuerza:.0%}) → LONG · {pts}/{_PESO_CRYPTO['ls']}pts",
                "dir": "LONG"}
    return {"valido": True, "puntos": 0,
            "razon": f"❌ L/S {lp:.0f}% longs — neutral", "dir": None}


def _ind_taker(taker_data: dict, dir_tentativa: str | None) -> dict:
    """Taker Ratio CONTINUO según extremo."""
    if not taker_data.get("disponible"):
        return {"valido": False, "puntos": 0, "razon": "⚠️ Taker no disponible", "dir": None}
    ratio = taker_data["ratio"]
    # SHORT: ratio > 1.2 → 0 | 1.6 → 0.5 | 2.0+ → 1.0
    # LONG:  ratio < 0.8 → 0 | 0.4 → 0.5 | 0.2- → 1.0 (inverso)
    if ratio > 1.2 and dir_tentativa == "SHORT":
        fuerza = max(0, min((ratio - 1.2) / 0.8, 1.0))
        pts = round(_PESO_CRYPTO["taker"] * fuerza, 1)
        return {"valido": True, "puntos": pts,
                "razon": f"{'✅' if fuerza >= 0.4 else '⚠️'} Taker {ratio:.2f} "
                         f"(fuerza {fuerza:.0%}) → SHORT · {pts}/{_PESO_CRYPTO['taker']}pts",
                "dir": "SHORT"}
    elif ratio < 0.8 and dir_tentativa == "LONG":
        fuerza = max(0, min((0.8 - ratio) / 0.6, 1.0))
        pts = round(_PESO_CRYPTO["taker"] * fuerza, 1)
        return {"valido": True, "puntos": pts,
                "razon": f"{'✅' if fuerza >= 0.4 else '⚠️'} Taker {ratio:.2f} "
                         f"(fuerza {fuerza:.0%}) → LONG · {pts}/{_PESO_CRYPTO['taker']}pts",
                "dir": "LONG"}
    return {"valido": True, "puntos": 0,
            "razon": f"❌ Taker {ratio:.2f} — neutral", "dir": None}


def _ind_netflow(bars: list, dir_tentativa: str | None) -> dict:
    """Netflow CONTINUO según momentum de cierres."""
    nf = _netflow_desde_ohlcv(bars) if bars else {"disponible": False}
    if not nf.get("disponible"):
        return {"valido": False, "puntos": 0, "razon": "⚠️ Netflow no disponible", "dir": None}

    # Confirmación binaria + factor de fuerza moderado (no hay magnitud real)
    confirma_short = (not nf["saliendo"]) and dir_tentativa == "SHORT"
    confirma_long  = nf["saliendo"] and dir_tentativa == "LONG"

    if confirma_short or confirma_long:
        # Default 70% del peso (señal inferida, no real)
        pts = round(_PESO_CRYPTO["netflow"] * 0.7, 1)
        accion = "entrando" if confirma_short else "saliendo"
        signal = "presión vendedora → SHORT" if confirma_short else "acumulación → LONG"
        return {"valido": True, "puntos": pts,
                "razon": f"✅ BTC {accion} a exchanges → {signal} (inferido) · "
                         f"{pts}/{_PESO_CRYPTO['netflow']}pts",
                "dir": dir_tentativa}
    return {"valido": True, "puntos": 0,
            "razon": "❌ Netflow no confirma dirección (inferido)", "dir": None}


# ── Detección de conflicto de flujo ──────────────────────────────────────────

def _detectar_conflicto(resultados: dict) -> tuple[bool, str]:
    dirs_activas = [
        r["dir"] for r in resultados.values()
        if r["valido"] and r["puntos"] > 0 and r["dir"] in ("LONG", "SHORT")
    ]
    if not dirs_activas:
        return False, ""
    longs  = dirs_activas.count("LONG")
    shorts = dirs_activas.count("SHORT")
    if longs > 0 and shorts > 0:
        return True, f"Indicadores activos en conflicto: {longs} LONG vs {shorts} SHORT"
    return False, ""


# ── Dirección tentativa desde indicadores primarios ───────────────────────────

def _direccion_tentativa(cvd_r: dict, funding_r: dict) -> str | None:
    dirs = [r["dir"] for r in (cvd_r, funding_r) if r["valido"] and r["dir"]]
    if not dirs:
        return None
    longs  = dirs.count("LONG")
    shorts = dirs.count("SHORT")
    if shorts > longs:
        return "SHORT"
    if longs > shorts:
        return "LONG"
    return None


# ── Evaluación CRYPTO ─────────────────────────────────────────────────────────

async def _evaluar_crypto(simbolo: str) -> dict:
    from bots import data_provider as dp

    # Todas las fuentes en paralelo — barras solo para netflow/CVD inferido
    ls_data, taker_data, fr_data, oi_data, cvd_data, bars_data = await asyncio.gather(
        dp.get_long_short_ratio(simbolo),
        dp.get_taker_ratio(simbolo),
        dp.get_funding_rate(simbolo),
        dp.get_open_interest(simbolo),
        dp.get_cvd(simbolo, "30m", 20),
        dp.get_ohlcv(simbolo, "1h", 24),
    )

    bars = bars_data or []

    # ── Paso 1: indicadores independientes de dirección ────────
    cvd_r     = _ind_cvd(cvd_data, bars)
    funding_r = _ind_funding(fr_data)

    dir_tent = _direccion_tentativa(cvd_r, funding_r)

    # ── Paso 2: indicadores que requieren dirección tentativa ───
    oi_r      = _ind_oi(oi_data, dir_tent)
    ls_r      = _ind_ls(ls_data, dir_tent)
    taker_r   = _ind_taker(taker_data, dir_tent)
    netflow_r = _ind_netflow(bars, dir_tent)

    resultados = {
        "cvd":     cvd_r,
        "funding": funding_r,
        "oi":      oi_r,
        "ls":      ls_r,
        "taker":   taker_r,
        "netflow": netflow_r,
    }

    # ── Paso 3: separar usados / descartados ───────────────────
    usados      = [n for n, r in resultados.items() if r["valido"]]
    descartados = [n for n, r in resultados.items() if not r["valido"]]
    razones     = [r["razon"] for r in resultados.values()]

    puntos_raw = sum(r["puntos"] for r in resultados.values() if r["valido"])
    peso_max   = sum(_PESO_CRYPTO[n] for n in usados)

    # ── Paso 4: validar indicadores críticos (al menos 1 de CVD o Funding) ────
    criticos_presentes = INDICADORES_CRITICOS_CRYPTO & set(usados)
    if len(criticos_presentes) < CRITICOS_MINIMO_CRYPTO:
        faltantes = INDICADORES_CRITICOS_CRYPTO - criticos_presentes
        razones.append(
            f"❌ Faltan indicadores críticos: {', '.join(faltantes)} — no operable"
        )
        return {
            "puntuacion":              0,
            "direccion":               None,
            "estrategia":              "smart_flow",
            "simbolo":                 simbolo,
            "score":                   puntos_raw,
            "score_max":               peso_max,
            "score_raw":               puntos_raw,
            "score_pct":               0,
            "leverage":                2,
            "razones":                 razones,
            "indicadores_usados":      usados,
            "indicadores_descartados": descartados,
            "error":                   None,
            "operable":                False,
        }

    # ── Paso 5: mínimo de indicadores (4+) ──────────────────────
    if len(usados) < MIN_CRYPTO:
        razones.append(
            f"❌ Solo {len(usados)} indicador/es (mínimo {MIN_CRYPTO}) — no operable"
        )
        return {
            "puntuacion":              0,
            "direccion":               None,
            "estrategia":              "smart_flow",
            "simbolo":                 simbolo,
            "score":                   puntos_raw,
            "score_max":               peso_max,
            "score_raw":               puntos_raw,
            "score_pct":               0,
            "leverage":                2,
            "razones":                 razones,
            "indicadores_usados":      usados,
            "indicadores_descartados": descartados,
            "error":                   None,
            "operable":                False,
        }

    # ── Paso 6: score normalizado ───────────────────────────────
    score_pct = round((puntos_raw / peso_max) * 100, 1) if peso_max > 0 else 0

    # ── Paso 7: validar ratio mínimo de puntos (65% del peso_max)
    ratio_puntos = (puntos_raw / peso_max) if peso_max > 0 else 0
    if ratio_puntos < RATIO_MINIMO_PUNTOS_CRYPTO:
        razones.append(
            f"⚠️ Score {score_pct}% pero solo {ratio_puntos:.0%} confirmado — no operable"
        )
        return {
            "puntuacion":              0,
            "direccion":               None,
            "estrategia":              "smart_flow",
            "simbolo":                 simbolo,
            "score":                   puntos_raw,
            "score_max":               peso_max,
            "score_raw":               puntos_raw,
            "score_pct":               score_pct,
            "leverage":                2,
            "razones":                 razones,
            "indicadores_usados":      usados,
            "indicadores_descartados": descartados,
            "error":                   None,
            "operable":                False,
        }

    # ── Paso 6: conflicto de flujo ──────────────────────────────
    conflicto, razon_conflicto = _detectar_conflicto(resultados)
    if conflicto:
        razones.append(f"⚠️ Conflicto de flujo: {razon_conflicto}")
        return {
            "puntuacion":              int(score_pct),
            "direccion":               None,
            "estrategia":              "smart_flow",
            "simbolo":                 simbolo,
            "score":                   puntos_raw,
            "score_max":               peso_max,
            "score_raw":               puntos_raw,
            "score_pct":               score_pct,
            "leverage":                2,
            "razones":                 razones,
            "indicadores_usados":      usados,
            "indicadores_descartados": descartados,
            "error":                   None,
            "operable":                False,
        }

    # ── Paso 8: dirección final ─────────────────────────────────
    dirs_activas = [
        r["dir"] for r in resultados.values()
        if r["valido"] and r["puntos"] > 0 and r["dir"]
    ]
    if not dirs_activas or score_pct < SCORE_MINIMO_OPERACION_CRYPTO:
        if score_pct < SCORE_MINIMO_OPERACION_CRYPTO:
            razones.append(f"⚠️ Score {score_pct}% < {SCORE_MINIMO_OPERACION_CRYPTO}% — sin señal")
        else:
            razones.append("⚠️ Sin indicadores activos que definan dirección")
        direccion = None
        operable  = False
    else:
        longs  = dirs_activas.count("LONG")
        shorts = dirs_activas.count("SHORT")
        if shorts > longs:
            direccion = "SHORT"
        elif longs > shorts:
            direccion = "LONG"
        else:
            razones.append("⚠️ Empate LONG/SHORT entre indicadores activos — sin señal")
            direccion = None
        operable = direccion is not None

    leverage = min(_leverage_desde_score(score_pct), 10) if operable else 2
    razones.append(
        f"Score: {puntos_raw}/{peso_max} ({score_pct}%) → Leverage: {leverage}x"
    )
    if descartados:
        razones.append(f"⚠️ Descartados: {', '.join(descartados)}")

    return {
        "puntuacion":              int(score_pct),
        "direccion":               direccion,
        "estrategia":              "smart_flow",
        "simbolo":                 simbolo,
        "score":                   puntos_raw,
        "score_max":               peso_max,
        "score_raw":               puntos_raw,
        "score_pct":               score_pct,
        "leverage":                leverage,
        "razones":                 razones,
        "indicadores_usados":      usados,
        "indicadores_descartados": descartados,
        "error":                   None,
        "operable":                operable,
    }


# ── API pública ───────────────────────────────────────────────────────────────

# ── Evaluación ACCIONES ───────────────────────────────────────────────────────
# Las acciones (perpetuos linear en Bybit) no tienen funding/OI/L-S/CVD on-chain.
# Reemplazamos el flujo cripto por proxies derivados de precio+volumen de Twelve
# Data: CVD inferido (cuerpos de vela), spike de volumen y sesgo put/call proxy.

_PESO_STOCK = {"cvd": 35, "volumen": 25, "putcall": 25, "momentum": 15}
SCORE_MINIMO_OPERACION_STOCK = 60   # sobre peso_total=100 → exige 2 indicadores fuertes
MIN_INDICADORES_STOCK        = 2    # mínimo de indicadores de acuerdo en la dirección
DOMINANCIA_MINIMA_STOCK      = 35   # ventaja neta sobre el lado contrario (≥ 1 indicador fuerte)


def _volume_ratio_desde_bars(bars: list) -> float | None:
    """Volumen de la última vela vs promedio de las últimas 20. Sin API externa."""
    if len(bars) < 5:
        return None
    vol_rec = bars[-1]["volume"]
    vol_avg = sum(b["volume"] for b in bars[-20:]) / min(20, len(bars))
    return round(vol_rec / vol_avg, 2) if vol_avg > 0 else None


def _put_call_proxy_desde_bars(bars: list) -> dict:
    """Sesgo direccional por volumen alcista vs bajista (proxy put/call)."""
    if len(bars) < 10:
        return {"sesgo": "neutral", "disponible": False}
    ultimas = bars[-10:]
    sub = sum(b["volume"] for b in ultimas if b["close"] > b["open"])
    baj = sum(b["volume"] for b in ultimas if b["close"] < b["open"])
    total = sub + baj
    if total == 0:
        return {"sesgo": "neutral", "disponible": False}
    ratio = sub / total
    sesgo = "alcista" if ratio > 0.65 else "bajista" if ratio < 0.35 else "neutral"
    return {"sesgo": sesgo, "ratio_alcista": round(ratio, 2), "disponible": True}


async def _evaluar_stock(simbolo: str) -> dict:
    from bots import data_provider as dp

    cvd_data, bars_1h = await asyncio.gather(
        dp.get_cvd(simbolo, "1h", 24),     # inferido desde OHLCV para stocks
        dp.get_ohlcv(simbolo, "1h", 24),
    )
    bars = bars_1h or []
    vol_ratio = _volume_ratio_desde_bars(bars)
    putcall   = _put_call_proxy_desde_bars(bars)

    votos: list[tuple[str, int, str]] = []   # (dir, puntos, razon)
    razones: list[str] = []
    usados, descartados = [], []

    # 1) CVD inferido (sesgo de compra/venta por cuerpos de vela)
    if cvd_data.get("disponible") and cvd_data.get("sesgo") in ("LONG", "SHORT"):
        d = cvd_data["sesgo"]
        votos.append((d, _PESO_STOCK["cvd"], f"✅ CVD inferido → {d} (buy_ratio {cvd_data.get('buy_ratio')})"))
        usados.append("cvd")
    else:
        descartados.append("cvd")

    # 2) Spike de volumen (confirma la vela actual, no define dirección sola)
    vela_dir = None
    if bars:
        vela_dir = "LONG" if bars[-1]["close"] >= bars[-1]["open"] else "SHORT"
    if vol_ratio and vol_ratio >= 1.3 and vela_dir:
        votos.append((vela_dir, _PESO_STOCK["volumen"], f"✅ Volumen {vol_ratio}× sobre media → {vela_dir}"))
        usados.append("volumen")
    else:
        descartados.append("volumen")

    # 3) Proxy put/call (sesgo direccional por volumen alcista vs bajista)
    if putcall.get("disponible") and putcall.get("sesgo") in ("alcista", "bajista"):
        d = "LONG" if putcall["sesgo"] == "alcista" else "SHORT"
        votos.append((d, _PESO_STOCK["putcall"], f"✅ Put/Call proxy {putcall['sesgo']} → {d}"))
        usados.append("putcall")
    else:
        descartados.append("putcall")

    # 4) Momentum simple (precio actual vs 3 velas atrás)
    nf = _netflow_desde_ohlcv(bars)
    if nf.get("disponible"):
        d = "LONG" if nf["saliendo"] else "SHORT"
        votos.append((d, _PESO_STOCK["momentum"], f"✅ Momentum 1H → {d}"))
        usados.append("momentum")
    else:
        descartados.append("momentum")

    if not votos:
        return _resultado_vacio(
            simbolo, razones=["⚠️ Sin datos de flujo para la acción — no operable"]
        )

    # ── Consenso direccional ponderado por puntos ──────────────
    long_pts  = sum(p for d, p, _ in votos if d == "LONG")
    short_pts = sum(p for d, p, _ in votos if d == "SHORT")
    if long_pts > short_pts:
        direccion, puntos_dir = "LONG", long_pts
    elif short_pts > long_pts:
        direccion, puntos_dir = "SHORT", short_pts
    else:
        return {
            **_resultado_vacio(simbolo, razones=["⚠️ Empate LONG/SHORT en flujo de la acción"]),
            "indicadores_usados": usados, "indicadores_descartados": descartados,
        }

    # ── Score normalizado contra el peso TOTAL (no solo lo que disparó) ──
    # Clave: normalizar contra peso_total evita el bug de "1 indicador = 100%".
    # Un solo indicador a favor (ej. CVD=35) da 35%, nunca operable.
    peso_total = sum(_PESO_STOCK.values())            # 100
    opuesto    = short_pts if direccion == "LONG" else long_pts
    n_acuerdo  = sum(1 for d, _, _ in votos if d == direccion)
    score_pct  = round((puntos_dir / peso_total) * 100, 1)
    razones = [r for d, _, r in votos if d == direccion]

    # Requisitos para operar una acción (estrictos, anti "cualquier trade"):
    #   - al menos 2 indicadores de acuerdo en la misma dirección
    #   - score ≥ mínimo (necesita 2 indicadores fuertes, no 1)
    #   - dominancia neta clara sobre el lado contrario
    motivos_no = []
    if n_acuerdo < MIN_INDICADORES_STOCK:
        motivos_no.append(f"solo {n_acuerdo} indicador/es a favor (mín {MIN_INDICADORES_STOCK})")
    if score_pct < SCORE_MINIMO_OPERACION_STOCK:
        motivos_no.append(f"score {score_pct}% < {SCORE_MINIMO_OPERACION_STOCK}%")
    if puntos_dir - opuesto < DOMINANCIA_MINIMA_STOCK:
        motivos_no.append(f"dominancia {puntos_dir - opuesto} < {DOMINANCIA_MINIMA_STOCK}")

    operable = not motivos_no
    if not operable:
        razones.append("⚠️ No operable: " + " · ".join(motivos_no))
    leverage = min(_leverage_desde_score(score_pct), 10) if operable else 2
    razones.append(
        f"Score acción: {puntos_dir}/{peso_total} ({score_pct}%) · "
        f"{n_acuerdo} a favor · neto {puntos_dir - opuesto} → Leverage {leverage}x"
    )
    if descartados:
        razones.append(f"⚠️ Descartados (no aplican a acciones): {', '.join(descartados)}")
    peso_max = peso_total

    return {
        "puntuacion":              int(score_pct),
        "direccion":               direccion if operable else None,
        "estrategia":              "smart_flow",
        "simbolo":                 simbolo,
        "score":                   puntos_dir,
        "score_max":               peso_max,
        "score_raw":               puntos_dir,
        "score_pct":               score_pct,
        "leverage":                leverage,
        "razones":                 razones,
        "indicadores_usados":      usados,
        "indicadores_descartados": descartados,
        "error":                   None,
        "operable":                operable,
    }


async def evaluar_smart_flow(simbolo: str) -> dict:
    """
    Evalúa Smart Flow. Crypto usa flujo on-chain (CVD/funding/OI/L-S/taker);
    las acciones usan proxies de precio+volumen desde Twelve Data.
    Retorna formato normalizado compatible con el engine de confluencia.
    """
    try:
        from bots.data_provider import es_stock
        if es_stock(simbolo):
            return await _evaluar_stock(simbolo)
        return await _evaluar_crypto(simbolo)
    except Exception as e:
        log.error(f"[smart_flow] Error inesperado en {simbolo}: {e}")
        return _resultado_vacio(simbolo, error=str(e), razones=[f"❌ Error: {e}"])


async def formatear_smart_flow(simbolo: str) -> str:
    r      = await evaluar_smart_flow(simbolo)
    estado = "⚡ SEÑAL" if r.get("operable") else "⏳ SIN SEÑAL"

    score_pct  = r.get("score_pct", 0)
    usados_str = ", ".join(r.get("indicadores_usados", [])) or "—"
    desc_str   = ""
    if r.get("indicadores_descartados"):
        desc_str = f"\n⚠️ Descartados: {', '.join(r['indicadores_descartados'])}"

    razones_str = "\n".join([f"  {ra}" for ra in r.get("razones", [])])

    from bots.data_provider import es_stock
    fuente = "📡 Yahoo Finance" if es_stock(simbolo) else "📡 Binance + CoinGlass"

    return (
        f"🔮 **Smart Flow — {simbolo}** {estado} {fuente}\n"
        f"Score: {r.get('score_raw', 0)}/{r.get('score_max', 0)} ({score_pct}%) → "
        f"Leverage: {r.get('leverage', 2)}x\n"
        f"Dirección: {r.get('direccion') or 'No operar'}\n"
        f"Indicadores usados: {usados_str}"
        f"{desc_str}\n"
        f"{razones_str}"
    )
