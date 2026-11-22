"""Descarga subtítulos en castellano (español de España) para los vídeos de una carpeta.

Uso:
    python -I bajar_subs.py <carpeta_o_video> [--force] [--solo-castellano] [--sin-ingles]
                            [--ingles] [--dry-run] [--max-videos N] [--max-descargas N]

El subtítulo principal se guarda como <video>.srt (mismo nombre que el vídeo), para que
cualquier reproductor lo cargue solo. Orden de preferencia: castellano > latino > inglés.
Nunca se sustituye un .srt por otro de un tipo peor.

Primero mira las pistas de texto incrustadas en el vídeo (MKV y MP4, sin ffmpeg); si no
hay castellano, busca en todos los proveedores útiles, descarga todos los candidatos en
español y elige solo los que son castellano de España (por vocabulario), del episodio
correcto y con una duración compatible con el vídeo. Las etiquetas de idioma de las webs
NO son fiables (se han visto latinos marcados como "es" y episodios mal etiquetados), por
eso se comprueba el texto.

Credenciales en %USERPROFILE%\\.subtitulos-esp\\credenciales.json (las dos son opcionales):
    {"opensubtitles": {"username": "...", "password": "..."},
     "opensubtitlescom": {"username": "...", "password": "..."}}
"opensubtitles" es la cuenta de opensubtitles.org; "opensubtitlescom", la de opensubtitles.com, que
solo se usa como último recurso (ver UltimoRecurso).
"""
import argparse
import contextlib
import itertools
import json
import logging
import re
import shutil
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

from babelfish import Language
from guessit import guessit
from subliminal import region, refine, scan_video
from subliminal.core import ProviderPool
from subliminal.score import compute_score, episode_scores, movie_scores
from subliminal.video import Episode

HOME = Path.home() / '.subtitulos-esp'
VIDEO_EXT = {'.mkv', '.mp4', '.avi', '.m4v', '.mov', '.wmv', '.ts'}
MKV_EXT = {'.mkv'}
MP4_EXT = {'.mp4', '.m4v', '.mov'}
# opensubtitlescom (opensubtitles.com, otra cuenta distinta de la de .org) no va aquí: tiene un cupo
# pequeño de descargas al día, así que solo se le pide como último recurso (ver UltimoRecurso).
# subtis (Argentina) y napiprojekt (polaco) no aportan castellano.
# podnapisi: el dominio dejó de resolver en 2026 (podnapisi.net sin registro A, www sin DNS).
# tvsubtitles: la búsqueda (search.php) responde 403 a programas. Además, ambos daban latino.
PROVIDERS = ['opensubtitles', 'addic7ed', 'gestdown', 'subtitulamos']
ULTIMO = 'opensubtitlescom'
# Límites para no saturar a los proveedores con búsquedas masivas.
MAX_VIDEOS = 40     # vídeos que buscan en internet por ejecución (--max-videos; 0 = sin límite)
MAX_DESCARGAS = 5   # candidatos que se descargan por vídeo (--max-descargas; 0 = sin límite)
PAUSA = 3           # segundos de espera entre un vídeo que busca en internet y el siguiente
# Límites de seguridad frente a vídeos o subtítulos corruptos o manipulados.
MAX_SRT = 2 * 1024 * 1024        # caracteres de un subtítulo (los normales tienen 20-200 mil)
MAX_ELEMENTO = 16 * 1024 * 1024  # bytes que se leen de golpe de un elemento MKV o muestra MP4
MAX_MOOV = 64 * 1024 * 1024      # tamaño máximo del índice ('moov') de un MP4
MAX_MUESTRAS = 1_000_000         # entradas de una tabla de muestras de una pista de subtítulos
# Caracteres de control (C0 y C1), de ancho cero y de dirección del texto: no aparecen en un
# subtítulo normal y pueden servir para engañar a reproductores, terminales o al agente.
CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]')
CONTROL_LINEA = re.compile(r'[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]')

ES_RE = re.compile(
    r'\b(vosotr[oa]s|vuestr[oa]s?|os|vale(?!\s+la\s+pena)|chaval\w*|leches|ordenador\w*|móvil\w*'
    r'|gilipollas|joder|mola|guay|tío|tía|\w+áis|\w+éis)\b',
    re.IGNORECASE,
)
LA_RE = re.compile(
    r'\b(ustedes|computadora\w*|celular\w*|carro|carros|enojad\w*|ahorita|platic\w*|chamac\w*'
    r'|rentar|nomás|recién|lindo|jalar)\b',
    re.IGNORECASE,
)
# Para saber si un .srt que ya existe está en inglés o en español.
EN_PALABRAS = re.compile(r"\b(the|you|and|what|this|that|is|are|it's|don't|i'm|with)\b", re.IGNORECASE)
SP_PALABRAS = re.compile(r'\b(que|qué|el|los|las|una|por|para|está|pero|con|es)\b', re.IGNORECASE)
# Nombres de pistas incrustadas: "... LA", "Spanish (Latin America)", "Español (España)"...
LATINO_NOMBRE = re.compile(r'\b(la|lat|latam|latino\w*|latinoam\w*|latin|419|mx|méxico|mexico)\b',
                           re.IGNORECASE)
ESPANA_NOMBRE = re.compile(r'\b(es|esp|españa|spain|castellano|castilian|european|eur)\b', re.IGNORECASE)

RANGO = {None: 0, 'otro': 0, 'ingles': 1, 'latino': 2, 'castellano': 3}
ETIQUETA = {'castellano': 'CASTELLANO', 'latino': 'LATINO', 'ingles': 'INGLÉS'}


