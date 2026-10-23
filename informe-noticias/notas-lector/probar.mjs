// Prueba del sistema de notas con Chromium (Playwright).
// A: sin window.claude: la página no cambia, el bloque de ayuda queda oculto, sin errores.
// B: con un window.claude.use simulado que concede "comments": botones, flotante, Range/Element, casos de borde.
// C: use("comments") resuelve null: nada visible.
// D: openComposer rechaza con unavailable: se apaga todo (botones y bloque de ayuda).
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
const { chromium } = createRequire(import.meta.url)("playwright");

const archivo = new URL("./informe.html", import.meta.url);
const browser = await chromium.launch();
let fallas = 0;
function ok(cond, msg) { console.log((cond ? "OK   " : "FALLA") + " " + msg); if (!cond) fallas++; }

async function abrir(ctx, init) {
  const page = await ctx.newPage();
  const errores = [];
  page.on("pageerror", (e) => errores.push(String(e)));
  page.on("console", (m) => { if (m.type() === "error") errores.push(m.text()); });
  if (init) await page.addInitScript(init);
  await page.goto(pathToFileURL(archivo.pathname).href);
  await page.waitForTimeout(300);
  return { page, errores };
}
const seleccionar = (sel, desde, hasta) => ({ sel, desde, hasta });
async function marcar(page, selector, desde = 0, hasta = 40) {
  await page.evaluate(({ selector, desde, hasta }) => {
    const p = document.querySelector(selector);
    let nodo = null;
    const walker = document.createTreeWalker(p, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) { if (walker.currentNode.textContent.trim().length > hasta) { nodo = walker.currentNode; break; } }
    const r = document.createRange(); r.setStart(nodo, desde); r.setEnd(nodo, hasta);
    const s = getSelection(); s.removeAllRanges(); s.addRange(r);
  }, { selector, desde, hasta });
  await page.waitForTimeout(350);
}

// Caso A
{
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const { page, errores } = await abrir(ctx, null);
  ok(errores.length === 0, "A: sin errores de consola (" + errores.join(" | ") + ")");
  ok(await page.locator(".btn-nota:visible").count() === 0, "A: ningún botón visible sin window.claude");
  ok(await page.locator("#notas-lector").isHidden(), "A: el bloque de ayuda queda oculto sin window.claude");
  const ancho = await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);
  ok(ancho, "A: sin scroll horizontal a 390px");
  await ctx.close();
}

