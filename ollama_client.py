import asyncio
import os

import aiohttp
import requests

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OLLAMA_URL = f"{OLLAMA_BASE_URL}/api/generate"
MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "120"))


def _error_ollama(exc: Exception) -> str:
    detalle = str(exc).strip()
    if isinstance(exc, (aiohttp.ClientConnectorError, requests.exceptions.ConnectionError)):
        return (
            "Error con Ollama: el servicio local no esta respondiendo en "
            f"{OLLAMA_BASE_URL}. Inicia Ollama y verifica que el modelo `{MODEL}` este disponible."
        )
    if isinstance(exc, (asyncio.TimeoutError, requests.exceptions.Timeout)):
        return "Error con Ollama: timeout esperando respuesta del modelo."
    return f"Error con Ollama: {detalle or type(exc).__name__}"


async def _parse_ollama_response(r: aiohttp.ClientResponse) -> str:
    if r.status >= 400:
        detalle = (await r.text()).strip()
        return f"Error con Ollama ({r.status}): {detalle or r.reason}"

    data = await r.json(content_type=None)
    if data.get("error"):
        return f"Error con Ollama: {data['error']}"

    respuesta = (data.get("response") or "").strip()
    return respuesta or "Error con Ollama: Ollama respondio sin texto."


def _parse_ollama_response_sync(r: requests.Response) -> str:
    if r.status_code >= 400:
        detalle = r.text.strip()
        return f"Error con Ollama ({r.status_code}): {detalle or r.reason}"

    data = r.json()
    if data.get("error"):
        return f"Error con Ollama: {data['error']}"

    respuesta = (data.get("response") or "").strip()
    return respuesta or "Error con Ollama: Ollama respondio sin texto."

# ─── Versión asíncrona (para Discord) ──────────────────────────
async def analizar_async(prompt: str) -> str:
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                OLLAMA_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=OLLAMA_TIMEOUT)
            ) as r:
                return await _parse_ollama_response(r)
    except Exception as e:
        return _error_ollama(e)

# ─── Versión síncrona (para scripts externos) ──────────────────
def analizar(prompt: str) -> str:
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False
    }
    try:
        r = requests.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT)
        return _parse_ollama_response_sync(r)
    except Exception as e:
        return _error_ollama(e)

# ─── Pine Script (asíncrono) ───────────────────────────────────
async def generar_pine_script(descripcion: str, cripto: str, temporalidad: str) -> str:
    prompt = f"""Sos un experto en Pine Script v5 para TradingView.
Generás scripts completos, funcionales y listos para usar.
Solo respondés con el código Pine Script, sin explicaciones antes ni después.
El código debe estar entre //@version=5 y el final del script.

Generá un script de Pine Script v5 para TradingView con estas características:
- Activo: {cripto}
- Temporalidad sugerida: {temporalidad}
- Estrategia/indicador: {descripcion}

Requisitos del script:
- Usar Pine Script v5
- Incluir entradas configurables (input) para los parámetros principales
- Mostrar señales de entrada y salida claramente en el gráfico
- Incluir alertas (alertcondition) para las señales principales
- Comentarios en español explicando cada sección
- Si es estrategia: incluir strategy.entry y strategy.close
- Si es indicador: usar indicator() con overlay cuando corresponda

Solo respondé con el código, nada más."""

    return await analizar_async(prompt)
