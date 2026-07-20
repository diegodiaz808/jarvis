"""
Trading Engine — motor autónomo de decisión y ejecución.
El bot DECIDE y EJECUTA. No sugiere, no duda.

v2.0 — Sistema de confluencia estricta + gestión activa de posiciones.
"""
import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from bots.trading_bot.zone_flip import evaluar_zone_flip
from bots.trading_bot.wave_hunt import evaluar_wave_hunt
from bots.trading_bot.smart_flow import evaluar_smart_flow
from bots.trading_bot.bybit_client import ejecutar_orden, bybit_disponible
from bots.trading_bot.risk_manager import calcular_sl_tp, calcular_tamano_posicion

log = logging.getLogger("engine")

# ─── Configuración central ─────────────────────────────────────
ACTIVOS = ["BTC", "ETH", "SOL", "TSLA", "AAPL", "NVDA"]

SCORE_EJECUTAR = 85      # score mínimo para abrir trade
SCORE_OBSERVAR = 70      # score mínimo para trackear
RR_MINIMO = 1.8          # ratio riesgo/beneficio mínimo
MAX_TRADES_ABIERTOS = 2  # máximo simultáneo
COOLDOWN_MINUTOS = 30    # minutos entre trades del mismo activo
CAPITAL = 75.0           # capital total en USDT

TRADE_LOG_PATH = Path("trades.json")
MEMORIA_PATH   = Path("memoria_trades.json")

VALID_DIRECTIONS = {"LONG", "SHORT"}

# ─── Estado del engine ─────────────────────────────────────────
class EngineState:
    ANALIZANDO    = "ANALIZANDO"
    EN_TRADE      = "EN_TRADE"
    POST_ANALISIS = "POST_ANALISIS"


estado_engine = EngineState.ANALIZANDO
trades_abiertos: list[dict] = []
ultimo_trade: dict[str, datetime] = defaultdict(lambda: datetime.min)

# Timestamp de último chequeo de gestión de posiciones
_ultimo_gestion_posiciones: datetime = datetime.min


# ─── Persistencia ──────────────────────────────────────────────
def _cargar_json(path: Path) -> list:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return []
    return []


def _guardar_json(path: Path, data: list):
    try:
        path.write_text(json.dumps(data, indent=2, default=str))
    except Exception as e:
        log.error(f"Error guardando {path}: {e}")


def registrar_trade(trade: dict):
    trades = _cargar_json(TRADE_LOG_PATH)
    trades.append(trade)
    _guardar_json(TRADE_LOG_PATH, trades)


def guardar_aprendizaje(aprendizaje: dict):
    memoria = _cargar_json(MEMORIA_PATH)
    memoria.append(aprendizaje)
    _guardar_json(MEMORIA_PATH, memoria)