// Caso B
const simulado = () => {
  window.__llamadas = [];
  window.__noAbrir = false;
  window.__demora = 0;
  window.claude = {
    use: (name) => new Promise((res) => setTimeout(() => res(name === "comments" ? Object.freeze({
      openComposer: (t) => {
        window.__llamadas.push(t.range ? { range: t.range.toString() } : { element: t.element && (t.element.className || t.element.id) });
        const r = { opened: !window.__noAbrir };
        return window.__demora ? new Promise((res) => setTimeout(() => res(r), window.__demora)) : Promise.resolve(r);
      },
      anchorFor: () => Promise.resolve({ path: "x", x: 0, y: 0 }),
    }) : null), 50)),
  };
};
{
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true });
  const { page, errores } = await abrir(ctx, simulado);
  await page.waitForTimeout(300);
  ok(errores.length === 0, "B: sin errores de consola (" + errores.join(" | ") + ")");
  ok(await page.locator("#notas-lector").isVisible(), "B: bloque de ayuda visible");
  ok(await page.locator("#btn-nota-general:visible").count() === 1, "B: botón general visible");
  const nFichas = await page.locator("details").count();
  const nBot = await page.locator("details .nota-ficha .btn-nota").count();
  ok(nBot === nFichas && nFichas > 10, `B: un botón por ficha (${nBot}/${nFichas})`);
  const aria = await page.locator("details .nota-ficha .btn-nota").first().getAttribute("aria-label");
  ok(aria && aria.startsWith("Comentar esta ficha: ") && aria.length > 30, "B: aria-label con el titular -> " + aria);
  ok(await page.locator("#nota-sel").count() === 0 || await page.locator("#nota-sel").isHidden(), "B: flotante oculto sin selección");

  // Botón general: ancla al bloque de ayuda
  await page.locator("#btn-nota-general").click();
  let ll = await page.evaluate(() => window.__llamadas);
  ok(ll.length === 1 && ll[0].element === "notas", "B: 'Dejar una nota general' llama openComposer({element: .notas}) -> " + JSON.stringify(ll));

  // Botón de ficha: abre el compositor anclado al titular (.st)
  await page.locator("details").first().evaluate((d) => { d.open = true; });
  await page.locator("details .nota-ficha .btn-nota").first().click();
  ll = await page.evaluate(() => window.__llamadas);
  ok(ll.length === 2 && ll[1].element === "st", "B: 'Comentar esta ficha' llama openComposer({element: .st}) -> " + JSON.stringify(ll[1]));

  // Selección de texto: aparece el flotante debajo y abre con el Range
  await marcar(page, "#datos .note", 0, 40);
  ok(await page.locator("#nota-sel").isVisible(), "B: flotante visible tras seleccionar");
  let pos = await page.evaluate(() => {
    const b = document.getElementById("nota-sel").getBoundingClientRect();
    const r = getSelection().getRangeAt(0).getBoundingClientRect();
    return { debajo: b.top >= r.bottom, dentro: b.left >= 0 && b.right <= innerWidth && b.bottom <= innerHeight };
  });
  ok(pos.debajo && pos.dentro, "B: flotante debajo de la selección y dentro de la pantalla " + JSON.stringify(pos));
  await page.locator("#nota-sel").click();
  ll = await page.evaluate(() => window.__llamadas);
  ok(ll.length === 3 && typeof ll[2].range === "string" && ll[2].range.length >= 3, "B: 'Comentar lo marcado' llama openComposer({range}) -> " + JSON.stringify(ll[2]));
  ok(await page.locator("#nota-sel").isHidden(), "B: flotante se esconde después de usarlo");
  await page.waitForTimeout(200);
  ok(await page.evaluate(() => getSelection().isCollapsed), "B: la selección se suelta después de abrir el compositor");
  await page.setViewportSize({ width: 390, height: 600 });
  await page.waitForTimeout(400);
  ok(await page.locator("#nota-sel").isHidden(), "B: un resize después de abrir no vuelve a mostrar el flotante");
  await page.setViewportSize({ width: 390, height: 844 });

  // Negativa suave del visor ({opened:false}): el flotante vuelve a ofrecerse
  await page.evaluate(() => { window.__noAbrir = true; });
  await marcar(page, "#agenda .note", 0, 30);
  ok(await page.locator("#nota-sel").isVisible(), "B: flotante visible (previo a negativa suave)");
  await page.locator("#nota-sel").click();
  await page.waitForTimeout(400);
  ok(await page.locator("#nota-sel").isVisible(), "B: tras {opened:false} el flotante se vuelve a mostrar y la selección sigue");
  await page.evaluate(() => { window.__noAbrir = false; getSelection().removeAllRanges(); });
  await page.waitForTimeout(350);

  // Mouse down sobre el flotante, soltar afuera (sin click): no debe quedar trabado
  await marcar(page, "#normas .note", 0, 30);
  ok(await page.locator("#nota-sel").isVisible(), "B: flotante visible antes del arrastre");
  const bb = await page.locator("#nota-sel").boundingBox();
  await page.mouse.move(bb.x + 5, bb.y + 5);
  await page.mouse.down();
  await page.mouse.move(bb.x + 200, bb.y + 200, { steps: 4 });
  await page.mouse.up();
  await page.waitForTimeout(100);
  const topAntes = await page.locator("#nota-sel").evaluate((b) => b.style.top);
  await marcar(page, "#politica .note", 0, 30);
  await page.waitForTimeout(700);
  const topDespues = await page.locator("#nota-sel").evaluate((b) => b.style.top);
  ok(await page.locator("#nota-sel").isVisible() && topAntes !== topDespues, `B: tras soltar afuera, una selección nueva reubica el flotante (${topAntes} -> ${topDespues})`);
  await page.evaluate(() => getSelection().removeAllRanges());
  await page.waitForTimeout(700);
  ok(await page.locator("#nota-sel").isHidden(), "B: y al deseleccionar se esconde");

  // Selección pegada al borde inferior: el botón no se sale de la pantalla
  await page.evaluate(() => {
    const p = document.querySelector("#economia .note");
    p.scrollIntoView({ block: "end" });
    const walker = document.createTreeWalker(p, NodeFilter.SHOW_TEXT); let nodo = null;
    while (walker.nextNode()) nodo = walker.currentNode;
    const r = document.createRange(); r.setStart(nodo, Math.max(0, nodo.length - 30)); r.setEnd(nodo, nodo.length);
    const s = getSelection(); s.removeAllRanges(); s.addRange(r);
  });
  await page.waitForTimeout(400);
  pos = await page.evaluate(() => {
    const b = document.getElementById("nota-sel").getBoundingClientRect();
    const r = getSelection().getRangeAt(0).getBoundingClientRect();
    return { visible: b.height > 0, dentro: b.top >= 0 && b.bottom <= innerHeight, rSel: Math.round(r.bottom), b: Math.round(b.top) + "-" + Math.round(b.bottom), vh: innerHeight };
  });
  ok(pos.visible && pos.dentro, "B: selección al borde inferior -> botón dentro de la pantalla " + JSON.stringify(pos));

  // Selección que cambia justo antes del toque: debe citar la selección viva, no la copia vieja
  await marcar(page, "#mundo .note", 0, 25);
  ok(await page.locator("#nota-sel").isVisible(), "B: flotante visible con la selección A");
  await page.evaluate(() => {
    const r = getSelection().getRangeAt(0); const nodo = r.startContainer;
    const r2 = document.createRange(); r2.setStart(nodo, 0); r2.setEnd(nodo, Math.min(nodo.length, 120));
    const s = getSelection(); s.removeAllRanges(); s.addRange(r2);
  });
  await page.waitForTimeout(10);
  await page.locator("#nota-sel").click({ force: true });
  ll = await page.evaluate(() => window.__llamadas);
  ok(ll[ll.length - 1].range && ll[ll.length - 1].range.length > 100, "B: cita la selección extendida (B), no la vieja (A): " + (ll[ll.length - 1].range || "").length + " caracteres");
  await page.waitForTimeout(400);

  // Visor lento (300 ms) y resize durante la espera: el flotante no debe reaparecer
  await page.evaluate(() => { window.__demora = 300; });
  await page.evaluate(() => {
    const c = window.claude; const orig = c.use;
    // el simulado ya está creado: parcheamos la promesa de openComposer vía __demora en el mock (ver abajo)
  });
  await marcar(page, "#negocios .note", 0, 30);
  ok(await page.locator("#nota-sel").isVisible(), "B: flotante visible antes del visor lento");
  await page.locator("#nota-sel").click();
  await page.waitForTimeout(30);
  await page.setViewportSize({ width: 390, height: 700 });
  const muestras = [];
  for (let i = 0; i < 8; i++) { await page.waitForTimeout(50); muestras.push(await page.locator("#nota-sel").isVisible() ? "V" : "."); }
  ok(!muestras.includes("V"), "B: con visor lento y resize, el flotante no reaparece (" + muestras.join("") + ")");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => { window.__demora = 0; });
  await page.waitForTimeout(300);

  // Selección dentro de un botón: no aparece
  await page.evaluate(() => {
    const b = document.getElementById("btn-nota-general");
    const r = document.createRange(); r.selectNodeContents(b);
    const s = getSelection(); s.removeAllRanges(); s.addRange(r);
  });
  await page.waitForTimeout(350);
  ok(await page.locator("#nota-sel").isHidden(), "B: no aparece al seleccionar el texto de un botón");

  // Escape esconde
  await marcar(page, "#caba .note", 0, 30);
  ok(await page.locator("#nota-sel").isVisible(), "B: flotante visible con selección nueva");
  await page.keyboard.press("Escape");
  ok(await page.locator("#nota-sel").isHidden(), "B: Escape esconde el flotante");

  // z-index: el flotante queda por debajo del nav pegado (z-index 2)
  const z = await page.evaluate(() => getComputedStyle(document.getElementById("nota-sel")).zIndex);
  ok(z === "1", "B: z-index del flotante menor que el del nav (" + z + ")");

  await page.emulateMedia({ colorScheme: "dark" });
  const borde = await page.locator("#btn-nota-general").evaluate((b) => getComputedStyle(b).borderTopWidth);
  ok(borde === "1px", "B: botón con borde en modo oscuro");
  ok(errores.length === 0, "B: sin errores de consola al final (" + errores.join(" | ") + ")");
  await ctx.close();
}

