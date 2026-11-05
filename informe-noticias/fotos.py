#!/usr/bin/env python3
"""Fotos de las fichas del informe diario de Buttom.

Baja la imagen de las notas que el informe usa como fuente principal de cada ficha (solo esas, nunca más),
para mostrarla en la página con el medio en el pie. La dirección de cada imagen la guardó la ingesta en
notas.json (campo "imagen": la que el medio publica en su feed o en las etiquetas og:image / twitter:image
de la nota). El efecto vintage (blanco y negro, contraste y grano) lo pone el CSS de la página: acá no se
toca la imagen.

Reglas de red: las mismas de tendencias.py, que se reutiliza (user-agent honesto, robots.txt según el RFC 9309
y cerrado ante errores, 1 pedido por segundo por dominio, redirecciones revisadas, límite de tiempo y de
tamaño por pedido y presupuesto de tiempo para toda la corrida), más las de ingestar.py:
  - solo se piden imágenes del propio medio de la nota (su dominio o los "dominios_imagen" que declara en
    fuentes.json) y solo de fuentes con acceso "texto_completo". Todo pedido (también los saltos de una
    redirección y la lectura de robots.txt) pasa por ese control, y nunca se toca un dominio excluido;
  - si el robots.txt del dominio bloquea agentes de IA, no se pide nada; se respeta su Crawl-delay (si pide
    más de 60 s, no se pide);
  - sin fuentes.json legible (con su lista "excluidas") no se baja ninguna foto.
Nunca falla: si una foto no se puede bajar, queda anotada en "fallas" y la ficha va sin foto.

Uso:
  python3 -I fotos.py --notas /tmp/ingesta/notas.json --ids id1,id2,... --dir CARPETA/fotos/2026-11-05 \
      --prefijo fotos/2026-11-05/ --manifest CARPETA/fotos/manifest.json [--fuentes fuentes.json]
"""
import argparse, datetime as dt, importlib.util, json, os, re, struct, sys, time, urllib.error, urllib.parse

MAX_FOTOS = 40                # tope por corrida
MAX_BYTES_FOTO = 1_500_000    # tope de tamaño por imagen
MAX_BYTES_TOTAL = 40_000_000  # tope para todas las fotos de la corrida (la página se publica con ellas)
MIN_BYTES_FOTO = 2_000        # menos que esto suele ser un píxel de seguimiento o un ícono
MIN_ANCHO, MAX_LADO = 300, 10_000
MIN_ALTO = 150
MIN_PROPORCION, MAX_PROPORCION = 0.4, 3.5   # ancho / alto: fuera de esto son banners o tiras, no fotos
LIMITE_RED_FOTOS = 300        # segundos de red para toda la corrida
ACEPTA = "image/webp,image/jpeg,image/png;q=0.9,image/gif;q=0.5"
AQUI = os.path.dirname(os.path.abspath(__file__))


