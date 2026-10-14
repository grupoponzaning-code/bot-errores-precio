#!/usr/bin/env python3
"""
Bot gratuito de grandes bajadas de precio (todas las tiendas)

Lee el listado de ofertas nuevas de Chollometro (varias páginas) y avisa por
Telegram de cada oferta cuyo precio esté al menos DESCUENTO_MIN % por debajo
del precio de referencia que muestra Chollometro (el precio tachado).

Nivel del aviso según la bajada:
  🟢 verde    del 50 al 65 %
  🟠 naranja  más del 65 % hasta el 80 %
  🔴 rojo     más del 80 %, o si el título menciona "error de precio"

Variables de entorno:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID   (obligatorias)
  DESCUENTO_MIN  porcentaje mínimo de bajada (por defecto 50)
  TIENDAS        opcional: limitar a estas tiendas, separadas por comas.
                 Vacío (por defecto) = todas las tiendas.
  VENTANA_MIN    minutos hacia atrás que se revisan en cada pasada (por defecto 180)
  INTERVALO_MIN  minutos entre consultas en modo continuo (por defecto 3)

Uso:
  python3 bot_errores_gratis.py            modo continuo (ordenador o servidor)
  python3 bot_errores_gratis.py --una-vez  una sola pasada (para GitHub Actions)
"""

import html
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import requests

TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
DESCUENTO_MIN = int(os.getenv("DESCUENTO_MIN") or "50")
VENTANA_MIN = int(os.getenv("VENTANA_MIN") or "180")
INTERVALO_MIN = int(os.getenv("INTERVALO_MIN") or "3")
MAX_PAGINAS = 10


def normalizar(texto):
    texto = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", texto.lower()).strip()


TIENDAS = [normalizar(t) for t in (os.getenv("TIENDAS") or "").split(",") if t.strip()]

LISTADO = "https://www.chollometro.com/nuevos?page={}"
CABECERAS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9",
}
ESTADO = Path(__file__).with_name("vistos_gratis.json")
BLOQUE = re.compile(r"data-vue3='([^']*ThreadMainListItemNormalizer[^']*)'")
PALABRAS_ERROR = re.compile(
    r"error\s+de\s+precio|precio\s+(err[oó]neo|de\s+error)|posible\s+error|"
    r"error\s+en\s+el\s+precio|\bglitch\b", re.I)


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def leer_listado(html_texto):
    """Extrae las ofertas de una página del listado de Chollometro."""
    ofertas = []
    for bloque in BLOQUE.findall(html_texto):
        try:
            hilo = json.loads(html.unescape(bloque))["props"]["thread"]
        except (ValueError, KeyError, TypeError):
            continue
        ofertas.append({
            "id": str(hilo.get("threadId")),
            "titulo": hilo.get("title") or "",
            "tienda": (hilo.get("merchant") or {}).get("merchantName") or hilo.get("linkHost") or "",
            "precio": hilo.get("price"),
            "referencia": hilo.get("nextBestPrice"),
            "envio": (hilo.get("shipping") or {}).get("price"),
            "publicada": hilo.get("publishedAt") or 0,
            "caducada": bool(hilo.get("isExpired")),
            "link": f"https://www.chollometro.com/ofertas/{hilo.get('titleSlug')}-{hilo.get('threadId')}",
        })
    return ofertas


def descargar(ventana_min):
    """Descarga páginas del listado hasta cubrir la ventana de tiempo."""
    limite = time.time() - ventana_min * 60
    todas = {}
    for pagina in range(1, MAX_PAGINAS + 1):
        r = requests.get(LISTADO.format(pagina), headers=CABECERAS, timeout=30)
        r.raise_for_status()
        ofertas = leer_listado(r.text)
        if not ofertas:
            log(f"Página {pagina} sin ofertas legibles (¿cambió la web?)")
            break
        for o in ofertas:
            todas[o["id"]] = o
        if min(o["publicada"] for o in ofertas) < limite:
            break
        time.sleep(1)
    return [o for o in todas.values() if o["publicada"] >= limite]