# ─── Sistema de scoring con confluencia estricta ───────────────
async def scoring_completo(activo: str) -> dict:
    """
    Corre las 3 estrategias en paralelo.
    Solo activa confluencia si todas las señales válidas apuntan al mismo lado.
    Si hay contradicción → NO_OPERAR siempre.
    """
    # ── 1. Ejecutar en paralelo con return_exceptions ───────────
    results = await asyncio.gather(
        evaluar_wave_hunt(activo),
        evaluar_zone_flip(activo),
        evaluar_smart_flow(activo),
        return_exceptions=True,
    )
    nombres = ["wave_hunt", "zone_flip", "smart_flow"]
    raw = dict(zip(nombres, results))

    # ── 2. Normalizar: separar válidas de descartadas ───────────
    estrategias: dict[str, dict] = {}
    descartadas: list[dict]      = []

    for nombre, resultado in raw.items():
        motivo = None

        if isinstance(resultado, Exception):
            motivo = f"excepción: {resultado}"
        elif not isinstance(resultado, dict):
            motivo = "retorno inválido (no es dict)"
        elif resultado.get("puntuacion") is None and resultado.get("score") is None:
            motivo = "sin campo puntuacion/score"
        elif resultado.get("direccion") not in (None, "LONG", "SHORT", "NEUTRAL", "NO_OPERAR"):
            motivo = f"dirección desconocida: {resultado.get('direccion')}"

        if motivo:
            descartadas.append({"estrategia": nombre, "motivo": motivo})
            log.warning(f"[scoring] {nombre}@{activo} descartada — {motivo}")
        else:
            # Normalizar campo puntuacion
            if resultado.get("puntuacion") is None:
                resultado["puntuacion"] = resultado.get("score", 0)
            resultado.setdefault("razones",  [])
            resultado.setdefault("leverage", 2)
            resultado.setdefault("activo",   activo)
            resultado.setdefault("estrategia", nombre)
            estrategias[nombre] = resultado

    # ── 3. Filtrar señales con dirección válida (LONG / SHORT) ──
    validas = {
        n: e for n, e in estrategias.items()
        if e.get("direccion") in VALID_DIRECTIONS
    }

    _no_operar_base = {
        "activo": activo,
        "operar": False,
        "score": 0,
        "direccion": None,
        "estrategia": None,
        "leverage": 2,
        "confluencia": 0,
        "prioridad": "NO_OPERAR",
        "razones": [],
        "estrategias": estrategias,
        "descartadas": descartadas,
    }

    if not validas:
        _no_operar_base["razones"] = ["Sin señales válidas (LONG/SHORT)"]
        return _no_operar_base

    # ── 4. Detectar contradicción ───────────────────────────────
    dirs_activas = {e["direccion"] for e in validas.values()}

    if len(dirs_activas) > 1:
        # Hay señales LONG y SHORT al mismo tiempo → contradicción
        detalle = " | ".join(f"{n}→{e['direccion']}" for n, e in validas.items())
        log.info(f"[scoring] {activo} CONTRADICCIÓN: {detalle} → NO_OPERAR")
        _no_operar_base["razones"] = [f"⚠️ Contradicción: {detalle}"]
        return _no_operar_base

    # ── 5. Confluencia en mismo sentido ────────────────────────
    direccion_final = dirs_activas.pop()   # único elemento
    confluencias    = len(validas)
    scores_activos  = [e.get("puntuacion", 0) for e in validas.values()]
    leverages_activos = [e.get("leverage", 2) for e in validas.values()]

    if confluencias == 3:
        prioridad       = "TRIPLE_CONFLUENCIA"
        leverage_calc   = max(leverages_activos)
        score_final     = min(max(scores_activos) + 20, 100)
        estrategia_lider = max(validas, key=lambda n: validas[n].get("puntuacion", 0))
        razones_conf    = [f"⚡⚡ TRIPLE CONFLUENCIA 3/3 → {direccion_final}"]

    elif confluencias == 2:
        prioridad       = "DOBLE_CONFLUENCIA"
        leverage_calc   = min(max(leverages_activos), 6)
        score_promedio  = sum(scores_activos) / 2
        score_final     = min(int(score_promedio) + 10, 100)
        estrategia_lider = max(validas, key=lambda n: validas[n].get("puntuacion", 0))
        razones_conf    = [f"⚡ DOBLE CONFLUENCIA 2/3 → {direccion_final}"]

    elif confluencias == 1:
        prioridad       = "SEÑAL_UNICA"
        nombre_unico    = list(validas.keys())[0]
        e_unico         = validas[nombre_unico]
        leverage_calc   = e_unico.get("leverage", 2)
        score_final     = e_unico.get("puntuacion", 0)
        estrategia_lider = nombre_unico
        razones_conf    = [f"📶 Señal única: {nombre_unico} → {direccion_final}"]

    else:
        _no_operar_base["razones"] = ["Sin confluencias computables"]
        return _no_operar_base

    # ── 6. Hard cap leverage ────────────────────────────────────
    leverage_final = min(leverage_calc, 10)

    # ── 7. Armar razones consolidadas ──────────────────────────
    razones_lider = validas[estrategia_lider].get("razones", [])
    razones_final = (razones_conf + razones_lider)[:6]

    # ── 8. Score por estrategia (para Discord / log) ───────────
    score_por_estrategia = {n: e.get("puntuacion", 0) for n, e in estrategias.items()}

    return {
        "activo":             activo,
        "operar":             True,
        "score":              score_final,
        "direccion":          direccion_final,
        "estrategia":         estrategia_lider,
        "leverage":           leverage_final,
        "confluencia":        confluencias,
        "prioridad":          prioridad,
        "razones":            razones_final,
        "estrategias":        estrategias,
        "descartadas":        descartadas,
        "score_por_estrategia": score_por_estrategia,
        # compatibilidad con código legacy
        "zf": estrategias.get("zone_flip", {}),
        "wh": estrategias.get("wave_hunt", {}),
        "sf": estrategias.get("smart_flow", {}),
    }