def _cargar(nombre, archivo):
    """Carga un módulo hermano por ruta (con python3 -I el directorio del script no está en sys.path)."""
    spec = importlib.util.spec_from_file_location(nombre, os.path.join(AQUI, archivo))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Red:
    """La capa de red de tendencias.py con los controles propios de las fotos:
    - `permitidos`: dominios base a los que se puede pedir durante la foto en curso (el medio de la nota);
      todo pedido a otro dominio, o a uno excluido, se corta antes de salir;
    - robots.txt: además de lo que ya aplica tendencias.py, se niega si bloquea agentes de IA y se respeta
      su Crawl-delay (con el mismo parser y el mismo tope de 60 s que ingestar.py)."""

    def __init__(self, excluidos):
        self.mod = mod = _cargar("tendencias_red", "tendencias.py")
        self.ing = _cargar("ingestar_reglas", "ingestar.py")
        mod.LIMITE_RED = LIMITE_RED_FOTOS
        mod.MAX_BYTES = MAX_BYTES_FOTO
        self.excluidos = set(excluidos)
        self.permitidos = set()
        self.demoras = {}       # host -> segundos entre pedidos que pide su robots.txt
        self.politica = {}      # base (esquema://host) -> None si se puede, o (estado, detalle)

        original_pedido, original_motivo, red = mod._pedido_unico, mod.motivo_bloqueo, self

        class _RobotsConTexto(mod._Robots):
            def __init__(self, texto):
                super().__init__(texto)
                self.texto = texto

        def pedido_controlado(req):
            motivo = red.fuera_de_lugar(req.full_url)
            if motivo:
                raise ValueError(motivo)
            return original_pedido(req)

        def motivo_controlado(url):
            fuera = red.fuera_de_lugar(url)
            if fuera:
                return "dominio no permitido", fuera
            motivo = original_motivo(url)
            return motivo or red._politica(url)

        def esperar(host):
            demora = max(1.0, red.demoras.get(host, 1.0))
            antes = mod._ultimo.get(host)
            if antes is not None:
                falta = demora - (time.monotonic() - antes)
                if falta > 0:
                    if falta >= mod._restante():
                        raise mod.TiempoAgotado(f"el Crawl-delay de {host} no entra en el tiempo de red que queda")
                    time.sleep(falta)
            mod._ultimo[host] = time.monotonic()

        mod._Robots = _RobotsConTexto
        mod._pedido_unico = pedido_controlado
        mod.motivo_bloqueo = motivo_controlado
        mod._esperar = esperar

    def fuera_de_lugar(self, url):
        """None si url está en un dominio permitido para la foto en curso; si no, el motivo."""
        host = host_de(url)
        base = dominio_base(host)
        if not host or base in self.excluidos:
            return f"{host or url[:80]} es un medio excluido por política o no tiene dominio"
        if base not in self.permitidos:
            return f"{host} no es un dominio del medio de la nota"
        return None

    def _politica(self, url):
        """Bloqueo de agentes de IA y Crawl-delay del robots.txt ya leído para url (None si se puede pedir)."""
        base = self.mod._base(url)
        if base in self.politica:
            return self.politica[base]
        r = self.mod._robots.get(base)
        motivo = None
        texto = getattr(r, "texto", None)
        if texto is None:
            if r is not None:   # robots.txt leído pero sin texto guardado: no se sabe qué pide, no se pide
                motivo = ("robots.txt sin leer", "no se pudo revisar el robots.txt")
        else:
            reglas = self.ing.ReglasRobots(texto.lstrip("\ufeff"))
            ia = reglas.bloquea_ia()
            demora = reglas.crawl_delay(self.mod.UA)
            if ia:
                motivo = ("robots bloquea IA", f"robots.txt de {base} bloquea agentes de IA ({', '.join(ia)})")
            elif demora and demora > self.ing.MAX_CRAWL_DELAY:
                motivo = ("crawl-delay", f"robots.txt de {base} pide {demora:.0f} s entre pedidos")
            elif demora:
                self.demoras[urllib.parse.urlsplit(url).netloc.lower()] = float(demora)
        self.politica[base] = motivo
        return motivo

    def motivo_bloqueo(self, url):
        return self.mod.motivo_bloqueo(url)

    def get(self, url):
        return self.mod._get(url, accept=ACEPTA)


