#!/usr/bin/env python3
"""Termómetro de tendencias para el informe diario de Buttom.

Tres medidas, cada una con su origen a la vista:
  1. "medios":  de qué hablan los 22 medios de la ingesta (cuántos medios distintos tocan cada tema en la ventana).
               Se calcula sobre notas.json, sin pedir nada a internet.
  2. "google":  búsquedas en alza en la Argentina, RSS oficial de Google Trends (geo=AR).
  3. "x":       tendencias de X para la Argentina leídas desde el espejo público trends24.in
               (X prohíbe la lectura automática en su robots.txt; el espejo la permite como referencia).
               ES UN INDICADOR DE CONVERSACIÓN, NO UNA FUENTE: cada tendencia se cruza con las notas de la ingesta
               para ver si los medios la cubren, y es ahí donde hay que leer.

Reglas de red:
  - User-Agent honesto, sin simular navegador.
  - robots.txt respetado según el RFC 9309 (parser propio: grupos combinados, Allow/Disallow con * y $, la regla
    más larga gana). Si robots.txt no se puede leer (red caída, 5xx, tiempo agotado, cuerpo cortado) se asume que
    está todo prohibido y el estado dice "robots.txt inaccesible".
  - 1 pedido por segundo por dominio, contando cada salto de redirección y cada reintento.
  - Las redirecciones se siguen a mano: cada destino pasa por su propio robots.txt.
  - Cada pedido tiene un límite de tiempo total (no solo por operación de socket) y un tope de tamaño,
    y toda la parte de red de la corrida tiene un presupuesto de tiempo. Un servidor lento nunca cuelga la rutina.
  - Un solo reintento, y solo ante un 5xx.
main() escribe siempre los dos archivos de salida, aunque fallen todas las medidas.
Solo biblioteca estándar; corre con `python3 -I`.

Uso:  python3 -I tendencias.py [--notas notas.json] [--salida tendencias.json] [--md indice/tendencias.md] [--horas 24]
"""
import argparse, datetime as dt, email.utils, html, http.client, json, os, re, sys, threading, time, unicodedata
import urllib.error, urllib.parse, urllib.request

UA = "ButtomNewsReader/1.0 (lector RSS personal)"
URL_GOOGLE = "https://trends.google.com/trending/rss?geo=AR"
URL_X = "https://trends24.in/argentina/"

TIMEOUT = 20             # segundos por operación de socket (conectar, cada lectura)
LIMITE_PEDIDO = 30       # segundos como máximo por pedido completo: DNS, conexión, encabezados y cuerpo
LIMITE_RED = 120         # segundos como máximo para toda la red de una corrida (se cuenta desde el primer pedido)
MAX_BYTES = 5_000_000    # tope de tamaño por respuesta
MAX_REDIRECCIONES = 5    # RFC 9309 2.3.1.2: seguir al menos 5 saltos para robots.txt
PAUSA_REINTENTO = 2      # segundos antes del único reintento ante un 5xx

_ultimo = {}             # host -> momento (monotónico) del último pedido
_robots = {}             # "esquema://host" -> _Robots | None (no hay robots.txt) | BLOQUEADO | INACCESIBLE
_robots_detalle = {}     # "esquema://host" -> texto que explica por qué no se pudo usar robots.txt
_reloj = {}              # "fin": momento en que se acaba el presupuesto de red de la corrida

BLOQUEADO = "bloqueado"      # robots.txt respondió 401/403 u otro 4xx que no es "no existe": no se pide nada
INACCESIBLE = "inaccesible"  # robots.txt no se pudo leer (red, 5xx, tiempo, cuerpo cortado): no se pide nada


class TiempoAgotado(Exception):
    """Un pedido superó LIMITE_PEDIDO, o se terminó el presupuesto LIMITE_RED de la corrida."""


class RobotsBloquea(Exception):
    """Una redirección lleva a una dirección que robots.txt no permite (o cuyo robots.txt no se puede leer)."""

    def __init__(self, estado, detalle):
        super().__init__(detalle)
        self.estado = estado


# ---------- Texto ----------
def _texto(v):
    """Cualquier valor de notas.json como texto (None -> '', números -> str)."""
    return v if isinstance(v, str) else ("" if v is None else str(v))


def _norm(s):
    """Minúsculas, sin tildes ni diéresis, espacios colapsados. La ñ se conserva: en castellano es otra letra
    (Peña no es pena). "papá" (y "papa noel", que suele escribirse sin tilde) queda como "papa_", para que nunca
    coincida con "papa" (el Papa) mientras el resto se sigue comparando sin tildes ("Papa Leon XIV" encuentra
    "Papa León XIV")."""
    s = re.sub(r"(?i)(?<!\w)pap(?:[áÁ]|a(?=\W+noel(?!\w)))(?!\w)", "papa_", unicodedata.normalize("NFC", _texto(s)))
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for i, c in enumerate(s)
                if unicodedata.category(c) != "Mn" or (c == "\u0303" and i > 0 and s[i - 1] in "nN"))
    s = unicodedata.normalize("NFC", s)
    return re.sub(r"\s+", " ", s.lower()).strip()