// Caso C: use resuelve null
{
  const ctx = await browser.newContext({ viewport: { width: 1200, height: 900 } });
  const { page, errores } = await abrir(ctx, () => { window.claude = { use: () => Promise.resolve(null) }; });
  await page.waitForTimeout(300);
  ok(errores.length === 0, "C: sin errores de consola");
  ok(await page.locator(".btn-nota:visible").count() === 0, "C: ningún botón visible cuando use() da null");
  ok(await page.locator("#notas-lector").isHidden(), "C: bloque de ayuda oculto cuando use() da null");
  await ctx.close();
}

// Caso D: openComposer rechaza con unavailable -> se apagan todos los botones y la ayuda
{
  const ctx = await browser.newContext({ viewport: { width: 1200, height: 900 } });
  const { page } = await abrir(ctx, () => {
    window.claude = { use: (n) => Promise.resolve(n === "comments" ? { openComposer: () => Promise.reject({ code: "unavailable", message: "x" }), anchorFor: () => Promise.resolve({}) } : null) };
  });
  await page.waitForTimeout(300);
  ok(await page.locator("#notas-lector").isVisible(), "D: ayuda visible antes del rechazo");
  await page.locator("#btn-nota-general").click();
  await page.waitForTimeout(100);
  ok(await page.locator(".btn-nota:visible").count() === 0, "D: tras 'unavailable' no queda ningún botón visible");
  ok(await page.locator("#notas-lector").isHidden(), "D: tras 'unavailable' la ayuda se oculta");
  await ctx.close();
}

await browser.close();
console.log(fallas ? `\n${fallas} falla(s)` : "\nTodo OK");
process.exit(fallas ? 1 : 0);
