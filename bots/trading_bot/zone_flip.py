"""
Zone Flip Strategy — datos desde data_provider (Binance/Bybit/CoinGecko).

Detección de zonas ESTRUCTURAL: swing highs/lows reales, clusters por toques,
zone flips (niveles que pasaron de S→R o R→S). No usa bandas matemáticas.

Retorna formato normalizado para el engine de confluencia:
  puntuacion, direccion, score, score_max, score_raw, score_pct,
  leverage, razones, indicadores_usados, indicadores_descartados,
  error, operable
"""
import logging

log = logging.getLogger("zone_flip")

# Peso máximo de cada indicador (sobre 100 puntos totales posibles)
_PESO = {
    "zona":    40,   # precio en zona estructural (proporcional a fuerza)
    "macd":    20,   # MACD confirma dirección
    "rsi":     15,   # RSI confirma momentum
    "rechazo": 15,   # vela de rechazo (solo SHORT)
    "rr":      10,   # R:R mínimo cumplido
}

# Validación profesional
MIN_INDICADORES    = 3                     # mínimo 3 de 5 indicadores
INDICADORES_CRITICOS = {"zona", "macd"}    # ambos obligatorios
SCORE_MINIMO_OPERACION = 70                # score mínimo 70%
RATIO_MINIMO_PUNTOS    = 0.70              # al menos 70% del peso_max confirmado

# Parámetros detección estructural
_SWING_LOOKBACK = 4      # velas 2H cada lado para confirmar swing (≈8h)
_CLUSTER_TOL    = 0.015  # 1.5% tolerancia para agrupar swings en una zona
_FLIP_TOL       = 0.020  # 2.0% tolerancia para detectar zone flip
_ZONE_BUFFER    = 0.012  # 1.2% buffer para considerar que el precio está EN la zona


# ── Respuesta vacía estandarizada ────────────────────────────

def _resultado_vacio(simbolo: str, error: str = None, razones: list = None) -> dict:
    return {
        "puntuacion":              0,
        "direccion":               None,
        "estrategia":              "zone_flip",
        "simbolo":                 simbolo,
        "score":                   0,
        "score_max":               0,
        "score_raw":               0,
        "score_pct":               0,
        "leverage":                2,
        "razones":                 razones or [],
        "indicadores_usados":      [],
        "indicadores_descartados": [],
        "zonas":                   {},
        "error":                   error,
        "operable":                False,
    }


# ── Leverage según score_pct ─────────────────────────────────

def _leverage_desde_score(score_pct: float) -> int:
    """
    Escala de leverage basada en score_pct normalizado.
    Siempre retorna entre 2 y 10.
    """
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


# ── Detección estructural de zonas ───────────────────────────

def _detectar_swing_points(bars: list) -> tuple[list, list]:
    """
    Swing high: mayor high que los _SWING_LOOKBACK barras a cada lado.
    Swing low:  menor low  que los _SWING_LOOKBACK barras a cada lado.
    """
    n = len(bars)
    lb = _SWING_LOOKBACK
    swing_highs, swing_lows = [], []

    for i in range(lb, n - lb):
        ventana_h = [bars[j]["high"] for j in range(i - lb, i + lb + 1)]
        ventana_l = [bars[j]["low"]  for j in range(i - lb, i + lb + 1)]
        if bars[i]["high"] == max(ventana_h):
            swing_highs.append({"precio": bars[i]["high"], "idx": i})
        if bars[i]["low"] == min(ventana_l):
            swing_lows.append({"precio": bars[i]["low"],  "idx": i})

    return swing_highs, swing_lows


def _agrupar_swings(puntos: list, precio_ref: float) -> list:
    """
    Agrupa swing points dentro de _CLUSTER_TOL en zonas.
    Retorna lista de zonas ordenadas por nivel.
    """
    if not puntos:
        return []

    zonas  = []
    usados = set()
    pts    = sorted(puntos, key=lambda x: x["precio"])

    for i, p in enumerate(pts):
        if i in usados:
            continue
        grupo = [p]
        usados.add(i)
        for j, q in enumerate(pts):
            if j not in usados and abs(p["precio"] - q["precio"]) / precio_ref <= _CLUSTER_TOL:
                grupo.append(q)
                usados.add(j)

        precios = [x["precio"] for x in grupo]
        zonas.append({
            "nivel":   round(sum(precios) / len(precios), 2),
            "inf":     round(min(precios), 2),
            "sup":     round(max(precios), 2),
            "touches": len(grupo),
            "idxs":    [x["idx"] for x in grupo],
        })

    return sorted(zonas, key=lambda z: z["nivel"])


