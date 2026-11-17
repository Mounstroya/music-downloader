"""Archivos de la biblioteca de música (NAVIDROME_MUSIC_PATH): rutas seguras y links públicos.

Lo usan el endpoint GET /api/library/file y el servidor MCP.
"""
import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import config
from playlist_feature import AUDIO_EXT, MIME  # mismas extensiones que indexa la revisión de playlists


def music_root() -> str:
    return os.path.realpath(config.NAVIDROME_MUSIC_PATH)


def mime_for(path: str) -> str:
    return MIME.get(Path(path).suffix.lower(), "application/octet-stream")


def resolve_library_file(path: Optional[str]) -> Optional[str]:
    """Ruta real de un archivo de audio de la biblioteca, o None si no es válido.

    Acepta una ruta relativa a la biblioteca o una absoluta. La ruta real (resolviendo
    symlinks y "..") tiene que quedar DENTRO de la carpeta de música y ser un archivo de audio.
    """
    if not path or "\x00" in path:
        return None
    root = music_root()
    full = os.path.realpath(path if os.path.isabs(path) else os.path.join(root, path))
    if not full.startswith(root + os.sep):
        return None
    if Path(full).suffix.lower() not in AUDIO_EXT or not os.path.isfile(full):
        return None
    return full


def relative_path(path: str) -> str:
    """Ruta relativa a la biblioteca (con "/" como separador)."""
    return os.path.relpath(os.path.realpath(path), music_root()).replace(os.sep, "/")


def base_url_from_headers(headers, scheme: str = "http") -> str:
    """URL base pública: PUBLIC_BASE_URL si está definida; si no, la de la petición.

    Respeta X-Forwarded-Proto / X-Forwarded-Host y el X-Forwarded-Prefix que ya usa la app.
    """
    if config.PUBLIC_BASE_URL:
        return config.PUBLIC_BASE_URL
    if headers is None:
        return f"http://localhost:{config.API_PORT}"
    proto = (headers.get("x-forwarded-proto") or scheme or "http").split(",")[0].strip()
    host = (headers.get("x-forwarded-host") or headers.get("host") or f"localhost:{config.API_PORT}").split(",")[0].strip()
    prefix = (headers.get("x-forwarded-prefix") or "").strip().rstrip("/")
    return f"{proto}://{host}{prefix}"


def file_url(base_url: str, rel_path: str) -> str:
    return f"{base_url}/api/library/file?path={quote(rel_path, safe='')}"