# ─── Filtros de riesgo ─────────────────────────────────────────
def _puede_operar(activo: str, direccion: str) -> tuple[bool, str]:
    if len(trades_abiertos) >= MAX_TRADES_ABIERTOS:
        return False, f"Máximo {MAX_TRADES_ABIERTOS} trades abiertos"

    cooldown = ultimo_trade[activo]
    if datetime.now() - cooldown < timedelta(minutes=COOLDOWN_MINUTOS):
        mins = int(
            (timedelta(minutes=COOLDOWN_MINUTOS) - (datetime.now() - cooldown)).seconds / 60
        )
        return False, f"Cooldown activo en {activo} — {mins}m restantes"

    for t in trades_abiertos:
        if t["activo"] == activo and t["direccion"] == direccion:
            return False, f"Ya hay trade {direccion} abierto en {activo}"

    return True, "OK"


def _valida_rr(precio: float, direccion: str, score: int) -> tuple[bool, dict]:
    riesgo = calcular_sl_tp(precio, direccion, score)
    rr = riesgo.get("rr", 0)
    if rr < RR_MINIMO:
        return False, riesgo
    return True, riesgo


# ─── Motor de ejecución ────────────────────────────────────────
async def ejecutar_trade(señal: dict, precio: float, canal_discord) -> bool:
    """
    Ejecuta un trade si pasa todos los filtros.
    La señal DEBE tener operar=True. Si tiene contradicción nunca llega aquí.
    """
    global estado_engine

    # Guardia principal: nunca ejecutar si operar es False
    if not señal.get("operar", False):
        log.info(f"⛔ {señal.get('activo')} operar=False — {señal.get('prioridad', 'NO_OPERAR')}")
        return False

    activo    = señal["activo"]
    direccion = señal["direccion"]
    score     = señal["score"]

    puede, motivo = _puede_operar(activo, direccion)
    if not puede:
        log.info(f"⛔ {activo} bloqueado: {motivo}")
        return False

    rr_ok, riesgo = _valida_rr(precio, direccion, score)
    if not rr_ok:
        log.info(f"⛔ {activo} R:R insuficiente: {riesgo.get('rr', 0):.2f} < {RR_MINIMO}")
        return False

    monto = calcular_tamano_posicion(CAPITAL, score)

    # ── Calcular TPs escalonados ────────────────────────────────
    sl  = riesgo["sl"]
    tp1 = riesgo.get("tp1") or riesgo["tp"]
    tp2 = riesgo.get("tp2") or riesgo["tp"]
    tp3 = riesgo.get("tp3") or riesgo["tp"]

    estrategias_activas  = [
        n for n, e in señal.get("estrategias", {}).items()
        if e.get("direccion") in VALID_DIRECTIONS
    ]
    estrategias_resultado = señal.get("estrategias", {})

    trade = {
        # ── identificación ──────────────────────────────────────
        "activo":               activo,
        "estrategia":           señal["estrategia"],
        "estrategia_lider":     señal["estrategia"],
        "estrategias_activas":  estrategias_activas,
        "estrategias_resultado": estrategias_resultado,
        "prioridad":            señal.get("prioridad", "SEÑAL_UNICA"),
        "confluencia":          señal.get("confluencia", 1),
        "score_por_estrategia": señal.get("score_por_estrategia", {}),
        # ── ejecución ───────────────────────────────────────────
        "direccion":            direccion,
        "entrada":              precio,
        "sl":                   sl,
        "current_sl":           sl,
        "tp":                   tp1,
        "tp1":                  tp1,
        "tp2":                  tp2,
        "tp3":                  tp3,
        "tp1_filled":           False,
        "tp2_filled":           False,
        "tp3_filled":           False,
        "remaining_size":       monto,
        "fase_tp":              0,
        "pnl_realizado":        0.0,
        "eventos":              [],
        # ── metadata ────────────────────────────────────────────
        "score":                score,
        "leverage":             señal["leverage"],
        "leverage_final":       señal["leverage"],
        "monto":                monto,
        "razones":              señal["razones"][:3],
        "timestamp":            datetime.now().isoformat(),
        "estado":               "ABIERTO",
        "last_checked_at":      datetime.now().isoformat(),
    }

    resultado   = ejecutar_orden(activo, direccion, monto, sl, tp1)
    estado_bybit = resultado.get("estado", "")
    modo = "SIMULADO" if resultado.get("simulado") else "EJECUTADO"
    trade["modo"] = modo

    if estado_bybit in ("EJECUTADO", "SIMULADO — sin API keys", "CANCELADO"):
        trades_abiertos.append(trade)
        ultimo_trade[activo] = datetime.now()
        registrar_trade(trade)
        estado_engine = EngineState.EN_TRADE
        await _notificar_trade(trade, canal_discord)
        log.info(
            f"✅ TRADE {modo}: {activo} {direccion} "
            f"score={score} lev={señal['leverage']}x "
            f"prioridad={trade['prioridad']}"
        )
        return True
    else:
        log.error(f"❌ Trade NO registrado — Bybit rechazó: {resultado.get('error', estado_bybit)}")
        return False


