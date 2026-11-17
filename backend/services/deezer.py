"""Metadatos desde la API pública de Deezer (sin cuenta ni llaves).

Devuelve diccionarios con la misma forma que SpotifyService para que el resto de la app
no note la diferencia. Los IDs llevan el prefijo "dz:" (p. ej. "dz:3135556") para
distinguirlos de los de Spotify.
"""
import time
from typing import Dict, List, Optional

import requests

API = "https://api.deezer.com"
PREFIX = "dz:"


def is_deezer_id(value: str) -> bool:
    return str(value or "").startswith(PREFIX)


def raw_id(value: str) -> str:
    return str(value)[len(PREFIX):] if is_deezer_id(value) else str(value)


class DeezerError(Exception):
    pass


class DeezerService:
    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "music-downloader"

    # ------------------------------------------------------------------ http
    def _get(self, path: str, **params) -> Dict:
        last = None
        for attempt in range(3):
            try:
                r = self.session.get(f"{API}/{path.lstrip('/')}", params=params, timeout=self.timeout)
                data = r.json()
            except Exception as e:  # red / JSON inválido
                last = e
                time.sleep(1 + attempt)
                continue
            err = data.get("error") if isinstance(data, dict) else None
            if err:
                # code 4 = cuota (50 peticiones / 5 s): esperar y reintentar
                if err.get("code") == 4:
                    last = DeezerError(err.get("message"))
                    time.sleep(2 + attempt * 2)
                    continue
                raise DeezerError(f"{err.get('type', 'Error')}: {err.get('message', '')}")
            return data
        raise DeezerError(f"Deezer no respondió: {last}")

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _cover(album: Dict) -> Optional[str]:
        return album.get("cover_xl") or album.get("cover_big") or album.get("cover_medium") or None

    def _track(self, t: Dict, album: Optional[Dict] = None) -> Dict:
        album = album or t.get("album") or {}
        contributors = [c["name"] for c in t.get("contributors") or [] if c.get("name")]
        main = (t.get("artist") or {}).get("name") or ""
        artists = contributors or ([main] if main else [])
        album_artist = ((album.get("artist") or {}).get("name")) or main
        return {
            "id": PREFIX + str(t["id"]),
            "name": t.get("title") or t.get("title_short") or "",
            "artists": artists,
            "artist": ", ".join(artists),
            "album_artists": [album_artist] if album_artist else [],
            "album_artist": album_artist,
            "album": album.get("title") or "",
            "album_id": PREFIX + str(album["id"]) if album.get("id") else "",
            "duration_ms": int(t.get("duration") or 0) * 1000,
            "external_url": t.get("link") or f"https://www.deezer.com/track/{t['id']}",
            "preview_url": t.get("preview") or None,
            "track_number": t.get("track_position") or 1,
            "album_art": self._cover(album),
            "release_date": album.get("release_date") or t.get("release_date") or "",
            "isrc": t.get("isrc"),
            "source": "deezer",
        }

    # ------------------------------------------------------------------ API pública
    def search_tracks(self, query: str, limit: int = 10, offset: int = 0) -> List[Dict]:
        data = self._get("search", q=query, limit=limit, index=offset)
        return [self._track(t) for t in data.get("data", []) if t.get("type", "track") == "track"]

    def get_track_details(self, track_id: str) -> Optional[Dict]:
        try:
            t = self._get(f"track/{raw_id(track_id)}")
        except DeezerError as e:
            print(f"Deezer track error: {e}")
            return None
        album = t.get("album") or {}
        # el álbum completo trae el artista del álbum (para la carpeta Artista/Álbum)
        if album.get("id") and not album.get("artist"):
            try:
                full = self._get(f"album/{album['id']}")
                album = {**album, "artist": full.get("artist"), "release_date": full.get("release_date") or album.get("release_date")}
            except DeezerError:
                pass
        return self._track(t, album)

    def search_albums(self, query: str, limit: int = 10, offset: int = 0) -> List[Dict]:
        data = self._get("search/album", q=query, limit=limit, index=offset)
        out = []
        for a in data.get("data", []):
            artist = (a.get("artist") or {}).get("name") or ""
            out.append({
                "id": PREFIX + str(a["id"]),
                "name": a.get("title") or "",
                "artist": artist,
                "artists": [artist] if artist else [],
                "release_date": a.get("release_date") or "",
                "total_tracks": a.get("nb_tracks") or 0,
                "album_art": self._cover(a),
                "external_url": a.get("link") or "",
                "source": "deezer",
            })
        return out

    def get_album_details(self, album_id: str) -> Optional[Dict]:
        try:
            a = self._get(f"album/{raw_id(album_id)}")
        except DeezerError as e:
            print(f"Deezer album error: {e}")
            return None
        items = list((a.get("tracks") or {}).get("data") or [])
        total = a.get("nb_tracks") or len(items)
        while len(items) < total:  # paginación de discos grandes
            page = self._get(f"album/{a['id']}/tracks", index=len(items), limit=100).get("data") or []
            if not page:
                break
            items.extend(page)
        tracks = []
        for pos, t in enumerate(items, start=1):
            tr = self._track(t, a)
            if not t.get("track_position"):
                tr["track_number"] = pos
            tracks.append(tr)
        artist = (a.get("artist") or {}).get("name") or ""
        return {
            "id": PREFIX + str(a["id"]),
            "name": a.get("title") or "",
            "artist": artist,
            "artists": [c["name"] for c in a.get("contributors") or []] or [artist],
            "release_date": a.get("release_date") or "",
            "total_tracks": total,
            "album_art": self._cover(a),
            "external_url": a.get("link") or "",
            "tracks": tracks,
            "source": "deezer",
        }

    def get_playlist(self, playlist_id: str) -> Dict:
        """Playlist pública completa (sin el límite de 100 canciones de Spotify)."""
        p = self._get(f"playlist/{raw_id(playlist_id)}")
        items = list((p.get("tracks") or {}).get("data") or [])
        total = p.get("nb_tracks") or len(items)
        while len(items) < total:
            page = self._get(f"playlist/{p['id']}/tracks", index=len(items), limit=100).get("data") or []
            if not page:
                break
            items.extend(page)
        return {
            "name": p.get("title") or "",
            "owner": (p.get("creator") or {}).get("name"),
            "total": total,
            "tracks": [{**self._track(t), "album_art": (t.get("album") or {}).get("cover_medium")}  # miniatura ligera
                       for t in items if t.get("type", "track") == "track" and t.get("readable", True) is not False],
        }
