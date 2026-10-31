#!/usr/bin/env python3
"""Capa de ingesta del informe diario de noticias de Buttom.

Uso:
    python3 -I ingestar.py --fuentes fuentes.json [--horas 36] --salida notas.json

Principios:
- User-agent honesto en todo pedido; nunca se simula un navegador.
- robots.txt (RFC 9309, con comodines) verificado por URL antes de cada pedido de
  nota, feed y redirección; se respeta Crawl-delay. Si robots.txt no se puede leer
  (red, 5xx, 401/403/429) no se bajan notas de ese dominio (se usa solo el feed).
- Máximo 1 pedido por segundo por dominio (o el Crawl-delay del sitio si es mayor),
  timeout de 20 s, reintento solo ante 5xx que no sea un desafío anti-bot.
- Ante 401, 403, 429 o desafío anti-bot el dominio queda bloqueado por el resto de la corrida.
- Las fuentes "solo_rss" nunca bajan notas; los dominios de "excluidas" nunca se piden.
- Corre con la biblioteca estándar; si feedparser / trafilatura están instalados, los usa.

Salida (junto a --salida): notas.json, resumen_ingesta.md, indice/*.md, textos/<id>.md.
"""
import argparse
import datetime as dt
import email.utils
import hashlib
import html
import html.entities
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from html.parser import HTMLParser

try:
    import feedparser  # type: ignore
except Exception:  # pragma: no cover
    feedparser = None
try:
    import trafilatura  # type: ignore
except Exception:  # pragma: no cover
    trafilatura = None

UA = "ButtomNewsReader/1.0 (lector RSS personal)"
TIMEOUT = 20
MAX_NOTAS_TEXTO = 12              # notas con texto completo por fuente
MAX_INTENTOS_TEXTO = 2 * MAX_NOTAS_TEXTO
MAX_FALLAS_SEGUIDAS = 4           # fallas consecutivas de descarga/extracción → se abandona la fuente
MAX_TEXTO = 6000
LIMITE_TOTAL_S = 6 * 60           # el proceso entero termina antes de esto
LIMITE_FUENTE_S = 150             # una fuente que demora más se corta y se anota
MAX_CRAWL_DELAY = 60.0            # si el sitio pide más, se usa solo el feed
MAX_REDIRECCIONES = 3
INICIO = time.monotonic()
ZONA_AR = dt.timezone(dt.timedelta(hours=-3))
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
DIAS_CORTO = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
AGENTES_IA = ["ClaudeBot", "Claude-User", "Claude-SearchBot", "Claude-Web",
              "anthropic-ai", "GPTBot", "CCBot"]
PRIORITARIOS = {"velez", "perfumeria", "normas", "caba", "macro", "ecommerce", "consumo"}

DENEGADOS = set()                 # dominios base de fuentes excluidas (de fuentes.json)
BLOQUEADOS = set()                # dominios que pidieron parar en esta corrida (401/403/429/anti-bot)
_LOCK_BLOQUEADOS = threading.Lock()

# ---------------------------------------------------------------- utilidades


def log(*a):
    print(f"[{time.monotonic()-INICIO:6.1f}s]", *a, file=sys.stderr, flush=True)


def tiempo_restante():
    return LIMITE_TOTAL_S - (time.monotonic() - INICIO)


def ahora_utc():
    return dt.datetime.now(dt.timezone.utc)


def ahora_ar():
    return dt.datetime.now(ZONA_AR)


def dominio_base(netloc):
    """www.clarin.com → clarin.com; www.lanacion.com.ar → lanacion.com.ar."""
    h = (netloc or "").lower().split(":")[0]
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", h) or h in ("localhost", ""):
        return h
    partes = h.split(".")
    if len(partes) >= 3 and partes[-2] in ("com", "org", "gob", "gov", "net", "edu", "co") and len(partes[-1]) == 2:
        return ".".join(partes[-3:])
    return ".".join(partes[-2:]) if len(partes) >= 2 else h


def bloquear_dominio(dominio, motivo):
    with _LOCK_BLOQUEADOS:
        if dominio not in BLOQUEADOS:
            BLOQUEADOS.add(dominio)
            log(f"dominio bloqueado por el resto de la corrida: {dominio} ({motivo})")


def esta_bloqueado(dominio):
    with _LOCK_BLOQUEADOS:
        return dominio in BLOQUEADOS


class LimitadorDominio:
    """Máximo un pedido por `intervalo` segundos por dominio (también entre hilos)."""

    def __init__(self):
        self._g = threading.Lock()
        self._locks = {}
        self._ultimo = {}
        self.intervalos = {}

    def esperar(self, dominio):
        with self._g:
            lk = self._locks.setdefault(dominio, threading.Lock())
        with lk:
            intervalo = max(1.0, self.intervalos.get(dominio, 1.0))
            falta = self._ultimo.get(dominio, 0.0) + intervalo - time.monotonic()
            if falta > 0:
                time.sleep(falta)
            self._ultimo[dominio] = time.monotonic()


LIMITADOR = LimitadorDominio()


