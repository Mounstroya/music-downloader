"""Revisión de playlists de Spotify contra la biblioteca de Navidrome.

Permite importar una playlist, detectar qué canciones ya están en la carpeta de
música, marcarlas a mano, buscar candidatos en YouTube, descargar y aprobar o
rechazar lo descargado.
"""
import difflib
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


# ---------------------------------------------------------------- normalización
_VERSION_WORDS = r"remaster|version|versión|mix|edit|live|en vivo|mono|stereo|radio|from|feat|ft\.|con |with |deluxe|bonus|acoustic|acústic"


LIVE_RE = re.compile(r"\b(live|en vivo|en directo|unplugged|unstaged|concert|concierto|tour)\b", re.I)


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
class Library:
    def __init__(self, root: str):
        self.root = root
        self.items: List[Dict] = []
        self.by_title: Dict[str, List[Dict]] = {}
        self.scanned_at = 0
        self._lock = threading.Lock()

    def scan(self):
        from mutagen import File as MFile
        items = []
        for dirpath, _, files in os.walk(self.root):
            for fn in files:
                p = os.path.join(dirpath, fn)
                if Path(fn).suffix.lower() not in AUDIO_EXT:
                    continue
                title = artist = ""
                length = 0
                try:
                    mf = MFile(p, easy=True)
                    if mf is not None:
                        tags = mf.tags or {}
                        title = (tags.get("title") or [""])[0]
                        artist = " ; ".join((tags.get("artist") or []) + (tags.get("albumartist") or []))
                        length = int(getattr(mf.info, "length", 0) or 0)
                except Exception:
                    pass
                stem = Path(fn).stem
                if not title:
                    title = stem.split(" - ", 1)[1] if " - " in stem else stem
                    title = re.sub(r"^\d+[\s.\-_]+", "", title)
                rel = os.path.relpath(p, self.root)
                items.append({
                    "path": p, "rel": rel, "title": title, "bt": base_title(title),
                    "artist_n": norm(artist + " " + rel.replace(os.sep, " ")), "length": length,
                })
        idx: Dict[str, List[Dict]] = {}
        for it in items:
            idx.setdefault(it["bt"], []).append(it)
        with self._lock:
            self.items, self.by_title, self.scanned_at = items, idx, int(time.time())

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
    m = re.search(r"playlist[/:]([A-Za-z0-9]{22})", url or "")
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{22}", (url or "").strip()):
        return url.strip()
    raise HTTPException(400, "No reconozco ese link de playlist de Spotify")


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


def build_router(spotify_service, youtube_service, download_and_process: Callable, templates) -> APIRouter:
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
            if data["source"] == "cuenta":
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
            for k in ("match_path", "file_path"):
                if x.get(k):
                    x[k.replace("_path", "_rel")] = os.path.relpath(os.path.realpath(x[k]), root)
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
        if req.status == "have" and not row.get("match_path") and row.get("file_path"):
            fields["match_path"] = row["file_path"]
        set_track(pid, tid, **fields)
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
        get_row(pid, tid)
        if spotify_service is None:
            raise HTTPException(500, "Spotify no está configurado")
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
        if inside_music(p):
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
        set_track(pid, tid, status="missing", file_path=None, match_path=None, note="Descarga rechazada")
        lib.scanned_at = 0  # forzar reescaneo la próxima vez
        return {"ok": True}

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

    return r
