# ─── Estrategias de trading ────────────────────────────────────
import requests
from bots.indicadores import analisis_completo

PARES = [
    # Crypto
    "BTC/USDT", "ETH/USDT", "SOL/USDT",
    # Tradicional
    "AAPL", "NVDA", "TSLA", "META", "MSFT", "AMZN", "GOOGL",
]

# Mapeo Binance para crypto
BINANCE_SYMBOLS = {
    "BTC/USDT": "BTCUSDT",
    "ETH/USDT": "ETHUSDT",
    "SOL/USDT": "SOLUSDT",
}

# Mapeo Yahoo para tradfi
YAHOO_SYMBOLS = {
    "TSLA": "TSLA",
    "AAPL": "AAPL",
    "NVDA": "NVDA",
}


# ─── Mapeo de par a símbolo para indicadores ───────────────────
def par_a_simbolo(par: str) -> str:
    return par.split("/")[0] if "/" in par else par


# ─── Precio actual del par ─────────────────────────────────────
def obtener_precio_par(par: str) -> dict:
    try:
        if "USDT" in par:
            # ── Binance API — sin rate limit, sin key ─────────
            sym = BINANCE_SYMBOLS.get(par, par.replace("/", ""))
            r = requests.get(
                f"https://api.binance.com/api/v3/ticker/24hr?symbol={sym}",
                timeout=10
            )
            data = r.json()
            return {
                "par": par,
                "precio": float(data["lastPrice"]),
                "cambio_24h": float(data["priceChangePercent"]),
                "volumen_24h": float(data["quoteVolume"]),
                "tipo": "crypto"
            }
        else:
            # ── Yahoo Finance para tradfi ──────────────────────
            simbolo = YAHOO_SYMBOLS.get(par, par)
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{simbolo}?interval=1d&range=2d"
            headers = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(url, headers=headers, timeout=15)
            meta = r.json()["chart"]["result"][0]["meta"]
            precio = meta["regularMarketPrice"]
            prev = meta["chartPreviousClose"]
            cambio = ((precio - prev) / prev) * 100
            volumen = meta.get("regularMarketVolume", 0)
            return {
                "par": par,
                "precio": precio,
                "cambio_24h": cambio,
                "volumen_24h": volumen,
                "tipo": "tradicional"
            }
    except Exception as e:
        return {"par": par, "error": str(e)}


# ─── Obtener indicadores técnicos reales ───────────────────────
def obtener_indicadores(par: str) -> dict:
    simbolo = par_a_simbolo(par)
    try:
        ind = analisis_completo(simbolo)
        if "error" in ind:
            return {
                "rsi": 50, "macd": {}, "stochastic": {},
                "fibonacci": {}, "elliott": {}, "error": ind["error"]
            }
        return ind
    except Exception as e:
        return {
            "rsi": 50, "macd": {}, "stochastic": {},
            "fibonacci": {}, "elliott": {}, "error": str(e)
        }


