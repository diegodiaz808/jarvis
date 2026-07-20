import requests


# ─── Tendencias crypto (CoinGecko) ─────────────────────────────
def obtener_trending_crypto() -> str:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/search/trending",
            timeout=10
        )
        coins = r.json()["coins"][:5]
        resultado = []
        for c in coins:
            item = c["item"]
            resultado.append(
                f"#{item['market_cap_rank']} {item['name']} ({item['symbol'].upper()})"
            )
        return "🔥 Trending: " + " | ".join(resultado)
    except Exception as e:
        return f"Error trending: {e}"


# ─── Top gainers y losers (CoinGecko) ──────────────────────────
def obtener_gainers_losers() -> str:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets"
            "?vs_currency=usd&order=percent_change_24h_desc&per_page=100&page=1",
            timeout=10
        )
        data = r.json()
        data_filtrada = [d for d in data if d.get("price_change_percentage_24h") is not None]
        gainers = sorted(data_filtrada, key=lambda x: x["price_change_percentage_24h"], reverse=True)[:3]
        losers = sorted(data_filtrada, key=lambda x: x["price_change_percentage_24h"])[:3]

        g_str = " | ".join([f"{c['symbol'].upper()} +{c['price_change_percentage_24h']:.1f}%" for c in gainers])
        l_str = " | ".join([f"{c['symbol'].upper()} {c['price_change_percentage_24h']:.1f}%" for c in losers])

        return f"🟢 Gainers: {g_str}\n🔴 Losers: {l_str}"
    except Exception as e:
        return f"Error gainers/losers: {e}"


# ─── Historial de dominancia BTC ───────────────────────────────
def obtener_historial_dominancia() -> str:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/global",
            timeout=10
        )
        data = r.json()["data"]
        dom_btc = data["market_cap_percentage"]["btc"]
        dom_eth = data["market_cap_percentage"]["eth"]
        dom_otros = 100 - dom_btc - dom_eth

        if dom_btc > 55:
            interpretacion = "BTC dominante — altcoins en riesgo"
        elif dom_btc < 45:
            interpretacion = "Altseason posible — BTC perdiendo dominio"
        else:
            interpretacion = "Mercado equilibrado"

        return (
            f"BTC: {dom_btc:.1f}% | ETH: {dom_eth:.1f}% | Otros: {dom_otros:.1f}%\n"
            f"→ {interpretacion}"
        )
    except Exception as e:
        return f"Error dominancia: {e}"


# ─── Resumen completo de tendencias ────────────────────────────
def obtener_tendencias_completas() -> str:
    trending = obtener_trending_crypto()
    gainers = obtener_gainers_losers()
    dominancia = obtener_historial_dominancia()
    return f"{trending}\n{gainers}\n📈 Dominancia: {dominancia}"