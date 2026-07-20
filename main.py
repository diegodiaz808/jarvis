import asyncio
import discord
import os
import logging
from collections import Counter
from datetime import datetime, timedelta
from dotenv import load_dotenv
from database import init_db, guardar_reporte, obtener_ultimos_reportes
from ollama_client import analizar_async, generar_pine_script
from bots.crypto_bot import obtener_datos_cripto
from bots.stocks_bot import obtener_datos_acciones
from bots.news_bot import obtener_noticias
from bots.market_data_bot import obtener_todo, obtener_todo_sync
from bots.pc_monitor import obtener_info_pc, formatear_pc, hay_alertas, verificar_internet
# trading_bot.py eliminado — toda la lógica vive en engine.py
from bots.indicadores import formatear_analisis
from bots.tendencias import obtener_tendencias_completas
from bots.trading_bot.zone_flip import formatear_zone_flip, evaluar_zone_flip
from bots.trading_bot.wave_hunt import formatear_wave_hunt, evaluar_wave_hunt
from bots.trading_bot.smart_flow import formatear_smart_flow, evaluar_smart_flow
from bots.trading_bot.fast_paper import (
    mark_engine_start as mark_fast_paper_start,
    record_engine_error as record_fast_paper_error,
    run_cycle as run_fast_paper,
    summary as fast_paper_summary,
    weekly_report as fast_paper_weekly_report,
)
from bots.trading_bot.engine import (
    loop_scan_global,
    loop_confirmaciones,
    loop_actualizar_contexto,
    scoring_completo,
    trades_abiertos,
    analizar_post_trade,
    restaurar_cooldowns,
    SCORE_EJECUTAR,
    MAX_TRADES,
    CAPITAL,
    estado_engine,
    gestionar_posiciones_activas,
    _confirmaciones,
)
from discord.ext import tasks
from bots.trading_bot.bybit_client import (
    sync_posiciones_abiertas,
    obtener_pnl_posiciones,
    obtener_balance_usdt,
)


# ─── Logging ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("jarvis.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("jarvis")

# ─── Config ────────────────────────────────────────────────────
load_dotenv()
TOKEN      = os.getenv("DISCORD_TOKEN")
CANAL_TALK = int(os.getenv("CANAL_TALK"))
CANAL_LOGS = int(os.getenv("CANAL_LOGS"))
# Canal de auditoría (opcional). Si no está, usa CANAL_LOGS como fallback.
_canal_audit_env = os.getenv("CANAL_AUDIT")
CANAL_AUDIT = int(_canal_audit_env) if _canal_audit_env else CANAL_LOGS
_canal_paper_fast_env = os.getenv("CANAL_PAPER_FAST")
CANAL_PAPER_FAST = int(_canal_paper_fast_env) if _canal_paper_fast_env else None


async def enviar_fast_paper(canal, mensaje: str) -> None:
    """Publica mensajes Fast Paper con separación visual en Discord."""
    await canal.send(f"{mensaje}\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━")

if not TOKEN:
    raise ValueError("❌ Falta DISCORD_TOKEN en .env")
if not CANAL_TALK:
    raise ValueError("❌ Falta CANAL_TALK en .env")
if not CANAL_LOGS:
    raise ValueError("❌ Falta CANAL_LOGS en .env")

# ─── Discord client ────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

historial          = []
internet_estaba_ok = True
hora_inicio        = datetime.now()
ultimo_mensaje     = {}
cache_mercado      = {"data": None, "timestamp": None}


# ─── Activos reconocidos ───────────────────────────────────────
ACTIVOS = ["BTC", "ETH", "SOL", "AAPL", "NVDA", "TSLA", "META", "MSFT", "AMZN", "GOOGL"]


# ─── Caché de mercado (2 minutos) ──────────────────────────────
async def obtener_mercado_cacheado():
    ahora = datetime.now()
    if (
        cache_mercado["data"] is None or
        cache_mercado["timestamp"] is None or
        ahora - cache_mercado["timestamp"] > timedelta(minutes=2)
    ):
        log.info("🔄 Actualizando caché de mercado...")
        cache_mercado["data"] = {
            "cripto":   obtener_datos_cripto(),
            "acciones": obtener_datos_acciones(),
            "noticias": obtener_noticias(),
            "market":   await obtener_todo(),
            "pc":       formatear_pc(obtener_info_pc()),
        }
        cache_mercado["timestamp"] = ahora
        log.info("✅ Caché actualizado")
    return cache_mercado["data"]


# ─── Detectar activo en el texto ───────────────────────────────
def detectar_activo(texto: str, default: str = "BTC") -> str:
    for a in ACTIVOS:
        if a.lower() in texto.lower():
            return a
    return default


# ─── Helper: enviar mensaje largo en partes ────────────────────
async def enviar_en_partes(canal, texto: str, prefix: str = ""):
    if not texto.strip():
        return
    contenido = prefix + texto if prefix else texto
    if len(contenido) <= 1900:
        await canal.send(contenido)
    else:
        partes = [contenido[i:i + 1900] for i in range(0, len(contenido), 1900)]
        for parte in partes:
            await canal.send(parte)


# ─── Helper: formatear posiciones activas para !positions ──────
def _formatear_posicion(t: dict) -> str:
    """Devuelve un bloque de texto resumido para un trade abierto."""
    trade_id   = t.get("trade_id", "")
    id_prefix  = f"{trade_id} " if trade_id else ""
    activo    = t.get("activo") or t.get("par", "?")
    direccion = t.get("direccion", "?")
    entrada   = t.get("entrada") or t.get("precio", 0)
    sl        = t.get("sl") or (t.get("riesgo") or {}).get("sl", 0)
    tp        = t.get("tp") or (t.get("riesgo") or {}).get("tp", 0)
    estrategia = t.get("estrategia", "?").upper()
    score      = t.get("score", "?")

    # TPs escalonados (engine nuevo)
    tp1_hit = "✅" if t.get("tp1_hit") else "⏳"
    tp2_hit = "✅" if t.get("tp2_hit") else "⏳"
    tp3_hit = "✅" if t.get("tp3_hit") else "⏳"
    tp1 = t.get("tp1") or tp
    tp2 = t.get("tp2") or 0
    tp3 = t.get("tp3") or 0

    size_orig  = t.get("qty", 0)
    size_rem   = t.get("qty_restante") or size_orig

    pnl_ur = t.get("pnl_unrealized")
    pnl_r  = t.get("pnl_realized")
    pnl_ur_str = f"  💰 PnL no realizado: `${pnl_ur:+,.2f}`\n" if pnl_ur is not None else ""
    pnl_r_str  = f"  💵 PnL realizado:     `${pnl_r:+,.2f}`\n"  if pnl_r  is not None else ""

    tp_str = f"  🎯 TP1: `${tp1:,.2f}` {tp1_hit}"
    if tp2:
        tp_str += f" · TP2: `${tp2:,.2f}` {tp2_hit}"
    if tp3:
        tp_str += f" · TP3: `${tp3:,.2f}` {tp3_hit}"

    lines = [
        f"**`{id_prefix}{activo}`** — {direccion} [{estrategia}] score={score}",
        f"  📍 Entrada: `${entrada:,.2f}` · SL actual: `${sl:,.2f}`",
        tp_str,
        f"  📦 Size restante: `{size_rem}` / `{size_orig}`",
    ]
    if pnl_ur_str:
        lines.append(pnl_ur_str.strip())
    if pnl_r_str:
        lines.append(pnl_r_str.strip())
    return "\n".join(lines)


def _confirmaciones_pendientes_ids() -> list[int]:
    return list(_confirmaciones.keys())


# ─── Cierre manual interactivo (pide número + confirmación) ────
# user_id → {"paso": "numero"|"confirmar", "trade": dict, "ts": datetime}
_cierres_pendientes: dict[int, dict] = {}
CIERRE_TIMEOUT_SEG = 120


def _buscar_trade_por_numero(token: str) -> dict | None:
    """Busca en trades_abiertos por trade_id (#0068 / 0068 / 68) o, si no
    matchea ningún id, por posición en la lista (1 = primero)."""
    es_id_explicito = token.strip().startswith("#")
    token = token.strip().lstrip("#")
    if not token.isdigit():
        return None
    num = int(token)
    for t in trades_abiertos:
        tid = str(t.get("trade_id") or "").lstrip("#")
        if tid.isdigit() and int(tid) == num:
            return t
    if not es_id_explicito and 1 <= num <= len(trades_abiertos):
        return trades_abiertos[num - 1]
    return None


