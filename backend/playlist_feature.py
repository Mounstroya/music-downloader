"""Revisión de playlists de Spotify contra la biblioteca de Navidrome.

Permite importar una playlist, detectar qué canciones ya están en la carpeta de
música, marcarlas a mano, buscar candidatos en YouTube, descargar y aprobar o
rechazar lo descargado.
"""
import asyncio
import difflib
import inspect
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import requests
import spotipy
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from spotipy.cache_handler import CacheFileHandler
from spotipy.oauth2 import SpotifyOAuth

import config
from utils.job_store import get_job, upsert_job

DB_PATH = os.path.join(config.DOWNLOAD_DIR, "playlists.db")
TOKEN_PATH = os.path.join(config.DOWNLOAD_DIR, ".spotify_user_token")
AUDIO_EXT = {".mp3", ".flac", ".m4a", ".opus", ".ogg", ".wav", ".aac"}
MIME = {".mp3": "audio/mpeg", ".flac": "audio/flac", ".m4a": "audio/mp4", ".opus": "audio/ogg",
        ".ogg": "audio/ogg", ".wav": "audio/wav", ".aac": "audio/aac"}

# Estados que se recalculan solos al reescanear la biblioteca
AUTO_STATES = ("missing", "auto", "maybe")
ACTIVE_STATES = ("queued", "downloading")

_db_lock = threading.Lock()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


# ---- sincronización automática (todo apagado por defecto)
AUTO_SYNC_ENABLED = os.getenv("AUTO_SYNC_ENABLED", "false").strip().lower() in ("1", "true", "yes", "si", "sí")
AUTO_SYNC_MIN_HOURS = 6
AUTO_SYNC_DEFAULT_HOURS = 24
AUTO_SYNC_PAUSE_S = max(30, _env_int("AUTO_SYNC_PAUSE_SECONDS", 120))        # pausa entre playlists
AUTO_SYNC_MAX_DOWNLOADS = max(0, min(50, _env_int("AUTO_SYNC_MAX_DOWNLOADS", 20)))
AUTO_SYNC_STARTUP_DELAY_S = 15 * 60       # nada de peticiones justo al arrancar el contenedor
AUTO_SYNC_MAX_BACKOFF_S = 7 * 24 * 3600
# SOLO PRUEBAS: intervalo en minutos en vez de horas (y esperas cortas). Vacío en producción.
AUTO_SYNC_TEST_MINUTES = _env_int("AUTO_SYNC_TEST_MINUTES", 0)
# Fuentes que se pueden volver a leer sin intervención del usuario
SYNCABLE_SOURCES = {"cuenta", "embed", "deezer"}
UPGRADE_MIN_KBPS = _env_int("UPGRADE_MIN_KBPS", 160)
_scheduler_started = False
_bulk = {"running": False, "playlist_id": None, "done": 0, "total": 0, "stop": False}


