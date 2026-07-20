import requests


def obtener_datos_acciones():
    try:
        simbolos = {
            "AAPL":  "Apple",
            "TSLA":  "Tesla",
            "NVDA":  "Nvidia",
            "META":  "Meta",
            "MSFT":  "Microsoft",
            "AMZN":  "Amazon",
            "GOOGL": "Google",
        }
        resultados = []
        for simbolo, nombre in simbolos.items():
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{simbolo}?interval=1d&range=1d"
            headers = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(url, headers=headers, timeout=10)
            meta = r.json()["chart"]["result"][0]["meta"]
            precio = meta["regularMarketPrice"]
            prev = meta["chartPreviousClose"]
            cambio = ((precio - prev) / prev) * 100
            emoji = "▲" if cambio > 0 else "▼"
            resultados.append(f"{nombre}: ${precio:,.2f} {emoji}{abs(cambio):.1f}%")
        return " | ".join(resultados)
    except Exception as e:
        return f"Error acciones: {e}"