def _listado_para_cerrar() -> str:
    if not trades_abiertos:
        return "📭 No hay trades abiertos para cerrar."
    lines = [f"📂 **Trades abiertos — {len(trades_abiertos)}:**"]
    for i, t in enumerate(trades_abiertos, 1):
        tid = t.get("trade_id") or f"(sin id — usá {i})"
        lines.append(
            f"  {i}. `{tid}` {t.get('activo')} {t.get('direccion')} "
            f"[{t.get('estrategia', '?')}] — entrada `${t.get('entrada', 0):,.2f}` · "
            f"SL `${t.get('current_sl', t.get('sl', 0)):,.2f}`"
        )
    return "\n".join(lines)


def _detalle_trade_confirmar(t: dict) -> str:
    tid = t.get("trade_id") or "(sin id)"
    return (
        f"⚠️ **Vas a cerrar este trade a precio de mercado:**\n"
        f"  `{tid}` **{t.get('activo')} {t.get('direccion')}** [{t.get('estrategia', '?')}]\n"
        f"  📍 Entrada `${t.get('entrada', 0):,.2f}` · SL `${t.get('current_sl', t.get('sl', 0)):,.2f}` · "
        f"Lev `{t.get('leverage', '?')}x` · qty restante `{t.get('qty_restante', t.get('qty', '?'))}`\n"
        f"¿Confirmás el cierre? (**sí** / **no**)"
    )


async def _ejecutar_cierre_manual(trade: dict, channel) -> None:
    from bots.trading_bot.engine import _obtener_precio
    from bots.trading_bot.bybit_client import cerrar_posicion, bybit_disponible

    tid = trade.get("trade_id") or trade.get("activo", "?")
    precio = await _obtener_precio(trade["activo"])
    if not precio:
        await channel.send(
            f"⚠️ No pude obtener precio actual de {trade['activo']} — "
            f"no cerré nada. Probá de nuevo en un momento."
        )
        return

    if bybit_disponible():
        try:
            await asyncio.to_thread(cerrar_posicion, trade["activo"], trade["direccion"])
        except Exception as e:
            log.error(f"Error cerrando {tid} en Bybit: {e}")
            await channel.send(
                f"⚠️ Bybit devolvió error al cerrar (`{e}`). "
                f"Igualmente registro el cierre en Jarvis — verificá la posición en Bybit."
            )

    canal_logs = client.get_channel(CANAL_LOGS)
    await analizar_post_trade(
        trade, precio, canal_logs, motivo_cierre="MANUAL_DISCORD",
    )
    await channel.send(f"✅ Trade `{tid}` cerrado @ `${precio:,.4f}`.")


# ─── Eventos ───────────────────────────────────────────────────
@client.event
async def on_ready():
    init_db()
    log.info(f"✅ Jarvis online como {client.user}")

    # ── Conectar canal de auditoría al engine ──────────────────
    import bots.trading_bot.engine as _engine_mod
    _engine_mod.canal_auditoria = client.get_channel(CANAL_AUDIT)
    if _engine_mod.canal_auditoria:
        log.info(f"📊 Canal de auditoría conectado: {CANAL_AUDIT}")
    else:
        log.warning(f"⚠️ No se encontró canal de auditoría {CANAL_AUDIT}")

    canal_fast = client.get_channel(CANAL_PAPER_FAST) if CANAL_PAPER_FAST else None
    if canal_fast:
        mark_fast_paper_start()
        log.info(f"⚡ Canal Fast Paper conectado: {CANAL_PAPER_FAST}")
        await enviar_fast_paper(canal_fast,
            "🧪 **FAST PAPER v1.1 ONLINE**\n"
            "Motor aislado en modo `SHADOW`: BTC/ETH · entrada 5m/15m (limit/maker) · "
            "RR 2.0 anclado a entrada · breakeven a +0.25% · tendencia 1h informativa · "
            "capital paper $500 · riesgo 0.5% · máximo 5 posiciones. No envía órdenes reales."
        )
    else:
        log.warning(f"⚠️ No se encontró canal Fast Paper {CANAL_PAPER_FAST}")

    # ── Recuperar posiciones abiertas en Bybit tras reinicio ───
    posiciones_sync = await asyncio.to_thread(sync_posiciones_abiertas)

    # ── Restaurar cooldowns desde historial para evitar reabrir inmediatamente ─
    restaurar_cooldowns()

    # ── Reconciliar trades "ABIERTO" del historial ─────────────────────────────
    # Si el activo sigue con posición en Bybit → re-adoptar el trade en
    # trades_abiertos para que el loop gestione su SL/TP (antes quedaban
    # "zombi": nunca volvían al engine y jamás se cerraban).
    # Si el activo ya no está en Bybit → fue cerrado externamente: marcar
    # CERRADO_MANUAL y aplicar cooldown.
    from bots.trading_bot.engine import _cargar_json, _guardar_json, TRADE_LOG_PATH, ultimo_trade, COOLDOWN_MINUTOS
    activos_bybit = {p["activo"] for p in posiciones_sync}
    ya_aplicado: set[str] = set()
    adoptados: set[str] = set()
    all_trades = _cargar_json(TRADE_LOG_PATH)
    trades_modificados = False
    for t in all_trades:
        activo = t.get("activo")
        if t.get("estado") != "ABIERTO" or not activo:
            continue
        if activo in activos_bybit:
            trades_abiertos.append(t)
            adoptados.add(activo)
            log.info(
                f"♻️ Trade {t.get('trade_id', '(sin id)')} {activo} {t.get('direccion')} "
                f"re-adoptado desde historial — el engine gestiona su SL/TP"
            )
        else:
            # Marcar como cerrado manualmente para no acumular entradas stale
            t["estado"] = "CERRADO_MANUAL"
            trades_modificados = True
            if activo not in ya_aplicado:
                ya_aplicado.add(activo)
                # restaurar_cooldowns() ya pudo haber seteado un timestamp reciente.
                # Solo aplicar cooldown fresco si el existente ya expiró (trade abierto hace mucho).
                delta = datetime.now() - ultimo_trade[activo]
                if delta >= timedelta(minutes=COOLDOWN_MINUTOS):
                    ultimo_trade[activo] = datetime.now()
                    log.info(f"🕐 Cooldown aplicado a {activo} — cerrado manualmente en Bybit (30m)")
                else:
                    mins = int((timedelta(minutes=COOLDOWN_MINUTOS) - delta).total_seconds() / 60)
                    log.info(f"🕐 {activo} cerrado manualmente en Bybit — {mins}m restantes")
    if trades_modificados:
        _guardar_json(TRADE_LOG_PATH, all_trades)

    # Posiciones de Bybit sin trade propio en el historial → entrada genérica "sync"
    posiciones_sync = [p for p in posiciones_sync if p["activo"] not in adoptados]
    if posiciones_sync:
        trades_abiertos.extend(posiciones_sync)
        log.info(f"🔄 {len(posiciones_sync)} posición/es recuperada/s desde Bybit (sync genérico)")

    canal_logs = client.get_channel(CANAL_LOGS)
    if canal_logs:
        sync_str = (
            f"\n⚠️ {len(posiciones_sync)} posición/es recuperada/s desde Bybit"
            if posiciones_sync else ""
        )
        await canal_logs.send(
            f"🟢 **Jarvis online** — {datetime.now().strftime('%d/%m/%Y %H:%M')}hs\n"
            f"{formatear_pc(obtener_info_pc())}{sync_str}"
        )

    reporte_automatico.start()
    monitor_pc.start()
    ciclo_contexto_macro.start()
    ciclo_scan_global.start()
    ciclo_confirmaciones.start()
    ciclo_posiciones_activas.start()
    if CANAL_PAPER_FAST and not ciclo_fast_paper.is_running():
        ciclo_fast_paper.start()
    if CANAL_PAPER_FAST and not reporte_fast_paper.is_running():
        reporte_fast_paper.start()
    if CANAL_PAPER_FAST and not reporte_semanal_fast_paper.is_running():
        reporte_semanal_fast_paper.start()