class _SinRedireccion(urllib.request.HTTPRedirectHandler):
    """No sigue redirecciones solo: cada salto pasa por robots, limitador y lista de bloqueo."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(_SinRedireccion())


class RespuestaHTTP:
    def __init__(self, codigo, cuerpo, tipo, url, antibot=False, headers=None):
        self.codigo, self.cuerpo, self.tipo, self.url = codigo, cuerpo, tipo, url
        self.antibot = antibot
        self.headers = headers or {}

    @property
    def motivo(self):
        if self.antibot:
            return f"desafío anti-bot (HTTP {self.codigo})"
        if self.codigo == 0:
            return self.tipo or "sin respuesta"
        return f"HTTP {self.codigo}"


def url_para_pedir(url):
    """Percent-encoding de caracteres no ASCII o espacios; deja intacto lo ya codificado."""
    p = urllib.parse.urlsplit(url.strip())
    path = urllib.parse.quote(p.path, safe="/%:@+,;=~!$'()*")
    query = urllib.parse.quote(p.query, safe="=&%+:/?,;~@!$'()*")
    return urllib.parse.urlunsplit((p.scheme, p.netloc, path, query, ""))


def es_desafio_antibot(texto):
    """Señales de desafío JS/captcha. Mira el <title> y el comienzo del documento."""
    t = texto[:3000].lower()
    m = re.search(r"<title[^>]*>(.*?)</title>", texto[:20000], re.I | re.S)
    titulo = (m.group(1) if m else "").lower()
    patrones = ["checking your browser", "datadome", "just a moment", "captcha-delivery.com",
                "__cf_chl_", "cf-chl-bypass", "attention required! | cloudflare",
                "verifica que eres humano", "please enable javascript and cookies to continue",
                "verify you are human"]
    if any(p in titulo for p in patrones):
        return True
    return any(p in t for p in patrones) and "<article" not in texto[:60000].lower()


def descargar(url, reintentos=3, mismo_dominio=None, verificar_robots=False, marcar_bloqueo=True):
    """GET con UA honesto. Nunca lanza. Resuelve redirecciones a mano (máx. 3), verificando
    en cada salto el dominio permitido, robots.txt y el limitador."""
    saltos = 0
    while True:
        try:
            url = url_para_pedir(url)
        except Exception:
            return RespuestaHTTP(0, b"", "URL inválida", url)
        p = urllib.parse.urlsplit(url)
        dominio = p.netloc.lower()
        if not dominio or p.scheme not in ("http", "https"):
            return RespuestaHTTP(0, b"", "URL inválida", url)
        if dominio_base(dominio) in DENEGADOS:
            return RespuestaHTTP(0, b"", "dominio excluido por política", url)
        if esta_bloqueado(dominio):
            return RespuestaHTTP(0, b"", "dominio bloqueado en esta corrida (pidió parar)", url)
        if mismo_dominio and dominio_base(dominio) != mismo_dominio:
            return RespuestaHTTP(0, b"", f"fuera del dominio de la fuente ({dominio})", url)
        if verificar_robots and not ROBOTS.permite(url):
            return RespuestaHTTP(0, b"", "robots.txt no permite esta URL", url)
        ultimo = None
        redirigir_a = None
        for intento in range(max(1, reintentos)):
            LIMITADOR.esperar(dominio)
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, "
                          "text/html;q=0.9, application/json;q=0.9, */*;q=0.5",
                "Accept-Language": "es-AR, es;q=0.9, en;q=0.5",
            })
            try:
                with OPENER.open(req, timeout=TIMEOUT) as r:
                    cuerpo = r.read(5_000_000)
                    return RespuestaHTTP(r.status, cuerpo, r.headers.get("Content-Type", ""), r.geturl(),
                                         headers=dict(r.headers))
            except urllib.error.HTTPError as e:
                if 300 <= e.code < 400:
                    loc = e.headers.get("Location") if e.headers else None
                    if not loc:
                        return RespuestaHTTP(e.code, b"", "redirección sin Location", url)
                    redirigir_a = urllib.parse.urljoin(url, loc)
                    break
                try:
                    cuerpo = e.read(200_000)
                except Exception:
                    cuerpo = b""
                texto = cuerpo.decode("utf-8", "ignore")
                cabeceras = dict(e.headers) if e.headers else {}
                antibot = es_desafio_antibot(texto) or \
                    (cabeceras.get("cf-mitigated", "").lower() == "challenge")
                if antibot or e.code in (401, 403, 429):
                    if marcar_bloqueo:
                        bloquear_dominio(dominio, "anti-bot" if antibot else f"HTTP {e.code}")
                    return RespuestaHTTP(e.code, b"", cabeceras.get("Content-Type", ""), url,
                                         antibot=antibot, headers=cabeceras)
                ultimo = RespuestaHTTP(e.code, b"", cabeceras.get("Content-Type", ""), url, headers=cabeceras)
                if 500 <= e.code < 600 and intento < reintentos - 1 and tiempo_restante() > 15:
                    time.sleep(2.0 * (intento + 1))
                    continue
                return ultimo
            except Exception as e:  # timeout, DNS, TLS, reset
                return RespuestaHTTP(0, b"", f"sin respuesta: {str(e)[:120]}", url)
        if redirigir_a is None:
            return ultimo or RespuestaHTTP(0, b"", "sin respuesta", url)
        saltos += 1
        if saltos > MAX_REDIRECCIONES:
            return RespuestaHTTP(0, b"", "demasiadas redirecciones", url)
        url = redirigir_a


# ------------------------------------------------------------------ robots


class ReglasRobots:
    """robots.txt según RFC 9309: grupos por user-agent, comodines * y $, gana la regla
    más larga que coincide y, a igual longitud, Allow."""

    def __init__(self, texto):
        self.grupos = []  # [{'agentes': [..], 'reglas': [(permite, patron)], 'delay': float|None}]
        actual = None
        ultimo_fue_ua = False
        for linea in (texto or "").splitlines():
            linea = linea.split("#", 1)[0].strip()
            if not linea or ":" not in linea:
                continue
            clave, valor = linea.split(":", 1)
            clave, valor = clave.strip().lower(), valor.strip()
            if clave == "user-agent":
                if actual is None or not ultimo_fue_ua:
                    actual = {"agentes": [], "reglas": [], "delay": None}
                    self.grupos.append(actual)
                actual["agentes"].append(valor.lower())
                ultimo_fue_ua = True
                continue
            ultimo_fue_ua = False
            if actual is None:
                continue
            if clave in ("allow", "disallow"):
                actual["reglas"].append((clave == "allow", valor))
            elif clave == "crawl-delay":
                try:
                    actual["delay"] = float(valor)
                except ValueError:
                    pass
        self._cache_re = {}

    def _grupo(self, ua):
        token = ua.split("/")[0].strip().lower()
        mejor, mejor_len = None, -1
        for g in self.grupos:
            for a in g["agentes"]:
                if a == "*":
                    continue
                if a == token or a in token or token in a:
                    if len(a) > mejor_len:
                        mejor, mejor_len = g, len(a)
        if mejor is not None:
            return mejor, True
        for g in self.grupos:
            if "*" in g["agentes"]:
                return g, False
        return None, False

    def _coincide(self, patron, path):
        if patron == "":
            return False
        rx = self._cache_re.get(patron)
        if rx is None:
            fin = patron.endswith("$")
            cuerpo = patron[:-1] if fin else patron
            partes = [re.escape(x) for x in cuerpo.split("*")]
            rx = re.compile("^" + ".*".join(partes) + ("$" if fin else ""))
            self._cache_re[patron] = rx
        return rx.match(path) is not None

    def permite(self, ua, path):
        g, _ = self._grupo(ua)
        if g is None:
            return True
        mejor = None  # (longitud, permite)
        for permite, patron in g["reglas"]:
            if self._coincide(patron, path):
                cand = (len(patron), permite)
                if mejor is None or cand[0] > mejor[0] or (cand[0] == mejor[0] and permite):
                    mejor = cand
        return True if mejor is None else mejor[1]

    def crawl_delay(self, ua):
        g, _ = self._grupo(ua)
        return g["delay"] if g else None

    def bloquea_ia(self):
        """Agentes de IA con grupo propio que no pueden leer la raíz."""
        res = []
        for ag in AGENTES_IA:
            g, especifico = self._grupo(ag)
            if especifico and not self.permite(ag, "/"):
                res.append(ag)
        return res


class Robots:
    """Cache de robots.txt por (esquema, dominio)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cache = {}

    def _cargar(self, esquema, dominio):
        url = f"{esquema}://{dominio}/robots.txt"
        info = {"reglas": None, "estado": "", "delay": None, "bloquea_ia": [], "codigo": None}
        resp = descargar(url, reintentos=2, marcar_bloqueo=False)
        info["codigo"] = resp.codigo
        if resp.codigo == 200:
            texto = resp.cuerpo.decode("utf-8", "ignore")
            info["reglas"] = ReglasRobots(texto)
            info["estado"] = "ok"
        elif resp.codigo in (404, 410) or (400 <= resp.codigo < 500 and resp.codigo not in (401, 403, 429)):
            info["reglas"] = ReglasRobots("")   # sin reglas: todo permitido
            info["estado"] = f"sin robots.txt (HTTP {resp.codigo})"
        else:
            # 401/403/429, 5xx o sin respuesta: por prudencia (RFC 9309) no se bajan notas.
            info["reglas"] = None
            info["estado"] = f"robots.txt inaccesible ({resp.motivo}): no se bajan notas de este dominio"
        if info["reglas"] is not None:
            delay = info["reglas"].crawl_delay(UA)
            if delay:
                info["delay"] = delay
                LIMITADOR.intervalos[dominio] = max(1.0, min(delay, MAX_CRAWL_DELAY))
            info["bloquea_ia"] = info["reglas"].bloquea_ia()
        return info

    def info(self, url):
        p = urllib.parse.urlsplit(url)
        esquema = p.scheme if p.scheme in ("http", "https") else "https"
        dominio = p.netloc.lower()
        clave = (esquema, dominio)
        with self._lock:
            if clave not in self._cache:
                self._cache[clave] = None  # marca de "cargando"
                cargando = True
            else:
                cargando = False
        if cargando:
            try:
                datos = self._cargar(esquema, dominio)
            except Exception as e:
                datos = {"reglas": None, "estado": f"error leyendo robots.txt: {str(e)[:80]}",
                         "delay": None, "bloquea_ia": [], "codigo": None}
            with self._lock:
                self._cache[clave] = datos
            return datos
        for _ in range(900):  # esperar a que otro hilo termine de cargar
            with self._lock:
                datos = self._cache.get(clave)
            if datos is not None:
                return datos
            time.sleep(0.1)
        return {"reglas": None, "estado": "robots.txt: espera agotada", "delay": None, "bloquea_ia": [], "codigo": None}

    def permite(self, url):
        datos = self.info(url)
        reglas = datos.get("reglas")
        if reglas is None:
            return False
        p = urllib.parse.urlsplit(url)
        path = (p.path or "/") + (("?" + p.query) if p.query else "")
        try:
            return reglas.permite(UA, path)
        except Exception:
            return False


