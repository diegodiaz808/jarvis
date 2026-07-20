"""
Bybit API Client — apertura, gestión y cierre de posiciones.

v3.0 — Leverage explícito · qty real · Margin/qty separation · Paper trading completo.

─── CONCEPTOS CLAVE ────────────────────────────────────────────────────────────
  margin (USDT): capital propio que se compromete como garantía.
                 Calculado en risk_manager.py con calcular_tamano_posicion().
                 Ej: 3.0 USDT de tu cuenta.

  qty (activo):  unidades REALES del activo que Bybit necesita para la orden.
                 Calculado en risk_manager.py con calcular_qty().
                 Ej: 0.012 ETH  ← esto va en place_order(qty=...)

  ¡NUNCA enviar margin como qty!
  Si se envía qty=3.0 en ETHUSDT, Bybit lo interpreta como 3 ETH ≈ 7500 USDT
  y lanza ErrCode 110007 (Insufficient balance).

  leverage: debe setearse EXPLÍCITAMENTE antes de place_order().
  Bybit no hereda el leverage de configuraciones anteriores en cada orden nueva.
  Si se omite set_leverage(), Bybit usará el leverage previo de esa cuenta/símbolo,
  que puede ser 1x por defecto → posición mucho más pequeña de lo esperado.
────────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import os
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("bybit")

# ─── Mapeo de símbolos ─────────────────────────────────────────
BYBIT_SYMBOLS: dict[str, str] = {
    "BTC":  "BTCUSDT",
    "ETH":  "ETHUSDT",
    "SOL":  "SOLUSDT",
    "BNB":  "BNBUSDT",
    "AVAX": "AVAXUSDT",
    # Acciones tokenizadas (perpetuos linear)
    "AAPL":  "AAPLUSDT",
    "NVDA":  "NVDAUSDT",
    "TSLA":  "TSLAUSDT",
    "META":  "METAUSDT",
    "MSFT":  "MSFTUSDT",
    "AMZN":  "AMZNUSDT",
    "GOOGL": "GOOGLUSDT",
}

# Activos no disponibles en Bybit
NO_DISPONIBLES: set[str] = {"SPX", "NDX", "GOLD"}

# Step sizes mínimos por símbolo (qty mínima aceptada por Bybit)
# Actualizar si Bybit cambia los instrumentos.
QTY_STEP: dict[str, float] = {
    "BTCUSDT":  0.001,
    "ETHUSDT":  0.01,
    "SOLUSDT":  0.1,
    "BNBUSDT":  0.01,
    "AVAXUSDT": 0.1,
    # Acciones tokenizadas (perpetuos linear)
    "AAPLUSDT":  0.01,
    "NVDAUSDT":  0.01,
    "TSLAUSDT":  0.01,
    "METAUSDT":  0.01,
    "MSFTUSDT":  0.01,
    "AMZNUSDT":  0.01,
    "GOOGLUSDT": 0.01,
}


# ══════════════════════════════════════════════════════════════════
# HELPERS INTERNOS
# ══════════════════════════════════════════════════════════════════

def _symbol(par: str) -> str:
    """Convierte símbolo interno → símbolo Bybit perpetuo."""
    base = par.split("/")[0] if "/" in par else par
    return BYBIT_SYMBOLS.get(base.upper(), base.upper() + "USDT")


def _base(par: str) -> str:
    """Extrae el símbolo base (sin slash)."""
    return (par.split("/")[0] if "/" in par else par).upper()


def bybit_disponible() -> bool:
    """True si hay API keys configuradas."""
    return bool(os.getenv("BYBIT_API_KEY") and os.getenv("BYBIT_API_SECRET"))


def _is_paper() -> bool:
    return os.getenv("PAPER_TRADING", "true").lower() == "true"


def _get_session(paper: bool | None = None):
    from pybit.unified_trading import HTTP
    paper = _is_paper() if paper is None else paper
    return HTTP(
        testnet=paper,
        api_key=os.getenv("BYBIT_API_KEY", ""),
        api_secret=os.getenv("BYBIT_API_SECRET", ""),
    )


def _check_disponible(par: str) -> str | None:
    """
    Verifica que el activo esté disponible en Bybit.
    Devuelve el motivo de rechazo o None si está OK.
    """
    if _base(par) in NO_DISPONIBLES:
        return f"{par} no disponible en Bybit"
    return None


def _handle_error_code(ret_code: int, ret_msg: str, symbol: str) -> str:
    """
    Traduce códigos de error de Bybit a mensajes accionables.
    Facilita el diagnóstico sin tener que buscar la documentación.
    """
    error_map = {
        110007: (
            f"[110007] Insufficient margin / balance en {symbol}. "
            "Verificar: (1) qty enviada en unidades del activo, NO en USDT. "
            "(2) Balance USDT suficiente para cubrir margin + fees. "
            "(3) Leverage configurado correctamente antes de la orden."
        ),
        110001: f"[110001] Order not found — {symbol}",
        110012: f"[110012] Insufficient available balance — {symbol}",
        110013: f"[110013] Risk limit exceeded — reducir qty o leverage",
        110021: f"[110021] Position mode error — verificar One-Way vs Hedge mode",
        110025: f"[110025] Position not found para {symbol}",
        110040: f"[110040] Order qty inferior al mínimo del instrumento",
        110043: f"[110043] Set leverage no permitido con posición abierta",
    }
    return error_map.get(ret_code, f"[{ret_code}] {ret_msg}")


# ══════════════════════════════════════════════════════════════════
# HELPER: REDONDEO DE QTY POR STEP SIZE
# ══════════════════════════════════════════════════════════════════

def round_qty_by_symbol(qty: float, symbol: str) -> float:
    """
    Redondea qty al step size mínimo aceptado por Bybit para ese símbolo.

    Bybit rechaza órdenes cuya qty no sea múltiplo del stepSize del instrumento.
    Ej: BTCUSDT stepSize=0.001 → qty=0.0123 → round → 0.012

    Args:
        qty:    cantidad calculada por calcular_qty()
        symbol: símbolo Bybit (ej: "ETHUSDT")

    Returns:
        qty redondeada hacia abajo al step válido más cercano.

    Nota: si el símbolo no está en QTY_STEP, retorna qty con 6 decimales.
    Para mayor precisión, consultar GET /v5/market/instruments-info.
    """
    step = QTY_STEP.get(symbol)
    if step is None:
        log.warning(f"Step size no configurado para {symbol} — usando 6 decimales")
        return round(qty, 6)

    # Floor al múltiplo de step más cercano
    import math
    factor = 1 / step
    rounded = math.floor(qty * factor) / factor

    # Redondear a los decimales del step para evitar floating-point drift
    decimals = len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0
    return round(rounded, decimals)


# ══════════════════════════════════════════════════════════════════
# TEST DE CONEXIÓN
# ══════════════════════════════════════════════════════════════════

def test_conexion() -> dict:
    """
    Verifica que las keys funcionan consultando el balance USDT.
    Llamar con !test_bybit en Discord.
    """
    if not bybit_disponible():
        return {"ok": False, "error": "Sin API keys"}

    try:
        paper   = _is_paper()
        session = _get_session(paper)
        r       = session.get_wallet_balance(accountType="UNIFIED")
        ret_code = r.get("retCode", -1)

        if ret_code == 0:
            coins   = r["result"]["list"][0]["coin"] if r["result"]["list"] else []
            usdt    = next((c for c in coins if c["coin"] == "USDT"), {})
            balance = usdt.get("walletBalance", "?")
            modo    = "PAPER" if paper else "REAL"
            log.info(f"✅ Bybit {modo} conectado — Balance USDT: {balance}")
            return {"ok": True, "balance": balance, "modo": modo, "ret_code": ret_code}
        else:
            log.error(f"Bybit test falló — retCode: {ret_code} | {r.get('retMsg')}")
            return {"ok": False, "ret_code": ret_code, "error": r.get("retMsg")}

    except Exception as e:
        log.error(f"Bybit test exception: {e}")
        return {"ok": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════
# SET MARGIN MODE — ISOLATED (interno)
# ══════════════════════════════════════════════════════════════════

def _set_isolated_margin(session, symbol: str, leverage: int) -> tuple[bool, str]:
    """
    Cambia el modo de margen a ISOLATED antes de abrir la orden.
    En modo aislado, cada posición tiene su propio margen separado:
    si el SL se toca, solo se pierde el margen de esa posición,
    no se toca el resto de la cuenta (a diferencia de cross margin).

    tradeMode=1 → ISOLATED
    tradeMode=0 → CROSS

    Errores no fatales:
    - 110026: ya estaba en isolated → OK, seguir.
    - 110043: posición abierta, no se puede cambiar → OK, seguir.
    """
    try:
        r = session.switch_margin_mode(
            category="linear",
            symbol=symbol,
            tradeMode=1,
            buyLeverage=str(leverage),
            sellLeverage=str(leverage),
        )
        ret_code = r.get("retCode", -1)
        ret_msg  = r.get("retMsg", "")

        if ret_code == 0:
            log.info(f"✅ Margin mode: ISOLATED — {symbol}")
            return True, "Isolated OK"

        if ret_code in (110026, 110043):
            log.info(f"ℹ️ Isolated {symbol}: ya configurado (retCode {ret_code})")
            return True, "Isolated ya configurado"

        log.debug(f"switch_isolated_margin {symbol}: retCode {ret_code} | {ret_msg} — continuando")
        return True, f"Isolated no confirmado ({ret_code})"

    except Exception as e:
        log.debug(f"switch_isolated_margin excepción en {symbol}: {e} — continuando")
        return True, str(e)


# ══════════════════════════════════════════════════════════════════
# SET LEVERAGE (interno)
# ══════════════════════════════════════════════════════════════════

def _set_leverage(session, symbol: str, leverage: int) -> tuple[bool, str]:
    """
    Setea el leverage explícitamente antes de abrir una orden.

    ¿Por qué es obligatorio?
    Bybit Unified Trading API NO aplica el leverage de la señal automáticamente.
    Si se omite set_leverage(), la cuenta puede tener 1x configurado de una
    sesión anterior → qty correcta pero posición notional incorrecta → R:R roto.

    Errores no fatales:
    - ErrCode 110043: posición ya abierta con ese leverage → se ignora y sigue.
    - ErrCode 0: éxito.

    Returns:
        (ok: bool, mensaje: str)
    """
    try:
        r = session.set_leverage(
            category="linear",
            symbol=symbol,
            buyLeverage=str(leverage),
            sellLeverage=str(leverage),
        )
        ret_code = r.get("retCode", -1)
        ret_msg  = r.get("retMsg", "")

        if ret_code == 0:
            log.info(f"✅ Leverage seteado: {symbol} → {leverage}x")
            return True, f"Leverage {leverage}x OK"

        # 110043: leverage ya estaba configurado o hay posición abierta → no bloqueante
        if ret_code == 110043:
            log.info(f"ℹ️ Leverage {leverage}x en {symbol}: ya configurado — continuando")
            return True, f"Leverage {leverage}x ya configurado"

        # Cualquier otro error es bloqueante
        msg = _handle_error_code(ret_code, ret_msg, symbol)
        log.error(f"❌ set_leverage falló en {symbol}: {msg}")
        return False, msg

    except Exception as e:
        # pybit lanza excepción directa para algunos códigos (ej: 110043)
        if "110043" in str(e):
            log.info(f"ℹ️ Leverage {leverage}x en {symbol}: ya configurado — continuando")
            return True, "Leverage ya configurado"
        log.error(f"❌ set_leverage excepción en {symbol}: {e}")
        return False, str(e)


# ══════════════════════════════════════════════════════════════════
# VALIDACIONES DEFENSIVAS
# ══════════════════════════════════════════════════════════════════

def _validar_parametros_orden(
    qty: float,
    leverage: int,
    sl: float,
    tp: float,
    direccion: str,
    precio_ref: float | None = None,
) -> tuple[bool, str]:
    """
    Valida los parámetros de la orden antes de llamar a Bybit.
    Evita enviar órdenes malformadas que generan errores 110007/110040.

    Args:
        qty:       unidades del activo (NO margen USDT)
        leverage:  multiplicador de apalancamiento
        sl:        precio de stop-loss
        tp:        precio de take-profit
        direccion: "LONG" o "SHORT"
        precio_ref: precio actual de referencia (opcional, para validar SL/TP)

    Returns:
        (válido: bool, mensaje: str)
    """
    if qty <= 0:
        return False, f"qty inválida: {qty} — debe ser > 0 unidades del activo"
    if leverage <= 0:
        return False, f"leverage inválido: {leverage} — debe ser > 0"
    if sl <= 0:
        return False, f"SL inválido: {sl}"
    if tp <= 0:
        return False, f"TP inválido: {tp}"
    if direccion not in ("LONG", "SHORT"):
        return False, f"Dirección inválida: {direccion}"

    # Validar coherencia SL/TP con dirección si hay precio de referencia
    if precio_ref and precio_ref > 0:
        if direccion == "LONG":
            if sl >= precio_ref:
                return False, f"SL ({sl}) ≥ precio ({precio_ref}) en LONG — SL debe estar por debajo"
            if tp <= precio_ref:
                return False, f"TP ({tp}) ≤ precio ({precio_ref}) en LONG — TP debe estar por encima"
        else:  # SHORT
            if sl <= precio_ref:
                return False, f"SL ({sl}) ≤ precio ({precio_ref}) en SHORT — SL debe estar por encima"
            if tp >= precio_ref:
                return False, f"TP ({tp}) ≥ precio ({precio_ref}) en SHORT — TP debe estar por debajo"

    return True, "OK"


# ══════════════════════════════════════════════════════════════════
# ABRIR ORDEN
# ══════════════════════════════════════════════════════════════════

def ejecutar_orden(
    par:       str,
    direccion: str,
    qty:       float,
    sl:        float,
    tp:        float,
    leverage:  int = 2,
    precio_ref: float | None = None,
) -> dict:
    """
    Abre una orden de mercado con leverage explícito, SL y TP.

    Flujo interno:
        1. Validar parámetros defensivamente
        2. set_leverage() → garantizar leverage correcto antes de la orden
        3. place_order(qty=qty_real) → qty en unidades del activo, NO en USDT

    Args:
        par:        símbolo interno (ej: "ETH", "BTC")
        direccion:  "LONG" o "SHORT"
        qty:        cantidad REAL del activo calculada por calcular_qty()
                    Ej: 0.012 ETH — NO margen en USDT
        sl:         precio de stop-loss
        tp:         precio de take-profit
        leverage:   apalancamiento de la señal (default: 2)
        precio_ref: precio actual de referencia para validar SL/TP (opcional)

    Returns:
        dict con ok / simulado / estado / order_id / qty / leverage / symbol

    Errores comunes y su causa:
        110007 → qty enviada como USDT en lugar de unidades del activo
        110040 → qty menor al mínimo del instrumento
        110013 → leverage × qty excede el risk limit de la cuenta
    """
    paper  = _is_paper()
    symbol = _symbol(par)

    # ── Modo simulado: sin API keys ────────────────────────────
    if not bybit_disponible():
        log.warning(f"Orden SIMULADA — sin API keys: {par} {direccion} qty={qty} lev={leverage}x")
        return {
            "simulado":  True,
            "par":       par,
            "symbol":    symbol,
            "direccion": direccion,
            "qty":       qty,
            "leverage":  leverage,
            "sl":        sl,
            "tp":        tp,
            "estado":    "SIMULADO — sin API keys",
        }

    # ── Activo disponible en Bybit ─────────────────────────────
    motivo = _check_disponible(par)
    if motivo:
        log.warning(f"Activo {par} no disponible — orden cancelada: {motivo}")
        return {"simulado": True, "par": par, "symbol": symbol, "estado": f"CANCELADO — {motivo}"}

    # ── Validaciones defensivas ────────────────────────────────
    valido, v_msg = _validar_parametros_orden(qty, leverage, sl, tp, direccion, precio_ref)
    if not valido:
        log.error(f"❌ Validación fallida para {par}: {v_msg}")
        return {
            "simulado": False,
            "ok":       False,
            "par":      par,
            "symbol":   symbol,
            "estado":   "RECHAZADO — validación",
            "error":    v_msg,
        }

    # ── Redondear qty al step size del instrumento ─────────────
    qty_redondeada = round_qty_by_symbol(qty, symbol)
    if qty_redondeada <= 0:
        msg = (
            f"qty post-redondeo inválida: {qty_redondeada} "
            f"(original={qty}, step={QTY_STEP.get(symbol, 'N/A')}) — "
            "aumentar margen o reducir precio de entrada"
        )
        log.error(f"❌ {par}: {msg}")
        return {"simulado": False, "ok": False, "par": par, "symbol": symbol,
                "estado": "RECHAZADO — qty mínima", "error": msg}

    try:
        session = _get_session(paper)
        lado    = "Buy" if direccion == "LONG" else "Sell"

        log.info(
            f"📤 Preparando orden Bybit: {symbol} {lado} | "
            f"leverage={leverage}x | qty={qty_redondeada} | "
            f"SL={sl} | TP={tp}"
        )

        # ── Paso 1: margen aislado → cada posición tiene su propio margen
        _set_isolated_margin(session, symbol, leverage)

        # ── Paso 2: set leverage ANTES de place_order ──────────
        # Bybit requiere que el leverage esté seteado explícitamente.
        # No hereda el leverage de órdenes anteriores ni de configuración global.
        lev_ok, lev_msg = _set_leverage(session, symbol, leverage)
        if not lev_ok:
            return {
                "simulado": False,
                "ok":       False,
                "par":      par,
                "symbol":   symbol,
                "estado":   "RECHAZADO — set_leverage",
                "error":    lev_msg,
            }

        # ── Paso 2: place_order con qty real del activo ────────
        # qty_redondeada = unidades del activo (ej: 0.012 ETH)
        # NUNCA debe ser el margen en USDT (ej: 3.0)
        result   = session.place_order(
            category="linear",
            symbol=symbol,
            side=lado,
            orderType="Market",
            qty=str(qty_redondeada),   # unidades reales del activo
            stopLoss=str(sl),
            takeProfit=str(tp),
        )

        ret_code = result.get("retCode", -1)
        ret_msg  = result.get("retMsg", "")
        order_id = result.get("result", {}).get("orderId", "?")

        if ret_code == 0:
            log.info(
                f"✅ Bybit EJECUTADO — {symbol} {lado} | "
                f"leverage={leverage}x | qty={qty_redondeada} | "
                f"SL={sl} | TP={tp} | orderId={order_id}"
            )
            return {
                "simulado":  False,
                "ok":        True,
                "estado":    "EJECUTADO",
                "symbol":    symbol,
                "par":       par,
                "direccion": direccion,
                "qty":       qty_redondeada,
                "leverage":  leverage,
                "sl":        sl,
                "tp":        tp,
                "order_id":  order_id,
                "ret_code":  ret_code,
                "resultado": result,
            }
        else:
            msg = _handle_error_code(ret_code, ret_msg, symbol)
            log.error(
                f"❌ Bybit RECHAZADO — {symbol} {lado} | "
                f"qty={qty_redondeada} | leverage={leverage}x | {msg}"
            )
            return {
                "simulado":  False,
                "ok":        False,
                "estado":    "RECHAZADO",
                "symbol":    symbol,
                "par":       par,
                "qty":       qty_redondeada,
                "leverage":  leverage,
                "ret_code":  ret_code,
                "error":     msg,
                "resultado": result,
            }

    except Exception as e:
        log.error(f"❌ ejecutar_orden excepción ({par}): {e}")
        return {
            "simulado": False,
            "ok":       False,
            "par":      par,
            "symbol":   symbol,
            "estado":   "ERROR",
            "error":    str(e),
        }


# ══════════════════════════════════════════════════════════════════
# CERRAR POSICIÓN (TOTAL O PARCIAL)
# ══════════════════════════════════════════════════════════════════

def cerrar_posicion(
    par:        str,
    direccion:  str,
    qty:        float | None = None,
    porcentaje: float | None = None,
) -> dict:
    """
    Cierra total o parcialmente una posición activa.

    Lógica de cierre:
        - Si qty está presente → cierra esa cantidad exacta.
        - Si porcentaje está presente → consulta la posición actual
          y cierra ese % de qty.
        - Si ninguno → cierra la posición completa (reduceOnly).

    Para LONG el cierre es Sell; para SHORT es Buy.
    Soporta PAPER_TRADING (simula sin llamar a Bybit).

    Returns:
        {
          "ok": bool,
          "simulado": bool,
          "estado": str,
          "par": str,
          "direccion": str,
          "qty_cerrada": float | None,
          "porcentaje": float | None,
          "order_id": str | None,
          "error": str | None,
        }
    """
    paper  = _is_paper()
    symbol = _symbol(par)

    base_result = {
        "simulado":    False,
        "par":         par,
        "direccion":   direccion,
        "symbol":      symbol,
        "qty_cerrada": qty,
        "porcentaje":  porcentaje,
        "order_id":    None,
        "error":       None,
    }

    # ── Modo simulado ──────────────────────────────────────────
    if not bybit_disponible():
        tipo  = "PARCIAL" if (qty or porcentaje) else "TOTAL"
        desc  = f"{qty} qty" if qty else (f"{porcentaje}%" if porcentaje else "completa")
        log.warning(f"Cierre SIMULADO {tipo} — {par} {direccion} ({desc})")
        return {
            **base_result,
            "ok":       True,
            "simulado": True,
            "estado":   f"SIMULADO — cierre {tipo} ({desc})",
        }

    motivo = _check_disponible(par)
    if motivo:
        return {**base_result, "ok": False, "estado": "CANCELADO", "error": motivo}

    try:
        session = _get_session(paper)
        lado    = "Sell" if direccion == "LONG" else "Buy"   # inverso para cerrar

        qty_a_cerrar = qty  # puede ser None aún

        # ── Si se pasó porcentaje, consultar qty actual ─────────
        if porcentaje is not None and qty is None:
            pos_resp = session.get_positions(category="linear", symbol=symbol)
            pos_list = pos_resp.get("result", {}).get("list", [])
            if not pos_list:
                return {
                    **base_result,
                    "ok":     False,
                    "estado": "ERROR",
                    "error":  f"Sin posición activa para {symbol}",
                }
            pos_size     = float(pos_list[0].get("size", 0))
            qty_a_cerrar = round_qty_by_symbol(pos_size * porcentaje / 100, symbol)
            base_result["porcentaje"]  = porcentaje
            base_result["qty_cerrada"] = qty_a_cerrar
            log.info(
                f"Cierre parcial {porcentaje}% de {pos_size} → qty={qty_a_cerrar} "
                f"en {symbol}"
            )

        # ── Armar kwargs de la orden ────────────────────────────
        order_kwargs: dict = {
            "category":   "linear",
            "symbol":     symbol,
            "side":       lado,
            "orderType":  "Market",
            "reduceOnly": True,
        }

        if qty_a_cerrar is not None:
            order_kwargs["qty"] = str(qty_a_cerrar)
        else:
            # Cierre total: consultar size actual de la posición
            pos_resp = session.get_positions(category="linear", symbol=symbol)
            pos_list = pos_resp.get("result", {}).get("list", [])
            pos_size = float(pos_list[0].get("size", 0)) if pos_list else 0
            if pos_size <= 0:
                return {
                    **base_result,
                    "ok":     False,
                    "estado": "ERROR",
                    "error":  f"Sin posición activa para {symbol}",
                }
            order_kwargs["qty"]        = str(pos_size)
            base_result["qty_cerrada"] = pos_size
            log.info(f"Cierre TOTAL {symbol} — qty={pos_size}")

        result   = session.place_order(**order_kwargs)
        ret_code = result.get("retCode", -1)
        ret_msg  = result.get("retMsg", "")
        order_id = result.get("result", {}).get("orderId")

        if ret_code == 0:
            tipo = "PARCIAL" if (porcentaje or qty) else "TOTAL"
            log.info(f"✅ Cierre {tipo} ejecutado — {symbol} | orderId: {order_id}")
            return {
                **base_result,
                "ok":       True,
                "estado":   f"CERRADO_{tipo}",
                "order_id": order_id,
                "ret_code": ret_code,
            }
        else:
            msg = _handle_error_code(ret_code, ret_msg, symbol)
            log.error(f"❌ Error cerrando posición {symbol}: {msg}")
            return {
                **base_result,
                "ok":       False,
                "estado":   "RECHAZADO",
                "ret_code": ret_code,
                "error":    msg,
            }

    except Exception as e:
        log.error(f"❌ cerrar_posicion excepción ({par}): {e}")
        return {**base_result, "ok": False, "estado": "ERROR", "error": str(e)}


# ══════════════════════════════════════════════════════════════════
# MOVER STOP-LOSS
# ══════════════════════════════════════════════════════════════════

def mover_stop_loss(par: str, direccion: str, nuevo_sl: float) -> dict:
    """
    Actualiza el stop-loss de la posición activa.

    Args:
        par:       símbolo interno
        direccion: "LONG" o "SHORT" (para logging)
        nuevo_sl:  nuevo precio de stop-loss

    Returns:
        {"ok": bool, "simulado": bool, "estado": str, "nuevo_sl": float, ...}
    """
    paper  = _is_paper()
    symbol = _symbol(par)

    base_result = {
        "par":       par,
        "symbol":    symbol,
        "direccion": direccion,
        "nuevo_sl":  nuevo_sl,
        "simulado":  False,
        "error":     None,
    }

    if nuevo_sl <= 0:
        return {**base_result, "ok": False, "estado": "RECHAZADO", "error": f"SL inválido: {nuevo_sl}"}

    # ── Modo simulado ──────────────────────────────────────────
    if not bybit_disponible():
        log.warning(f"SL SIMULADO movido — {par} {direccion} → {nuevo_sl}")
        return {
            **base_result,
            "ok":       True,
            "simulado": True,
            "estado":   f"SIMULADO — SL movido a {nuevo_sl}",
        }

    motivo = _check_disponible(par)
    if motivo:
        return {**base_result, "ok": False, "estado": "CANCELADO", "error": motivo}

    try:
        session  = _get_session(paper)
        result   = session.set_trading_stop(
            category="linear",
            symbol=symbol,
            stopLoss=str(nuevo_sl),
            slTriggerBy="LastPrice",
        )
        ret_code = result.get("retCode", -1)
        ret_msg  = result.get("retMsg", "")

        if ret_code == 0:
            log.info(f"✅ SL actualizado — {symbol} {direccion} → {nuevo_sl}")
            return {
                **base_result,
                "ok":       True,
                "estado":   "SL_ACTUALIZADO",
                "ret_code": ret_code,
            }
        else:
            msg = _handle_error_code(ret_code, ret_msg, symbol)
            log.error(f"❌ Error moviendo SL {symbol}: {msg}")
            return {
                **base_result,
                "ok":       False,
                "estado":   "RECHAZADO",
                "ret_code": ret_code,
                "error":    msg,
            }

    except Exception as e:
        log.error(f"❌ mover_stop_loss excepción ({par}): {e}")
        return {**base_result, "ok": False, "estado": "ERROR", "error": str(e)}


# ══════════════════════════════════════════════════════════════════
# KLINES 1H
# ══════════════════════════════════════════════════════════════════

def obtener_klines_1h(
    par:   str,
    since: datetime | None = None,
    limit: int = 50,
) -> dict:
    """
    Obtiene velas de 1 hora desde Bybit.

    Args:
        par:   símbolo interno (e.g. "BTC")
        since: datetime desde el cual traer velas (UTC).
               Si es None, trae las últimas `limit` velas.
        limit: máximo de velas a traer (tope interno: 200).

    Returns:
        {
          "ok":     bool,
          "par":    str,
          "symbol": str,
          "candles": [
            {
              "open_time": datetime (UTC),
              "open":   float,
              "high":   float,
              "low":    float,
              "close":  float,
              "volume": float,
              "ts":     int,     # timestamp Unix ms
            },
            ...
          ],
          "error": str | None,
        }
    """
    symbol = _symbol(par)
    limit  = min(limit, 200)  # Bybit max por request

    base_result = {
        "par":     par,
        "symbol":  symbol,
        "candles": [],
        "error":   None,
    }

    if not bybit_disponible():
        return {
            **base_result,
            "ok":    False,
            "error": "Sin API keys — Bybit no disponible",
        }

    motivo = _check_disponible(par)
    if motivo:
        return {**base_result, "ok": False, "error": motivo}

    try:
        session = _get_session()

        kwargs: dict = {
            "category": "linear",
            "symbol":   symbol,
            "interval": "60",    # 60 minutos = 1H en Bybit
            "limit":    limit,
        }

        if since is not None:
            since_utc       = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since
            kwargs["start"] = int(since_utc.timestamp() * 1000)

        result   = session.get_kline(**kwargs)
        ret_code = result.get("retCode", -1)
        ret_msg  = result.get("retMsg", "")

        if ret_code != 0:
            log.error(f"❌ Klines 1H {symbol}: retCode {ret_code} | {ret_msg}")
            return {
                **base_result,
                "ok":       False,
                "error":    ret_msg,
                "ret_code": ret_code,
            }

        raw_list = result.get("result", {}).get("list", [])
        candles  = []

        for row in raw_list:
            try:
                ts_ms = int(row[0])
                candles.append({
                    "open_time": datetime.utcfromtimestamp(ts_ms / 1000),
                    "open":      float(row[1]),
                    "high":      float(row[2]),
                    "low":       float(row[3]),
                    "close":     float(row[4]),
                    "volume":    float(row[5]),
                    "ts":        ts_ms,
                })
            except (IndexError, ValueError, TypeError) as parse_err:
                log.warning(f"Error parseando vela {row}: {parse_err}")
                continue

        # Bybit devuelve en orden DESC; invertir para orden cronológico
        candles.sort(key=lambda c: c["ts"])

        log.info(f"✅ Klines 1H {symbol} — {len(candles)} velas obtenidas")
        return {
            **base_result,
            "ok":      True,
            "candles": candles,
        }

    except Exception as e:
        log.error(f"❌ obtener_klines_1h excepción ({par}): {e}")
        return {**base_result, "ok": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════
# CANCELAR ORDEN ABIERTA (legacy)
# ══════════════════════════════════════════════════════════════════

def cerrar_orden(order_id: str, symbol: str) -> dict:
    """Cancela una orden pendiente por ID (no cierra posición)."""
    if not bybit_disponible():
        return {"ok": False, "error": "Sin API keys"}
    try:
        paper   = _is_paper()
        session = _get_session(paper)
        result  = session.cancel_order(
            category="linear",
            symbol=symbol,
            orderId=order_id,
        )
        ret_code = result.get("retCode", -1)
        if ret_code == 0:
            log.info(f"✅ Orden {order_id} cancelada en Bybit")
            return {"ok": True, "ret_code": ret_code}
        else:
            msg = _handle_error_code(ret_code, result.get("retMsg", ""), symbol)
            log.error(f"❌ Error cancelando orden {order_id}: {msg}")
            return {"ok": False, "ret_code": ret_code, "error": msg}
    except Exception as e:
        log.error(f"❌ cerrar_orden excepción: {e}")
        return {"ok": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════
# SYNC DE POSICIONES ABIERTAS — recuperar estado tras reinicio
# ══════════════════════════════════════════════════════════════════

def sync_posiciones_abiertas() -> list[dict]:
    """
    Consulta Bybit y devuelve las posiciones abiertas en este momento.

    Uso principal: llamar en on_ready() para reconstruir trades_abiertos
    en memoria si Jarvis se reinició con posiciones vivas en Bybit.

    Sin esto, si el bot cae y vuelve, gestionar_posiciones_activas()
    queda ciego y no puede mover SL ni ejecutar cierres parciales.

    Returns:
        Lista de dicts con los datos mínimos para reconstruir cada trade:
        {
          "activo":    str,   # símbolo interno (ej: "ETH")
          "symbol":   str,   # símbolo Bybit (ej: "ETHUSDT")
          "direccion": str,  # "LONG" o "SHORT"
          "entrada":  float, # precio de entrada promedio
          "qty":      float, # size actual de la posición
          "leverage": int,
          "sl":       float,
          "tp":       float,
          "pnl_usdt": float, # P&L no realizado actual
          "modo":     str,   # "REAL" o "PAPER"
        }
        Lista vacía si no hay posiciones o no hay API keys.
    """
    if not bybit_disponible():
        log.info("sync_posiciones_abiertas: sin API keys — skipping")
        return []

    try:
        paper   = _is_paper()
        session = _get_session(paper)
        result  = session.get_positions(
            category="linear",
            settleCoin="USDT",
        )
        ret_code = result.get("retCode", -1)

        if ret_code != 0:
            log.error(f"sync_posiciones_abiertas: retCode {ret_code} — {result.get('retMsg')}")
            return []

        pos_list = result.get("result", {}).get("list", [])
        abiertas = []

        # Invertir mapa BYBIT_SYMBOLS para resolver symbol → activo interno
        symbol_to_activo = {v: k for k, v in BYBIT_SYMBOLS.items()}

        for pos in pos_list:
            size = float(pos.get("size", 0))
            if size <= 0:
                continue   # posición cerrada o vacía

            symbol   = pos.get("symbol", "")
            activo   = symbol_to_activo.get(symbol, symbol.replace("USDT", "").replace("PERP", ""))
            side     = pos.get("side", "")
            direccion = "LONG" if side == "Buy" else "SHORT"

            entrada  = float(pos.get("avgPrice", 0))
            leverage = int(float(pos.get("leverage", 1)))
            sl       = float(pos.get("stopLoss", 0))
            tp       = float(pos.get("takeProfit", 0))
            pnl      = float(pos.get("unrealisedPnl", 0))

            trade = {
                "activo":      activo,
                "symbol":      symbol,
                "direccion":   direccion,
                "entrada":     entrada,
                "qty":         size,
                "qty_restante": size,
                "leverage":    leverage,
                "sl":          sl,
                "current_sl":  sl,
                "tp":          tp,
                "tp1":         tp,   # sin info de TPs escalonados — usar tp como fallback
                "tp2":         tp,
                "tp3":         tp,
                "sl_tras_tp1": entrada,  # breakeven por defecto
                "sl_tras_tp2": tp,
                "pnl_usdt":    round(pnl, 4),
                "fase_tp":     0,
                "margen":      round(entrada * size / leverage, 4) if leverage > 0 else 0,
                "score":       0,        # desconocido tras reinicio
                "confluencia": 0,
                "estrategia":  "sync",   # marca que viene de recuperación
                "razones":     ["Posición recuperada desde Bybit tras reinicio"],
                "timestamp":   datetime.now().isoformat(),
                "estado":      "ABIERTO",
                "modo":        "PAPER" if paper else "REAL",
                "zf": {}, "wh": {}, "sf": {},
            }
            abiertas.append(trade)
            log.info(
                f"🔄 Posición recuperada: {activo} {direccion} | "
                f"qty={size} | entrada={entrada} | SL={sl} | TP={tp} | "
                f"P&L={pnl:+.4f} USDT"
            )

        if abiertas:
            log.info(f"✅ sync_posiciones_abiertas: {len(abiertas)} posición/es recuperada/s")
        else:
            log.info("sync_posiciones_abiertas: sin posiciones abiertas en Bybit")

        return abiertas

    except Exception as e:
        log.error(f"❌ sync_posiciones_abiertas excepción: {e}")
        return []


# ══════════════════════════════════════════════════════════════════
# P&L EN TIEMPO REAL
# ══════════════════════════════════════════════════════════════════

def obtener_pnl_posiciones() -> list[dict]:
    """
    Consulta el P&L no realizado de todas las posiciones abiertas.

    Usado por el comando !pnl en Discord.

    Returns:
        Lista de dicts:
        {
          "activo":      str,
          "direccion":   str,
          "entrada":     float,
          "precio_mark": float,  # precio mark actual
          "qty":         float,
          "pnl_usdt":    float,  # P&L no realizado
          "pnl_pct":     float,  # % sobre margen
          "leverage":    int,
        }
    """
    if not bybit_disponible():
        return []

    try:
        paper   = _is_paper()
        session = _get_session(paper)
        result  = session.get_positions(
            category="linear",
            settleCoin="USDT",
        )
        ret_code = result.get("retCode", -1)
        if ret_code != 0:
            log.error(f"obtener_pnl_posiciones: retCode {ret_code}")
            return []

        pos_list = result.get("result", {}).get("list", [])
        symbol_to_activo = {v: k for k, v in BYBIT_SYMBOLS.items()}
        salida = []

        for pos in pos_list:
            size = float(pos.get("size", 0))
            if size <= 0:
                continue

            symbol    = pos.get("symbol", "")
            activo    = symbol_to_activo.get(symbol, symbol)
            side      = pos.get("side", "")
            direccion = "LONG" if side == "Buy" else "SHORT"
            entrada   = float(pos.get("avgPrice", 0))
            mark      = float(pos.get("markPrice", 0))
            leverage  = int(float(pos.get("leverage", 1)))
            pnl       = float(pos.get("unrealisedPnl", 0))
            margen    = entrada * size / leverage if leverage > 0 else 0
            pnl_pct   = (pnl / margen * 100) if margen > 0 else 0

            salida.append({
                "activo":      activo,
                "direccion":   direccion,
                "entrada":     entrada,
                "precio_mark": mark,
                "qty":         size,
                "pnl_usdt":    round(pnl, 4),
                "pnl_pct":     round(pnl_pct, 2),
                "leverage":    leverage,
            })

        return salida

    except Exception as e:
        log.error(f"❌ obtener_pnl_posiciones excepción: {e}")
        return []


# ══════════════════════════════════════════════════════════════════
# BALANCE USDT DISPONIBLE
# ══════════════════════════════════════════════════════════════════

def _safe_float(value, default: float = 0.0) -> float:
    """
    Convierte un valor a float de forma segura.
    Bybit a veces devuelve strings vacíos "" en lugar de "0".
    """
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def obtener_balance_usdt() -> dict:
    """
    Devuelve el balance USDT de la cuenta Unified.

    Usado por !riesgo en Discord y por engine.py para validar
    margen disponible antes de ejecutar órdenes.

    Returns:
        {
          "ok":              bool,
          "wallet_balance":  float,  # balance total
          "available":       float,  # disponible para operar
          "margin_used":     float,  # margen en uso
          "modo":            str,    # "PAPER" o "REAL"
        }
    """
    if not bybit_disponible():
        return {"ok": False, "error": "Sin API keys"}

    try:
        paper   = _is_paper()
        session = _get_session(paper)
        result  = session.get_wallet_balance(accountType="UNIFIED")
        ret_code = result.get("retCode", -1)

        if ret_code != 0:
            return {"ok": False, "error": result.get("retMsg", "Error desconocido")}

        # Bybit Unified: el balance disponible está en list[0].totalAvailableBalance
        # No siempre en coin.availableToWithdraw (puede venir vacío)
        cuenta = result["result"]["list"][0] if result["result"]["list"] else {}
        coins  = cuenta.get("coin", [])
        usdt   = next((c for c in coins if c["coin"] == "USDT"), {})

        # Wallet total y locked del USDT específico
        wallet = _safe_float(usdt.get("walletBalance"))
        locked = _safe_float(usdt.get("locked"))

        # Available: probar varios campos (Bybit a veces devuelve "")
        available = (
            _safe_float(usdt.get("availableToWithdraw"))
            or _safe_float(usdt.get("availableBalance"))
            or _safe_float(cuenta.get("totalAvailableBalance"))
            or max(wallet - locked, 0)  # fallback: total - locked
        )

        return {
            "ok":             True,
            "wallet_balance": round(wallet,    4),
            "available":      round(available, 4),
            "margin_used":    round(locked,    4),
            "modo":           "PAPER" if paper else "REAL",
        }

    except Exception as e:
        log.error(f"❌ obtener_balance_usdt excepción: {e}")
        return {"ok": False, "error": str(e)}