class ArchivoInvalido(Exception):
    """Vídeo corrupto, cortado o manipulado: se dejan de leer sus pistas incrustadas."""


def limpiar_texto(texto):
    """Quita del subtítulo los caracteres de control, de ancho cero y de dirección del texto."""
    return CONTROL.sub('', texto)


def visible(valor):
    """Texto de terceros (nombres de pista, ids...) seguro para mostrarlo en una sola línea."""
    return CONTROL_LINEA.sub(' ', str(valor))


# Coincidencias de la puntuación de subliminal que indican que el subtítulo va sincronizado:
# el mismo archivo (hash) o el mismo release (grupo, plataforma, fps). El resto (source,
# resolución, códecs: 1-4 puntos) apenas dice nada de la sincronía.
SINCRONIA = ('hash', 'release_group', 'streaming_service', 'fps')


def sincronia(sub, video):
    """Parte de la puntuación de subliminal que indica sincronía con este vídeo."""
    pesos = episode_scores if isinstance(video, Episode) else movie_scores
    coincidencias = sub.get_matches(video)
    return sum(pesos[m] for m in SINCRONIA if m in coincidencias)


def mejores_candidatos(candidatos, video, maximo):
    """Los `maximo` candidatos que merece la pena descargar, alternando proveedores.

    Dentro de cada proveedor se ordenan por sincronía y puntuación, y se van tomando por
    rondas (el mejor de cada proveedor, luego el segundo...). Así un proveedor con muchos
    resultados empatados (OpenSubtitles) no deja fuera al mejor de otro (Addic7ed), y la
    carga se reparte entre servicios.
    """
    por_proveedor = {}
    for sub in sorted(candidatos, key=lambda s: (sincronia(s, video), compute_score(s, video)),
                      reverse=True):
        por_proveedor.setdefault(sub.provider_name, []).append(sub)
    rondas = itertools.zip_longest(*por_proveedor.values())
    return [sub for ronda in rondas for sub in ronda if sub is not None][:maximo]


def dialecto(texto):
    es = len(ES_RE.findall(texto))
    la = len(LA_RE.findall(texto))
    return es, la, es >= 3 and es > 2 * la


def idioma_srt(texto):
    """'castellano', 'latino', 'ingles', 'otro' o None (vacío)."""
    if not texto.strip():
        return None
    if dialecto(texto)[2]:
        return 'castellano'
    n_en, n_sp = len(EN_PALABRAS.findall(texto)), len(SP_PALABRAS.findall(texto))
    if n_sp > n_en:
        return 'latino'
    if n_en > n_sp:
        return 'ingles'
    return 'otro'


def ultimo_tiempo(texto):
    tiempos = re.findall(r'-->\s*(\d+):(\d+):(\d+)', texto)
    if not tiempos:
        return None
    h, m, s = map(int, tiempos[-1])
    return h * 3600 + m * 60 + s


def episodio_correcto(sub, video):
    """Rechaza subtítulos cuyo nombre de release indica otro episodio."""
    if not isinstance(video, Episode):
        return True
    nombres = [getattr(sub, a, None) for a in ('movie_release_name', 'release_info', 'filename', 'title')]
    for nombre in filter(None, nombres):
        if not isinstance(nombre, str):
            continue
        g = guessit(nombre, {'type': 'episode'})
        ep, temp = g.get('episode'), g.get('season')
        if isinstance(ep, int) and video.episode and ep != video.episode:
            return False
        if isinstance(temp, int) and video.season and temp != video.season:
            return False
    return True


def duracion_ok(texto, video):
    dur = getattr(video, 'duration', None)
    fin = ultimo_tiempo(texto)
    if not dur or not fin:
        return True
    return 0.6 * dur <= fin <= dur + 120


def buscar_videos(ruta):
    if ruta.is_file():
        return [ruta]
    return sorted(p for p in ruta.rglob('*') if p.suffix.lower() in VIDEO_EXT and 'sample' not in p.name.lower())


def _ts(ms):
    ms = max(0, int(round(ms)))
    return '%02d:%02d:%02d,%03d' % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)


def _a_srt(lineas):
    """[(inicio_ms, fin_ms, texto)] -> texto SRT con CRLF."""
    lineas = sorted(l for l in lineas if l[2])
    return ''.join(f'{k}\r\n{_ts(a)} --> {_ts(b)}\r\n{txt.replace(chr(10), chr(13) + chr(10))}\r\n\r\n'
                   for k, (a, b, txt) in enumerate(lineas, 1))


# ---------- Pistas de subtítulos incrustadas en MKV (sin ffmpeg/mkvtoolnix) ----------

def _vint(f):
    """Tamaño EBML; None si es 'desconocido' o fin de archivo."""
    b = f.read(1)
    if not b:
        return None
    b = b[0]; l = 1; m = 0x80
    while l <= 8 and not b & m:
        m >>= 1; l += 1
    v = b & (m - 1); unk = v == m - 1
    for x in f.read(l - 1):
        v = (v << 8) | x; unk = unk and x == 0xFF
    return None if unk else v


def _vint_b(d, p):
    b = d[p]; l = 1; m = 0x80
    while l <= 8 and not b & m:
        m >>= 1; l += 1
    if l > 8:                    # byte 0x00: sin este tope el bucle no terminaba nunca
        raise ArchivoInvalido('número de pista no válido en un bloque MKV')
    v = b & (m - 1)
    for x in d[p + 1:p + l]:
        v = (v << 8) | x
    return v, p + l


def _eid(f):
    b = f.read(1)
    if not b:
        return None
    b0 = b[0]; l = 1; m = 0x80
    while l <= 4 and not b0 & m:
        m >>= 1; l += 1
    v = b0
    for x in f.read(l - 1):
        v = (v << 8) | x
    return v


