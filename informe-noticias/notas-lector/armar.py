#!/usr/bin/env python3
"""Arma informe.html: la página publicada más el sistema de notas del lector.

Lee original.html (la versión publicada, con el esqueleto del Artifact), le quita el esqueleto,
e inserta: las reglas CSS (notas.css) al final del <style> propio, el bloque de ayuda
(notas_header.html) al final del <header>, y el script (notas.js) antes de cerrar la página.
Falla si algún ancla no aparece exactamente una vez.
"""
import pathlib, re, sys

aqui = pathlib.Path(__file__).resolve().parent
html = (aqui / "original.html").read_text(encoding="utf-8")

# 1. Quitar el esqueleto del Artifact (la herramienta lo vuelve a poner al publicar).
m = re.match(r"<!doctype html><html><head>.*?</head><body>\n?", html, re.S)
if not m:
    sys.exit("no encontré el esqueleto inicial")
html = html[m.end():]
if html.endswith("</body></html>"):
    html = html[: -len("</body></html>")].rstrip("\n") + "\n"
else:
    sys.exit("no encontré el cierre </body></html>")

def insertar_una_vez(texto, ancla, nuevo, antes=True):
    n = texto.count(ancla)
    if n != 1:
        sys.exit(f"ancla {ancla!r} aparece {n} veces, esperaba 1")
    return texto.replace(ancla, (nuevo + ancla) if antes else (ancla + nuevo))

css = (aqui / "notas.css").read_text(encoding="utf-8")
cabecera = (aqui / "notas_header.html").read_text(encoding="utf-8")
js = (aqui / "notas.js").read_text(encoding="utf-8")

html = insertar_una_vez(html, "\n</style>\n\n<div class=\"wrap\">", css.rstrip("\n"), antes=True)
html = insertar_una_vez(html, "\n</header>\n", "\n" + cabecera.rstrip("\n"), antes=True)
# Registro durable de hilos respondidos (lo mantiene la rutina) y hora de la edición en el kicker
html = insertar_una_vez(html, "</nav>\n", "\n<div id=\"notas-registro\" hidden data-hilos=\"\"></div>\n", antes=False)
html = insertar_una_vez(html, '<div class="kicker">Informe diario · viernes 23 de octubre de 2026</div>',
                        '<div class="kicker">Informe diario · <time datetime="2026-10-23T09:00-03:00">viernes 23 de octubre de 2026, 09:00</time></div>', antes=False)
html = html.replace('<div class="kicker">Informe diario · viernes 23 de octubre de 2026</div>', "")
script = "<script id=\"notas-script\">\n" + js.rstrip("\n") + "\n</script>\n"
html = html.rstrip("\n") + "\n" + script

(aqui / "informe.html").write_text(html, encoding="utf-8")
print(f"informe.html: {len(html.encode('utf-8'))} bytes")