def _canon(s):
    """Unifica grafías de un mismo nombre en un texto ya normalizado: ee.uu., ee. uu., eeuu -> estados unidos."""
    return re.sub(r"(?<!\w)ee\.?\s?uu(?!\w)\.?", "estados unidos", s)


# ---------- Red ----------
def _host(url):
    return urllib.parse.urlsplit(url).netloc.lower()


def _base(url):
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}".lower()


def _esperar(host):
    """1 pedido por segundo por dominio."""
    antes = _ultimo.get(host)
    if antes is not None:
        falta = 1.0 - (time.monotonic() - antes)
        if falta > 0:
            time.sleep(falta)
    _ultimo[host] = time.monotonic()


def _restante():
    """Segundos que quedan del presupuesto de red de la corrida (el reloj arranca con el primer pedido)."""
    if "fin" not in _reloj:
        _reloj["fin"] = time.monotonic() + LIMITE_RED
    return _reloj["fin"] - time.monotonic()


class _SinRedirecciones(urllib.request.HTTPRedirectHandler):
    """urllib no sigue redirecciones por su cuenta: las sigue _get, para pasar cada salto por robots.txt y por la
    espera de 1 s. Al devolver None, el 3xx llega a _pedido_unico como HTTPError con su encabezado Location."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_SinRedirecciones)


def _pedido_unico(req):
    """Un solo intercambio HTTP, sin seguir redirecciones. Devuelve (código, encabezados, cuerpo).
    Lee el cuerpo en pedazos y corta si pasa de MAX_BYTES."""
    try:
        r = _opener.open(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as e:     # 3xx, 4xx y 5xx: alcanza con el código y los encabezados
        try:
            return e.code, e.headers, b""
        finally:
            e.close()
    with r:
        partes, total = [], 0
        while True:
            pedazo = r.read(64 * 1024)
            if not pedazo:
                break
            total += len(pedazo)
            if total > MAX_BYTES:
                raise ValueError(f"la respuesta pasa de {MAX_BYTES} bytes")
            partes.append(pedazo)
        # read(n) no avisa si la conexión se corta antes de Content-Length: un robots.txt cortado no puede valer
        largo = (r.headers.get("Content-Length") or "").strip()
        if largo.isdigit() and total < int(largo):
            raise http.client.IncompleteRead(b"".join(partes), int(largo) - total)
        return r.status, r.headers, b"".join(partes)


def _con_limite(segundos, funcion, *args):
    """Corre funcion(*args) en un hilo aparte y espera como máximo `segundos` de reloj.
    El timeout de socket es por operación: un servidor que manda un byte cada pocos segundos, o encabezados sin
    fin, o un DNS que no contesta, lo esquivan. Este límite no. Si se vence, el hilo queda abandonado (es daemon y
    solo estaba haciendo ese intercambio: no puede pedir nada más) y se levanta TiempoAgotado."""
    resultado = {}

    def correr():
        try:
            resultado["ok"] = funcion(*args)
        except BaseException as e:  # se pasa tal cual al hilo principal
            resultado["error"] = e

    hilo = threading.Thread(target=correr, daemon=True)
    hilo.start()
    hilo.join(segundos)
    if hilo.is_alive():
        raise TiempoAgotado(f"el pedido superó {segundos:.0f} s")
    if "error" in resultado:
        raise resultado["error"]
    return resultado["ok"]


def _get(url, accept="text/html,application/xml;q=0.9,*/*;q=0.5", robots=False):
    """Pide url y devuelve (cuerpo, content-type).
    - Sigue hasta MAX_REDIRECCIONES saltos. Cada salto pasa por la espera de 1 s de su dominio y, salvo que sea
      la lectura del propio robots.txt (RFC 9309 2.3.1.2), por el robots.txt de su dominio (RobotsBloquea si no).
    - Un solo reintento, y solo ante un 5xx. Cualquier otro código no 2xx levanta HTTPError.
    - Cada intercambio tiene límite de tiempo total y tope de tamaño; además hay un presupuesto para toda la corrida."""
    saltos, reintentado = 0, False
    while True:
        _esperar(_host(url))
        queda = _restante()
        if queda <= 0:
            raise TiempoAgotado(f"se agotó el tiempo de red de la corrida ({LIMITE_RED} s)")
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept, "Accept-Language": "es-AR,es;q=0.9"})
        codigo, encabezados, cuerpo = _con_limite(min(LIMITE_PEDIDO, queda), _pedido_unico, req)
        if 200 <= codigo < 300:
            return cuerpo, encabezados.get("Content-Type", "") or ""
        destino = encabezados.get("Location") if encabezados is not None else None
        if codigo in (301, 302, 303, 307, 308) and destino:
            saltos += 1
            if saltos > MAX_REDIRECCIONES:
                raise urllib.error.URLError(f"más de {MAX_REDIRECCIONES} redirecciones")
            nueva = urllib.parse.urljoin(url, destino.strip())
            if urllib.parse.urlsplit(nueva).scheme not in ("http", "https"):
                raise urllib.error.URLError(f"redirección a un esquema no admitido: {nueva}")
            if not robots:
                motivo = motivo_bloqueo(nueva)
                if motivo:
                    raise RobotsBloquea(motivo[0], f"redirección a {nueva}: {motivo[1]}")
            url = nueva
            continue
        if 500 <= codigo < 600 and not reintentado:
            reintentado = True
            time.sleep(PAUSA_REINTENTO)
            continue
        raise urllib.error.HTTPError(url, codigo, f"HTTP {codigo}", encabezados, None)


class _Robots:
    """Parser del RFC 9309: grupos por user-agent (los grupos que nombran al mismo agente se combinan),
    Allow/Disallow con * y $, la regla más larga gana y, a igual largo, gana Allow."""

    def __init__(self, texto):
        self.grupos = []  # [(agentes, reglas)]
        agentes, reglas = [], []
        for linea in texto.lstrip("\ufeff").splitlines():   # un BOM al principio no es parte del contenido
            linea = linea.split("#", 1)[0].strip()
            if not linea or ":" not in linea:
                continue
            campo, valor = [x.strip() for x in linea.split(":", 1)]
            campo = campo.lower()
            if campo == "user-agent":
                if reglas:
                    self.grupos.append((agentes, reglas))
                    agentes, reglas = [], []
                # solo el nombre del producto ("ButtomNewsReader/1.0" -> "buttomnewsreader"); vacío o inválido no vale
                agente = re.match(r"[A-Za-z_*-]*", valor).group(0).lower()
                if agente:
                    agentes.append(agente)
            elif campo in ("allow", "disallow") and agentes:
                reglas.append((campo == "allow", valor))
        if agentes:
            self.grupos.append((agentes, reglas))

    @staticmethod
    def _coincide(patron, ruta):
        fin = patron.endswith("$")   # $ solo ancla al final del patrón
        if fin:
            patron = patron[:-1]
        rx = "^" + ".*".join(re.escape(p) for p in patron.split("*")) + ("$" if fin else "")
        return re.match(rx, ruta) is not None

    def permite(self, ua, url):
        p = urllib.parse.urlsplit(url)
        ruta = (p.path or "/") + ("?" + p.query if p.query else "")
        token = re.match(r"[A-Za-z_-]*", ua).group(0).lower()
        # RFC 9309 2.2.1: coincidencia exacta del nombre (sin distinguir mayúsculas) y todos los grupos que lo
        # nombran se combinan en uno; si ninguno lo nombra, se combinan todos los grupos "*"
        if any(token in agentes for agentes, _ in self.grupos):
            reglas = [r for agentes, rs in self.grupos if token in agentes for r in rs]
        else:
            reglas = [r for agentes, rs in self.grupos if "*" in agentes for r in rs]
        mejor = (-1, True)
        for permitir, patron in reglas:
            if patron and self._coincide(patron, ruta):
                if len(patron) > mejor[0] or (len(patron) == mejor[0] and permitir):
                    mejor = (len(patron), permitir)
        return mejor[1]


def _leer_robots(base):
    """Lee base/robots.txt. Devuelve _Robots, None (no existe: todo permitido), BLOQUEADO o INACCESIBLE."""
    try:
        cuerpo, _ = _get(base + "/robots.txt", "text/plain", robots=True)
        return _Robots(cuerpo.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return None
        _robots_detalle[base] = f"robots.txt respondió {e.code}"
        # RFC 9309 2.3.1.4: un error del servidor cuenta como inaccesible. 401/403 y demás se toman como prohibición.
        return INACCESIBLE if (e.code >= 500 or e.code == 429) else BLOQUEADO
    except Exception as e:
        # red caída, tiempo agotado, cuerpo cortado, TLS, demasiadas redirecciones...: RFC 9309 2.3.1.4 -> todo prohibido
        _robots_detalle[base] = f"robots.txt no se pudo leer: {type(e).__name__}: {str(e)[:150]}"
        return INACCESIBLE


def permitido(url):
    base = _base(url)
    if base not in _robots:
        _robots[base] = _leer_robots(base)
    r = _robots[base]
    if r is None:
        return True
    if isinstance(r, str):          # BLOQUEADO o INACCESIBLE
        return False
    return r.permite(UA, url)


def motivo_bloqueo(url):
    """None si se puede pedir url; si no, (estado, detalle) para el informe."""
    if permitido(url):
        return None
    base = _base(url)
    if _robots.get(base) == INACCESIBLE:
        return "robots.txt inaccesible", _robots_detalle.get(base, "")
    return "robots bloquea", _robots_detalle.get(base) or f"robots.txt de {base} no permite {urllib.parse.urlsplit(url).path or '/'}"


def _fallo(e, extra):
    """Estado de una medida cuando el pedido falló."""
    if isinstance(e, RobotsBloquea):
        return {"estado": e.estado, "items": [], **extra, "detalle": str(e)}
    return {"estado": f"error: {type(e).__name__}", "items": [], **extra, "detalle": str(e)[:200]}


# ---------- Fechas ----------
def _limite(horas):
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=horas)


def _fecha(n):
    """Fecha de una nota (campo fecha o, si falta, publicado), ISO 8601 o RFC 822. None si no se entiende."""
    f = n.get("fecha") or n.get("publicado")
    if not isinstance(f, str) or not f.strip():
        return None
    f = f.strip()
    try:
        d = dt.datetime.fromisoformat(f.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = email.utils.parsedate_to_datetime(f)
        except Exception:
            return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _en_ventana(n, limite):
    """La misma regla para medir_medios y cruzar: entra la nota con fecha legible posterior al límite."""
    f = _fecha(n) if isinstance(n, dict) else None
    return f is not None and f >= limite


# ---------- 1. Medios: de qué hablan los 22 medios ----------
STOP = {_norm(w) for w in """el la los las un una unos unas de del al a y o en con por para sin sobre segun según tras hacia
desde hasta ante entre contra como que qué quien quién quienes quiénes cuando cuándo donde dónde cual cuál cuales cuáles
cuanto cuánto cuanta cuánta cuantos cuántos cuantas cuántas es son fue será ser hay ya no si sí mas más muy ahora asi así
lunes martes miercoles miércoles jueves viernes sabado sábado domingo enero febrero marzo abril mayo junio julio agosto
septiembre octubre noviembre diciembre hoy ayer mañana argentina argentino argentinos argentinas gobierno pais país nacional
video fotos foto vivo minuto ultimo último ultima última noticias noticia the of and in to for on with""".split()}
MEDIOS_NOMBRES = {_norm(w) for w in (
    "clarin", "clarín", "ole", "olé", "la nacion", "la nación", "pagina/12", "página/12", "infobae", "perfil", "442",
    "eldiarioar", "ambito", "ámbito", "cronista", "iprofesional", "lpo", "tn", "c5n", "bloomberg linea",
    "bloomberg línea", "buenos aires times", "herald", "glossy", "modern retail", "retail dive", "cosmetics business",
    "premium beauty news", "liga profesional")}
_PUNTUACION = "\"“”«»‘’'():;,.!?¿¡"


def _corta(token):
    """Una secuencia de nombres se corta en la puntuación ("Kicillof, Milei", "River: Gallardo"), salvo siglas (EE.UU.)."""
    return (token.rstrip("\"”»’')").endswith((",", ":", ";", ".", "!", "?"))
            and not re.fullmatch(r"(?:[A-Z]{1,3}\.)+", token))


def _terminos(titulo, partes=None):
    """Secuencias de palabras con mayúscula (admitiendo de/del/la/y en el medio), de 1 a 5 palabras.
    Si se pasa `partes`, ahí se guardan las palabras sueltas de cada secuencia ("Javier Milei" -> Javier, Milei):
    medir_medios las cuenta solo si en algún otro título aparecen solas."""
    tokens = re.findall(r"\S+", _texto(titulo))
    out = set()
    i = 0
    while i < len(tokens):
        t = tokens[i].strip(_PUNTUACION)
        if not t or not t[0].isupper() or len(t) < 3 or _norm(t) in STOP:
            i += 1
            continue
        j = i
        seq = [t]
        while j + 1 < len(tokens) and len(seq) < 5 and not _corta(tokens[j]):
            n = tokens[j + 1].strip(_PUNTUACION)
            low = _norm(n)
            if n and n[0].isupper() and low not in STOP and len(n) >= 2:
                seq.append(n)
                j += 1
            elif low in ("de", "del", "la", "y") and j + 2 < len(tokens) and not _corta(tokens[j + 1]):
                n2 = tokens[j + 2].strip(_PUNTUACION)
                if n2 and n2[0].isupper() and _norm(n2) not in STOP:
                    seq.append(n)
                    seq.append(n2)
                    j += 2
                else:
                    break
            else:
                break
        frase = " ".join(seq)
        if _norm(frase) not in MEDIOS_NOMBRES and not re.fullmatch(r"[\d.,%$]+", frase):
            out.add(frase)
            # con de/del/la es un nombre compuesto ("Mar del Plata", "Cámara de Diputados"): sus palabras no son
            # temas propios ("Plata" mezclaría La Plata y Mar del Plata). Con "y" sí: "Bullrich y Milei" son dos nombres.
            if len(seq) > 1 and partes is not None and not any(_norm(w) in ("de", "del", "la") for w in seq):
                partes.update(w for w in seq if w[:1].isupper() and len(w) >= 4 and _norm(w) not in STOP)
        i = j + 1
    return out


# "papa" suelto es el Papa: "papá" ya llega como "papa_" (ver _norm), así que "Papá Noel" y "papá" no caen acá;
# y "Papa Francisco" es su propia secuencia (medir_medios no pasa sus palabras a un alias).
ALIAS = {"ee.uu": "estados unidos", "eeuu": "estados unidos", "ee.uu.": "estados unidos", "papa": "papa leon xiv",
         "leon xiv": "papa leon xiv", "papa leon": "papa leon xiv", "papa leon xiv": "papa leon xiv",
         "wall street": "wall street", "la camara": "camara de diputados", "diputados": "camara de diputados"}
NOMBRE_ALIAS = {"estados unidos": "Estados Unidos", "papa leon xiv": "Papa León XIV", "camara de diputados": "Diputados"}


def _clave(t):
    """Clave con la que se agrupa un término: normalizada y con alias."""
    k = _canon(_norm(t))
    return ALIAS.get(k, k)


def medir_medios(notas, horas, max_items=15):
    limite = _limite(horas)
    previas = []   # (índice, nota, título, términos, partes)
    for idx, n in enumerate(notas):
        if not _en_ventana(n, limite):
            continue
        titulo = _texto(n.get("titulo"))
        partes = set()
        previas.append((idx, n, titulo, _terminos(titulo, partes), partes))
    # una palabra de una secuencia ("Milei" en "Javier Milei", "River" en "Boca y River") cuenta por su cuenta
    # solo si en algún título de la ventana aparece sola; así no aparecen temas falsos como "Santiago" o "Habló"
    sueltos = {_clave(t) for *_, ts, _ in previas for t in ts if " " not in t}
    # pero no si la secuencia entera ya es un tema (3 o más medios): "Santiago Caputo" o "Jorge Macri" son otra
    # persona que el "Caputo" o el "Macri" sueltos, y "Real Madrid" no es la ciudad
    fuentes_secuencia = {}
    for _, n, _, ts, _ in previas:
        for t in ts:
            if " " in t:
                fuentes_secuencia.setdefault(_clave(t), set()).add(n.get("fuente") or "?")
    temas_secuencia = {k for k, f in fuentes_secuencia.items() if len(f) >= 3}
    por_termino = {}
    for idx, n, titulo, ts, partes in previas:
        vistos = set()
        de_temas = {w for t in ts if " " in t and _clave(t) in temas_secuencia for w in t.split()}
        # una palabra suelta tampoco pasa por un alias: "Papa" en "Papa Francisco" no es León XIV
        partes = {p for p in partes if p not in de_temas and _clave(p) == _canon(_norm(p)) and _clave(p) in sueltos}
        for t in sorted(ts) + sorted(partes):
            k = _clave(t)
            if k in vistos:
                continue
            vistos.add(k)
            d = por_termino.setdefault(k, {"nombre": NOMBRE_ALIAS.get(k, t), "medios": {}, "notas": 0, "ids": set()})
            d["notas"] += 1
            d["ids"].add(idx)
            d["medios"].setdefault(n.get("fuente") or "?", []).append({"titulo": titulo, "link": n.get("link"), "fuente": n.get("fuente")})
    items = []
    for k, d in por_termino.items():
        if len(d["medios"]) < 3:
            continue
        items.append({"tema": d["nombre"], "medios": len(d["medios"]), "notas": d["notas"],
                      "ejemplos": [v[0] for v in list(d["medios"].values())[:3]], "clave": k, "ids": d["ids"]})
    items.sort(key=lambda x: (-x["medios"], -x["notas"], x["tema"]))
    # un término se descarta solo si otro más largo lo contiene como palabras enteras y cubre todas sus notas
    # ("Papa" dentro de "Papa León XIV" con las mismas notas); "Fed" no cae por "Federico", ni "Milei" por "Javier Milei"
    filtrados = []
    for it in items:
        rx = re.compile(r"(?<!\w)" + re.escape(it["clave"]) + r"(?!\w)")
        if any(o is not it and o["clave"] != it["clave"] and rx.search(o["clave"]) and it["ids"] <= o["ids"] for o in items):
            continue
        filtrados.append(it)
        if len(filtrados) >= max_items:
            break
    for it in filtrados:
        it.pop("clave", None)
        it.pop("ids", None)
    return filtrados


# ---------- 2. Google Trends Argentina (RSS oficial) ----------
def _texto_xml(v):
    """Texto de un campo XML: las secciones CDATA van literales y el resto se desescapa."""
    partes = re.split(r"<!\[CDATA\[(.*?)\]\]>", v, flags=re.S)
    return "".join(p if i % 2 else html.unescape(p) for i, p in enumerate(partes)).strip()


def _campo(bloque, etiqueta):
    e = re.escape(etiqueta)
    m = re.search(r"<" + e + r"\b[^>]*>(.*?)</" + e + r">", bloque, flags=re.S)
    return _texto_xml(m.group(1)) if m else ""


def medir_google(max_items=15):
    url = URL_GOOGLE
    extra = {"origen": "Google Trends Argentina (RSS oficial)", "url": url}
    motivo = motivo_bloqueo(url)
    if motivo:
        return {"estado": motivo[0], "items": [], **extra, "detalle": motivo[1]}
    try:
        cuerpo, _ = _get(url, "application/rss+xml,application/xml;q=0.9,*/*;q=0.5")
    except Exception as e:
        return _fallo(e, extra)
    s = cuerpo.decode("utf-8", "replace")
    items = []
    for it in re.findall(r"<item\b[^>]*>(.*?)</item>", s, flags=re.S)[:max_items]:
        noticias = []
        for ni in re.findall(r"<ht:news_item\b[^>]*>(.*?)</ht:news_item>", it, flags=re.S):
            titulo, link = _campo(ni, "ht:news_item_title"), _campo(ni, "ht:news_item_url")
            if titulo and link:
                noticias.append({"titulo": titulo, "link": link, "medio": _campo(ni, "ht:news_item_source")})
        items.append({"tema": _campo(it, "title"), "busquedas": _campo(it, "ht:approx_traffic"),
                      "desde": _campo(it, "pubDate"), "noticias": noticias[:2]})
    return {"estado": "ok", "items": items, **extra}


# ---------- 3. X vía espejo público ----------
_RX_BLOQUE = re.compile(
    r"""<div\b[^>]*\bclass=["']?list-container\b[^>]*>\s*"""
    r"""<h3\b[^>]*\bdata-timestamp=["']?([^"'\s>]*)[^>]*>.*?</h3>\s*"""
    r"""<ol\b[^>]*>(.*?)</ol>""", re.S)