@client.event
async def on_message(message):
    if message.author == client.user:
        return

    texto   = message.content.strip()
    texto_l = texto.lower()
    if not texto:
        return

    # ─── Comandos en CANAL_TALK ────────────────────────────────
    if message.channel.id == CANAL_TALK:

        # ── Flujo de cierre pendiente (número / confirmación) ──
        pend = _cierres_pendientes.get(message.author.id)
        if pend and (datetime.now() - pend["ts"]).total_seconds() > CIERRE_TIMEOUT_SEG:
            del _cierres_pendientes[message.author.id]
            pend = None
        if pend:
            if texto_l in ("no", "cancelar", "cancel", "salir", "nada"):
                del _cierres_pendientes[message.author.id]
                await message.channel.send("❌ Cierre cancelado — no toqué nada.")
                return

            if pend["paso"] == "numero":
                trade = _buscar_trade_por_numero(texto)
                if trade:
                    pend["paso"]  = "confirmar"
                    pend["trade"] = trade
                    pend["ts"]    = datetime.now()
                    await message.channel.send(_detalle_trade_confirmar(trade))
                elif any(c.isdigit() for c in texto):
                    await message.channel.send(
                        "No encontré un trade abierto con ese número. "
                        f"Decime uno de estos (o `cancelar`):\n{_listado_para_cerrar()}"
                    )
                else:
                    # No parece una respuesta al flujo → abandonar y seguir normal
                    del _cierres_pendientes[message.author.id]
                return

            if pend["paso"] == "confirmar":
                if texto_l in ("si", "sí", "yes", "confirmo", "confirmar", "dale", "ok"):
                    trade = pend["trade"]
                    del _cierres_pendientes[message.author.id]
                    if trade not in trades_abiertos:
                        await message.channel.send(
                            "⚠️ Ese trade ya no figura abierto (se cerró mientras tanto)."
                        )
                        return
                    await _ejecutar_cierre_manual(trade, message.channel)
                    return
                await message.channel.send(
                    "Respondé **sí** para cerrar o **no** para cancelar."
                )
                return

        # ── !estado / !engine / !status ────────────────────────
        if texto_l in ("!estado", "!engine", "!status"):
            if trades_abiertos:
                lines = [
                    f"  • `{t.get('trade_id','')} {t['activo']}` {t['direccion']} [{t['estrategia']}] "
                    f"score={t['score']} — entrada ${t['entrada']:,.2f}"
                    for t in trades_abiertos
                ]
                ops_str = "\n".join(lines)
            else:
                ops_str = "  Sin trades abiertos"
            await message.channel.send(
                f"⚙️ **Engine state:** `{estado_engine}`\n"
                f"📊 Trades abiertos ({len(trades_abiertos)}/{MAX_TRADES}):\n{ops_str}\n"
                f"🎯 Umbral ejecución: `{SCORE_EJECUTAR}%` — por debajo se ignora\n"
                f"🤖 **Modo:** AUTO ≥{SCORE_EJECUTAR}% · IGNORAR <{SCORE_EJECUTAR}%"
            )
            return

        # ── !positions ─────────────────────────────────────────
        if texto_l in ("!positions", "!pos", "!trades"):
            if not trades_abiertos:
                await message.channel.send("📭 Sin posiciones abiertas en este momento.")
                return

            header  = f"📂 **Posiciones abiertas — {len(trades_abiertos)} trade/s**\n{'─'*40}"
            bloques = [header]
            for t in trades_abiertos:
                bloques.append(_formatear_posicion(t))
            await enviar_en_partes(message.channel, "\n\n".join(bloques))
            return

        # ── !test_bybit ────────────────────────────────────────
        if texto_l in ("!test_bybit", "!testbybit"):
            from bots.trading_bot.bybit_client import test_conexion
            await message.channel.send("🔌 Testeando conexión con Bybit...")
            resultado = test_conexion()
            if resultado["ok"]:
                await message.channel.send(
                    f"✅ **Bybit conectado — {resultado['modo']}**\n"
                    f"Balance USDT: `{resultado['balance']}`"
                )
            else:
                await message.channel.send(
                    f"❌ **Bybit ERROR**\n"
                    f"`{resultado.get('error', 'Error desconocido')}`"
                )
            return

        # ── !scan ──────────────────────────────────────────────
        if texto_l in ("!scan", "!escanear", "!forzar scan"):
            canal_logs = client.get_channel(CANAL_LOGS)
            canal_talk = client.get_channel(CANAL_TALK)
            await message.channel.send("🔍 Forzando scan global...")
            await loop_scan_global(canal_logs, canal_talk)
            return

        # ── !score ACTIVO ──────────────────────────────────────
        if texto_l.startswith("!score"):
            activo = detectar_activo(texto)
            await message.channel.send(f"📊 Calculando scoring completo para **{activo}**...")
            try:
                señal = await scoring_completo(activo)

                # Decisión final
                decision     = señal.get("decision", señal.get("direccion") or "—")
                prioridad    = señal.get("prioridad", "—")
                confluencia  = señal.get("confluencia", señal.get("confluencia", "?"))
                dir_final    = señal.get("direccion_final", señal.get("direccion") or "—")
                score_final  = señal.get("score", 0)
                lev_final    = señal.get("leverage", señal.get("lev", "—"))

                # Score por estrategia
                zf_score = señal.get("zf", {}).get("puntuacion", 0)
                wh_score = señal.get("wh", {}).get("puntuacion", 0)
                sf_score = señal.get("sf", {}).get("puntuacion", 0)
                zf_dir   = señal.get("zf", {}).get("direccion") or "—"
                wh_dir   = señal.get("wh", {}).get("direccion") or "—"
                sf_dir   = señal.get("sf", {}).get("direccion") or "—"

                # Indicadores descartados y razón de contradicción
                descartados   = señal.get("indicadores_descartados", [])
                razon_no_op   = señal.get("razon_contradiccion") or señal.get("razon", "")
                desc_str      = ", ".join(descartados) if descartados else "Ninguno"
                no_op_str     = f"\n⛔ Razón NO_OPERAR: `{razon_no_op}`" if razon_no_op else ""

                # Emoji de decisión
                emoji_dec = "🚀" if "LONG" in str(decision).upper() else \
                            "🔻" if "SHORT" in str(decision).upper() else \
                            "⛔" if "NO_OPERAR" in str(decision).upper() else "⏳"

                respuesta = (
                    f"**Score completo — {activo}**\n"
                    f"{'─'*38}\n"
                    f"{emoji_dec} **Decisión final:** `{decision}`\n"
                    f"⭐ **Prioridad:** `{prioridad}`\n"
                    f"🔗 **Confluencia:** `{confluencia}/3`\n"
                    f"📍 **Dirección final:** `{dir_final}`\n"
                    f"📊 **Score final:** `{score_final}%`\n"
                    f"⚖️  **Leverage final:** `{lev_final}x`\n"
                    f"{'─'*38}\n"
                    f"**Score por estrategia:**\n"
                    f"  🎯 Zone Flip:  `{zf_dir}` — `{zf_score}%`\n"
                    f"  🌊 Wave Hunt:  `{wh_dir}` — `{wh_score}%`\n"
                    f"  🔮 Smart Flow: `{sf_dir}` — `{sf_score}%`\n"
                    f"{'─'*38}\n"
                    f"🗑️  Indicadores descartados: `{desc_str}`"
                    f"{no_op_str}"
                )
                await enviar_en_partes(message.channel, respuesta)

            except Exception as e:
                log.error(f"Error en !score: {e}")
                await message.channel.send(f"⚠️  Error calculando score para {activo}: {e}")
            return

        # ── !cerrar ────────────────────────────────────────────
        # `!cerrar`            → lista trades y pide número + confirmación
        # `!cerrar #0068`      → pide confirmación directa
        # `!cerrar BTC 95000`  → legacy: registra cierre a precio dado
        if texto_l.startswith("!cerrar"):
            partes = texto.split()
            if len(partes) >= 3 and not partes[1].lstrip("#").isdigit():
                activo_cerrar = partes[1].upper()
                try:
                    precio_cierre = float(partes[2])
                    trade = next((t for t in trades_abiertos if t["activo"] == activo_cerrar), None)
                    if trade:
                        canal_logs = client.get_channel(CANAL_LOGS)
                        await analizar_post_trade(
                            trade, precio_cierre, canal_logs,
                            motivo_cierre="MANUAL_DISCORD",
                        )
                    else:
                        await message.channel.send(f"No hay trade abierto en {activo_cerrar}")
                except ValueError:
                    await message.channel.send("Uso: `!cerrar BTC 95000`")
                return

            if not trades_abiertos:
                await message.channel.send("📭 No hay trades abiertos para cerrar.")
                return

            if len(partes) >= 2 and partes[1].lstrip("#").isdigit():
                trade = _buscar_trade_por_numero(partes[1])
                if trade:
                    _cierres_pendientes[message.author.id] = {
                        "paso": "confirmar", "trade": trade, "ts": datetime.now(),
                    }
                    await message.channel.send(_detalle_trade_confirmar(trade))
                else:
                    await message.channel.send(
                        f"No encontré un trade abierto con ese número.\n{_listado_para_cerrar()}"
                    )
                return

            _cierres_pendientes[message.author.id] = {
                "paso": "numero", "trade": None, "ts": datetime.now(),
            }
            await message.channel.send(
                f"{_listado_para_cerrar()}\n"
                f"🔢 ¿Cuál cierro? Decime el número (ej: `#0068` o `68`), o `cancelar`."
            )
            return

        # ── !pnl ───────────────────────────────────────────────
        if texto_l in ("!pnl", "!ganancia", "!perdida"):
            await message.channel.send("💰 Consultando P&L en Bybit...")
            posiciones = await asyncio.to_thread(obtener_pnl_posiciones)
            if not posiciones:
                await message.channel.send(
                    "📭 Sin posiciones abiertas en Bybit (o sin API keys)."
                )
                return
            lineas = [f"📊 **P&L en tiempo real — {len(posiciones)} posición/es**\n{'─'*38}"]
            for p in posiciones:
                emoji  = "🟢" if p["pnl_usdt"] >= 0 else "🔴"
                lineas.append(
                    f"{emoji} **`{p['activo']}`** {p['direccion']} {p['leverage']}×\n"
                    f"  📍 Entrada: `${p['entrada']:,.4f}` · Mark: `${p['precio_mark']:,.4f}`\n"
                    f"  💰 P&L: `{p['pnl_usdt']:+.4f} USDT` (`{p['pnl_pct']:+.2f}%`)\n"
                    f"  📦 Qty: `{p['qty']}`"
                )
            await enviar_en_partes(message.channel, "\n\n".join(lineas))
            return

        # ── !riesgo ────────────────────────────────────────────
        if texto_l in ("!riesgo", "!balance", "!capital"):
            await message.channel.send("💼 Consultando balance en Bybit...")
            bal = await asyncio.to_thread(obtener_balance_usdt)
            if not bal.get("ok"):
                await message.channel.send(
                    f"⚠️  Error consultando balance: `{bal.get('error', 'desconocido')}`"
                )
                return
            margen_engine = sum(t.get("margen", 0) for t in trades_abiertos)
            exposure_total = sum(
                t.get("margen", 0) * t.get("leverage", 1) for t in trades_abiertos
            )
            await message.channel.send(
                f"💼 **Balance Bybit — {bal['modo']}**\n"
                f"{'─'*38}\n"
                f"  💰 Wallet total:   `${bal['wallet_balance']:,.2f} USDT`\n"
                f"  ✅ Disponible:     `${bal['available']:,.2f} USDT`\n"
                f"  🔒 Margen en uso:  `${bal['margin_used']:,.2f} USDT`\n"
                f"{'─'*38}\n"
                f"  📊 Margen engine:  `${margen_engine:.2f} USDT` ({len(trades_abiertos)} trade/s)\n"
                f"  📈 Exposure total: `${exposure_total:.2f} USDT`\n"
                f"  🏦 Capital config: `${CAPITAL:.2f} USDT`"
            )
            return

        # ── !historial ─────────────────────────────────────────
        if texto_l in ("!historial", "!trades_cerrados", "!log"):
            from bots.trading_bot.engine import _cargar_json, MEMORIA_PATH
            memoria = _cargar_json(MEMORIA_PATH)
            if not memoria:
                await message.channel.send("📭 Sin historial de trades registrado.")
                return
            ultimos = memoria[-5:]  # últimos 5
            lineas  = [f"📜 **Historial — últimos {len(ultimos)} trades**\n{'─'*38}"]
            for ap in reversed(ultimos):
                emoji   = "✅" if ap.get("resultado") == "WIN" else "❌"
                gana_pct = ap.get("ganancia_pct", 0)
                pnl      = ap.get("pnl_usdt", 0)
                trade_id  = ap.get("trade_id", "")
                id_prefix = f"{trade_id} " if trade_id else ""
                lineas.append(
                    f"{emoji} **`{id_prefix}{ap.get('activo', '?')}`** {ap.get('direccion', '?')} "
                    f"[{ap.get('estrategia', '?')}] `{ap.get('resultado', '?')}`\n"
                    f"  📍 Entrada: `${ap.get('entrada', 0):,.4f}` → Cierre: `${ap.get('cierre', 0):,.4f}`\n"
                    f"  💰 P&L: `{gana_pct:+.2f}%` · `{pnl:+.4f} USDT`\n"
                    f"  🏷️  Score: `{ap.get('score_original', '?')}%` · Conf: `{ap.get('confluencia', 0)}/3`\n"
                    f"  📅 {ap.get('timestamp', '?')[:16]}"
                )
            await enviar_en_partes(message.channel, "\n\n".join(lineas))
            return

        # ── !auditoria / !audit ─────────────────────────────────
        if texto_l in ("!auditoria", "!audit", "!auditoría", "!performance"):
            from bots.trading_bot.auditor import (
                auditar_global, formatear_global_discord, construir_prompt_global
            )
            a = auditar_global()
            if not a.get("ok"):
                await message.channel.send(f"📭 {a.get('motivo')}")
                return

            # 1. Datos puros
            await enviar_en_partes(message.channel, formatear_global_discord(a))

            # 2. Opinión técnica (trader 25 años vía Ollama)
            await message.channel.send("🧠 Analizando como trader veterano...")
            try:
                opinion = await analizar_async(construir_prompt_global(a))
                await enviar_en_partes(message.channel, opinion, "💼 **Análisis del veterano:**\n")
            except Exception as e:
                log.error(f"Error opinión auditoría: {e}")
                await message.channel.send("⚠️ No pude generar la opinión técnica.")
            return

        # ── !trade #XXXX ────────────────────────────────────────
        if texto_l.startswith("!trade"):
            from bots.trading_bot.auditor import (
                auditar_trade, formatear_trade_discord, construir_prompt_trade
            )
            partes = texto.split()
            if len(partes) < 2:
                await message.channel.send("Uso: `!trade #0005` o `!trade 5`")
                return
            d = auditar_trade(partes[1])
            if not d.get("ok"):
                await message.channel.send(f"📭 {d.get('motivo')}")
                return

            # 1. Datos puros
            await enviar_en_partes(message.channel, formatear_trade_discord(d))

            # 2. Opinión técnica del trade puntual
            await message.channel.send("🧠 Analizando el trade...")
            try:
                opinion = await analizar_async(construir_prompt_trade(d))
                await enviar_en_partes(message.channel, opinion, f"💼 **Veterano sobre {d['trade_id']}:**\n")
            except Exception as e:
                log.error(f"Error opinión trade: {e}")
                await message.channel.send("⚠️ No pude generar la opinión del trade.")
            return

    # ─── Solo responder en CANAL_TALK ──────────────────────────
    if message.channel.id != CANAL_TALK:
        return

    # ─── Rate limiting — 15 segundos entre mensajes ────────────
    ahora  = datetime.now()
    ultimo = ultimo_mensaje.get(message.author.id)
    if ultimo and (ahora - ultimo).total_seconds() < 15:
        await message.add_reaction("⏳")
        return
    ultimo_mensaje[message.author.id] = ahora

    log.info(f"Mensaje de {message.author}: {texto[:80]}")

    async with message.channel.typing():
        try:
            datos    = await obtener_mercado_cacheado()
            cripto   = datos["cripto"]
            acciones = datos["acciones"]
            noticias = datos["noticias"]
            market   = datos["market"]
            pc       = datos["pc"]

            # ─── Auditoría / Performance (habla natural) ───────
            palabras_audit = [
                "como venis", "cómo venís", "como vas", "cómo vas",
                "como vamos", "cómo vamos", "como van los trades",
                "rendimiento", "performance", "como rinde", "cómo rinde",
                "como te fue", "cómo te fue", "estas ganando", "estás ganando",
                "estas perdiendo", "estás perdiendo", "win rate", "winrate",
                "tus resultados", "tus trades", "como vienen los trades",
                "balance de trades", "auditate", "audita tus", "analiza tus trades"
            ]
            import re as _re

            # ─── Cierre de trade por lenguaje natural ───────────
            # "cerrá el trade", "cierra el #68", "quiero cerrar un trade"...
            pide_cierre = (
                _re.search(r"\b(cierr\w*|cerr[aá]\w*|close)\b", texto_l)
                and ("trade" in texto_l or "posici" in texto_l or "#" in texto)
            )
            if pide_cierre:
                if not trades_abiertos:
                    await message.channel.send("📭 No hay trades abiertos para cerrar.")
                    return
                num_match = _re.search(r"#\s*(\d+)|\btrade\s*(\d+)\b", texto_l)
                trade = None
                if num_match:
                    trade = _buscar_trade_por_numero(num_match.group(1) or num_match.group(2))
                if trade:
                    _cierres_pendientes[message.author.id] = {
                        "paso": "confirmar", "trade": trade, "ts": datetime.now(),
                    }
                    await message.channel.send(_detalle_trade_confirmar(trade))
                else:
                    _cierres_pendientes[message.author.id] = {
                        "paso": "numero", "trade": None, "ts": datetime.now(),
                    }
                    await message.channel.send(
                        f"{_listado_para_cerrar()}\n"
                        f"🔢 ¿Cuál cierro? Decime el número (ej: `#0068` o `68`), o `cancelar`."
                    )
                guardar_reporte("jarvis", "cierre_manual", texto[:200])
                return

            # Pregunta por un trade específico (#N o "trade N")
            match_trade = _re.search(r"trade\s*#?\s*(\d+)", texto_l)

            if match_trade:
                from bots.trading_bot.auditor import (
                    auditar_trade, formatear_trade_discord, construir_prompt_trade
                )
                d = auditar_trade(match_trade.group(1))
                if not d.get("ok"):
                    await message.channel.send(f"📭 {d.get('motivo')}")
                    return
                await enviar_en_partes(message.channel, formatear_trade_discord(d))
                try:
                    opinion = await analizar_async(construir_prompt_trade(d))
                    await enviar_en_partes(message.channel, opinion, f"💼 **{d['trade_id']}:**\n")
                except Exception as e:
                    log.error(f"Error trade natural: {e}")
                guardar_reporte("jarvis", "trade_especifico", texto[:200])
                return

            # ─── Solo trades abiertos / posiciones activas ──────
            palabras_abiertos = [
                "trades abiertos", "trades activos", "posiciones abiertas",
                "posiciones activas", "que tenes abierto", "qué tenés abierto",
                "que tenes activo", "qué tenés activo", "trades vivos",
                "que estoy operando", "qué estoy operando",
                "operaciones activas", "que hay activo", "qué hay activo",
            ]
            if any(p in texto_l for p in palabras_abiertos):
                from bots.trading_bot.auditor import formatear_periodo_solo_abiertos
                await enviar_en_partes(
                    message.channel,
                    formatear_periodo_solo_abiertos("ahora")
                )
                guardar_reporte("jarvis", "abiertos", texto[:200])
                return

            # ─── Reporte por período (hoy / últimas N horas) ────
            # Detecta: "hoy", "últimas 24h", "últimas X horas", "últimos X días"
            horas_periodo = None
            etiqueta_periodo = None

            palabras_hoy = [
                "trades de hoy", "trades hoy", "como va el dia",
                "cómo va el día", "como vamos hoy", "cómo vamos hoy",
                "resumen del dia", "resumen del día", "performance hoy",
                "balance de hoy", "como vamos en el dia", "como vamos en el día",
                "ultimas 24", "últimas 24", "ultimas 24hs", "últimas 24hs",
                "ultimas 24 horas", "últimas 24 horas", "ultimo dia", "último día",
            ]
            if any(p in texto_l for p in palabras_hoy):
                horas_periodo = 24
                etiqueta_periodo = "últimas 24h"
            else:
                # "últimas N horas" / "últimas N hs"
                match_horas = _re.search(r"[uú]ltim[oa]s?\s+(\d+)\s*(?:horas?|hs|h\b)", texto_l)
                if match_horas:
                    horas_periodo = int(match_horas.group(1))
                    etiqueta_periodo = f"últimas {horas_periodo}h"
                else:
                    # "últimos N días"
                    match_dias = _re.search(r"[uú]ltim[oa]s?\s+(\d+)\s*d[ií]as?", texto_l)
                    if match_dias:
                        dias = int(match_dias.group(1))
                        horas_periodo = dias * 24
                        etiqueta_periodo = f"últimos {dias}d"

            if horas_periodo:
                from bots.trading_bot.auditor import (
                    auditar_periodo, formatear_periodo_discord, construir_prompt_periodo
                )
                a = auditar_periodo(horas_periodo)
                await enviar_en_partes(
                    message.channel,
                    formatear_periodo_discord(a, etiqueta_periodo)
                )
                # Opinión del veterano solo si hay datos cerrados
                if a.get("ok"):
                    try:
                        opinion = await analizar_async(
                            construir_prompt_periodo(a, etiqueta_periodo)
                        )
                        await enviar_en_partes(
                            message.channel, opinion,
                            f"💼 **Veterano sobre {etiqueta_periodo}:**\n"
                        )
                    except Exception as e:
                        log.error(f"Error opinión periodo: {e}")
                guardar_reporte("jarvis", "periodo", texto[:200])
                return

            if any(p in texto_l for p in palabras_audit):
                from bots.trading_bot.auditor import (
                    auditar_global, formatear_global_discord, construir_prompt_global
                )
                a = auditar_global()
                if not a.get("ok"):
                    await message.channel.send(f"📭 {a.get('motivo')}")
                    return
                await enviar_en_partes(message.channel, formatear_global_discord(a))
                try:
                    opinion = await analizar_async(construir_prompt_global(a))
                    await enviar_en_partes(message.channel, opinion, "💼 **Análisis del veterano:**\n")
                except Exception as e:
                    log.error(f"Error auditoría natural: {e}")
                guardar_reporte("jarvis", "auditoria", texto[:200])
                return

            # ─── Pine Script ───────────────────────────────────
            palabras_pine = [
                "pine script", "pinescript", "tradingview", "estrategia pine",
                "script trading", "indicador pine", "armame un script",
                "haceme un script", "generame un script"
            ]
            if any(p in texto_l for p in palabras_pine):
                temporalidades = {
                    "1m": "1 minuto", "5m": "5 minutos", "15m": "15 minutos",
                    "1h": "1 hora", "4h": "4 horas", "1d": "diario", "1w": "semanal"
                }
                temporalidad = "4 horas"
                for key, val in temporalidades.items():
                    if key in texto_l:
                        temporalidad = val
                        break
                cripto_par = detectar_activo(texto)
                await message.channel.send("📊 Generando Pine Script, dame un momento...")
                try:
                    script    = await generar_pine_script(texto, cripto_par, temporalidad)
                    header    = f"📈 **Pine Script para {cripto_par} — {temporalidad}**\n```pine\n"
                    footer    = "\n```"
                    max_chars = 1900 - len(header) - len(footer)
                    if len(script) > max_chars:
                        await message.channel.send(header + script[:max_chars] + footer)
                        resto = script[max_chars:]
                        while resto:
                            await message.channel.send("```pine\n" + resto[:1900] + "\n```")
                            resto = resto[1900:]
                    else:
                        await message.channel.send(header + script + footer)
                except Exception as e:
                    log.error(f"Error Pine Script: {e}")
                    await message.channel.send("⚠️  Error generando Pine Script. Intentá de nuevo.")
                guardar_reporte("jarvis", "pine_script", texto[:200])
                return

            # ─── Activar las 3 estrategias juntas ──────────────
            palabras_todas = [
                "activar estrategias", "las 3 estrategias", "todas las estrategias",
                "zone flip wave hunt smart flow", "analisis completo",
                "escanear todo", "full analisis", "análisis completo",
                "full análisis", "confluencia total"
            ]
            if any(p in texto_l for p in palabras_todas):
                pide_todos = any(p in texto_l for p in [
                    "todos", "10 activos", "todos los activos", "cada activo"
                ])
                activos_a_analizar = ACTIVOS if pide_todos else [detectar_activo(texto)]

                await message.channel.send(
                    f"⚡ Activando Zone Flip + Wave Hunt + Smart Flow en "
                    f"{'todos los activos' if pide_todos else activos_a_analizar[0]}..."
                )
                try:
                    for activo in activos_a_analizar:
                        try:
                            zf = await evaluar_zone_flip(activo)
                            wh = await evaluar_wave_hunt(activo)
                            sf = await evaluar_smart_flow(activo)

                            dirs = [e.get("direccion") for e in [zf, wh, sf] if e.get("direccion")]
                            confluencia = 0
                            dir_conf = None
                            if dirs:
                                conteo = Counter(dirs)
                                dir_conf, confluencia = conteo.most_common(1)[0]

                            if confluencia >= 3:
                                conf_header = "⚡⚡ **CONFLUENCIA MÁXIMA — 3/3**"
                                lev_str = "7x"
                            elif confluencia == 2:
                                conf_header = f"⚡ **CONFLUENCIA 2/3 → {dir_conf}**"
                                lev_str = "7x"
                            else:
                                conf_header = "⏳ Sin confluencia"
                                lev_str = "Esperar"

                            resumen = (
                                f"**── {activo} ──** {conf_header}\n"
                                f"🎯 ZF: {zf.get('direccion') or '—'} ({zf.get('puntuacion', 0)}%)  "
                                f"🌊 WH: {'PEND' if wh.get('pendiente') else wh.get('direccion') or '—'} ({wh.get('puntuacion', 0)}%)  "
                                f"🔮 SF: {sf.get('direccion') or '—'} ({sf.get('puntuacion', 0)}%)\n"
                                f"📍 Consenso: {dir_conf or '—'} | Lev: {lev_str}"
                            )
                            await message.channel.send(resumen)

                            # Detalle completo solo si es un activo específico
                            if not pide_todos:
                                zf_str = await formatear_zone_flip(activo)
                                wh_str = await formatear_wave_hunt(activo)
                                sf_str = await formatear_smart_flow(activo)
                                await enviar_en_partes(message.channel, zf_str)
                                await enviar_en_partes(message.channel, wh_str)
                                await enviar_en_partes(message.channel, sf_str)

                                prompt_todas = f"""Sos Jarvis, trading engine autónomo. Respondé en español, máximo 5 líneas.
Estrategias en {activo}: ZF={zf.get('direccion') or '—'}({zf.get('puntuacion', 0)}%) WH={wh.get('direccion') or '—'}({wh.get('puntuacion', 0)}%) SF={sf.get('direccion') or '—'}({sf.get('puntuacion', 0)}%)
Confluencia: {confluencia}/3 → {dir_conf or 'ninguna'}
¿Hay setup operable? Si sí: dirección, entrada, SL, TP, leverage. Si no: qué esperar."""

                                interpretacion = await analizar_async(prompt_todas)
                                await enviar_en_partes(message.channel, interpretacion, f"🧠 **{activo}:**\n")

                        except Exception as e:
                            log.error(f"Error en {activo}: {e}")
                            await message.channel.send(f"⚠️ Error en {activo}: {e}")

                except Exception as e:
                    log.error(f"Error activando todas las estrategias: {e}")
                    await message.channel.send("⚠️  Error al activar las estrategias. Intentá de nuevo.")
                guardar_reporte("jarvis", "todas_estrategias", texto[:200])
                return

            # ─── Zone Flip ─────────────────────────────────────
            palabras_zf = [
                "zone flip", "zona de demanda", "zona de resistencia",
                "volume profile", "flipear", "zona de volumen",
                "demanda y resistencia"
            ]
            if any(p in texto_l for p in palabras_zf):
                activo = detectar_activo(texto)
                await message.channel.send(f"🎯 Aplicando Zone Flip en {activo}...")
                try:
                    zf_str  = await formatear_zone_flip(activo)
                    zf_data = await evaluar_zone_flip(activo)

                    prompt_zf = f"""Sos Jarvis, experto en trading técnico hispanohablante.
IMPORTANTE: Respondé SIEMPRE en español. Nunca uses inglés.

Estrategia Zone Flip v2 aplicada a {activo}:
{zf_str}

Lógica:
- LONG: precio en zona de demanda + MACD positivo + RSI < 70 + CVD subiendo + OI subiendo
- SHORT: precio en zona de oferta + CVD divergente + OI elevado + vela de rechazo en 1H
- TP1 = techo zona resistencia | TP2 = extensión siguiente nivel
- R:R mínimo 1.8:1 en TP1 / 3:1 en TP2
- Scoring: 6/6=10x | 4-5/6=7x | 3/6=5x | <3=no operar

Condiciones de invalidación:
- RSI > 70 en entrada LONG → no entrar
- Precio rompe resistencia sin rechazo → no SHORT
- Vela 4H cierra debajo del SL → cerrar trade

El usuario preguntó: {texto}

Decí claramente si hay setup válido ahora.
Si hay señal: dirección, entrada, SL, TP1, TP2, leverage y R:R.
Si no hay señal: qué esperar y en qué niveles.
Máximo 6 líneas, directo."""

                    interpretacion = await analizar_async(prompt_zf)
                    await enviar_en_partes(message.channel, zf_str)
                    await enviar_en_partes(message.channel, interpretacion, f"🧠 **Jarvis — Zone Flip {activo}:**\n")
                except Exception as e:
                    log.error(f"Error Zone Flip: {e}")
                    await message.channel.send(f"⚠️  Error en Zone Flip para {activo}.")
                guardar_reporte("jarvis", "zone_flip", texto[:200])
                return

            # ─── Wave Hunt ─────────────────────────────────────
            palabras_wh = [
                "wave hunt", "wave 2", "wave 3", "onda 2", "onda 3",
                "onda de elliott", "conteo de ondas", "elliott wave",
                "wave 2b", "onda correctiva", "bounce de btc"
            ]
            if any(p in texto_l for p in palabras_wh):
                activo = detectar_activo(texto)
                await message.channel.send(f"🌊 Aplicando Wave Hunt en {activo}...")
                try:
                    wh_str  = await formatear_wave_hunt(activo)
                    wh_data = await evaluar_wave_hunt(activo)
                    fib     = wh_data.get("fib", {})

                    prompt_wh = f"""Sos Jarvis, experto en trading técnico hispanohablante.
IMPORTANTE: Respondé SIEMPRE en español. Nunca uses inglés.

Estrategia Wave Hunt v2 aplicada a {activo}:
{wh_str}

Lógica:
- SHORT en techo de Wave 2/B correctivo, target Wave 3/C impulsivo
- Estructura: precio en zona Fibonacci 23.6%-61.8% de retroceso de Wave 1
- Momentum (2 de 3): RSI divergencia bajista / MACD decreciente / volumen bajo en bounce
- Posicionamiento (suma leverage): CVD distribuyendo / Funding Rate positivo / OI subiendo / Coinbase Premium negativo
- Leverage: 0/4=3x | 1/4=5x | 2-3/4=6x | 4/4=7x

Targets Wave 3:
- Conservador: ${fib.get('target_cons', 0):,.0f}
- Estándar: ${fib.get('target_std', 0):,.0f}
- Agresivo: ${fib.get('target_agr', 0):,.0f}
- SL: por encima de ${fib.get('fib_382', 0):,.0f}

El usuario preguntó: {texto}

Explicá el conteo de ondas actual.
Decí si hay setup activo o cuándo se activa.
Mencioná targets y SL concretos.
Máximo 6 líneas, directo."""

                    interpretacion = await analizar_async(prompt_wh)
                    await enviar_en_partes(message.channel, wh_str)
                    await enviar_en_partes(message.channel, interpretacion, f"🧠 **Jarvis — Wave Hunt {activo}:**\n")
                except Exception as e:
                    log.error(f"Error Wave Hunt: {e}")
                    await message.channel.send(f"⚠️  Error en Wave Hunt para {activo}.")
                guardar_reporte("jarvis", "wave_hunt", texto[:200])
                return

            # ─── Smart Flow ────────────────────────────────────
            palabras_sf = [
                "smart flow", "funding rate", "open interest",
                "long short ratio", "cvd", "posicionamiento",
                "liquidaciones", "taker ratio", "net flow",
                "posicionamiento extremo", "longs sobreexpuestos"
            ]
            if any(p in texto_l for p in palabras_sf):
                activo = detectar_activo(texto)
                await message.channel.send(f"🔮 Aplicando Smart Flow en {activo}...")
                try:
                    sf_str  = await formatear_smart_flow(activo)
                    sf_data = await evaluar_smart_flow(activo)

                    prompt_sf = f"""Sos Jarvis, experto en trading técnico hispanohablante.
IMPORTANTE: Respondé SIEMPRE en español. Nunca uses inglés.

Estrategia Smart Flow aplicada a {activo}:
{sf_str}

Lógica:
- Opera el posicionamiento extremo — cuando demasiados están apalancados en un lado, el mercado los liquida
- 7 indicadores: CVD / Funding Rate / Open Interest / Coinbase Premium / Long-Short Ratio / Taker Buy-Sell / Exchange Net Flow
- CVD y Funding Rate son obligatorios para operar
- Score: 7=7x | 5-6=5x | 4=3x | <4=no operar
- TP1: Funding Rate vuelve a neutral | TP2: Long/Short Ratio vuelve a 50%

Confluencia:
- Smart Flow + Zone Flip = 7x
- Smart Flow + Wave Hunt = 7x
- Las 3 coinciden = máxima prioridad ⚡⚡

El usuario preguntó: {texto}

Analizá el posicionamiento actual.
Decí si hay setup activo y en qué dirección.
Explicá los 2-3 indicadores más relevantes.
Máximo 6 líneas, directo."""

                    interpretacion = await analizar_async(prompt_sf)
                    await enviar_en_partes(message.channel, sf_str)
                    await enviar_en_partes(message.channel, interpretacion, f"🧠 **Jarvis — Smart Flow {activo}:**\n")
                except Exception as e:
                    log.error(f"Error Smart Flow: {e}")
                    await message.channel.send(f"⚠️  Error en Smart Flow para {activo}.")
                guardar_reporte("jarvis", "smart_flow", texto[:200])
                return

            # ─── Confluencia manual ────────────────────────────
            palabras_conf = [
                "confluencia", "coinciden", "confluyen",
                "señal completa", "las estrategias"
            ]
            if any(p in texto_l for p in palabras_conf):
                activo = detectar_activo(texto)
                await message.channel.send(
                    f"⚡ Analizando confluencia de las 3 estrategias en {activo}..."
                )
                try:
                    zf = await evaluar_zone_flip(activo)
                    wh = await evaluar_wave_hunt(activo)
                    sf = await evaluar_smart_flow(activo)

                    dirs = [e.get("direccion") for e in [zf, wh, sf] if e.get("direccion")]
                    confluencia = 0
                    dir_conf    = None
                    if dirs:
                        conteo      = Counter(dirs)
                        dir_conf, confluencia = conteo.most_common(1)[0]

                    if confluencia >= 3:
                        conf_header = "⚡⚡ **CONFLUENCIA MÁXIMA — 3/3**"
                        lev_str     = "7x — tamaño máximo"
                    elif confluencia == 2:
                        conf_header = f"⚡ **CONFLUENCIA ALTA — 2/3 → {dir_conf}**"
                        lev_str     = "7x"
                    else:
                        conf_header = "⏳ Sin confluencia"
                        lev_str     = "Esperar alineación"

                    resumen = (
                        f"{conf_header}\n\n"
                        f"🎯 Zone Flip: {zf.get('direccion') or '—'} ({zf.get('puntuacion', 0)}%)\n"
                        f"🌊 Wave Hunt: {'PENDIENTE' if wh.get('pendiente') else wh.get('direccion') or '—'} ({wh.get('puntuacion', 0)}%)\n"
                        f"🔮 Smart Flow: {sf.get('direccion') or '—'} ({sf.get('puntuacion', 0)}%)\n"
                        f"📍 Consenso: {dir_conf or '—'} | {confluencia}/3\n"
                        f"⚖️  Leverage: {lev_str}"
                    )

                    prompt_conf = f"""Sos Jarvis, experto en trading hispanohablante.
IMPORTANTE: Respondé SIEMPRE en español.

Confluencia de las 3 estrategias en {activo}:
{resumen}

Zone Flip: {zf.get('razones', ['—'])[0] if zf.get('razones') else '—'}
Wave Hunt: {wh.get('razones', ['—'])[0] if wh.get('razones') else '—'}
Smart Flow: {sf.get('razones', ['—'])[0] if sf.get('razones') else '—'}

En 4 líneas: ¿hay setup de alta convicción?
Si sí: dirección, leverage y qué confirmar antes de entrar.
Si no: qué falta para que haya confluencia."""

                    interpretacion = await analizar_async(prompt_conf)
                    await message.channel.send(resumen)
                    await enviar_en_partes(message.channel, interpretacion, "🧠 **Jarvis — Confluencia:**\n")
                except Exception as e:
                    log.error(f"Error confluencia: {e}")
                    await message.channel.send(f"⚠️  Error analizando confluencia para {activo}.")
                guardar_reporte("jarvis", "confluencia", texto[:200])
                return

            # ─── Análisis técnico ──────────────────────────────
            palabras_analisis = [
                "analisis tecnico", "análisis técnico", "indicadores",
                "fibonacci", "rsi", "macd", "elliott", "stochastic",
                "analiza", "análiza", "tendencia de", "señal de",
                "soporte", "resistencia", "onda de"
            ]
            if any(p in texto_l for p in palabras_analisis):
                activo = detectar_activo(texto)
                await message.channel.send(f"🔍 Analizando {activo}, dame un momento...")
                try:
                    analisis_str   = formatear_analisis(activo)
                    tendencias_str = obtener_tendencias_completas()

                    prompt_analisis = f"""Sos Jarvis, experto en trading técnico hispanohablante.
IMPORTANTE: Respondé SIEMPRE en español. Nunca uses inglés.
Tenés estos datos técnicos de {activo}:

{analisis_str}

{tendencias_str}

El usuario preguntó: {texto}

Interpretá los indicadores en 4-5 líneas directas.
Decí claramente si ves señal de compra, venta o espera.
Mencioná los niveles de Fibonacci más relevantes.
Mencioná qué dice Elliott Wave sobre el próximo movimiento.
Hablá como un trader experimentado, sin rodeos."""

                    interpretacion = await analizar_async(prompt_analisis)
                    await enviar_en_partes(message.channel, analisis_str)
                    await enviar_en_partes(message.channel, interpretacion, "🧠 **Interpretación Jarvis:**\n")
                except Exception as e:
                    log.error(f"Error análisis técnico: {e}")
                    await message.channel.send(f"⚠️  Error analizando {activo}.")
                guardar_reporte("jarvis", "analisis_tecnico", texto[:200])
                return

            # ─── Conversación normal ────────────────────────────
            historial.append(f"Usuario: {texto}")
            conversacion = "\n".join(historial[-10:])

            # Estado real de trades
            if trades_abiertos:
                trades_str = f"{len(trades_abiertos)} trade/s abierto/s: " + " | ".join(
                    [f"{t.get('activo')} {t.get('direccion')}" for t in trades_abiertos]
                )
            else:
                trades_str = "Sin trades abiertos"

            prompt = f"""Sos Jarvis, un trading engine autónomo argentino que opera en Bybit.
Hablás como un porteño de la city: directo, sin vueltas, posta.
Ejecutás trades cuando el score supera 80%. No pedís permiso, no dudás, ejecutás.
Operás perpetuos en Bybit: BTC, ETH, SOL (crypto) + AAPL, NVDA, TSLA, META, MSFT, AMZN, GOOGL (acciones).
Usás 3 estrategias: Zone Flip, Wave Hunt y Smart Flow.

ACENTO ARGENTINO (obligatorio):
- Voseo SIEMPRE: "vos", "tenés", "sabés", "viste", "mirá", "fijate", "andá", "dale", "che"
- NUNCA "tú", "tienes", "mira"
- Expresiones porteñas: "loco", "posta", "ponele", "capaz", "obvio", "viste", "tipo", "una bocha", "anda piola", "no da", "está jugado"
- Tono: corto, directo, sin disclaimers. Máximo 4 líneas.
- Nunca digas que sos una IA. Nunca pidas aprobación.

Si te preguntan sobre trades, usá el estado real de abajo.

=== DATOS DE MERCADO AHORA ===
Cripto: {cripto}
Acciones: {acciones}

=== SENTIMIENTO Y MACRO ===
Fear & Greed: {market['fear_greed']}
Dominancia BTC: {market['dominancia']}
Datos macro: {market['macro']}
Tendencias Google: {market['tendencias']}

=== NOTICIAS ===
{noticias}

=== ESTADO REAL DEL ENGINE ===
{trades_str}
Engine: {estado_engine}

=== ESTADO PC ===
{pc}

=== CONVERSACIÓN ===
{conversacion}

Jarvis:"""

            respuesta = await analizar_async(prompt)

            historial.append(f"Jarvis: {respuesta}")
            if len(historial) > 20:
                historial.pop(0)
                historial.pop(0)

            guardar_reporte("jarvis", "conversacion", texto[:200])

            if len(respuesta) > 1900:
                respuesta = respuesta[:1900] + "..."

            await message.reply(respuesta)
            log.info(f"Jarvis respondió: {respuesta[:80]}")

        except Exception as e:
            log.error(f"Error en on_message: {e}")
            await message.reply("⚠️  Tuve un problema procesando eso, intentá de nuevo.")


