"""
Trade Auditor — análisis estadístico de los trades históricos de Jarvis.

Dos capas:
  1. DATOS PUROS: estadística exacta (win rate, PnL, patrones) — sin IA.
  2. OPINIÓN TÉCNICA: prompt para Ollama con persona de trader veterano (25 años).

Fuentes:
  memoria_trades.json → trades cerrados con resultado (WIN/LOSS, PnL, etc.)
  trades.json         → todos los trades abiertos (incluye trade_id, razones, SL/TP)

Filosofía: SOLO INFORMA. No ejecuta ni ajusta nada automáticamente.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("auditor")

TRADE_LOG_PATH = Path("trades.json")
MEMORIA_PATH   = Path("memoria_trades.json")


# ══════════════════════════════════════════════════════════════════
# CARGA DE DATOS
# ══════════════════════════════════════════════════════════════════

def _cargar(path: Path) -> list:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as e:
            log.error(f"Error leyendo {path}: {e}")
            return []
    return []


def _cerrados() -> list[dict]:
    """Trades con resultado definitivo (WIN/LOSS)."""
    m = _cargar(MEMORIA_PATH)
    return [t for t in m if t.get("resultado") in ("WIN", "LOSS")]


def _cerrados_periodo(horas: int) -> list[dict]:
    """Trades cerrados en las últimas N horas."""
    desde = datetime.now() - timedelta(hours=horas)
    out = []
    for t in _cerrados():
        try:
            ts = datetime.fromisoformat(t.get("timestamp", ""))
            if ts >= desde:
                out.append(t)
        except Exception:
            continue
    return out


def _abiertos_actuales() -> list[dict]:
    """Trades que siguen ABIERTOS según trades.json (sin estado CERRADO/SL_HIT)."""
    trades = _cargar(TRADE_LOG_PATH)
    abiertos = []
    for t in trades:
        estado = t.get("estado", "ABIERTO")
        if estado not in ("CERRADO", "SL_HIT", "CERRADO_MANUAL"):
            abiertos.append(t)
    return abiertos


def _pct(parte: int, total: int) -> float:
    return round(parte / total * 100, 1) if total else 0.0


# ══════════════════════════════════════════════════════════════════
# AUDITORÍA GLOBAL — DATOS PUROS
# ══════════════════════════════════════════════════════════════════

def _calcular_metricas(cerrados: list[dict]) -> dict:
    """
    Dado un set de trades cerrados, calcula todas las métricas.
    Usado por auditar_global() y auditar_periodo().
    """
    if not cerrados:
        return {"ok": False, "motivo": "Sin trades cerrados en el rango"}

    wins   = [t for t in cerrados if t["resultado"] == "WIN"]
    losses = [t for t in cerrados if t["resultado"] == "LOSS"]
    total  = len(cerrados)

    pnl_total = sum(t.get("pnl_usdt", 0) for t in cerrados)
    pnl_wins  = sum(t.get("pnl_usdt", 0) for t in wins)
    pnl_losses = sum(t.get("pnl_usdt", 0) for t in losses)

    avg_win  = (pnl_wins / len(wins)) if wins else 0
    avg_loss = (pnl_losses / len(losses)) if losses else 0

    # Profit factor: ganancia bruta / pérdida bruta
    profit_factor = (pnl_wins / abs(pnl_losses)) if pnl_losses else 0

    # ── Por dimensión ────────────────────────────────────────────
    def _agrupar(campo: str) -> dict:
        d = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0})
        for t in cerrados:
            k = t.get(campo, "?")
            if t["resultado"] == "WIN":
                d[k]["w"] += 1
            else:
                d[k]["l"] += 1
            d[k]["pnl"] += t.get("pnl_usdt", 0)
        # Agregar win rate
        out = {}
        for k, v in d.items():
            tot = v["w"] + v["l"]
            out[k] = {
                "wins": v["w"], "losses": v["l"], "total": tot,
                "win_rate": _pct(v["w"], tot),
                "pnl": round(v["pnl"], 4),
            }
        return out

    # ── Análisis de SL/TP ────────────────────────────────────────
    toco_sl  = sum(1 for t in cerrados if t.get("toco_sl"))
    toco_tp1 = sum(1 for t in cerrados if t.get("toco_tp1"))
    toco_tp2 = sum(1 for t in cerrados if t.get("toco_tp2"))
    toco_tp3 = sum(1 for t in cerrados if t.get("toco_tp3"))
    sin_evento = sum(
        1 for t in cerrados
        if not t.get("toco_sl") and t.get("fase_tp_final", 0) == 0
    )

    # ── Análisis por score ───────────────────────────────────────
    score_buckets = defaultdict(lambda: {"w": 0, "l": 0})
    for t in cerrados:
        s = t.get("score_original", 0)
        if s == 0:
            bucket = "sin_score"
        elif s >= 85:
            bucket = "85+"
        elif s >= 75:
            bucket = "75-84"
        elif s >= 70:
            bucket = "70-74"
        else:
            bucket = "<70"
        if t["resultado"] == "WIN":
            score_buckets[bucket]["w"] += 1
        else:
            score_buckets[bucket]["l"] += 1

    return {
        "ok": True,
        "total": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": _pct(len(wins), total),
        "pnl_total": round(pnl_total, 4),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "profit_factor": round(profit_factor, 2),
        "por_activo": _agrupar("activo"),
        "por_direccion": _agrupar("direccion"),
        "por_estrategia": _agrupar("estrategia"),
        "sl_tp": {
            "toco_sl": toco_sl,
            "toco_tp1": toco_tp1,
            "toco_tp2": toco_tp2,
            "toco_tp3": toco_tp3,
            "sin_evento": sin_evento,
            "sl_rate": _pct(toco_sl, total),
        },
        "por_score": dict(score_buckets),
    }


def auditar_global() -> dict:
    """Métricas de TODOS los trades cerrados (sin filtro temporal)."""
    return _calcular_metricas(_cerrados())


def auditar_periodo(horas: int) -> dict:
    """
    Métricas de los trades cerrados en las últimas N horas.
    Incluye también los trades abiertos como contexto.
    """
    cerrados = _cerrados_periodo(horas)
    metricas = _calcular_metricas(cerrados)
    metricas["horas"] = horas
    metricas["abiertos"] = _abiertos_actuales()
    return metricas


# ══════════════════════════════════════════════════════════════════
# AUDITORÍA DE UN TRADE ESPECÍFICO
# ══════════════════════════════════════════════════════════════════

def auditar_trade(trade_id: str) -> dict:
    """
    Busca un trade por su ID (#0001) y devuelve TODOS sus datos,
    cruzando trades.json (apertura) con memoria_trades.json (resultado).
    """
    # Normalizar ID: aceptar "1", "#1", "0001", "#0001"
    tid_norm = trade_id.strip().lstrip("#")
    try:
        tid_num = int(tid_norm)
        tid_fmt = f"#{tid_num:04d}"
    except ValueError:
        return {"ok": False, "motivo": f"ID inválido: {trade_id}"}

    # 1. Buscar en trades.json (datos de apertura)
    trades = _cargar(TRADE_LOG_PATH)
    apertura = next((t for t in trades if t.get("trade_id") == tid_fmt), None)

    if not apertura:
        return {"ok": False, "motivo": f"No existe el trade {tid_fmt}"}

    # 2. Buscar resultado en memoria (por activo + timestamp aproximado)
    memoria = _cargar(MEMORIA_PATH)
    resultado = None
    ts_apertura = apertura.get("timestamp", "")
    for m in memoria:
        # Match por activo + dirección + entrada cercana
        if (
            m.get("activo") == apertura.get("activo")
            and m.get("direccion") == apertura.get("direccion")
            and abs(m.get("entrada", 0) - apertura.get("entrada", 0)) < 0.01
        ):
            resultado = m
            break

    return {
        "ok": True,
        "trade_id": tid_fmt,
        "apertura": apertura,
        "resultado": resultado,  # None si sigue abierto
        "estado": "CERRADO" if resultado else apertura.get("estado", "ABIERTO"),
    }


# ══════════════════════════════════════════════════════════════════
# FORMATEO PARA DISCORD — DATOS PUROS
# ══════════════════════════════════════════════════════════════════

def formatear_global_discord(a: dict) -> str:
    """Formatea la auditoría global en texto plano para Discord."""
    if not a.get("ok"):
        return f"📭 {a.get('motivo', 'Sin datos')}"

    emoji_pnl = "🟢" if a["pnl_total"] >= 0 else "🔴"

    lines = [
        f"📊 **AUDITORÍA GLOBAL — {a['total']} trades cerrados**",
        f"{'─'*40}",
        f"✅ Wins: `{a['wins']}` · ❌ Losses: `{a['losses']}` · WR: `{a['win_rate']}%`",
        f"{emoji_pnl} PnL total: `{a['pnl_total']:+.4f} USDT`",
        f"📈 Avg win: `{a['avg_win']:+.4f}` · 📉 Avg loss: `{a['avg_loss']:+.4f}`",
        f"⚖️ Profit factor: `{a['profit_factor']}`",
        "",
        f"**Por dirección:**",
    ]
    for d, v in a["por_direccion"].items():
        em = "🟢" if v["pnl"] >= 0 else "🔴"
        lines.append(f"  {em} {d}: `{v['wins']}W/{v['losses']}L` ({v['win_rate']}%) · `{v['pnl']:+.3f}`")

    lines.append("")
    lines.append(f"**Por activo:**")
    for act, v in sorted(a["por_activo"].items(), key=lambda x: x[1]["pnl"]):
        em = "🟢" if v["pnl"] >= 0 else "🔴"
        lines.append(f"  {em} {act}: `{v['wins']}W/{v['losses']}L` ({v['win_rate']}%) · `{v['pnl']:+.3f}`")

    lines.append("")
    lines.append(f"**Por estrategia:**")
    for est, v in a["por_estrategia"].items():
        em = "🟢" if v["pnl"] >= 0 else "🔴"
        lines.append(f"  {em} {est}: `{v['wins']}W/{v['losses']}L` ({v['win_rate']}%) · `{v['pnl']:+.3f}`")

    sl = a["sl_tp"]
    lines.append("")
    lines.append(f"**Salidas:**")
    lines.append(f"  🛑 Tocaron SL: `{sl['toco_sl']}` ({sl['sl_rate']}%)")
    lines.append(f"  🎯 TP1: `{sl['toco_tp1']}` · TP2: `{sl['toco_tp2']}` · TP3: `{sl['toco_tp3']}`")
    lines.append(f"  ⚪ Sin evento (cierre prematuro): `{sl['sin_evento']}`")

    return "\n".join(lines)


def formatear_trade_discord(d: dict) -> str:
    """Formatea el análisis de un trade específico."""
    if not d.get("ok"):
        return f"📭 {d.get('motivo', 'No encontrado')}"

    ap  = d["apertura"]
    res = d["resultado"]

    lines = [
        f"🔍 **Trade {d['trade_id']} — {ap.get('activo')} {ap.get('direccion')}**",
        f"{'─'*40}",
        f"📊 Estrategia: `{ap.get('estrategia')}` · Score: `{ap.get('score')}%` · Conf: `{ap.get('confluencia')}/3`",
        f"📍 Entrada: `${ap.get('entrada', 0):,.4f}`",
        f"🛡️ SL: `${ap.get('sl', 0):,.4f}` · 🎯 TP1: `${ap.get('tp1', 0):,.4f}` · TP2: `${ap.get('tp2', 0):,.4f}`",
        f"⚖️ Leverage: `{ap.get('leverage')}x` · Margen: `${ap.get('margen', 0):.2f}` · Qty: `{ap.get('qty')}`",
        f"📅 {ap.get('timestamp', '?')[:19]}",
    ]

    if ap.get("razones"):
        lines.append(f"\n**Razones de entrada:**")
        for r in ap["razones"]:
            lines.append(f"  • {r}")

    if res:
        em = "✅" if res["resultado"] == "WIN" else "❌"
        lines.append(f"\n{'─'*40}")
        lines.append(f"{em} **RESULTADO: {res['resultado']}**")
        lines.append(f"  Cierre: `${res.get('cierre', 0):,.4f}` · PnL: `{res.get('ganancia_pct', 0):+.2f}%` (`{res.get('pnl_usdt', 0):+.4f} USDT`)")
        lines.append(f"  Tocó SL: `{res.get('toco_sl')}` · Fase TP: `{res.get('fase_tp_final', 0)}`")
    else:
        lines.append(f"\n⏳ **Trade aún ABIERTO** (sin resultado todavía)")

    return "\n".join(lines)


def formatear_periodo_discord(a: dict, etiqueta: str = "últimas 24h") -> str:
    """
    Formatea auditoría de un período + lista de trades abiertos.
    """
    if not a.get("ok"):
        return f"📭 Sin trades cerrados en {etiqueta}."

    abiertos = a.get("abiertos", [])
    emoji_pnl = "🟢" if a["pnl_total"] >= 0 else "🔴"

    lines = [
        f"📅 **REPORTE — {etiqueta.upper()}**",
        f"{'─'*40}",
        f"📊 **Cerrados en el período: {a['total']}**",
        f"✅ Wins: `{a['wins']}` · ❌ Losses: `{a['losses']}` · WR: `{a['win_rate']}%`",
        f"{emoji_pnl} PnL del período: `{a['pnl_total']:+.4f} USDT`",
    ]
    if a["wins"] > 0 or a["losses"] > 0:
        lines.append(f"📈 Avg win: `{a['avg_win']:+.4f}` · 📉 Avg loss: `{a['avg_loss']:+.4f}`")
        lines.append(f"⚖️ Profit factor: `{a['profit_factor']}`")

    # Por dirección/activo (compacto)
    if a.get("por_direccion"):
        lines.append("")
        lines.append("**Por dirección:**")
        for d, v in a["por_direccion"].items():
            em = "🟢" if v["pnl"] >= 0 else "🔴"
            lines.append(f"  {em} {d}: `{v['wins']}W/{v['losses']}L` ({v['win_rate']}%) · `{v['pnl']:+.3f}`")

    if a.get("por_activo"):
        lines.append("")
        lines.append("**Por activo:**")
        for act, v in sorted(a["por_activo"].items(), key=lambda x: x[1]["pnl"]):
            em = "🟢" if v["pnl"] >= 0 else "🔴"
            lines.append(f"  {em} {act}: `{v['wins']}W/{v['losses']}L` ({v['win_rate']}%) · `{v['pnl']:+.3f}`")

    # Trades abiertos
    lines.append("")
    lines.append(f"{'─'*40}")
    if abiertos:
        lines.append(f"📂 **Posiciones abiertas ahora: {len(abiertos)}**")
        for t in abiertos:
            tid = t.get("trade_id", "")
            id_str = f"`{tid}` " if tid else ""
            dir_emoji = "🟢" if t.get("direccion") == "LONG" else "🔴"
            lines.append(
                f"  {dir_emoji} {id_str}{t.get('activo')} {t.get('direccion')} "
                f"[{t.get('estrategia', '?')}] · Entrada `${t.get('entrada', 0):,.4f}` · "
                f"Score `{t.get('score', '?')}%`"
            )
    else:
        lines.append("📭 Sin posiciones abiertas en este momento.")

    return "\n".join(lines)


def formatear_periodo_solo_abiertos(etiqueta: str = "ahora") -> str:
    """
    Devuelve solo el listado de trades abiertos (sin métricas históricas).
    Útil cuando preguntan 'qué trades tenés abiertos'.
    """
    abiertos = _abiertos_actuales()
    if not abiertos:
        return "📭 No hay posiciones abiertas en este momento."

    lines = [
        f"📂 **POSICIONES ABIERTAS ({etiqueta}) — {len(abiertos)} trade(s)**",
        f"{'─'*40}",
    ]
    for t in abiertos:
        tid = t.get("trade_id", "")
        id_str = f"`{tid}` " if tid else ""
        dir_emoji = "🟢" if t.get("direccion") == "LONG" else "🔴"
        lines.append(
            f"{dir_emoji} {id_str}**{t.get('activo')} {t.get('direccion')}** "
            f"[{t.get('estrategia', '?')}] · Score `{t.get('score', '?')}%`"
        )
        lines.append(
            f"  📍 Entrada `${t.get('entrada', 0):,.4f}` · "
            f"SL `${t.get('current_sl', t.get('sl', 0)):,.4f}` · "
            f"TP1 `${t.get('tp1', 0):,.4f}` · "
            f"Lev `{t.get('leverage', '?')}x`"
        )
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
# PROMPT PARA OLLAMA — TRADER VETERANO 25 AÑOS
# ══════════════════════════════════════════════════════════════════

def construir_prompt_global(a: dict) -> str:
    """
    Construye el prompt para que Ollama analice como trader veterano.
    Le inyecta los datos REALES para que no invente.
    """
    if not a.get("ok"):
        return ""

    # Resumen compacto de datos para el prompt
    dir_str = " | ".join(
        f"{d}: {v['wins']}W/{v['losses']}L ({v['win_rate']}%) PnL {v['pnl']:+.2f}"
        for d, v in a["por_direccion"].items()
    )
    act_str = " | ".join(
        f"{act}: {v['wins']}W/{v['losses']}L ({v['win_rate']}%) PnL {v['pnl']:+.2f}"
        for act, v in a["por_activo"].items()
    )
    est_str = " | ".join(
        f"{est}: {v['wins']}W/{v['losses']}L ({v['win_rate']}%)"
        for est, v in a["por_estrategia"].items()
    )

    return f"""Sos un trader argentino de Buenos Aires con 25 años de experiencia en mercados.
Pasaste el 2001, el cepo, varias crisis. Operás cripto desde 2017.
Hablás como un porteño laburante de finanzas: directo, técnico, SIN vueltas, SIN disclaimers, SIN "como IA".

REGLAS DE ACENTO ARGENTINO (obligatorio):
- Voseo SIEMPRE: "vos", "tenés", "sabés", "viste", "mirá", "fijate", "andá", "dale", "che"
- NUNCA "tú", "tienes", "sabes", "mira"
- Usá lunfardo moderado de trader: "está jugado", "se viene la mano", "está flojito", "no da", "anda piola", "estás al horno", "te la comiste", "cagada", "una bocha"
- Expresiones tipo: "loco", "boludo" (con cariño, no insultar), "posta", "ponele", "capaz", "obvio"
- Conectores: "tipo", "viste", "che", "dale"
- Tono: mentor exigente, sin filtros, como un viejo del mostrador de la city

DATOS REALES del bot (NO inventes nada, usá SOLO estos números):

═══ GLOBAL ═══
Trades cerrados: {a['total']}
Win rate: {a['win_rate']}% ({a['wins']}W / {a['losses']}L)
PnL total: {a['pnl_total']:+.4f} USDT
Avg win: {a['avg_win']:+.4f} | Avg loss: {a['avg_loss']:+.4f}
Profit factor: {a['profit_factor']}

═══ POR DIRECCIÓN ═══
{dir_str}

═══ POR ACTIVO ═══
{act_str}

═══ POR ESTRATEGIA ═══
{est_str}

═══ SALIDAS ═══
Tocaron SL: {a['sl_tp']['toco_sl']} ({a['sl_tp']['sl_rate']}%)
TP1: {a['sl_tp']['toco_tp1']} | TP2: {a['sl_tp']['toco_tp2']} | TP3: {a['sl_tp']['toco_tp3']}
Cierres prematuros (sin tocar SL ni TP): {a['sl_tp']['sin_evento']}

Tu análisis (máximo 8 líneas):
1. Diagnóstico brutal: ¿está ganando o perdiendo y por qué?
2. El flagelo más grave (lo que más sangra)
3. El acierto (lo que sí funciona)
4. 2-3 recomendaciones CONCRETAS y accionables
No repitas los números crudos, INTERPRETALOS como lo haría un trader experto."""


def construir_prompt_trade(d: dict) -> str:
    """Prompt para analizar UN trade específico como trader veterano."""
    if not d.get("ok"):
        return ""

    ap  = d["apertura"]
    res = d["resultado"]

    razones = "; ".join(ap.get("razones", [])) or "sin razones registradas"

    resultado_str = "TRADE AÚN ABIERTO (sin cerrar)"
    if res:
        resultado_str = (
            f"Resultado: {res['resultado']} | "
            f"Cierre: ${res.get('cierre', 0):,.4f} | "
            f"PnL: {res.get('ganancia_pct', 0):+.2f}% ({res.get('pnl_usdt', 0):+.4f} USDT) | "
            f"Tocó SL: {res.get('toco_sl')} | Fase TP alcanzada: {res.get('fase_tp_final', 0)}"
        )

    return f"""Sos un trader argentino de Buenos Aires con 25 años de experiencia.
Analizás UN trade puntual de forma directa, técnica, sin vueltas.
Hablá SOLO de ESTE trade. NO generalices, NO divagues.

REGLAS DE ACENTO ARGENTINO (obligatorio):
- Voseo: "vos", "tenés", "sabés", "viste", "mirá", "fijate", "andá", "dale", "che"
- NUNCA "tú", "tienes", "sabes"
- Lunfardo de trader: "está jugado", "no da", "te la comiste", "anda piola", "una cagada", "estás al horno"
- Expresiones: "loco", "posta", "ponele", "capaz", "obvio", "viste"
- Tono: mentor exigente, sin filtros. Como un viejo del mostrador de la city porteña.

DATOS REALES del trade {d['trade_id']} (usá SOLO estos datos):

Activo: {ap.get('activo')} {ap.get('direccion')}
Estrategia: {ap.get('estrategia')} | Score: {ap.get('score')}% | Confluencia: {ap.get('confluencia')}/3
Entrada: ${ap.get('entrada', 0):,.4f}
SL: ${ap.get('sl', 0):,.4f} | TP1: ${ap.get('tp1', 0):,.4f} | TP2: ${ap.get('tp2', 0):,.4f} | TP3: ${ap.get('tp3', 0):,.4f}
Leverage: {ap.get('leverage')}x | Margen: ${ap.get('margen', 0):.2f}
Razones de entrada: {razones}
{resultado_str}

Tu análisis (máximo 5 líneas):
1. ¿El setup de entrada estaba bien justificado?
2. ¿La gestión (SL/TP) fue correcta?
3. ¿Qué se aprende de ESTE trade puntual?
Sé específico a este trade, con sus números."""


def construir_prompt_periodo(a: dict, etiqueta: str = "últimas 24h") -> str:
    """
    Prompt para Ollama: análisis del veterano sobre un período + trades activos.
    """
    if not a.get("ok"):
        return ""

    dir_str = " | ".join(
        f"{d}: {v['wins']}W/{v['losses']}L PnL {v['pnl']:+.2f}"
        for d, v in a.get("por_direccion", {}).items()
    ) or "—"

    act_str = " | ".join(
        f"{act}: {v['wins']}W/{v['losses']}L PnL {v['pnl']:+.2f}"
        for act, v in a.get("por_activo", {}).items()
    ) or "—"

    abiertos = a.get("abiertos", [])
    abiertos_str = " | ".join(
        f"{t.get('trade_id', '?')} {t.get('activo')} {t.get('direccion')} "
        f"entrada ${t.get('entrada', 0):,.2f} score {t.get('score', '?')}%"
        for t in abiertos
    ) if abiertos else "ninguna"

    return f"""Sos un trader argentino de Buenos Aires con 25 años de experiencia.
Analizás cómo viene el día/período del bot. Directo, técnico, sin vueltas.
Máximo 6 líneas. SIN disclaimers, SIN "como IA".

REGLAS DE ACENTO ARGENTINO (obligatorio):
- Voseo: "vos", "tenés", "sabés", "viste", "mirá", "fijate", "andá", "dale", "che"
- NUNCA "tú", "tienes", "sabes"
- Lunfardo de trader: "viene flojito", "no da", "te la comiste", "anda piola", "estás al horno", "se viene la mano"
- Expresiones: "loco", "posta", "ponele", "capaz", "obvio", "viste", "tipo"
- Tono: mentor porteño exigente, como un viejo del mostrador de la city.

DATOS REALES del período {etiqueta} (usá SOLO estos números):

═══ CERRADOS EN {etiqueta.upper()} ═══
Total: {a['total']} trades
Win rate: {a['win_rate']}% ({a['wins']}W / {a['losses']}L)
PnL del período: {a['pnl_total']:+.4f} USDT
Avg win: {a['avg_win']:+.4f} | Avg loss: {a['avg_loss']:+.4f}
Profit factor: {a['profit_factor']}

Por dirección: {dir_str}
Por activo: {act_str}

═══ POSICIONES ABIERTAS AHORA ═══
{abiertos_str}

Tu análisis en máximo 6 líneas:
1. ¿Cómo viene el día/período? (1 línea con veredicto)
2. Lo que destaca (positivo o negativo) — 1 línea
3. Las posiciones abiertas: ¿están bien posicionadas según lo visto? — 2 líneas
4. Acción inmediata sugerida — 1-2 líneas
INTERPRETÁ los números, no los repitas."""