def _leer(f, n, maximo=MAX_ELEMENTO):
    """Lee exactamente n bytes; error si falta el tamaño, es anormal o el archivo se acaba antes.

    Sin esta comprobación, un MKV cortado o manipulado dejaba los bucles de lectura leyendo
    datos vacíos al final del archivo para siempre.
    """
    if n is None or n > maximo:
        raise ArchivoInvalido('tamaño de elemento no válido')
    d = f.read(n)
    if len(d) < n:
        raise ArchivoInvalido('el archivo se acaba antes de tiempo')
    return d


def pistas_mkv(path):
    """Lista de dicts {num, codec, lang, ietf, name, forced} de las pistas del MKV."""
    pistas = []
    with open(path, 'rb') as f:
        if _eid(f) != 0x1A45DFA3:
            return []
        f.seek(_vint(f), 1)
        if _eid(f) != 0x18538067:
            return []
        _vint(f)
        for _ in range(50):
            i = _eid(f); s = _vint(f)
            if i is None or s is None:
                break
            if i != 0x1654AE6B:
                f.seek(s, 1); continue
            fin = f.tell() + s
            while f.tell() < fin:
                _eid(f); s2 = _vint(f)
                if s2 is None:
                    raise ArchivoInvalido('pista sin tamaño')
                fin_t = f.tell() + s2
                t = {'lang': 'eng', 'ietf': '', 'name': '', 'forced': 0, 'codec': ''}
                while f.tell() < fin_t:
                    j = _eid(f); d = _leer(f, _vint(f))
                    if j == 0xD7: t['num'] = int.from_bytes(d, 'big')
                    elif j == 0x86: t['codec'] = d.decode('ascii', 'replace')
                    elif j == 0x22B59C: t['lang'] = d.decode('ascii', 'replace')
                    elif j == 0x22B59D: t['ietf'] = d.decode('ascii', 'replace')
                    elif j == 0x536E: t['name'] = d.decode('utf-8', 'replace')
                    elif j == 0x55AA: t['forced'] = int.from_bytes(d, 'big')
                pistas.append(t)
            break
    return pistas


def extraer_srt_mkv(path, nums):
    """Extrae pistas S_TEXT/UTF8 del MKV. Devuelve {num: texto_srt}. Salta los datos de vídeo/audio."""
    nums = set(nums)
    subs = {n: [] for n in nums}
    escala, ctc = 1_000_000, 0

    def bloque(d, dur):
        tn, p = _vint_b(d, 0)
        if tn not in nums:
            return
        t = (ctc + int.from_bytes(d[p:p + 2], 'big', signed=True)) * escala / 1e6
        fin = t + dur * escala / 1e6 if dur else t + 2000
        subs[tn].append((t, fin, d[p + 3:].decode('utf-8', 'replace').strip()))

    with open(path, 'rb') as f:
        size = f.seek(0, 2); f.seek(0)
        _eid(f); f.seek(_vint(f), 1); _eid(f); _vint(f)
        while f.tell() < size:
            i = _eid(f)
            if i is None:
                break
            s = _vint(f)
            if i == 0x1F43B675:          # Cluster: entrar
                continue
            if s is None:
                continue
            if i == 0x1549A966:          # Info: TimecodeScale
                fin = f.tell() + s
                while f.tell() < fin:
                    j = _eid(f); d = _leer(f, _vint(f))
                    if j == 0x2AD7B1:
                        escala = int.from_bytes(d, 'big')
            elif i == 0xE7:              # Timecode del cluster
                ctc = int.from_bytes(_leer(f, s), 'big')
            elif i == 0xA3:              # SimpleBlock
                tn, _ = _vint_b(f.read(1) + b'\0' * 8, 0); f.seek(-1, 1)
                if tn in nums:
                    bloque(_leer(f, s), None)
                else:
                    f.seek(s, 1)
            elif i == 0xA0:              # BlockGroup
                fin = f.tell() + s; blk = None; dur = None
                while f.tell() < fin:
                    j = _eid(f); s2 = _vint(f)
                    if s2 is None:
                        raise ArchivoInvalido('bloque sin tamaño')
                    if j == 0xA1:
                        blk = _leer(f, s2)
                    elif j == 0x9B:
                        dur = int.from_bytes(_leer(f, s2), 'big')
                    else:
                        f.seek(s2, 1)
                if blk:
                    bloque(blk, dur)
            else:
                f.seek(s, 1)

    return {n: _a_srt(lineas) for n, lineas in subs.items()}


# ---------- Pistas de subtítulos incrustadas en MP4/M4V/MOV (sin ffmpeg) ----------
# Solo se lee la caja 'moov' (índice de muestras, unos pocos MB) y luego las muestras de
# las pistas de subtítulos, saltando directamente a su posición: no se lee vídeo ni audio.

def _cajas(d, ini, fin):
    """Itera las cajas ISO-BMFF de d[ini:fin] -> (tipo, inicio_datos, fin)."""
    while ini + 8 <= fin:
        tam, tipo = struct.unpack_from('>I4s', d, ini); cab = 8
        if tam == 1:
            tam = struct.unpack_from('>Q', d, ini + 8)[0]; cab = 16
        elif tam == 0:
            tam = fin - ini
        if tam < cab:
            break
        yield tipo.decode('latin-1'), ini + cab, min(ini + tam, fin)
        ini += tam