async def _notificar_trade(trade: dict, canal):
    emoji_dir  = "🟢" if trade["direccion"] == "LONG" else "🔴"
    prioridad  = trade.get("prioridad", "SEÑAL_UNICA")
    conf       = trade.get("confluencia", 1)
    modo_str   = "🔵 SIMULADO" if trade["modo"] == "SIMULADO" else "🟠 REAL"

    # Prefijo según prioridad
    if prioridad == "TRIPLE_CONFLUENCIA":
        prefijo = "⚡⚡⚡ TRIPLE CONFLUENCIA"
    elif prioridad == "DOBLE_CONFLUENCIA":
        prefijo = "⚡⚡ DOBLE CONFLUENCIA"
    else:
        prefijo = "📶 SEÑAL ÚNICA"

    # Scores individuales
    scores = trade.get("score_por_estrategia", {})
    scores_str = "  ".join(
        f"`{n.upper()[:2]}: {v}%`" for n, v in scores.items()
    )

    # Descartadas
    descartadas = trade.get("estrategias_resultado", {})
    desc_str = ""

    razones_str = "\n".join([f"  • {r}" for r in trade["razones"]])

    msg = (
        f"@everyone\n"
        f"**{prefijo} — {trade['activo']}** {modo_str}\n"
        f"📊 Prioridad: `{prioridad}` · Confluencia: `{conf}/3`\n"
        f"📍 {emoji_dir} `{trade['direccion']}` · "
        f"Líder: `{trade['estrategia_lider'].upper()}` · "
        f"Score: `{trade['score']}%`\n"
        f"📈 Scores por estrategia: {scores_str}\n"
        f"💰 Entrada: `${trade['entrada']:,.2f}` · "
        f"Monto: `${trade['monto']} USDT` · "
        f"Leverage: `{trade['leverage']}x`\n"
        f"🛡️ SL: `${trade['sl']:,.2f}` · "
        f"🎯 TP1: `${trade['tp1']:,.2f}` · "
        f"TP2: `${trade['tp2']:,.2f}` · "
        f"TP3: `${trade['tp3']:,.2f}`\n"
        f"📋 Señales:\n{razones_str}"
    )
    try:
        await canal.send(msg)
    except Exception as e:
        log.error(f"Error notificando Discord: {e}")


# ─── Gestión activa de posiciones ─────────────────────────────
async def gestionar_posiciones_activas(canal_discord):
    """
    Chequea cada trade abierto con velas 1H desde el último chequeo.
    Ejecuta cierres parciales y mueve SL automáticamente.
    Se llama cada ciclo pero sólo actúa si pasó ≥1h desde el último run.
    """
    global trades_abiertos, _ultimo_gestion_posiciones

    ahora = datetime.now()
    if ahora - _ultimo_gestion_posiciones < timedelta(hours=1):
        return  # Todavía no toca
    _ultimo_gestion_posiciones = ahora

    if not trades_abiertos:
        return

    log.info(f"📊 Gestión de posiciones — {len(trades_abiertos)} trade(s) abierto(s)")

    trades_a_cerrar = []

    for trade in list(trades_abiertos):
        try:
            await _gestionar_un_trade(trade, canal_discord)
        except Exception as e:
            log.error(f"Error gestionando trade {trade.get('activo')}: {e}")

        if trade.get("estado") in ("CERRADO", "SL_HIT"):
            trades_a_cerrar.append(trade)

    for trade in trades_a_cerrar:
        if trade in trades_abiertos:
            trades_abiertos.remove(trade)
            registrar_trade(trade)
            guardar_aprendizaje(_construir_aprendizaje(trade))
            await _notificar_cierre_final(trade, canal_discord)

    # Actualizar estado del engine
    global estado_engine
    if not trades_abiertos:
        estado_engine = EngineState.ANALIZANDO
    else:
        estado_engine = EngineState.EN_TRADE


