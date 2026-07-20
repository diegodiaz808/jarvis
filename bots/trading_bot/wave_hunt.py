"""
Wave Hunt v2 — usa TradingView Bridge para datos reales del chart.
SHORT en techo Wave 2/B, target Wave 3/C.
Horizonte: semanas / meses.

Retorna formato normalizado para el engine de confluencia:
  puntuacion, direccion, score, score_max, score_raw, score_pct,
  leverage, razones, indicadores_usados, indicadores_descartados,
  error, operable
"""
import asyncio
import logging

log = logging.getLogger("wave_hunt")

# WAVE_CONTEXT — se calcula UNA VEZ y se congela.
# Solo se invalida si el precio supera wave1_high (nuevo ATH = nuevo ciclo).
# Mientras estemos en corrección/Wave 3, el contexto no cambia aunque haya nuevos mínimos.
WAVE_CONTEXT: dict = {}

# Peso máximo de cada indicador (sobre N puntos posibles)
_PESO = {
    "rsi":     25,
    "macd":    25,
    "cvd":     25,
    "funding": 25,
}

# Validación profesional
MIN_INDICADORES = 3                        # mínimo 3 de 4 indicadores
INDICADORES_CRITICOS = {"rsi", "macd"}    # ambos obligatorios (momentum crítico)
SCORE_MINIMO_OPERACION = 70                # score mínimo 70%
RATIO_MINIMO_PUNTOS = 0.70                 # al menos 70% de puntos


# ── Respuesta vacía estandarizada ────────────────────────────────────────────

def _resultado_vacio(simbolo: str, error: str = None, razones: list = None,
                     pendiente: bool = False, fib: dict = None) -> dict:
    return {
        "puntuacion":              0,
        "direccion":               None,
        "estrategia":              "wave_hunt",
        "simbolo":                 simbolo,
        "score":                   0,
        "score_max":               0,
        "score_raw":               0,
        "score_pct":               0,
        "leverage":                2,
        "razones":                 razones or [],
        "indicadores_usados":      [],
        "indicadores_descartados": [],
        "fib":                     fib or {},
        "pendiente":               pendiente,
        "activacion":              None,
        "error":                   error,
        "operable":                False,
    }


# ── Leverage basado en score_pct ─────────────────────────────────────────────

def _leverage_desde_score(score_pct: float) -> int:
    if score_pct >= 90:
        return 10
    elif score_pct >= 75:
        return 7
    elif score_pct >= 60:
        return 6
    elif score_pct >= 45:
        return 5
    elif score_pct >= 30:
        return 3
    else:
        return 2


# ── Fibonacci ─────────────────────────────────────────────────────────────────