# el enlace de cada tendencia, admita la clase con o sin comillas, junto a otras clases y con otros atributos
_RX_TENDENCIA = re.compile(
    r"""<a\b[^>]*\bclass=(?:"[^"]*\btrend-link\b[^"]*"|'[^']*\btrend-link\b[^']*'|trend-link\b)[^>]*>(.*?)</a>""", re.S)


def _marca_de_tiempo(ts):
    """data-timestamp de trends24 (segundos o milisegundos) en ISO 8601; None si no se entiende."""
    try:
        t = float(ts)
        t = t / 1000 if t > 1e11 else t
        return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def medir_x(max_items=20, snapshots=24):
    url = URL_X
    extra = {"origen": "Tendencias de X en la Argentina según trends24.in (espejo público; X no permite lectura directa)", "url": url}
    sin_datos = {"estado": "sin datos (cambió el formato del espejo)", "items": [], **extra}
    motivo = motivo_bloqueo(url)
    if motivo:
        return {"estado": motivo[0], "items": [], **extra, "detalle": motivo[1]}
    try:
        cuerpo, _ = _get(url)
    except Exception as e:
        return _fallo(e, extra)
    s = cuerpo.decode("utf-8", "replace")
    listas = []   # (data-timestamp tal cual, nombres), de la más nueva a la más vieja
    for ts, ol in _RX_BLOQUE.findall(s)[:snapshots]:
        nombres = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", m))).strip() for m in _RX_TENDENCIA.findall(ol)]
        listas.append((ts, [n for n in nombres if n]))
    actuales = [(ts, nombres) for ts, nombres in listas if nombres]
    if not actuales:
        return sin_datos
    ts_ultimo, ultimo = actuales[0]
    # horas en tendencia: en cuántas listas horarias aparece; un nombre cuenta una vez por lista ("Irán" e "Iran")
    persistencia = {}
    for _, nombres in listas:
        for k in {_norm(n) for n in nombres}:
            persistencia[k] = persistencia.get(k, 0) + 1
    items = []
    for pos, n in enumerate(ultimo[:max_items]):
        items.append({"tema": n, "puesto": pos + 1, "horas_en_tendencia": persistencia.get(_norm(n), 1),
                      "buscar": "https://x.com/search?q=" + urllib.parse.quote(n)})
    # la marca de tiempo es solo informativa: si no se entiende, las listas valen igual
    return {"estado": "ok", "items": items, "actualizado": _marca_de_tiempo(ts_ultimo), **extra, "snapshots": len(listas)}