async def _gestionar_un_trade(trade: dict, canal_discord):
    """
    Procesa un trade individual vela por vela (1H) desde last_checked_at.
    Modifica el trade in-place.
    """
    activo    = trade["activo"]
    direccion = trade["direccion"]

    # ── Obtener velas 1H desde último chequeo ──────────────────
    try:
        from bots.tradingview_bridge import get_chart_data
        data = await get_chart_data(activo, timeframe="1h", bars=24)
        if not data or not data.get("success"):
            log.warning(f"Sin datos 1H para {activo}")
            return
        velas = data.get("candles", data.get("bars", []))
    except Exception as e:
        log.error(f"Error obteniendo velas 1H para {activo}: {e}")
        return

    # Filtrar velas posteriores a last_checked_at
    last_checked = datetime.fromisoformat(
        trade.get("last_checked_at", trade["timestamp"])
    )
    velas_nuevas = [
        v for v in velas
        if _ts_vela(v) > last_checked
    ]

    if not velas_nuevas:
        log.info(f"[gestión] {activo} sin velas nuevas desde {last_checked}")
        return

    # ── Iterar vela por vela ────────────────────────────────────
    for vela in velas_nuevas:
        high = float(vela.get("high", 0))
        low  = float(vela.get("low",  0))
        ts_v = _ts_vela(vela)

        current_sl = float(trade["current_sl"])
        tp1 = float(trade["tp1"])
        tp2 = float(trade["tp2"])
        tp3 = float(trade["tp3"])

        if direccion == "SHORT":
            # ── SL hit ──────────────────────────────────────────
            if high >= current_sl:
                await _cerrar_total(trade, current_sl, "SL_HIT", ts_v, canal_discord)
                trade["last_checked_at"] = ts_v.isoformat()
                return  # No seguir procesando

            # ── TP1 ─────────────────────────────────────────────
            if not trade["tp1_filled"] and low <= tp1:
                porcion = round(trade["remaining_size"] * 0.40, 4)
                _cerrar_parcial(trade, porcion, tp1, "TP1", ts_v)
                trade["current_sl"]  = trade["entrada"]   # breakeven
                trade["tp1_filled"]  = True
                await _notificar_evento(trade, "TP1", tp1, porcion, canal_discord)

            # ── TP2 ─────────────────────────────────────────────
            if trade["tp1_filled"] and not trade["tp2_filled"] and low <= tp2:
                porcion = round(trade["remaining_size"] * 0.35, 4)
                _cerrar_parcial(trade, porcion, tp2, "TP2", ts_v)
                trade["current_sl"]  = tp1
                trade["tp2_filled"]  = True
                await _notificar_evento(trade, "TP2", tp2, porcion, canal_discord)

            # ── TP3 ─────────────────────────────────────────────
            if trade["tp2_filled"] and not trade["tp3_filled"] and low <= tp3:
                porcion = trade["remaining_size"]
                _cerrar_parcial(trade, porcion, tp3, "TP3", ts_v)
                trade["tp3_filled"] = True
                trade["estado"]     = "CERRADO"
                await _notificar_evento(trade, "TP3", tp3, porcion, canal_discord)
                trade["last_checked_at"] = ts_v.isoformat()
                return

        else:  # LONG
            # ── SL hit ──────────────────────────────────────────
            if low <= current_sl:
                await _cerrar_total(trade, current_sl, "SL_HIT", ts_v, canal_discord)
                trade["last_checked_at"] = ts_v.isoformat()
                return

            # ── TP1 ─────────────────────────────────────────────
            if not trade["tp1_filled"] and high >= tp1:
                porcion = round(trade["remaining_size"] * 0.40, 4)
                _cerrar_parcial(trade, porcion, tp1, "TP1", ts_v)
                trade["current_sl"]  = trade["entrada"]
                trade["tp1_filled"]  = True
                await _notificar_evento(trade, "TP1", tp1, porcion, canal_discord)

            # ── TP2 ─────────────────────────────────────────────
            if trade["tp1_filled"] and not trade["tp2_filled"] and high >= tp2:
                porcion = round(trade["remaining_size"] * 0.35, 4)
                _cerrar_parcial(trade, porcion, tp2, "TP2", ts_v)
                trade["current_sl"]  = tp1
                trade["tp2_filled"]  = True
                await _notificar_evento(trade, "TP2", tp2, porcion, canal_discord)

            # ── TP3 ─────────────────────────────────────────────
            if trade["tp2_filled"] and not trade["tp3_filled"] and high >= tp3:
                porcion = trade["remaining_size"]
                _cerrar_parcial(trade, porcion, tp3, "TP3", ts_v)
                trade["tp3_filled"] = True
                trade["estado"]     = "CERRADO"
                await _notificar_evento(trade, "TP3", tp3, porcion, canal_discord)
                trade["last_checked_at"] = ts_v.isoformat()
                return

        trade["last_checked_at"] = ts_v.isoformat()