async def calcular_fibonacci_wave(simbolo: str) -> dict:
    """
    Calcula la estructura Wave Hunt desde velas semanales.

    Regla de congelamiento:
      - Si ya existe contexto para este símbolo Y el precio actual no supera
        wave1_high (no hay nuevo ATH), devuelve el contexto cacheado sin tocar.
      - Solo recalcula si: (a) no hay contexto previo, o (b) el precio superó
        wave1_high → nuevo ciclo alcista comenzó, contexto obsoleto.

    Esto evita que nuevos mínimos en Wave 3/C desplacen la zona_techo hacia arriba,
    invalidando entradas que ya estaban bien posicionadas.
    """
    from bots import data_provider as dp

    ctx = WAVE_CONTEXT.get(simbolo)

    # Si hay contexto previo y el precio no rompió el ATH anterior → usar caché
    if ctx and not ctx.get("error"):
        precio_live = await dp.get_precio(simbolo)
        if precio_live and precio_live < ctx["wave1_high"]:
            return ctx  # contexto congelado — Wave 3/C en curso

    # Sin contexto (primera vez) o nuevo ATH → recalcular
    bars = await dp.get_ohlcv(simbolo, "1w", 104)
    if not bars:
        return {"error": f"Sin datos OHLCV semanales para {simbolo}"}

    highs = [b["high"] for b in bars]
    lows  = [b["low"]  for b in bars]

    wave1_high_idx = highs.index(max(highs))
    wave1_high     = highs[wave1_high_idx]
    wave1_low      = min(lows[:wave1_high_idx + 1]) if wave1_high_idx > 0 else lows[0]

    post_high_lows = lows[wave1_high_idx:]
    if not post_high_lows:
        return {"error": "Sin datos post-ATH para calcular Wave 2"}
    wave2_low = min(post_high_lows)

    caida    = wave1_high - wave2_low
    zona_min = round(wave2_low + caida * 0.382, 2)
    zona_max = round(wave2_low + caida * 0.618, 2)

    wave_total  = wave1_high - wave1_low
    target_cons = round(wave1_high - wave_total * 0.618, 2)
    target_std  = round(wave1_high - wave_total * 1.0,   2)
    target_agr  = round(wave1_high - wave_total * 1.618, 2)

    resultado = {
        "wave1_high":  round(wave1_high, 2),
        "wave1_low":   round(wave1_low,  2),
        "wave1_size":  round(wave1_high - wave1_low, 2),
        "wave2_low":   round(wave2_low,  2),
        "fib_382":     zona_min,
        "zona_techo":  (zona_min, zona_max),
        "target_cons": target_cons,
        "target_std":  target_std,
        "target_agr":  target_agr,
    }

    # Guardar en caché — congelado hasta nuevo ATH
    WAVE_CONTEXT[simbolo] = resultado
    return resultado


# ── Indicadores individuales ──────────────────────────────────────────────────

def _evaluar_rsi(rsi) -> dict:
    """RSI CONTINUO: cuanto más alto, mejor para techo SHORT."""
    if rsi is None or not isinstance(rsi, (int, float)):
        return {"valido": False, "puntos": 0, "razon": "⚠️ RSI sin datos — descartado"}
    # RSI 60 → fuerza 0 | RSI 75 → 0.6 | RSI 85+ → 1.0
    if rsi > 60:
        fuerza = max(0, min((rsi - 60) / 25, 1.0))
        pts = round(_PESO["rsi"] * fuerza, 1)
        return {"valido": True, "puntos": pts,
                "razon": f"{'✅' if fuerza >= 0.4 else '⚠️'} RSI diario {rsi:.1f} "
                         f"(fuerza {fuerza:.0%}) → {pts}/{_PESO['rsi']}pts"}
    return {"valido": True, "puntos": 0,
            "razon": f"❌ RSI diario {rsi:.1f} — sin señal de techo"}


def _evaluar_macd(macd: dict) -> dict:
    """MACD CONTINUO: fuerza del histograma negativo + cruce bajista."""
    if not macd or macd.get("histogram") is None:
        return {"valido": False, "puntos": 0, "razon": "⚠️ MACD sin datos — descartado"}
    hist     = macd.get("histogram", 0) or 0
    macd_val = macd.get("macd",      0) or 0
    sig_val  = macd.get("signal",    0) or 0
    bajista  = hist < 0 or macd_val < sig_val
    if bajista:
        # 60% por confirmar + 40% según magnitud del histograma negativo
        base = _PESO["macd"] * 0.60
        # Fuerza relativa: hist/|macd_val|, cap 1.0
        denom  = max(abs(macd_val), abs(sig_val), 0.0001)
        fuerza = min(abs(hist) / denom, 1.0)
        pts = round(base + (_PESO["macd"] * 0.40 * fuerza), 1)
        return {"valido": True, "puntos": pts,
                "razon": f"✅ MACD diario bajista ({hist:.3f}, fuerza {fuerza:.0%}) → "
                         f"{pts}/{_PESO['macd']}pts"}
    return {"valido": True, "puntos": 0,
            "razon": f"❌ MACD diario ({hist:.3f}) — no confirma bajada"}