ROBOTS = Robots()

# ------------------------------------------------------------- parseo feeds

MESES = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
         "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
         "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "sep": 9, "sept": 9,
         "oct": 10, "nov": 11, "dic": 12}


def parsear_fecha(s, tz_naive=dt.timezone.utc):
    """Devuelve datetime con zona horaria o None. Fechas sin zona → tz_naive."""
    if not s:
        return None
    s = s.strip()
    s = re.sub(r"\b(ART|ARST)\b\s*$", "-0300", s)
    try:
        d = email.utils.parsedate_to_datetime(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=tz_naive)
        return d
    except Exception:
        pass
    try:
        s2 = re.sub(r"[Zz]$", "+00:00", s)
        s2 = re.sub(r"\.(\d{3})\d+", r".\1", s2)
        d = dt.datetime.fromisoformat(s2)
        if d.tzinfo is None:
            d = d.replace(tzinfo=tz_naive)
        return d
    except Exception:
        pass
    m = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})(?:[ T](\d{1,2}):(\d{2}))?", s)
    if m:
        try:
            return dt.datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                               int(m.group(4) or 0), int(m.group(5) or 0), tzinfo=tz_naive)
        except ValueError:
            return None
    m = re.match(r"(\d{1,2})\s+de\s+([a-záé]+)\s+de\s+(\d{4})(?:[ ,]+(\d{1,2}):(\d{2}))?", s, re.I)
    if m and m.group(2).lower() in MESES:
        try:
            return dt.datetime(int(m.group(3)), MESES[m.group(2).lower()], int(m.group(1)),
                               int(m.group(4) or 0), int(m.group(5) or 0), tzinfo=tz_naive)
        except ValueError:
            return None
    return None


def limpiar_html(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _txt(el):
    return (el.text or "").strip() if el is not None else ""


_ENTIDAD_HTML = re.compile(rb"&([A-Za-z][A-Za-z0-9]{1,8});")
_XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"


def _reemplazar_entidades(cuerpo):
    def rep(m):
        nombre = m.group(1).decode("ascii", "ignore")
        if nombre in ("amp", "lt", "gt", "quot", "apos"):
            return m.group(0)
        cp = html.entities.name2codepoint.get(nombre)
        return f"&#{cp};".encode("ascii") if cp else m.group(0)
    return _ENTIDAD_HTML.sub(rep, cuerpo)


def parsear_feed_stdlib(cuerpo, base_url=""):
    cuerpo = cuerpo.lstrip(b"\xef\xbb\xbf \t\r\n")
    try:
        root = ET.fromstring(cuerpo)
    except ET.ParseError as e:
        if "undefined entity" in str(e).lower():
            root = ET.fromstring(_reemplazar_entidades(cuerpo))
        else:
            raise
    ns = {"a": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/elements/1.1/",
          "r1": "http://purl.org/rss/1.0/", "content": "http://purl.org/rss/1.0/modules/content/"}
    base = root.get(_XML_BASE) or base_url
    items = []
    for it in root.findall(".//item"):          # RSS 2.0
        items.append({
            "titulo": _txt(it.find("title")),
            "resumen": _txt(it.find("description")) or _txt(it.find("content:encoded", ns)),
            "link": urllib.parse.urljoin(base, _txt(it.find("link")) or _txt(it.find("guid"))),
            "fecha": _txt(it.find("pubDate")) or _txt(it.find("dc:date", ns)),
        })
    if not items:
        for it in root.findall(".//a:entry", ns):   # Atom
            link = ""
            for l in it.findall("a:link", ns):
                if l.get("rel") in (None, "alternate"):
                    link = l.get("href", "")
                    break
            b = it.get(_XML_BASE) or base
            items.append({
                "titulo": _txt(it.find("a:title", ns)),
                "resumen": _txt(it.find("a:summary", ns)) or _txt(it.find("a:content", ns)),
                "link": urllib.parse.urljoin(b, link) if link else "",
                "fecha": _txt(it.find("a:published", ns)) or _txt(it.find("a:updated", ns)),
            })
    if not items:
        for it in root.findall(".//r1:item", ns):   # RSS 1.0 / RDF
            items.append({
                "titulo": _txt(it.find("r1:title", ns)),
                "resumen": _txt(it.find("r1:description", ns)),
                "link": urllib.parse.urljoin(base, _txt(it.find("r1:link", ns)) or
                                             it.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about", "")),
                "fecha": _txt(it.find("dc:date", ns)),
            })
    return items


def parsear_feed(cuerpo, base_url="", tz_naive=dt.timezone.utc):
    """Devuelve lista de dicts {titulo, resumen, link, fecha(datetime|None)}."""
    items = []
    if feedparser is not None:
        try:
            fp = feedparser.parse(cuerpo)
            for e in fp.entries:
                fecha = None
                for k in ("published_parsed", "updated_parsed", "created_parsed"):
                    if e.get(k):
                        fecha = dt.datetime(*e[k][:6], tzinfo=dt.timezone.utc)
                        break
                if fecha is None:
                    fecha = parsear_fecha(e.get("published") or e.get("updated") or "", tz_naive)
                resumen = e.get("summary") or ""
                if not resumen and e.get("content"):
                    resumen = e["content"][0].get("value", "")
                link = e.get("link", "") or ""
                items.append({"titulo": e.get("title", ""), "resumen": resumen,
                              "link": urllib.parse.urljoin(base_url, link) if link else "", "fecha": fecha})
            if items:
                return items
        except Exception:
            items = []
    for it in parsear_feed_stdlib(cuerpo, base_url):
        it["fecha"] = parsear_fecha(it["fecha"], tz_naive)
        items.append(it)
    return items


# ------------------------------------------------------ normalización / dedupe

PARAMS_RASTREO = re.compile(r"^(utm_|fbclid|gclid|at_|ref$|ns_|mc_|cmpid|ito)", re.I)


def link_canonico(url):
    """Solo para deduplicar: sin parámetros de rastreo ni barra final. Para pedir se usa el link original."""
    if not url:
        return ""
    try:
        p = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return ""
    if not p.netloc:
        return ""
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
         if not PARAMS_RASTREO.match(k)]
    path = p.path or "/"
    if path.endswith("/") and len(path) > 1:
        path = path[:-1]
    return urllib.parse.urlunsplit((p.scheme.lower() or "https", p.netloc.lower(), path,
                                    urllib.parse.urlencode(q), ""))


def link_sin_rastreo(url):
    """URL para pedir la nota: la original sin parámetros de rastreo (utm_ y similares), path intacto."""
    try:
        p = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
         if not PARAMS_RASTREO.match(k)]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), ""))