# ---------- Cruce: ¿los medios cubren lo que es tendencia? ----------
_RX_CAMEL = re.compile(r"(?<=[a-záéíóúñü])(?=[A-ZÁÉÍÓÚÑÜ])|(?<=[A-ZÁÉÍÓÚÑÜ])(?=[A-ZÁÉÍÓÚÑÜ][a-záéíóúñü])"
                       r"|(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])")


def _rx_frase(palabras, separador=r"\W+"):
    """Las palabras juntas y en orden (con un separador entre ellas), como palabras enteras."""
    return re.compile(r"(?<!\w)" + separador.join(map(re.escape, palabras)) + r"(?!\w)")


# Están en STOP porque son demasiado generales para ser un tema de la prensa argentina, pero en una búsqueda o un
# hashtag distinguen ("boca vs argentinos", #BocaArgentinos): ahí no se descartan.
GENERICAS = {"argentina", "argentino", "argentinos", "argentinas", "gobierno", "pais", "nacional"}


def _relleno(w):
    """Palabra que no alcanza para buscar sola en una tendencia: vacía (de, el, hoy, jueves) o de menos de 3 letras."""
    return (w in STOP and w not in GENERICAS) or (len(w) < 3 and not w.isdigit())


def _patrones(tema, frase):
    """Expresiones que una nota tiene que contener (todas) para contar como cobertura de la tendencia.
    None si no se puede verificar."""
    hashtag = tema.startswith("#")
    t = tema.lstrip("#")
    if hashtag:
        # #LimiteAlAutoritarismo -> "Limite Al Autoritarismo"; #9DeJulio -> "9 De Julio"; #GH2026 -> "GH 2026"
        t = _RX_CAMEL.sub(" ", t.replace("_", " "))
    clave = _canon(_norm(t))
    if not re.search(r"\w", clave) or clave in STOP:
        return None      # vacío, solo símbolos o una palabra vacía ("el", "la"): no se puede medir
    if hashtag:
        todas = re.findall(r"\w+", clave)
        palabras = [w for w in todas if not _relleno(w) and len(w) >= 3]
        if len(palabras) < len(todas) or all(w.isdigit() for w in palabras):
            # se descartó algo (#FelizJueves, #ElNueve) o quedaron solo números: la frase entera y en orden; las
            # piezas pueden ir pegadas como en el hashtag (#8M -> "8M", #G20 -> "G20", #COP30 -> "COP30")
            return [_rx_frase(todas, r"\W*")]
        # en la prensa las palabras de un hashtag rara vez van juntas ("Boca y River"): basta con que estén todas
        return [_rx_frase([w]) for w in palabras]
    # Google separa los partidos con " - " o "vs": cada lado se busca por separado
    grupos = [g for g in (re.findall(r"\w+", p) for p in re.split(r"\s+(?:-|vs\.?)\s+", clave)) if g]
    if frase:
        # X: la tendencia es una frase; sus palabras tienen que ir juntas y no se descarta ninguna ("Feliz 4")
        return [_rx_frase(g) for g in grupos]
    # Google: la búsqueda es una bolsa de palabras; tienen que estar todas, en cualquier orden, salvo las de relleno
    # ("dolar hoy", "river en vivo", la "u" de "u. catolica"); si no queda ninguna, van todas
    todas = [w for g in grupos for w in g]
    palabras = [w for w in todas if not _relleno(w)] or todas
    return [_rx_frase([w]) for w in palabras]