def _evaluar_cvd(cvd_data: dict, bars_ref: list) -> dict:
    """CVD CONTINUO: fuerza de divergencia + sesgo del taker."""
    closes = [b["close"] for b in bars_ref]

    if cvd_data.get("disponible"):
        fuente = cvd_data.get("fuente", "?")
        sesgo  = cvd_data.get("sesgo")  # SHORT/LONG/None
        # SHORT explícito = mejor para Wave Hunt
        if sesgo == "SHORT":
            br = cvd_data.get("buy_ratio", 0.5)
            # buy_ratio 0.5 → fuerza 0 | 0.3 → 0.4 | 0.0 → 1.0
            fuerza = max(0, min((0.5 - br) / 0.5, 1.0))
            pts = round(_PESO["cvd"] * (0.6 + 0.4 * fuerza), 1)
            return {"valido": True, "puntos": pts,
                    "razon": f"✅ CVD taker SHORT (br={br:.0%}, fuerza {fuerza:.0%}) "
                             f"[{fuente}] → {pts}/{_PESO['cvd']}pts"}
        if cvd_data.get("divergente"):
            return {"valido": True, "puntos": _PESO["cvd"] * 0.7,
                    "razon": f"✅ CVD divergente [{fuente}] → {_PESO['cvd']*0.7:.1f}/{_PESO['cvd']}pts"}
        return {"valido": True, "puntos": 0,
                "razon": f"❌ CVD sin divergencia [{fuente}]"}

    # Fallback inferido
    if len(closes) < 5:
        return {"valido": False, "puntos": 0,
                "razon": "⚠️ CVD: pocas barras — descartado"}
    subida   = closes[-1] > closes[-5]
    mom      = closes[-1] - closes[-3]
    mom_prev = closes[-3] - closes[-5]
    if subida and mom < mom_prev:
        # Cuánto se está desacelerando
        if mom_prev > 0:
            decel = max(0, min(1 - (mom / mom_prev), 1.0))
        else:
            decel = 0.5
        pts = round(_PESO["cvd"] * (0.5 + 0.5 * decel), 1)
        return {"valido": True, "puntos": pts,
                "razon": f"✅ CVD inferido (decel {decel:.0%}) → {pts}/{_PESO['cvd']}pts"}
    return {"valido": True, "puntos": 0, "razon": "❌ CVD sin divergencia inferida"}


def _evaluar_funding(fr_data: dict) -> dict:
    """Funding Rate CONTINUO: cuanto más positivo, mejor para SHORT."""
    if not fr_data.get("disponible"):
        return {"valido": False, "puntos": 0,
                "razon": "⚠️ Funding Rate no disponible — descartado"}
    rate   = fr_data["rate"]
    fuente = fr_data.get("fuente", "?")
    # rate 0.01 → fuerza 0 | 0.05 → 0.5 | 0.10+ → 1.0
    if rate > 0.01:
        fuerza = max(0, min((rate - 0.01) / 0.09, 1.0))
        pts = round(_PESO["funding"] * fuerza, 1)
        return {"valido": True, "puntos": pts,
                "razon": f"{'✅' if fuerza >= 0.4 else '⚠️'} Funding {rate:.4f}% "
                         f"(fuerza {fuerza:.0%}) [{fuente}] → {pts}/{_PESO['funding']}pts"}
    return {"valido": True, "puntos": 0,
            "razon": f"❌ Funding {rate:.4f}% [{fuente}] — neutro"}


# ── Detección de conflictos entre indicadores ────────────────────────────────

def _hay_conflicto(resultados: dict) -> tuple[bool, str]:
    """
    Detecta conflictos internos entre indicadores válidos.
    Wave Hunt es una estrategia bajista (SHORT), así que verificamos
    que los indicadores válidos no estén mayoritariamente alcistas.
    Retorna (hay_conflicto, razon).
    """
    validos_con_puntos  = sum(1 for r in resultados.values() if r["valido"] and r["puntos"] > 0)
    validos_sin_puntos  = sum(1 for r in resultados.values() if r["valido"] and r["puntos"] == 0)
    total_validos       = validos_con_puntos + validos_sin_puntos

    if total_validos == 0:
        return False, ""

    # Conflicto: más indicadores válidos contradicen la tesis bajista que la confirman
    if validos_sin_puntos > validos_con_puntos:
        razon = (
            f"{validos_sin_puntos}/{total_validos} indicadores válidos contradicen SHORT — "
            f"solo {validos_con_puntos} confirman"
        )
        return True, razon

    return False, ""