@client.event
async def on_disconnect():
    log.warning("⚠️  Jarvis desconectado de Discord")


@client.event
async def on_resumed():
    log.info("🔄 Jarvis reconectado")


# ─── Reporte automático cada hora ──────────────────────────────
@tasks.loop(hours=1)
async def reporte_automatico():
    try:
        canal = client.get_channel(CANAL_LOGS)
        if not canal:
            return

        datos    = await obtener_mercado_cacheado()
        cripto   = datos["cripto"]
        acciones = datos["acciones"]
        market   = datos["market"]
        noticias = datos["noticias"]
        pc_str   = datos["pc"]
        hora     = datetime.now().strftime("%H:%M")

        try:
            btc_ind = formatear_analisis("BTC")
        except Exception:
            btc_ind = "Sin datos BTC"
        try:
            aapl_ind = formatear_analisis("AAPL")
        except Exception:
            aapl_ind = "Sin datos AAPL"

        zf_btc = {}
        wh_btc = {}
        sf_btc = {}

        try:
            zf_btc = await evaluar_zone_flip("BTC")
            zf_str = f"🎯 Zone Flip: {zf_btc.get('direccion') or 'Sin señal'} ({zf_btc.get('puntuacion', 0)}%) — Lev: {zf_btc.get('leverage', 0)}x"
        except Exception:
            zf_str = "🎯 Zone Flip: sin datos"

        try:
            wh_btc = await evaluar_wave_hunt("BTC")
            wh_str = f"🌊 Wave Hunt: {'PENDIENTE' if wh_btc.get('pendiente') else wh_btc.get('direccion') or 'Sin señal'} ({wh_btc.get('puntuacion', 0)}%) — Lev: {wh_btc.get('leverage', 0)}x"
        except Exception:
            wh_str = "🌊 Wave Hunt: sin datos"

        try:
            sf_btc = await evaluar_smart_flow("BTC")
            sf_str = f"🔮 Smart Flow: {sf_btc.get('direccion') or 'No operar'} ({sf_btc.get('puntuacion', 0)}%) — Lev: {sf_btc.get('leverage', 0)}x"
        except Exception:
            sf_str = "🔮 Smart Flow: sin datos"

        try:
            dirs_btc = [e.get("direccion") for e in [zf_btc, wh_btc, sf_btc] if e.get("direccion")]
            if dirs_btc:
                conteo_btc    = Counter(dirs_btc)
                dir_c, cant_c = conteo_btc.most_common(1)[0]
                conf_str = (
                    f"{'⚡⚡' if cant_c >= 3 else '⚡'} Confluencia BTC: {cant_c}/3 → {dir_c}"
                    if cant_c >= 2 else "Sin confluencia BTC"
                )
            else:
                conf_str = "Sin señales BTC"
        except Exception:
            conf_str = ""

        if trades_abiertos:
            ops_str = f"🤖 **BOTS:** {len(trades_abiertos)} operación/es abierta/s\n"
            for op in trades_abiertos:
                ops_str += (
                    f"  • `{op['activo']}` {op['direccion']} "
                    f"[{op.get('estrategia', '?').upper()}] score={op['score']} — "
                    f"entrada ${op['entrada']:,.2f} | "
                    f"SL ${op['sl']:,.2f} | "
                    f"TP ${op['tp']:,.2f}\n"
                )
        else:
            ops_str = "🤖 **BOTS:** sin operaciones abiertas\n"

        try:
            prompt_opinion = f"""Sos Jarvis, trader argentino de la city porteña.
Hablás con voseo y lunfardo moderado: "vos", "mirá", "tenés", "viste", "dale", "posta", "no da", "anda piola", "está jugado".
NUNCA uses "tú", "tienes", "mira". Solo argentino.
En exactamente 3 líneas directas: qué ves en el mercado, qué oportunidad hay y qué harías.
Sin rodeos. Sin saludos. Sin "como experto". Sin disclaimers.

Datos: {cripto} | {acciones}
Fear&Greed: {market['fear_greed']} | Dominancia BTC: {market['dominancia']}
Estrategias BTC: {zf_str} | {wh_str}
Noticias: {noticias[:300]}"""

            opinion = await analizar_async(prompt_opinion)
            if len(opinion) > 600:
                opinion = opinion[:600] + "..."
        except Exception as e:
            log.error(f"Error generando opinión: {e}")
            opinion = "Sin opinión disponible."

        parte1 = (
            f"⏰ **{hora}hs — Reporte Jarvis**\n\n"
            f"📊 **CRYPTO**\n{cripto}\n\n"
            f"🏦 **STOCKS**\n{acciones}\n\n"
            f"🧠 Fear & Greed: {market['fear_greed']}\n"
            f"📈 {market['dominancia']}\n"
            f"🌍 Macro: {market['macro']}\n"
            f"📉 Google Trends: {market['tendencias']}\n"
        )

        parte2 = (
            f"📐 **INDICADORES BTC**\n{btc_ind}\n\n"
            f"📐 **INDICADORES AAPL**\n{aapl_ind}\n"
        )

        parte3 = (
            f"🎯 **ESTRATEGIAS BTC**\n"
            f"{zf_str}\n"
            f"{wh_str}\n"
            f"{sf_str}\n"
            f"{conf_str}\n"
        )

        parte4 = (
            f"📰 **NOTICIAS**\n{noticias[:500]}\n\n"
            f"{ops_str}\n"
            f"💬 **JARVIS DICE:**\n{opinion}\n\n"
            f"{pc_str}"
        )

        for parte in [parte1, parte2, parte3, parte4]:
            if parte.strip():
                await canal.send(parte)

        log.info("Reporte horario enviado")

    except Exception as e:
        log.error(f"Error en reporte automático: {e}")