def _leer_moov(f):
    tam_arch = f.seek(0, 2); pos = 0
    while pos + 8 <= tam_arch:
        f.seek(pos); cab = f.read(16)
        tam, tipo = struct.unpack_from('>I4s', cab); h = 8
        if tam == 1:
            tam = struct.unpack_from('>Q', cab, 8)[0]; h = 16
        elif tam == 0:
            tam = tam_arch - pos
        if tam < h:
            break
        if tipo == b'moov':
            if tam - h > MAX_MOOV:
                raise ArchivoInvalido('índice MP4 (moov) demasiado grande')
            f.seek(pos + h)
            return f.read(tam - h)
        pos += tam
    return None


def _trak_mp4(m, ini, fin):
    t = {'num': 0, 'codec': '', 'lang': '', 'ietf': '', 'name': '', 'forced': 0, 'handler': '',
         'escala': 1000, 'stts': [], 'stsc': [], 'stsz': [], 'co': [], 'elst': [], 'chap': [],
         'cajas': {}}

    def rec(i, f_):
        for tipo, a, b in _cajas(m, i, f_):
            v = m[a] if a < len(m) else 0
            if tipo in ('mdia', 'minf', 'stbl', 'udta', 'edts'):
                rec(a, b)
            elif tipo == 'tkhd':
                t['num'] = struct.unpack_from('>I', m, a + (20 if v == 1 else 12))[0]
            elif tipo == 'tref':
                for t2, a2, b2 in _cajas(m, a, b):
                    if t2 == 'chap':
                        t['chap'] += list(struct.unpack_from(f'>{(b2 - a2) // 4}I', m, a2))
            elif tipo == 'hdlr':
                t['handler'] = m[a + 8:a + 12].decode('latin-1')
            elif tipo == 'mdhd':
                t['escala'] = struct.unpack_from('>I', m, a + (20 if v == 1 else 12))[0] or 1000
                c = struct.unpack_from('>H', m, a + (32 if v == 1 else 20))[0]
                if c:
                    t['lang'] = ''.join(chr(((c >> s) & 31) + 0x60) for s in (10, 5, 0))
            elif tipo == 'elng':
                t['ietf'] = m[a + 4:b].split(b'\0')[0].decode('ascii', 'replace')
            elif tipo == 'name':       # título de la pista (udta/name)
                t['name'] = m[a:b].split(b'\0')[0].decode('utf-8', 'replace').strip()
            elif tipo == 'stsd':
                t['codec'] = m[a + 12:a + 16].decode('latin-1')
            elif tipo in ('stts', 'stsc', 'stsz', 'stco', 'co64', 'elst'):
                t['cajas'][tipo] = (a, v)   # se leen después, y solo en pistas de subtítulos

    rec(ini, fin)
    return t


def _tablas_mp4(m, t):
    """Lee las tablas de muestras de una pista de subtítulos, con límites de tamaño.

    Antes se leían las de todas las pistas sin límite: un MP4 manipulado (o un vídeo con
    audio PCM) podía declarar miles de millones de muestras y agotar la memoria.
    """
    def entradas(a, desp=4):
        n = struct.unpack_from('>I', m, a + desp)[0]
        if n > MAX_MUESTRAS:
            raise ArchivoInvalido('tabla de muestras MP4 demasiado grande')
        return n

    cajas = t.pop('cajas')
    if 'stts' in cajas:
        a, _ = cajas['stts']
        t['stts'] = [struct.unpack_from('>II', m, a + 8 + 8 * k) for k in range(entradas(a))]
    if 'stsc' in cajas:
        a, _ = cajas['stsc']
        t['stsc'] = [struct.unpack_from('>III', m, a + 8 + 12 * k) for k in range(entradas(a))]
    if 'stsz' in cajas:
        a, _ = cajas['stsz']
        tam, n = struct.unpack_from('>I', m, a + 4)[0], entradas(a, 8)
        t['stsz'] = [tam] * n if tam else list(struct.unpack_from(f'>{n}I', m, a + 12))
    for tipo, fmt in (('stco', 'I'), ('co64', 'Q')):
        if tipo in cajas:
            a, _ = cajas[tipo]
            t['co'] = list(struct.unpack_from(f'>{entradas(a)}{fmt}', m, a + 8))
    if 'elst' in cajas:
        a, v = cajas['elst']
        fmt, paso = ('>Qq', 20) if v == 1 else ('>Ii', 12)
        t['elst'] = [struct.unpack_from(fmt, m, a + 8 + paso * k) for k in range(entradas(a))]


def pistas_mp4(path):
    """Pistas de subtítulos de texto (tx3g / WebVTT) del MP4, con el mismo formato que pistas_mkv
    más los índices de muestras. Excluye las pistas de capítulos."""
    with open(path, 'rb') as f:
        moov = _leer_moov(f)
    if not moov:
        return []
    escala_mov, traks = 1000, []
    for tipo, a, b in _cajas(moov, 0, len(moov)):
        if tipo == 'mvhd':
            escala_mov = struct.unpack_from('>I', moov, a + (20 if moov[a] == 1 else 12))[0] or 1000
        elif tipo == 'trak':
            traks.append(_trak_mp4(moov, a, b))
    capitulos = {n for t in traks for n in t['chap']}
    pistas = []
    for t in traks:
        if t['codec'] in ('tx3g', 'wvtt') and t['num'] not in capitulos:
            _tablas_mp4(moov, t)
            t['escala_mov'] = escala_mov
            t['lang'] = t['lang'] or 'und'
            pistas.append(t)
    return pistas