def tienda_vigilada(o):
    if not TIENDAS:
        return True
    tienda = normalizar(o["tienda"])
    return any(t in tienda for t in TIENDAS)


def nivel(caida):
    if caida > 80:
        return "🔴"
    if caida > 65:
        return "🟠"
    return "🟢"


def evaluar(o):
    """Devuelve (color, caida) o None si no hay que avisar."""
    if o["caducada"] or not tienda_vigilada(o):
        return None
    precio, ref = o["precio"], o["referencia"]
    caida = None
    if precio and ref and ref > precio > 0:
        caida = round((1 - precio / ref) * 100)
    if caida is not None and caida >= DESCUENTO_MIN:
        return nivel(caida), caida
    if PALABRAS_ERROR.search(o["titulo"]):
        return "🔴", caida
    return None


def formatear(o, aviso):
    color, caida = aviso
    cabecera = f"-{caida}%" if caida is not None else "Posible error de precio"
    lineas = [f"{color} <b>{cabecera}</b> · {html.escape(o['tienda'] or 'Tienda')}",
              html.escape(o["titulo"][:150]), ""]
    precio = f"<b>{o['precio']:.2f} €</b>" if o["precio"] else "ver oferta"
    if o["referencia"]:
        precio += f"  (antes {o['referencia']:.2f} €)"
    lineas.append(f"Precio: {precio}")
    if o["envio"]:
        lineas.append(f"Envío: {o['envio']:.2f} €")
    lineas.append(o["link"])
    return "\n".join(lineas)


def enviar_telegram(texto):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": texto, "parse_mode": "HTML"},
                      timeout=20)
    r.raise_for_status()


def cargar_vistos():
    try:
        return json.loads(ESTADO.read_text())
    except Exception:
        return {}


def guardar_vistos(vistos):
    limite = time.time() - 7 * 86400
    vistos = {k: v for k, v in vistos.items() if v > limite}
    ESTADO.write_text(json.dumps(vistos))
    return vistos


def pasada(vistos):
    ofertas = descargar(VENTANA_MIN)
    avisos = 0
    for o in sorted(ofertas, key=lambda x: x["publicada"]):
        if o["id"] in vistos:
            continue
        aviso = evaluar(o)
        if not aviso:
            continue
        try:
            enviar_telegram(formatear(o, aviso))
            vistos[o["id"]] = time.time()
            avisos += 1
            log(f"Aviso {aviso[0]} {aviso[1]}% | {o['tienda']} | {o['precio']} / {o['referencia']} | {o['titulo'][:50]}")
        except Exception as e:
            log(f"Error enviando a Telegram: {e}")
    con_ref = sum(1 for o in ofertas if o["referencia"])
    log(f"Pasada completada: {len(ofertas)} ofertas de los últimos {VENTANA_MIN} min "
        f"({con_ref} con precio de referencia), {avisos} avisos enviados")
    return guardar_vistos(vistos)


def main():
    falta = [n for n, v in [("TELEGRAM_TOKEN", TG_TOKEN), ("TELEGRAM_CHAT_ID", TG_CHAT)] if not v]
    if falta:
        sys.exit("Faltan variables: " + ", ".join(falta))
    vistos = cargar_vistos()
    if "--una-vez" in sys.argv:
        pasada(vistos)
        return
    enviar_telegram(f"✅ Bot activo. Aviso de bajadas desde el {DESCUENTO_MIN}% en "
                    f"{', '.join(TIENDAS) if TIENDAS else 'todas las tiendas'}.")
    while True:
        try:
            vistos = pasada(vistos)
        except Exception as e:
            log(f"Error en la pasada: {e}")
        time.sleep(INTERVALO_MIN * 60)


if __name__ == "__main__":
    main()