def cruzar(items, notas, horas, frase=True):
    """Agrega a cada tendencia "en_medios": cuántos medios y notas de la ventana la mencionan.
    frase=True para X (frase literal), False para Google (todas las palabras)."""
    limite = _limite(horas)
    recientes = []
    for n in notas:
        if not _en_ventana(n, limite):
            continue
        recientes.append((_canon(_norm(_texto(n.get("titulo")) + " " + _texto(n.get("resumen")))), n))
    for it in items:
        rxs = _patrones(_texto(it.get("tema")), frase)
        if not rxs:
            # no se afirma "0 medios" de algo que no se buscó
            it["en_medios"] = {"medios": 0, "notas": 0, "ejemplos": [], "verificado": False}
            continue
        medios, ejemplos, total = set(), [], 0
        for texto, n in recientes:
            if all(rx.search(texto) for rx in rxs):
                total += 1
                medios.add(n.get("fuente"))
                if len(ejemplos) < 3 and n.get("fuente") not in [e["fuente"] for e in ejemplos]:
                    ejemplos.append({"titulo": n.get("titulo"), "link": n.get("link"), "fuente": n.get("fuente")})
        it["en_medios"] = {"medios": len(medios), "notas": total, "ejemplos": ejemplos, "verificado": True}
    return items