def _texto_muestra(d, codec):
    if codec == 'wvtt':
        partes = [d[a2:b2].decode('utf-8', 'replace')
                  for tipo, a, b in _cajas(d, 0, len(d)) if tipo == 'vttc'
                  for t2, a2, b2 in _cajas(d, a, b) if t2 == 'payl']
        return '\n'.join(partes).strip()
    n = int.from_bytes(d[:2], 'big')
    raw = d[2:2 + n]
    if raw[:2] == b'\xfe\xff':
        txt = raw[2:].decode('utf-16-be', 'replace')
    else:
        txt = raw.decode('utf-8', 'replace')
    return txt.replace('\r\n', '\n').replace('\r', '\n').strip()


def _muestras_mp4(f, p):
    """[(inicio_ms, fin_ms, texto)] de una pista de subtítulos MP4."""
    tams, stsc = p['stsz'], p['stsc']
    if not stsc or not tams:
        return []                    # MP4 fragmentado (moof): no soportado
    offs, k = [], 0
    for ci, base in enumerate(p['co'], 1):
        while k + 1 < len(stsc) and stsc[k + 1][0] <= ci:
            k += 1
        for _ in range(stsc[k][1]):
            if len(offs) >= len(tams):
                break
            offs.append(base)
            base += tams[len(offs) - 1]
    # Una entrada de 'stts' puede declarar miles de millones de muestras: solo hacen falta tantas
    # duraciones como muestras tiene la pista.
    durs = []
    for n, d in p['stts']:
        durs.extend([d] * min(n, len(tams) - len(durs)))
        if len(durs) >= len(tams):
            break
    # Lista de edición: un hueco inicial retrasa la pista; un media_time > 0 la adelanta.
    desfase = 0.0
    for dur_seg, t_media in p['elst']:
        if t_media == -1:
            desfase += dur_seg / p['escala_mov']
        else:
            desfase -= t_media / p['escala']
            break
    lineas, t = [], 0
    for i, (o, tam) in enumerate(zip(offs, tams)):
        d = durs[i] if i < len(durs) else 0
        if tam > MAX_ELEMENTO:
            raise ArchivoInvalido('muestra de subtítulo MP4 demasiado grande')
        if tam > 2:
            f.seek(o)
            texto = _texto_muestra(f.read(tam), p['codec'])
            if texto:
                ini = (t / p['escala'] + desfase) * 1000
                fin = ((t + d) / p['escala'] + desfase) * 1000
                if lineas and lineas[-1][2] == texto and abs(lineas[-1][1] - ini) < 1:
                    lineas[-1] = (lineas[-1][0], fin, texto)     # WebVTT parte una frase en varias muestras
                else:
                    lineas.append((ini, fin, texto))
        t += d
    return lineas


def extraer_srt_mp4(path, nums):
    nums = set(nums)
    pistas = [p for p in pistas_mp4(path) if p['num'] in nums]
    with open(path, 'rb') as f:
        return {p['num']: _a_srt(_muestras_mp4(f, p)) for p in pistas}


# ---------- Pistas incrustadas (común a MKV y MP4) ----------

def pistas_texto(path):
    ext = path.suffix.lower()
    if ext in MKV_EXT:
        return [p for p in pistas_mkv(path) if p['codec'] == 'S_TEXT/UTF8']
    if ext in MP4_EXT:
        return pistas_mp4(path)
    return []


def extraer_srt(path, nums):
    return (extraer_srt_mkv if path.suffix.lower() in MKV_EXT else extraer_srt_mp4)(path, nums)


def _forzada(p):
    return p.get('forced') or re.search(r'forced|forzad', p['name'], re.IGNORECASE)


def _es_espanol(p):
    return p['lang'] in ('spa', 'es') or p['ietf'].lower().startswith('es')


def _es_ingles(p):
    return p['lang'] in ('eng', 'en') or p['ietf'].lower().startswith('en')


def _etiqueta_latina(p):
    ietf = p['ietf'].lower()
    return (ietf.startswith('es-') and ietf != 'es-es') or bool(LATINO_NOMBRE.search(p['name']))


def _etiqueta_espana(p):
    return p['ietf'].lower() == 'es-es' or bool(ESPANA_NOMBRE.search(p['name']))


def incrustado(path, con_ingles=False):
    """Busca en las pistas de texto del MKV/MP4.
    Devuelve {'castellano', 'latino', 'ingles'} -> (texto, descripción) o None."""
    res = {'castellano': None, 'latino': None, 'ingles': None}
    try:
        pistas = [p for p in pistas_texto(path) if not _forzada(p)]
    except Exception as e:
        print(f'  (no se pudieron leer las pistas incrustadas: {visible(e)})')
        return res
    if not pistas:
        return res
    print(f'  {len(pistas)} pistas de subtítulos incrustadas', flush=True)
    esp = [p for p in pistas if _es_espanol(p)]
    eng = sorted((p for p in pistas if _es_ingles(p)), key=lambda p: 'sdh' not in p['name'].lower())
    nums = [p['num'] for p in esp] + ([eng[0]['num']] if eng and con_ingles else [])
    if not nums:
        return res
    try:
        textos = extraer_srt(path, nums)
    except Exception as e:
        print(f'  (no se pudieron extraer las pistas incrustadas: {visible(e)})')
        return res
    cast, lat = [], []
    for p in esp:
        texto = textos.get(p['num'])
        if not texto or len(texto) > MAX_SRT:
            continue
        es, la, ok = dialecto(texto)
        print(f'  pista incrustada {p["num"]} [{visible(p["ietf"] or p["lang"])}] {visible(p["name"])}: '
              f'españa={es} latino={la}', flush=True)
        desc = visible(f'pista {p["num"]} {p["name"]}'.strip())
        # En las pistas oficiales la etiqueta latina es fiable, y el detector de dialecto no
        # basta (una pista "LA" de Disney+ dio españa=26 latino=11 y pasaba por castellano).
        if ok and not _etiqueta_latina(p):
            cast.append(((_etiqueta_espana(p), es / (la + 1)), texto, desc))
        else:
            lat.append(((_etiqueta_latina(p), la), texto, desc))
    if cast:
        res['castellano'] = max(cast, key=lambda c: c[0])[1:]
    if lat:
        res['latino'] = max(lat, key=lambda c: c[0])[1:]
    if eng and con_ingles and textos.get(eng[0]['num']) and len(textos[eng[0]['num']]) <= MAX_SRT:
        res['ingles'] = (textos[eng[0]['num']], visible(f'pista {eng[0]["num"]} {eng[0]["name"]}'.strip()))
    return res