# ── Helpers de gestión ─────────────────────────────────────────
def _ts_vela(vela: dict) -> datetime:
    """Convierte el timestamp de una vela a datetime."""
    ts = vela.get("time") or vela.get("timestamp") or vela.get("ts")
    if isinstance(ts, (int, float)):
        # Binance-style milisegundos
        if ts > 1e12:
            ts = ts / 1000
        return datetime.utcfromtimestamp(ts)
    if isinstance(ts, str):
        return datetime.fromisoformat(ts)
    return datetime.utcnow()


def _cerrar_parcial(trade: dict, porcion: float, precio: float, nivel: str, ts: datetime):
    """Registra cierre parcial en el trade."""
    entrada    = float(trade["entrada"])
    direccion  = trade["direccion"]

    if direccion == "LONG":
        pnl_pct = (precio - entrada) / entrada * 100
    else:
        pnl_pct = (entrada - precio) / entrada * 100

    pnl_usdt = round(porcion * pnl_pct / 100, 4)
    trade["remaining_size"]  = round(trade["remaining_size"] - porcion, 4)
    trade["pnl_realizado"]   = round(trade.get("pnl_realizado", 0) + pnl_usdt, 4)
    trade["fase_tp"]         = {"TP1": 1, "TP2": 2, "TP3": 3}.get(nivel, 0)
    trade["eventos"].append({
        "tipo":      nivel,
        "precio":    precio,
        "porcion":   porcion,
        "pnl_usdt":  pnl_usdt,
        "pnl_pct":   round(pnl_pct, 2),
        "timestamp": ts.isoformat(),
    })
    log.info(f"[gestión] {trade['activo']} {nivel} @ {precio:.2f} — "
             f"cerrado {porcion} USDT · P&L parcial: {pnl_usdt:+.4f} USDT")


async def _cerrar_total(trade: dict, precio: float, motivo: str, ts: datetime, canal):
    """Cierra toda la posición restante."""
    porcion = trade.get("remaining_size", trade["monto"])
    _cerrar_parcial(trade, porcion, precio, motivo, ts)
    trade["estado"]     = "CERRADO" if motivo != "SL_HIT" else "SL_HIT"
    trade["precio_cierre"] = precio

    emoji = "🛑" if motivo == "SL_HIT" else "✅"
    msg = (
        f"{emoji} **{motivo} — {trade['activo']}**\n"
        f"Precio cierre: `${precio:,.2f}`\n"
        f"P&L realizado total: `{trade['pnl_realizado']:+.4f} USDT`\n"
        f"Posición remaining cerrada: `{porcion} USDT`"
    )
    try:
        await canal.send(msg)
    except Exception as e:
        log.error(f"Error notificando cierre: {e}")


async def _notificar_evento(trade: dict, nivel: str, precio: float, porcion: float, canal):
    """Notifica TP parcial con nuevo SL."""
    emojis = {"TP1": "🎯", "TP2": "🎯🎯", "TP3": "🎯🎯🎯"}
    emoji  = emojis.get(nivel, "🎯")
    nuevo_sl = trade.get("current_sl", trade["sl"])
    pnl_parcial = trade["eventos"][-1].get("pnl_usdt", 0) if trade["eventos"] else 0

    msg = (
        f"{emoji} **{nivel} alcanzado — {trade['activo']}**\n"
        f"Precio: `${precio:,.2f}` · Cerrado: `{porcion} USDT`\n"
        f"P&L parcial: `{pnl_parcial:+.4f} USDT`\n"
        f"SL movido a: `${nuevo_sl:,.2f}`\n"
        f"Posición restante: `{trade['remaining_size']} USDT`"
    )
    try:
        await canal.send(msg)
    except Exception as e:
        log.error(f"Error notificando {nivel}: {e}")


