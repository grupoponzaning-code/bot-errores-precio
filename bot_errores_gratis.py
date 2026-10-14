#!/usr/bin/env python3
"""
Bot gratuito de errores de precio (Amazon, El Corte Inglés y otras tiendas)

Vigila el feed RSS de ofertas nuevas de Chollometro y avisa por Telegram
cuando una oferta de las tiendas elegidas:
  - menciona "error de precio" o similar, o
  - indica un precio anterior con una bajada >= DESCUENTO_MIN % (50 por defecto).

Nivel del aviso según la bajada:
  🟢 verde    del 50 al 65 %
  🟠 naranja  más del 65 % hasta el 80 %
  🔴 rojo     más del 80 %, o si se menciona "error de precio" sin porcentaje conocido

No necesita ninguna cuenta de pago. Solo un bot de Telegram.

Variables de entorno:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID   (obligatorias)
  TIENDAS        tiendas separadas por comas (por defecto "amazon,corte ingles")
  DESCUENTO_MIN  porcentaje mínimo de bajada (por defecto 50)
  INTERVALO_MIN  minutos entre consultas en modo continuo (por defecto 3)
  AVISAR_TODO    "1" para recibir todas las ofertas de esas tiendas

Uso:
  python3 bot_errores_gratis.py            modo continuo (ordenador o servidor)
  python3 bot_errores_gratis.py --una-vez  una sola pasada (para GitHub Actions)
  python3 bot_errores_gratis.py --chat-id  muestra tu TELEGRAM_CHAT_ID
"""

import html
import json
import os
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
DESCUENTO_MIN = int(os.getenv("DESCUENTO_MIN", "50"))
INTERVALO_MIN = int(os.getenv("INTERVALO_MIN", "3"))
AVISAR_TODO = os.getenv("AVISAR_TODO", "0") == "1"
VENTANA_UNA_VEZ_MIN = int(os.getenv("VENTANA_MIN", "20"))


def normalizar(texto):
    texto = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", texto.lower()).strip()


TIENDAS = [normalizar(t) for t in os.getenv("TIENDAS", "amazon,corte ingles").split(",") if t.strip()]

FEED = "https://www.chollometro.com/rss/nuevos"
CABECERAS = {"User-Agent": "Mozilla/5.0 (bot personal de alertas RSS)"}
ESTADO = Path(__file__).with_name("vistos_gratis.json")

PALABRAS_ERROR = re.compile(
    r"error\s+de\s+precio|precio\s+(err[oó]neo|de\s+error)|posible\s+error|"
    r"\bbug\b|\bglitch\b|error\s+en\s+el\s+precio", re.I)
PRECIO = r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:,\d{1,2})?)\s?€"
PRECIO_ANTES = re.compile(
    r"(?:antes|pvp|precio\s+(?:original|habitual|anterior|normal|recomendado)|era[n]?)\s*:?\s*"
    + PRECIO, re.I)


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def a_float(texto):
    try:
        return float(texto.replace("€", "").strip().replace(".", "").replace(",", "."))
    except (ValueError, AttributeError):
        return None


def limpiar(texto_html):
    return html.unescape(re.sub(r"<[^>]+>", " ", texto_html or ""))


def leer_feed(xml_bytes):
    ofertas = []
    for item in ET.fromstring(xml_bytes).iter("item"):
        o = {"titulo": "", "link": "", "texto": "", "tienda": "", "precio": None, "fecha": None}
        for hijo in item:
            tag = hijo.tag.split("}")[-1]
            if tag == "title":
                o["titulo"] = (hijo.text or "").strip()
            elif tag in ("link", "guid") and not o["link"]:
                o["link"] = (hijo.text or "").strip()
            elif tag == "description":
                o["texto"] = limpiar(hijo.text)
            elif tag == "merchant":
                o["tienda"] = hijo.get("name", "")
                o["precio"] = a_float(hijo.get("price") or "")
            elif tag == "pubDate":
                try:
                    o["fecha"] = parsedate_to_datetime(hijo.text)
                except (TypeError, ValueError):
                    pass
        ofertas.append(o)
    return ofertas


def tienda_vigilada(o):
    tienda = normalizar(o["tienda"])
    return next((t for t in TIENDAS if t in tienda), None)


def nivel(caida):
    if caida is None or caida > 80:
        return "🔴"
    if caida > 65:
        return "🟠"
    return "🟢"


def calcular_caida(o):
    actual = o["precio"]
    if not actual:
        return None, None
    texto = o["titulo"] + " " + o["texto"]
    antes = [p for p in (a_float(m) for m in PRECIO_ANTES.findall(texto)) if p and p > actual]
    if not antes:
        return None, None
    return round((1 - actual / max(antes)) * 100), max(antes)