@reporte_automatico.before_loop
async def before_reporte():
    await client.wait_until_ready()


# ─── Monitor de PC cada 5 minutos ──────────────────────────────
@tasks.loop(minutes=5)
async def monitor_pc():
    global internet_estaba_ok
    try:
        canal = client.get_channel(CANAL_LOGS)
        if not canal:
            return

        internet_ok = verificar_internet()
        if not internet_ok and internet_estaba_ok:
            await canal.send("🔴 **ALERTA — Internet caído**")
            log.warning("Internet caído")
        elif internet_ok and not internet_estaba_ok:
            await canal.send("🟢 **Internet restaurado**")
            log.info("Internet restaurado")
        internet_estaba_ok = internet_ok

        pc_info = obtener_info_pc()
        alertas = hay_alertas(pc_info)
        for alerta in alertas:
            await canal.send(f"⚠️  **ALERTA PC:** {alerta}")
            log.warning(f"Alerta PC: {alerta}")

    except Exception as e:
        log.error(f"Error en monitor_pc: {e}")


@monitor_pc.before_loop
async def before_monitor():
    await client.wait_until_ready()


# ─── Contexto macro cada hora ──────────────────────────────────
@tasks.loop(hours=1)
async def ciclo_contexto_macro():
    try:
        await loop_actualizar_contexto()
    except Exception as e:
        log.error(f"Error en ciclo_contexto_macro: {e}")