async def _notificar_cierre_final(trade: dict, canal):
    """Notifica resumen final del trade tras su cierre completo."""
    pnl  = trade.get("pnl_realizado", 0)
    emoji = "✅" if pnl >= 0 else "❌"
    eventos_str = "\n".join(
        f"  • {ev['tipo']} @ ${ev['precio']:,.2f} → {ev['pnl_usdt']:+.4f} USDT"
        for ev in trade.get("eventos", [])
    )
    msg = (
        f"{emoji} **TRADE CERRADO — {trade['activo']}**\n"
        f"Dirección: `{trade['direccion']}` · "
        f"Estrategia líder: `{trade.get('estrategia_lider','').upper()}`\n"
        f"Entrada: `${trade['entrada']:,.2f}` · "
        f"P&L total realizado: `{pnl:+.4f} USDT`\n"
        f"Resultado:\n{eventos_str}"
    )
    try:
        await canal.send(msg)
    except Exception as e:
        log.error(f"Error notificando cierre final: {e}")


def _construir_aprendizaje(trade: dict) -> dict:
    pnl     = trade.get("pnl_realizado", 0)
    gano    = pnl > 0
    toco_sl = trade.get("estado") == "SL_HIT"
    return {
        "timestamp":        datetime.now().isoformat(),
        "activo":           trade["activo"],
        "estrategia":       trade.get("estrategia_lider", trade.get("estrategia")),
        "prioridad":        trade.get("prioridad"),
        "confluencia":      trade.get("confluencia", 0),
        "direccion":        trade["direccion"],
        "entrada":          trade["entrada"],
        "pnl_realizado":    pnl,
        "resultado":        "WIN" if gano else "LOSS",
        "toco_sl":          toco_sl,
        "tp1_filled":       trade.get("tp1_filled", False),
        "tp2_filled":       trade.get("tp2_filled", False),
        "tp3_filled":       trade.get("tp3_filled", False),
        "score_original":   trade.get("score", 0),
        "score_por_estrategia": trade.get("score_por_estrategia", {}),
        "eventos":          trade.get("eventos", []),
        "notas": {
            "leccion": (
                "Setup válido, ejecución correcta"          if gano
                else "Revisar filtros — stop fue activado"  if toco_sl
                else "Posición cerrada antes de targets"
            ),
        },
    }


# ─── Post-trade analysis (legacy, aún útil) ───────────────────
async def analizar_post_trade(trade: dict, precio_cierre: float, canal_discord):
    """
    Evalúa el resultado del trade y guarda el aprendizaje.
    Versión legacy — la gestión activa usa _construir_aprendizaje().
    """
    global estado_engine

    ganancia = (
        (precio_cierre - trade["entrada"]) / trade["entrada"] * 100
        if trade["direccion"] == "LONG"
        else (trade["entrada"] - precio_cierre) / trade["entrada"] * 100
    )
    gano    = ganancia > 0
    toco_sl = precio_cierre <= trade["sl"] if trade["direccion"] == "LONG" else precio_cierre >= trade["sl"]
    toco_tp = precio_cierre >= trade["tp"] if trade["direccion"] == "LONG" else precio_cierre <= trade["tp"]

    aprendizaje = {
        "timestamp":       datetime.now().isoformat(),
        "activo":          trade["activo"],
        "estrategia":      trade.get("estrategia_lider", trade.get("estrategia")),
        "prioridad":       trade.get("prioridad"),
        "confluencia":     trade.get("confluencia", 0),
        "direccion":       trade["direccion"],
        "entrada":         trade["entrada"],
        "cierre":          precio_cierre,
        "ganancia_pct":    round(ganancia, 2),
        "resultado":       "WIN" if gano else "LOSS",
        "toco_sl":         toco_sl,
        "toco_tp":         toco_tp,
        "score_original":  trade.get("score", 0),
        "score_por_estrategia": trade.get("score_por_estrategia", {}),
        "notas": {
            "sl_correcto":     not toco_sl or (toco_sl and not gano),
            "entrada_correcta": gano,
            "leccion": (
                "Setup válido, ejecución correcta"          if gano
                else "Revisar filtros — stop fue activado"  if toco_sl
                else "Posición cerrada antes de targets"
            ),
        },
    }

    guardar_aprendizaje(aprendizaje)

    if trade in trades_abiertos:
        trades_abiertos.remove(trade)

    estado_engine = EngineState.POST_ANALISIS if trades_abiertos else EngineState.ANALIZANDO

    emoji = "✅" if gano else "❌"
    msg = (
        f"{emoji} **POST-TRADE — {trade['activo']}**\n"
        f"Resultado: `{'WIN' if gano else 'LOSS'}` · "
        f"P&L: `{ganancia:+.2f}%`\n"
        f"Entrada: `${trade['entrada']:,.2f}` → Cierre: `${precio_cierre:,.2f}`\n"
        f"Lección: {aprendizaje['notas']['leccion']}"
    )
    try:
        await canal_discord.send(msg)
    except Exception as e:
        log.error(f"Error notificando post-trade: {e}")

    log.info(f"Post-trade {trade['activo']}: {aprendizaje['resultado']} {ganancia:+.2f}%")
    return aprendizaje


