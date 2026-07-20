"""
TradingView Bridge — stub minimal. TV bridge eliminado.
Todos los datos vienen de data_provider (Binance/Bybit/CoinGecko).
"""
from typing import Optional


async def health_check() -> dict:
    return {"success": False, "error": "TV bridge no activo"}


async def get_chart_data(activo: str, timeframe: str = "4h", bars: int = 100) -> dict:
    return {"success": False, "error": "TV bridge no activo"}


async def get_quote(activo: str) -> dict:
    return {"success": False, "error": "TV bridge no activo"}


def get_price(data: dict) -> Optional[float]:
    return None


def get_rsi(data: dict) -> Optional[float]:
    return None


def get_macd(data: dict) -> dict:
    return {}


def get_cvd(data: dict) -> Optional[float]:
    return None


def get_ema(data: dict, period: int) -> Optional[float]:
    return None


def get_bollinger(data: dict) -> dict:
    return {}


def get_ohlcv_bars(data: dict) -> list:
    return []


def get_last_bars(data: dict, n: int = 5) -> list:
    return []


def get_studies_list(data: dict) -> list:
    return []


def find_study(studies_list: list, name_fragment: str) -> Optional[dict]:
    return None


def parse_float(val) -> Optional[float]:
    try:
        return float(str(val).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def resolve_symbol(activo: str) -> str:
    return activo


def resolve_timeframe(tf: str) -> str:
    return tf
