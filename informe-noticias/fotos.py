#!/usr/bin/env python3
"""Fotos de las fichas del informe diario de Buttom.

Baja la imagen de las notas que el informe usa como fuente principal de cada ficha (solo esas, nunca más),
para mostrarla en la página con el medio en el pie. La dirección de cada imagen la guardó la ingesta en
notas.json (campo "imagen": la que el medio publica en su feed o en las etiquetas og:image / twitter:image
de la nota). El efecto vintage (blanco y negro, contraste y grano) lo pone el CSS de la página: acá no se
toca la imagen.

Reglas de red: las mismas de tendencias.py, que se reutiliza (user-agent honesto, robots.txt según el RFC 9309
y cerrado ante errores, 1 pedido por segundo por dominio, redirecciones revisadas, límite de tiempo y de
tamaño por pedido y presupuesto de tiempo para toda la corrida). Nunca falla: si una foto no se puede bajar,
queda anotada en "fallas" y la ficha va sin foto.

Uso:
  python3 -I fotos.py --notas /tmp/ingesta/notas.json --ids id1,id2,... --dir /tmp/fotos \
      --prefijo fotos/2026-11-05/ --manifest /tmp/fotos/manifest.json [--fuentes fuentes.json]
"""
import argparse, datetime as dt, importlib.util, json, os, re, struct, sys, urllib.error, urllib.parse

MAX_FOTOS = 40              # tope por corrida
MAX_BYTES_FOTO = 3_000_000  # tope de tamaño por imagen
MIN_BYTES_FOTO = 2_000      # menos que esto suele ser un píxel de seguimiento o un ícono
MIN_ANCHO = 300             # imágenes más angostas no sirven como foto de ficha
LIMITE_RED_FOTOS = 300      # segundos de red para toda la corrida
ACEPTA = "image/webp,image/jpeg,image/png;q=0.9,image/gif;q=0.5"


def _red():
    """Carga tendencias.py (mismo directorio) para reutilizar su capa de red. Con python3 -I el directorio
    del script no está en sys.path, por eso se carga por ruta."""
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tendencias.py")
    spec = importlib.util.spec_from_file_location("tendencias_red", ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.LIMITE_RED = LIMITE_RED_FOTOS
    mod.MAX_BYTES = MAX_BYTES_FOTO
    return mod


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


def dominio_base(netloc):
    """www.clarin.com -> clarin.com; www.lanacion.com.ar -> lanacion.com.ar (la misma regla que ingestar.py)."""
    h = (netloc or "").lower().split(":")[0]
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", h) or h in ("localhost", ""):
        return h
    partes = h.split(".")
    if len(partes) >= 3 and partes[-2] in ("com", "org", "gob", "gov", "net", "edu", "co") and len(partes[-1]) == 2:
        return ".".join(partes[-3:])
    return ".".join(partes[-2:]) if len(partes) >= 2 else h


def leer_fuentes(ruta):
    """(id de fuente -> nombre del medio, dominios base excluidos), desde fuentes.json (opcional)."""
    if not ruta:
        return {}, set()
    try:
        d = json.load(open(ruta, encoding="utf-8-sig"))
        fuentes = d.get("fuentes", []) if isinstance(d, dict) else d
        nombres = {f["id"]: f.get("nombre") or f["id"] for f in fuentes if isinstance(f, dict) and f.get("id")}
        excluidos = {dominio_base(e.get("dominio")) for e in (d.get("excluidas", []) if isinstance(d, dict) else [])
                     if isinstance(e, dict) and e.get("dominio")}
        return nombres, excluidos
    except Exception:
        return {}, set()


def nombre_archivo(id_nota, fmt):
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", id_nota).strip("-")[:80] or "foto"
    return f"{base}.{fmt}"


# ---------- Bajada ----------
def bajar(red, nota, excluidos=frozenset()):
    """Devuelve (bytes, formato, ancho, alto) o levanta ValueError con el motivo."""
    url = nota.get("imagen")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError("la nota no tiene imagen")
    if dominio_base(urllib.parse.urlsplit(url).netloc) in excluidos:
        raise ValueError("la imagen está en un medio excluido por política")
    motivo = red.motivo_bloqueo(url)
    if motivo:
        raise ValueError(f"{motivo[0]}: {motivo[1]}")
    cuerpo, _tipo = red._get(url, accept=ACEPTA)
    fmt = formato(cuerpo)
    if not fmt:
        raise ValueError("lo que bajó no es una imagen JPEG, PNG, WebP o GIF")
    if len(cuerpo) < MIN_BYTES_FOTO:
        raise ValueError(f"imagen demasiado chica ({len(cuerpo)} bytes)")
    ancho, alto = medidas(cuerpo, fmt)
    if ancho is not None and ancho < MIN_ANCHO:
        raise ValueError(f"imagen demasiado angosta ({ancho} px)")
    return cuerpo, fmt, ancho, alto


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--notas", required=True)
    ap.add_argument("--ids", required=True, help="ids de notas separados por coma, en el orden de las fichas")
    ap.add_argument("--dir", required=True, help="carpeta local donde se guardan las fotos")
    ap.add_argument("--prefijo", default="", help="prefijo de la ruta publicada, p. ej. fotos/2026-11-05/")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--fuentes", default="")
    a = ap.parse_args(argv)

    res = {"generado": dt.datetime.now(dt.timezone(dt.timedelta(hours=-3))).isoformat(timespec="minutes"),
           "prefijo": a.prefijo, "fotos": [], "fallas": []}
    try:
        notas = leer_notas(a.notas)
        nombres, excluidos = leer_fuentes(a.fuentes)
        ids = []
        for i in (a.ids or "").split(","):
            i = i.strip()
            if i and i not in ids:
                ids.append(i)
        if len(ids) > MAX_FOTOS:
            res["fallas"] += [{"id": i, "motivo": f"se pasó el tope de {MAX_FOTOS} fotos por corrida"} for i in ids[MAX_FOTOS:]]
            ids = ids[:MAX_FOTOS]
        os.makedirs(a.dir, exist_ok=True)
        red = _red()
        for i in ids:
            nota = notas.get(i)
            if not nota:
                res["fallas"].append({"id": i, "motivo": "el id no está en notas.json"})
                continue
            try:
                cuerpo, fmt, ancho, alto = bajar(red, nota, excluidos)
                archivo = nombre_archivo(i, fmt)
                destino = os.path.join(a.dir, archivo)
                tmp = destino + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(cuerpo)
                os.replace(tmp, destino)
                res["fotos"].append({"id": i, "archivo": a.prefijo + archivo, "local": destino, "formato": fmt,
                                     "ancho": ancho, "alto": alto, "bytes": len(cuerpo),
                                     "fuente": nota.get("fuente"), "medio": nombres.get(nota.get("fuente"), nota.get("fuente")),
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