def ingles_incrustado(path):
    """Extrae la mejor pista inglesa de texto del MKV/MP4 (prefiere SDH). Devuelve (texto | None, nombre)."""
    try:
        pistas = [p for p in pistas_texto(path) if not _forzada(p) and _es_ingles(p)]
        if not pistas:
            return None, ''
        pistas.sort(key=lambda p: 'sdh' not in p['name'].lower())
        texto = extraer_srt(path, [pistas[0]['num']]).get(pistas[0]['num'])
    except Exception:
        return None, ''
    if not texto or len(texto) > MAX_SRT:
        return None, ''
    return texto, visible(f'pista {pistas[0]["num"]} {pistas[0]["name"]}'.strip())


class UltimoRecurso:
    """opensubtitles.com, con la misma forma que un ProviderPool (list_subtitles y download_subtitle).

    Solo se usa si hay cuenta y solo cuando los demás proveedores no han dado castellano (o, al final,
    inglés). Su pool se abre la primera vez que hace falta (la búsqueda no inicia sesión; la primera
    descarga, sí). Se deja de usar en toda la ejecución en cuanto falla (cuenta incorrecta, servicio
    caído) o se agota el cupo de descargas del día. Tras cada descarga, el servidor dice cuántas quedan."""

    def __init__(self, configs, pila):
        self.config = configs.get(ULTIMO)
        self.activo = isinstance(self.config, dict)
        self._pila = pila
        self._pool = None
        self.restantes = None
        self._informado = None

    def _abrir(self):
        if self._pool is None:
            self._pool = self._pila.enter_context(ProviderPool(providers=[ULTIMO],
                                                               provider_configs={ULTIMO: self.config}))
        return self._pool

    def _vigilar_cupo(self):
        """Apunta las descargas que quedan (subliminal no las guarda): de la respuesta del servidor."""
        proveedor = getattr(self._pool, 'initialized_providers', {}).get(ULTIMO)
        if proveedor is None or getattr(proveedor, '_vigilado', False) or not hasattr(proveedor, 'api_post'):
            return
        original = proveedor.api_post

        def api_post(path, body=None, **k):
            r = original(path, body, **k)
            if path == 'download' and isinstance(r, dict) and isinstance(r.get('remaining'), int):
                self.restantes = r['remaining']
            return r
        proveedor.api_post = api_post
        proveedor._vigilado = True

    def _apagar(self, motivo):
        self.activo = False
        print(motivo, flush=True)

    def list_subtitles(self, video, idiomas):
        if not self.activo:
            return []
        pool = self._abrir()
        candidatos = pool.list_subtitles(video, idiomas)
        if ULTIMO in pool.discarded_providers:
            self._apagar('  opensubtitles.com no responde o no acepta la cuenta: no se le pide nada más en '
                         'esta ejecución')
            return []
        self._vigilar_cupo()
        return candidatos

    def download_subtitle(self, sub):
        if not self.activo:
            return False
        ok = self._pool.download_subtitle(sub)
        if ULTIMO in self._pool.discarded_providers:
            self._apagar('  opensubtitles.com no deja descargar (cupo de hoy agotado o cuenta no válida): no '
                         'se le pide nada más en esta ejecución')
            return False
        return ok

    def informar(self):
        """Descargas que quedan hoy, si han cambiado desde el último aviso."""
        if self.activo and self.restantes is not None and self.restantes != self._informado:
            self._informado = self.restantes
            print(f'  opensubtitles.com: quedan {self.restantes} descargas por hoy', flush=True)


def ingles_proveedores(pool, video, max_descargas=MAX_DESCARGAS):
    """Busca subtítulos en inglés; descarga por orden de puntuación hasta encontrar uno válido."""
    candidatos = pool.list_subtitles(video, {Language('eng')})
    candidatos.sort(key=lambda s: compute_score(s, video), reverse=True)
    for sub in candidatos[:max_descargas]:
        if not pool.download_subtitle(sub) or not sub.content:
            continue
        texto = sub.text
        if len(texto) > MAX_SRT:
            print(f'  [inglés] {sub.provider_name:13} id={visible(sub.id)} descartado: tamaño anormal',
                  flush=True)
            continue
        ep_ok = episodio_correcto(sub, video)
        dur_ok = duracion_ok(texto, video)
        print(f'  [inglés] {sub.provider_name:13} id={visible(sub.id)} score={compute_score(sub, video)} '
              f'episodio_ok={ep_ok} duración_ok={dur_ok}', flush=True)
        if ep_ok and dur_ok:
            return texto, sub
    return None, None


def guardar(texto, destino):
    if destino.exists():
        bk = HOME / 'backup' / f'{datetime.now():%Y%m%d-%H%M%S}_{destino.name}'
        bk.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(destino, bk)
    # El texto viene de terceros: se guarda sin caracteres de control ni de dirección del texto.
    destino.write_text(limpiar_texto(texto), encoding='utf-8', newline='')


