# Sistema de notas del lector (página del informe)

Fuentes del bloque de ayuda, el CSS y el script que la página publicada lleva inline
(`<div id="notas-lector">`, reglas `.notas/.btn-nota/.nota-ficha/#nota-sel`, `<script id="notas-script">`).
Usa la capacidad `comments` de los artifacts de claude.ai en su forma `composer_only`: la página solo abre el
compositor nativo de comentarios (anclado a la selección o al titular de una ficha); la plataforma escribe,
atribuye y muestra los hilos. La rutina diaria conserva estos bloques al regenerar la página.

- `notas.js`, `notas.css`, `notas_header.html`: las tres piezas.
- `armar.py`: inserta las piezas en una copia de la página publicada (`original.html`) y genera `informe.html`.
- `probar.mjs`: pruebas con Playwright (sin visor, con visor simulado, `use()` nulo, `unavailable`).

Para cambiar algo: editar la pieza, correr `python3 -I armar.py` sobre la página publicada del día y
`node probar.mjs`, y publicar `informe.html` en la misma URL del informe (con `capabilities: {comments: {composer_only: true}}`
la primera vez; después, sin pasar capabilities).
