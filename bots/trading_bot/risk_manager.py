"""
Risk Manager — gestión de riesgo, sizing y parámetros de trade.

v3.0 — Qty precision por símbolo · Exposure/margin separation ·
        Riesgo monetario real · Validaciones defensivas production-ready.

─── CONCEPTOS DE SIZING EN FUTURES ────────────────────────────────────────────

  margin (USDT):
    Capital propio que se deposita como garantía de la posición.
    Es el dinero que puedes perder si el precio toca el SL.
    Máx permitido: MARGIN_MAX_PCT (5%) del portfolio por trade.
    Ej: capital=75 USDT, score=90 → margin ≈ 3.75 USDT

  exposure (USDT):
    Tamaño nocional total de la posición en el mercado.
    exposure = margin × leverage
    Ej: margin=3.75, leverage=10 → exposure=37.50 USDT en el mercado
    La exposure determina cuánto ganas/pierdes por cada % de movimiento.

  qty (unidades del activo):
    Cantidad real de tokens/contratos que Bybit registra en la orden.
    qty = exposure / precio_entrada = (margin × leverage) / precio_entrada
    Ej: exposure=37.50 USDT, precio=2500 → qty=0.015 ETH
    ¡ESTO es lo que va en place_order(qty=...) — nunca el margin en USDT!

  ¿Por qué qty necesita precisión específica?
    Bybit define un "stepSize" mínimo por instrumento.
    BTCUSDT: stepSize=0.001 → qty=0.0123 → floor → 0.012 (válido)
    ETHUSDT: stepSize=0.01  → qty=0.015  → floor → 0.01  (válido)
    Si se envía qty con más decimales que el stepSize, Bybit retorna error 110040.
    Si se envía margin USDT como qty, Bybit retorna error 110007 (balance insuficiente).

  riesgo_monetario (USDT):
    Pérdida real en USDT si el precio llega al SL.
    riesgo = abs(entrada - sl) / entrada × exposure
           = abs(entrada - sl) × qty
    Permite verificar que el riesgo en USDT ≈ el margin comprometido.
────────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import math
import logging

log = logging.getLogger("risk_manager")

# ─── Configuración central ─────────────────────────────────────
MARGIN_MIN      = 10.0   # margen mínimo por trade en USDT
MAX_POR_TRADE   = 15.0   # hard cap de margen en USDT
CAPITAL_BASE    = 75.0   # capital total del bot
LEVERAGE_MAX    = 10     # hard cap absoluto de leverage
MARGIN_MAX_PCT  = 0.05   # máximo 5% del portfolio por trade

# ─── SL/TP fallback (independiente del score — fix H2) ─────────
_ATR_MULT_SL    = 1.5    # SL fallback = 1.5 × ATR del activo
_SL_PCT_PLANO   = 1.8    # % plano si no hay ATR disponible

# ─── Qty mínima aceptada por Bybit por símbolo ─────────────────
# Fuente: GET /v5/market/instruments-info (lotSizeFilter.minOrderQty)
# Actualizar si Bybit modifica los instrumentos.
QTY_MIN: dict[str, float] = {
    "BTCUSDT":  0.001,
    "ETHUSDT":  0.01,
    "SOLUSDT":  0.1,
    "BNBUSDT":  0.01,
    "AVAXUSDT": 0.1,
    # Acciones tokenizadas — perpetuos linear en Bybit (todas min/step 0.01)
    "AAPLUSDT":  0.01,
    "NVDAUSDT":  0.01,
    "TSLAUSDT":  0.01,
    "METAUSDT":  0.01,
    "MSFTUSDT":  0.01,
    "AMZNUSDT":  0.01,
    "GOOGLUSDT": 0.01,
}

# ─── Step size (incremento mínimo) por símbolo ─────────────────
# En la mayoría de los casos coincide con QTY_MIN, pero pueden diferir.
# Fuente: GET /v5/market/instruments-info (lotSizeFilter.qtyStep)
QTY_STEP: dict[str, float] = {
    "BTCUSDT":  0.001,
    "ETHUSDT":  0.01,
    "SOLUSDT":  0.1,
    "BNBUSDT":  0.01,
    "AVAXUSDT": 0.1,
    "AAPLUSDT":  0.01,
    "NVDAUSDT":  0.01,
    "TSLAUSDT":  0.01,
    "METAUSDT":  0.01,
    "MSFTUSDT":  0.01,
    "AMZNUSDT":  0.01,
    "GOOGLUSDT": 0.01,
}

# ─── Mapeo símbolo interno → símbolo Bybit ─────────────────────
_SYMBOL_MAP: dict[str, str] = {
    "BTC":  "BTCUSDT",
    "ETH":  "ETHUSDT",
    "SOL":  "SOLUSDT",
    "BNB":  "BNBUSDT",
    "AVAX": "AVAXUSDT",
    "AAPL":  "AAPLUSDT",
    "NVDA":  "NVDAUSDT",
    "TSLA":  "TSLAUSDT",
    "META":  "METAUSDT",
    "MSFT":  "MSFTUSDT",
    "AMZN":  "AMZNUSDT",
    "GOOGL": "GOOGLUSDT",
}

# ─── Qty máxima razonable por símbolo (anti-absurdo) ──────────
# Protege contra bugs donde qty se calcula con precio incorrecto.
QTY_MAX_SANITY: dict[str, float] = {
    "BTCUSDT":  0.5,      # 0.5 BTC ≈ 30k USDT a 60k
    "ETHUSDT":  5.0,      # 5 ETH ≈ 12.5k USDT a 2.5k
    "SOLUSDT":  50.0,
    "BNBUSDT":  20.0,
    "AVAXUSDT": 100.0,
    # Acciones: con margen ~10-15 USDT y precios de 3 dígitos, qty real es < 2.
    "AAPLUSDT":  5.0,
    "NVDAUSDT":  5.0,
    "TSLAUSDT":  5.0,
    "METAUSDT":  5.0,
    "MSFTUSDT":  5.0,
    "AMZNUSDT":  5.0,
    "GOOGLUSDT": 5.0,
}

# ─── Universo de acciones (perpetuos linear en Bybit) ──────────
# Mismo motor que la cripto, pero los datos vienen de Twelve Data y
# carecen de funding/OI/CVD on-chain.
STOCKS: set[str] = {"AAPL", "NVDA", "TSLA", "META", "MSFT", "AMZN", "GOOGL"}


def es_stock(par: str) -> bool:
    base = par.split("/")[0].upper() if "/" in par else par.upper()
    return base in STOCKS


# ══════════════════════════════════════════════════════════════════
# HELPERS DE SÍMBOLO
# ══════════════════════════════════════════════════════════════════

def resolver_symbol(par: str) -> str:
    """
    Convierte símbolo interno (ej: "ETH") a símbolo Bybit (ej: "ETHUSDT").
    Si ya viene en formato Bybit, lo devuelve igual.
    """
    base = par.split("/")[0].upper() if "/" in par else par.upper()
    return _SYMBOL_MAP.get(base, base + "USDT")


def get_qty_precision(symbol: str) -> int:
    """
    Devuelve los decimales válidos para qty de ese símbolo.

    ¿Por qué importa?
    Bybit rechaza con error 110040 cualquier qty que no sea múltiplo
    exacto del stepSize del instrumento. No es solo cosmético:
    0.0123 ETH es inválido si stepSize=0.01.

    Args:
        symbol: símbolo Bybit (ej: "ETHUSDT") o interno (ej: "ETH")

    Returns:
        número de decimales (ej: ETHUSDT → 2, BTCUSDT → 3)
    """
    sym = resolver_symbol(symbol) if symbol not in QTY_STEP else symbol
    step = QTY_STEP.get(sym)
    if step is None:
        log.warning(f"Step size no definido para {sym} — usando 6 decimales como fallback")
        return 6
    # Contar decimales del step: 0.001 → 3, 0.01 → 2, 0.1 → 1
    if "." in str(step):
        return len(str(step).rstrip("0").split(".")[-1])
    return 0


def round_qty_by_symbol(symbol: str, qty: float) -> float:
    """
    Redondea qty hacia abajo (floor) al stepSize válido para ese símbolo.

    Floor en lugar de round normal: siempre preferimos qty conservadora.
    round(0.0195, 2) = 0.02 → podría superar balance
    floor(0.0195, 2) = 0.01 → seguro

    Args:
        symbol: símbolo Bybit (ej: "ETHUSDT") o interno (ej: "ETH")
        qty:    cantidad calculada por calcular_qty()

    Returns:
        qty redondeada hacia abajo, 0.0 si el resultado es inválido.
    """
    sym  = resolver_symbol(symbol) if symbol not in QTY_STEP else symbol
    step = QTY_STEP.get(sym)

    if step is None:
        log.warning(f"Step size no definido para {sym} — redondeando a 6 decimales")
        return round(qty, 6)

    factor  = 1.0 / step
    floored = math.floor(qty * factor) / factor

    # Precisión fija para evitar floating-point drift: 0.01000000001 → 0.01
    decimals = get_qty_precision(sym)
    return round(floored, decimals)


# ══════════════════════════════════════════════════════════════════
# A) LEVERAGE — confluencia estricta, hard-cap 10x
# ══════════════════════════════════════════════════════════════════

def calcular_leverage(
    score:           int,
    confluencia:     int,
    leverage_lider:  int | None = None,
    prioridad:       str | None = None,
) -> int:
    """
    Devuelve el leverage final según CONFLUENCIA únicamente.

    CONGELADO (P1-6, 2026-07): el escalado por score y el leverage_lider
    (que derivaba del score de la estrategia) quedan suspendidos.
    Motivo: en los últimos 35 trades el score resultó ANTI-correlacionado
    con el resultado (WIN avg 69 vs LOSS avg 81) — escalar leverage con
    score amplificaba justamente las peores señales.
    `score` y `leverage_lider` se conservan en la firma por compatibilidad
    y para poder reactivar el escalado cuando el score vuelva a ser
    predictivo (validar con ≥30 trades).

    Reglas vigentes:
      confluencia >= 3 → 5x
      confluencia == 2 → 4x
      resto            → 3x

    Siempre aplica hard-cap de LEVERAGE_MAX (10x).
    """
    if prioridad == "TRIPLE_CONFLUENCIA" or confluencia >= 3:
        lev = 5
    elif prioridad == "DOBLE_CONFLUENCIA" or confluencia == 2:
        lev = 4
    else:
        lev = 3

    return min(lev, LEVERAGE_MAX)


# ══════════════════════════════════════════════════════════════════
# B) MARGIN / TAMAÑO DE POSICIÓN — máx 5% portfolio
# ══════════════════════════════════════════════════════════════════

def calcular_tamano_posicion(
    capital:     float,
    confianza:   int,
    confluencia: int = 1,
) -> float:
    """
    Margen (USDT) a arriesgar por trade. No es la exposure total.

    Reglas de sizing:
      confianza >= 85 y confluencia >= 2  → 5% del portfolio
      confianza >= 85                     → 4% del portfolio
      confianza >= 70                     → 3% del portfolio
      resto                               → 2% del portfolio

    Nunca supera MARGIN_MAX_PCT (5%) del capital.
    MAX_POR_TRADE actúa como hard-cap de seguridad adicional.

    Returns:
        margen en USDT (no qty, no exposure).
    """
    if capital <= 0:
        log.error(f"capital inválido: {capital}")
        return 0.0

    if confianza >= 85 and confluencia >= 2:
        pct = 0.05
    elif confianza >= 85:
        pct = 0.04
    elif confianza >= 70:
        pct = 0.03
    else:
        pct = 0.02

    # Con capital pequeño el % no escala — usar tabla fija por confluencia
    if confluencia >= 3:
        margen = 15.0
    elif confluencia >= 2:
        margen = 12.0
    else:
        margen = 10.0

    return min(margen, MAX_POR_TRADE)


# ══════════════════════════════════════════════════════════════════
# C) EXPOSURE — nocional total en el mercado
# ══════════════════════════════════════════════════════════════════

def calcular_exposure(margen: float, leverage: int | float) -> float:
    """
    Calcula la exposure nocional total de la posición.

    exposure = margen × leverage

    Diferencia clave con margin:
      - margin:   dinero tuyo depositado como garantía (lo que arriesgas)
      - exposure: dinero total que controlas en el mercado (incluyendo el
                  apalancamiento del exchange)

    Ej: margin=3.75 USDT, leverage=10 → exposure=37.50 USDT
    Un movimiento del 1% en contra representa 0.375 USDT de pérdida
    (1% × 37.50), no 0.0375 (1% × 3.75).

    Args:
        margen:   USDT de garantía (output de calcular_tamano_posicion)
        leverage: multiplicador ya cappado a LEVERAGE_MAX

    Returns:
        exposure en USDT.
    """
    if margen <= 0:
        log.warning(f"calcular_exposure: margen inválido ({margen})")
        return 0.0
    if leverage <= 0:
        log.warning(f"calcular_exposure: leverage inválido ({leverage})")
        return 0.0

    return round(margen * leverage, 4)


# ══════════════════════════════════════════════════════════════════
# D.0) AUTO-AJUSTE DE SIZING PARA SATISFACER QTY MÍNIMO
# ══════════════════════════════════════════════════════════════════

def ajustar_sizing_para_minimo(
    margen:         float,
    leverage:       int,
    precio_entrada: float,
    symbol:         str,
) -> tuple[float, int, str]:
    """
    Auto-ajusta margen y leverage para cumplir el qty mínimo del instrumento.

    Estrategia (en orden de preferencia):
        1. Mantener margen y leverage actuales si ya cumple
        2. Subir leverage hasta LEVERAGE_MAX (10x) si el actual es menor
        3. Si aún no alcanza, subir margen hasta MAX_POR_TRADE (15 USDT)
        4. Si tampoco alcanza, fallar con mensaje claro

    Esto resuelve el caso BTC: margen=10 × 4x / 77k = qty 0.0005 < 0.001 mínimo
        → Sube leverage a 8x: 10 × 8 / 77k = 0.001 OK
        → No necesita subir margen

    Args:
        margen:         margen base en USDT
        leverage:       leverage base
        precio_entrada: precio actual del activo
        symbol:         símbolo interno o Bybit

    Returns:
        (margen_final, leverage_final, motivo_ajuste)
        motivo_ajuste: explicación del cambio (vacío si no hubo cambio)
    """
    sym     = resolver_symbol(symbol) if symbol not in QTY_MIN else symbol
    qty_min = QTY_MIN.get(sym)

    if qty_min is None or precio_entrada <= 0:
        return margen, leverage, ""

    # Calcular qty actual
    exposure_actual = margen * leverage
    qty_actual      = exposure_actual / precio_entrada

    # Si ya cumple, no ajustar
    if qty_actual >= qty_min:
        return margen, leverage, ""

    # ── Paso 1: intentar subir leverage ────────────────────────
    # Exposure necesaria: qty_min * precio
    exposure_necesaria = qty_min * precio_entrada
    leverage_necesario = math.ceil(exposure_necesaria / margen)

    if leverage_necesario <= LEVERAGE_MAX:
        nuevo_lev = max(leverage_necesario, leverage)
        log.info(
            f"🔧 {sym} auto-ajuste: leverage {leverage}x → {nuevo_lev}x "
            f"(qty mínima {qty_min} requiere exposure ${exposure_necesaria:.2f})"
        )
        return margen, nuevo_lev, f"Leverage subido a {nuevo_lev}x para qty mínima"

    # ── Paso 2: leverage al máximo + subir margen ──────────────
    exposure_con_lev_max = MAX_POR_TRADE * LEVERAGE_MAX
    if exposure_con_lev_max >= exposure_necesaria:
        # Con margen máximo y leverage máximo alcanza
        margen_min_necesario = exposure_necesaria / LEVERAGE_MAX
        nuevo_margen = min(math.ceil(margen_min_necesario * 1.05), MAX_POR_TRADE)  # 5% buffer
        log.info(
            f"🔧 {sym} auto-ajuste: margen ${margen:.2f} → ${nuevo_margen:.2f} "
            f"+ leverage {leverage}x → {LEVERAGE_MAX}x"
        )
        return float(nuevo_margen), LEVERAGE_MAX, (
            f"Margen subido a ${nuevo_margen} + leverage {LEVERAGE_MAX}x"
        )

    # ── Paso 3: ni con max margen + max leverage alcanza ───────
    log.warning(
        f"⚠️ {sym} imposible alcanzar qty mínima con capital disponible: "
        f"max exposure ${exposure_con_lev_max:.2f} < necesaria ${exposure_necesaria:.2f}"
    )
    return margen, leverage, (
        f"Capital insuficiente: precio ${precio_entrada} requiere "
        f"${exposure_necesaria:.2f} exposure pero máximo es ${exposure_con_lev_max:.2f}"
    )


# ══════════════════════════════════════════════════════════════════
# D) CÁLCULO DE CANTIDAD (QTY) — production-hardened
# ══════════════════════════════════════════════════════════════════

def calcular_qty(
    margen:          float,
    leverage:        int | float,
    precio_entrada:  float,
    symbol:          str | None = None,
) -> float:
    """
    Calcula la cantidad REAL del activo para enviar a Bybit.

    Fórmula:
        exposure = margen × leverage
        qty      = exposure / precio_entrada

    Protecciones aplicadas:
        1. Validaciones defensivas de inputs
        2. Cap de leverage a LEVERAGE_MAX
        3. Floor al stepSize del símbolo (si se provee symbol)
        4. Validación de qty mínima (si se provee symbol)
        5. Sanity check contra qty absurda (si se provee symbol)

    Args:
        margen:          USDT de garantía (output de calcular_tamano_posicion)
        leverage:        multiplicador de apalancamiento
        precio_entrada:  precio actual del activo en USDT
        symbol:          símbolo interno ("ETH") o Bybit ("ETHUSDT"), opcional.
                         Si se provee, aplica precision y validaciones por símbolo.

    Returns:
        qty redondeada al stepSize del instrumento.

    Raises:
        ValueError: si algún input básico es inválido (precio<=0, margen<=0, etc.)
        RuntimeError: si qty calculada queda por debajo del mínimo del instrumento.
    """
    # ── Validaciones defensivas de inputs ─────────────────────
    if precio_entrada <= 0:
        raise ValueError(f"precio_entrada inválido: {precio_entrada} — debe ser > 0")
    if margen <= 0:
        raise ValueError(f"margen inválido: {margen} — debe ser > 0 USDT")
    if leverage <= 0:
        raise ValueError(f"leverage inválido: {leverage} — debe ser > 0")

    # ── Cap de leverage (defensivo, ya debería venir cappado) ──
    leverage_efectivo = leverage
    if leverage > LEVERAGE_MAX:
        log.warning(
            f"calcular_qty: leverage {leverage}x supera hard-cap {LEVERAGE_MAX}x "
            f"— cappado automáticamente"
        )
        leverage_efectivo = LEVERAGE_MAX

    # ── Fórmula base ───────────────────────────────────────────
    exposure = margen * leverage_efectivo
    qty_raw  = exposure / precio_entrada

    log.debug(
        f"calcular_qty: margen={margen} × lev={leverage_efectivo} "
        f"= exposure={exposure:.4f} / precio={precio_entrada} "
        f"= qty_raw={qty_raw:.8f}"
    )

    # ── Sin símbolo: redondeo genérico a 6 decimales ──────────
    if symbol is None:
        qty_final = round(qty_raw, 6)
        if qty_final <= 0:
            raise RuntimeError(
                f"qty calculada es 0 o negativa ({qty_raw:.8f}) — "
                "aumentar margen o reducir precio"
            )
        return qty_final

    # ── Con símbolo: precision + validaciones completas ───────
    sym = resolver_symbol(symbol)

    # Floor al stepSize
    qty_rounded = round_qty_by_symbol(sym, qty_raw)

    # Sanity check: qty absurdamente grande (bug de precio incorrecto)
    qty_max = QTY_MAX_SANITY.get(sym)
    if qty_max is not None and qty_rounded > qty_max:
        raise RuntimeError(
            f"qty={qty_rounded} {sym} supera el límite de sanity ({qty_max}) — "
            f"verificar precio_entrada={precio_entrada} (¿viene en USD, no en USDT?)"
        )

    # Validación de qty mínima del instrumento
    qty_min = QTY_MIN.get(sym)
    if qty_min is not None and qty_rounded < qty_min:
        raise RuntimeError(
            f"qty calculada ({qty_rounded} {sym}) menor al mínimo de Bybit ({qty_min}) — "
            f"margen={margen} USDT × {leverage_efectivo}x / precio={precio_entrada} "
            f"es insuficiente para abrir posición. "
            f"Aumentar margen, leverage o esperar precio más bajo."
        )

    if qty_rounded <= 0:
        raise RuntimeError(
            f"qty post-redondeo es 0 ({qty_raw:.8f} → {qty_rounded}) en {sym} — "
            "aumentar margen o leverage"
        )

    log.debug(f"calcular_qty: qty_raw={qty_raw:.8f} → qty_final={qty_rounded} {sym}")
    return qty_rounded


# ══════════════════════════════════════════════════════════════════
# E) RIESGO MONETARIO REAL
# ══════════════════════════════════════════════════════════════════

def riesgo_monetario(
    entrada: float,
    sl:      float,
    qty:     float,
) -> dict:
    """
    Calcula la pérdida real potencial en USDT si el precio toca el SL.

    Fórmula:
        distancia_sl = abs(entrada - sl)
        riesgo_usdt  = distancia_sl × qty

    ¿Por qué usarlo?
    calcular_tamano_posicion() define el margen máximo a arriesgar.
    Pero el riesgo real depende de dónde esté el SL relativo a la entrada.
    Si el SL está muy lejos → riesgo_usdt > margen → se está arriesgando más de lo planeado.
    Si el SL está muy cerca → riesgo_usdt < margen → posición infrautilizada.

    Un sizing correcto debería tener: riesgo_usdt ≈ margen.

    Args:
        entrada: precio de entrada de la posición
        sl:      precio del stop-loss
        qty:     cantidad del activo (output de calcular_qty)

    Returns:
        {
          "riesgo_usdt":    float,  # pérdida máxima en USDT
          "distancia_sl":   float,  # puntos entre entrada y SL
          "distancia_pct":  float,  # % de movimiento al SL
          "ok":             bool,   # True si los inputs son válidos
        }
    """
    if entrada <= 0 or sl <= 0 or qty <= 0:
        log.warning(
            f"riesgo_monetario: inputs inválidos "
            f"(entrada={entrada}, sl={sl}, qty={qty})"
        )
        return {"riesgo_usdt": 0.0, "distancia_sl": 0.0, "distancia_pct": 0.0, "ok": False}

    distancia_sl  = abs(entrada - sl)
    distancia_pct = round(distancia_sl / entrada * 100, 4)
    riesgo_usdt   = round(distancia_sl * qty, 4)

    return {
        "riesgo_usdt":   riesgo_usdt,
        "distancia_sl":  round(distancia_sl, 4),
        "distancia_pct": distancia_pct,
        "ok":            True,
    }


# ══════════════════════════════════════════════════════════════════
# F) SL / TP — con niveles técnicos opcionales
# ══════════════════════════════════════════════════════════════════

def calcular_atr(bars: list, periodo: int = 14) -> float | None:
    """
    Average True Range clásico sobre velas OHLCV.

    Se usa como fallback de SL sensible a la volatilidad de CADA activo:
    un 1.5% fijo es ruido en SOL y excesivo en BTC lateral. ATR adapta
    la distancia del stop al comportamiento real del instrumento.

    Args:
        bars:    lista de dicts {open, high, low, close} (ascendente)
        periodo: ventana del ATR (default 14)

    Returns:
        ATR en unidades de precio, o None si no hay datos suficientes.
    """
    if not bars or len(bars) < periodo + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[-periodo:]) / periodo
    return round(atr, 6) if atr > 0 else None


def calcular_sl_tp(
    precio:          float,
    direccion:       str,
    confianza:       int,
    # Niveles técnicos opcionales — si se pasan, sobreescriben el fallback
    entrada_tecnica: float | None = None,
    sl_tecnico:      float | None = None,
    tp1_tecnico:     float | None = None,
    tp2_tecnico:     float | None = None,
    tp3_tecnico:     float | None = None,
    atr:             float | None = None,
) -> dict:
    """
    Calcula SL y tres niveles de TP.

    Prioridad del SL:
        1. sl_tecnico (nivel estructural de la estrategia — ej. bajo el soporte)
        2. Fallback por ATR: 1.5 × ATR (volatilidad real del activo)
        3. Fallback plano: 1.8% (independiente del score)

    IMPORTANTE (fix H2): el score/confianza NO ajusta la distancia del SL.
    Antes, score alto → SL más ajustado → las mejores señales eran las más
    fáciles de barrer. El stop sale de la estructura o de la volatilidad;
    el score solo debe modular el tamaño de la posición.

    Prioridad de TPs: técnico si vino; si no, múltiplos del riesgo real
    (distancia entrada→SL): TP1 = 1.5R, TP2 = 2.0R, TP3 = 3.0R.

    TP parciales:
        TP1 (40% posición) → SL se mueve a breakeven (entrada)
        TP2 (35% posición) → SL se mueve a TP1
        TP3 (25% posición) → cierre total

    Compatibilidad:
        "tp"  → TP2 (igual que antes)
        "rr"  → R:R de TP2
    """
    # ── Entrada efectiva ────────────────────────────────────────
    entrada = entrada_tecnica if entrada_tecnica and entrada_tecnica > 0 else precio

    # ── Distancia de SL fallback (sin score): ATR → % plano ────
    # Clamp de sanidad: entre 0.6% y 4.0% de la entrada, para que un ATR
    # anómalo (datos corruptos, vela extrema) no genere stops absurdos.
    if atr and atr > 0 and entrada > 0:
        sl_dist = _ATR_MULT_SL * atr
        sl_dist = max(entrada * 0.006, min(sl_dist, entrada * 0.04))
    else:
        sl_dist = entrada * (_SL_PCT_PLANO / 100)

    # ── SL: técnico o fallback ──────────────────────────────────
    if direccion == "LONG":
        sl = sl_tecnico if sl_tecnico else entrada - sl_dist
    else:  # SHORT
        sl = sl_tecnico if sl_tecnico else entrada + sl_dist

    # ── TPs: técnicos o múltiplos del riesgo REAL (entrada→SL) ──
    # Derivarlos del stop efectivo garantiza R:R coherente aunque el SL
    # venga de estructura y los TPs de fallback (o viceversa).
    riesgo_dist = abs(entrada - sl)
    if direccion == "LONG":
        tp1 = tp1_tecnico if tp1_tecnico else entrada + riesgo_dist * 1.5
        tp2 = tp2_tecnico if tp2_tecnico else entrada + riesgo_dist * 2.0
        tp3 = tp3_tecnico if tp3_tecnico else entrada + riesgo_dist * 3.0
    else:  # SHORT
        tp1 = tp1_tecnico if tp1_tecnico else entrada - riesgo_dist * 1.5
        tp2 = tp2_tecnico if tp2_tecnico else entrada - riesgo_dist * 2.0
        tp3 = tp3_tecnico if tp3_tecnico else entrada - riesgo_dist * 3.0

    # ── SL dinámico tras cada TP ────────────────────────────────
    sl_tras_tp1 = entrada   # breakeven
    sl_tras_tp2 = tp1       # asegura ganancia de TP1

    # ── R:R efectivo (basado en distancia real, no en pct) ─────
    riesgo_real = abs(entrada - sl)

    def _rr(objetivo):
        return round(abs(objetivo - entrada) / riesgo_real, 2) if riesgo_real > 0 else 0.0

    rr_tp1 = _rr(tp1)
    rr_tp2 = _rr(tp2)
    rr_tp3 = _rr(tp3)

    # ── Pct reales para el formatter ───────────────────────────
    def _pct(nivel, base, es_corto=False):
        if es_corto:
            return round((base - nivel) / base * 100, 2)
        return round((nivel - base) / base * 100, 2)

    if direccion == "LONG":
        sl_pct_real  = round((entrada - sl)  / entrada * 100, 2)
        tp1_pct_real = _pct(tp1, entrada)
        tp2_pct_real = _pct(tp2, entrada)
        tp3_pct_real = _pct(tp3, entrada)
    else:
        sl_pct_real  = round((sl - entrada) / entrada * 100, 2)
        tp1_pct_real = _pct(tp1, entrada, es_corto=True)
        tp2_pct_real = _pct(tp2, entrada, es_corto=True)
        tp3_pct_real = _pct(tp3, entrada, es_corto=True)

    return {
        # ── Entrada efectiva ────────────────────────────────────
        "entrada":       round(entrada, 4),
        # ── Stop-loss ──────────────────────────────────────────
        "sl":            round(sl, 4),
        "sl_pct":        sl_pct_real,
        # ── TP1 — 40% de la posición ───────────────────────────
        "tp1":           round(tp1, 4),
        "tp1_pct":       tp1_pct_real,
        "tp1_size":      0.40,
        "sl_tras_tp1":   round(sl_tras_tp1, 4),
        # ── TP2 — 35% de la posición ───────────────────────────
        "tp2":           round(tp2, 4),
        "tp2_pct":       tp2_pct_real,
        "tp2_size":      0.35,
        "sl_tras_tp2":   round(sl_tras_tp2, 4),
        # ── TP3 — 25% restante ─────────────────────────────────
        "tp3":           round(tp3, 4),
        "tp3_pct":       tp3_pct_real,
        "tp3_size":      0.25,
        # ── R:R real por nivel ─────────────────────────────────
        "rr_tp1":        rr_tp1,
        "rr_tp2":        rr_tp2,
        "rr_tp3":        rr_tp3,
        # ── Flags de niveles técnicos ──────────────────────────
        "sl_tecnico":    sl_tecnico  is not None,
        "tp1_tecnico":   tp1_tecnico is not None,
        "tp2_tecnico":   tp2_tecnico is not None,
        "tp3_tecnico":   tp3_tecnico is not None,
        # ── Compatibilidad con código anterior ─────────────────
        "tp":            round(tp2, 4),
        "tp_pct":        tp2_pct_real,
        "rr":            rr_tp2,
    }


# ══════════════════════════════════════════════════════════════════
# G) SL VIGENTE SEGÚN FASE DEL TRADE
# ══════════════════════════════════════════════════════════════════

def sl_actual(trade: dict) -> float:
    """
    Devuelve el SL vigente según el estado del trade.
    Usa current_sl si está disponible (engine v2+), sino fallback por fase.

    fase_tp:  0 = inicial · 1 = tras TP1 · 2 = tras TP2
    """
    if "current_sl" in trade:
        return float(trade["current_sl"])

    riesgo = trade.get("riesgo", {})
    fase   = trade.get("fase_tp", 0)

    if fase >= 2:
        return riesgo.get("sl_tras_tp2") or riesgo.get("sl")
    elif fase >= 1:
        return riesgo.get("sl_tras_tp1") or riesgo.get("sl")
    return riesgo.get("sl")


# ══════════════════════════════════════════════════════════════════
# H) VALIDACIÓN DE RIESGO GLOBAL
# ══════════════════════════════════════════════════════════════════

def evaluar_riesgo_global(operaciones_abiertas: list) -> dict:
    """Máximo 2 operaciones abiertas simultáneas."""
    if len(operaciones_abiertas) >= 2:
        return {"puede_operar": False, "razon": "Máximo 2 operaciones abiertas"}
    return {"puede_operar": True, "razon": "OK"}


# ══════════════════════════════════════════════════════════════════
# I) FORMATTER PARA DISCORD
# ══════════════════════════════════════════════════════════════════

def formatear_riesgo(
    precio:     float,
    direccion:  str,
    riesgo:     dict,
    leverage:   int,
    margen:     float | None = None,
    qty:        float | None = None,
    symbol:     str | None = None,
) -> str:
    """
    Formatea los parámetros del trade para Discord.

    Args:
        precio:    precio actual / referencia
        direccion: "LONG" o "SHORT"
        riesgo:    dict devuelto por calcular_sl_tp()
        leverage:  leverage final (ya cappado)
        margen:    USDT de margen (opcional)
        qty:       cantidad calculada (opcional)
        symbol:    símbolo para mostrar exposure y riesgo monetario (opcional)
    """
    emoji   = "🟢" if direccion == "LONG" else "🔴"
    entrada = riesgo.get("entrada", precio)

    def _tag(key):
        return " 📐" if riesgo.get(key) else ""

    # Líneas opcionales de sizing
    margen_line    = f"💼 Margen: `${margen:.2f} USDT` (≤5% portfolio)\n"   if margen is not None else ""
    qty_line       = f"📦 Qty: `{qty} {symbol or ''}`\n"                    if qty    is not None else ""

    # Exposure y riesgo monetario si hay datos suficientes
    exposure_line  = ""
    riesgo_mon_line = ""

    if margen is not None and leverage:
        exposure = calcular_exposure(margen, leverage)
        exposure_line = f"📊 Exposure: `${exposure:.2f} USDT` ({leverage}× apalancado)\n"

    if qty is not None and entrada > 0:
        rm = riesgo_monetario(entrada, riesgo.get("sl", 0), qty)
        if rm["ok"]:
            riesgo_mon_line = (
                f"⚠️ Riesgo real: `${rm['riesgo_usdt']} USDT` "
                f"(-{rm['distancia_pct']}% al SL)\n"
            )

    return (
        f"{emoji} **{direccion}** · Leverage: `{leverage}×` · Hard-cap: `{LEVERAGE_MAX}×`\n"
        f"💰 Entrada: `${entrada:,.4f}`\n"
        f"{margen_line}"
        f"{exposure_line}"
        f"{qty_line}"
        f"{riesgo_mon_line}"
        f"🛡️ SL: `${riesgo['sl']:,.4f}` (-{riesgo['sl_pct']}%){_tag('sl_tecnico')}\n"
        f"🎯 TP1: `${riesgo['tp1']:,.4f}` (+{riesgo['tp1_pct']}%) → 40% · R:R `{riesgo['rr_tp1']}`{_tag('tp1_tecnico')}\n"
        f"   └ SL se mueve a breakeven `${riesgo['sl_tras_tp1']:,.4f}`\n"
        f"🎯 TP2: `${riesgo['tp2']:,.4f}` (+{riesgo['tp2_pct']}%) → 35% · R:R `{riesgo['rr_tp2']}`{_tag('tp2_tecnico')}\n"
        f"   └ SL se mueve a TP1 `${riesgo['sl_tras_tp2']:,.4f}`\n"
        f"🎯 TP3: `${riesgo['tp3']:,.4f}` (+{riesgo['tp3_pct']}%) → 25% · R:R `{riesgo['rr_tp3']}`{_tag('tp3_tecnico')}\n"
        f"   └ Cierre total de posición"
    )