class SinRuido(logging.Filter):
    """subliminal escribe trazas largas en stderr y el refinador omdb da 401 siempre.

    Se descartan, porque no cambian el resultado:
    - los fallos de refinado (omdb...);
    - el cierre de sesión de opensubtitles.org (el servidor ya no tiene el método
      `close`, pero la búsqueda y las descargas funcionan);
    - los avisos de knowit (largos banners de "ffprobe/mkvmerge not found": el script lee
      las pistas MKV/MP4 por su cuenta y no necesita esos programas).
    Del resto de avisos se deja una sola línea, sin traza. Así la salida es limpia en
    cualquier shell y agente sin tener que filtrarla con grep / Where-Object.
    """

    def filter(self, record):
        mensaje = record.getMessage()
        if (record.name.startswith('knowit') or 'Failed to refine' in mensaje
                or 'improperly terminated' in mensaje
                # opensubtitles.com sin cupo: el script ya lo dice
                or 'download quota exceeded' in mensaje or f'Discarding provider {ULTIMO}' in mensaje):
            return False
        record.exc_info = record.exc_text = None
        return True


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    aviso = logging.StreamHandler()
    aviso.setFormatter(logging.Formatter('  aviso (%(name)s): %(message)s'))
    aviso.addFilter(SinRuido())
    logging.basicConfig(level=logging.WARNING, handlers=[aviso])
    ap = argparse.ArgumentParser()
    ap.add_argument('ruta', type=Path)
    ap.add_argument('--force', action='store_true',
                    help='rehacer aunque ya haya un .srt castellano (nunca se baja a un tipo peor)')
    ap.add_argument('--solo-castellano', action='store_true',
                    help='no guardar latino aunque no haya castellano (por defecto, si solo hay '
                         'latino, se guarda como .srt y el resumen lo marca como LATINO)')
    ap.add_argument('--sin-ingles', action='store_true',
                    help='no bajar inglés cuando no haya ningún subtítulo en español '
                         '(por defecto se guarda como .srt y el resumen lo marca como INGLÉS)')
    ap.add_argument('--dry-run', action='store_true', help='no escribir nada, solo mostrar qué haría')
    ap.add_argument('--ingles', action='store_true',
                    help='además, extraer la pista inglesa incrustada en el vídeo como .en.srt')
    ap.add_argument('--max-videos', type=int, default=MAX_VIDEOS, metavar='N',
                    help=f'máximo de vídeos que buscan en internet por ejecución (por defecto '
                         f'{MAX_VIDEOS}; 0 = sin límite). Los que ya tienen castellano o lo traen '
                         f'incrustado no cuentan. El resto queda como PENDIENTE (límite)')
    ap.add_argument('--max-descargas', type=int, default=MAX_DESCARGAS, metavar='N',
                    help=f'máximo de candidatos que se descargan por vídeo (por defecto '
                         f'{MAX_DESCARGAS}; 0 = sin límite), elegidos por sincronía y '
                         f'puntuación alternando proveedores')
    args = ap.parse_args()
    for opcion in ('max_videos', 'max_descargas'):
        if getattr(args, opcion) < 0:
            ap.error(f"--{opcion.replace('_', '-')} debe ser 0 (sin límite) o un número positivo")
    max_descargas = args.max_descargas or None  # None: sin límite
    if args.dry_run:
        global guardar
        guardar = lambda texto, destino: print(f'  [dry-run] guardaría {destino.name}')

    region.configure('dogpile.cache.memory')
    configs = {}
    cred = HOME / 'credenciales.json'
    if cred.exists():
        configs = json.loads(cred.read_text(encoding='utf-8'))
    providers = [p for p in PROVIDERS if p != 'opensubtitles' or 'opensubtitles' in configs]

    resumen = []
    buscados = 0  # vídeos que ya han buscado en internet en esta ejecución
    with contextlib.ExitStack() as pila:
        pool = pila.enter_context(ProviderPool(providers=providers, provider_configs=configs))
        ultimo = UltimoRecurso(configs, pila)
        for path in buscar_videos(args.ruta):
            destino = path.with_suffix('.srt')
            # Formato antiguo: <video>.es.srt (o .en.srt si no había español) -> <video>.srt
            previo = destino if destino.exists() else next(
                (p for p in (path.with_suffix('.es.srt'), path.with_suffix('.en.srt')) if p.exists()), None)
            actual = idioma_srt(previo.read_text(encoding='utf-8', errors='replace')) if previo else None
            nota = '-'
            if previo and previo != destino:
                nota = f'renombrado desde {previo.suffixes[-2]}.srt'
                if args.dry_run:
                    print(f'  [dry-run] renombraría {previo.name} -> {destino.name}')
                else:
                    previo.rename(destino)

            if actual == 'castellano' and not args.force:
                resumen.append((path.name, 'ya tenía castellano', nota))
                continue

            print(f'\n== {path.name}', flush=True)
            if actual:
                print(f'  ya tiene {destino.name} en {actual}', flush=True)

            def aplicar(tipo, texto, estado, fuente):
                """Guarda como <video>.srt salvo que el que ya hay sea de un tipo mejor."""
                if RANGO[tipo] > RANGO[actual] or (args.force and RANGO[tipo] == RANGO[actual]):
                    guardar(texto, destino)
                    resumen.append((path.name, estado, fuente))
                else:
                    resumen.append((path.name, f'{ETIQUETA[actual]} (ya tenía .srt)', nota))

            emb = incrustado(path, args.ingles)
            destino_en = path.with_suffix('.en.srt')
            if emb['ingles'] and (args.force or not destino_en.exists()):
                guardar(emb['ingles'][0], destino_en)
            if emb['castellano']:
                aplicar('castellano', emb['castellano'][0], 'CASTELLANO (incrustado)', emb['castellano'][1][:30])
                continue

            # A partir de aquí se busca en internet: límite por ejecución y pausa entre vídeos.
            if args.max_videos and buscados >= args.max_videos:
                print(f'  límite de {args.max_videos} vídeos con búsqueda en internet alcanzado: '
                      f'queda para otra ejecución', flush=True)
                resumen.append((path.name, 'PENDIENTE (límite)', f'máx. {args.max_videos} por ejecución'))
                continue
            if buscados:
                time.sleep(PAUSA)
            buscados += 1

            video = scan_video(str(path))
            # scan_video adivina sobre la ruta completa: una carpeta como "<SERIE> - FULL"
            # convierte la serie en "<SERIE> FULL" y ningún proveedor encuentra nada.
            if isinstance(video, Episode):
                g = guessit(path.name)
                if g.get('title'):
                    video.series = g['title']
                    video.title = g.get('episode_title')
            refine(video, refiners=['hash', 'metadata', 'tvdb'])

            candidatos = pool.list_subtitles(video, {Language('spa')})
            if max_descargas and len(candidatos) > max_descargas:
                print(f'  {len(candidatos)} candidatos en español: se descargan los {max_descargas} '
                      f'mejores, alternando proveedores', flush=True)
            castellanos, latinos = [], []
            vistos = set()

            def revisar(fuente, candidatos, hasta_el_primero=False):
                """Descarga los mejores candidatos de `fuente` y los clasifica. Con `hasta_el_primero`,
                para en el primer castellano válido (opensubtitles.com: cada descarga gasta cupo)."""
                for sub in mejores_candidatos(candidatos, video, max_descargas):
                    if not fuente.download_subtitle(sub) or not sub.content:
                        continue
                    texto = sub.text
                    if len(texto) > MAX_SRT:
                        print(f'  {sub.provider_name:13} id={visible(sub.id)} descartado: tamaño anormal',
                              flush=True)
                        continue
                    if texto in vistos:
                        continue
                    vistos.add(texto)
                    es, la, ok = dialecto(texto)
                    ep_ok = episodio_correcto(sub, video)
                    dur_ok = duracion_ok(texto, video)
                    score = compute_score(sub, video)
                    sinc = sincronia(sub, video)
                    print(f'  {sub.provider_name:13} id={visible(sub.id)} score={score} sincronía={sinc} '
                          f'españa={es} latino={la} episodio_ok={ep_ok} duración_ok={dur_ok}', flush=True)
                    if not (ep_ok and dur_ok):
                        continue
                    (castellanos if ok else latinos).append((sinc, es, score, sub, texto))
                    if hasta_el_primero and ok:
                        return

            revisar(pool, candidatos)
            if not castellanos and ultimo.activo:
                # Último recurso: opensubtitles.com, solo si los demás no han dado castellano.
                candidatos = ultimo.list_subtitles(video, {Language('spa')})
                if candidatos:
                    print(f'  opensubtitles.com (último recurso): {len(candidatos)} candidatos en español',
                          flush=True)
                    revisar(ultimo, candidatos, hasta_el_primero=True)
                    ultimo.informar()

            if castellanos:
                # 1) sincronía (mismo archivo > mismo release), 2) más señales de castellano,
                # 3) puntuación de subliminal. Antes mandaba la puntuación y un +2 por coincidir
                # en resolución y códec ganaba a un candidato con 5 veces más señales.
                sinc, es, score, sub, texto = max(castellanos, key=lambda c: (c[0], c[1], c[2]))
                aplicar('castellano', texto, 'CASTELLANO', f'{sub.provider_name} {sub.id}')
            elif emb['latino'] and not args.solo_castellano:
                # La pista incrustada va sincronizada exactamente con el vídeo: mejor que una web.
                aplicar('latino', emb['latino'][0], 'LATINO (incrustado)', emb['latino'][1][:30])
            elif latinos and not args.solo_castellano:
                sinc, es, score, sub, texto = max(latinos, key=lambda c: (c[0], c[2]))
                aplicar('latino', texto, 'LATINO', f'{sub.provider_name} {sub.id}')
            elif RANGO[actual] >= RANGO['ingles'] and (not args.force or RANGO[actual] > RANGO['ingles']):
                # Ya hay latino o inglés y no se ha encontrado nada mejor: se deja como está.
                resumen.append((path.name, f'{ETIQUETA[actual]} (ya tenía .srt)', nota))
            elif args.sin_ingles:
                resumen.append((path.name, 'NO ENCONTRADO', f'{len(latinos)} latinos descartados'))
            else:
                # Ni castellano ni latino: subtítulos en inglés como último recurso.
                texto, pista = (emb['ingles'] or (None, ''))
                texto, pista = (texto, pista) if texto else ingles_incrustado(path)
                if texto:
                    aplicar('ingles', texto, 'INGLÉS (incrustado)', pista[:30])
                    continue
                texto, sub = ingles_proveedores(pool, video, max_descargas)
                if not texto and ultimo.activo:
                    texto, sub = ingles_proveedores(ultimo, video, max_descargas)
                    ultimo.informar()
                if texto:
                    aplicar('ingles', texto, 'INGLÉS', f'{sub.provider_name} {sub.id}')
                else:
                    resumen.append((path.name, 'NO ENCONTRADO', 'ni español ni inglés'))

    print('\n==== RESUMEN ====')
    for nombre, estado, fuente in resumen:
        print(f'{visible(estado):28} {visible(fuente):30} {visible(nombre)}')


if __name__ == '__main__':
    sys.exit(main())