def _calcular_zonas(bars: list, precio_actual: float) -> dict:
    if not bars or precio_actual is None:
        return {"error": "Sin datos OHLCV"}

    bars_rec = bars[-150:] if len(bars) >= 150 else bars

    swing_highs, swing_lows = _detectar_swing_points(bars_rec)

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return {"error": f"Insuficientes swing points (H:{len(swing_highs)} L:{len(swing_lows)}) — estructura indefinida"}

    zonas_h = _agrupar_swings(swing_highs, precio_actual)  # clusters de resistencia
    zonas_l = _agrupar_swings(swing_lows,  precio_actual)  # clusters de demanda

    # ── Zone Flip: zona que fue tanto high como low ───────────
    flips_bullish = []  # resistencia → soporte (long bias)
    flips_bearish = []  # soporte → resistencia (short bias)

    for zh in zonas_h:
        for zl in zonas_l:
            if abs(zh["nivel"] - zl["nivel"]) / precio_actual <= _FLIP_TOL:
                idx_h = max(zh["idxs"])
                idx_l = max(zl["idxs"])
                nivel_flip = round((zh["nivel"] + zl["nivel"]) / 2, 2)
                if idx_h > idx_l:
                    flips_bearish.append(nivel_flip)  # último toque como resistencia
                else:
                    flips_bullish.append(nivel_flip)  # último toque como soporte

    # ── Zonas más cercanas arriba y abajo del precio ──────────
    debajo = [z for z in zonas_l if z["nivel"] <= precio_actual]
    arriba = [z for z in zonas_h if z["nivel"] >= precio_actual]

    if not debajo or not arriba:
        return {"error": "No hay zonas estructurales definidas arriba y abajo del precio actual"}

    zona_demanda = max(debajo, key=lambda z: z["nivel"])  # la más cercana abajo
    zona_resist  = min(arriba, key=lambda z: z["nivel"])  # la más cercana arriba

    # ── ¿Precio está EN la zona? (dentro del buffer) ─────────
    en_demanda = (
        precio_actual >= zona_demanda["inf"] * (1 - _ZONE_BUFFER) and
        precio_actual <= zona_demanda["sup"] * (1 + _ZONE_BUFFER)
    )
    en_resistencia = (
        precio_actual >= zona_resist["inf"] * (1 - _ZONE_BUFFER) and
        precio_actual <= zona_resist["sup"] * (1 + _ZONE_BUFFER)
    )

    # ── ¿La zona activa es un flip? ───────────────────────────
    es_flip_demanda = any(
        abs(f - zona_demanda["nivel"]) / precio_actual <= _FLIP_TOL
        for f in flips_bullish
    )
    es_flip_resist = any(
        abs(f - zona_resist["nivel"]) / precio_actual <= _FLIP_TOL
        for f in flips_bearish
    )

    # ── Fuerza de la zona: touches + flip ────────────────────
    def _fuerza(zona: dict, es_flip: bool) -> float:
        # touches: 1→25% / 2→50% / 3→75% / 4+→100% del peso base (60%)
        base = min(zona["touches"] / 4, 1.0) * 0.60
        return round(min(base + (0.40 if es_flip else 0.0), 1.0), 2)

    fuerza_demanda = _fuerza(zona_demanda, es_flip_demanda)
    fuerza_resist  = _fuerza(zona_resist,  es_flip_resist)
    fuerza_zona    = fuerza_demanda if en_demanda else (fuerza_resist if en_resistencia else 0.0)

    # ── TP / SL desde zonas estructurales ────────────────────
    if en_demanda:
        tp1     = zona_resist["inf"]
        tp2     = zona_resist["sup"]
        sl_long = zona_demanda["inf"] * 0.99
        sl_short = zona_resist["sup"] * 1.01
        rr_tp1  = round((tp1 - precio_actual) / (precio_actual - sl_long),  2) if precio_actual > sl_long  else 0.0
        rr_tp2  = round((tp2 - precio_actual) / (precio_actual - sl_long),  2) if precio_actual > sl_long  else 0.0
    elif en_resistencia:
        tp1     = zona_demanda["sup"]
        tp2     = zona_demanda["inf"]
        sl_short = zona_resist["sup"] * 1.01
        sl_long  = zona_demanda["inf"] * 0.99
        rr_tp1  = round((precio_actual - tp1) / (sl_short - precio_actual), 2) if sl_short > precio_actual else 0.0
        rr_tp2  = round((precio_actual - tp2) / (sl_short - precio_actual), 2) if sl_short > precio_actual else 0.0
    else:
        tp1 = tp2 = 0.0
        sl_long  = zona_demanda["inf"] * 0.99
        sl_short = zona_resist["sup"]  * 1.01
        rr_tp1 = rr_tp2 = 0.0

    dist_demanda = round(((precio_actual - zona_demanda["sup"]) / precio_actual) * 100, 2)
    dist_resist  = round(((zona_resist["inf"] - precio_actual)  / precio_actual) * 100, 2)

    return {
        "precio_actual":         precio_actual,
        "zona_demanda":          (zona_demanda["inf"], zona_demanda["sup"]),
        "zona_resistencia":      (zona_resist["inf"],  zona_resist["sup"]),
        "zona_demanda_touches":  zona_demanda["touches"],
        "zona_resist_touches":   zona_resist["touches"],
        "es_flip_demanda":       es_flip_demanda,
        "es_flip_resistencia":   es_flip_resist,
        "fuerza_zona":           fuerza_zona,
        "en_demanda":            en_demanda,
        "en_resistencia":        en_resistencia,
        "tp1":                   round(tp1,      2),
        "tp2":                   round(tp2,      2),
        "sl_long":               round(sl_long,  2),
        "sl_short":              round(sl_short, 2),
        "rr_tp1":                rr_tp1,
        "rr_tp2":                rr_tp2,
        "dist_demanda_pct":      dist_demanda,
        "dist_resist_pct":       dist_resist,
        "n_swing_highs":         len(swing_highs),
        "n_swing_lows":          len(swing_lows),
        "flips_bullish":         flips_bullish,
        "flips_bearish":         flips_bearish,
    }