def evaluar(o):
    """Devuelve (color, motivo) o None si no hay que avisar."""
    if not tienda_vigilada(o):
        return None
    caida, antes = calcular_caida(o)
    mencion = bool(PALABRAS_ERROR.search(o["titulo"] + " " + o["texto"]))
    if caida is not None and caida >= DESCUENTO_MIN:
        motivo = f"-{caida}% (antes {antes:.2f} €)"
        if mencion:
            motivo += " · mencionado como error de precio"
        return nivel(caida), motivo
    if mencion:
        return "🔴", "mencionado como error de precio"
    return ("⚪", "oferta nueva") if AVISAR_TODO else None


def formatear(o, aviso):
    color, motivo = aviso
    precio = f"{o['precio']:.2f} €" if o["precio"] else "ver oferta"
    return (f"{color} <b>{html.escape(o['tienda'] or 'Tienda')}</b>: {html.escape(motivo)}\n"
            f"{html.escape(o['titulo'][:150])}\n\n"
            f"Precio: <b>{precio}</b>\n{o['link']}")


def enviar_telegram(texto):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": texto, "parse_mode": "HTML"},
                      timeout=20)
    r.raise_for_status()


def descargar():
    r = requests.get(FEED, headers=CABECERAS, timeout=30)
    r.raise_for_status()
    return leer_feed(r.content)


def avisar(ofertas, ya_vistos):
    """Envía los avisos de las ofertas no vistas. Devuelve los enlaces procesados."""
    procesados = []
    for o in ofertas:
        if not o["link"] or o["link"] in ya_vistos:
            continue
        procesados.append(o["link"])
        aviso = evaluar(o)
        if aviso:
            try:
                enviar_telegram(formatear(o, aviso))
                log(f"Aviso: {o['tienda']} | {o['titulo'][:60]}")
            except Exception as e:
                log(f"Error enviando a Telegram: {e}")
    return procesados


# ---------- modo continuo ----------

def cargar_vistos():
    try:
        return json.loads(ESTADO.read_text())
    except Exception:
        return {}


def guardar_vistos(vistos):
    limite = time.time() - 14 * 86400
    vistos = {k: v for k, v in vistos.items() if v > limite}
    ESTADO.write_text(json.dumps(vistos))
    return vistos


def modo_continuo():
    vistos = cargar_vistos()
    if not vistos:  # primera vez: no avisar de lo que ya estaba publicado
        try:
            vistos = {o["link"]: time.time() for o in descargar() if o["link"]}
            vistos = guardar_vistos(vistos)
        except Exception as e:
            log(f"Error inicial leyendo el feed: {e}")
    enviar_telegram(f"✅ Bot activo. Tiendas: {', '.join(TIENDAS)}. "
                    f"Reviso cada {INTERVALO_MIN} min.")
    while True:
        time.sleep(INTERVALO_MIN * 60)
        try:
            ofertas = descargar()
        except Exception as e:
            log(f"Error leyendo el feed: {e}")
            continue
        for link in avisar(ofertas, vistos):
            vistos[link] = time.time()
        vistos = guardar_vistos(vistos)


# ---------- modo una vez (GitHub Actions) ----------

def modo_una_vez():
    """Sin estado: solo procesa ofertas publicadas en los últimos VENTANA_MIN minutos.
    Los enlaces ya avisados se guardan en el fichero de estado si existe (caché)."""
    vistos = cargar_vistos()
    ahora = datetime.now(timezone.utc)
    recientes = [o for o in descargar()
                 if o["fecha"] and (ahora - o["fecha"]).total_seconds() < VENTANA_UNA_VEZ_MIN * 60]
    for link in avisar(recientes, vistos):
        vistos[link] = time.time()
    guardar_vistos(vistos)
    log(f"Pasada completada: {len(recientes)} ofertas recientes revisadas")


def mostrar_chat_id():
    r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates", timeout=20)
    for u in r.json().get("result", []):
        chat = (u.get("message") or {}).get("chat")
        if chat:
            print(f"Tu TELEGRAM_CHAT_ID es: {chat['id']}")
            return
    print("No hay mensajes. Escribe cualquier cosa a tu bot en Telegram y vuelve a ejecutar.")


def main():
    if not TG_TOKEN:
        sys.exit("Falta la variable TELEGRAM_TOKEN")
    if "--chat-id" in sys.argv:
        mostrar_chat_id()
        return
    if not TG_CHAT:
        sys.exit("Falta la variable TELEGRAM_CHAT_ID (ejecuta con --chat-id para obtenerla)")
    if "--una-vez" in sys.argv:
        modo_una_vez()
    else:
        modo_continuo()


if __name__ == "__main__":
    main()
