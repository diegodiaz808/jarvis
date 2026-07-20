import requests


def obtener_datos_cripto():
    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {
            "ids": "bitcoin,ethereum,solana",
            "vs_currencies": "usd",
            "include_24hr_change": "true"
        }
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        btc = data["bitcoin"]
        eth = data["ethereum"]
        sol = data["solana"]
        return (
            f"BTC: ${btc['usd']:,.0f} ({btc['usd_24h_change']:.1f}%) | "
            f"ETH: ${eth['usd']:,.0f} ({eth['usd_24h_change']:.1f}%) | "
            f"SOL: ${sol['usd']:,.0f} ({sol['usd_24h_change']:.1f}%)"
        )
    except Exception as e:
        return f"Error cripto: {e}"