# ── Evaluación normalizada de indicadores ────────────────────

def _evaluar_indicadores(bars: list, precio_actual: float, rsi, macd: dict) -> dict:
    """
    Evalúa cada indicador de forma independiente.
    Si un indicador falla o no tiene datos, lo descarta y
    recalcula el score sobre el máximo posible sin él.

    Retorna un dict con toda la info para construir el resultado final.
    """
    usados      = []
    descartados = []
    razones     = []
    puntos_raw  = 0
    peso_max    = 0
    direccion   = None

    # ── 1. Zonas (indicador base — si falla, no hay señal) ────
    zonas = _calcular_zonas(bars, precio_actual)
    if "error" in zonas:
        # Sin zonas no hay nada que evaluar
        return {
            "ok":          False,
            "error":       zonas["error"],
            "usados":      [],
            "descartados": ["zona", "rsi", "macd", "rechazo", "rr"],
            "razones":     [f"❌ Zonas: {zonas['error']}"],
            "puntos_raw":  0,
            "peso_max":    0,
            "direccion":   None,
            "zonas":       {},
        }

    en_demanda     = zonas["en_demanda"]
    en_resistencia = zonas["en_resistencia"]

    fuerza_zona = zonas.get("fuerza_zona", 0.0)

    if en_demanda:
        direccion  = "LONG"
        pts_zona   = round(_PESO["zona"] * fuerza_zona, 1)
        puntos_raw += pts_zona
        peso_max   += _PESO["zona"]
        usados.append("zona")
        touches   = zonas.get("zona_demanda_touches", 1)
        flip_tag  = " · FLIP ✨" if zonas.get("es_flip_demanda") else ""
        razones.append(
            f"✅ Demanda ${zonas['zona_demanda'][0]:,.2f}–${zonas['zona_demanda'][1]:,.2f} "
            f"({touches} toques{flip_tag}, fuerza {fuerza_zona:.0%}) → {pts_zona}/{_PESO['zona']}pts"
        )
    elif en_resistencia:
        direccion  = "SHORT"
        pts_zona   = round(_PESO["zona"] * fuerza_zona, 1)
        puntos_raw += pts_zona
        peso_max   += _PESO["zona"]
        usados.append("zona")
        touches   = zonas.get("zona_resist_touches", 1)
        flip_tag  = " · FLIP ✨" if zonas.get("es_flip_resistencia") else ""
        razones.append(
            f"✅ Resistencia ${zonas['zona_resistencia'][0]:,.2f}–${zonas['zona_resistencia'][1]:,.2f} "
            f"({touches} toques{flip_tag}, fuerza {fuerza_zona:.0%}) → {pts_zona}/{_PESO['zona']}pts"
        )
    else:
        descartados.append("zona")
        dist_d = zonas.get("dist_demanda_pct", 0)
        dist_r = zonas.get("dist_resist_pct",  0)
        razones.append(
            f"⏳ Precio fuera de zonas estructurales — "
            f"a {dist_d:.1f}% de demanda | a {dist_r:.1f}% de resistencia"
        )
        return {
            "ok":          True,
            "error":       None,
            "usados":      [],
            "descartados": descartados,
            "razones":     razones,
            "puntos_raw":  0,
            "peso_max":    0,
            "direccion":   None,
            "zonas":       zonas,
        }

    # ── 2. MACD (scoring CONTINUO según fuerza del histograma) ──
    macd_valido = (
        macd and
        macd.get("histogram") is not None and
        macd.get("macd")      is not None and
        macd.get("signal")    is not None
    )
    if macd_valido:
        hist       = macd["histogram"]
        macd_val   = macd["macd"]
        signal_val = macd["signal"]
        peso_max  += _PESO["macd"]
        usados.append("macd")

        # Confirmación direccional (binario)
        confirma = (
            (direccion == "LONG" and hist > 0) or
            (direccion == "SHORT" and hist < 0)
        )

        if confirma:
            # 60% del peso por confirmar + 40% según fuerza relativa al precio
            # Fuerza máxima cuando hist >= 0.05% del precio
            base_pts = _PESO["macd"] * 0.60
            fuerza_pct = (abs(hist) / precio_actual * 100) if precio_actual > 0 else 0
            fuerza = min(fuerza_pct / 0.05, 1.0)   # cap a 1.0
            fuerza_pts = _PESO["macd"] * 0.40 * fuerza
            pts_macd = round(base_pts + fuerza_pts, 1)
            puntos_raw += pts_macd

            cruce = ""
            if direccion == "LONG" and macd_val > signal_val:
                cruce = " · cruce alcista"
            elif direccion == "SHORT" and macd_val < signal_val:
                cruce = " · cruce bajista"

            razones.append(
                f"✅ MACD {('positivo' if direccion=='LONG' else 'negativo')} "
                f"({hist:.3f}, fuerza {fuerza:.0%}){cruce} → {pts_macd}/{_PESO['macd']}pts"
            )
        else:
            razones.append(
                f"⚠️ MACD {('negativo' if direccion=='LONG' else 'positivo')} "
                f"({hist:.3f}) — no confirma {direccion}"
            )

    else:
        descartados.append("macd")
        razones.append("⚠️ MACD sin datos — descartado")

    # ── 3. RSI (scoring CONTINUO según cercanía al extremo) ────
    rsi_valido = rsi is not None and isinstance(rsi, (int, float))
    if rsi_valido:
        peso_max += _PESO["rsi"]
        usados.append("rsi")

        if direccion == "LONG":
            # Mejor cuanto MÁS bajo (sobreventa = mejor entrada LONG)
            # RSI 30 → fuerza 1.0 | RSI 50 → 0.5 | RSI 70+ → 0.0
            if rsi < 70:
                fuerza = max(0, min((70 - rsi) / 40, 1.0))
                pts_rsi = round(_PESO["rsi"] * fuerza, 1)
                puntos_raw += pts_rsi
                razones.append(
                    f"✅ RSI {rsi:.1f} (fuerza {fuerza:.0%}) → {pts_rsi}/{_PESO['rsi']}pts"
                )
            else:
                razones.append(f"⚠️ RSI sobrecompra ({rsi:.1f}) — riesgo LONG")
        elif direccion == "SHORT":
            # Mejor cuanto MÁS alto (sobrecompra = mejor entrada SHORT)
            # RSI 70 → fuerza 1.0 | RSI 50 → 0.5 | RSI 30- → 0.0
            if rsi > 30:
                fuerza = max(0, min((rsi - 30) / 40, 1.0))
                pts_rsi = round(_PESO["rsi"] * fuerza, 1)
                puntos_raw += pts_rsi
                razones.append(
                    f"✅ RSI {rsi:.1f} (fuerza {fuerza:.0%}) → {pts_rsi}/{_PESO['rsi']}pts"
                )
            else:
                razones.append(f"⚠️ RSI sobreventa ({rsi:.1f}) — riesgo SHORT")
    else:
        descartados.append("rsi")
        razones.append("⚠️ RSI sin datos — descartado")

    # ── 4. Vela de rechazo (solo SHORT) — CONTINUO ───────────
    # Ratio mecha/cuerpo: 0=sin mecha, 1.5=mínima, 3+=mecha grande
    if direccion == "SHORT":
        if bars and len(bars) >= 2:
            u         = bars[-1]
            cuerpo    = abs(u["close"] - u["open"])
            mecha_sup = max(u["high"] - max(u["close"], u["open"]), 0)
            es_bajista = u["close"] < u["open"]

            peso_max += _PESO["rechazo"]
            usados.append("rechazo")

            if cuerpo > 0 and es_bajista:
                ratio = mecha_sup / cuerpo
                # ratio 1.5 → fuerza 0.0 | ratio 3.0 → fuerza 1.0
                fuerza = max(0, min((ratio - 1.5) / 1.5, 1.0))
                pts_rechazo = round(_PESO["rechazo"] * fuerza, 1)
                puntos_raw += pts_rechazo
                razones.append(
                    f"{'✅' if fuerza > 0.5 else '⚠️'} Vela rechazo "
                    f"(mecha/cuerpo {ratio:.2f}, fuerza {fuerza:.0%}) → "
                    f"{pts_rechazo}/{_PESO['rechazo']}pts"
                )
            else:
                razones.append("⚠️ Sin vela de rechazo válida — 0 pts")
        else:
            descartados.append("rechazo")
            razones.append("⚠️ Sin barras para evaluar rechazo — descartado")

    # ── 5. R:R — CONTINUO según calidad del ratio ────────────
    rr_tp1 = zonas.get("rr_tp1", 0)
    rr_tp2 = zonas.get("rr_tp2", 0)
    rr_evalable = precio_actual is not None

    if rr_evalable:
        peso_max += _PESO["rr"]
        usados.append("rr")
        # R:R TP1: 1.0 → fuerza 0 | 1.8 → 0.4 | 3.0 → 1.0 (cap)
        if rr_tp1 >= 1.0:
            fuerza = max(0, min((rr_tp1 - 1.0) / 2.0, 1.0))
            pts_rr = round(_PESO["rr"] * fuerza, 1)
            puntos_raw += pts_rr
            extra_tp2 = f" · TP2 R:R {rr_tp2:.2f}" if rr_tp2 > 0 else ""
            razones.append(
                f"{'✅' if fuerza >= 0.4 else '⚠️'} R:R TP1 = {rr_tp1:.2f} "
                f"(fuerza {fuerza:.0%}){extra_tp2} → {pts_rr}/{_PESO['rr']}pts"
            )
        else:
            razones.append(f"⚠️ R:R TP1 = {rr_tp1:.2f} < 1.0 — 0 pts")
    else:
        descartados.append("rr")
        razones.append("⚠️ R:R no calculable — descartado")

    return {
        "ok":          True,
        "error":       None,
        "usados":      usados,
        "descartados": descartados,
        "razones":     razones,
        "puntos_raw":  puntos_raw,
        "peso_max":    peso_max,
        "direccion":   direccion,
        "zonas":       zonas,
    }


