"""Servidor MCP (Model Context Protocol) para que agentes de IA usen la app.

Se monta en la misma app FastAPI en /mcp con transporte "streamable HTTP" (SDK oficial
`mcp`, FastMCP). Las herramientas reutilizan la lógica existente: el catálogo
(Spotify/Deezer), `download_and_process` (igual que /api/download con location
"navidrome"), los jobs de `utils.job_store` y los endpoints de revisión de playlists.

Autenticación opcional: si MCP_TOKEN está vacío no se pide nada; si tiene valor se exige
el header "Authorization: Bearer <MCP_TOKEN>".
"""
import asyncio
import contextlib
import difflib
import hmac
import os
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.fastmcp.utilities.types import Audio
from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Route

import config
from playlist_feature import Library, base_title, norm
from utils.job_store import get_job, upsert_job
from utils.library_files import base_url_from_headers, file_url, mime_for, relative_path, resolve_library_file

MAX_AUDIO_BYTES = 15 * 1024 * 1024
LIBRARY_TTL_S = 120  # reescanear la carpeta de música si el índice tiene más de esto
ACTIVE_JOB = ("queued", "processing")

# Cómo se agrupan los estados de las canciones de una playlist (playlist_feature)
HAVE_STATES = ("auto", "have", "approved")
REVIEW_STATES = ("maybe", "downloaded")
MISSING_STATES = ("missing", "error")
BUSY_STATES = ("queued", "downloading")

INSTRUCTIONS = """\
Music Downloader: busca canciones (metadatos de Spotify/Deezer), las descarga de YouTube y las
guarda en la biblioteca de música de Navidrome del usuario.

Flujo típico: buscar_canciones -> descargar_cancion(track_id) -> obtener_cancion para darle al
usuario el link de descarga (o el audio). Antes de descargar conviene revisar con
buscar_en_biblioteca si ya la tiene. Para playlists: revisar_playlist(url) ->
descargar_faltantes(playlist_id) -> estado_playlist(playlist_id).
YouTube bloquea por IP si se descarga demasiado: no descargues canciones que el usuario no pidió.
"""


def _fmt_duration(ms: Optional[int]) -> str:
    s = int((ms or 0) / 1000)
    return f"{s // 60}:{s % 60:02d}"


