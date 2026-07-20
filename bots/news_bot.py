import requests
import xml.etree.ElementTree as ET


def obtener_noticias():
    # ── Intento 1: CoinDesk RSS ───────────────────────────────
    try:
        r = requests.get(
            "https://www.coindesk.com/arc/outboundfeeds/rss/",
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        root = ET.fromstring(r.content)
        items = root.findall(".//item")[:5]
        noticias = []
        for item in items:
            titulo = item.findtext("title", "").strip()
            if titulo:
                noticias.append(f"⚪ {titulo}")
        if noticias:
            return "\n".join(noticias)
    except Exception:
        pass

    # ── Intento 2: Cointelegraph RSS ──────────────────────────
    try:
        r = requests.get(
            "https://cointelegraph.com/rss",
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        root = ET.fromstring(r.content)
        items = root.findall(".//item")[:5]
        noticias = []
        for item in items:
            titulo = item.findtext("title", "").strip()
            if titulo:
                noticias.append(f"⚪ {titulo}")
        if noticias:
            return "\n".join(noticias)
    except Exception:
        pass

    return "⚠️ Noticias no disponibles"