# ── Lógica de confluencia y score final ──────────────────────

def _construir_resultado(simbolo: str, eval_: dict) -> dict:
    """
    Toma la evaluación cruda y construye el resultado normalizado final.
    Validación profesional: indicadores críticos + mínimo + ratio.
    """
    usados      = eval_["usados"]
    descartados = eval_["descartados"]
    razones     = eval_["razones"]
    puntos_raw  = eval_["puntos_raw"]
    peso_max    = eval_["peso_max"]
    direccion   = eval_["direccion"]
    zonas       = eval_["zonas"]

    # ── 1. Validar indicadores críticos (OBLIGATORIOS) ──────────
    criticos_presentes = INDICADORES_CRITICOS & set(usados)
    if len(criticos_presentes) < len(INDICADORES_CRITICOS):
        faltantes = INDICADORES_CRITICOS - criticos_presentes
        razones.append(
            f"❌ Faltan indicadores críticos: {', '.join(faltantes)} — no operable"
        )
        return {
            "puntuacion":              0,
            "direccion":               None,
            "estrategia":              "zone_flip",
            "simbolo":                 simbolo,
            "score":                   0,
            "score_max":               peso_max,
            "score_raw":               puntos_raw,
            "score_pct":               0,
            "leverage":                2,
            "razones":                 razones,
            "indicadores_usados":      usados,
            "indicadores_descartados": descartados,
            "zonas":                   zonas,
            "error":                   None,
            "operable":                False,
        }

    # ── 2. Mínimo de indicadores (3+) ────────────────────────────
    if len(usados) < MIN_INDICADORES:
        razones.append(
            f"❌ Solo {len(usados)} indicador/es (mínimo {MIN_INDICADORES}) — no operable"
        )
        return {
            "puntuacion":              0,
            "direccion":               None,
            "estrategia":              "zone_flip",
            "simbolo":                 simbolo,
            "score":                   0,
            "score_max":               peso_max,
            "score_raw":               puntos_raw,
            "score_pct":               0,
            "leverage":                2,
            "razones":                 razones,
            "indicadores_usados":      usados,
            "indicadores_descartados": descartados,
            "zonas":                   zonas,
            "error":                   None,
            "operable":                False,
        }

    # ── 3. Normalizar score sobre el máximo disponible ──────────
    score_pct = round((puntos_raw / peso_max) * 100, 1) if peso_max > 0 else 0
    leverage  = _leverage_desde_score(score_pct)

    # ── 4. Validar ratio mínimo de puntos (70% del peso_max) ────
    ratio_puntos = (puntos_raw / peso_max) if peso_max > 0 else 0
    if ratio_puntos < RATIO_MINIMO_PUNTOS:
        razones.append(
            f"⚠️ Score {score_pct}% pero solo {ratio_puntos:.0%} de confirmación — no operable"
        )
        return {
            "puntuacion":              0,
            "direccion":               None,
            "estrategia":              "zone_flip",
            "simbolo":                 simbolo,
            "score":                   0,
            "score_max":               peso_max,
            "score_raw":               puntos_raw,
            "score_pct":               score_pct,
            "leverage":                2,
            "razones":                 razones,
            "indicadores_usados":      usados,
            "indicadores_descartados": descartados,
            "zonas":                   zonas,
            "error":                   None,
            "operable":                False,
        }

    # ── 5. Score mínimo absoluto (70%, no 40%) ───────────────────
    if score_pct < SCORE_MINIMO_OPERACION:
        direccion = None
        razones.append(f"⚠️ Score {score_pct}% < {SCORE_MINIMO_OPERACION}% — sin señal")

    operable = direccion is not None and score_pct >= SCORE_MINIMO_OPERACION

    return {
        "puntuacion":              int(score_pct),
        "direccion":               direccion,
        "estrategia":              "zone_flip",
        "simbolo":                 simbolo,
        "score":                   int(score_pct),
        "score_max":               peso_max,
        "score_raw":               puntos_raw,
        "score_pct":               score_pct,
        "leverage":                min(leverage, 10),
        "razones":                 razones,
        "indicadores_usados":      usados,
        "indicadores_descartados": descartados,
        "zonas":                   zonas,
        "error":                   None,
        "operable":                operable,
    }


