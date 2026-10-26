# Preferencias del lector

Ajustes permanentes que pidió el lector a través de las notas de la página del informe o del chat.
La rutina los lee al empezar cada edición y los aplica además de su prompt.

Qué va acá: solo lo que debe repetirse todos los días (más o menos cobertura de un tema, un dato fijo
en Datos del día, un cambio de formato). Qué no va acá: preguntas y correcciones de una edición,
que se responden en la sección "Tus notas" del informe del día siguiente.

Formato: una línea por ajuste, con la fecha en que se pidió. Si un ajuste se revierte, se borra la línea.

## Ajustes vigentes

- 2026-10-24 · Nuevas y ya publicadas: en cada sección van primero las fichas NUEVAS y después, bajo la nota «Seguimiento de temas ya publicados», las ACTUALIZADAS, que cuentan solo lo que cambió desde la vez anterior. El resumen (.ss) de cada ficha de noticia empieza con «NUEVA ·» o con «ACTUALIZADA (publicada por primera vez el <día dd/mm>) ·». Para poder dar esa fecha aunque el tema lleve varios días, cada <details> de noticia (no las fichas de «Tus notas» ni la de Buttom) lleva el atributo data-desde="AAAA-MM-DD" con el día en que el tema apareció por primera vez en el informe: en una ficha nueva es la fecha de hoy; en una actualizada se copia el data-desde de la ficha de ayer sobre el mismo tema (si la ficha de ayer no lo tiene, se toma la fecha de su cartel «ACTUALIZADA (publicada el …)», y si decía NUEVA, la fecha de la edición de ayer). Un tema de ayer que hoy no tiene novedad no se repite. El atributo data-desde no cambia el estilo ni las clases, así que se agrega aunque el prompt pida conservar la estructura.
- 2026-10-24 · Horarios deportivos: todo evento deportivo de la Agenda y de Deportes lleva día, hora argentina y canal de TV o streaming. Si la nota no lo trae, buscarlo con WebSearch (es un dato puntual) y marcarlo ⚠️ si sale de una sola fuente; si todavía no está publicado, escribir «a confirmar». Nunca dejar un evento sin hora porque la nota no la traía.
