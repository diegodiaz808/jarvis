import psutil
import requests
from datetime import datetime


def obtener_info_pc():
    try:
        cpu = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory()
        disco = psutil.disk_usage('/')

        # Temperatura (funciona en Mac con psutil)
        try:
            temps = psutil.sensors_temperatures()
            temp = list(temps.values())[0][0].current if temps else 0
            temp_str = f"{temp:.0f}°C"
        except:
            temp_str = "N/D"

        ram_usada = ram.used / 1e9
        ram_total = ram.total / 1e9
        disco_usado = disco.used / 1e9
        disco_total = disco.total / 1e9

        return {
            "cpu": cpu,
            "ram_usada": ram_usada,
            "ram_total": ram_total,
            "ram_pct": ram.percent,
            "disco_usado": disco_usado,
            "disco_total": disco_total,
            "disco_pct": disco.percent,
            "temp": temp_str,
        }
    except Exception as e:
        return None


def verificar_internet():
    try:
        requests.get("https://google.com", timeout=5)
        return True
    except:
        return False


def formatear_pc(info):
    if not info:
        return "💻 PC: error al leer"
    internet = "✅" if verificar_internet() else "❌"
    return (
        f"💻 CPU {info['cpu']}% | "
        f"RAM {info['ram_usada']:.1f}/{info['ram_total']:.0f}GB ({info['ram_pct']}%) | "
        f"Disco {info['disco_usado']:.0f}/{info['disco_total']:.0f}GB ({info['disco_pct']}%) | "
        f"Temp {info['temp']} | "
        f"Internet {internet}"
    )


def hay_alertas(info):
    alertas = []
    if not info:
        return alertas
    if info['cpu'] > 80:
        alertas.append(f"🔴 CPU alta: {info['cpu']}%")
    if info['ram_pct'] > 90:
        alertas.append(f"🔴 RAM alta: {info['ram_pct']}%")
    if info['disco_pct'] > 90:
        alertas.append(f"🔴 Disco lleno: {info['disco_pct']}%")
    return alertas