class _TokenGuard:
    """ASGI: exige "Authorization: Bearer <MCP_TOKEN>" solo si MCP_TOKEN tiene valor."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        token = config.MCP_TOKEN
        if token and scope["type"] == "http":
            got = Headers(scope=scope).get("authorization", "")
            if not hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
                resp = JSONResponse(
                    {"error": "unauthorized",
                     "error_description": "Falta el header Authorization: Bearer <MCP_TOKEN> o es incorrecto"},
                    status_code=401, headers={"WWW-Authenticate": 'Bearer realm="music-downloader"'})
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_mcp(app, catalog, download_and_process) -> FastMCP:
    mcp = FastMCP(
        "music-downloader",
        instructions=INSTRUCTIONS,
        streamable_http_path="/mcp",
        # La app se instala en la red local y se llama por IP/nombre: no limitar el Host.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    lib = Library(config.NAVIDROME_MUSIC_PATH)
    lib_lock = threading.Lock()

    # ------------------------------------------------------------ helpers
    def base_url(ctx: Optional[Context]) -> str:
        req = None
        try:
            req = ctx.request_context.request if ctx else None
        except Exception:
            req = None
        if req is None:
            return base_url_from_headers(None)
        return base_url_from_headers(req.headers, req.url.scheme)

    def file_info(full: str, ctx: Optional[Context]) -> Dict[str, Any]:
        rel = relative_path(full)
        return {"ruta": rel, "link_descarga": file_url(base_url(ctx), rel),
                "tamano_mb": round(os.path.getsize(full) / 1024 / 1024, 2), "mime_type": mime_for(full)}

    def job_file(job: Optional[Dict]) -> Optional[str]:
        return resolve_library_file((job or {}).get("file_path"))

    def job_summary(track_id: str, ctx: Optional[Context]) -> Dict[str, Any]:
        job = get_job(track_id)
        if not job:
            return {"track_id": track_id, "estado": "sin_descarga",
                    "mensaje": "No hay ninguna descarga registrada para esta canción"}
        out = {"track_id": track_id, "estado": job.get("status"), "etapa": job.get("stage"),
               "progreso": job.get("progress"), "mensaje": job.get("message")}
        full = job_file(job)
        if full:
            out.update(file_info(full, ctx))
        elif job.get("status") == "completed":
            out["mensaje"] = (out["mensaje"] or "") + " (el archivo ya no está en la biblioteca)"
        return out

    def ensure_library():
        with lib_lock:
            if not lib.scanned_at or time.time() - lib.scanned_at > LIBRARY_TTL_S:
                lib.scan()

    def track_out(t: Dict) -> Dict[str, Any]:
        return {"id": t.get("id"), "nombre": t.get("name"), "artista": t.get("artist"),
                "album": t.get("album"), "duracion": _fmt_duration(t.get("duration_ms")),
                "duracion_ms": t.get("duration_ms"), "fuente": t.get("source")}

    async def api(method: str, path: str, json: Optional[Dict] = None) -> Any:
        """Llama a un endpoint de la propia app (en proceso, sin red) para reutilizarlo tal cual."""
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://music-downloader.internal",
                                     timeout=600) as c:
            r = await c.request(method, path, json=json)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except Exception:
                detail = r.text
            raise ToolError(f"{detail or 'Error'} (HTTP {r.status_code})")
        return r.json()

    def playlist_counts(tracks: List[Dict]) -> Dict[str, Any]:
        by_status: Dict[str, int] = {}
        for t in tracks:
            by_status[t["status"]] = by_status.get(t["status"], 0) + 1

        def n(states):
            return sum(by_status.get(s, 0) for s in states)

        return {"total": len(tracks), "ya_estan": n(HAVE_STATES), "faltan": n(MISSING_STATES),
                "por_revisar": n(REVIEW_STATES), "en_descarga": n(BUSY_STATES),
                "omitidas": by_status.get("skip", 0), "por_estado": by_status}

    def short_list(tracks: List[Dict], states, limit: int = 15) -> List[str]:
        return [f"{t['name']} - {t['artist']}" for t in tracks if t["status"] in states][:limit]

    # ------------------------------------------------------------ herramientas
    @mcp.tool(title="Buscar canciones en el catálogo")
    async def buscar_canciones(consulta: str, limite: int = 5, fuente: Optional[str] = None) -> Dict[str, Any]:
        """Busca canciones en el catálogo de metadatos (Spotify o Deezer) por título y/o artista.

        Úsala para obtener el `id` de una canción antes de descargarla con descargar_cancion.
        - consulta: texto libre, p. ej. "Bohemian Rhapsody Queen".
        - limite: cuántos resultados devolver (1 a 20).
        - fuente: "spotify", "deezer" o "auto" (por defecto la configurada en la app).
        Los IDs de Deezer empiezan con "dz:".
        """
        if not (consulta or "").strip():
            raise ToolError("La consulta está vacía")
        if fuente is not None and fuente not in ("spotify", "deezer", "auto"):
            raise ToolError('fuente debe ser "spotify", "deezer" o "auto"')
        limite = max(1, min(int(limite or 5), 20))
        try:
            res = await asyncio.to_thread(catalog.search_tracks, consulta.strip(), limite, 0, fuente)
        except Exception as e:
            raise ToolError(f"La búsqueda falló: {e}")
        return {"resultados": [track_out(t) for t in res], "fuentes_disponibles": catalog.available()}

    @mcp.tool(title="Buscar en la biblioteca de música")
    async def buscar_en_biblioteca(consulta: str, limite: int = 20, ctx: Context = None) -> Dict[str, Any]:
        """Busca qué archivos de audio ya existen en la biblioteca de música (carpeta de Navidrome).

        Úsala antes de descargar para no repetir canciones que el usuario ya tiene. Compara sin
        acentos ni mayúsculas contra el título, el artista y la ruta del archivo.
        - consulta: p. ej. "queen bohemian" o solo el nombre de un artista.
        Devuelve la ruta relativa y un link de descarga de cada archivo encontrado.
        """
        q = norm(consulta)
        if not q:
            raise ToolError("La consulta está vacía")
        await asyncio.to_thread(ensure_library)
        words = q.split()
        qt = base_title(consulta)
        hits = []
        for it in lib.items:
            hay = norm(it["title"]) + " " + it["artist_n"]
            if all(w in hay for w in words):
                hits.append((difflib.SequenceMatcher(None, qt, it["bt"]).ratio(), it))
        hits.sort(key=lambda h: -h[0])
        limite = max(1, min(int(limite or 20), 100))
        base = base_url(ctx)
        return {
            "total_encontrados": len(hits),
            "archivos": [{"titulo": it["title"], "ruta": it["rel"].replace(os.sep, "/"),
                          "duracion": _fmt_duration(it["length"] * 1000),
                          "link_descarga": file_url(base, it["rel"].replace(os.sep, "/"))}
                         for _, it in hits[:limite]],
            "archivos_en_biblioteca": len(lib.items),
        }

    @mcp.tool(title="Descargar una canción a la biblioteca")
    async def descargar_cancion(track_id: Optional[str] = None, consulta: Optional[str] = None,
                                esperar: bool = True, timeout_s: int = 240, ctx: Context = None) -> Dict[str, Any]:
        """Descarga una canción de YouTube, le pone los metadatos y la guarda en la biblioteca de Navidrome.

        - track_id: id devuelto por buscar_canciones (Spotify o "dz:..." de Deezer).
        - consulta: si no hay track_id, se busca en el catálogo y se toma el mejor resultado.
        - esperar: si es true espera a que termine (hasta timeout_s segundos); si no, solo la encola
          y se consulta después con estado_descarga.
        Devuelve el estado, la ruta relativa en la biblioteca y un link de descarga del archivo.
        Cada descarga consulta YouTube, que bloquea por IP si se abusa: descarga solo lo que se pidió.
        """
        elegido = None
        if not track_id:
            if not (consulta or "").strip():
                raise ToolError("Indica track_id o consulta")
            try:
                res = await asyncio.to_thread(catalog.search_tracks, consulta.strip(), 1, 0, None)
            except Exception as e:
                raise ToolError(f"La búsqueda falló: {e}")
            if not res:
                raise ToolError(f"No encontré ninguna canción para «{consulta}»")
            elegido = track_out(res[0])
            track_id = res[0]["id"]

        job = get_job(track_id)
        if job and job.get("status") == "completed" and job_file(job):
            out = job_summary(track_id, ctx)
            out["mensaje"] = "La canción ya se había descargado antes"
        else:
            if not job or job.get("status") not in ACTIVE_JOB:
                # misma lógica que POST /api/download con location "navidrome"
                upsert_job(track_id, status="queued", message="Download queued for Navidrome server",
                           progress=0, stage="queued")
                threading.Thread(target=download_and_process, args=(track_id, "navidrome"), daemon=True).start()
            if esperar:
                deadline = time.monotonic() + max(5, min(int(timeout_s or 240), 900))
                while time.monotonic() < deadline:
                    job = get_job(track_id) or {}
                    if job.get("status") not in ACTIVE_JOB:
                        break
                    if ctx is not None:
                        with contextlib.suppress(Exception):
                            await ctx.report_progress(job.get("progress") or 0, 100, job.get("message"))
                    await asyncio.sleep(2)
            out = job_summary(track_id, ctx)
            if out.get("estado") in ACTIVE_JOB and esperar:
                out["mensaje"] = f"{out.get('mensaje')} (sigue en curso; consulta con estado_descarga)"
            if out.get("estado") == "completed":
                lib.scanned_at = 0  # que buscar_en_biblioteca vea el archivo nuevo
        if elegido:
            out["cancion"] = elegido
        return out

    @mcp.tool(title="Estado de una descarga")
    async def estado_descarga(track_id: str, ctx: Context = None) -> Dict[str, Any]:
        """Consulta el estado de la descarga de una canción (queued, processing, completed o error).

        Si ya terminó devuelve la ruta en la biblioteca y el link de descarga.
        """
        return job_summary(track_id, ctx)

    @mcp.tool(title="Obtener una canción (link o audio)", structured_output=False)
    async def obtener_cancion(track_id: Optional[str] = None, ruta: Optional[str] = None,
                              incluir_audio: bool = False, ctx: Context = None) -> list:
        """Devuelve el link de descarga de una canción de la biblioteca para dárselo al usuario.

        - track_id: id de una canción descargada con descargar_cancion, o
        - ruta: ruta relativa dentro de la biblioteca (la que devuelven buscar_en_biblioteca o
          descargar_cancion).
        - incluir_audio: si es true y el archivo pesa 15 MB o menos, también devuelve el audio
          embebido (base64) para entregarlo directamente.
        """
        if ruta:
            full = resolve_library_file(ruta)
            if not full:
                raise ToolError("No existe ese archivo de audio en la biblioteca")
        elif track_id:
            job = get_job(track_id)
            full = job_file(job)
            if not full:
                st = (job or {}).get("status")
                raise ToolError("Esa canción no está descargada en la biblioteca" +
                                (f" (estado: {st})" if st else "; usa descargar_cancion primero"))
        else:
            raise ToolError("Indica track_id o ruta")
        info = file_info(full, ctx)
        if track_id:
            info["track_id"] = track_id
        result: list = []
        if incluir_audio:
            if os.path.getsize(full) <= MAX_AUDIO_BYTES:
                audio = await asyncio.to_thread(lambda: Audio(path=full).to_audio_content())
                audio.mimeType = mime_for(full)
                info["audio_incluido"] = True
                result = [info, audio]
            else:
                info["audio_incluido"] = False
                info["nota"] = "El archivo pesa más de 15 MB; usa el link de descarga"
                result = [info]
        else:
            result = [info]
        return result

    @mcp.tool(title="Revisar una playlist contra la biblioteca")
    async def revisar_playlist(url: str, ctx: Context = None) -> Dict[str, Any]:
        """Importa (o actualiza) una playlist de Spotify o Deezer y la compara con la biblioteca.

        - url: link de la playlist (open.spotify.com/playlist/..., deezer.com/playlist/... o link
          corto de Deezer).
        Devuelve el playlist_id y un resumen: total, ya están, faltan (con una lista corta),
        por revisar (coincidencias dudosas o descargas pendientes de aprobar). La revisión fina
        se hace en la página /playlist de la app.
        """
        imp = await api("POST", "/api/playlists/import", {"url": url})
        pid = imp["id"]
        data = await api("GET", f"/api/playlists/{quote(pid, safe='')}")
        tracks = data["tracks"]
        out = {"playlist_id": pid, "nombre": imp.get("name"), "total_en_fuente": imp.get("total"),
               "cargadas": imp.get("loaded"), **playlist_counts(tracks),
               "lista_faltan": short_list(tracks, MISSING_STATES),
               "lista_por_revisar": short_list(tracks, REVIEW_STATES),
               "pagina_revision": f"{base_url(ctx)}/playlist"}
        loaded, total = imp.get("loaded") or 0, imp.get("total") or 0
        if loaded < total:
            why = ("Deezer marca las demás como no disponibles" if imp.get("source") == "deezer"
                   else "conecta la cuenta de Spotify en /playlist para leerla completa")
            out["nota"] = f"Solo se pudieron leer {loaded} de {total} canciones ({why})"
        return out

    @mcp.tool(title="Descargar las canciones faltantes de una playlist")
    async def descargar_faltantes(playlist_id: str) -> Dict[str, Any]:
        """Encola la descarga de todas las canciones que faltan (o fallaron) de una playlist ya revisada.

        - playlist_id: el que devuelve revisar_playlist.
        Se descargan de una en una con pausas para que YouTube no bloquee la IP, así que puede
        tardar varios minutos; consulta el avance con estado_playlist.
        """
        res = await api("POST", f"/api/playlists/{quote(playlist_id, safe='')}/download-missing")
        return {"playlist_id": playlist_id, "encoladas": res.get("queued", 0),
                "mensaje": "Descarga masiva iniciada; consulta el avance con estado_playlist"
                if res.get("queued") else "No hay canciones faltantes para descargar"}

    @mcp.tool(title="Estado de una playlist")
    async def estado_playlist(playlist_id: str) -> Dict[str, Any]:
        """Muestra cómo va una playlist: cuántas ya están, faltan, se están descargando o hay que revisar,
        y el avance de la descarga masiva si hay una corriendo.
        """
        data = await api("GET", f"/api/playlists/{quote(playlist_id, safe='')}")
        tracks = data["tracks"]
        bulk = data.get("bulk") or {}
        out = {"playlist_id": playlist_id, "nombre": (data.get("playlist") or {}).get("name"),
               **playlist_counts(tracks),
               "descarga_masiva": {"corriendo": bool(bulk.get("running")) and bulk.get("playlist_id") == playlist_id,
                                   "hechas": bulk.get("done"), "total": bulk.get("total")}
               if bulk.get("playlist_id") == playlist_id else {"corriendo": False},
               "lista_faltan": short_list(tracks, MISSING_STATES),
               "lista_por_revisar": short_list(tracks, REVIEW_STATES),
               "errores": [f"{t['name']} - {t['artist']}: {t.get('note') or ''}".strip()
                           for t in tracks if t["status"] == "error"][:10]}
        return out

    return mcp


def mount_mcp(app, catalog, download_and_process) -> FastMCP:
    """Registra el servidor MCP en la app FastAPI en /mcp (streamable HTTP)."""
    mcp = build_mcp(app, catalog, download_and_process)
    mcp.streamable_http_app()  # crea el session manager
    handler = _TokenGuard(StreamableHTTPASGIApp(mcp.session_manager))
    app.router.routes.append(Route("/mcp", endpoint=handler))

    prev_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a):
        async with prev_lifespan(a) as state, mcp.session_manager.run():
            yield state

    app.router.lifespan_context = lifespan
    print(f"MCP disponible en /mcp ({'con token' if config.MCP_TOKEN else 'sin autenticación'})")
    return mcp
