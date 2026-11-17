"""Importar listas desde otras fuentes además de Spotify/Deezer.

- YouTube Music / YouTube: playlists públicas vía ytmusicapi (sin login).
- Texto pegado ("Artista - Canción", tabuladores, comas) o CSV (Exportify y variantes).
- Apple Music: página pública (JSON embebido "serialized-server-data").

Cada función devuelve el mismo formato que usa ``import_playlist``:
``{"name","owner","total","tracks":[{"id","name","artists","album","duration_ms","album_art"}],
"skipped","source"}``; las canciones pueden traer además ``video_id`` (video de YouTube ya
elegido) y ``unresolved`` (no se encontró con confianza en el catálogo).

Las canciones se resuelven contra el catálogo (Deezer primero; Spotify si Deezer no la tiene)
para que ``download_and_process`` pueda pedir sus detalles. Las que no se resuelven quedan con
un ID sintético ``q:<hash>`` y se descargan con metadatos básicos (``reverse_download_and_process``).
"""
import csv
import difflib
import hashlib
import io
import json
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Tuple

import requests
from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from services.deezer import DeezerError

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36",
      "Accept-Language": "es-MX,es;q=0.9,en;q=0.8"}
UNRESOLVED_NOTE = "No encontrada en el catálogo"
DUR_TOLERANCE_MS = 10_000
LOOSE_DUR_MS = 30_000
WORKERS = 6
MAX_TEXT_LINES = 3000

YT_RE = re.compile(r"(?:music\.)?youtube\.com/.*[?&]list=([A-Za-z0-9_-]+)|youtu\.be/.*[?&]list=([A-Za-z0-9_-]+)")
APPLE_RE = re.compile(r"music\.apple\.com/([a-z]{2})/playlist/(?:[^/?#]+/)?(pl\.[A-Za-z0-9_-]+)")


# ---------------------------------------------------------------- progreso
PROGRESS: Dict[str, Dict] = {}
_prog_lock = threading.Lock()


def _progress(token: Optional[str], **f):
    if not token:
        return
    with _prog_lock:
        now = time.time()
        for k in [k for k, v in PROGRESS.items() if now - v.get("ts", now) > 3600]:
            PROGRESS.pop(k, None)
        PROGRESS.setdefault(token, {}).update(f, ts=now)


router = APIRouter()


@router.get("/api/import/progress/{token}")
async def import_progress(token: str):
    p = PROGRESS.get(token)
    if not p:
        return {"stage": None}
    return {k: v for k, v in p.items() if k != "ts"}