# ── API pública (async) ───────────────────────────────────────

async def evaluar_zone_flip(simbolo: str) -> dict:
    """
    Evalúa Zone Flip para un activo.
    Retorna formato normalizado compatible con el engine de confluencia.

    Fuentes:
      2H (200 barras = 400h = ~16 días) → zonas estructurales reales de demanda/resistencia
      2H (200 barras) → RSI y MACD en timeframe de swing
      precio live     → posición exacta dentro de la zona
    """
    try:
        import asyncio as _asyncio
        from bots import data_provider as dp
        bars_2h, ind_2h, precio_live = await _asyncio.gather(
            dp.get_ohlcv(simbolo, "2h", 200),
            dp.get_indicadores(simbolo, "2h", 200),
            dp.get_precio(simbolo),
        )
        bars          = bars_2h                        # zonas desde 2H (≈16 días)
        precio_actual = precio_live or ind_2h["precio"]
        rsi           = ind_2h["rsi"]
        macd          = ind_2h["macd"] or {}
    except Exception as e:
        log.error(f"Zone Flip data error {simbolo}: {e}")
        return _resultado_vacio(simbolo, error=str(e), razones=[f"❌ Error datos: {e}"])

    if not bars:
        return _resultado_vacio(
            simbolo,
            error="Sin barras OHLCV",
            razones=["❌ Sin barras OHLCV"],
        )
    if precio_actual is None:
        return _resultado_vacio(
            simbolo,
            error="Sin precio actual",
            razones=["❌ Sin precio actual"],
        )

    # Evaluar indicadores (con descarte automático)
    eval_ = _evaluar_indicadores(bars, precio_actual, rsi, macd)

    if not eval_["ok"]:
        return _resultado_vacio(
            simbolo,
            error=eval_["error"],
            razones=eval_["razones"],
        )

    return _construir_resultado(simbolo, eval_)