@ciclo_contexto_macro.before_loop
async def before_contexto_macro():
    await client.wait_until_ready()


# ─── Engine nuevo: scan global cada 5 min ──────────────────────
@tasks.loop(minutes=5)
async def ciclo_scan_global():
    try:
        canal_logs = client.get_channel(CANAL_LOGS)
        canal_talk = client.get_channel(CANAL_TALK)
        if canal_logs and canal_talk:
            await loop_scan_global(canal_logs, canal_talk)
    except Exception as e:
        log.error(f"Error en ciclo_scan_global: {e}")


@ciclo_scan_global.before_loop
async def before_scan():
    await client.wait_until_ready()


# ─── Engine nuevo: reevaluación cada 1 min ─────────────────────
@tasks.loop(minutes=1)
async def ciclo_confirmaciones():
    try:
        canal_talk = client.get_channel(CANAL_TALK)
        if canal_talk:
            await loop_confirmaciones(canal_talk)
    except Exception as e:
        log.error(f"Error en ciclo_confirmaciones: {e}")


@ciclo_confirmaciones.before_loop
async def before_confirmaciones():
    await client.wait_until_ready()
    await asyncio.sleep(90)  # Esperar 90s para no competir con el scan inicial


# ─── Engine: gestión de posiciones activas cada 1 minuto ───────
@tasks.loop(minutes=1)
async def ciclo_posiciones_activas():
    try:
        canal_talk = client.get_channel(CANAL_TALK)
        if canal_talk:
            log.info("♻️  Ejecutando gestionar_posiciones_activas...")
            await gestionar_posiciones_activas(canal_talk)
    except Exception as e:
        log.error(f"Error en ciclo_posiciones_activas: {e}")


