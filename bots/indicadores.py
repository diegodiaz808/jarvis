import requests
import numpy as np


# ─── Datos OHLCV desde Yahoo Finance ───────────────────────────
def obtener_velas(simbolo: str, periodo: str = "3mo", intervalo: str = "1d") -> list:
    try:
        simbolos_map = {
            "BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD",
            "BNB": "BNB-USD", "AVAX": "AVAX-USD", "SPX": "^GSPC",
            "NDX": "^NDX", "AAPL": "AAPL", "NVDA": "NVDA", "GOLD": "GC=F"
        }
        sym = simbolos_map.get(simbolo, simbolo)
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
            f"?interval={intervalo}&range={periodo}"
        )
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()["chart"]["result"][0]
        closes = data["indicators"]["quote"][0]["close"]
        highs = data["indicators"]["quote"][0]["high"]
        lows = data["indicators"]["quote"][0]["low"]
        closes = [c for c in closes if c is not None]
        highs = [h for h in highs if h is not None]
        lows = [l for l in lows if l is not None]
        return {"closes": closes, "highs": highs, "lows": lows}
    except Exception as e:
        return {"error": str(e)}


# ─── RSI ───────────────────────────────────────────────────────
def calcular_rsi(closes: list, periodo: int = 14) -> float:
    if len(closes) < periodo + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    ganancias = [d if d > 0 else 0 for d in deltas]
    perdidas = [abs(d) if d < 0 else 0 for d in deltas]
    avg_g = sum(ganancias[-periodo:]) / periodo
    avg_p = sum(perdidas[-periodo:]) / periodo
    if avg_p == 0:
        return 100.0
    rs = avg_g / avg_p
    return round(100 - (100 / (1 + rs)), 2)


# ─── MACD ──────────────────────────────────────────────────────
def calcular_macd(closes: list, rapida: int = 12, lenta: int = 26, senal: int = 9) -> dict:
    def ema(datos, periodo):
        k = 2 / (periodo + 1)
        ema_vals = [datos[0]]
        for precio in datos[1:]:
            ema_vals.append(precio * k + ema_vals[-1] * (1 - k))
        return ema_vals

    if len(closes) < lenta + senal:
        return {"error": "datos insuficientes"}

    ema_r = ema(closes, rapida)
    ema_l = ema(closes, lenta)
    macd_line = [ema_r[i] - ema_l[i] for i in range(len(ema_l))]
    signal_line = ema(macd_line[-senal * 2:], senal)

    macd_actual = round(macd_line[-1], 4)
    signal_actual = round(signal_line[-1], 4)
    histograma = round(macd_actual - signal_actual, 4)

    tendencia = "alcista" if histograma > 0 else "bajista"
    cruce = None
    if macd_line[-2] < signal_line[-2] and macd_actual > signal_actual:
        cruce = "cruce alcista — señal de compra"
    elif macd_line[-2] > signal_line[-2] and macd_actual < signal_actual:
        cruce = "cruce bajista — señal de venta"

    return {
        "macd": macd_actual,
        "signal": signal_actual,
        "histograma": histograma,
        "tendencia": tendencia,
        "cruce": cruce
    }


# ─── Stochastic Oscillator ─────────────────────────────────────
def calcular_stochastic(closes: list, highs: list, lows: list, periodo: int = 14) -> dict:
    if len(closes) < periodo:
        return {"error": "datos insuficientes"}

    k_values = []
    for i in range(periodo - 1, len(closes)):
        high_max = max(highs[i - periodo + 1:i + 1])
        low_min = min(lows[i - periodo + 1:i + 1])
        if high_max == low_min:
            k_values.append(50)
        else:
            k = ((closes[i] - low_min) / (high_max - low_min)) * 100
            k_values.append(round(k, 2))

    k_actual = k_values[-1]
    d_actual = round(sum(k_values[-3:]) / 3, 2)

    estado = "neutral"
    if k_actual > 80:
        estado = "sobrecompra"
    elif k_actual < 20:
        estado = "sobreventa"

    return {
        "k": k_actual,
        "d": d_actual,
        "estado": estado
    }


