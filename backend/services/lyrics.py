import requests
from typing import Optional, Dict


class LyricsService:
    BASE_URL = "https://lrclib.net/api"

    def get_lyrics(self, artist: str, name: str, duration: float = None) -> Dict:
        """Fetch lyrics from lrclib.net.

        Returns dict with 'synced' (LRC format string or None) and 'plain' (str or None).
        Tries exact match first (when duration is known), then falls back to search.
        """
        if duration:
            exact = self._get_exact(artist, name, duration)
            if exact is not None:
                print(f"lrclib: exact match for '{artist} - {name}' (synced={bool(exact.get('synced'))})")
                return exact

        result = self._search_best(artist, name, duration)
        if result.get('synced') or result.get('plain'):
            print(f"lrclib: search match for '{artist} - {name}' (synced={bool(result.get('synced'))})")
        else:
            print(f"lrclib: no lyrics found for '{artist} - {name}'")
        return result

    def _get_exact(self, artist: str, name: str, duration: float) -> Optional[Dict]:
        try:
            resp = requests.get(
                f"{self.BASE_URL}/get",
                params={
                    'artist_name': artist,
                    'track_name': name,
                    'duration': round(duration),
                },
                timeout=10,
            )
            if resp.status_code == 404:
                return None
            if not resp.ok:
                return None
            data = resp.json()
            if data.get('instrumental'):
                return {'synced': None, 'plain': None}
            synced = data.get('syncedLyrics') or None
            plain = data.get('plainLyrics') or None
            if not synced and not plain:
                return None
            return {'synced': synced, 'plain': plain}
        except Exception as e:
            print(f"lrclib exact lookup error: {e}")
            return None

    def _search_best(self, artist: str, name: str, duration: float = None) -> Dict:
        try:
            resp = requests.get(
                f"{self.BASE_URL}/search",
                params={
                    'artist_name': artist,
                    'track_name': name,
                },
                timeout=10,
            )
            if not resp.ok:
                return {'synced': None, 'plain': None}
            results = resp.json()
            if not results:
                return {'synced': None, 'plain': None}

            # Prefer results with synced lyrics; fall back to any
            with_synced = [r for r in results if r.get('syncedLyrics')]
            pool = with_synced if with_synced else results

            if duration:
                best = min(pool, key=lambda r: abs((r.get('duration') or 0) - duration))
            else:
                best = pool[0]

            if best.get('instrumental'):
                return {'synced': None, 'plain': None}

            return {
                'synced': best.get('syncedLyrics') or None,
                'plain': best.get('plainLyrics') or None,
            }
        except Exception as e:
            print(f"lrclib search error: {e}")
            return {'synced': None, 'plain': None}