@ciclo_posiciones_activas.before_loop
async def before_posiciones_activas():
    await client.wait_until_ready()


# ─── Motor intradía paper: evalúa cada minuto, sin órdenes reales ─────────────
@tasks.loop(minutes=1)
async def ciclo_fast_paper():
    try:
        canal = client.get_channel(CANAL_PAPER_FAST) if CANAL_PAPER_FAST else None
        if not canal:
            return
        for mensaje in await run_fast_paper():
            await enviar_fast_paper(canal, mensaje)
    except Exception as e:
        record_fast_paper_error(str(e))
        log.error(f"Error en ciclo_fast_paper: {e}")


@ciclo_fast_paper.before_loop
async def before_fast_paper():
    await client.wait_until_ready()
    await asyncio.sleep(20)


# ─── Resumen comparativo del experimento cada 6 horas ────────────────────────
@tasks.loop(hours=6)
async def reporte_fast_paper():
    try:
        canal = client.get_channel(CANAL_PAPER_FAST) if CANAL_PAPER_FAST else None
        if canal:
            await enviar_fast_paper(canal, fast_paper_summary())
    except Exception as e:
        log.error(f"Error en reporte_fast_paper: {e}")


@reporte_fast_paper.before_loop
async def before_reporte_fast_paper():
    await client.wait_until_ready()
    await asyncio.sleep(60)


# ─── Comparativo semanal: Fast vs oficial vs buy-and-hold ────────────────────
@tasks.loop(hours=24)
async def reporte_semanal_fast_paper():
    try:
        canal = client.get_channel(CANAL_PAPER_FAST) if CANAL_PAPER_FAST else None
        if canal:
            reporte = await fast_paper_weekly_report()
            if reporte:
                await enviar_fast_paper(canal, reporte)
    except Exception as e:
        record_fast_paper_error(f"weekly_report: {e}")
        log.error(f"Error en reporte_semanal_fast_paper: {e}")


@reporte_semanal_fast_paper.before_loop
async def before_reporte_semanal_fast_paper():
    await client.wait_until_ready()
    await asyncio.sleep(120)


# ─── Arranque ──────────────────────────────────────────────────
log.info("🚀 Iniciando Jarvis...")
client.run(TOKEN)