# ---------- Salida ----------
def _en_medios_md(em):
    if not em or em.get("verificado") is False:    # sin cruce (falló o no se pudo buscar): no se afirma "0 medios"
        return "sin verificar"
    return f"{em.get('medios', 0)} medios / {em.get('notas', 0)} notas"


def _estado_md(seccion):
    estado = seccion.get("estado", "?")
    return f"{estado} ({seccion['detalle']})" if seccion.get("detalle") else estado


def a_markdown(res):
    out = ["# Termómetro de tendencias", "", f"Generado: {res['generado']} · ventana {res['ventana_horas']} h", ""]
    out.append("## De qué hablan los medios (cobertura propia, cuántos medios distintos tocan el tema)")
    for it in res["medios"]:
        out.append(f"- **{it['tema']}** · {it['medios']} medios · {it['notas']} notas")
        for e in it["ejemplos"][:2]:
            out.append(f"  - {e['fuente']}: [{e['titulo']}]({e['link']})")
    out += ["", f"## Búsquedas en alza en la Argentina ({res['google'].get('origen', 'Google Trends')}) · {_estado_md(res['google'])}"]
    for it in res["google"].get("items", []):
        out.append(f"- **{it.get('tema', '')}** · {it.get('busquedas', '')} búsquedas · en los medios de la ingesta: {_en_medios_md(it.get('en_medios', {}))}")
    out += ["", f"## Tendencias de X en la Argentina · {_estado_md(res['x'])} · indicador de conversación, no fuente"]
    for it in res["x"].get("items", []):
        out.append(f"- {it.get('puesto', '?')}. **{it.get('tema', '')}** · {it.get('horas_en_tendencia', '?')} h en tendencia · en los medios: {_en_medios_md(it.get('en_medios', {}))}")
    return "\n".join(out) + "\n"