# ── Evaluación principal ──────────────────────────────────────────────────────

async def evaluar_wave_hunt(simbolo: str) -> dict:
    """
    Evalúa Wave Hunt para un activo.
    Retorna formato normalizado compatible con el engine de confluencia.
    """
    from bots import data_provider as dp

    # 1. Fibonacci
    fib = await calcular_fibonacci_wave(simbolo)
    if "error" in fib:
        return _resultado_vacio(
            simbolo,
            error=fib["error"],
            razones=[f"❌ Fibonacci: {fib['error']}"],
        )

    # 2. Daily (estructura wave + RSI/MACD macro) + 1H (RSI/MACD entry) + extras en paralelo
    #    Wave Hunt es estrategia de semanas → RSI/MACD Daily para techo de ciclo
    #    CVD en 30M para ver distribución reciente (sweet spot ruido/responsividad)
    ind_d, ind_1h, fr_data, cvd_data, precio_live = await asyncio.gather(
        dp.get_indicadores(simbolo, "1d", 180),
        dp.get_indicadores(simbolo, "1h", 48),   # RSI/MACD 1H para timing de entrada
        dp.get_funding_rate(simbolo),
        dp.get_cvd(simbolo, "30m", 20),
        dp.get_precio(simbolo),
    )

    razones = [
        f"📊 Wave 1: ${fib['wave1_low']:,.0f} → ${fib['wave1_high']:,.0f} (${fib['wave1_size']:,.0f})",
        f"📉 Wave 2 fondo actual: ${fib.get('wave2_low', fib['wave1_low']):,.0f}",
        f"🎯 Zona techo Wave 2/B (Fib 38.2-61.8%): ${fib['zona_techo'][0]:,.0f}–${fib['zona_techo'][1]:,.0f}",
    ]

    bars_d = ind_d.get("bars", [])
    precio = precio_live or ind_1h.get("precio") or ind_d.get("precio")
    # RSI/MACD: usa 1H para timing pero confirma con Daily para techo de ciclo
    rsi    = ind_1h.get("rsi") or ind_d.get("rsi")
    macd   = ind_1h.get("macd") or ind_d.get("macd") or {}

    if not bars_d or precio is None:
        return _resultado_vacio(
            simbolo,
            fib=fib,
            error="Sin datos diarios",
            razones=razones + ["❌ Sin datos diarios o precio"],
        )

    # 3. Verificar si precio está en zona Wave 2/B
    zona_min, zona_max = fib["zona_techo"]
    en_zona = zona_min <= precio <= zona_max

    razones.append(
        f"📍 Precio: ${precio:,.2f} {'✅ EN ZONA' if en_zona else '⏳ fuera de zona'}"
    )

    if not en_zona:
        # Pendiente — no es error, simplemente setup no activado
        return {
            "puntuacion":              5,
            "direccion":               None,
            "estrategia":              "wave_hunt",
            "simbolo":                 simbolo,
            "score":                   0,
            "score_max":               0,
            "score_raw":               0,
            "score_pct":               0,
            "leverage":                2,
            "razones":                 razones,
            "indicadores_usados":      [],
            "indicadores_descartados": [],
            "fib":                     fib,
            "pendiente":               True,
            "activacion":              f"${zona_min:,.0f}–${zona_max:,.0f}",
            "error":                   None,
            "operable":                False,
        }

    # 4. Evaluar cada indicador de forma independiente
    bars_ref = bars_d[-20:] if len(bars_d) >= 20 else bars_d

    resultados = {
        "rsi":     _evaluar_rsi(rsi),
        "macd":    _evaluar_macd(macd),
        "cvd":     _evaluar_cvd(cvd_data, bars_ref),
        "funding": _evaluar_funding(fr_data),
    }

    # 5. Separar usados / descartados / sumar score
    usados      = []
    descartados = []
    puntos_raw  = 0
    peso_max    = 0

    for nombre, res in resultados.items():
        razones.append(res["razon"])
        if res["valido"]:
            usados.append(nombre)
            puntos_raw += res["puntos"]
            peso_max   += _PESO[nombre]
        else:
            descartados.append(nombre)

    # 6. Validar indicadores críticos (RSI + MACD obligatorios)
    criticos_presentes = INDICADORES_CRITICOS & set(usados)
    if len(criticos_presentes) < len(INDICADORES_CRITICOS):
        faltantes = INDICADORES_CRITICOS - criticos_presentes
        razones.append(
            f"❌ Faltan indicadores críticos: {', '.join(faltantes)} — no operable"
        )
        return {
            "puntuacion":              0,
            "direccion":               None,
            "estrategia":              "wave_hunt",
            "simbolo":                 simbolo,
            "score":                   0,
            "score_max":               peso_max,
            "score_raw":               puntos_raw,
            "score_pct":               0,
            "leverage":                2,
            "razones":                 razones,
            "indicadores_usados":      usados,
            "indicadores_descartados": descartados,
            "fib":                     fib,
            "pendiente":               False,
            "activacion":              None,
            "error":                   None,
            "operable":                False,
        }

    # 7. Mínimo de indicadores (3+)
    if len(usados) < MIN_INDICADORES:
        razones.append(
            f"❌ Solo {len(usados)} indicador/es (mínimo {MIN_INDICADORES}) — no operable"
        )
        return {
            "puntuacion":              0,
            "direccion":               None,
            "estrategia":              "wave_hunt",
            "simbolo":                 simbolo,
            "score":                   0,
            "score_max":               peso_max,
            "score_raw":               puntos_raw,
            "score_pct":               0,
            "leverage":                2,
            "razones":                 razones,
            "indicadores_usados":      usados,
            "indicadores_descartados": descartados,
            "fib":                     fib,
            "pendiente":               False,
            "activacion":              None,
            "error":                   None,
            "operable":                False,
        }

    # 8. Score normalizado
    score_pct = round((puntos_raw / peso_max) * 100, 1) if peso_max > 0 else 0

    # 9. Validar ratio mínimo (70% de puntos)
    ratio_puntos = (puntos_raw / peso_max) if peso_max > 0 else 0
    if ratio_puntos < RATIO_MINIMO_PUNTOS:
        razones.append(
            f"⚠️ Score {score_pct}% pero solo {ratio_puntos:.0%} confirmado — no operable"
        )
        return {
            "puntuacion":              0,
            "direccion":               None,
            "estrategia":              "wave_hunt",
            "simbolo":                 simbolo,
            "score":                   puntos_raw,
            "score_max":               peso_max,
            "score_raw":               puntos_raw,
            "score_pct":               score_pct,
            "leverage":                2,
            "razones":                 razones,
            "indicadores_usados":      usados,
            "indicadores_descartados": descartados,
            "fib":                     fib,
            "pendiente":               False,
            "activacion":              None,
            "error":                   None,
            "operable":                False,
        }

    # 10. Detectar conflictos
    conflicto, razon_conflicto = _hay_conflicto(resultados)
    if conflicto:
        razones.append(f"⚠️ Conflicto detectado: {razon_conflicto}")
        return {
            "puntuacion":              int(score_pct),
            "direccion":               None,
            "estrategia":              "wave_hunt",
            "simbolo":                 simbolo,
            "score":                   puntos_raw,
            "score_max":               peso_max,
            "score_raw":               puntos_raw,
            "score_pct":               score_pct,
            "leverage":                2,
            "razones":                 razones,
            "indicadores_usados":      usados,
            "indicadores_descartados": descartados,
            "fib":                     fib,
            "pendiente":               False,
            "activacion":              None,
            "error":                   None,
            "operable":                False,
        }

    # 11. Dirección y leverage
    # Wave Hunt es exclusivamente bajista (SHORT en techo Wave 2/B)
    # Solo confirmamos SHORT si score_pct >= 70% (professional standard)
    if score_pct >= SCORE_MINIMO_OPERACION:
        direccion = "SHORT"
        operable  = True
    else:
        direccion = None
        operable  = False
        razones.append(f"⚠️ Score {score_pct}% < {SCORE_MINIMO_OPERACION}% — sin señal SHORT")

    leverage = min(_leverage_desde_score(score_pct), 10)

    # 10. Info targets
    razones += [
        f"🎯 Targets Wave 3:",
        f"  Conservador: ${fib['target_cons']:,.0f}",
        f"  Estándar:    ${fib['target_std']:,.0f}",
        f"  Agresivo:    ${fib['target_agr']:,.0f}",
        f"  SL: por encima de ${fib['fib_382']:,.0f}",
        f"  Score: {puntos_raw}/{peso_max} ({score_pct}%) → Leverage: {leverage}x",
    ]

    return {
        "puntuacion":              int(score_pct),
        "direccion":               direccion,
        "estrategia":              "wave_hunt",
        "simbolo":                 simbolo,
        "score":                   puntos_raw,
        "score_max":               peso_max,
        "score_raw":               puntos_raw,
        "score_pct":               score_pct,
        "leverage":                leverage,
        "razones":                 razones,
        "indicadores_usados":      usados,
        "indicadores_descartados": descartados,
        "fib":                     fib,
        "pendiente":               False,
        "activacion":              None,
        "error":                   None,
        "operable":                operable,
    }


