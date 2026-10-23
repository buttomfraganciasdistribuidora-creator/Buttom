# Informe diario de noticias: capa de ingesta

Script y registro de fuentes que usa la rutina "Informe diario de noticias" cada mañana, antes de redactar.

```
pip install -q feedparser trafilatura   # opcional; sin esto usa la biblioteca estándar
python3 -I ingestar.py --fuentes fuentes.json --salida /tmp/ingesta/notas.json
```

Genera, junto a la salida: `notas.json`, `resumen_ingesta.md`, `indice/*.md` (por sección y por tema) y `textos/<id>.md`.

Principios: user-agent honesto (`ButtomNewsReader/1.0`), robots.txt verificado por URL (RFC 9309, con Crawl-delay),
un pedido por segundo por dominio, sin simular navegador, y los medios que prohíben el acceso automatizado o a
agentes de IA no se consultan (bloque `excluidas` de `fuentes.json`).

Para sumar o sacar un medio, editá `fuentes.json` (campo `acceso`: `texto_completo` o `solo_rss`).

## Notas del lector

La página del informe tiene botones para dejar comentarios anclados al texto («Comentar lo marcado», «Comentar esta ficha»,
«Dejar una nota general»). Son los comentarios nativos de los artifacts de claude.ai: la rutina los lee cada mañana y
responde en la sección "Tus notas" del informe siguiente; los enviados con "Enviar a Claude" se responden en el mismo hilo.

`preferencias.md` guarda los ajustes permanentes que pidió el lector (los que deben repetirse todos los días). La rutina lo
lee al empezar cada edición; se edita desde el chat, no desde la rutina.
