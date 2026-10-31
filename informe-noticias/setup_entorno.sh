#!/bin/bash
# Script de configuración del entorno "Informe de noticias" (claude.ai: menú del entorno > Editar > Setup script).
# Corre la ingesta antes de que arranque cada sesión del entorno, así la rutina diaria encuentra
# /tmp/ingesta lista y no tiene que ejecutar el script desde la sesión. Nunca hace fallar el entorno.
D=/tmp/informe-repo
rm -rf "$D"
git clone -q --depth 1 --branch informe-noticias https://github.com/buttomfraganciasdistribuidora-creator/Buttom.git "$D" || exit 0
pip install -q feedparser trafilatura >/dev/null 2>&1 || true
mkdir -p /tmp/ingesta
cd "$D/informe-noticias" && timeout 600 python3 -I ingestar.py --fuentes fuentes.json --salida /tmp/ingesta/notas.json > /tmp/ingesta/log.txt 2>&1 || true
exit 0