async def formatear_zone_flip(simbolo: str) -> str:
    """Formatea el resultado de Zone Flip para mostrar en Discord."""
    r      = await evaluar_zone_flip(simbolo)
    z      = r.get("zonas", {})
    fuente = "📡 Zonas 2H · RSI/MACD 2H"

    if r.get("error") and not z:
        return f"❌ **Zone Flip {simbolo}** — {r['error']}"

    score_pct = r.get("score_pct", 0)
    estado    = "⚡ SEÑAL" if r.get("operable") else "⏳ SIN SEÑAL"

    desc_str = ""
    if r.get("indicadores_descartados"):
        desc_str = f"\n⚠️ Descartados: {', '.join(r['indicadores_descartados'])}"

    usados_str = ", ".join(r.get("indicadores_usados", [])) or "—"

    zonas_str = ""
    if z and "zona_demanda" in z:
        flip_d = " · FLIP ✨" if z.get("es_flip_demanda")      else f" · {z.get('zona_demanda_touches',1)} toques"
        flip_r = " · FLIP ✨" if z.get("es_flip_resistencia")  else f" · {z.get('zona_resist_touches', 1)} toques"
        swings_str = f"Swings detectados: {z.get('n_swing_highs',0)} highs / {z.get('n_swing_lows',0)} lows\n"
        flips_str  = ""
        if z.get("flips_bullish") or z.get("flips_bearish"):
            fb = [f"${v:,.2f}" for v in z.get("flips_bullish", [])]
            fr = [f"${v:,.2f}" for v in z.get("flips_bearish", [])]
            flips_str = f"Flips bullish: {', '.join(fb) or '—'} | Flips bearish: {', '.join(fr) or '—'}\n"
        zonas_str = (
            f"{swings_str}"
            f"{flips_str}"
            f"🟢 Demanda: ${z['zona_demanda'][0]:,.2f} – ${z['zona_demanda'][1]:,.2f}{flip_d}\n"
            f"🔴 Resistencia: ${z['zona_resistencia'][0]:,.2f} – ${z['zona_resistencia'][1]:,.2f}{flip_r}\n"
            f"TP1: ${z['tp1']:,.2f} | TP2: ${z['tp2']:,.2f}\n"
            f"SL LONG: ${z['sl_long']:,.2f} | SL SHORT: ${z['sl_short']:,.2f}\n"
            f"R:R TP1: {z.get('rr_tp1', 0)} | R:R TP2: {z.get('rr_tp2', 0)}\n"
        )
    precio_str = f"Precio actual: ${z.get('precio_actual', 0):,.2f}\n" if z else ""

    return (
        f"🎯 **Zone Flip — {simbolo}** {estado} {fuente}\n"
        f"{precio_str}"
        f"{zonas_str}"
        f"Score: {score_pct}% (raw {r.get('score_raw', 0)}/{r.get('score_max', 0)})\n"
        f"Leverage: {r.get('leverage', 2)}x | Dirección: {r.get('direccion') or 'Esperar'}\n"
        f"Indicadores usados: {usados_str}"
        f"{desc_str}\n"
        f"Señales: {' · '.join(r.get('razones', [])[:3])}"
    )