# ─── Estrategia 1: Momentum ────────────────────────────────────
def puntuar_momentum(datos: dict, fear_greed: int) -> dict:
    """Seguir la tendencia — entra cuando el movimiento es fuerte y confirmado"""
    if "error" in datos:
        return {"puntuacion": 0, "direccion": None, "razones": ["Error de datos"]}

    puntos = 0
    razones = []
    cambio = datos["cambio_24h"]
    ind = obtener_indicadores(datos["par"])
    rsi = ind.get("rsi", 50)
    macd = ind.get("macd", {})
    stoch = ind.get("stochastic", {})
    elliott = ind.get("elliott", {})
    fib = ind.get("fibonacci", {})
    es_short = False

    # ── Cambio de precio ───────────────────────────────────────
    if cambio > 3:
        puntos += 20
        razones.append(f"subida fuerte {cambio:.1f}%")
    elif cambio > 1:
        puntos += 12
        razones.append(f"subida moderada {cambio:.1f}%")
    elif cambio < -3:
        puntos += 20
        razones.append(f"caída fuerte {cambio:.1f}%")
        es_short = True
    elif cambio < -1:
        puntos += 12
        razones.append(f"caída moderada {cambio:.1f}%")
        es_short = True

    # ── RSI ────────────────────────────────────────────────────
    if not es_short and 50 < rsi < 70:
        puntos += 15
        razones.append(f"RSI alcista ({rsi})")
    elif not es_short and rsi < 30:
        puntos += 20
        razones.append(f"RSI en sobreventa ({rsi}) — rebote probable")
    elif es_short and 30 < rsi < 50:
        puntos += 15
        razones.append(f"RSI bajista ({rsi})")
    elif es_short and rsi > 70:
        puntos += 20
        razones.append(f"RSI en sobrecompra ({rsi}) — corrección probable")

    # ── MACD ───────────────────────────────────────────────────
    if macd.get("cruce") == "cruce alcista — señal de compra" and not es_short:
        puntos += 20
        razones.append("MACD cruce alcista confirmado")
    elif macd.get("cruce") == "cruce bajista — señal de venta" and es_short:
        puntos += 20
        razones.append("MACD cruce bajista confirmado")
    elif macd.get("tendencia") == "alcista" and not es_short:
        puntos += 10
        razones.append("MACD tendencia alcista")
    elif macd.get("tendencia") == "bajista" and es_short:
        puntos += 10
        razones.append("MACD tendencia bajista")

    # ── Stochastic ─────────────────────────────────────────────
    if stoch.get("estado") == "sobreventa" and not es_short:
        puntos += 10
        razones.append(f"Stochastic en sobreventa (K:{stoch.get('k')})")
    elif stoch.get("estado") == "sobrecompra" and es_short:
        puntos += 10
        razones.append(f"Stochastic en sobrecompra (K:{stoch.get('k')})")

    # ── Elliott Wave ───────────────────────────────────────────
    if elliott.get("onda"):
        if "alcista" in elliott["onda"] and not es_short:
            puntos += 10
            razones.append(f"Elliott: {elliott['onda']}")
        elif "bajista" in elliott["onda"] and es_short:
            puntos += 10
            razones.append(f"Elliott: {elliott['onda']}")

    # ── Fibonacci — soporte/resistencia ────────────────────────
    if fib.get("niveles") and datos.get("precio"):
        precio_actual = datos["precio"]
        niveles = fib["niveles"]
        nivel_50 = niveles.get("50%", 0)
        nivel_618 = niveles.get("61.8%", 0)
        if not es_short and abs(precio_actual - nivel_618) / precio_actual < 0.01:
            puntos += 10
            razones.append(f"Precio en soporte Fib 61.8% (${nivel_618:,.2f})")
        elif es_short and abs(precio_actual - nivel_50) / precio_actual < 0.01:
            puntos += 10
            razones.append(f"Precio en resistencia Fib 50% (${nivel_50:,.2f})")

    # ── Fear & Greed ───────────────────────────────────────────
    if fear_greed < 30 and not es_short:
        puntos += 15
        razones.append("miedo extremo con momentum alcista — señal fuerte long")
    elif fear_greed > 70 and es_short:
        puntos += 15
        razones.append("euforia con momentum bajista — señal fuerte short")
    elif 40 <= fear_greed <= 60:
        puntos += 5
        razones.append("sentimiento neutral")

    # ── Volumen ────────────────────────────────────────────────
    if datos["volumen_24h"] > 0:
        puntos += 8
        razones.append("volumen activo")

    direccion = "SHORT" if es_short else "LONG"
    return {
        "puntuacion": min(puntos, 100),
        "direccion": direccion,
        "estrategia": "momentum",
        "razones": razones,
        "indicadores": {
            "rsi": rsi,
            "macd": macd.get("tendencia", "N/D"),
            "stoch": stoch.get("estado", "N/D"),
            "elliott": elliott.get("onda", "N/D"),
        }
    }


