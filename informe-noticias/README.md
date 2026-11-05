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

## Termómetro de tendencias

Después de la ingesta, `tendencias.py` arma el termómetro:

```
python3 -I tendencias.py --notas /tmp/ingesta/notas.json --salida /tmp/ingesta/tendencias.json --md /tmp/ingesta/indice/tendencias.md
```

Mide tres cosas: qué temas tocan más medios de la ingesta (sin pedir nada a internet), qué búsquedas suben en Google
en la Argentina (RSS oficial de Google Trends) y qué es tendencia en X en la Argentina, leído del espejo público
trends24.in porque X no permite la lectura automática. Cada tendencia se cruza con las notas de la ingesta para ver
si algún medio la cubre. Es un indicador de conversación, no una fuente: en el informe nunca entra como dato.

Mismos principios de red que la ingesta. Si robots.txt no se puede leer, no pide nada a ese sitio. Cada pedido tiene
un límite de tiempo y de tamaño, y la corrida un presupuesto total de 120 s de red. Escribe siempre los dos
archivos de salida, aunque fallen las tres medidas.

## Fotos de las fichas

La ingesta guarda en cada nota el campo `imagen` (la foto que el medio publica en su feed o en las etiquetas
og:image de la nota; solo en fuentes con acceso `texto_completo`). `fotos.py` baja únicamente las de las notas que
cita cada ficha:

```
python3 -I fotos.py --notas /tmp/ingesta/notas.json --fuentes fuentes.json --ids id1,id2 \
    --dir SCRATCH/fotos/2026-11-05 --prefijo fotos/2026-11-05/ --manifest SCRATCH/fotos/manifest.json
```

Solo pide imágenes al propio medio de la nota (su `dominio` o los `dominios_imagen` declarados en `fuentes.json`),
nunca a un dominio de `excluidas` (`dominio` o `dominios`), también en redirecciones y en la lectura de robots.txt.
Si el robots.txt bloquea agentes de IA no pide nada, respeta el Crawl-delay (más de 60 s: no pide) y, si no puede leer
`fuentes.json`, no baja ninguna foto. El efecto vintage lo pone el CSS de la página (`.foto`). Las fotos se guardan en
el scratchpad porque Artifact no publica archivos de /tmp.

## Notas del lector

La página del informe tiene botones para dejar comentarios anclados al texto («Comentar lo marcado», «Comentar esta ficha»,
«Dejar una nota general»). Son los comentarios nativos de los artifacts de claude.ai: la rutina los lee cada mañana y
responde en la sección "Tus notas" del informe siguiente; los enviados con "Enviar a Claude" se responden en el mismo hilo.

`prompt_rutina.md` es una copia del prompt que corre la rutina cada mañana (la versión que vale es la guardada en la rutina).
`preferencias.md` guarda los ajustes permanentes que pidió el lector (los que deben repetirse todos los días). La rutina lo
lee al empezar cada edición; se edita desde el chat, no desde la rutina.