def _aviso(texto):
    print(f"[aviso] {texto}", file=sys.stderr)


def _leer_notas(ruta):
    """Lista de notas (solo las que son objetos). Un archivo ilegible, {"notas": null} o algo raro -> []."""
    try:
        with open(ruta, encoding="utf-8-sig") as f:
            d = json.load(f)
    except Exception as e:
        _aviso(f"no pude leer {ruta}: {e}")
        return []
    notas = d.get("notas") if isinstance(d, dict) else d
    if not isinstance(notas, list):
        _aviso(f"{ruta} no trae una lista de notas")
        return []
    limpias = [n for n in notas if isinstance(n, dict)]
    if len(limpias) < len(notas):
        _aviso(f"{len(notas) - len(limpias)} entradas de {ruta} no son notas y se ignoran")
    return limpias


def _escribir(ruta, texto):
    """Escribe de una vez (archivo temporal + reemplazo), creando la carpeta si hace falta. Nunca levanta."""
    tmp = ruta + ".tmp"
    try:
        os.makedirs(os.path.dirname(ruta) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(texto)
        os.replace(tmp, ruta)
        return True
    except Exception as e:
        _aviso(f"no pude escribir {ruta}: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--notas", default="notas.json")
    ap.add_argument("--salida", default="tendencias.json")
    ap.add_argument("--md", default="indice/tendencias.md")
    ap.add_argument("--horas", type=int, default=24)
    a = ap.parse_args(argv)
    notas = _leer_notas(a.notas)
    res = {"generado": dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=-3))).isoformat(timespec="minutes"),
           "ventana_horas": a.horas, "notas_leidas": len(notas)}
    try:
        limite = _limite(a.horas)
        res["notas_en_ventana"] = sum(1 for n in notas if _en_ventana(n, limite))
        res["medios"] = medir_medios(notas, a.horas)
    except Exception as e:
        _aviso(f"medios: {type(e).__name__}: {e}")
        res["medios"] = []
    # cada medida y su cruce van por separado: si falla una, las demás se escriben igual
    for clave, medir, frase in (("google", medir_google, False), ("x", medir_x, True)):
        try:
            res[clave] = medir()
        except Exception as e:
            res[clave] = {"estado": f"error: {type(e).__name__}", "items": [], "detalle": str(e)[:200]}
        try:
            res[clave]["items"] = cruzar(res[clave]["items"], notas, a.horas, frase=frase)
        except Exception as e:
            _aviso(f"cruce {clave}: {type(e).__name__}: {e}")
    _escribir(a.salida, json.dumps(res, ensure_ascii=False, indent=1, default=str))
    try:
        md = a_markdown(res)
    except Exception as e:
        _aviso(f"markdown: {type(e).__name__}: {e}")
        md = f"# Termómetro de tendencias\n\nGenerado: {res['generado']} · no pude armar el resumen ({type(e).__name__}); ver {a.salida}\n"
    _escribir(a.md, md)
    print(f"medios: {len(res['medios'])} temas · google: {res['google']['estado']} ({len(res['google']['items'])}) · x: {res['x']['estado']} ({len(res['x']['items'])})")


if __name__ == "__main__":
    main()
