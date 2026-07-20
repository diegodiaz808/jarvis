import sqlite3
from datetime import datetime

DB_PATH = "jarvis.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS reportes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_origen TEXT,
        tipo TEXT,
        mensaje TEXT,
        timestamp TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS alertas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nivel TEXT,
        mensaje TEXT,
        timestamp TEXT
    )
    """)

    conn.commit()
    conn.close()


def guardar_reporte(bot_origen, tipo, mensaje):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute(
        "INSERT INTO reportes (bot_origen, tipo, mensaje, timestamp) VALUES (?, ?, ?, ?)",
        (bot_origen, tipo, mensaje, datetime.now().isoformat())
    )

    conn.commit()
    conn.close()


def obtener_ultimos_reportes(n=10):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT * FROM reportes ORDER BY id DESC LIMIT ?", (n,))
    rows = c.fetchall()

    conn.close()
    return rows