# ─── Loop principal: scan global cada 5 min ───────────────────
async def loop_scan_global(canal_logs, canal_talk):
    """
    Analiza todos los activos en paralelo.
    Ejecuta solo si operar=True AND score >= SCORE_EJECUTAR AND no contradicción.
    """
    global estado_engine
    estado_engine = EngineState.ANALIZANDO
    log.info(f"🔍 Scan global — {len(ACTIVOS)} activos")

    señales = []

    for activo in ACTIVOS:
        try:
            señal = await scoring_completo(activo)
            score    = señal["score"]
            operar   = señal.get("operar", False)
            prioridad = señal.get("prioridad", "NO_OPERAR")

            if operar and score >= SCORE_EJECUTAR and señal.get("direccion"):
                señales.append(señal)
                log.info(
                    f"🎯 {activo} → score={score} dir={señal['direccion']} "
                    f"[{prioridad}] líder={señal['estrategia']}"
                )
            elif score >= SCORE_OBSERVAR and operar:
                log.info(f"👁 {activo} → observando score={score} [{prioridad}]")
            elif not operar:
                log.info(f"⛔ {activo} → NO_OPERAR [{prioridad}] score={score}")
            else:
                log.info(f"⏭ {activo} → ignorado score={score}")

        except Exception as e:
            log.error(f"Error analizando {activo}: {e}")

    if not señales:
        log.info("Sin señales ejecutables este ciclo")
        return

    # ── Ordenar: triple > doble > simple, luego score ──────────
    ORDEN_PRIORIDAD = {"TRIPLE_CONFLUENCIA": 3, "DOBLE_CONFLUENCIA": 2, "SEÑAL_UNICA": 1}
    señales.sort(
        key=lambda s: (ORDEN_PRIORIDAD.get(s.get("prioridad", ""), 0), s["score"], s["leverage"]),
        reverse=True,
    )

    CRYPTOS = {"BTC", "ETH", "SOL"}
    TRAD    = {"TSLA", "AAPL", "NVDA"}

    candidatos = []
    for grupo in [CRYPTOS, TRAD]:
        mejor = next((s for s in señales if s["activo"] in grupo), None)
        if mejor:
            candidatos.append(mejor)

    for señal in candidatos:
        activo = señal["activo"]
        try:
            from bots.tradingview_bridge import get_chart_data, get_price
            data   = await get_chart_data(activo, timeframe="4h", bars=1)
            precio = get_price(data) if data.get("success") else None
        except Exception:
            precio = None

        if precio is None:
            log.warning(f"Sin precio para {activo}, saltando")
            continue

        await ejecutar_trade(señal, precio, canal_talk)


# ─── Loop secundario: reevaluación cada 1 min ─────────────────
async def loop_confirmaciones(canal_talk):
    """
    Reevalúa activos con score ≥ SCORE_OBSERVAR cada 1 min.
    Solo ejecuta si operar=True y score >= SCORE_EJECUTAR.
    Contradicción bloquea siempre.
    """
    if estado_engine == EngineState.EN_TRADE and len(trades_abiertos) >= MAX_TRADES_ABIERTOS:
        return

    for activo in ACTIVOS:
        puede, _ = _puede_operar(activo, "LONG")
        if not puede:
            continue
        try:
            señal = await scoring_completo(activo)

            # Doble guardia: operar=True + score suficiente
            if (
                señal.get("operar", False)
                and señal["score"] >= SCORE_EJECUTAR
                and señal.get("direccion")
            ):
                from bots.tradingview_bridge import get_chart_data, get_price
                data   = await get_chart_data(activo, timeframe="4h", bars=1)
                precio = get_price(data) if data.get("success") else None
                if precio:
                    await ejecutar_trade(señal, precio, canal_talk)

        except Exception as e:
            log.error(f"Error en loop_confirmaciones({activo}): {e}")
