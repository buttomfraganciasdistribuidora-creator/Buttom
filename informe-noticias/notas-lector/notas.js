/* Notas del lector. Abre el compositor de comentarios de claude.ai desde una selección de texto
   o desde cada ficha (la plataforma escribe, atribuye y muestra el hilo; esta página no guarda nada).
   Si la vista no ofrece comentarios, no se muestra ningún botón. */
(function () {
  "use strict";
  var claude = window.claude;
  if (!claude || typeof claude.use !== "function") return;

  var com = null;          // namespace "comments", si la vista lo concede
  var selBtn = null;       // botón flotante sobre la selección
  var lastRange = null;    // copia de la selección al mostrar el botón
  var timer = 0;
  var apagado = false;     // true cuando la vista avisa que no puede abrir el compositor
  var ignorarHasta = 0;    // mientras Date.now() sea menor, el lector está tocando el flotante: no esconderlo
  var abriendo = false;    // true mientras el visor responde a openComposer del flotante: no volver a mostrarlo
  var caja = null;         // bloque de ayuda del header (#notas-lector)

  function esconder() {
    if (selBtn) selBtn.hidden = true;
    lastRange = null;
  }

  function apagar() {
    apagado = true;
    esconder();
    if (caja) caja.hidden = true;
    var botones = document.querySelectorAll(".btn-nota");
    for (var i = 0; i < botones.length; i++) botones[i].hidden = true;
  }

  /* Pide al visor que abra el compositor. Resuelve true si abrió; false si no (negativa suave o error). */
  function abrir(target) {
    if (!com || apagado) return Promise.resolve(false);
    var p;
    try { p = com.openComposer(target); } catch (e) { return Promise.resolve(false); }
    return Promise.resolve(p).then(function (r) {
      return !!(r && r.opened);           /* {opened:false}: negativa suave del visor, no reintentar en bucle */
    }, function (e) {
      var code = e && e.code;
      if (code === "unavailable" || code === "not_granted" || code === "capability_disabled" || code === "capability_removed") apagar();
      /* rate_limited, invalid u otros: no reintentar */
      return false;
    });
  }

  function botonFicha(det) {
    var body = det.querySelector(":scope > .body");
    if (!body) return;
    var titulo = det.querySelector(":scope > summary .st") || det.querySelector(":scope > summary") || det;
    var fila = document.createElement("div");
    fila.className = "nota-ficha";
    var b = document.createElement("button");
    b.type = "button";
    b.className = "btn-nota";
    b.textContent = "Comentar esta ficha";
    var nombre = (titulo.textContent || "").replace(/\s+/g, " ").trim().slice(0, 90);
    if (nombre) b.setAttribute("aria-label", "Comentar esta ficha: " + nombre);
    b.addEventListener("click", function () { abrir({ element: titulo }); });
    fila.appendChild(b);
    body.appendChild(fila);
  }

  function seleccionValida() {
    var sel = document.getSelection();
    if (!sel || sel.rangeCount !== 1 || sel.isCollapsed) return null;
    var r = sel.getRangeAt(0);
    if (r.toString().trim().length < 3) return null;
    var n = r.commonAncestorContainer;
    if (n.nodeType !== 1) n = n.parentNode;
    if (!n || typeof n.closest !== "function") return null;
    if (!n.closest(".wrap") || n.closest("button")) return null;
    return r;
  }

  function crearBotonFlotante() {
    selBtn = document.createElement("button");
    selBtn.type = "button";
    selBtn.id = "nota-sel";
    selBtn.className = "btn-nota";
    selBtn.textContent = "Comentar lo marcado";
    selBtn.hidden = true;
    /* No robar la selección al tocar el botón; la ventana de gracia evita que selectionchange lo esconda antes del click */
    selBtn.addEventListener("pointerdown", function (e) {
      if (e.button !== 0) return;
      e.preventDefault();
      ignorarHasta = Date.now() + 500;
    });
    selBtn.addEventListener("click", function () {
      ignorarHasta = 0;
      clearTimeout(timer);
      var r = seleccionValida() || lastRange;   /* la selección viva (puede haber cambiado hace un instante); la copia si el toque la borró */
      esconder();
      if (!r) return;
      abriendo = true;
      try { window.focus(); } catch (e) { /* sin foco programático: el visor decide */ }
      abrir({ range: r }).then(function (abierto) {
        abriendo = false;
        if (abierto) {
          /* La cita ya quedó en el compositor: soltar la selección para que el flotante no reaparezca al cambiar el viewport */
          setTimeout(function () {
            var sel = document.getSelection();
            if (sel && !sel.isCollapsed) sel.removeAllRanges();
          }, 50);
        } else {
          programar();                     /* el visor no abrió: volver a ofrecer el botón si la selección sigue */
        }
      });
    });
    document.body.appendChild(selBtn);
  }

  function ubicar() {
    if (apagado || !com) return;
    if (abriendo) { esconder(); return; }                      // el visor está abriendo el compositor: nada que mostrar
    if (Date.now() < ignorarHasta) { programar(); return; }   // el lector está tocando el botón: reevaluar después
    var r = seleccionValida();
    if (!r) { esconder(); return; }
    var rect = r.getBoundingClientRect();
    if (!rect || (rect.width === 0 && rect.height === 0)) { esconder(); return; }
    if (!selBtn) crearBotonFlotante();
    lastRange = r.cloneRange();
    selBtn.hidden = false;
    var ancho = document.documentElement.clientWidth;
    var alto = window.innerHeight;
    var h = selBtn.offsetHeight || 32;
    var x = rect.left + window.scrollX;
    var maxX = window.scrollX + ancho - selBtn.offsetWidth - 8;
    /* Debajo de la selección; si no entra en la pantalla, encima; si tampoco, pegado al borde inferior visible */
    var y;
    if (rect.bottom + 8 + h <= alto) y = rect.bottom + 8;
    else if (rect.top - 8 - h >= 0) y = rect.top - 8 - h;
    else y = Math.max(8, alto - h - 8);
    selBtn.style.left = Math.max(window.scrollX + 8, Math.min(x, maxX)) + "px";
    selBtn.style.top = (y + window.scrollY) + "px";
  }

  function programar() {
    clearTimeout(timer);
    timer = setTimeout(ubicar, 200);
  }

  claude.use("comments").then(function (c) {
    if (!c || typeof c.openComposer !== "function") return;
    com = c;
    caja = document.getElementById("notas-lector");
    if (caja) caja.hidden = false;          /* la ayuda solo se muestra cuando los botones que describe existen */
    var gen = document.getElementById("btn-nota-general");
    if (gen) {
      gen.hidden = false;
      gen.addEventListener("click", function () {
        abrir({ element: document.getElementById("notas-lector") || gen });
      });
    }
    var fichas = document.querySelectorAll("details");
    for (var i = 0; i < fichas.length; i++) botonFicha(fichas[i]);
    document.addEventListener("selectionchange", programar);
    window.addEventListener("resize", programar);
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") esconder(); });
  }, function () { /* sin comentarios en esta vista */ });
})();
