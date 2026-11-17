"""Catálogo de metadatos: Spotify y/o Deezer detrás de una sola interfaz.

- METADATA_SOURCE=auto (por defecto): busca en Spotify; si no está configurado o falla, en Deezer.
- METADATA_SOURCE=spotify: solo Spotify.   METADATA_SOURCE=deezer: solo Deezer (no necesita llaves).
- Los detalles de una canción/álbum se piden a quien corresponde según el ID ("dz:…" = Deezer).
"""
from typing import Dict, List, Optional

from services.deezer import DeezerService, is_deezer_id

SOURCES = ("auto", "spotify", "deezer")


class CatalogService:
    def __init__(self, spotify=None, deezer: Optional[DeezerService] = None, source: str = "auto"):
        self.spotify = spotify
        self.deezer = deezer or DeezerService()
        self.source = source if source in SOURCES else "auto"

    # ------------------------------------------------------------------ info
    def available(self) -> List[str]:
        return (["spotify"] if self.spotify else []) + ["deezer"]

    def search_order(self, source: Optional[str] = None) -> List[str]:
        src = source if source in SOURCES else self.source
        if src == "deezer" or (src != "deezer" and not self.spotify):
            return ["deezer"]
        if src == "spotify":
            return ["spotify"]
        return ["spotify", "deezer"]

    def _svc(self, name: str):
        return self.spotify if name == "spotify" else self.deezer

    def _search(self, method: str, source: Optional[str], *args, **kwargs):
        last = None
        for name in self.search_order(source):
            try:
                res = getattr(self._svc(name), method)(*args, **kwargs)
                for item in res:
                    item.setdefault("source", name)
                return res
            except Exception as e:
                print(f"{name} {method} falló: {e}")
                last = e
        raise last or RuntimeError("No hay fuente de metadatos disponible")

    # ------------------------------------------------------------------ misma interfaz que SpotifyService
    def search_tracks(self, query: str, limit: int = 10, offset: int = 0, source: Optional[str] = None) -> List[Dict]:
        return self._search("search_tracks", source, query, limit, offset)

    def search_albums(self, query: str, limit: int = 10, offset: int = 0, source: Optional[str] = None) -> List[Dict]:
        return self._search("search_albums", source, query, limit, offset)

    def get_track_details(self, track_id: str) -> Optional[Dict]:
        if is_deezer_id(track_id):
            return self.deezer.get_track_details(track_id)
        if not self.spotify:
            return None
        t = self.spotify.get_track_details(track_id)
        if t:
            t.setdefault("source", "spotify")
        return t

    def get_album_details(self, album_id: str) -> Optional[Dict]:
        if is_deezer_id(album_id):
            return self.deezer.get_album_details(album_id)
        if not self.spotify:
            return None
        a = self.spotify.get_album_details(album_id)
        if a:
            a.setdefault("source", "spotify")
        return a