# ─── Estrategia 2: Reversión a la media ────────────────────────
def puntuar_reversion(datos: dict, fear_greed: int) -> dict:
    """Ir contra el extremo — entra cuando el mercado está sobreextendido"""
    if "error" in datos:
        return {"puntuacion": 0, "direccion": None, "razones": ["Error de datos"]}

    puntos = 0
    razones = []
    cambio = datos["cambio_24h"]
    ind = obtener_indicadores(datos["par"])
    rsi = ind.get("rsi", 50)
    macd = ind.get("macd", {})
    stoch = ind.get("stochastic", {})
    elliott = ind.get("elliott", {})
    fib = ind.get("fibonacci", {})
    direccion = None

    # ── Sobreextensión de precio ───────────────────────────────
    if cambio > 8:
        puntos += 25
        razones.append(f"sobreextendido al alza {cambio:.1f}% — corrección probable")
        direccion = "SHORT"
    elif cambio < -8:
        puntos += 25
        razones.append(f"sobreextendido a la baja {cambio:.1f}% — rebote probable")
        direccion = "LONG"
    elif cambio > 5:
        puntos += 15
        razones.append(f"extendido al alza {cambio:.1f}%")
        direccion = "SHORT"
    elif cambio < -5:
        puntos += 15
        razones.append(f"extendido a la baja {cambio:.1f}%")
        direccion = "LONG"
    else:
        return {"puntuacion": 0, "direccion": None, "razones": ["sin sobreextensión suficiente"]}

    # ── RSI confirma sobreextensión ────────────────────────────
    if direccion == "SHORT" and rsi > 70:
        puntos += 20
        razones.append(f"RSI en sobrecompra extrema ({rsi}) — reversión inminente")
    elif direccion == "LONG" and rsi < 30:
        puntos += 20
        razones.append(f"RSI en sobreventa extrema ({rsi}) — rebote inminente")
    elif direccion == "SHORT" and rsi > 60:
        puntos += 10
        razones.append(f"RSI elevado ({rsi})")
    elif direccion == "LONG" and rsi < 40:
        puntos += 10
        razones.append(f"RSI deprimido ({rsi})")

    # ── MACD agotamiento ───────────────────────────────────────
    if direccion == "SHORT" and macd.get("cruce") == "cruce bajista — señal de venta":
        puntos += 15
        razones.append("MACD confirma agotamiento alcista")
    elif direccion == "LONG" and macd.get("cruce") == "cruce alcista — señal de compra":
        puntos += 15
        razones.append("MACD confirma agotamiento bajista")

    # ── Stochastic en extremo ──────────────────────────────────
    if direccion == "SHORT" and stoch.get("estado") == "sobrecompra":
        puntos += 12
        razones.append(f"Stochastic en sobrecompra (K:{stoch.get('k')}) — reversión")
    elif direccion == "LONG" and stoch.get("estado") == "sobreventa":
        puntos += 12
        razones.append(f"Stochastic en sobreventa (K:{stoch.get('k')}) — rebote")

    # ── Elliott Wave correctivo ────────────────────────────────
    if elliott.get("onda"):
        if direccion == "SHORT" and "correctiva" in elliott.get("siguiente_movimiento", ""):
            puntos += 10
            razones.append("Elliott sugiere corrección próxima")
        elif direccion == "LONG" and "rebote" in elliott.get("siguiente_movimiento", ""):
            puntos += 10
            razones.append("Elliott sugiere rebote próximo")

    # ── Fibonacci — precio en resistencia/soporte clave ────────
    if fib.get("niveles") and datos.get("precio"):
        precio_actual = datos["precio"]
        niveles = fib["niveles"]
        for nivel_nombre, nivel_valor in niveles.items():
            if nivel_valor == 0:
                continue
            distancia = abs(precio_actual - nivel_valor) / precio_actual
            if distancia < 0.005:
                if direccion == "SHORT":
                    puntos += 12
                    razones.append(f"Precio tocando resistencia Fib {nivel_nombre} (${nivel_valor:,.2f})")
                else:
                    puntos += 12
                    razones.append(f"Precio tocando soporte Fib {nivel_nombre} (${nivel_valor:,.2f})")
                break

    # ── Fear & Greed extremo ───────────────────────────────────
    if fear_greed > 80 and direccion == "SHORT":
        puntos += 15
        razones.append("euforia extrema — alta probabilidad de corrección")
    elif fear_greed < 20 and direccion == "LONG":
        puntos += 15
        razones.append("miedo extremo — alta probabilidad de rebote")
    elif fear_greed > 70 and direccion == "SHORT":
        puntos += 8
        razones.append("sentimiento muy alcista — precaución")
    elif fear_greed < 30 and direccion == "LONG":
        puntos += 8
        razones.append("sentimiento muy bajista — oportunidad")

    # ── Volumen confirma ───────────────────────────────────────
    if datos["volumen_24h"] > 0:
        puntos += 8
        razones.append("volumen activo confirma movimiento")

    return {
        "puntuacion": min(puntos, 100),
        "direccion": direccion,
        "estrategia": "reversion",
        "razones": razones,
        "indicadores": {
            "rsi": rsi,
            "macd": macd.get("tendencia", "N/D"),
            "stoch": stoch.get("estado", "N/D"),
            "elliott": elliott.get("onda", "N/D"),
        }
    }