# ─── Fibonacci ─────────────────────────────────────────────────
def calcular_fibonacci(highs: list, lows: list) -> dict:
    maximo = max(highs)
    minimo = min(lows)
    rango = maximo - minimo

    niveles = {
        "0%": round(maximo, 4),
        "23.6%": round(maximo - rango * 0.236, 4),
        "38.2%": round(maximo - rango * 0.382, 4),
        "50%": round(maximo - rango * 0.500, 4),
        "61.8%": round(maximo - rango * 0.618, 4),
        "78.6%": round(maximo - rango * 0.786, 4),
        "100%": round(minimo, 4),
    }
    return {
        "maximo": round(maximo, 4),
        "minimo": round(minimo, 4),
        "niveles": niveles
    }


# ─── Elliott Wave (básico) ─────────────────────────────────────
def detectar_elliott(closes: list) -> dict:
    if len(closes) < 10:
        return {"error": "datos insuficientes"}

    # Detectar impulso o corrección básica
    ultimos = closes[-10:]
    subidas = sum(1 for i in range(1, len(ultimos)) if ultimos[i] > ultimos[i - 1])
    bajadas = len(ultimos) - 1 - subidas

    tendencia_general = closes[-1] > closes[0]

    if subidas >= 6 and tendencia_general:
        onda = "Onda impulsiva alcista (posible onda 3 o 5)"
        siguiente = "esperar corrección — posible onda A o B"
    elif bajadas >= 6 and not tendencia_general:
        onda = "Onda correctiva bajista (posible onda A o C)"
        siguiente = "esperar rebote — posible onda B o inicio de nuevo impulso"
    elif subidas >= 4:
        onda = "Movimiento mixto con sesgo alcista"
        siguiente = "confirmar con volumen y MACD"
    else:
        onda = "Movimiento mixto con sesgo bajista"
        siguiente = "confirmar con RSI y Stochastic"

    return {
        "onda": onda,
        "siguiente_movimiento": siguiente,
        "subidas": subidas,
        "bajadas": bajadas
    }


# ─── Análisis completo de un activo ────────────────────────────
def analisis_completo(simbolo: str) -> dict:
    velas = obtener_velas(simbolo)
    if "error" in velas:
        return {"error": velas["error"]}

    closes = velas["closes"]
    highs = velas["highs"]
    lows = velas["lows"]

    return {
        "simbolo": simbolo,
        "rsi": calcular_rsi(closes),
        "macd": calcular_macd(closes),
        "stochastic": calcular_stochastic(closes, highs, lows),
        "fibonacci": calcular_fibonacci(highs, lows),
        "elliott": detectar_elliott(closes),
    }


def formatear_analisis(simbolo: str) -> str:
    a = analisis_completo(simbolo)
    if "error" in a:
        return f"Error analizando {simbolo}: {a['error']}"

    fib = a["fibonacci"]
    macd = a["macd"]
    stoch = a["stochastic"]
    elliott = a["elliott"]

    niveles_fib = " | ".join([f"{k}: ${v:,.2f}" for k, v in fib["niveles"].items()])

    return (
        f"📊 **{simbolo}**\n"
        f"RSI: {a['rsi']} | "
        f"Stoch K:{stoch.get('k', 'N/D')} D:{stoch.get('d', 'N/D')} ({stoch.get('estado', 'N/D')})\n"
        f"MACD: {macd.get('macd', 'N/D')} | Signal: {macd.get('signal', 'N/D')} | "
        f"Tendencia: {macd.get('tendencia', 'N/D')}"
        + (f" | ⚡ {macd.get('cruce')}" if macd.get('cruce') else "") + "\n"
        f"🌊 Elliott: {elliott.get('onda', 'N/D')}\n"
        f"   → Próximo: {elliott.get('siguiente_movimiento', 'N/D')}\n"
        f"📐 Fibonacci — Máx: ${fib['maximo']:,.2f} Mín: ${fib['minimo']:,.2f}\n"
        f"   {niveles_fib}"
    )