# ---------------------------------------------------------------- esquema
def migrate(conn):
    """Columnas nuevas de ptracks (compatible con bases existentes)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ptracks)")}
    if "video_id" not in cols:
        conn.execute("ALTER TABLE ptracks ADD COLUMN video_id TEXT")


def save_extra(conn, pid: str, tracks: List[Dict]):
    """Guarda lo que el INSERT genérico no conoce: video de YouTube y nota de 'no encontrada'."""
    for t in tracks:
        if t.get("video_id"):
            conn.execute("UPDATE ptracks SET video_id=? WHERE playlist_id=? AND track_id=?",
                         (t["video_id"], pid, t["id"]))
            # el mismo video importado antes con otro ID (p. ej. antes no se encontró en el catálogo)
            conn.execute("DELETE FROM ptracks WHERE playlist_id=? AND video_id=? AND track_id<>? "
                         "AND status IN ('missing','auto','maybe')", (pid, t["video_id"], t["id"]))
        if t.get("unresolved"):
            conn.execute("UPDATE ptracks SET note=COALESCE(note, ?) WHERE playlist_id=? AND track_id=?",
                         (UNRESOLVED_NOTE, pid, t["id"]))


# ---------------------------------------------------------------- normalización
_VERSION = re.compile(r"\b(live|en vivo|en directo|unplugged|desenchufado|remix|karaoke|instrumental|acoustic|acustic[oa]|"
                      r"cover|dub|mix|sped up|slowed|reverb|8d|nightcore|tribute|made famous)\b", re.I)
_YT_TAIL = re.compile(r"(\s+|\s*[-|:]\s*)(video\s?clips?(\s+oficiales)?|official\s+(music\s+)?video|video\s+oficial|"
                      r"letra|lyrics?|full\s+hq|hq|hd|4k|audio|oficial|official)\s*$|\.(wmv|avi|mp4|flv|mpg)$", re.I)
_YT_JUNK = re.compile(r"\s*[\(\[\{][^\)\]\}]*\b(official|oficial|video|v[ií]deo|lyrics?|letra|audio|hd|hq|4k|"
                      r"visuali[sz]er|clip|mv|m/v)\b[^\)\]\}]*[\)\]\}]", re.I)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("&", " and ").replace("$", "s")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def base_title(t: str) -> str:
    orig = t = t or ""
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", t)
    t = re.sub(r"\s+(feat\.?|ft\.?|featuring)\s+.*$", "", t, flags=re.I)
    t = re.sub(r"\s+[-–—]\s+.*(remaster|version|versión|mix|edit|mono|stereo|from|deluxe|bonus).*$", "", t, flags=re.I)
    return norm(t) or norm(orig)


def split_artists(s: str) -> List[str]:
    parts = re.split(r"\s*(?:,|;|/|&|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b|\bx\b|\by\b|\band\b)\s*", s or "", flags=re.I)
    return [p.strip() for p in parts if p and p.strip()]


def clean_channel(name: str) -> str:
    name = re.sub(r"\s*-\s*Topic$", "", name or "", flags=re.I)
    name = re.sub(r"(VEVO|Official|Oficial|Music|TV)$", "", name).strip()
    return name or ""


def qid(name: str, artist: str) -> str:
    h = hashlib.sha1(f"{norm(artist)}|{norm(name)}".encode()).hexdigest()[:12]
    return "q:" + h


# ---------------------------------------------------------------- límite de Deezer
class RateLimiter:
    """Ventana deslizante: como máximo ``n`` peticiones cada ``per`` segundos."""

    def __init__(self, n: int, per: float):
        self.n, self.per = n, per
        self.calls: List[float] = []
        self.lock = threading.Lock()

    def wait(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.calls = [c for c in self.calls if now - c < self.per]
                if len(self.calls) < self.n:
                    self.calls.append(now)
                    return
                delay = self.per - (now - self.calls[0]) + 0.01
            time.sleep(delay)


DEEZER_LIMIT = RateLimiter(40, 5.0)   # límite real 50 / 5 s; margen para el resto de la app


# ---------------------------------------------------------------- resolución contra el catálogo
class Resolver:
    def __init__(self, catalog):
        self.catalog = catalog
        self.dz = catalog.deezer
        self.sp = catalog.spotify
        self._sp_lock = threading.Lock()
        self._cache: Dict = {}

    def _dz(self, path: str, **params) -> Dict:
        DEEZER_LIMIT.wait()
        return self.dz._get(path, **params)

    # -- validación de un candidato
    @staticmethod
    def score(cand: Dict, name: str, artists: List[str], duration_ms: int, loose: bool = False) -> Optional[float]:
        """Puntaje del candidato o None si no es confiable. ``loose``: la duración viene de un video
        musical (intro/outro), así que se acepta hasta ±30 s solo si título y artista coinciden exacto."""
        ct, t = base_title(cand.get("name", "")), base_title(name)
        if not ct or not t:
            return None
        if ct == t:
            ts = 1.0
        else:
            ts = difflib.SequenceMatcher(None, ct, t).ratio()
            if len(t) >= 4 and (f" {t} " in f" {ct} " or f" {ct} " in f" {t} "):
                ts = max(ts, 0.9)
            if ts < 0.85:
                return None
        # versiones (en vivo, remix, karaoke…) solo si se pidieron
        want = set(m.lower() for m in _VERSION.findall(name))
        got = set(m.lower() for m in _VERSION.findall(re.sub(r"original mix", "", cand.get("name", "") + " " + cand.get("album", ""), flags=re.I)))
        if got - want:
            return None
        aq = Resolver.artist_ok(cand.get("artists") or [cand.get("artist", "")], artists) if artists else 0.0
        if artists and not aq:
            return None
        cd = cand.get("duration_ms") or 0
        ds = 0.0
        if duration_ms and cd:
            diff = abs(cd - duration_ms)
            if diff > DUR_TOLERANCE_MS:
                if not (loose and diff <= LOOSE_DUR_MS and ts == 1.0 and aq == 1.0):
                    return None
                ds = -0.5
            else:
                ds = 1 - diff / DUR_TOLERANCE_MS
        if not artists and not (ts == 1.0 and duration_ms and cd and abs(cd - duration_ms) <= 3000):
            return None   # sin artista (JSON-LD de Apple): solo título exacto y duración casi idéntica
        # preferir el título "limpio" (sin "Radio Edit", "Remastered"… que no se pidieron)
        nofeat = re.compile(r"\b(feat|ft|featuring|with|con)\b\.?[^\)\]]*", re.I)
        extra = len(set(norm(nofeat.sub("", cand.get("name", ""))).split()) - set(norm(nofeat.sub("", name)).split()))
        return ts * 2 + ds + aq * 0.4 - min(extra, 5) * 0.08

    @staticmethod
    def artist_ok(cand_artists: List[str], artists: List[str]) -> float:
        """1.0 = mismo artista, 0.6 = casi igual (acentos, "The"), 0 = otro artista."""
        cands = [norm(c) for c in cand_artists if norm(c)]
        best = 0.0
        for a in artists:
            na = norm(a)
            if not na:
                continue
            ta = set(na.split())
            for ca in cands:
                if ca == na:
                    return 1.0
                # erratas en nombres largos ("Richie Valens" ~ "Ritchie Valens"); no "sakira" ~ "Shakira"
                if len(na) >= 8 and difflib.SequenceMatcher(None, ca, na).ratio() >= 0.9:
                    best = 0.6
                tc = set(ca.split())
                # "The Weeknd" ~ "Weeknd", "Beyoncé" ~ "Beyoncé Knowles"; no "Hello I'm Adele" ~ "Adele"
                small, big = (ta, tc) if len(ta) <= len(tc) else (tc, ta)
                if small and small <= big and len(" ".join(small)) >= 4 and (
                        big - small <= {"the", "los", "las", "la", "el", "and", "y"}
                        or (len(small) >= 2 and len(big) - len(small) <= 1)):
                    best = max(best, 0.6)
        return best

    def _best(self, cands: List[Dict], name, artists, duration_ms, loose=False) -> Optional[Dict]:
        best, best_s = None, None
        for c in cands:
            s = self.score(c, name, artists, duration_ms, loose)
            if s is not None and (best_s is None or s > best_s):
                best, best_s = c, s
        return best

    def by_isrc(self, isrc: str) -> Optional[Dict]:
        isrc = re.sub(r"[^A-Za-z0-9]", "", isrc or "").upper()
        if len(isrc) != 12:
            return None
        try:
            t = self._dz(f"track/isrc:{isrc}")
        except DeezerError:
            return None
        if not t.get("id"):
            return None
        return self.dz._track(t)

    def _dz_search(self, q: str, limit: int) -> List[Dict]:
        key = (q.lower(), limit)
        if key in self._cache:
            return self._cache[key]
        try:
            res = [self.dz._track(t) for t in self._dz("search", q=q, limit=limit).get("data", [])
                   if t.get("type", "track") == "track"]
        except DeezerError as e:
            print(f"importar: Deezer falló con {q!r}: {e}")
            res = []
        self._cache[key] = res
        return res

    def search(self, name: str, artists: List[str], duration_ms: int = 0, loose: bool = False) -> Optional[Dict]:
        """Deezer: por título (filtro "track:") y por "artista título"; el artista se valida aquí
        porque el filtro "artist:" de Deezer ya no devuelve resultados."""
        main = artists[0] if artists else ""
        clean = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", name).replace('"', "").strip() or name.replace('"', "")
        queries = [(f'track:"{clean}"', 50)] if main else []
        queries.append((f"{main} {clean}".strip(), 25))
        for q, lim in queries:
            hit = self._best(self._dz_search(q, lim), name, artists, duration_ms, loose)
            if hit:
                return hit
        return None

    def search_free(self, text: str, duration_ms: int = 0, loose: bool = False) -> Optional[Dict]:
        """Texto sin separador ("kraken lenguaje de mi piel"): se acepta un resultado solo si su
        título y su artista aparecen completos en el texto."""
        nt = f" {norm(text)} "
        if len(nt.split()) < 2:
            return None
        for c in self._dz_search(text, 10):
            bt = base_title(c.get("name", ""))
            arts = [norm(a) for a in c.get("artists") or [] if norm(a)]
            if bt and f" {bt} " in nt and any(f" {a} " in nt for a in arts):
                rest = nt.replace(f" {bt} ", " ")
                if self.score(c, c["name"], [a for a in c["artists"] if f" {norm(a)} " in rest] or c["artists"],
                              duration_ms, loose) is not None:
                    return c
        return None

    def search_spotify(self, name: str, artists: List[str], duration_ms: int = 0, loose: bool = False):
        if not (self.sp and artists):
            return None
        try:
            with self._sp_lock:
                res = self.sp.search_tracks(f"{artists[0]} {name}", limit=10)
            return self._best(res, name, artists, duration_ms, loose)
        except Exception as e:
            print(f"importar: Spotify falló: {e}")
            return None

    def resolve(self, item: Dict) -> Dict:
        """item: {name, artists, album, duration_ms, isrc, spotify_id, video_id, album_art, alts}.
        ``alts`` = otras interpretaciones (artista, canción) a intentar si la primera no aparece."""
        name, artists = item.get("name") or "", item.get("artists") or []
        dur = item.get("duration_ms") or 0
        hit = None
        if item.get("spotify_id") and self.sp:
            hit = {"id": item["spotify_id"], "name": name, "artists": artists, "album": item.get("album") or "",
                   "duration_ms": dur, "album_art": item.get("album_art")}
        if not hit and item.get("isrc"):
            hit = self.by_isrc(item["isrc"])
        loose = bool(item.get("loose"))
        tries = [] if item.get("free_only") else [(n, a) for n, a in [(name, artists)] + list(item.get("alts") or []) if n]
        for n, a in ([] if hit else tries):
            hit = self.search(n, a, dur, loose)
            if hit:
                break
        if not hit and item.get("free"):
            hit = self.search_free(item["free"], dur, loose)
        for n, a in ([] if hit else tries[:2]):   # Spotify al final (tiene límites por cuenta)
            hit = self.search_spotify(n, a, dur, loose)
            if hit:
                break
        if hit:
            return {"id": hit["id"], "name": hit.get("name") or name, "artists": hit.get("artists") or artists,
                    "album": hit.get("album") or item.get("album") or "",
                    "duration_ms": hit.get("duration_ms") or dur,
                    "album_art": hit.get("album_art") or item.get("album_art"),
                    "video_id": item.get("video_id")}
        return {"id": qid(name, ", ".join(artists)), "name": name, "artists": artists,
                "album": item.get("album") or "", "duration_ms": dur, "album_art": item.get("album_art"),
                "video_id": item.get("video_id"), "unresolved": True, "drop": bool(item.get("free_only"))}

    def resolve_all(self, items: List[Dict], token: Optional[str] = None) -> Tuple[List[Dict], int]:
        """Resuelve en paralelo (respetando el límite de Deezer), conserva el orden y quita duplicados."""
        total = len(items)
        _progress(token, stage="resolving", done=0, total=total)
        out: List[Optional[Dict]] = [None] * total
        done = [0]
        lock = threading.Lock()

        def work(i):
            try:
                out[i] = self.resolve(items[i])
            except Exception as e:
                print(f"importar: error resolviendo {items[i].get('name')!r}: {e}")
                it = items[i]
                out[i] = {"id": qid(it.get("name", ""), ", ".join(it.get("artists") or [])),
                          "name": it.get("name", ""), "artists": it.get("artists") or [], "album": it.get("album") or "",
                          "duration_ms": it.get("duration_ms") or 0, "album_art": it.get("album_art"),
                          "video_id": it.get("video_id"), "unresolved": True, "drop": bool(it.get("free_only"))}
            with lock:
                done[0] += 1
                _progress(token, done=done[0])

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            list(ex.map(work, range(total)))
        seen, tracks, dups = set(), [], 0
        for t in out:
            if t["id"] in seen:
                dups += 1
                continue
            seen.add(t["id"])
            tracks.append(t)
        return tracks, dups


# ---------------------------------------------------------------- YouTube Music / YouTube
def _yt_item(t: Dict) -> Optional[Dict]:
    vid = t.get("videoId")
    title = (t.get("title") or "").strip()
    if not vid or not title:
        return None
    album = ((t.get("album") or {}) or {}).get("name") or ""
    is_song = bool(album) or (t.get("videoType") or "").endswith("ATV")
    arts = [a.get("name").strip() for a in t.get("artists") or [] if a.get("name")]
    if not is_song:  # video normal: el "artista" suele ser el canal (FooVEVO, Foo - Topic)
        arts = [clean_channel(a) for a in arts]
    arts = [a for a in arts if a]
    thumbs = t.get("thumbnails") or []
    art = thumbs[-1]["url"] if thumbs else None
    dur = int(t.get("duration_seconds") or 0) * 1000
    clean = _YT_JUNK.sub("", title).strip() or title
    if not is_song:
        for _ in range(3):
            clean = _YT_TAIL.sub("", clean).strip() or clean
    name, artists, alts, free = clean, arts, [], None
    m = None if is_song else (re.match(r"^(.+?)\s*(?:\s[-–—|]+|[-–—|]+\s|:{1,3})\s*(.+)$", clean)
                              or re.match(r'^(.+?)\s+["“](.+?)["”]$', clean))
    if m:
        # lo normal en videos subidos es "Artista - Canción"; el canal suele ser otra persona
        left, right = m.group(1).strip(), m.group(2).strip()
        name, artists = right, split_artists(left)
        alts = [(left, split_artists(right))] + ([(clean, arts)] if arts else [])
    if not is_song:
        free = clean   # "kraken lenguaje de mi piel": título y artista sin separador
    return {"name": name, "artists": artists, "album": album, "duration_ms": dur, "loose": not is_song,
            "video_id": vid, "album_art": art, "alts": alts, "free": free}


def fetch_youtube(list_id: str, ytmusic, resolver: Resolver, token=None) -> Dict:
    if ytmusic is None:
        from ytmusicapi import YTMusic
        ytmusic = YTMusic()
    _progress(token, stage="reading", done=0, total=0)
    try:
        pl = ytmusic.get_playlist(list_id, limit=None)
    except Exception as e:
        raise HTTPException(502, f"YouTube no devolvió la playlist (¿es pública?): {e}")
    raw = pl.get("tracks") or []
    items = [x for x in (_yt_item(t) for t in raw if t.get("isAvailable", True) is not False) if x]
    tracks, dups = resolver.resolve_all(items, token)
    author = pl.get("author")
    owner = author.get("name") if isinstance(author, dict) else author
    total = int(pl.get("trackCount") or len(raw))
    return {"name": pl.get("title") or "Playlist de YouTube", "owner": owner, "total": total,
            "tracks": tracks, "skipped": max(0, total - len(tracks)), "source": "youtube",
            "complete": True}   # get_playlist(limit=None) trae todo lo disponible


# ---------------------------------------------------------------- Apple Music
def _iso_dur(s: str) -> int:
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", s or "")
    if not m:
        return 0
    h, mi, se = (float(x or 0) for x in m.groups())
    return int((h * 3600 + mi * 60 + se) * 1000)


def fetch_apple(country: str, apl_id: str, resolver: Resolver, token=None) -> Dict:
    changed = "Apple Music cambió su página; usa la opción de texto/CSV."
    _progress(token, stage="reading", done=0, total=0)
    try:
        r = requests.get(f"https://music.apple.com/{country}/playlist/x/{apl_id}", headers=UA, timeout=25)
    except Exception as e:
        raise HTTPException(502, f"No pude abrir Apple Music: {e}")
    if r.status_code == 404:
        raise HTTPException(404, "Apple Music no encontró esa playlist (¿es pública?)")
    if r.status_code != 200:
        raise HTTPException(502, f"Apple Music respondió {r.status_code}. {changed}")
    h = r.content.decode("utf-8", "replace")   # Apple no manda charset: r.text lo leería como latin-1
    name, owner, total, items = None, None, 0, []
    m = re.search(r'<script[^>]*id="serialized-server-data"[^>]*>(.*?)</script>', h, re.S)
    if m:
        try:
            data = json.loads(m.group(1))
            pages = data.get("data") if isinstance(data, dict) else data
            sections = (pages[0].get("data") or {}).get("sections") or []
            for s in sections:
                kind = s.get("itemKind") or ""
                for it in s.get("items") or []:
                    if kind == "containerDetailHeaderLockup":
                        name = it.get("title") or name
                        subs = it.get("subtitleLinks") or []
                        owner = subs[0].get("title") if subs else owner
                        total = int(it.get("trackCount") or 0)
                    elif kind == "trackLockup":
                        title = it.get("title") or ""
                        artist = it.get("artistName") or ", ".join(x.get("title", "") for x in it.get("subtitleLinks") or [])
                        album = ((it.get("tertiaryLinks") or [{}])[0] or {}).get("title") or ""
                        album = re.sub(r"\s+-\s+(Single|EP)$", "", album)
                        art = (((it.get("artwork") or {}).get("dictionary") or {}).get("url") or "")
                        art = art.replace("{w}", "200").replace("{h}", "200").replace("{f}", "jpg") or None
                        if title:
                            items.append({"name": title, "artists": split_artists(artist) if artist else [],
                                          "album": album, "duration_ms": int(it.get("duration") or 0),
                                          "album_art": art})
        except Exception as e:
            print(f"importar: Apple serialized-server-data ilegible: {e}")
            items = []
    if not items:  # plan B: JSON-LD (no trae artista; se busca solo por título y duración)
        for blob in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', h, re.S):
            try:
                j = json.loads(blob)
            except Exception:
                continue
            if j.get("@type") != "MusicPlaylist":
                continue
            name = name or j.get("name")
            owner = owner or ((j.get("author") or {}).get("name") if isinstance(j.get("author"), dict) else None)
            total = total or int(j.get("numTracks") or 0)
            for t in j.get("track") or []:
                if t.get("name"):
                    by = t.get("byArtist")
                    by = by[0] if isinstance(by, list) and by else by
                    artist = by.get("name") if isinstance(by, dict) else ""
                    items.append({"name": t["name"], "artists": split_artists(artist) if artist else [],
                                  "album": "", "duration_ms": _iso_dur(t.get("duration")), "album_art": None})
    if not items:
        raise HTTPException(502, changed)
    tracks, dups = resolver.resolve_all(items, token)
    total = max(total, len(items))
    return {"name": name or "Playlist de Apple Music", "owner": owner, "total": total, "tracks": tracks,
            "skipped": max(0, total - len(tracks)), "source": "apple", "complete": len(items) >= total}


# ---------------------------------------------------------------- texto / CSV
def _hkey(s: str) -> str:
    return norm(s).replace(" ", "")


HEADERS = {
    "uri": {"trackuri", "spotifyuri", "uri", "spotifyid", "trackid", "iddespotify", "spotifytrackid"},
    "name": {"trackname", "name", "title", "song", "songname", "track", "titulo", "cancion", "nombre",
             "nombredelacancion", "tema", "nombredecancion"},
    "artist": {"artistnames", "artistname", "artist", "artists", "artista", "artistas", "interprete",
               "interpretes", "nombredelartista"},
    "album": {"albumname", "album", "disco", "nombredelalbum"},
    "duration": {"durationms", "duration", "duracion", "duracionms", "length", "time", "tiempo"},
    "isrc": {"isrc"},
}


def _dur_value(v: str, header: str) -> int:
    v = (v or "").strip()
    if not v:
        return 0
    m = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{2})", v)
    if m:
        h, mi, s = (int(x or 0) for x in m.groups())
        return (h * 3600 + mi * 60 + s) * 1000
    try:
        n = float(v.replace(",", "."))
    except ValueError:
        return 0
    return int(n if ("ms" in header or n > 10_000) else n * 1000)


def _spotify_id(v: str) -> Optional[str]:
    m = re.search(r"(?:spotify:track:|open\.spotify\.com/(?:intl-[a-z]+/)?track/)([A-Za-z0-9]{22})", v or "")
    if m:
        return m.group(1)
    v = (v or "").strip()
    return v if re.fullmatch(r"[A-Za-z0-9]{22}", v) else None


def parse_csv(text: str) -> Optional[Tuple[List[Dict], List[str]]]:
    """Si el texto parece un CSV con encabezados conocidos, devuelve (items, ignoradas)."""
    first = text.lstrip("﻿").splitlines()[0] if text.strip() else ""
    delim = max([",", ";", "\t"], key=first.count)
    if not first.count(delim):
        return None
    try:
        rows = list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=delim))
    except csv.Error:
        return None
    head = [_hkey(h) for h in rows[0]]
    cols = {}
    for field, names in HEADERS.items():
        for i, h in enumerate(head):
            if h in names and field not in cols:
                cols[field] = i
    if "name" not in cols and "uri" not in cols:
        return None
    if "name" in cols and "artist" not in cols and "uri" not in cols and "isrc" not in cols:
        return None
    dur_header = rows[0][cols["duration"]].lower() if "duration" in cols else ""
    items, ignored = [], []

    def cell(r, f):
        i = cols.get(f)
        return r[i].strip() if i is not None and i < len(r) else ""

    for r in rows[1:]:
        if not any(x.strip() for x in r):
            continue
        name, artist = cell(r, "name"), cell(r, "artist")
        sid = _spotify_id(cell(r, "uri"))
        if not name and not sid:
            ignored.append(delim.join(r)[:120])
            continue
        # Exportify separa varios artistas con coma dentro de la celda
        arts = [a.strip() for a in re.split(r"\s*[,;]\s*", artist) if a.strip()] if artist else []
        items.append({"name": name, "artists": arts, "album": cell(r, "album"),
                      "duration_ms": _dur_value(cell(r, "duration"), dur_header),
                      "isrc": cell(r, "isrc"), "spotify_id": sid if name else None,
                      "_sid_only": sid if not name else None})
    return items, ignored


_LINE_SEP = re.compile(r"\s+[-–—]\s+|\s*\t\s*")


def parse_lines(text: str) -> Tuple[List[Dict], List[str]]:
    items, ignored = [], []
    for raw in text.splitlines()[:MAX_TEXT_LINES]:
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith(("#", "//")):
            continue
        sid = _spotify_id(line) if "spotify" in line else None
        if sid:
            items.append({"name": "", "artists": [], "_sid_only": sid})
            continue
        line = re.sub(r"^\s*(?:\d{1,4}\s*[.)\-:]|[-*•·])\s+", "", line)   # "1. ", "- ", "• "
        line = line.strip().strip('"').strip()
        if not re.search(r"[^\W\d_]", line) or len(line) < 3 or re.match(r"https?://", line):
            ignored.append(raw.strip()[:120])
            continue
        parts = [p.strip().strip('"') for p in _LINE_SEP.split(line, maxsplit=1)]
        if len(parts) < 2:
            m = re.match(r"^(.+?)\s+(?:by|de|por)\s+(.+)$", line, re.I) if " by " in line.lower() else None
            if m:
                parts = [m.group(2), m.group(1)]          # "Canción by Artista"
            elif "," in line:
                parts = [p.strip() for p in line.split(",", 1)]
        parts = [p for p in parts if p]
        if len(parts) < 2:
            # sin separador ("Queen Bohemian Rhapsody"): se intenta como búsqueda libre; si no aparece, se ignora
            items.append({"name": line, "artists": [], "album": "", "duration_ms": 0, "free": line,
                          "free_only": True, "_line": raw.strip()})
            continue
        a, b = parts[0], parts[1]
        # quita la duración al final ("Artista - Canción 3:45")
        dur = 0
        md = re.search(r"\s*[\(\[]?(\d{1,2}):(\d{2})[\)\]]?\s*$", b)
        if md:
            dur = (int(md.group(1)) * 60 + int(md.group(2))) * 1000
            b = b[:md.start()].strip()
        items.append({"name": b, "artists": split_artists(a), "album": "", "duration_ms": dur,
                      "alts": [(a, split_artists(b))], "_line": raw.strip()})
    return items, ignored


def fetch_text(text: str, name: Optional[str], resolver: Resolver, catalog, token=None) -> Tuple[str, Dict]:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise HTTPException(400, "La lista está vacía")
    if len(text) > 2_000_000:
        raise HTTPException(400, "La lista es demasiado grande (máximo 2 MB)")
    parsed = parse_csv(text)
    kind = "csv"
    if parsed is None:
        parsed, kind = parse_lines(text), "texto"
    items, ignored = parsed
    # filas que solo traen un link/URI de Spotify: se completan con la API de Spotify
    for it in items:
        sid = it.pop("_sid_only", None)
        if sid:
            info = None
            if catalog.spotify:
                try:
                    info = catalog.spotify.get_track_details(sid)
                except Exception:
                    info = None
            if info:
                it.update(name=info["name"], artists=info.get("artists") or [], album=info.get("album") or "",
                          duration_ms=info.get("duration_ms") or 0, spotify_id=sid, album_art=info.get("album_art"))
            else:
                it["name"] = ""
    ignored += ["Link de Spotify sin datos" for it in items if not it.get("name")]
    items = [it for it in items if it.get("name")]
    if not items:
        raise HTTPException(400, "No encontré canciones en el texto. Usa una línea por canción: Artista - Canción")
    digest = hashlib.sha1(norm(text).encode()).hexdigest()[:10]
    pid = "txt:" + digest
    tracks, dups = resolver.resolve_all(items, token)
    ignored += [t["name"][:120] for t in tracks if t.get("drop")]
    tracks = [t for t in tracks if not t.get("drop")]
    if not tracks:
        raise HTTPException(400, "No encontré canciones en el texto. Usa una línea por canción: Artista - Canción")
    title = (name or "").strip()[:120] or f"Lista importada ({time.strftime('%d/%m/%Y')})"
    return pid, {"name": title, "owner": "CSV" if kind == "csv" else "Texto", "total": len(tracks) + dups,
                 "tracks": tracks, "skipped": len(ignored) + dups, "source": "texto", "complete": True,
                 "ignored": ignored[:20]}


# ---------------------------------------------------------------- despacho
def detect(url: str) -> Optional[Tuple[str, Tuple]]:
    url = (url or "").strip()
    m = YT_RE.search(url)
    if m:
        return "youtube", (m.group(1) or m.group(2),)
    m = re.fullmatch(r"yt:([A-Za-z0-9_-]+)", url)
    if m:
        return "youtube", (m.group(1),)
    m = APPLE_RE.search(url)
    if m:
        return "apple", (m.group(1), m.group(2))
    m = re.fullmatch(r"am:([a-z]{2}):(pl\.[A-Za-z0-9_-]+)", url)
    if m:
        return "apple", (m.group(1), m.group(2))
    if re.fullmatch(r"txt:[0-9a-f]+", url):
        raise HTTPException(400, "Esta lista se importó desde texto/CSV: para actualizarla, pégala o súbela otra vez.")
    return None


def _fetch_external_sync(catalog, youtube_service, url: str, text: Optional[str], name: Optional[str],
                         token: Optional[str]) -> Optional[Tuple[str, Dict]]:
    resolver = Resolver(catalog)
    try:
        if text and text.strip():
            return fetch_text(text, name, resolver, catalog, token)
        d = detect(url)
        if not d:
            return None
        kind, args = d
        if kind == "youtube":
            data = fetch_youtube(args[0], getattr(youtube_service, "ytmusic", None), resolver, token)
            return "yt:" + args[0], data
        data = fetch_apple(args[0], args[1], resolver, token)
        return f"am:{args[0]}:{args[1]}", data
    finally:
        _progress(token, stage="saving")


async def fetch_external(catalog, youtube_service, url: str, text: Optional[str] = None,
                         name: Optional[str] = None, token: Optional[str] = None) -> Optional[Tuple[str, Dict]]:
    """(pid, data) si el link/texto es de una fuente nueva; None si es Spotify/Deezer.
    Corre en un hilo aparte para no bloquear la app mientras se resuelven las canciones."""
    if not (text and text.strip()) and not detect(url):
        return None
    return await run_in_threadpool(_fetch_external_sync, catalog, youtube_service, url, text, name, token)


# ---------------------------------------------------------------- descargas
def wrap_download(download_and_process: Callable, reverse_download_and_process: Callable) -> Callable:
    """Envuelve ``download_and_process`` para la página de playlists:
    - si la canción vino de YouTube con un video ya elegido, usa ese video (salvo que el usuario elija otro);
    - las canciones sin ID del catálogo (``q:…``) se descargan del video elegido con metadatos básicos."""
    from utils.job_store import upsert_job

    def lookup(track_id: str) -> Optional[Dict]:
        from playlist_feature import _db
        try:
            with _db() as c:
                row = c.execute("SELECT * FROM ptracks WHERE track_id=? ORDER BY (video_id IS NULL), updated_at DESC "
                                "LIMIT 1", (track_id,)).fetchone()
            return dict(row) if row else None
        except Exception as e:
            print(f"importar: no pude leer la canción {track_id}: {e}")
            return None

    def run(track_id: str, location: str = "navidrome", video_id: Optional[str] = None, *args, **kwargs):
        row = lookup(track_id)
        if not video_id and row and row.get("video_id"):
            video_id = row["video_id"]
        if not str(track_id).startswith("q:"):
            return download_and_process(track_id, location, video_id, *args, **kwargs)
        if not row:
            upsert_job(track_id, status="error", message="Canción no encontrada", progress=0)
            return
        if not video_id:
            upsert_job(track_id, status="error", progress=0,
                       message="No está en el catálogo: usa «Buscar» para elegir el video.")
            return
        arts = json.loads(row.get("artists_json") or "[]")
        artist = ", ".join(arts) or row.get("artist") or "Desconocido"
        meta = {"name": row["name"], "artist": artist, "album_artist": (arts or [artist])[0],
                "album": row.get("album") or "", "album_art": row.get("album_art")}
        return reverse_download_and_process(track_id, f"https://www.youtube.com/watch?v={video_id}",
                                            location, None, meta)

    return run
