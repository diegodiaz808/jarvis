"""
Trading Engine — motor autónomo de decisión y ejecución.
Reemplaza completamente a trading_bot.py.

El bot DECIDE y EJECUTA. No sugiere, no duda.

─── CONCEPTOS CLAVE DE SIZING ──────────────────────────────────────────────────
  margen (USDT): capital real que arriesgamos como garantía.
                 Ej: 3.75 USDT de nuestra cuenta.

  qty (activo):  cantidad real del activo que Bybit necesita.
                 qty = (margen × leverage) / precio_entrada
                 Ej: 3.75 × 10 / 2500 = 0.015 ETH  ← esto va a Bybit

  ¡NUNCA enviar margen como qty!
  Bybit interpreta qty = unidades del activo, NO USDT.
  Enviar 3.75 como qty en ETH = abrir 3.75 ETH ≈ 9375 USDT → ErrCode 110007.

─── FLUJO COMPLETO ─────────────────────────────────────────────────────────────
  loop_scan_global()            cada 5 min  → scoring → ejecutar_trade()
  loop_confirmaciones()         cada 1 min  → re-evalúa setups en observación
  gestionar_posiciones_activas() cada 1 min → trailing SL, cierres parciales
────────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from bots.trading_bot.zone_flip      import evaluar_zone_flip
from bots.trading_bot.wave_hunt      import evaluar_wave_hunt
from bots.trading_bot.smart_flow     import evaluar_smart_flow
from bots.trading_bot.market_context import get_contexto, actualizar_todos
from bots.trading_bot.bybit_client import (
    ejecutar_orden,
    cerrar_posicion,
    mover_stop_loss,
    bybit_disponible,
    sync_posiciones_abiertas,
    obtener_balance_usdt,
)
from bots.trading_bot.risk_manager import (
    calcular_sl_tp,
    calcular_atr,
    calcular_tamano_posicion,
    calcular_qty,
    calcular_leverage,
    calcular_exposure,
    riesgo_monetario,
    ajustar_sizing_para_minimo,
)

log = logging.getLogger("engine")

# ══════════════════════════════════════════════════════════════════
# CONFIGURACIÓN CENTRAL
# ══════════════════════════════════════════════════════════════════

CRYPTOS = {"BTC", "ETH", "SOL"}
STOCKS  = {"AAPL", "NVDA", "TSLA", "META", "MSFT", "AMZN", "GOOGL"}
# SOL retirado del scan automático (P1-4): 0W/5L en los últimos 35 trades.
# Su volatilidad no encaja con los parámetros actuales de Zone Flip.
# Sigue disponible para consultas manuales (!score SOL) vía main.py.
ACTIVOS = ["BTC", "ETH", "AAPL", "NVDA", "TSLA", "META", "MSFT", "AMZN", "GOOGL"]

# Interruptor de seguridad: las acciones NO se operan hasta activarlo explícitamente
# (poner STOCKS_ENABLED=true en .env). Requiere: (1) firmar el acuerdo de stocks en
# Bybit, (2) validar el scoring con mercado abierto. Con esto en False, el engine
# corre solo cripto y ni siquiera intenta órdenes de acciones.
STOCKS_ENABLED = os.getenv("STOCKS_ENABLED", "false").lower() == "true"
TRAD    = set()

SCORE_EJECUTAR   = 75    # abre trade automáticamente — por debajo se ignora
RR_MINIMO        = 1.8   # R:R mínimo TP2 (objetivo principal)
RR_MINIMO_TP1    = 1.0   # R:R mínimo TP1 (primer cierre parcial — break-even mínimo)
MAX_TRADES       = 5     # trades simultáneos máximos
COOLDOWN_MINUTOS = 30    # minutos entre trades del mismo activo
CAPITAL          = 75.0  # capital total del bot en USDT
RIESGO_TOLERANCIA = 1.5  # riesgo real máximo = margen × 1.5
MARGEN_TOTAL_MAX_PCT = 0.70  # máx 70% del capital comprometido en trades simultáneos

TRADE_LOG_PATH    = Path("trades.json")
MEMORIA_PATH      = Path("memoria_trades.json")
TRADE_COUNTER_PATH = Path("trade_counter.json")

EMOJI_ESTRATEGIA = {
    "zone_flip":  "🎯 Zone Flip",
    "wave_hunt":  "🌊 Wave Hunt",
    "smart_flow": "🔮 Smart Flow",
}


# ══════════════════════════════════════════════════════════════════
# ESTADO GLOBAL
# ══════════════════════════════════════════════════════════════════

class EngineState:
    ANALIZANDO    = "ANALIZANDO"
    EN_TRADE      = "EN_TRADE"
    POST_ANALISIS = "POST_ANALISIS"


estado_engine   = EngineState.ANALIZANDO
trades_abiertos: list[dict] = []
ultimo_trade: dict[str, datetime] = defaultdict(lambda: datetime.min)

# Canal de auditoría (lo setea main.py en on_ready). Si es None, no se postea.
canal_auditoria = None

_confirmaciones: dict[int, dict] = {}  # legacy — ya no se usa


# ══════════════════════════════════════════════════════════════════
# PERSISTENCIA
# ══════════════════════════════════════════════════════════════════

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


def _next_trade_id() -> str:
    """
    Genera el próximo ID de trade en formato #0001, #0002, ...
    Persiste el contador en trade_counter.json.

    Si el archivo no existe, intenta deducir el último ID desde trades.json.
    """
    last_id = 0

    # 1. Leer contador persistente
    if TRADE_COUNTER_PATH.exists():
        try:
            data = json.loads(TRADE_COUNTER_PATH.read_text())
            last_id = int(data.get("last_id", 0))
        except Exception:
            last_id = 0

    # 2. Fallback: deducir desde trades.json si el contador está en 0
    if last_id == 0:
        trades = _cargar_json(TRADE_LOG_PATH)
        for t in trades:
            tid = t.get("trade_id", "")
            if isinstance(tid, str) and tid.startswith("#"):
                try:
                    n = int(tid.lstrip("#"))
                    last_id = max(last_id, n)
                except ValueError:
                    pass

    # 3. Incrementar y persistir
    new_id = last_id + 1
    try:
        TRADE_COUNTER_PATH.write_text(json.dumps({"last_id": new_id}, indent=2))
    except Exception as e:
        log.error(f"Error guardando contador de trades: {e}")

    return f"#{new_id:04d}"


def restaurar_cooldowns():
    """
    Al arrancar, lee trades.json y restaura ultimo_trade para cada activo.
    Evita que el bot abra trades inmediatamente tras un reinicio si hubo
    trades recientes (cerrados manual o automáticamente).
    """
    trades = _cargar_json(TRADE_LOG_PATH)
    if not trades:
        return
    for trade in trades:
        activo = trade.get("activo")
        ts_str = trade.get("timestamp")
        if not activo or not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts > ultimo_trade[activo]:
                ultimo_trade[activo] = ts
        except Exception:
            pass
    activos_con_cd = {a: t for a, t in ultimo_trade.items()
                      if datetime.now() - t < timedelta(minutes=COOLDOWN_MINUTOS)}
    if activos_con_cd:
        for activo, ts in activos_con_cd.items():
            mins = int((timedelta(minutes=COOLDOWN_MINUTOS) - (datetime.now() - ts)).total_seconds() / 60)
            log.info(f"🕐 Cooldown restaurado: {activo} — {mins}m restantes")


def registrar_trade(trade: dict):
    trades = _cargar_json(TRADE_LOG_PATH)
    trades.append(trade)
    _guardar_json(TRADE_LOG_PATH, trades)


def actualizar_trade_en_log(trade: dict):
    """Actualiza el registro de un trade existente en trades.json."""
    trades = _cargar_json(TRADE_LOG_PATH)
    for i, t in enumerate(trades):
        if (t.get("timestamp") == trade.get("timestamp")
                and t.get("activo") == trade.get("activo")):
            trades[i] = trade
            break
    _guardar_json(TRADE_LOG_PATH, trades)


def guardar_aprendizaje(aprendizaje: dict):
    memoria = _cargar_json(MEMORIA_PATH)
    memoria.append(aprendizaje)
    _guardar_json(MEMORIA_PATH, memoria)


# ══════════════════════════════════════════════════════════════════
# PRECIO EN TIEMPO REAL
# ══════════════════════════════════════════════════════════════════

async def _obtener_precio(activo: str) -> float | None:
    """Precio actual via data_provider (Binance → Bybit → CoinGecko)."""
    from bots import data_provider as dp
    precio = await dp.get_precio(activo)
    if not precio:
        log.warning(f"Sin precio disponible para {activo}")
    return precio


# ══════════════════════════════════════════════════════════════════
# SCORING — 3 ESTRATEGIAS EN PARALELO
# ══════════════════════════════════════════════════════════════════

async def scoring_completo(activo: str) -> dict:
    """
    Corre Zone Flip, Wave Hunt y Smart Flow en paralelo.
    Aplica bonus por confluencia (2/3 → +10pts, 3/3 → +20pts).
    Devuelve la mejor señal.
    """
    try:
        zf, wh, sf = await asyncio.gather(
            evaluar_zone_flip(activo),
            evaluar_wave_hunt(activo),
            evaluar_smart_flow(activo),
            return_exceptions=True,
        )
    except Exception as e:
        log.error(f"scoring_completo({activo}): {e}")
        return _señal_vacia(activo)

    def _safe(r, nombre):
        if isinstance(r, Exception):
            log.warning(f"{nombre} error en {activo}: {r}")
            return {"puntuacion": 0, "direccion": None,
                    "estrategia": nombre, "razones": [], "leverage": 2}
        return r

    zf = _safe(zf, "zone_flip")
    wh = _safe(wh, "wave_hunt")
    sf = _safe(sf, "smart_flow")

    estrategias = [zf, wh, sf]

    # ── Confluencia ────────────────────────────────────────────
    dirs = [e["direccion"] for e in estrategias if e.get("direccion")]
    confluencia = 0
    dir_confluencia = None
    if dirs:
        conteo = Counter(dirs)
        dir_confluencia, confluencia = conteo.most_common(1)[0]

    # (P2-7) Bonus de confluencia ELIMINADO (antes: 2/3 → +10, 3/3 → +20).
    # Inflaba señales hasta 100% sin edge validado y, combinado con el
    # SL-por-score (ya corregido), volvía contraproducente la confluencia.
    # La confluencia sigue contando: filtros de shorts/contra-tendencia,
    # sizing y leverage la usan — pero el score refleja solo la señal líder.
    bonus = 0

    if confluencia >= 2 and dir_confluencia:
        candidatas = [e for e in estrategias if e.get("direccion") == dir_confluencia]
    else:
        candidatas = [e for e in estrategias if e.get("puntuacion", 0) > 0]

    if not candidatas:
        return {**_señal_vacia(activo), "zf": zf, "wh": wh, "sf": sf,
                "razones": ["Sin señales"]}

    mejor       = max(candidatas, key=lambda e: (e.get("puntuacion", 0), e.get("leverage", 0)))
    score_final = min(mejor["puntuacion"] + bonus, 100)

    razones = mejor.get("razones", [])
    if confluencia >= 2:
        razones = [f"⚡ CONFLUENCIA {confluencia}/3 → {dir_confluencia}"] + razones

    return {
        "activo":      activo,
        "score":       score_final,
        "direccion":   mejor.get("direccion"),
        "estrategia":  mejor.get("estrategia"),
        "leverage":    mejor.get("leverage", 2),
        "confluencia": confluencia,
        "razones":     razones[:5],
        "zf": zf, "wh": wh, "sf": sf,
    }


def _señal_vacia(activo: str) -> dict:
    return {
        "activo": activo, "score": 0, "direccion": None,
        "estrategia": None, "confluencia": 0,
        "razones": [], "leverage": 2,
        "zf": {}, "wh": {}, "sf": {},
    }


# ══════════════════════════════════════════════════════════════════
# FILTROS DE RIESGO
# ══════════════════════════════════════════════════════════════════

def _puede_operar(activo: str, direccion: str) -> tuple[bool, str]:
    if len(trades_abiertos) >= MAX_TRADES:
        return False, f"Máximo {MAX_TRADES} trades abiertos"

    delta = datetime.now() - ultimo_trade[activo]
    if delta < timedelta(minutes=COOLDOWN_MINUTOS):
        mins = int((timedelta(minutes=COOLDOWN_MINUTOS) - delta).total_seconds() / 60)
        return False, f"Cooldown {activo} — {mins}m restantes"

    for t in trades_abiertos:
        if t["activo"] == activo and t["direccion"] == direccion:
            return False, f"Ya hay trade {direccion} abierto en {activo}"

    return True, "OK"


def _extraer_niveles_tecnicos(señal: dict, precio: float, direccion: str) -> dict:
    """
    Extrae SL/TP ESTRUCTURALES desde las zonas de Zone Flip (fix H1).

    Zone Flip calcula el SL bajo el soporte real y los TPs en la resistencia
    real, pero históricamente esos niveles se descartaban y se operaba con
    porcentajes ciegos. Acá se recuperan y validan contra el precio actual:
    un nivel del lado equivocado del precio (zonas calculadas para la otra
    dirección, datos viejos) se descarta y cae al fallback ATR.

    Returns:
        {"sl": float|None, "tp1": float|None, "tp2": float|None}
    """
    zonas = (señal.get("zf") or {}).get("zonas") or {}
    out = {"sl": None, "tp1": None, "tp2": None}
    if not zonas:
        return out

    sl  = zonas.get("sl_long") if direccion == "LONG" else zonas.get("sl_short")
    tp1 = zonas.get("tp1")
    tp2 = zonas.get("tp2")

    if direccion == "LONG":
        if sl and 0 < sl < precio:
            out["sl"] = sl
        if tp1 and tp1 > precio:
            out["tp1"] = tp1
        if tp2 and tp2 > precio and (not out["tp1"] or tp2 >= out["tp1"]):
            out["tp2"] = tp2
    else:  # SHORT
        if sl and sl > precio:
            out["sl"] = sl
        if tp1 and 0 < tp1 < precio:
            out["tp1"] = tp1
        if tp2 and 0 < tp2 < precio and (not out["tp1"] or tp2 <= out["tp1"]):
            out["tp2"] = tp2
    return out


def _valida_rr(
    precio: float,
    direccion: str,
    score: int,
    señal: dict | None = None,
    atr: float | None = None,
) -> tuple[bool, dict, str]:
    """
    Valida R:R en AMBOS niveles (TP1 y TP2).

    TP1 (1.0+): primer cierre parcial debe al menos cubrir el riesgo.
                Si TP1 R:R < 1.0, la primera salida pierde money.
    TP2 (1.8+): objetivo principal — donde se asegura ganancia real.

    Usa niveles estructurales de Zone Flip si están disponibles (fix H1);
    si no, calcular_sl_tp cae al fallback por ATR (fix H3).

    Returns:
        (ok, riesgo, motivo_rechazo)
    """
    niveles = _extraer_niveles_tecnicos(señal or {}, precio, direccion)
    riesgo  = calcular_sl_tp(
        precio, direccion, score,
        sl_tecnico=niveles["sl"],
        tp1_tecnico=niveles["tp1"],
        tp2_tecnico=niveles["tp2"],
        atr=atr,
    )
    rr_tp1  = riesgo.get("rr_tp1", 0)
    rr_tp2  = riesgo.get("rr", 0)

    if rr_tp1 < RR_MINIMO_TP1:
        return False, riesgo, f"R:R TP1 {rr_tp1:.2f} < {RR_MINIMO_TP1} (primer cierre pierde)"
    if rr_tp2 < RR_MINIMO:
        return False, riesgo, f"R:R TP2 {rr_tp2:.2f} < {RR_MINIMO} (objetivo insuficiente)"
    return True, riesgo, "OK"


def _validar_sizing(
    margen: float, leverage: int | float, precio: float, qty: float
) -> tuple[bool, str]:
    if precio   <= 0: return False, f"Precio inválido: {precio}"
    if leverage <= 0: return False, f"Leverage inválido: {leverage}"
    if margen   <= 0: return False, f"Margen inválido: {margen}"
    if qty      <= 0: return False, f"Qty inválida: {qty}"
    return True, "OK"


# ══════════════════════════════════════════════════════════════════
# EJECUCIÓN DE TRADE
# ══════════════════════════════════════════════════════════════════

async def ejecutar_trade(señal: dict, precio: float, canal_discord) -> bool:
    """
    Ejecuta un trade real o simulado si pasa todos los filtros.

    Flujo de sizing:
        capital → margen (calcular_tamano_posicion)
               → leverage (calcular_leverage — hard-cap 10x)
               → qty (calcular_qty con symbol= para validaciones por instrumento)
               → ejecutar_orden(qty) en Bybit
    """
    global estado_engine

    activo      = señal["activo"]
    direccion   = señal["direccion"]
    score       = señal["score"]
    confluencia = señal.get("confluencia", 0)

    # ── Leverage validado por risk_manager ─────────────────────
    leverage = calcular_leverage(
        score=score,
        confluencia=confluencia,
        leverage_lider=señal.get("leverage", 2),
    )

    puede, motivo = _puede_operar(activo, direccion)
    if not puede:
        log.info(f"⛔ {activo} bloqueado: {motivo}")
        return False

    # ── ATR 2H para el fallback de SL (mismo TF que las zonas de Zone Flip) ──
    atr = None
    try:
        from bots import data_provider as dp
        bars_atr = await dp.get_ohlcv(activo, "2h", 30)
        atr = calcular_atr(bars_atr)
    except Exception as e:
        log.warning(f"⚠️ Sin ATR para {activo}: {e} — fallback % plano")

    rr_ok, riesgo, rr_motivo = _valida_rr(precio, direccion, score, señal, atr)
    if not rr_ok:
        log.info(f"⛔ {activo} {rr_motivo}")
        return False

    # Trazabilidad: de dónde salió el stop (estructura > ATR > % plano)
    sl_origen = (
        "estructura" if riesgo.get("sl_tecnico")
        else ("ATR" if atr else "% plano")
    )
    log.info(
        f"🎚 SL/TP {activo}: SL={riesgo['sl']:,.4f} [{sl_origen}] "
        f"({riesgo['sl_pct']:.2f}%) · TP1={riesgo['tp1']:,.4f} (RR {riesgo['rr_tp1']}) · "
        f"TP2={riesgo['tp2']:,.4f} (RR {riesgo['rr']})"
    )

    # ── Sizing ─────────────────────────────────────────────────
    margen = calcular_tamano_posicion(CAPITAL, score, confluencia)

    # ── Auto-ajuste para cumplir qty mínima del instrumento ────
    # Resuelve casos como BTC: 10 USDT × 4x / 77k = qty 0.0005 < 0.001 mínimo
    # Sube leverage automáticamente (hasta 10x) y luego margen (hasta 15 USDT)
    margen_ajustado, leverage_ajustado, motivo_ajuste = ajustar_sizing_para_minimo(
        margen, leverage, precio, activo
    )
    if motivo_ajuste:
        log.info(f"📐 {activo} sizing ajustado: {motivo_ajuste}")
        margen   = margen_ajustado
        leverage = leverage_ajustado

    try:
        qty = calcular_qty(margen, leverage, precio, symbol=activo)
    except (ValueError, RuntimeError) as e:
        log.warning(f"⛔ {activo} qty inválida — cooldown {COOLDOWN_MINUTOS}m aplicado: {e}")
        ultimo_trade[activo] = datetime.now()  # cooldown completo para evitar spam
        await _notificar_trade_fallido(señal, precio, riesgo, margen, leverage, str(e), canal_discord)
        return False

    ok, msg = _validar_sizing(margen, leverage, precio, qty)
    if not ok:
        log.error(f"⛔ {activo} sizing inválido: {msg}")
        return False

    # ── Guardia de riesgo monetario ────────────────────────────
    rm = riesgo_monetario(precio, riesgo["sl"], qty)
    if rm["ok"] and rm["riesgo_usdt"] > margen * RIESGO_TOLERANCIA:
        log.warning(
            f"⚠️ {activo} riesgo real ${rm['riesgo_usdt']} > "
            f"1.5× margen ${margen} — cancelado"
        )
        return False

    # ── Guardia de margen TOTAL comprometido ───────────────────
    # Con MAX_TRADES=5, evitar que 5 trades × margen_max consuman todo el capital
    margen_comprometido = sum(t.get("margen", 0) for t in trades_abiertos)
    margen_max_permitido = CAPITAL * MARGEN_TOTAL_MAX_PCT
    margen_proyectado    = margen_comprometido + margen

    if margen_proyectado > margen_max_permitido:
        log.warning(
            f"⛔ {activo} bloqueado por margen total: "
            f"actual ${margen_comprometido:.2f} + nuevo ${margen:.2f} = ${margen_proyectado:.2f} "
            f"> límite ${margen_max_permitido:.2f} ({MARGEN_TOTAL_MAX_PCT:.0%} de ${CAPITAL})"
        )
        return False

    log.info(
        f"💼 Margen total: comprometido ${margen_comprometido:.2f} + "
        f"nuevo ${margen:.2f} = ${margen_proyectado:.2f} / ${margen_max_permitido:.2f}"
    )

    # ── Validar balance disponible en Bybit (evita error 110007) ─
    if bybit_disponible():
        try:
            bal = await asyncio.to_thread(obtener_balance_usdt)
            if bal.get("ok"):
                available = bal.get("available", 0)
                # Buffer 10% para fees y slippage
                margen_necesario = margen * 1.10
                if available < margen_necesario:
                    log.warning(
                        f"⛔ {activo} balance insuficiente: "
                        f"disponible ${available:.2f} < requerido ${margen_necesario:.2f} "
                        f"(margen ${margen:.2f} + 10% buffer)"
                    )
                    ultimo_trade[activo] = datetime.now()  # cooldown completo
                    return False
                log.info(
                    f"💰 Balance OK: ${available:.2f} disponible · "
                    f"requerido ${margen_necesario:.2f}"
                )
            else:
                log.warning(f"⚠️ No se pudo verificar balance: {bal.get('error')}")
        except Exception as e:
            log.warning(f"⚠️ Error verificando balance: {e} — continuando con ejecución")

    log.info(
        f"📐 Sizing {activo}: margen={margen:.2f} USDT | "
        f"lev={leverage}x | precio={precio:,.4f} | qty={qty}"
    )

    # ── Construir trade ────────────────────────────────────────
    trade = {
        "trade_id":     _next_trade_id(),
        "activo":       activo,
        "estrategia":   señal["estrategia"],
        "direccion":    direccion,
        "entrada":      precio,
        # Niveles SL/TP
        "sl":           riesgo["sl"],
        "tp1":          riesgo["tp1"],
        "tp2":          riesgo["tp2"],
        "tp3":          riesgo["tp3"],
        "tp":           riesgo["tp"],          # compatibilidad
        "sl_tras_tp1":  riesgo["sl_tras_tp1"],
        "sl_tras_tp2":  riesgo["sl_tras_tp2"],
        "current_sl":   riesgo["sl"],          # SL vigente (se actualiza)
        "fase_tp":      0,                     # 0=inicial,1=tras TP1,2=tras TP2
        # Sizing
        "score":        score,
        "leverage":     leverage,
        "confluencia":  confluencia,
        "margen":       margen,
        "qty":          qty,
        "qty_restante": qty,                   # se reduce con cierres parciales
        # Meta
        "razones":      señal["razones"][:3],
        "timestamp":    datetime.now().isoformat(),
        "estado":       "ABIERTO",
        # Detalle estrategias para Discord
        "zf": señal.get("zf", {}),
        "wh": señal.get("wh", {}),
        "sf": señal.get("sf", {}),
    }

    # ── Enviar a Bybit ─────────────────────────────────────────
    resultado = await asyncio.to_thread(
        ejecutar_orden,
        activo, direccion, qty,
        riesgo["sl"], riesgo["tp2"],   # Bybit usa TP2 como target principal
        leverage, precio,
    )
    # ── Abortar si la orden real fue RECHAZADA por Bybit ───────
    # (paper → simulado=True; éxito real → ok=True; cualquier otra cosa = fallo)
    if not resultado.get("simulado") and not resultado.get("ok"):
        log.error(
            f"⛔ {activo} orden RECHAZADA por Bybit — NO se registra el trade: "
            f"{resultado.get('estado')} · {resultado.get('error')}"
        )
        ultimo_trade[activo] = datetime.now()  # cooldown para no spamear el mismo rechazo
        await _notificar_trade_fallido(
            señal, precio, riesgo, margen, leverage,
            f"Bybit rechazó la orden: {resultado.get('error')}", canal_discord,
        )
        return False

    modo            = "SIMULADO" if resultado.get("simulado") else "EJECUTADO"
    trade["modo"]     = modo
    trade["order_id"] = resultado.get("order_id")

    trades_abiertos.append(trade)
    ultimo_trade[activo] = datetime.now()
    registrar_trade(trade)
    estado_engine = EngineState.EN_TRADE

    await _notificar_trade_abierto(trade, canal_discord)
    log.info(
        f"✅ TRADE {trade['trade_id']} {modo}: {activo} {direccion} score={score} | "
        f"margen={margen:.2f} USDT | lev={leverage}x | qty={qty} | "
        f"entrada={precio:,.4f}"
    )
    return True


# ── Stubs para compatibilidad con imports legados ─────────────────────────────

async def aprobar_confirmacion(message_id: int) -> bool:
    return False


async def cancelar_confirmacion(message_id: int) -> bool:
    return False


# ══════════════════════════════════════════════════════════════════
# GESTIÓN ACTIVA — TRAILING SL + CIERRES PARCIALES POR TP
# ══════════════════════════════════════════════════════════════════

async def gestionar_posiciones_activas(canal_discord) -> None:
    """
    Ejecutar cada ~1 minuto mientras hay trades abiertos.

    Por cada trade:
      fase 0 → verifica SL o TP1 (cierra 40%, SL → breakeven)
      fase 1 → verifica SL o TP2 (cierra 35%, SL → TP1)
      fase 2 → verifica SL o TP3 (cierra 25% restante)
    """
    global trades_abiertos, estado_engine

    if not trades_abiertos:
        estado_engine = EngineState.ANALIZANDO
        return

    log.info(f"📊 Gestionando {len(trades_abiertos)} trade(s) activos")

    # ── Sync con Bybit: detectar cierres externos (manual, SL, liquidación) ──
    activos_en_bybit: set[str] | None = None
    if bybit_disponible():
        try:
            posiciones_bybit = await asyncio.to_thread(sync_posiciones_abiertas)
            activos_en_bybit = {p["activo"] for p in posiciones_bybit}
        except Exception as e:
            log.warning(f"⚠️ No se pudo sincronizar con Bybit: {e}")

    for trade in list(trades_abiertos):
        activo = trade.get("activo", "UNKNOWN")
        try:
            precio_actual = await _obtener_precio(activo)
            if not precio_actual:
                log.warning(f"Sin precio para gestionar {activo}")
                continue

            # ── Detectar cierre externo (Bybit cerró la posición) ─
            if activos_en_bybit is not None and activo not in activos_en_bybit:
                log.info(
                    f"🔴 {activo} ya no está en Bybit — cerrado externamente "
                    f"(manual/liquidación/SL exchange) @ {precio_actual:,.4f}"
                )
                await analizar_post_trade(
                    trade, precio_actual, canal_discord,
                    motivo_cierre="EXTERNO_BYBIT",
                )
                continue

            direccion    = trade["direccion"]
            fase         = trade.get("fase_tp", 0)
            current_sl   = trade.get("current_sl", trade["sl"])
            qty_restante = trade.get("qty_restante", trade["qty"])

            # ── SL tocado → cierre total ───────────────────────
            sl_tocado = (
                precio_actual <= current_sl if direccion == "LONG"
                else precio_actual >= current_sl
            )
            if sl_tocado:
                log.info(f"🛑 SL tocado en {activo} @ {precio_actual}")
                await asyncio.to_thread(cerrar_posicion, activo, direccion)
                await analizar_post_trade(
                    trade, precio_actual, canal_discord,
                    motivo_cierre="SL_JARVIS",
                )
                continue

            # ── Trades recuperados del sync: solo monitorear SL ─
            if trade.get("estrategia") == "sync":
                log.info(
                    f"👁 {activo} [sync] precio={precio_actual:,.4f} | "
                    f"SL={current_sl:,.4f} — trailing pausado hasta nueva señal"
                )
                continue

            # ── TP1 — 40% ─────────────────────────────────────
            if fase == 0:
                tp1_tocado = (
                    precio_actual >= trade["tp1"] if direccion == "LONG"
                    else precio_actual <= trade["tp1"]
                )
                if tp1_tocado:
                    qty_tp1  = round(trade["qty"] * 0.40, 6)
                    nuevo_sl = trade["sl_tras_tp1"]    # breakeven

                    await asyncio.to_thread(cerrar_posicion, activo, direccion, qty_tp1)
                    await asyncio.to_thread(mover_stop_loss, activo, direccion, nuevo_sl)

                    trade["fase_tp"]      = 1
                    trade["current_sl"]   = nuevo_sl
                    trade["qty_restante"] = round(qty_restante - qty_tp1, 6)
                    actualizar_trade_en_log(trade)

                    log.info(f"🎯 TP1 {activo}: cerrado {qty_tp1} (40%) | SL → {nuevo_sl}")
                    await _notificar_tp_parcial(
                        trade, precio_actual, 1, qty_tp1, nuevo_sl, canal_discord
                    )
                    continue

            # ── TP2 — 35% ─────────────────────────────────────
            if fase == 1:
                tp2_tocado = (
                    precio_actual >= trade["tp2"] if direccion == "LONG"
                    else precio_actual <= trade["tp2"]
                )
                if tp2_tocado:
                    qty_tp2  = round(trade["qty"] * 0.35, 6)
                    nuevo_sl = trade["sl_tras_tp2"]    # SL → TP1

                    await asyncio.to_thread(cerrar_posicion, activo, direccion, qty_tp2)
                    await asyncio.to_thread(mover_stop_loss, activo, direccion, nuevo_sl)

                    trade["fase_tp"]      = 2
                    trade["current_sl"]   = nuevo_sl
                    trade["qty_restante"] = round(qty_restante - qty_tp2, 6)
                    actualizar_trade_en_log(trade)

                    log.info(f"🎯 TP2 {activo}: cerrado {qty_tp2} (35%) | SL → {nuevo_sl}")
                    await _notificar_tp_parcial(
                        trade, precio_actual, 2, qty_tp2, nuevo_sl, canal_discord
                    )
                    continue

            # ── TP3 — 25% restante → cierre total ─────────────
            if fase == 2:
                tp3_tocado = (
                    precio_actual >= trade["tp3"] if direccion == "LONG"
                    else precio_actual <= trade["tp3"]
                )
                if tp3_tocado:
                    log.info(f"🎯 TP3 {activo}: cierre total")
                    await asyncio.to_thread(cerrar_posicion, activo, direccion)
                    await analizar_post_trade(
                        trade, precio_actual, canal_discord,
                        motivo_cierre="TP3_JARVIS",
                    )
                    continue

            # ── Sin evento: log de seguimiento ────────────────
            # P&L no realizado (informativo)
            pnl_pct = (
                (precio_actual - trade["entrada"]) / trade["entrada"] * 100
                if direccion == "LONG"
                else (trade["entrada"] - precio_actual) / trade["entrada"] * 100
            )
            log.info(
                f"📈 {activo} {direccion} | precio={precio_actual:,.4f} | "
                f"SL={current_sl:,.4f} | fase_tp={fase} | "
                f"qty_restante={qty_restante} | PnL={pnl_pct:+.2f}%"
            )

            # ── Tracking informativo del score actual (NO actuar) ──
            # Regla: Una vez abierto el trade, el score es irrelevante para gestión.
            # Solo SL/TP definen salidas. Esto es contexto puro, sin acción.
            try:
                señal_actual = await scoring_completo(activo)
                score_orig   = trade.get("score", 0)
                score_actual = señal_actual.get("score", 0)
                dir_actual   = señal_actual.get("direccion") or "—"
                delta_score  = score_actual - score_orig

                # Alerta visual según deterioro/mejora (puramente informativo)
                if delta_score <= -20:
                    emoji = "🔻"
                elif delta_score <= -10:
                    emoji = "⚠️"
                elif delta_score >= 10:
                    emoji = "📈"
                else:
                    emoji = "📊"

                # Aviso especial si la dirección actual contradice el trade
                contradice = (
                    dir_actual in ("LONG", "SHORT")
                    and dir_actual != direccion
                )
                contra_str = " · ⚠️ dirección actual CONTRADICE" if contradice else ""

                log.info(
                    f"{emoji} [{activo}] setup score: original={score_orig}% → "
                    f"actual={score_actual}% (Δ{delta_score:+d}) dir_actual={dir_actual}"
                    f"{contra_str} — INFORMATIVO, no se actúa"
                )
            except Exception as score_err:
                log.debug(f"No se pudo recalcular score para {activo}: {score_err}")

        except Exception as e:
            log.error(f"Error gestionando {activo}: {e}")

    estado_engine = EngineState.EN_TRADE if trades_abiertos else EngineState.ANALIZANDO


# ══════════════════════════════════════════════════════════════════
# POST-TRADE ANALYSIS
# ══════════════════════════════════════════════════════════════════

async def analizar_post_trade(
    trade: dict,
    precio_cierre: float,
    canal_discord,
    motivo_cierre: str | None = None,
) -> dict:
    """
    Registra resultado, guarda aprendizaje y notifica Discord.
    Elimina el trade de trades_abiertos.

    Args:
        motivo_cierre: identificador de quién/cómo cerró el trade
            - "SL_JARVIS"       → Jarvis ejecutó cierre por SL
            - "TP1_JARVIS"      → Jarvis ejecutó cierre por TP1
            - "TP2_JARVIS"      → Jarvis ejecutó cierre por TP2
            - "TP3_JARVIS"      → Jarvis ejecutó cierre por TP3
            - "EXTERNO_BYBIT"   → Cerrado fuera de Jarvis (manual usuario / SL Bybit / liquidación)
            - "MANUAL_DISCORD"  → Cerrado vía comando !cerrar de Discord
            - None              → Se infiere desde precio vs niveles
    """
    global estado_engine

    entrada   = trade["entrada"]
    direccion = trade["direccion"]

    ganancia_pct = (
        (precio_cierre - entrada) / entrada * 100
        if direccion == "LONG"
        else (entrada - precio_cierre) / entrada * 100
    )
    gano = ganancia_pct > 0

    toco_sl  = (precio_cierre <= trade["sl"]  if direccion == "LONG" else precio_cierre >= trade["sl"])
    toco_tp1 = (precio_cierre >= trade.get("tp1", 0) if direccion == "LONG" else precio_cierre <= trade.get("tp1", 0))
    toco_tp2 = (precio_cierre >= trade.get("tp2", 0) if direccion == "LONG" else precio_cierre <= trade.get("tp2", 0))
    toco_tp3 = (precio_cierre >= trade.get("tp3", 0) if direccion == "LONG" else precio_cierre <= trade.get("tp3", 0))

    pnl_usdt = round(
        abs(precio_cierre - entrada) * trade.get("qty", 0) * (1 if gano else -1), 4
    )

    # ── Inferir motivo si no se pasó explícito ──────────────────
    if motivo_cierre is None:
        if toco_sl:
            motivo_cierre = "SL_JARVIS"
        elif toco_tp3:
            motivo_cierre = "TP3_JARVIS"
        elif toco_tp2:
            motivo_cierre = "TP2_JARVIS"
        elif toco_tp1:
            motivo_cierre = "TP1_JARVIS"
        else:
            motivo_cierre = "EXTERNO_BYBIT"

    # ── Análisis CONTEXTUAL para la lección ─────────────────────
    leccion = _generar_leccion_contextual(
        trade, precio_cierre, ganancia_pct, gano,
        toco_sl, toco_tp1, toco_tp2, toco_tp3, motivo_cierre,
    )

    # ── Etiqueta legible del motivo ────────────────────────────
    motivo_legible = {
        "SL_JARVIS":      "🛑 SL ejecutado por Jarvis",
        "TP1_JARVIS":     "🎯 TP1 ejecutado por Jarvis",
        "TP2_JARVIS":     "🎯🎯 TP2 ejecutado por Jarvis",
        "TP3_JARVIS":     "🎯🎯🎯 TP3 ejecutado por Jarvis",
        "EXTERNO_BYBIT":  "👤 Cerrado externamente (vos en Bybit, liquidación o SL exchange)",
        "MANUAL_DISCORD": "💬 Cerrado por vos vía comando Discord",
    }.get(motivo_cierre, f"❓ {motivo_cierre}")

    aprendizaje = {
        "trade_id":       trade.get("trade_id"),
        "timestamp":      datetime.now().isoformat(),
        "activo":         trade["activo"],
        "estrategia":     trade["estrategia"],
        "direccion":      direccion,
        "entrada":        entrada,
        "cierre":         precio_cierre,
        "ganancia_pct":   round(ganancia_pct, 2),
        "pnl_usdt":       pnl_usdt,
        "resultado":      "WIN" if gano else "LOSS",
        "fase_tp_final":  trade.get("fase_tp", 0),
        "toco_sl":        toco_sl,
        "toco_tp1":       toco_tp1,
        "toco_tp2":       toco_tp2,
        "toco_tp3":       toco_tp3,
        "motivo_cierre":  motivo_cierre,
        "score_original": trade["score"],
        "confluencia":    trade.get("confluencia", 0),
        "margen_usdt":    trade.get("margen"),
        "qty_activo":     trade.get("qty"),
        "leverage":       trade.get("leverage"),
        "modo":           trade.get("modo"),
        "notas": {
            "sl_correcto":      not toco_sl or (toco_sl and not gano),
            "entrada_correcta": gano,
            "leccion":          leccion,
        },
    }

    guardar_aprendizaje(aprendizaje)

    trade["estado"]        = "CERRADO"
    trade["precio_cierre"] = precio_cierre
    trade["ganancia_pct"]  = round(ganancia_pct, 2)
    trade["pnl_usdt"]      = pnl_usdt
    trade["motivo_cierre"] = motivo_cierre
    actualizar_trade_en_log(trade)

    if trade in trades_abiertos:
        trades_abiertos.remove(trade)

    estado_engine = EngineState.POST_ANALISIS if trades_abiertos else EngineState.ANALIZANDO

    # ── Discord ────────────────────────────────────────────────
    emoji  = "✅" if gano else "❌"
    trade_id = trade.get("trade_id", "")
    id_str   = f" `{trade_id}`" if trade_id else ""

    # Duración del trade
    try:
        ts_apertura = datetime.fromisoformat(trade.get("timestamp", ""))
        duracion    = datetime.now() - ts_apertura
        horas       = int(duracion.total_seconds() // 3600)
        minutos     = int((duracion.total_seconds() % 3600) // 60)
        duracion_str = f"{horas}h {minutos}m" if horas > 0 else f"{minutos}m"
    except Exception:
        duracion_str = "?"

    msg = (
        f"{emoji} **POST-TRADE{id_str} — {trade['activo']}**\n"
        f"Resultado: `{'WIN' if gano else 'LOSS'}` · Duración: `{duracion_str}`\n"
        f"Cierre por: {motivo_legible}\n"
        f"P&L: `{ganancia_pct:+.2f}%` · `{pnl_usdt:+.4f} USDT`\n"
        f"Entrada: `${entrada:,.4f}` → Cierre: `${precio_cierre:,.4f}`\n\n"
        f"📊 **Análisis:**\n{leccion}"
    )
    try:
        await canal_discord.send(msg)
    except Exception as e:
        log.error(f"Error notificando post-trade: {e}")

    log.info(
        f"Post-trade {trade_id} {trade['activo']}: {aprendizaje['resultado']} "
        f"{ganancia_pct:+.2f}% | {pnl_usdt:+.4f} USDT | motivo={motivo_cierre}"
    )

    # ── Reporte de auditoría global al canal dedicado ──────────
    # Cada cierre actualiza el panorama global: stats + cómo impactó este trade.
    if canal_auditoria is not None:
        try:
            await _postear_auditoria_cierre(aprendizaje, canal_auditoria)
        except Exception as e:
            log.error(f"Error posteando auditoría de cierre: {e}")

    return aprendizaje


async def _postear_auditoria_cierre(aprendizaje: dict, canal) -> None:
    """
    Postea al canal de auditoría tras cada cierre:
      1. Cómo impactó este trade puntual
      2. Stats globales actualizadas (incluyendo este trade)
      3. Opinión del veterano sobre el global actualizado
    """
    from bots.trading_bot.auditor import (
        auditar_global, formatear_global_discord, construir_prompt_global
    )

    a = auditar_global()
    if not a.get("ok"):
        return

    # ── 1. Encabezado: el trade que acaba de cerrar ────────────
    tid     = aprendizaje.get("trade_id", "")
    activo  = aprendizaje.get("activo", "?")
    direc   = aprendizaje.get("direccion", "?")
    res     = aprendizaje.get("resultado", "?")
    pnl     = aprendizaje.get("pnl_usdt", 0)
    gana    = aprendizaje.get("ganancia_pct", 0)
    emoji   = "✅" if res == "WIN" else "❌"

    header = (
        f"🔔 **CIERRE REGISTRADO {tid} — {activo} {direc}**\n"
        f"{emoji} {res} · `{gana:+.2f}%` · `{pnl:+.4f} USDT`\n"
        f"{'═'*40}\n"
        f"📊 **PANORAMA GLOBAL ACTUALIZADO**"
    )
    await canal.send(header)

    # ── 2. Stats globales (datos puros) ────────────────────────
    await _enviar_largo(canal, formatear_global_discord(a))

    # ── 3. Opinión del veterano sobre el global ────────────────
    try:
        from ollama_client import analizar_async
        prompt = construir_prompt_global(a)
        # Agregar contexto del trade recién cerrado al prompt
        prompt += (
            f"\n\nNOTA: El último trade cerrado fue {tid} ({activo} {direc}), "
            f"resultado {res} con {gana:+.2f}%. "
            f"Mencioná brevemente cómo este cierre afecta el panorama."
        )
        opinion = await analizar_async(prompt)
        await _enviar_largo(canal, opinion, "💼 **Veterano:**\n")
    except Exception as e:
        log.error(f"Error opinión auditoría cierre: {e}")


async def _enviar_largo(canal, texto: str, prefix: str = "") -> None:
    """Envía texto a Discord partiéndolo si supera 1900 caracteres."""
    if not texto or not texto.strip():
        return
    contenido = prefix + texto if prefix else texto
    if len(contenido) <= 1900:
        await canal.send(contenido)
    else:
        for i in range(0, len(contenido), 1900):
            await canal.send(contenido[i:i + 1900])


def _generar_leccion_contextual(
    trade:       dict,
    precio_cierre: float,
    ganancia_pct:  float,
    gano:          bool,
    toco_sl:       bool,
    toco_tp1:      bool,
    toco_tp2:      bool,
    toco_tp3:      bool,
    motivo_cierre: str,
) -> str:
    """
    Genera un análisis específico del trade, no genérico.
    Combina varios datos: motivo, fase, distancia a TP/SL, score, confluencia, etc.
    """
    entrada   = trade["entrada"]
    direccion = trade["direccion"]
    sl_orig   = trade.get("sl", 0)
    tp1       = trade.get("tp1", 0)
    tp2       = trade.get("tp2", 0)
    tp3       = trade.get("tp3", 0)
    score     = trade.get("score", 0)
    conf      = trade.get("confluencia", 0)
    estrategia = trade.get("estrategia", "?")

    lines: list[str] = []

    # ── 1. Caso: TODOS los TPs alcanzados (best case) ──────────
    if gano and toco_tp3:
        lines.append(f"🌟 Trade ejemplar: TP1 → TP2 → TP3 alcanzados.")
        lines.append(f"  • Setup {estrategia} con score {score}% se validó completamente.")
        lines.append(f"  • Movimiento total: {abs(ganancia_pct):.2f}% en la dirección esperada.")
        if conf >= 2:
            lines.append(f"  • Confluencia {conf}/3 confirmó la calidad del setup.")
        return "\n".join(lines)

    # ── 2. Caso: TP parciales (TP2 o TP1) ──────────────────────
    if gano and toco_tp2:
        lines.append(f"✅ Trade exitoso parcial: alcanzó TP2.")
        lines.append(f"  • 75% de posición cerrada con ganancia (40% en TP1 + 35% en TP2).")
        lines.append(f"  • Faltó TP3 a ${tp3:,.4f} ({abs(tp3-precio_cierre)/precio_cierre*100:.2f}% más).")
        lines.append(f"  • SL trailing protegió la ganancia en TP1.")
        return "\n".join(lines)

    if gano and toco_tp1:
        lines.append(f"✅ Trade rentable parcial: alcanzó TP1 (40% cerrado).")
        lines.append(f"  • Faltó TP2 a ${tp2:,.4f} ({abs(tp2-precio_cierre)/precio_cierre*100:.2f}% más).")
        if motivo_cierre == "SL_JARVIS":
            lines.append(f"  • SL en breakeven se activó después del TP1 — riesgo recuperado.")
        else:
            lines.append(f"  • Cerrado antes de TP2 pero protegido por SL breakeven.")
        return "\n".join(lines)

    # ── 3. Caso: SL ejecutado por Jarvis ───────────────────────
    if motivo_cierre == "SL_JARVIS":
        distancia_sl = abs(sl_orig - entrada) / entrada * 100
        distancia_tp1 = abs(tp1 - entrada) / entrada * 100
        lines.append(f"🛑 Stop Loss activado por Jarvis.")
        lines.append(f"  • SL a {distancia_sl:.2f}% de la entrada. TP1 estaba a {distancia_tp1:.2f}%.")
        lines.append(f"  • Score original {score}% (conf {conf}/3) {estrategia} no se materializó.")

        # Análisis del por qué
        if conf >= 2:
            lines.append(f"  ⚠️ Confluencia {conf}/3 era alta pero falló — revisar contexto macro.")
        else:
            lines.append(f"  💡 Señal única {estrategia} sin confluencia — más vulnerable a ruido.")

        if score < 80:
            lines.append(f"  💡 Score {score}% (no muy alto) — considerar exigir 80%+ para esta estrategia.")
        return "\n".join(lines)

    # ── 4. Caso: cerrado externamente (Bybit/manual) ───────────
    if motivo_cierre == "EXTERNO_BYBIT":
        # ¿Cuán cerca estaba del TP1?
        dist_tp1 = (abs(tp1 - precio_cierre) / precio_cierre * 100) if tp1 else 0
        dist_sl  = (abs(sl_orig - precio_cierre) / precio_cierre * 100) if sl_orig else 0
        avanzo = ganancia_pct >= 0
        lines.append(f"👤 Cerrado FUERA de Jarvis (vos en Bybit, liquidación o SL exchange).")

        if avanzo:
            lines.append(f"  • El trade iba a favor (+{ganancia_pct:.2f}%) pero se cortó.")
            if dist_tp1 < 0.5:
                lines.append(f"  💡 Estabas a solo {dist_tp1:.2f}% del TP1 — Jarvis lo habría capturado.")
            elif dist_tp1 < 2:
                lines.append(f"  💡 TP1 estaba cerca ({dist_tp1:.2f}%) — dar más tiempo al setup.")
        else:
            lines.append(f"  • El trade iba en contra ({ganancia_pct:.2f}%) pero NO tocó SL ({dist_sl:.2f}% restante).")
            lines.append(f"  💡 Cerrar manual antes del SL = no respetar plan. SL existe por algo.")

        if score >= 80:
            lines.append(f"  ✅ Score {score}% era sólido — confiar más en el setup la próxima.")
        return "\n".join(lines)

    if motivo_cierre == "MANUAL_DISCORD":
        lines.append(f"💬 Cerrado manual desde Discord (!cerrar).")
        lines.append(f"  • P&L final: {ganancia_pct:+.2f}%. Motivo: decisión humana.")
        return "\n".join(lines)

    # ── 5. Fallback genérico ────────────────────────────────────
    lines.append(f"Trade cerrado sin tocar SL ni TP definidos.")
    lines.append(f"  • Resultado: {ganancia_pct:+.2f}% · Motivo: {motivo_cierre}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
# MENSAJES DISCORD
# ══════════════════════════════════════════════════════════════════

async def _notificar_trade_fallido(
    señal: dict,
    precio: float,
    riesgo: dict,
    margen: float,
    leverage: int,
    motivo: str,
    canal,
) -> None:
    """Notifica un trade que no pudo abrirse por capital insuficiente."""
    activo    = señal["activo"]
    direccion = señal["direccion"]
    score     = señal["score"]
    conf      = señal.get("confluencia", 0)
    icono     = "🚀" if direccion == "LONG" else "🩸"
    emoji_dir = "🟢 LONG" if direccion == "LONG" else "🔴 SHORT"
    estrategia_nombre = EMOJI_ESTRATEGIA.get(señal.get("estrategia", ""), "📊 Estrategia")

    conf_str = (
        "⚡⚡ **CONFLUENCIA MÁXIMA — 3/3 estrategias**\n" if conf >= 3
        else "⚡ **CONFLUENCIA — 2/3 estrategias**\n" if conf == 2
        else ""
    )

    razones_str = "\n".join([f"  • {r}" for r in señal.get("razones", [])])
    exposure    = calcular_exposure(margen, leverage)

    msg = (
        f"@everyone\n"
        f"{icono} **TRADE INTENTADO — {activo}** ⚠️ NO EJECUTADO\n"
        f"{conf_str}"
        f"📍 {emoji_dir} · {estrategia_nombre} · Score `{score}%`\n\n"
        f"💰 Precio ref:  `${precio:,.4f}`\n"
        f"💼 Margen calc: `${margen:.2f} USDT`\n"
        f"📊 Exposure:    `${exposure:.2f} USDT` ({leverage}× apalancado)\n\n"
        f"🛡️ SL:  `${riesgo.get('sl', 0):,.4f}`\n"
        f"🎯 TP1: `${riesgo.get('tp1', 0):,.4f}`\n"
        f"🎯 TP2: `${riesgo.get('tp2', 0):,.4f}`\n"
        f"🎯 TP3: `${riesgo.get('tp3', 0):,.4f}`\n\n"
        f"❌ **Por qué no abrió:**\n"
        f"  Capital insuficiente para el mínimo de Bybit — {motivo}\n\n"
        f"📝 Señales:\n{razones_str}"
    )
    try:
        await canal.send(msg)
    except Exception as e:
        log.error(f"Error notificando trade fallido: {e}")


async def _notificar_trade_abierto(trade: dict, canal) -> None:
    """Mensaje completo con las 3 estrategias, sizing y niveles TP."""
    activo    = trade["activo"]
    direccion = trade["direccion"]
    conf      = trade.get("confluencia", 0)
    modo_str  = "🔵 SIMULADO" if trade.get("modo") == "SIMULADO" else "🟠 REAL"
    icono     = "🚀" if direccion == "LONG" else "🩸"
    emoji_dir = "🟢 LONG" if direccion == "LONG" else "🔴 SHORT"

    conf_str = (
        "⚡⚡ **CONFLUENCIA MÁXIMA — 3/3 estrategias**\n" if conf >= 3
        else "⚡ **CONFLUENCIA — 2/3 estrategias**\n" if conf == 2
        else ""
    )

    zf = trade.get("zf", {})
    wh = trade.get("wh", {})
    sf = trade.get("sf", {})

    estrategias_str = (
        f"🎯 Zone Flip:  {zf.get('direccion') or '—'} ({zf.get('puntuacion', 0)}%) lev {zf.get('leverage', 0)}x\n"
        f"🌊 Wave Hunt:  {wh.get('direccion') or '—'} ({wh.get('puntuacion', 0)}%) lev {wh.get('leverage', 0)}x\n"
        f"🔮 Smart Flow: {sf.get('direccion') or '—'} ({sf.get('puntuacion', 0)}%) lev {sf.get('leverage', 0)}x"
    )

    razones_str = "\n".join([f"  • {r}" for r in trade.get("razones", [])])
    exposure    = calcular_exposure(trade["margen"], trade["leverage"])
    estrategia_nombre = EMOJI_ESTRATEGIA.get(trade.get("estrategia", ""), "📊 Estrategia")

    trade_id = trade.get("trade_id", "")
    id_str   = f" `{trade_id}`" if trade_id else ""

    msg = (
        f"@everyone\n"
        f"{icono} **TRADE{id_str} {trade.get('modo', 'EJECUTADO')} — {activo}** {modo_str}\n"
        f"{conf_str}"
        f"📍 {emoji_dir} · {estrategia_nombre} · Score `{trade['score']}%`\n\n"
        f"💰 Entrada:  `${trade['entrada']:,.4f}`\n"
        f"💼 Margen:   `${trade['margen']:.2f} USDT`\n"
        f"📊 Exposure: `${exposure:.2f} USDT` ({trade['leverage']}× apalancado)\n"
        f"📦 Qty:      `{trade['qty']} {activo}`\n\n"
        f"🛡️ SL:  `${trade['sl']:,.4f}`\n"
        f"🎯 TP1: `${trade['tp1']:,.4f}` → 40% · SL pasa a breakeven\n"
        f"🎯 TP2: `${trade['tp2']:,.4f}` → 35% · SL pasa a TP1\n"
        f"🎯 TP3: `${trade['tp3']:,.4f}` → 25% · cierre total\n\n"
        f"📋 Estrategias:\n{estrategias_str}\n\n"
        f"📝 Señales:\n{razones_str}"
    )
    try:
        await canal.send(msg)
    except Exception as e:
        log.error(f"Error notificando trade abierto: {e}")


async def _notificar_tp_parcial(
    trade: dict,
    precio: float,
    nivel: int,
    qty_cerrada: float,
    nuevo_sl: float,
    canal,
) -> None:
    """Notifica cierre parcial al alcanzar TP1 o TP2."""
    activo    = trade["activo"]
    direccion = trade["direccion"]
    pct_map   = {1: 40, 2: 35, 3: 25}
    pct       = pct_map.get(nivel, 0)

    ganancia_pct = (
        (precio - trade["entrada"]) / trade["entrada"] * 100
        if direccion == "LONG"
        else (trade["entrada"] - precio) / trade["entrada"] * 100
    )
    pnl_parcial = round(abs(precio - trade["entrada"]) * qty_cerrada, 4)

    trade_id = trade.get("trade_id", "")
    id_str   = f" `{trade_id}`" if trade_id else ""

    msg = (
        f"🎯 **TP{nivel} ALCANZADO{id_str} — {activo}**\n"
        f"Precio: `${precio:,.4f}` · Ganancia parcial: `+{ganancia_pct:.2f}%`\n"
        f"Cerrado: `{qty_cerrada} {activo}` ({pct}%) · P&L: `+{pnl_parcial} USDT`\n"
        f"SL movido a: `${nuevo_sl:,.4f}` "
        f"{'(breakeven — riesgo 0)' if nivel == 1 else '(TP1 asegurado)'}\n"
        f"Qty restante: `{trade.get('qty_restante', '?')} {activo}`"
    )
    try:
        await canal.send(msg)
    except Exception as e:
        log.error(f"Error notificando TP{nivel}: {e}")


# ══════════════════════════════════════════════════════════════════
# LOOPS PRINCIPALES
# ══════════════════════════════════════════════════════════════════

async def loop_actualizar_contexto() -> None:
    """
    Actualiza el contexto macro (Daily + 4H) de todos los activos.
    Llamar cada hora desde el orquestador.
    """
    log.info("🌐 Actualizando contexto macro...")
    try:
        activos_ctx = ACTIVOS if STOCKS_ENABLED else [a for a in ACTIVOS if a not in STOCKS]
        ctx = await actualizar_todos(activos_ctx)
        for activo, c in ctx.items():
            log.info(
                f"  [{activo}] tendencia={c['tendencia']} "
                f"sesgo={c['sesgo']} RSI_d={c.get('rsi_diario')}"
            )
    except Exception as e:
        log.error(f"Error actualizando contexto macro: {e}")


async def loop_scan_global(canal_logs, canal_talk) -> None:
    """
    Scan principal — llamar cada 5 minutos desde el orquestador.
    Score ≥ 75 → ejecuta automáticamente. Por debajo se ignora.
    Señales contrarias al sesgo macro (Daily/4H) son bloqueadas.
    """
    global estado_engine
    estado_engine = EngineState.ANALIZANDO
    log.info(f"🔍 Scan global — {len(ACTIVOS)} activos")

    # ── Bias macro global desde Wave Hunt sobre BTC ───────────────
    # Doctrina: "Si BTC está en Wave 3/C bajista → bias SHORT para todo el mercado"
    bias_wave_hunt = "NEUTRAL"
    try:
        wh_btc = await evaluar_wave_hunt("BTC")
        wh_dir = wh_btc.get("direccion")
        wh_score = wh_btc.get("puntuacion", 0) or wh_btc.get("score", 0)
        wh_pendiente = wh_btc.get("pendiente", False)

        if not wh_pendiente and wh_dir in ("LONG", "SHORT") and wh_score >= 60:
            bias_wave_hunt = wh_dir
            log.info(
                f"🌊 Wave Hunt BTC activo: bias macro = {bias_wave_hunt} "
                f"(score {wh_score}%)"
            )
        elif wh_pendiente:
            # PENDIENTE = Wave Hunt identificó estructura bajista pero precio aún no llegó
            # a la zona de entrada. El sesgo sigue siendo SHORT — no es NEUTRAL.
            bias_wave_hunt = "SHORT"
            log.info(
                f"🌊 Wave Hunt BTC pendiente → bias macro = SHORT "
                f"(esperando zona, precio fuera de techo Wave 2/B)"
            )
        else:
            log.info(
                f"🌊 Wave Hunt BTC sin bias claro "
                f"(pendiente={wh_pendiente}, dir={wh_dir}, score={wh_score})"
            )
    except Exception as e:
        log.warning(f"No se pudo calcular bias Wave Hunt: {e}")

    señales = []

    for activo in ACTIVOS:
        # Gate de seguridad: acciones desactivadas → ni se escanean ni se operan
        if activo in STOCKS and not STOCKS_ENABLED:
            continue
        try:
            señal = await scoring_completo(activo)
            score = señal["score"]
            dir_señal = señal.get("direccion")

            if score >= SCORE_EJECUTAR and dir_señal:
                # ── Filtro 0: SHORT requiere confluencia 2/3 (P1-5) ──
                # Histórico: SHORT 1W/5L (17%) en los últimos 35 trades.
                # Un short necesita confirmación de al menos 2 estrategias.
                if dir_señal == "SHORT" and señal.get("confluencia", 0) < 2:
                    log.info(
                        f"🚫 {activo} SHORT bloqueado — confluencia "
                        f"{señal.get('confluencia', 0)}/3 < 2 requerida para shorts"
                    )
                    continue

                # ── Filtro 1: alineación con contexto macro propio ──
                # (P2-8) Veto relajado: Zone Flip es reversión a la media —
                # comprar soporte en tendencia bajista ES su naturaleza.
                # Contra-tendencia se permite SOLO con confluencia >= 2
                # (dos estrategias coinciden); con una sola, se veta.
                ctx = await get_contexto(activo)
                sesgo = ctx.get("sesgo", "NEUTRAL")
                if (sesgo != "NEUTRAL" and dir_señal != sesgo
                        and señal.get("confluencia", 0) < 2):
                    log.info(
                        f"🚫 {activo} señal {dir_señal} bloqueada — "
                        f"macro {ctx['tendencia']} / sesgo {sesgo} y "
                        f"confluencia {señal.get('confluencia', 0)}/3 < 2 "
                        f"(contra-tendencia exige doble confirmación)"
                    )
                    continue

                # ── Filtro 2: BTC veto para alts (doctrina) ─────────
                # "Solo abrís shorts en alts si BTC también está bajando.
                #  Si BTC está rebotando → no entrás en alts."
                # Las acciones NO siguen a BTC → exentas de este veto.
                if activo != "BTC" and activo not in STOCKS:
                    ctx_btc = await get_contexto("BTC")
                    tendencia_btc = ctx_btc.get("tendencia", "lateral")
                    sesgo_btc     = ctx_btc.get("sesgo", "NEUTRAL")

                    # (P2-8) Contra el sesgo de BTC solo con confluencia >= 2
                    if (sesgo_btc != "NEUTRAL" and dir_señal != sesgo_btc
                            and señal.get("confluencia", 0) < 2):
                        log.info(
                            f"🚫 {activo} señal {dir_señal} VETADA por BTC: "
                            f"BTC {tendencia_btc}/{sesgo_btc} y confluencia "
                            f"{señal.get('confluencia', 0)}/3 < 2 — alts siguen a BTC"
                        )
                        continue

                # ── Filtro 3: Wave Hunt macro bias (doctrina) ───────
                # "Con Wave Hunt activo: los rebotes son shorts, no longs"
                # Si Wave Hunt detecta bias bajista y la señal va contra → vetar
                # (solo cripto: el bias macro de BTC no rige las acciones).
                if (activo not in STOCKS
                        and bias_wave_hunt != "NEUTRAL"
                        and dir_señal != bias_wave_hunt):
                    log.info(
                        f"🚫 {activo} señal {dir_señal} VETADA por Wave Hunt: "
                        f"bias macro = {bias_wave_hunt}"
                    )
                    continue

                # ── Filtro 3b: LONG contra bias SHORT requiere confluencia 2/3 ──
                # Un LONG en cripto con bias Wave Hunt SHORT solo se permite si hay
                # confirmación de al menos 2 estrategias. Score alto sin confluencia
                # no es suficiente para ir contra la tendencia macro.
                if (activo not in STOCKS
                        and bias_wave_hunt == "SHORT"
                        and dir_señal == "LONG"
                        and señal.get("confluencia", 0) < 2):
                    log.info(
                        f"🚫 {activo} LONG bloqueado — bias SHORT activo y "
                        f"confluencia {señal.get('confluencia', 0)}/3 < 2 requerida"
                    )
                    continue

                # ── Filtro 4: Smart Flow valida timing (doctrina) ──
                # "Smart Flow → timing y sizing"
                # "Si Smart Flow score < 50% → no operar aunque se vea bien"
                sf_data = señal.get("sf", {}) or {}
                sf_score = sf_data.get("puntuacion", sf_data.get("score", 0)) or 0
                sf_dir   = sf_data.get("direccion")

                # Bloquear si Smart Flow contradice la dirección con score >= 50%
                if sf_dir and sf_dir != dir_señal and sf_score >= 50:
                    log.info(
                        f"🚫 {activo} señal {dir_señal} VETADA por Smart Flow: "
                        f"SF dice {sf_dir} con {sf_score}%"
                    )
                    continue
                # Sin confirmación de Smart Flow → no operar (timing no validado)
                if sf_score < 50:
                    log.info(
                        f"🚫 {activo} señal {dir_señal} VETADA — Smart Flow score "
                        f"{sf_score}% < 50% (timing no confirmado)"
                    )
                    continue

                señales.append(señal)
                log.info(
                    f"🎯 {activo} score={score} {dir_señal} "
                    f"[{señal['estrategia']}] conf={señal['confluencia']} "
                    f"macro={ctx.get('tendencia', '?')}"
                )
            else:
                log.info(f"⏭ {activo} ignorado score={score}")

        except Exception as e:
            log.error(f"Error analizando {activo}: {e}")

    if not señales:
        log.info("Sin señales ejecutables este ciclo")
        return

    señales.sort(key=lambda s: (s["score"], s.get("leverage", 0)), reverse=True)

    # Tomar los mejores candidatos hasta el límite de trades simultáneos
    candidatos = señales[:MAX_TRADES]

    for señal in candidatos:
        activo = señal["activo"]
        precio = await _obtener_precio(activo)

        if not precio:
            log.warning(f"Sin precio para {activo}, saltando")
            continue

        await ejecutar_trade(señal, precio, canal_talk)


async def loop_confirmaciones(canal_talk) -> None:
    """
    Re-evalúa activos en observación cada 1 minuto.
    Si un setup sube a ≥ 85, ejecuta directamente.
    """
    if len(trades_abiertos) >= MAX_TRADES:
        return

    for activo in ACTIVOS:
        puede, _ = _puede_operar(activo, "LONG")
        if not puede:
            continue
        try:
            señal = await scoring_completo(activo)
            if señal["score"] >= SCORE_EJECUTAR and señal.get("direccion"):
                precio = await _obtener_precio(activo)
                if precio:
                    await ejecutar_trade(señal, precio, canal_talk)
        except Exception as e:
            log.error(f"loop_confirmaciones({activo}): {e}")