def titulo_normalizado(t):
    t = limpiar_html(t).lower()
    t = re.sub(r"[^\w\s]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def id_nota(fuente_id, link):
    return fuente_id + "-" + hashlib.sha1(link.encode("utf-8")).hexdigest()[:12]


# ------------------------------------------- secciones del informe y temas

SECCION_INFORME = {
    "politica": "politica",
    "economia": "economia", "finanzas": "economia", "impuestos": "economia", "comercio-exterior": "economia",
    "negocios": "negocios", "retail": "negocios", "belleza": "negocios",
    "mundo": "mundo", "internacional": "mundo",
    "deportes": "deportes", "velez": "deportes", "futbol-primera": "deportes", "seleccion": "deportes",
    "libertadores": "deportes", "futbol": "deportes",
    "sociedad": "sociedad",
    "ultimas": "general", "portada": "general", "argentina": "general",
}
ORDEN_SECCIONES = ["politica", "economia", "negocios", "mundo", "sociedad", "deportes", "general"]


def seccion_de_nota(seccion_feed, link):
    """Sección de una nota. Si el feed es general (portada, últimas), la toma del camino del link
    cuando uno de sus tramos es una sección conocida (p. ej. /noticias/politica/...); si no, la del feed."""
    if SECCION_INFORME.get(seccion_feed or "", "general") != "general":
        return seccion_feed
    try:
        tramos = urllib.parse.urlsplit(link or "").path.lower().split("/")
    except ValueError:
        return seccion_feed
    for t in tramos:
        if t and SECCION_INFORME.get(t, "general") != "general":
            return t
    return seccion_feed

TEMAS = [
    ("velez", r"\bv[ée]lez\b|\bfort[ií]n\b|amalfitani|barros schelotto"),
    ("perfumeria", r"perfum|fragancia|cosm[ée]tic|\bbelleza\b|\bbeauty\b|fragrance|lattafa|\bdupes?\b|arom[aá]tic|\bvelas?\b|difusor"),
    ("ecommerce", r"e-?commerce|comercio electr[oó]nico|mercado ?libre|mercado ?pago|tiendanube|shopify|cyber ?monday|hot ?sale|correo argentino|courier|andreani|log[ií]stica|\bshein\b|\btemu\b|\bamazon\b"),
    ("normas", r"bolet[ií]n oficial|\bdecretos?\b|\bresoluci[oó]n\b|\bafip\b|arancel|importaci[oó]n|ingresos brutos|monotribut"),
    ("caba", r"\bcaba\b|ciudad de buenos aires|jorge macri|porte[ñn]|legislatura porte|\bsubte\b|\babl\b"),
    ("macro", r"\bd[oó]lar|riesgo pa[ií]s|inflaci[oó]n|\btasas? de inter|\breservas\b|\bcaputo\b|banco central"),
    ("consumo", r"ventas minoristas|consumo masivo|scentia|d[ií]a de la madre"),
    ("rumores", r"trascendi|\brumor|versiones?\b|\ben off\b|off the record|se especula|habr[ií]a\b|podr[ií]a\b"),
    ("agenda", r"\bhoy\b|\bma[ñn]ana\b|esta semana|\bvence\b|cronograma|calendario|se re[uú]ne|convoca|licitaci[oó]n|\bfixture\b"),
]
TEMAS_RE = [(k, re.compile(p, re.I)) for k, p in TEMAS]
# Siglas que solo cuentan en mayúsculas (CAPA, la cámara de cosmética, no "capa").
TEMAS_SIGLAS = [("perfumeria", re.compile(r"\bCAPA\b")), ("consumo", re.compile(r"\bCAME\b|\bCACE\b")),
                ("normas", re.compile(r"\bARCA\b|\bANMAT\b|\bDNU\b")), ("macro", re.compile(r"\bBCRA\b|\bINDEC\b|\bFMI\b"))]


def detectar_temas(nota):
    """Etiqueta por título y resumen (no por el cuerpo: una mención al pasar no hace tema)."""
    base = " ".join([nota.get("titulo") or "", nota.get("resumen") or ""])
    temas = [k for k, rx in TEMAS_RE if rx.search(base)]
    for k, rx in TEMAS_SIGLAS:
        if k not in temas and rx.search(base):
            temas.append(k)
    return temas


def fecha_local(iso):
    try:
        d = dt.datetime.fromisoformat(iso).astimezone(ZONA_AR)
        return f"{DIAS_CORTO[d.weekday()]} {d:%d/%m %H:%M}"
    except Exception:
        return (iso or "")[:16]


# --------------------------------------------------------- extracción texto


class ExtractorSimple(HTMLParser):
    """Toma los <p> del <article> o, si no hay, del bloque con más texto."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.en_article = 0
        self.en_p = False
        self.saltar = 0
        self.actual = []
        self.ps_article, self.ps_todo = [], []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg", "figure", "aside", "nav", "footer", "header"):
            self.saltar += 1
        elif tag == "article":
            self.en_article += 1
        elif tag == "p" and not self.saltar:
            self.en_p, self.actual = True, []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg", "figure", "aside", "nav", "footer", "header"):
            self.saltar = max(0, self.saltar - 1)
        elif tag == "article":
            self.en_article = max(0, self.en_article - 1)
        elif tag == "p" and self.en_p:
            texto = re.sub(r"\s+", " ", "".join(self.actual)).strip()
            self.en_p = False
            if len(texto) > 40:
                (self.ps_article if self.en_article else self.ps_todo).append(texto)

    def handle_data(self, data):
        if self.en_p and not self.saltar:
            self.actual.append(data)


def decodificar_html(cuerpo, tipo):
    """Usa el charset del Content-Type o del <meta charset>; UTF-8 como último recurso."""
    candidatos = []
    m = re.search(r"charset=\"?([\w-]+)", tipo or "", re.I)
    if m:
        candidatos.append(m.group(1))
    m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", cuerpo[:5000], re.I)
    if m:
        candidatos.append(m.group(1).decode("ascii", "ignore"))
    for cs in candidatos + ["utf-8"]:
        try:
            return cuerpo.decode(cs)
        except Exception:
            continue
    return cuerpo.decode("utf-8", "ignore")


LOCK_EXTRACCION = threading.Lock()  # lxml no es seguro con hilos en algunos builds


def extraer_texto(html_texto):
    if trafilatura is not None:
        try:
            with LOCK_EXTRACCION:
                t = trafilatura.extract(html_texto, include_comments=False, include_tables=False,
                                        favor_recall=True)
            if t and len(t) > 300:
                return t.strip()
        except Exception:
            pass
    try:
        ex = ExtractorSimple()
        ex.feed(html_texto)
        ps = ex.ps_article or ex.ps_todo
        t = "\n\n".join(ps)
        return t if len(t) > 200 else ""
    except Exception:
        return ""


# -------------------------------------------------------------- por fuente


def falla(lista, fid, url, motivo, tipo):
    lista.append({"fuente": fid, "url": url, "motivo": motivo, "tipo": tipo})


def procesar_fuente(fuente, desde, hasta, resultado):
    """Baja los feeds de una fuente, filtra, deduplica y (si corresponde) baja texto.
    Las listas se registran en `resultado` desde el inicio: si la fuente se corta por tiempo,
    lo ya obtenido se conserva."""
    fid = fuente["id"]
    notas, fallas, feeds_ok, avisos = [], [], [], []
    resultado[fid] = {"notas": notas, "fallas": fallas, "feeds_ok": feeds_ok, "avisos": avisos}
    inicio_fuente = time.monotonic()
    dom_base = dominio_base(fuente.get("dominio") or urllib.parse.urlsplit(fuente["feeds"][0]["url"]).netloc)
    tz_naive = ZONA_AR if (fuente.get("pais") or "").upper() == "AR" else dt.timezone.utc
    vistos_link, vistos_titulo = set(), set()

    def sin_tiempo(margen):
        if tiempo_restante() < margen:
            return "sin tiempo (límite global)"
        if time.monotonic() - inicio_fuente > LIMITE_FUENTE_S:
            return "sin tiempo (límite por fuente)"
        return None

    # robots.txt de cada dominio de feed, antes del primer pedido (fija el Crawl-delay)
    raices = sorted({f"{urllib.parse.urlsplit(f['url']).scheme or 'https'}://{urllib.parse.urlsplit(f['url']).netloc}/"
                     for f in fuente.get("feeds", [])})
    for raiz in raices:
        dom_feed = urllib.parse.urlsplit(raiz).netloc
        info = ROBOTS.info(raiz)
        if info.get("estado") and info["estado"] != "ok":
            avisos.append(f"{dom_feed}: {info['estado']}")
        if info.get("bloquea_ia"):
            avisos.append(f"{dom_feed}: robots.txt bloquea agentes de IA ({', '.join(info['bloquea_ia'])}); "
                          f"se usa solo el feed, revisar fuentes.json")
        if info.get("delay") and info["delay"] > MAX_CRAWL_DELAY:
            avisos.append(f"{dom_feed}: Crawl-delay de {info['delay']:.0f} s supera el máximo ({MAX_CRAWL_DELAY:.0f} s); se usa solo el feed")

    # Primero los feeds de sección específica, después portadas/últimas (así cada nota queda
    # en la sección más precisa); dentro de cada grupo, por prioridad.
    feeds = sorted(fuente.get("feeds", []),
                   key=lambda f: (SECCION_INFORME.get(f.get("seccion", ""), "general") == "general", f.get("prioridad", 3)))
    for feed in feeds:
        corte = sin_tiempo(20)
        if corte:
            falla(fallas, fid, feed["url"], corte, "feed")
            continue
        info = ROBOTS.info(feed["url"])
        if info.get("reglas") is not None and not ROBOTS.permite(feed["url"]):
            falla(fallas, fid, feed["url"], "robots.txt no permite el feed", "feed")
            continue
        resp = descargar(feed["url"])
        if resp.codigo != 200:
            falla(fallas, fid, feed["url"], resp.motivo, "feed")
            continue
        try:
            items = parsear_feed(resp.cuerpo, resp.url or feed["url"], tz_naive)
        except Exception as e:
            falla(fallas, fid, feed["url"], f"XML inválido: {str(e)[:80]}", "feed")
            continue
        if not items:
            falla(fallas, fid, feed["url"], "feed sin ítems", "feed")
            continue
        feeds_ok.append(feed["url"])
        descartes = {"sin_link": 0, "sin_fecha": 0, "fuera_de_ventana": 0, "futura": 0, "invalido": 0}
        nuevas = 0
        for it in items:
            try:
                link_orig = (it.get("link") or "").strip()
                link = link_canonico(link_orig)
                titulo = limpiar_html(it.get("titulo") or "")
                if not link or not titulo:
                    descartes["sin_link"] += 1
                    continue
                fecha = it.get("fecha")
                if fecha is None:
                    descartes["sin_fecha"] += 1
                    continue
                if fecha < desde:
                    descartes["fuera_de_ventana"] += 1
                    continue
                if fecha > hasta:
                    descartes["futura"] += 1
                    continue
                tn = titulo_normalizado(titulo)
                if link in vistos_link or tn in vistos_titulo:
                    continue
                vistos_link.add(link)
                vistos_titulo.add(tn)
                nota = {
                    "id": id_nota(fid, link),
                    "fuente": fid,
                    "linea_editorial": fuente.get("linea_editorial", ""),
                    "idioma": fuente.get("idioma", "es"),
                    "seccion": seccion_de_nota(feed.get("seccion", ""), link_orig),
                    "seccion_informe": SECCION_INFORME.get(seccion_de_nota(feed.get("seccion", ""), link_orig), "general"),
                    "prioridad": feed.get("prioridad", 3),
                    "titulo": titulo,
                    "resumen": limpiar_html(it.get("resumen") or "")[:1200],
                    "link": link_orig,
                    "fecha": fecha.astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
                    "texto": None,
                    "texto_recortado": False,
                    "metodo": "rss",
                }
                nota["temas"] = detectar_temas(nota)
                notas.append(nota)
                nuevas += 1
            except Exception as e:
                descartes["invalido"] += 1
                falla(fallas, fid, feed["url"], f"ítem inválido: {str(e)[:80]}", "feed")
        if nuevas == 0 and descartes["sin_fecha"] == len(items):
            falla(fallas, fid, feed["url"], f"feed con {len(items)} ítems sin fecha parseable", "feed")
        elif descartes["futura"]:
            avisos.append(f"{feed['url']}: {descartes['futura']} ítems con fecha futura descartados")

    # Texto completo
    if fuente.get("acceso") == "texto_completo" and notas:
        bajar_texto(fuente, notas, fallas, avisos, dom_base, sin_tiempo)
    for n in notas:
        n.pop("prioridad", None)
    log(f"{fid}: feeds_ok={len(feeds_ok)} notas={len(notas)} texto={sum(1 for n in notas if n['texto'])} fallas={len(fallas)}")


def bajar_texto(fuente, notas, fallas, avisos, dom_base, sin_tiempo):
    fid = fuente["id"]
    # Prioridad: notas de temas prioritarios para el informe, después prioridad del feed, después más recientes.
    candidatas = sorted(notas, key=lambda n: (0 if set(n["temas"]) & PRIORITARIOS else 1,
                                              n["prioridad"],
                                              -dt.datetime.fromisoformat(n["fecha"]).timestamp()))
    bajadas = intentos = seguidas = antibot_seguidos = 0
    for n in candidatas:
        if bajadas >= MAX_NOTAS_TEXTO:
            break
        corte = sin_tiempo(25)
        if corte:
            falla(fallas, fid, "", f"{corte}; quedan notas solo con RSS", "nota")
            break
        if intentos >= MAX_INTENTOS_TEXTO or seguidas >= MAX_FALLAS_SEGUIDAS:
            falla(fallas, fid, "", f"se abandona el texto completo tras {intentos} intentos "
                                   f"({seguidas} fallas seguidas); el resto queda solo con RSS", "nota")
            break
        dom_nota = urllib.parse.urlsplit(n["link"]).netloc.lower()
        if dominio_base(dom_nota) != dom_base:
            falla(fallas, fid, n["link"], f"link fuera del dominio de la fuente ({dom_nota}); no se pide", "nota")
            continue
        info = ROBOTS.info(n["link"])
        if info.get("reglas") is None:
            falla(fallas, fid, "", info.get("estado") or "robots.txt inaccesible; solo RSS", "robots")
            break
        if info.get("bloquea_ia"):
            falla(fallas, fid, "", f"robots.txt de {dom_nota} bloquea agentes de IA ({', '.join(info['bloquea_ia'])}); "
                                   "se usa solo el feed", "robots")
            break
        if info.get("delay") and info["delay"] > MAX_CRAWL_DELAY:
            falla(fallas, fid, "", f"Crawl-delay de {info['delay']:.0f} s en {dom_nota} supera el máximo; se usa solo el feed", "robots")
            break
        url_nota = link_sin_rastreo(n["link"])
        if not ROBOTS.permite(url_nota):
            falla(fallas, fid, url_nota, "robots.txt no permite la nota", "nota")
            continue  # no cuenta como falla del servidor
        intentos += 1
        resp = descargar(url_nota, mismo_dominio=dom_base, verificar_robots=True)
        if resp.codigo != 200:
            falla(fallas, fid, url_nota, f"nota: {resp.motivo}", "nota")
            if resp.antibot or resp.codigo in (401, 403, 429) or esta_bloqueado(dom_nota):
                break  # el dominio pidió parar: no insistir
            seguidas += 1
            continue
        cuerpo = decodificar_html(resp.cuerpo, resp.tipo)
        if es_desafio_antibot(cuerpo):
            antibot_seguidos += 1
            falla(fallas, fid, n["link"], "desafío anti-bot en la nota", "nota")
            if antibot_seguidos >= 2:
                bloquear_dominio(dom_nota, "anti-bot en notas")
                break
            seguidas += 1
            continue
        antibot_seguidos = 0
        texto = extraer_texto(cuerpo)
        if texto:
            n["texto_recortado"] = len(texto) > MAX_TEXTO
            n["texto"] = texto[:MAX_TEXTO]
            n["metodo"] = "texto_completo"
            n["temas"] = detectar_temas(n)
            bajadas += 1
            seguidas = 0
        else:
            seguidas += 1
            falla(fallas, fid, n["link"], "sin texto extraíble", "nota")


# ------------------------------------------------------------------- APIs


def bajar_json(url):
    resp = descargar(url)
    if resp.codigo != 200:
        raise RuntimeError(resp.motivo)
    return json.loads(resp.cuerpo.decode("utf-8", "ignore"))


def dias_atraso(fecha_iso):
    try:
        f = dt.datetime.fromisoformat(re.sub(r"[Zz]$", "+00:00", fecha_iso))
        if f.tzinfo is None:
            f = f.replace(tzinfo=ZONA_AR)
        return (ahora_ar().date() - f.astimezone(ZONA_AR).date()).days
    except Exception:
        return None


def bajar_datos(apis, fallas):
    datos = {"dolar": None, "riesgo_pais": None, "bcra": None, "fuentes": {}, "consultado": ahora_ar().isoformat(timespec="seconds")}
    url = apis.get("dolar")
    if url:
        datos["fuentes"]["dolar"] = url
        try:
            lst = bajar_json(url)
            datos["dolar"] = {}
            for d in lst:
                if "casa" not in d:
                    continue
                datos["dolar"][d["casa"]] = {"nombre": d.get("nombre"), "compra": d.get("compra"), "venta": d.get("venta"),
                                             "actualizado": d.get("fechaActualizacion"),
                                             "actualizado_ar": fecha_local(d.get("fechaActualizacion") or ""),
                                             "dias_atraso": dias_atraso(d.get("fechaActualizacion") or "")}
        except Exception as e:
            falla(fallas, "api_dolar", url, str(e)[:120], "datos")
    url = apis.get("riesgo_pais")
    if url:
        datos["fuentes"]["riesgo_pais"] = url
        try:
            d = bajar_json(url)
            datos["riesgo_pais"] = {"valor": d.get("valor"), "fecha": d.get("fecha"),
                                    "dias_atraso": dias_atraso(d.get("fecha") or "")}
        except Exception as e:
            falla(fallas, "api_riesgo_pais", url, str(e)[:120], "datos")
    bcra = apis.get("bcra") or {}
    base = bcra.get("base_serie")
    if base:
        datos["fuentes"]["bcra"] = base
        datos["bcra"] = {"series": {}}
        for clave, info in (bcra.get("variables") or {}).items():
            if tiempo_restante() < 15:
                falla(fallas, "api_bcra", base, "sin tiempo", "datos")
                break
            url = f"{base}/{info['id']}?limit=2"
            try:
                d = bajar_json(url)
                det = d["results"][0]["detalle"]
                datos["bcra"]["series"][clave] = {"id": info["id"], "descripcion": info.get("descripcion"),
                                                  "fecha": det[0]["fecha"], "valor": det[0]["valor"],
                                                  "dias_atraso": dias_atraso(det[0]["fecha"]),
                                                  "anterior": det[1] if len(det) > 1 else None}
            except Exception as e:
                falla(fallas, "api_bcra", url, f"{clave}: {str(e)[:100]}", "datos")
    return datos


# ----------------------------------------------------------------- salida


def escribir_resumen(path, registro, resultado, fallas_api, duracion, ventana):
    lineas = ["# Resumen de ingesta", "",
              f"- Generado: {ahora_ar().isoformat(timespec='seconds')} (hora AR)",
              f"- Ventana: últimas {ventana} h",
              f"- Duración: {duracion:.1f} s", "",
              "| Fuente | Acceso | Feeds OK / total | Ítems en ventana | Con texto | Fallas |",
              "|---|---|---|---|---|---|"]
    total_fallas = []
    for f in registro:
        nombre = f.get("nombre", f["id"])
        r = resultado.get(f["id"])
        if r is None:
            lineas.append(f"| {nombre} | {f.get('acceso')} | 0 / {len(f.get('feeds', []))} | 0 | 0 | cortada por tiempo |")
            total_fallas.append({"fuente": f["id"], "url": "", "motivo": "fuente cortada por tiempo", "tipo": "feed"})
            continue
        con_texto = sum(1 for n in r["notas"] if n["texto"])
        lineas.append(f"| {nombre} | {f.get('acceso')} | {len(r['feeds_ok'])} / {len(f.get('feeds', []))} | "
                      f"{len(r['notas'])} | {con_texto} | {len(r['fallas'])} |")
        total_fallas += r["fallas"]
    total_fallas += fallas_api
    lineas += ["", f"## Fallas ({len(total_fallas)})", ""]
    for x in total_fallas:
        lineas.append(f"- **{x['fuente']}** {x.get('url', '')} — {x['motivo']}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lineas) + "\n")


def linea_indice(n, nombres, con_seccion=False):
    marca = ""
    if n.get("texto"):
        marca = " · TEXTO(parcial)" if n.get("texto_recortado") else " · TEXTO"
    temas = (" · #" + " #".join(n["temas"])) if n.get("temas") else ""
    sec = f"[{n['seccion_informe']}] " if con_seccion else ""
    tambien = (" · también en: " + ", ".join(n["tambien_en"])) if n.get("tambien_en") else ""
    res = (n.get("resumen") or "")[:160]
    return (f"- {n['id']} · {nombres.get(n['fuente'], n['fuente'])} [{n['idioma']}] · {fecha_local(n['fecha'])} · "
            f"{sec}{n['titulo']} — {res}{marca}{temas}{tambien} · {n['link']}")


def escribir_indice_y_textos(dir_base, salida, registro):
    """indice/<seccion>.md, indice/tema_<tema>.md, indice/fuentes.md, indice/_resumen.md y textos/<id>.md."""
    notas, datos, fallas = salida["notas"], salida["datos"], salida["fallas"]
    dir_ind = os.path.join(dir_base, "indice")
    dir_txt = os.path.join(dir_base, "textos")
    shutil.rmtree(dir_ind, ignore_errors=True)
    shutil.rmtree(dir_txt, ignore_errors=True)
    os.makedirs(dir_ind, exist_ok=True)
    os.makedirs(dir_txt, exist_ok=True)
    nombres = {f["id"]: f.get("nombre", f["id"]) for f in registro}
    cab = (f"Hoy: {salida['dia_semana']} {salida['fecha_informe']} · ventana desde {fecha_local(salida['desde'])} (hora AR). "
           "La línea editorial de cada fuente está en indice/fuentes.md. Formato: id · fuente · fecha · título — resumen "
           "· TEXTO si hay texto completo en textos/<id>.md · #temas · también en (otros medios con la misma nota) · link")
    por_seccion = {s: [] for s in ORDEN_SECCIONES}
    for n in notas:
        por_seccion.setdefault(n.get("seccion_informe", "general"), []).append(n)
    conteo = {}
    for sec in ORDEN_SECCIONES:
        lst = por_seccion.get(sec, [])
        if sec == "deportes":
            lst.sort(key=lambda n: (0 if "velez" in n["temas"] else 1, -dt.datetime.fromisoformat(n["fecha"]).timestamp()))
        else:
            lst.sort(key=lambda n: -dt.datetime.fromisoformat(n["fecha"]).timestamp())
        conteo[sec] = len(lst)
        lineas = [f"# Índice: {sec} ({len(lst)} notas)", "", cab, ""] + [linea_indice(n, nombres) for n in lst]
        with open(os.path.join(dir_ind, f"{sec}.md"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lineas) + "\n")
    for tema, _ in TEMAS:
        lst = [n for n in notas if tema in n["temas"]]
        lst.sort(key=lambda n: -dt.datetime.fromisoformat(n["fecha"]).timestamp())
        lineas = [f"# Tema: {tema} ({len(lst)} notas)", "", cab, ""] + [linea_indice(n, nombres, True) for n in lst]
        with open(os.path.join(dir_ind, f"tema_{tema}.md"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lineas) + "\n")
    # Fuentes y líneas editoriales
    lineas = ["# Fuentes de esta corrida", "", "| id | Medio | Idioma | Acceso | Línea editorial |", "|---|---|---|---|---|"]
    for f in registro:
        lineas.append(f"| {f['id']} | {f.get('nombre', f['id'])} | {f.get('idioma', 'es')} | {f.get('acceso')} | {f.get('linea_editorial', '')} |")
    with open(os.path.join(dir_ind, "fuentes.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lineas) + "\n")
    # Textos completos
    for n in notas:
        if not n.get("texto"):
            continue
        pie = "\n\n[texto recortado a 6.000 caracteres; la nota completa está en el link]" if n.get("texto_recortado") else ""
        with open(os.path.join(dir_txt, f"{n['id']}.md"), "w", encoding="utf-8") as fh:
            fh.write(f"# {n['titulo']}\n\n{nombres.get(n['fuente'], n['fuente'])} · {n['linea_editorial']} · "
                     f"{fecha_local(n['fecha'])} (hora AR) · {n['link']}\n\n{n['texto']}{pie}\n")
    # Resumen para el redactor
    por_fuente = {}
    for n in notas:
        c = por_fuente.setdefault(n["fuente"], [0, 0])
        c[0] += 1
        if n.get("texto"):
            c[1] += 1
    L = ["# Resumen de la ingesta para el redactor", "",
         f"- Hoy: {salida['dia_semana']} {salida['fecha_informe']} (hora AR). Generado {salida['generado']}. "
         f"Ventana: {salida['ventana_horas']} h, desde {fecha_local(salida['desde'])}.",
         f"- Total: {len(notas)} notas de {len(salida['fuentes_ok'])} medios; {sum(1 for n in notas if n.get('texto'))} con texto completo.",
         "", "## Medios que no se pudieron leer", ""]
    if salida["fuentes_sin_lectura"]:
        for x in salida["fuentes_sin_lectura"]:
            L.append(f"- {x['nombre']}: {x['motivo']}")
    else:
        L.append("- ninguno")
    L += ["", "## Medios con lectura parcial (algún feed falló)", ""]
    if salida["fuentes_parciales"]:
        for x in salida["fuentes_parciales"]:
            L.append(f"- {x['nombre']}: {x['feeds_ok']}/{x['feeds_total']} feeds; {x['motivo']}")
    else:
        L.append("- ninguno")
    avisos = salida.get("avisos") or []
    L += ["", "## Avisos de robots.txt y acceso", ""]
    L += [f"- {a}" for a in avisos] if avisos else ["- ninguno"]
    L += ["", "## Datos duros (bloque `datos` de notas.json)", ""]
    motivos = {x["fuente"]: x["motivo"] for x in fallas if x.get("tipo") == "datos"}
    dol = (datos or {}).get("dolar")
    if dol:
        for casa in ("oficial", "mayorista", "bolsa", "contadoconliqui", "blue", "tarjeta", "cripto"):
            d = dol.get(casa)
            if d:
                atraso = f", hace {d['dias_atraso']} días" if d.get("dias_atraso") else ""
                L.append(f"- Dólar {d.get('nombre') or casa}: compra {d.get('compra')} / venta {d.get('venta')} "
                         f"(actualizado {d.get('actualizado_ar')} hora AR{atraso})")
    else:
        L.append(f"- Dólar: NO DISPONIBLE ({motivos.get('api_dolar', 'sin motivo registrado')})")
    rp = (datos or {}).get("riesgo_pais")
    if rp:
        atraso = f", hace {rp['dias_atraso']} días" if rp.get("dias_atraso") else ""
        L.append(f"- Riesgo país: {rp.get('valor')} pb (fecha {rp.get('fecha')}{atraso})")
    else:
        L.append(f"- Riesgo país: NO DISPONIBLE ({motivos.get('api_riesgo_pais', 'sin motivo registrado')})")
    bc = ((datos or {}).get("bcra") or {}).get("series") or {}
    if bc:
        for k, v in bc.items():
            ant = v.get("anterior") or {}
            ant_txt = f"; anterior {ant.get('valor')} ({ant.get('fecha')})" if ant else ""
            atraso = f", hace {v['dias_atraso']} días" if v.get("dias_atraso") else ""
            L.append(f"- BCRA {v.get('descripcion') or k}: {v.get('valor')} (fecha {v.get('fecha')}{atraso}{ant_txt})")
    else:
        L.append(f"- BCRA: NO DISPONIBLE ({motivos.get('api_bcra', 'sin motivo registrado')})")
    L += ["", "## Notas por sección del informe", ""]
    for sec in ORDEN_SECCIONES:
        L.append(f"- {sec}: {conteo.get(sec, 0)} notas → indice/{sec}.md")
    L += ["", "## Notas por tema", ""]
    for tema, _ in TEMAS:
        L.append(f"- {tema}: {sum(1 for n in notas if tema in n['temas'])} notas → indice/tema_{tema}.md")
    L += ["", "## Notas por fuente (total / con texto completo)", ""]
    for f in registro:
        c = por_fuente.get(f["id"], [0, 0])
        L.append(f"- {f.get('nombre', f['id'])} [{f.get('acceso')}] ({f.get('linea_editorial', '')}): {c[0]} / {c[1]}")
    L += ["", f"## Fallas ({len(fallas)})", ""]
    for x in fallas[:80]:
        L.append(f"- {x.get('fuente')} [{x.get('tipo', '')}] {x.get('url', '')} — {x.get('motivo')}")
    if len(fallas) > 80:
        L.append(f"- … y {len(fallas) - 80} más (ver notas.json)")
    with open(os.path.join(dir_ind, "_resumen.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


def marcar_repetidas(notas, nombres):
    """Misma noticia (título normalizado) en varias fuentes → tambien_en."""
    grupos = {}
    for n in notas:
        grupos.setdefault(titulo_normalizado(n["titulo"]), []).append(n)
    for lst in grupos.values():
        if len(lst) < 2:
            continue
        for n in lst:
            otros = sorted({nombres.get(m["fuente"], m["fuente"]) for m in lst if m["fuente"] != n["fuente"]})
            if otros:
                n["tambien_en"] = otros


def cargar_registro(path):
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    registro = []
    for f in cfg.get("fuentes", []):
        if not f.get("id") or not f.get("feeds"):
            log(f"fuente ignorada (sin id o sin feeds): {str(f)[:80]}")
            continue
        if f.get("acceso") not in ("texto_completo", "solo_rss"):
            continue
        registro.append(f)
    for e in cfg.get("excluidas", []):
        d = e.get("dominio")
        if d:
            DENEGADOS.add(dominio_base(d))
    return cfg, registro


def main():
    ap = argparse.ArgumentParser(description="Ingesta de feeds y datos para el informe diario")
    ap.add_argument("--fuentes", default="fuentes.json")
    ap.add_argument("--horas", type=float, default=None, help="ventana en horas (default: 36; lunes 72)")
    ap.add_argument("--salida", default="notas.json")
    ap.add_argument("--resumen", default=None, help="ruta del resumen Markdown (default: resumen_ingesta.md junto a la salida)")
    args = ap.parse_args()

    if feedparser is None or trafilatura is None:
        log("aviso: " + ", ".join(x for x, m in (("feedparser", feedparser), ("trafilatura", trafilatura)) if m is None)
            + " no instalado; se usa la biblioteca estándar")
    hoy = ahora_ar()
    horas = args.horas
    if horas is None:
        horas = 72 if hoy.weekday() == 0 else 36  # lunes: cubre el fin de semana
    desde = ahora_utc() - dt.timedelta(hours=horas)
    hasta = ahora_utc() + dt.timedelta(hours=2)  # fechas futuras: zona mal declarada o nota programada

    try:
        cfg, registro = cargar_registro(args.fuentes)
    except Exception as e:
        print(f"error: no se pudo leer {args.fuentes}: {e}", file=sys.stderr)
        sys.exit(2)
    if not registro:
        print("error: fuentes.json no tiene fuentes activas", file=sys.stderr)
        sys.exit(2)
    dir_salida = os.path.dirname(os.path.abspath(args.salida))
    os.makedirs(dir_salida, exist_ok=True)
    apis = cfg.get("apis", {})
    log(f"{len(registro)} fuentes, ventana {horas} h, {len(DENEGADOS)} dominios excluidos")

    resultado = {}
    fallas_api = []
    codigo_salida = 0
    try:
        ex = ThreadPoolExecutor(max_workers=len(registro) + 1)
        fut_api = ex.submit(bajar_datos, apis, fallas_api)  # primero: los datos duros no esperan a los feeds
        futuros = {ex.submit(procesar_fuente, f, desde, hasta, resultado): f for f in registro}
        pendientes = set(futuros) | {fut_api}
        while pendientes and tiempo_restante() > 5:
            hechos, pendientes = wait(pendientes, timeout=min(5.0, max(0.1, tiempo_restante() - 5)),
                                      return_when=FIRST_COMPLETED)
            for h in hechos:
                if h in futuros and h.exception():
                    f = futuros[h]
                    r = resultado.setdefault(f["id"], {"notas": [], "fallas": [], "feeds_ok": [], "avisos": []})
                    r["fallas"].append({"fuente": f["id"], "url": "", "motivo": f"error interno: {str(h.exception())[:120]}", "tipo": "feed"})
        datos = {"dolar": None, "riesgo_pais": None, "bcra": None, "fuentes": {}}
        if fut_api.done() and not fut_api.exception():
            datos = fut_api.result()
        elif fut_api.done():
            fallas_api.append({"fuente": "apis", "url": "", "motivo": f"error interno: {str(fut_api.exception())[:120]}", "tipo": "datos"})
        else:
            fallas_api.append({"fuente": "apis", "url": "", "motivo": "cortadas por tiempo", "tipo": "datos"})
        for h, f in futuros.items():
            if not h.done():
                r = resultado.setdefault(f["id"], {"notas": [], "fallas": [], "feeds_ok": [], "avisos": []})
                r["fallas"].append({"fuente": f["id"], "url": "", "motivo": "fuente cortada por tiempo (se conserva lo bajado)", "tipo": "feed"})
        ex.shutdown(wait=False, cancel_futures=True)

        nombres = {f["id"]: f.get("nombre", f["id"]) for f in registro}
        orden = {f["id"]: i for i, f in enumerate(registro)}
        notas, fallas, fuentes_ok, avisos = [], list(fallas_api), [], []
        fuentes_sin_lectura, fuentes_parciales = [], []
        for f in registro:
            r = resultado.get(f["id"])
            if r is None:
                fuentes_sin_lectura.append({"id": f["id"], "nombre": nombres[f["id"]], "motivo": "cortada por tiempo antes de empezar"})
                continue
            notas += list(r["notas"])
            fallas += list(r["fallas"])
            avisos += [f"{nombres[f['id']]}: {a}" for a in r.get("avisos", [])]
            total_feeds = len(f.get("feeds", []))
            if r["feeds_ok"]:
                fuentes_ok.append(f["id"])
                if len(r["feeds_ok"]) < total_feeds:
                    caidos = [x for x in r["fallas"] if x.get("tipo") == "feed"]
                    fuentes_parciales.append({"id": f["id"], "nombre": nombres[f["id"]], "feeds_ok": len(r["feeds_ok"]),
                                              "feeds_total": total_feeds,
                                              "motivo": "; ".join(x["motivo"] for x in caidos[:3]) or "feed sin ítems"})
            else:
                primera = next((x["motivo"] for x in r["fallas"]), "sin feeds válidos")
                fuentes_sin_lectura.append({"id": f["id"], "nombre": nombres[f["id"]], "motivo": primera})
        for n in notas:
            n.pop("prioridad", None)
            n["seccion_informe"] = SECCION_INFORME.get(n.get("seccion", ""), "general")
            n["temas"] = detectar_temas(n)
        marcar_repetidas(notas, nombres)
        notas.sort(key=lambda n: (orden.get(n["fuente"], 999), n["fecha"]))
        salida = {
            "generado": ahora_ar().isoformat(timespec="seconds"),
            "fecha_informe": hoy.strftime("%Y-%m-%d"),
            "dia_semana": DIAS[hoy.weekday()],
            "ventana_horas": horas,
            "desde": desde.astimezone(ZONA_AR).isoformat(timespec="seconds"),
            "datos": datos,
            "fuentes_ok": sorted(fuentes_ok, key=lambda x: orden.get(x, 999)),
            "fuentes_sin_lectura": fuentes_sin_lectura,
            "fuentes_parciales": fuentes_parciales,
            "avisos": avisos,
            "fallas": fallas,
            "fallas_feeds": [x for x in fallas if x.get("tipo") in ("feed", "robots")],
            "fallas_notas": [x for x in fallas if x.get("tipo") == "nota"],
            "fallas_datos": [x for x in fallas if x.get("tipo") == "datos"],
            "notas": notas,
        }
        with open(args.salida, "w", encoding="utf-8") as fh:
            json.dump(salida, fh, ensure_ascii=False, indent=1)
        duracion = time.monotonic() - INICIO
        resumen = args.resumen or os.path.join(dir_salida, "resumen_ingesta.md")
        try:
            escribir_resumen(resumen, registro, resultado, fallas_api, duracion, horas)
        except Exception as e:
            log(f"no se pudo escribir el resumen: {str(e)[:120]}")
        try:
            escribir_indice_y_textos(dir_salida, salida, registro)
        except Exception as e:  # el índice es una comodidad: nunca debe perder notas.json
            log(f"no se pudo escribir el índice: {str(e)[:120]}")
        log(f"listo: {len(notas)} notas ({sum(1 for n in notas if n['texto'])} con texto), {len(fallas)} fallas, "
            f"{duracion:.1f}s -> {args.salida}")
    except SystemExit:
        raise
    except Exception as e:
        codigo_salida = 1
        log(f"error fatal: {type(e).__name__}: {str(e)[:200]}")
    finally:
        # Si quedó algún hilo colgado en una descarga, no esperarlo: el intérprete haría join al salir.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(codigo_salida)


if __name__ == "__main__":
    main()