# ---------- Formato y tamaño de la imagen (solo cabeceras, sin bibliotecas externas) ----------
def formato(b):
    if b[:3] == b"\xff\xd8\xff":
        return "jpg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def medidas(b, fmt):
    """(ancho, alto) leídos de la cabecera, o (None, None) si no se pudieron leer."""
    try:
        if fmt == "png" and len(b) >= 24:
            return struct.unpack(">II", b[16:24])
        if fmt == "gif" and len(b) >= 10:
            return struct.unpack("<HH", b[6:10])
        if fmt == "webp" and len(b) >= 30:
            trozo = b[12:16]
            if trozo == b"VP8 ":
                ancho, alto = struct.unpack("<HH", b[26:30])
                return ancho & 0x3FFF, alto & 0x3FFF
            if trozo == b"VP8L":
                bits = int.from_bytes(b[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if trozo == b"VP8X":
                return int.from_bytes(b[24:27], "little") + 1, int.from_bytes(b[27:30], "little") + 1
        if fmt == "jpg":
            i = 2
            while i + 9 < len(b):
                if b[i] != 0xFF:
                    i += 1
                    continue
                while i + 1 < len(b) and b[i + 1] == 0xFF:   # bytes de relleno 0xFF antes de la marca
                    i += 1
                if i + 9 >= len(b):
                    break
                marca = b[i + 1]
                if marca in (0xD8, 0x01) or 0xD0 <= marca <= 0xD7:   # sin longitud
                    i += 2
                    continue
                largo = struct.unpack(">H", b[i + 2:i + 4])[0]
                if marca in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    alto, ancho = struct.unpack(">HH", b[i + 5:i + 9])
                    return ancho, alto
                if largo < 2:
                    break
                i += 2 + largo
    except Exception:
        pass
    return None, None


# ---------- Datos de entrada ----------
def leer_notas(ruta):
    try:
        d = json.load(open(ruta, encoding="utf-8-sig"))
    except Exception as e:
        print(f"[aviso] no pude leer {ruta}: {type(e).__name__}", file=sys.stderr)
        return {}
    notas = d.get("notas") if isinstance(d, dict) else d
    if not isinstance(notas, list):
        return {}
    return {n["id"]: n for n in notas if isinstance(n, dict) and isinstance(n.get("id"), str)}


def host_de(url):
    """Nombre del servidor de url, sin usuario, puerto ni punto final ("" si no se puede leer)."""
    try:
        return (urllib.parse.urlsplit(url).hostname or "").rstrip(".").lower()
    except Exception:
        return ""


def dominio_base(netloc):
    """www.clarin.com -> clarin.com; www.lanacion.com.ar -> lanacion.com.ar (la misma regla que ingestar.py)."""
    h = (netloc or "").lower().split(":")[0].rstrip(".")
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", h) or h in ("localhost", ""):
        return h
    partes = h.split(".")
    if len(partes) >= 3 and partes[-2] in ("com", "org", "gob", "gov", "net", "edu", "co") and len(partes[-1]) == 2:
        return ".".join(partes[-3:])
    return ".".join(partes[-2:]) if len(partes) >= 2 else h


def leer_fuentes(ruta):
    """(id de fuente -> datos de la fuente, dominios base excluidos) desde fuentes.json.
    Levanta ValueError si el archivo no se puede leer o no trae las listas "fuentes" y "excluidas": sin la
    lista de exclusiones no se baja ninguna foto."""
    try:
        d = json.load(open(ruta, encoding="utf-8-sig"))
    except Exception as e:
        raise ValueError(f"no se pudo leer {ruta}: {type(e).__name__}")
    if not isinstance(d, dict) or not isinstance(d.get("fuentes"), list) or not isinstance(d.get("excluidas"), list):
        raise ValueError(f"{ruta} no trae las listas \"fuentes\" y \"excluidas\"")
    fuentes = {f["id"]: f for f in d["fuentes"] if isinstance(f, dict) and isinstance(f.get("id"), str)}
    excluidos = set()
    for e in d["excluidas"]:
        if not isinstance(e, dict):
            continue
        for dom in [e.get("dominio")] + list(e.get("dominios") or []):
            if isinstance(dom, str) and dom.strip():
                excluidos.add(dominio_base(dom.strip()))
    return fuentes, excluidos


def dominios_del_medio(fuente, excluidos):
    """Dominios base desde los que se acepta la foto de una nota de esta fuente (nunca uno excluido)."""
    doms = [fuente.get("dominio")] + list(fuente.get("dominios_imagen") or [])
    return {dominio_base(x) for x in doms if isinstance(x, str) and x.strip()} - set(excluidos) - {""}


def nombre_archivo(id_nota, fmt):
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", id_nota).strip("-")[:80] or "foto"
    return f"{base}.{fmt}"


# ---------- Bajada ----------
def revisar_imagen(cuerpo):
    """(formato, ancho, alto) si los bytes son una foto usable; si no, ValueError con el motivo."""
    fmt = formato(cuerpo)
    if not fmt:
        raise ValueError("lo que bajó no es una imagen JPEG, PNG, WebP o GIF")
    if len(cuerpo) < MIN_BYTES_FOTO:
        raise ValueError(f"imagen demasiado chica ({len(cuerpo)} bytes)")
    ancho, alto = medidas(cuerpo, fmt)
    if not ancho or not alto:
        raise ValueError("no se pudieron leer las medidas de la imagen")
    if ancho < MIN_ANCHO or alto < MIN_ALTO:
        raise ValueError(f"imagen demasiado chica ({ancho}×{alto} px)")
    if ancho > MAX_LADO or alto > MAX_LADO:
        raise ValueError(f"medidas fuera de rango ({ancho}×{alto} px)")
    if not MIN_PROPORCION <= ancho / alto <= MAX_PROPORCION:
        raise ValueError(f"proporción de banner, no de foto ({ancho}×{alto} px)")
    return fmt, ancho, alto


def bajar(red, nota, fuentes, excluidos):
    """Devuelve (bytes, formato, ancho, alto, nombre del medio) o levanta ValueError con el motivo."""
    fuente = fuentes.get(nota.get("fuente"))
    if not fuente:
        raise ValueError("la fuente de la nota no está en fuentes.json")
    if fuente.get("acceso") != "texto_completo":
        raise ValueError("la fuente se usa solo por su feed: sin foto")
    medio = fuente.get("nombre")
    if not isinstance(medio, str) or not medio.strip():
        raise ValueError("la fuente no tiene nombre para el pie de foto")
    url = nota.get("imagen")
    if not isinstance(url, str) or not url.strip().lower().startswith(("http://", "https://")):
        raise ValueError("la nota no tiene imagen")
    url = red.ing.url_para_pedir(url)
    red.permitidos = dominios_del_medio(fuente, excluidos)
    try:
        fuera = red.fuera_de_lugar(url)
        if fuera:
            raise ValueError(fuera)
        motivo = red.motivo_bloqueo(url)
        if motivo:
            raise ValueError(f"{motivo[0]}: {motivo[1]}")
        cuerpo, _tipo = red.get(url)
    finally:
        red.permitidos = set()
    fmt, ancho, alto = revisar_imagen(cuerpo)
    return cuerpo, fmt, ancho, alto, medio.strip()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--notas", required=True)
    ap.add_argument("--ids", required=True, help="ids de notas separados por coma, en el orden de las fichas")
    ap.add_argument("--dir", required=True, help="carpeta local donde se guardan las fotos")
    ap.add_argument("--prefijo", default="", help="prefijo de la ruta publicada, p. ej. fotos/2026-11-05/")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--fuentes", default=os.path.join(AQUI, "fuentes.json"))
    a = ap.parse_args(argv)

    res = {"generado": dt.datetime.now(dt.timezone(dt.timedelta(hours=-3))).isoformat(timespec="minutes"),
           "prefijo": a.prefijo, "fotos": [], "fallas": []}
    try:
        ids = []
        for i in (a.ids or "").split(","):
            i = i.strip()
            if i and i not in ids:
                ids.append(i)
        try:
            fuentes, excluidos = leer_fuentes(a.fuentes)
        except ValueError as e:   # cerrado ante errores: sin exclusiones confiables no se pide nada
            res["fallas"] += [{"id": i, "motivo": f"{e}; sin la lista de medios excluidos no se baja ninguna foto"}
                              for i in ids]
            ids = []
        if len(ids) > MAX_FOTOS:
            res["fallas"] += [{"id": i, "motivo": f"se pasó el tope de {MAX_FOTOS} fotos por corrida"} for i in ids[MAX_FOTOS:]]
            ids = ids[:MAX_FOTOS]
        notas = leer_notas(a.notas) if ids else {}
        if ids:
            os.makedirs(a.dir, exist_ok=True)
            red = Red(excluidos)
        total = 0
        for i in ids:
            nota = notas.get(i)
            if not nota:
                res["fallas"].append({"id": i, "motivo": "el id no está en notas.json"})
                continue
            if total >= MAX_BYTES_TOTAL:
                res["fallas"].append({"id": i, "motivo": f"se llegó al tope de {MAX_BYTES_TOTAL // 1_000_000} MB de fotos"})
                continue
            try:
                cuerpo, fmt, ancho, alto, medio = bajar(red, nota, fuentes, excluidos)
                if total + len(cuerpo) > MAX_BYTES_TOTAL:
                    raise ValueError(f"no entra en el tope de {MAX_BYTES_TOTAL // 1_000_000} MB de fotos")
                total += len(cuerpo)
                archivo = nombre_archivo(i, fmt)
                destino = os.path.join(a.dir, archivo)
                tmp = destino + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(cuerpo)
                os.replace(tmp, destino)
                res["fotos"].append({"id": i, "archivo": a.prefijo + archivo, "local": destino, "formato": fmt,
                                     "ancho": ancho, "alto": alto, "bytes": len(cuerpo),
                                     "fuente": nota.get("fuente"), "medio": medio,
                                     "link": nota.get("link"), "imagen": nota.get("imagen")})
            except (ValueError, urllib.error.URLError, OSError) as e:
                res["fallas"].append({"id": i, "motivo": str(e)[:200] or type(e).__name__})
            except Exception as e:   # RobotsBloquea, TiempoAgotado, IncompleteRead y cualquier otra cosa
                res["fallas"].append({"id": i, "motivo": f"{type(e).__name__}: {str(e)[:180]}"})
    except Exception as e:
        res["fallas"].append({"id": "", "motivo": f"error general: {type(e).__name__}: {str(e)[:180]}"})
    try:
        os.makedirs(os.path.dirname(os.path.abspath(a.manifest)), exist_ok=True)
        tmp = a.manifest + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
        os.replace(tmp, a.manifest)
    except Exception as e:
        print(f"[aviso] no pude escribir {a.manifest}: {e}", file=sys.stderr)
    print(f"fotos: {len(res['fotos'])} bajadas · {len(res['fallas'])} sin foto")
    return 0


if __name__ == "__main__":
    sys.exit(main())