# ---------------------------------------------------------------- base de datos
def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db_lock, _db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS playlists(
            id TEXT PRIMARY KEY, name TEXT, owner TEXT, total INTEGER,
            loaded INTEGER, source TEXT, imported_at INTEGER);
        CREATE TABLE IF NOT EXISTS ptracks(
            playlist_id TEXT, track_id TEXT, position INTEGER, name TEXT, artist TEXT,
            artists_json TEXT, album TEXT, duration_ms INTEGER, album_art TEXT,
            status TEXT DEFAULT 'missing', match_path TEXT, file_path TEXT, note TEXT,
            updated_at INTEGER, PRIMARY KEY(playlist_id, track_id));
        """)
        _migrate_sync_upgrade(c)


def _ensure_column(c, table: str, column: str, decl: str):
    if column not in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate_sync_upgrade(c):
    """Columnas/tabla para sincronización y mejora de versiones (solo agrega, nunca borra)."""
    # Configuración y estado de sincronización en tabla aparte: `import_playlist` hace
    # INSERT OR REPLACE sobre `playlists` y borraría cualquier columna nueva de esa tabla.
    c.execute("""CREATE TABLE IF NOT EXISTS playlist_sync(
        playlist_id TEXT PRIMARY KEY,
        auto_sync INTEGER DEFAULT 0,        -- apagada por defecto
        interval_hours INTEGER DEFAULT 24,
        auto_download INTEGER DEFAULT 0,    -- apagada por defecto
        sync_url TEXT,                      -- link para volver a leerla (si el id no basta)
        last_sync_at INTEGER, last_sync_ok INTEGER, last_sync_result TEXT, last_sync_error TEXT,
        fail_count INTEGER DEFAULT 0, next_sync_at INTEGER)""")
    for col, decl in (("is_new", "INTEGER DEFAULT 0"),        # agregada en una sincronización
                      ("added_at", "INTEGER"),
                      ("lib_bitrate", "INTEGER"),             # kbps del archivo en la biblioteca
                      ("lib_length", "INTEGER"),              # segundos
                      ("upgrade_reason", "TEXT"),             # por qué conviene otra versión
                      ("upgrade_ignored", "INTEGER DEFAULT 0"),
                      ("old_path", "TEXT")):                  # versión anterior al buscar una mejor
        _ensure_column(c, "ptracks", col, decl)


# ---------------------------------------------------------------- normalización
_VERSION_WORDS = r"remaster|version|versión|mix|edit|live|en vivo|mono|stereo|radio|from|feat|ft\.|con |with |deluxe|bonus|acoustic|acústic"


LIVE_RE = re.compile(r"\b(live|en vivo|en directo|ao vivo|unplugged|unstaged|concert|concierto|tour)\b", re.I)

# Versiones "distintas" a la de estudio: si el archivo de la biblioteca es una de estas y la
# canción de la playlist no, se sugiere buscar una mejor versión.
KIND_RES = [
    ("en vivo", LIVE_RE),
    ("acústica", re.compile(r"\b(acoustic|acustic[oa]|acústic[oa])\b", re.I)),
    ("remix", re.compile(r"\b(remix|rmx|remixed|bootleg|mashup|(club|extended|dub|dance) mix)\b", re.I)),
    ("karaoke", re.compile(r"\bkaraoke\b", re.I)),
    ("instrumental", re.compile(r"\binstrumental\b", re.I)),
    ("sped up / slowed", re.compile(r"\b(sped up|speed up|slowed|nightcore|8d audio)\b", re.I)),
]
LOSSY_EXT = {".mp3", ".m4a", ".aac"}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("&", " and ").replace("$", "s")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def base_title(title: str) -> str:
    t = title or ""
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", t)          # (feat. X), [Remastered]
    t = re.sub(rf"\s+-\s+.*({_VERSION_WORDS}).*$", "", t, flags=re.I)  # - Remastered 2011
    return norm(t) or norm(title)


# ---------------------------------------------------------------- biblioteca
def read_audio(p: str, MFile=None) -> Dict:
    """Etiquetas básicas, duración (s) y bitrate (kbps) de un archivo de audio."""
    if MFile is None:
        from mutagen import File as MFile
    out = {"title": "", "artist": "", "album": "", "length": 0, "bitrate": 0, "tagged": False}
    try:
        mf = MFile(p, easy=True)
        if mf is not None:
            tags = mf.tags or {}
            out["title"] = (tags.get("title") or [""])[0]
            out["artist"] = " ; ".join((tags.get("artist") or []) + (tags.get("albumartist") or []))
            out["album"] = (tags.get("album") or [""])[0]
            out["length"] = int(getattr(mf.info, "length", 0) or 0)
            out["bitrate"] = int((getattr(mf.info, "bitrate", 0) or 0) / 1000)
            out["tagged"] = bool(out["title"].strip() and out["artist"].strip())
    except Exception:
        pass
    return out


def upgrade_reasons(want_name: str, want_ms: int, it: Dict, path: str, min_kbps: int,
                    skip_bitrate: bool = False) -> List[str]:
    """Razones legibles por las que la versión de la biblioteca podría mejorarse."""
    reasons = []
    rel_parts = Path(it.get("rel") or path).parts
    # se ignora la carpeta del artista (p. ej. la banda "Live") y se mira título, álbum y archivo
    hay = " ".join([it.get("tag_title") or "", it.get("tag_album") or "", *rel_parts[1:]])
    for label, rx in KIND_RES:
        if rx.search(hay) and not rx.search(want_name or ""):
            reasons.append(f"Versión {label}")
            break
    kbps = it.get("bitrate") or 0
    if not skip_bitrate and kbps and kbps < min_kbps and Path(path).suffix.lower() in LOSSY_EXT:
        reasons.append(f"Bitrate bajo ({kbps} kbps)")
    length = it.get("length") or 0
    if want_ms and length and abs(length - want_ms / 1000) > 20:
        diff = int(length - want_ms / 1000)
        reasons.append(f"Duración distinta ({'+' if diff > 0 else '−'}{abs(diff)} s)")
    if not it.get("tagged"):
        reasons.append("Archivo sin etiquetas")
    return reasons


class Library:
    def __init__(self, root: str):
        self.root = root
        self.items: List[Dict] = []
        self.by_title: Dict[str, List[Dict]] = {}
        self.scanned_at = 0
        self.by_path: Dict[str, Dict] = {}
        self._lock = threading.Lock()

    def scan(self):
        from mutagen import File as MFile
        items = []
        for dirpath, _, files in os.walk(self.root):
            for fn in files:
                p = os.path.join(dirpath, fn)
                if Path(fn).suffix.lower() not in AUDIO_EXT:
                    continue
                info = read_audio(p, MFile)
                title, artist, length = info["title"], info["artist"], info["length"]
                stem = Path(fn).stem
                if not title:
                    title = stem.split(" - ", 1)[1] if " - " in stem else stem
                    title = re.sub(r"^\d+[\s.\-_]+", "", title)
                rel = os.path.relpath(p, self.root)
                items.append({
                    "path": p, "rel": rel, "title": title, "bt": base_title(title),
                    "artist_n": norm(artist + " " + rel.replace(os.sep, " ")), "length": length,
                    "bitrate": info["bitrate"], "tagged": info["tagged"], "tag_title": info["title"],
                    "tag_album": info["album"],
                })
        idx: Dict[str, List[Dict]] = {}
        for it in items:
            idx.setdefault(it["bt"], []).append(it)
        with self._lock:
            self.items, self.by_title, self.scanned_at = items, idx, int(time.time())
            self.by_path = {it["path"]: it for it in items}

    def match(self, name: str, artists: List[str], duration_ms: int):
        """Devuelve (tipo, archivo) con tipo 'auto' (seguro), 'maybe' (posible) o None."""
        if not self.scanned_at:
            self.scan()
        bt = base_title(name)
        arts = [norm(a) for a in artists if norm(a)]

        def artist_ok(it):
            return any(a in it["artist_n"] for a in arts)

        def dur_ok(it):
            return not duration_ms or not it["length"] or abs(it["length"] - duration_ms / 1000) <= 12

        want_live = bool(LIVE_RE.search(name))

        def same_kind(it):  # en vivo vs estudio: si no coincide, que lo decida el usuario
            return bool(LIVE_RE.search(it["rel"])) == want_live

        same = self.by_title.get(bt, [])
        # preferir la versión de estudio si hay varias
        same = sorted(same, key=lambda it: (not same_kind(it), not dur_ok(it)))
        for it in same:
            if artist_ok(it) and dur_ok(it) and same_kind(it):
                return "auto", it
        for it in same:
            if artist_ok(it):
                return "maybe", it
        # título parecido del mismo artista
        best, best_r = None, 0.0
        for it in self.items:
            if not artist_ok(it):
                continue
            r = difflib.SequenceMatcher(None, bt, it["bt"]).ratio()
            if r > best_r:
                best, best_r = it, r
        if best and best_r >= 0.8:
            return "maybe", best
        # mismo título exacto, otro artista (p. ej. archivo sin etiquetas)
        for it in same:
            if dur_ok(it) and len(bt) > 3:
                return "maybe", it
        return None, None


# ---------------------------------------------------------------- Spotify
def parse_playlist_id(url: str) -> str:
    """Devuelve el ID interno de la playlist: "<id de Spotify>" o "dz:<id de Deezer>"."""
    url = (url or "").strip()
    if re.search(r"(^|\.)(deezer\.page\.link|link\.deezer\.com)/", url):
        try:  # links cortos de "Compartir" en la app de Deezer
            url = requests.get(url, timeout=15, allow_redirects=True).url
        except Exception:
            raise HTTPException(400, "No pude abrir ese link corto de Deezer")
    m = re.search(r"deezer\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?playlist/(\d+)", url)
    if m:
        return "dz:" + m.group(1)
    if re.fullmatch(r"dz:\d+", url):
        return url
    m = re.search(r"playlist[/:]([A-Za-z0-9]{22})", url)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{22}", url):
        return url
    raise HTTPException(400, "No reconozco ese link. Pega el link de una playlist de Spotify o Deezer.")


def _oauth() -> SpotifyOAuth:
    return SpotifyOAuth(
        client_id=config.SPOTIFY_CLIENT_ID,
        client_secret=config.SPOTIFY_CLIENT_SECRET,
        redirect_uri=config.SPOTIFY_REDIRECT_URI,
        scope="playlist-read-private playlist-read-collaborative",
        cache_handler=CacheFileHandler(cache_path=TOKEN_PATH),
        open_browser=False,
        requests_timeout=15,
    )


def user_client() -> Optional[spotipy.Spotify]:
    oa = _oauth()
    tok = oa.validate_token(oa.cache_handler.get_cached_token())
    if not tok:
        return None
    return spotipy.Spotify(auth=tok["access_token"], requests_timeout=15, retries=3)


def fetch_with_user(sp: spotipy.Spotify, pid: str) -> Dict:
    meta = sp.playlist(pid, fields="name,owner.display_name", market="MX")
    tracks, offset, skipped = [], 0, 0
    while True:
        page = sp._get(f"playlists/{pid}/items", limit=50, offset=offset, market="MX")
        for it in page.get("items", []):
            t = it.get("item") or it.get("track")
            if not t or t.get("type", "track") != "track" or not t.get("id") or it.get("is_local"):
                skipped += 1
                continue
            imgs = (t.get("album") or {}).get("images") or []
            tracks.append({
                "id": t["id"], "name": t["name"],
                "artists": [a["name"] for a in t.get("artists", [])],
                "album": (t.get("album") or {}).get("name", ""),
                "duration_ms": t.get("duration_ms") or 0,
                "album_art": imgs[-1]["url"] if imgs else None,
            })
        offset += 50
        if not page.get("next"):
            total = page.get("total", len(tracks) + skipped)
            break
    return {"name": meta.get("name"), "owner": (meta.get("owner") or {}).get("display_name"),
            "total": total, "tracks": tracks, "skipped": skipped, "source": "cuenta"}


def fetch_from_embed(pid: str) -> Dict:
    ua = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
    h = requests.get(f"https://open.spotify.com/embed/playlist/{pid}", headers=ua, timeout=20).text
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', h, re.S)
    if not m:
        raise HTTPException(502, "Spotify no devolvió la playlist (¿es pública?)")
    ent = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    tracks = []
    for t in ent.get("trackList", []):
        uri = t.get("uri") or ""
        if not uri.startswith("spotify:track:"):
            continue
        tracks.append({
            "id": uri.split(":")[-1], "name": t.get("title", ""),
            "artists": [a.strip() for a in (t.get("subtitle") or "").split(",") if a.strip()],
            "album": "", "duration_ms": t.get("duration") or 0, "album_art": None,
        })
    total = len(tracks)
    try:
        # con un User-Agent "simple" Spotify devuelve la página con metadatos (… · 508 items)
        page = requests.get(f"https://open.spotify.com/playlist/{pid}",
                            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}, timeout=20).text
        mt = re.search(r"·\s*([\d,.]+)\s*(items|songs|canciones|elementos)", page)
        if mt:
            total = int(re.sub(r"[^\d]", "", mt.group(1)))
    except Exception:
        pass
    owner = None
    if ent.get("subtitle"):
        owner = ent["subtitle"]
    return {"name": ent.get("name"), "owner": owner, "total": total, "tracks": tracks,
            "skipped": 0, "source": "embed"}


# ---------------------------------------------------------------- router
class ImportReq(BaseModel):
    url: str


class AuthCodeReq(BaseModel):
    url: str


class MarkReq(BaseModel):
    status: str


class DownloadReq(BaseModel):
    video_id: Optional[str] = None


def build_router(catalog, youtube_service, download_and_process: Callable, templates) -> APIRouter:
    init_db()
    lib = Library(config.NAVIDROME_MUSIC_PATH)
    r = APIRouter()

    def music_root():
        return os.path.realpath(config.NAVIDROME_MUSIC_PATH)

    def inside_music(p: Optional[str]) -> bool:
        if not p:
            return False
        rp = os.path.realpath(p)
        return rp.startswith(music_root() + os.sep) and os.path.isfile(rp)

    def set_track(pid, tid, **f):
        f["updated_at"] = int(time.time())
        cols = ", ".join(f"{k}=?" for k in f)
        with _db_lock, _db() as c:
            c.execute(f"UPDATE ptracks SET {cols} WHERE playlist_id=? AND track_id=?",
                      (*f.values(), pid, tid))

    def get_row(pid, tid):
        with _db() as c:
            row = c.execute("SELECT * FROM ptracks WHERE playlist_id=? AND track_id=?", (pid, tid)).fetchone()
        if not row:
            raise HTTPException(404, "Canción no encontrada en la playlist")
        return dict(row)

    def sync_jobs(pid):
        """Actualiza las canciones en descarga según el estado del job."""
        with _db() as c:
            rows = c.execute("SELECT track_id, status FROM ptracks WHERE playlist_id=? AND status IN (?,?)",
                             (pid, *ACTIVE_STATES)).fetchall()
        for row in rows:
            job = get_job(row["track_id"]) or {}
            st = job.get("status")
            if st == "completed" and job.get("file_path"):
                set_track(pid, row["track_id"], status="downloaded", file_path=job["file_path"], note=None)
            elif st == "error":
                old = get_row(pid, row["track_id"]).get("old_path")
                if old and inside_music(old):  # falló la versión nueva: se queda la anterior
                    set_track(pid, row["track_id"], status="have", match_path=old, old_path=None,
                              note=f"No se pudo descargar la otra versión: {job.get('message')}")
                else:
                    set_track(pid, row["track_id"], status="error", note=job.get("message"))
            elif st == "processing" and row["status"] != "downloading":
                set_track(pid, row["track_id"], status="downloading")

    def rematch(pid):
        with _db() as c:
            rows = c.execute(
                f"SELECT track_id,name,artists_json,duration_ms FROM ptracks WHERE playlist_id=? AND status IN ({','.join('?'*len(AUTO_STATES))})",
                (pid, *AUTO_STATES)).fetchall()
        for row in rows:
            kind, it = lib.match(row["name"], json.loads(row["artists_json"] or "[]"), row["duration_ms"])
            set_track(pid, row["track_id"], status=kind or "missing", match_path=it["path"] if it else None)
        assess_upgrades(pid)

    # ---- mejora de versiones
    FOUND_STATES = ("auto", "have", "approved", "maybe")

    def assess_upgrades(pid, tids=None):
        """Guarda bitrate/duración del archivo encontrado y si conviene buscar otra versión."""
        q = f"SELECT * FROM ptracks WHERE playlist_id=?"
        args = [pid]
        if tids:
            q += f" AND track_id IN ({','.join('?' * len(tids))})"
            args += list(tids)
        with _db() as c:
            rows = [dict(x) for x in c.execute(q, args)]
        updates = []
        for row in rows:
            fields = {"lib_bitrate": None, "lib_length": None, "upgrade_reason": None}
            path = None
            if row["status"] in FOUND_STATES:
                path = row.get("match_path")
                if row["status"] == "approved" and row.get("file_path"):
                    path = row["file_path"]
            if path and os.path.isfile(path):
                it = lib.by_path.get(path)
                if it is None:  # biblioteca sin escanear o archivo nuevo: leerlo directo
                    info = read_audio(path)
                    it = {"rel": os.path.relpath(path, config.NAVIDROME_MUSIC_PATH), "tag_title": info["title"],
                          "tag_album": info["album"], **info}
                # una descarga aprobada por el usuario ya es la calidad que esta app produce
                skip_br = row["status"] == "approved" and path == row.get("file_path")
                reasons = upgrade_reasons(row["name"], row["duration_ms"], it, path, UPGRADE_MIN_KBPS, skip_br)
                fields = {"lib_bitrate": it.get("bitrate") or None, "lib_length": it.get("length") or None,
                          "upgrade_reason": " · ".join(reasons) or None}
            if any(row.get(k) != v for k, v in fields.items()):
                updates.append((fields["lib_bitrate"], fields["lib_length"], fields["upgrade_reason"],
                                pid, row["track_id"]))
        if updates:
            with _db_lock, _db() as c:
                c.executemany("UPDATE ptracks SET lib_bitrate=?, lib_length=?, upgrade_reason=? "
                              "WHERE playlist_id=? AND track_id=?", updates)

    def queue_download(pid, tid, video_id=None):
        upsert_job(tid, status="queued", message="En cola (playlist)", progress=0, stage="queued")
        set_track(pid, tid, status="queued", note=None)
        return video_id

    # ---- página
    @r.get("/playlist", response_class=HTMLResponse)
    async def page(request: Request):
        return templates.TemplateResponse("playlist.html", context={"request": request})

    # ---- cuenta de Spotify
    @r.get("/api/spotify/status")
    async def sp_status():
        try:
            sp = user_client()
            if not sp:
                return {"connected": False, "redirect_uri": config.SPOTIFY_REDIRECT_URI}
            me = sp.current_user()
            return {"connected": True, "user": me.get("display_name") or me.get("id")}
        except Exception as e:
            return {"connected": False, "error": str(e), "redirect_uri": config.SPOTIFY_REDIRECT_URI}

    @r.get("/api/spotify/auth-url")
    async def sp_auth_url():
        return {"url": _oauth().get_authorize_url(), "redirect_uri": config.SPOTIFY_REDIRECT_URI}

    def _exchange(code: str):
        try:
            _oauth().get_access_token(code, as_dict=False, check_cache=False)
        except Exception as e:
            raise HTTPException(400, f"Spotify rechazó el código: {e}")

    @r.post("/api/spotify/auth-code")
    async def sp_auth_code(req: AuthCodeReq):
        txt = req.url.strip()
        code = parse_qs(urlparse(txt).query).get("code", [None])[0] if "code=" in txt else txt
        if not code:
            raise HTTPException(400, "No encontré el código en ese link")
        _exchange(code)
        return await sp_status()

    @r.get("/callback", response_class=HTMLResponse)
    async def sp_callback(code: Optional[str] = None, error: Optional[str] = None):
        if error or not code:
            return HTMLResponse(f"<p>Spotify devolvió un error: {error}</p>", status_code=400)
        _exchange(code)
        return HTMLResponse('<meta http-equiv="refresh" content="1;url=/playlist"><p>Cuenta conectada ✔</p>')

    @r.post("/api/spotify/logout")
    async def sp_logout():
        if os.path.exists(TOKEN_PATH):
            os.remove(TOKEN_PATH)
        return {"connected": False}

    # ---- playlists
    @r.get("/api/playlists")
    async def list_playlists():
        with _db() as c:
            return [dict(x) for x in c.execute("SELECT * FROM playlists ORDER BY imported_at DESC")]

    @r.post("/api/playlists/import")
    async def import_playlist(req: ImportReq):
        pid = parse_playlist_id(req.url)
        data, err = None, None
        if pid.startswith("dz:"):
            try:
                data = catalog.deezer.get_playlist(pid)
            except Exception as e:
                raise HTTPException(502, f"Deezer no devolvió la playlist (¿es pública?): {e}")
            data = {**data, "skipped": data["total"] - len(data["tracks"]), "source": "deezer"}
        else:
            try:
                sp = user_client()
                if sp:
                    data = fetch_with_user(sp, pid)
            except Exception as e:
                err = str(e)
            if data is None:
                data = fetch_from_embed(pid)
        lib.scan()
        now = int(time.time())
        with _db_lock, _db() as c:
            c.execute("INSERT OR REPLACE INTO playlists VALUES (?,?,?,?,?,?,?)",
                      (pid, data["name"], data["owner"], data["total"], len(data["tracks"]), data["source"], now))
            old = {x["track_id"]: dict(x) for x in c.execute("SELECT * FROM ptracks WHERE playlist_id=?", (pid,))}
            keep = set()
            for pos, t in enumerate(data["tracks"]):
                keep.add(t["id"])
                if t["id"] in old:
                    o = old[t["id"]]
                    c.execute("""UPDATE ptracks SET position=?, name=?, artist=?, artists_json=?,
                                 album=COALESCE(NULLIF(?,''),album), duration_ms=?, album_art=COALESCE(?,album_art)
                                 WHERE playlist_id=? AND track_id=?""",
                              (pos, t["name"], ", ".join(t["artists"]), json.dumps(t["artists"]), t["album"],
                               t["duration_ms"], t["album_art"], pid, t["id"]))
                else:
                    c.execute("""INSERT INTO ptracks(playlist_id,track_id,position,name,artist,artists_json,album,
                                 duration_ms,album_art,status,updated_at) VALUES (?,?,?,?,?,?,?,?,?,'missing',?)""",
                              (pid, t["id"], pos, t["name"], ", ".join(t["artists"]), json.dumps(t["artists"]),
                               t["album"], t["duration_ms"], t["album_art"], now))
            # solo borra las que ya no están si se cargó la playlist completa
            if data["source"] in ("cuenta", "deezer"):
                for tid in set(old) - keep:
                    c.execute("DELETE FROM ptracks WHERE playlist_id=? AND track_id=?", (pid, tid))
        rematch(pid)
        return {"id": pid, "name": data["name"], "total": data["total"], "loaded": len(data["tracks"]),
                "source": data["source"], "skipped": data["skipped"], "user_error": err}

    @r.get("/api/playlists/{pid}")
    async def get_playlist(pid: str):
        sync_jobs(pid)
        with _db() as c:
            pl = c.execute("SELECT * FROM playlists WHERE id=?", (pid,)).fetchone()
            if not pl:
                raise HTTPException(404, "Playlist no importada")
            rows = [dict(x) for x in c.execute("SELECT * FROM ptracks WHERE playlist_id=? ORDER BY position", (pid,))]
        root = music_root()
        for x in rows:
            x.pop("artists_json", None)
            for k in ("match_path", "file_path", "old_path"):
                if x.get(k):
                    x[k.replace("_path", "_rel")] = os.path.relpath(os.path.realpath(x[k]), root)
            if x.get("old_path"):
                x["old_exists"] = inside_music(x["old_path"]) and x["old_path"] != x.get("file_path")
            if x["status"] in ACTIVE_STATES:
                job = get_job(x["track_id"]) or {}
                x["progress"] = job.get("progress")
                x["job_message"] = job.get("message")
        return {"playlist": dict(pl), "tracks": rows, "library_scanned_at": lib.scanned_at,
                "bulk": {k: v for k, v in _bulk.items() if k != "stop"}}

    @r.post("/api/playlists/{pid}/rescan")
    async def rescan(pid: str):
        lib.scan()
        rematch(pid)
        return {"ok": True, "files": len(lib.items)}

    @r.delete("/api/playlists/{pid}")
    async def delete_playlist(pid: str):
        with _db_lock, _db() as c:
            c.execute("DELETE FROM ptracks WHERE playlist_id=?", (pid,))
            c.execute("DELETE FROM playlists WHERE id=?", (pid,))
            c.execute("DELETE FROM playlist_sync WHERE playlist_id=?", (pid,))
        return {"ok": True}

    # ---- acciones por canción
    VALID_MARKS = {"have", "missing", "skip", "approved"}

    @r.post("/api/playlists/{pid}/tracks/{tid}/mark")
    async def mark(pid: str, tid: str, req: MarkReq):
        if req.status not in VALID_MARKS:
            raise HTTPException(400, "Estado no válido")
        row = get_row(pid, tid)
        fields = {"status": req.status}
        if req.status == "missing":
            fields["match_path"] = None
            fields["old_path"] = None
        if req.status == "have" and not row.get("match_path") and row.get("file_path"):
            fields["match_path"] = row["file_path"]
        set_track(pid, tid, **fields)
        assess_upgrades(pid, [tid])
        return {"ok": True}

    @r.get("/api/playlists/{pid}/tracks/{tid}/candidates")
    async def candidates(pid: str, tid: str, q: Optional[str] = None):
        row = get_row(pid, tid)
        arts = json.loads(row.get("artists_json") or "[]")
        info = {"name": row["name"], "artist": row["artist"], "artists": arts,
                "album": row.get("album") or "", "duration_ms": row["duration_ms"]}
        if q and q.strip():
            res = youtube_service.search_candidates(q.strip(), "", {**info, "album": ""}, num_results=8)
        else:
            res = youtube_service.search_candidates(row["name"], arts[0] if arts else row["artist"], info, num_results=8)
        return res

    @r.post("/api/playlists/{pid}/tracks/{tid}/download")
    async def download(pid: str, tid: str, req: DownloadReq, bg: BackgroundTasks):
        remember_old_version(get_row(pid, tid))
        queue_download(pid, tid)
        bg.add_task(download_and_process, tid, "navidrome", req.video_id)
        return {"ok": True}

    @r.post("/api/playlists/{pid}/tracks/{tid}/reject")
    async def reject(pid: str, tid: str):
        """Borra lo que se descargó (no te gustó) y la deja como faltante."""
        row = get_row(pid, tid)
        if row["status"] != "downloaded":
            raise HTTPException(400, "Solo se puede rechazar una descarga pendiente de revisión")
        p = row.get("file_path")
        remove_music_file(p)
        old = row.get("old_path")
        if old and old != p and inside_music(old):
            # se buscaba una versión mejor: se queda la que ya había
            set_track(pid, tid, status="have", file_path=None, match_path=old, old_path=None,
                      note="Descarga rechazada; se conserva la versión anterior")
            assess_upgrades(pid, [tid])
        else:
            set_track(pid, tid, status="missing", file_path=None, match_path=None, old_path=None,
                      note="Descarga rechazada")
        lib.scanned_at = 0  # forzar reescaneo la próxima vez
        return {"ok": True, "restored": bool(old and old != p and inside_music(old))}

    def remove_music_file(p):
        """Borra un archivo de la carpeta de música (y su .lrc y carpetas vacías). Solo dentro de ella."""
        if not inside_music(p):
            return False
        os.remove(p)
        lrc = Path(p).with_suffix(".lrc")
        if lrc.exists():
            lrc.unlink()
        try:  # quita carpetas que quedaron vacías (álbum / artista)
            d = Path(p).parent
            for _ in range(2):
                if d.resolve() != Path(music_root()) and not any(d.iterdir()):
                    d.rmdir()
                    d = d.parent
        except Exception:
            pass
        return True

    def remember_old_version(row):
        """Al bajar otra versión de una canción ya encontrada, recuerda el archivo anterior."""
        if row["status"] not in FOUND_STATES:
            return
        cur = row.get("file_path") if row["status"] == "approved" and row.get("file_path") else row.get("match_path")
        if cur and inside_music(cur):
            set_track(row["playlist_id"], row["track_id"], old_path=cur)

    @r.get("/api/playlists/{pid}/tracks/{tid}/audio")
    async def audio(pid: str, tid: str, which: str = "file"):
        row = get_row(pid, tid)
        p = row.get("file_path") if which == "file" else row.get("match_path")
        if not inside_music(p):
            raise HTTPException(404, "No hay archivo para reproducir")
        return FileResponse(os.path.realpath(p), media_type=MIME.get(Path(p).suffix.lower(), "audio/mpeg"))

    # ---- descarga masiva
    def bulk_worker(pid, tids):
        _bulk.update(running=True, playlist_id=pid, done=0, total=len(tids), stop=False)
        try:
            for tid in tids:
                if _bulk["stop"]:
                    break
                try:
                    if get_row(pid, tid)["status"] != "queued":
                        continue
                    download_and_process(tid, "navidrome", None)
                except Exception as e:
                    print(f"bulk download {tid} failed: {e}")
                sync_jobs(pid)
                _bulk["done"] += 1
                time.sleep(6)  # no saturar a YouTube (bloquea por IP con "not a bot")
        finally:
            # las que quedaron en cola por haberse detenido regresan a faltantes
            with _db() as c:
                left = [x["track_id"] for x in c.execute(
                    "SELECT track_id FROM ptracks WHERE playlist_id=? AND status='queued'", (pid,))]
            for tid in left:
                if tid in tids:
                    set_track(pid, tid, status="missing")
            _bulk.update(running=False, stop=False)

    @r.post("/api/playlists/{pid}/download-missing")
    async def download_missing(pid: str):
        if _bulk["running"]:
            raise HTTPException(409, "Ya hay una descarga masiva corriendo")
        with _db() as c:
            tids = [x["track_id"] for x in c.execute(
                "SELECT track_id FROM ptracks WHERE playlist_id=? AND status IN ('missing','error') ORDER BY position",
                (pid,))]
        for tid in tids:
            queue_download(pid, tid)
        threading.Thread(target=bulk_worker, args=(pid, tids), daemon=True).start()
        return {"ok": True, "queued": len(tids)}

    @r.post("/api/playlists/bulk/stop")
    async def bulk_stop():
        _bulk["stop"] = True
        return {"ok": True}

    # ================================================================ mejora de versiones (endpoints)
    class IgnoreReq(BaseModel):
        ignore: bool = True

    @r.post("/api/playlists/{pid}/tracks/{tid}/upgrade-ignore")
    async def upgrade_ignore(pid: str, tid: str, req: IgnoreReq):
        get_row(pid, tid)
        set_track(pid, tid, upgrade_ignored=1 if req.ignore else 0)
        return {"ok": True}

    @r.post("/api/playlists/{pid}/tracks/{tid}/delete-old")
    async def delete_old(pid: str, tid: str):
        """Borra la versión anterior después de aprobar la nueva (acción explícita del usuario)."""
        row = get_row(pid, tid)
        old, new = row.get("old_path"), row.get("file_path")
        if row["status"] != "approved" or not new or not inside_music(new):
            raise HTTPException(400, "Primero aprueba la versión nueva")
        if not old or not inside_music(old):
            set_track(pid, tid, old_path=None)
            raise HTTPException(404, "La versión anterior ya no existe en la carpeta de música")
        if os.path.realpath(old) == os.path.realpath(new):
            raise HTTPException(400, "La versión anterior es el mismo archivo que la nueva")
        remove_music_file(old)
        with _db_lock, _db() as c:  # otras playlists que apuntaban al archivo borrado usan el nuevo
            c.execute("UPDATE ptracks SET match_path=? WHERE match_path=?", (new, old))
            c.execute("UPDATE ptracks SET old_path=NULL WHERE old_path=?", (old,))
        set_track(pid, tid, old_path=None, note=None)
        lib.scanned_at = 0
        return {"ok": True}

    @r.post("/api/playlists/{pid}/tracks/{tid}/keep-both")
    async def keep_both(pid: str, tid: str):
        get_row(pid, tid)
        set_track(pid, tid, old_path=None)
        return {"ok": True}

    # ================================================================ sincronización
    _sync_lock = threading.Lock()
    _sync_state = {"running": None}  # playlist que se está sincronizando ahora

    class SyncCfgReq(BaseModel):
        auto_sync: bool = False
        interval_hours: int = AUTO_SYNC_DEFAULT_HOURS
        auto_download: bool = False

    def sync_cfg(pid) -> Dict:
        with _db() as c:
            row = c.execute("SELECT * FROM playlist_sync WHERE playlist_id=?", (pid,)).fetchone()
        d = dict(row) if row else {"playlist_id": pid, "auto_sync": 0, "interval_hours": AUTO_SYNC_DEFAULT_HOURS,
                                   "auto_download": 0, "sync_url": None, "last_sync_at": None,
                                   "last_sync_ok": None, "last_sync_result": None, "last_sync_error": None,
                                   "fail_count": 0, "next_sync_at": None}
        try:
            d["last_sync_result"] = json.loads(d["last_sync_result"]) if d.get("last_sync_result") else None
        except ValueError:
            d["last_sync_result"] = None
        return d

    def save_sync(pid, **f):
        with _db_lock, _db() as c:
            c.execute("INSERT OR IGNORE INTO playlist_sync(playlist_id) VALUES (?)", (pid,))
            if f:
                c.execute(f"UPDATE playlist_sync SET {', '.join(k + '=?' for k in f)} WHERE playlist_id=?",
                          (*f.values(), pid))

    def interval_s(cfg) -> int:
        if AUTO_SYNC_TEST_MINUTES:
            return AUTO_SYNC_TEST_MINUTES * 60
        return max(AUTO_SYNC_MIN_HOURS, int(cfg.get("interval_hours") or AUTO_SYNC_DEFAULT_HOURS)) * 3600

    def startup_delay_s() -> int:
        return 20 if AUTO_SYNC_TEST_MINUTES else AUTO_SYNC_STARTUP_DELAY_S

    def run_maybe_async(res):
        return asyncio.run(res) if inspect.iscoroutine(res) else res

    def do_sync(pid, trigger="manual") -> Dict:
        """Vuelve a leer la playlist con el mismo flujo de importación y marca las canciones nuevas.
        Devuelve el resumen; las nuevas faltantes van en `new_missing` (para la descarga automática)."""
        if not _sync_lock.acquire(blocking=False):
            raise HTTPException(409, "Ya se está sincronizando otra playlist; intenta en un momento")
        _sync_state["running"] = pid
        try:
            with _db() as c:
                pl = c.execute("SELECT * FROM playlists WHERE id=?", (pid,)).fetchone()
                if not pl:
                    raise HTTPException(404, "Playlist no importada")
                before = {x["track_id"] for x in c.execute("SELECT track_id FROM ptracks WHERE playlist_id=?", (pid,))}
            cfg = sync_cfg(pid)
            res = run_maybe_async(import_playlist(ImportReq(url=cfg.get("sync_url") or pid)))
            with _db() as c:
                after = {x["track_id"] for x in c.execute("SELECT track_id FROM ptracks WHERE playlist_id=?", (pid,))}
            added, removed = after - before, before - after
            now = int(time.time())
            if before and added:  # en la primera importación no hay "nuevas"
                with _db_lock, _db() as c:
                    c.executemany("UPDATE ptracks SET is_new=1, added_at=? WHERE playlist_id=? AND track_id=?",
                                  [(now, pid, t) for t in added])
            with _db() as c:
                new_missing = [x["track_id"] for x in c.execute(
                    f"SELECT track_id FROM ptracks WHERE playlist_id=? AND status='missing' "
                    f"AND track_id IN ({','.join('?' * len(added)) or 'NULL'}) ORDER BY position",
                    (pid, *added))] if before else []
            partial = res.get("source") == "embed" and res.get("loaded", 0) < res.get("total", 0)
            summary = {"trigger": trigger, "added": len(added) if before else 0, "removed": len(removed),
                       "loaded": res.get("loaded"), "total": res.get("total"), "source": res.get("source"),
                       "partial": partial, "user_error": res.get("user_error"), "downloads": 0}
            fields = dict(last_sync_at=now, last_sync_ok=1, last_sync_error=None,
                          last_sync_result=json.dumps(summary), fail_count=0)
            if cfg.get("auto_sync"):
                fields["next_sync_at"] = now + interval_s(cfg)
            save_sync(pid, **fields)
            return {**summary, "new_missing": new_missing}
        except Exception as e:
            msg = e.detail if isinstance(e, HTTPException) else str(e)
            if not (isinstance(e, HTTPException) and e.status_code == 409):
                cfg = sync_cfg(pid)
                fields = dict(last_sync_at=int(time.time()), last_sync_ok=0, last_sync_error=str(msg)[:500])
                if trigger == "auto":  # backoff: no reintentar en bucle
                    fails = int(cfg.get("fail_count") or 0) + 1
                    wait = min(interval_s(cfg) * (2 ** min(fails - 1, 6)), AUTO_SYNC_MAX_BACKOFF_S)
                    fields.update(fail_count=fails, next_sync_at=int(time.time()) + wait)
                save_sync(pid, **fields)
            raise
        finally:
            _sync_state["running"] = None
            _sync_lock.release()

    def auto_download_new(pid, tids) -> int:
        """Encola las nuevas faltantes reutilizando la descarga masiva (máximo AUTO_SYNC_MAX_DOWNLOADS)."""
        tids = tids[:AUTO_SYNC_MAX_DOWNLOADS]
        if not tids or _bulk["running"]:
            return 0
        for tid in tids:
            queue_download(pid, tid)
        bulk_worker(pid, tids)  # en este mismo hilo: una cosa a la vez
        return len(tids)

    def sync_public(pid) -> Dict:
        with _db() as c:
            pl = c.execute("SELECT source, loaded, total FROM playlists WHERE id=?", (pid,)).fetchone()
            n_new = c.execute("SELECT COUNT(*) FROM ptracks WHERE playlist_id=? AND is_new=1", (pid,)).fetchone()[0]
        cfg = sync_cfg(pid)
        cfg.pop("sync_url", None)
        src = pl["source"] if pl else None
        return {**cfg, "syncable": src in SYNCABLE_SOURCES, "source": src,
                "partial": bool(pl and src == "embed" and pl["loaded"] < pl["total"]),
                "new_count": n_new, "running": _sync_state["running"] == pid,
                "global": {"enabled": AUTO_SYNC_ENABLED, "min_hours": AUTO_SYNC_MIN_HOURS,
                           "default_hours": AUTO_SYNC_DEFAULT_HOURS, "max_downloads": AUTO_SYNC_MAX_DOWNLOADS,
                           "test_minutes": AUTO_SYNC_TEST_MINUTES or None}}

    @r.get("/api/playlists/{pid}/sync")
    async def get_sync(pid: str):
        return sync_public(pid)

    @r.put("/api/playlists/{pid}/sync")
    async def put_sync(pid: str, req: SyncCfgReq):
        with _db() as c:
            pl = c.execute("SELECT source FROM playlists WHERE id=?", (pid,)).fetchone()
        if not pl:
            raise HTTPException(404, "Playlist no importada")
        if req.auto_sync and pl["source"] not in SYNCABLE_SOURCES:
            raise HTTPException(400, "Esta playlist no se puede sincronizar automáticamente")
        hours = max(AUTO_SYNC_MIN_HOURS, min(24 * 30, int(req.interval_hours or AUTO_SYNC_DEFAULT_HOURS)))
        cfg = sync_cfg(pid)
        fields = dict(auto_sync=int(req.auto_sync), interval_hours=hours, auto_download=int(req.auto_download))
        if req.auto_sync:
            # nunca sincroniza en el momento de activarla: espera el intervalo desde la última vez
            base = cfg.get("last_sync_at") or int(time.time())
            first = max(base + interval_s({"interval_hours": hours}), int(time.time()) + startup_delay_s())
            if not cfg.get("auto_sync") or cfg.get("interval_hours") != hours or not cfg.get("next_sync_at"):
                fields["next_sync_at"] = first
        else:
            fields.update(next_sync_at=None, fail_count=0)
        save_sync(pid, **fields)
        return sync_public(pid)

    @r.post("/api/playlists/{pid}/sync")
    def sync_now(pid: str):
        """Sincronización manual (corre en el threadpool para no bloquear el servidor)."""
        res = do_sync(pid, "manual")
        res.pop("new_missing", None)
        return res

    @r.post("/api/playlists/{pid}/new/seen")
    async def new_seen(pid: str):
        with _db_lock, _db() as c:
            c.execute("UPDATE ptracks SET is_new=0 WHERE playlist_id=?", (pid,))
        return {"ok": True}

    # ---- programador en segundo plano
    def scheduler_loop():
        tick = 10 if AUTO_SYNC_TEST_MINUTES else 300
        print(f"[auto-sync] programador activo (primera revisión en {startup_delay_s()} s)")
        time.sleep(startup_delay_s())
        # tras un reinicio, lo que "ya tocaba" se pospone: nunca hay ráfaga de peticiones al arrancar
        while True:
            try:
                now = int(time.time())
                with _db() as c:
                    due = [x["playlist_id"] for x in c.execute(
                        "SELECT s.playlist_id FROM playlist_sync s JOIN playlists p ON p.id=s.playlist_id "
                        "WHERE s.auto_sync=1 AND (s.next_sync_at IS NULL OR s.next_sync_at<=?) "
                        "ORDER BY COALESCE(s.next_sync_at,0) LIMIT 1", (now,))]
                if not due:
                    time.sleep(tick)
                    continue
                pid = due[0]
                cfg = sync_cfg(pid)
                with _db() as c:
                    src = (c.execute("SELECT source FROM playlists WHERE id=?", (pid,)).fetchone() or {"source": None})["source"]
                if src not in SYNCABLE_SOURCES:
                    save_sync(pid, auto_sync=0, next_sync_at=None,
                              last_sync_error="Fuente no sincronizable; se desactivó la sincronización automática")
                    continue
                print(f"[auto-sync] sincronizando {pid}")
                try:
                    res = do_sync(pid, "auto")
                except HTTPException as e:
                    if e.status_code == 409:  # manual en curso: reintenta en el siguiente tick
                        time.sleep(tick)
                        continue
                    print(f"[auto-sync] {pid} falló: {e.detail}")
                    res = None
                except Exception as e:
                    print(f"[auto-sync] {pid} falló: {e}")
                    res = None
                if res and cfg.get("auto_download") and res["new_missing"]:
                    n = auto_download_new(pid, res["new_missing"])
                    summary = {k: v for k, v in res.items() if k != "new_missing"}
                    summary["downloads"] = n
                    if not n:
                        summary["downloads_note"] = "Había otra descarga masiva en curso"
                    save_sync(pid, last_sync_result=json.dumps(summary))
                time.sleep(AUTO_SYNC_PAUSE_S if not AUTO_SYNC_TEST_MINUTES else 5)
            except Exception as e:  # el hilo nunca debe morir ni girar en bucle
                print(f"[auto-sync] error del programador: {e}")
                time.sleep(tick)

    global _scheduler_started
    if AUTO_SYNC_ENABLED and not _scheduler_started:
        _scheduler_started = True
        threading.Thread(target=scheduler_loop, name="auto-sync", daemon=True).start()
    elif not AUTO_SYNC_ENABLED:
        print("[auto-sync] desactivado (AUTO_SYNC_ENABLED=false)")

    return r