# ── Formateador Discord ───────────────────────────────────────────────────────

async def formatear_wave_hunt(simbolo: str) -> str:
    r   = await evaluar_wave_hunt(simbolo)
    fib = r.get("fib", {})

    if r.get("error") and not fib:
        return f"❌ **Wave Hunt {simbolo}** — {r['error']}"

    if r.get("pendiente"):
        zt = fib.get("zona_techo", (0, 0))
        return (
            f"🌊 **Wave Hunt v2 — {simbolo}** ⏳ PENDIENTE\n"
            f"Activación: precio en ${zt[0]:,.0f}–${zt[1]:,.0f}\n"
            f"Wave 1: ${fib.get('wave1_high', 0):,.0f} → ${fib.get('wave1_low', 0):,.0f}\n"
            f"Fib 38.2% (SL): ${fib.get('fib_382', 0):,.0f}\n"
            f"Targets: ${fib.get('target_cons', 0):,.0f} / "
            f"${fib.get('target_std', 0):,.0f} / ${fib.get('target_agr', 0):,.0f}\n"
            + "\n".join([f"  {ra}" for ra in r["razones"][:3]])
        )

    score_pct  = r.get("score_pct", 0)
    estado     = "⚡ SEÑAL" if r.get("operable") else "🔍 EN ZONA"
    desc_str   = ""
    if r.get("indicadores_descartados"):
        desc_str = f"\n⚠️ Descartados: {', '.join(r['indicadores_descartados'])}"
    usados_str = ", ".join(r.get("indicadores_usados", [])) or "—"
    razones_str = "\n".join([f"  {ra}" for ra in r["razones"]])

    return (
        f"🌊 **Wave Hunt v2 — {simbolo}** {estado}\n"
        f"Score: {r.get('score_raw', 0)}/{r.get('score_max', 0)} ({score_pct}%) → "
        f"Leverage: {r.get('leverage', 2)}x\n"
        f"Dirección: {r.get('direccion') or 'Esperar'}\n"
        f"Indicadores usados: {usados_str}"
        f"{desc_str}\n"
        f"{razones_str}"
    )