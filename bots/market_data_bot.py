import asyncio
import requests
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

_executor = ThreadPoolExecutor(max_workers=4)


# ─── Funciones síncronas (se corren en executor) ───────────────

def _fear_greed_sync() -> str:
    try:
        r = requests.get(
            "https://api.alternative.me/fng/?limit=1",
            timeout=8
        )
        data = r.json()["data"][0]
        return f"{data['value']}/100 — {data['value_classification']}"
    except Exception as e:
        return f"Error fear&greed: {e}"


def _dominancia_sync() -> str:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/global",
            timeout=8
        )
        data         = r.json()["data"]
        dominancia   = data["market_cap_percentage"]["btc"]
        total_market = data["total_market_cap"]["usd"]
        cambio_24h   = data["market_cap_change_percentage_24h_usd"]
        return (
            f"Dominancia BTC: {dominancia:.1f}% | "
            f"Market cap total: ${total_market / 1e12:.2f}T | "
            f"Cambio 24h: {cambio_24h:.1f}%"
        )
    except Exception as e:
        return f"Error dominancia: {e}"


def _tendencias_sync() -> str:
    headers    = {"User-Agent": "Mozilla/5.0"}
    resultados = []

    for kw in ["bitcoin", "crypto", "ethereum"]:
        try:
            url = f"https://trends.google.com/trends/trendingsearches/daily/rss?geo=US&q={kw}"
            r   = requests.get(url, headers=headers, timeout=5)
            if r.status_code != 200 or not r.text.strip():
                continue
            root  = ET.fromstring(r.content)
            items = root.findall(".//item")
            if items:
                titulo = items[0].findtext("title", "").strip()
                if titulo:
                    resultados.append(f"{kw}: {titulo}")
        except Exception:
            continue

    if resultados:
        return " | ".join(resultados)

    try:
        url    = "https://trends.google.com/trending/rss?geo=US"
        r      = requests.get(url, headers=headers, timeout=5)
        root   = ET.fromstring(r.content)
        items  = root.findall(".//item")[:3]
        titulos = [i.findtext("title", "").strip() for i in items if i.findtext("title")]
        if titulos:
            return "Trending US: " + " | ".join(titulos)
    except Exception:
        pass

    return "Google Trends no disponible"


def _macro_sync() -> str:
    try:
        indicadores = {
            "DFF":    "Fed Rate",
            "T10YIE": "Inflación esperada 10Y",
        }
        resultados = []
        for codigo, nombre in indicadores.items():
            url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={codigo}"
            r   = requests.get(url, timeout=8)
            lineas = r.text.strip().split("\n")
            ultima = lineas[-1].split(",")
            resultados.append(f"{nombre}: {ultima[1]}%")
        return " | ".join(resultados)
    except Exception as e:
        return f"Error macro: {e}"


# ─── API pública async ─────────────────────────────────────────

async def obtener_fear_greed() -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _fear_greed_sync)


async def obtener_dominancia_btc() -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _dominancia_sync)


async def obtener_tendencias_google() -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _tendencias_sync)


async def obtener_datos_macro() -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _macro_sync)


async def obtener_todo() -> dict:
    """Corre todas las fuentes en paralelo sin bloquear el event loop."""
    fg, dom, macro, tend = await asyncio.gather(
        obtener_fear_greed(),
        obtener_dominancia_btc(),
        obtener_datos_macro(),
        obtener_tendencias_google(),
        return_exceptions=True,
    )
    return {
        "fear_greed": fg    if isinstance(fg,    str) else "Error fear&greed",
        "dominancia": dom   if isinstance(dom,   str) else "Error dominancia",
        "macro":      macro if isinstance(macro, str) else "Error macro",
        "tendencias": tend  if isinstance(tend,  str) else "Google Trends no disponible",
    }


# ─── Compatibilidad síncrona para código legacy ────────────────
def obtener_todo_sync() -> dict:
    return {
        "fear_greed": _fear_greed_sync(),
        "dominancia": _dominancia_sync(),
        "macro":      _macro_sync(),
        "tendencias": _tendencias_sync(),
    }