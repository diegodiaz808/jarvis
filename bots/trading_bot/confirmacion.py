# ─── Sistema de confirmación en Discord ───────────────────────
import asyncio
from datetime import datetime

confirmaciones_pendientes = {}  # id_mensaje: datos de la operación


def agregar_pendiente(mensaje_id: int, datos: dict):
    confirmaciones_pendientes[mensaje_id] = {
        **datos,
        "timestamp": datetime.now()
    }


def remover_pendiente(mensaje_id: int):
    return confirmaciones_pendientes.pop(mensaje_id, None)


def obtener_pendiente(mensaje_id: int):
    return confirmaciones_pendientes.get(mensaje_id)


def hay_pendientes() -> bool:
    return len(confirmaciones_pendientes) > 0


async def esperar_confirmacion(canal, datos: dict, timeout: int = 180) -> bool:
    """
    Manda el mensaje de confirmación y espera respuesta.
    Retorna True si se aprueba, False si se cancela o vence el tiempo.
    """
    riesgo = datos["riesgo"]
    msg = await canal.send(
        f"⚠️ **JARVIS — CONFIRMACIÓN REQUERIDA**\n\n"
        f"📊 Par: `{datos['par']}`\n"
        f"📍 Dirección: `{datos['direccion']}`\n"
        f"💰 Entrada: `${datos['precio']:,.2f}`\n"
        f"🛡️ Stop Loss: `${riesgo['sl']:,.2f}` (-{riesgo['sl_pct']}%)\n"
        f"🎯 Take Profit: `${riesgo['tp']:,.2f}` (+{riesgo['tp_pct']}%)\n"
        f"⚖️ Riesgo/Beneficio: `1:{riesgo['rr']}`\n\n"
        f"🧠 **ANÁLISIS JARVIS**\n"
        f"{datos['analisis']}\n\n"
        f"📈 Estrategia: `{datos['estrategia'].upper()}`\n"
        f"⚡ Confianza: `{datos['puntuacion']}%`\n\n"
        f"✅ `!aprobar`   ❌ `!cancelar`\n"
        f"⏳ Se descarta en 3 minutos"
    )

    agregar_pendiente(msg.id, datos)

    # Esperar timeout
    await asyncio.sleep(timeout)

    # Si sigue pendiente después del timeout, descartar
    if obtener_pendiente(msg.id):
        remover_pendiente(msg.id)
        await canal.send(
            f"⏰ **Operación descartada por timeout**\n"
            f"`{datos['par']}` — {datos['direccion']} — {datos['puntuacion']}% confianza"
        )
        return False
    return True