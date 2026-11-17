"""Cookies de YouTube: subirlas (pegar / archivo / script automático), probarlas y borrarlas.

Con una sesión de YouTube, yt-dlp casi nunca recibe "Sign in to confirm you're not a bot".
Las cookies se guardan solo en el servidor (DOWNLOAD_DIR) y nunca se devuelven a la página.
"""
import json
import os
import re
import secrets
import tempfile
import threading
import time
from typing import Dict, List, Optional

import yt_dlp
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

import config

STATUS_PATH = os.path.join(config.DOWNLOAD_DIR, "youtube_cookies.status.json")
KEEP_DOMAINS = re.compile(r"(^|\.)(youtube\.com|google\.com|googlevideo\.com|youtu\.be)$", re.I)
AUTH_NAMES = {"SID", "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO", "SAPISID", "__Secure-3PAPISID"}
TEST_VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # "Me at the zoo", siempre disponible

_tokens: Dict[str, float] = {}   # token de un solo uso para el script -> expiración
_lock = threading.Lock()


def cookies_path() -> str:
    return config.YOUTUBE_COOKIES_PATH


# ------------------------------------------------------------------ parseo
def _from_json(text: str) -> List[List[str]]:
    """Exportaciones JSON (Cookie-Editor, EditThisCookie) -> filas Netscape."""
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("cookies", [])
    rows = []
    for c in data:
        dom = c.get("domain") or ""
        host_only = c.get("hostOnly", not dom.startswith("."))
        exp = c.get("expirationDate") or c.get("expires") or 0
        rows.append([dom, "FALSE" if host_only else "TRUE", c.get("path") or "/",
                     "TRUE" if c.get("secure") else "FALSE", str(int(float(exp or 0))),
                     c.get("name") or "", c.get("value") or ""])
    return rows


def parse_cookies(text: str) -> List[List[str]]:
    text = (text or "").strip().lstrip("﻿")
    if not text:
        raise HTTPException(400, "El archivo de cookies está vacío")
    if text[0] in "[{":
        try:
            rows = _from_json(text)
        except Exception:
            raise HTTPException(400, "No pude leer el JSON de cookies")
    else:
        rows = []
        for line in text.splitlines():
            line = line.rstrip("\r\n")
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) == 7:
                rows.append(parts)
    if not rows:
        raise HTTPException(400, "No encontré cookies. Debe ser formato Netscape (cookies.txt) o JSON.")
    # solo YouTube/Google: no guardamos cookies de otros sitios aunque vengan en el archivo
    rows = [r for r in rows if KEEP_DOMAINS.search(r[0].lstrip("."))]
    if not any(r[5] in AUTH_NAMES and "youtube.com" in r[0] for r in rows):
        raise HTTPException(400, "Las cookies no tienen una sesión de YouTube. Entra a youtube.com con tu cuenta y vuelve a exportarlas.")
    return rows


def to_netscape(rows: List[List[str]]) -> str:
    out = ["# Netscape HTTP Cookie File", "# Guardado por music-downloader", ""]
    for r in rows:
        dom, flag = r[0], r[1]
        if dom.startswith(".") and flag == "FALSE":
            flag = "TRUE"
        out.append("\t".join([dom, flag, r[2] or "/", r[3], r[4] or "0", r[5], r[6]]))
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ prueba
def test_cookie_file(path: str) -> Dict:
    """Comprueba que la sesión está iniciada (historial requiere login) y que un video se lee."""
    base = {"quiet": True, "no_warnings": True, "skip_download": True, "cookiefile": path}
    # yt-dlp reescribe el cookiefile al terminar; probamos sobre una copia para no tocar el original
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tmp:
        tmp.write(open(path).read())
    base["cookiefile"] = tmp.name
    try:
        try:
            with yt_dlp.YoutubeDL({**base, "extract_flat": True, "playlistend": 1}) as y:
                y.extract_info(":ythistory", download=False)
        except Exception as e:
            msg = str(e)
            if "logged in" in msg.lower() or "sign in" in msg.lower() or "login" in msg.lower():
                return {"ok": False, "logged_in": False, "message": "YouTube no reconoce la sesión: las cookies caducaron o no son de una cuenta iniciada."}
            return {"ok": False, "logged_in": None, "message": f"No se pudo comprobar la sesión: {msg[:200]}"}
        try:
            with yt_dlp.YoutubeDL({**base, "format": "bestaudio/best"}) as y:
                y.extract_info(TEST_VIDEO, download=False)
        except Exception as e:
            return {"ok": False, "logged_in": True, "message": f"La sesión es válida pero YouTube bloqueó la prueba: {str(e)[:200]}"}
        return {"ok": True, "logged_in": True, "message": "Sesión de YouTube válida"}
    finally:
        os.unlink(tmp.name)


def _read_status() -> Dict:
    try:
        return json.load(open(STATUS_PATH))
    except Exception:
        return {}


def _write_status(**kw):
    st = _read_status()
    st.update(kw)
    with open(STATUS_PATH, "w") as f:
        json.dump(st, f)
    return st


def save_cookies(text: str, source: str) -> Dict:
    rows = parse_cookies(text)
    with _lock:
        tmp = cookies_path() + ".nuevo"
        with open(tmp, "w") as f:
            f.write(to_netscape(rows))
        os.chmod(tmp, 0o600)
        result = test_cookie_file(tmp)
        if result["logged_in"] is False:
            os.unlink(tmp)
            raise HTTPException(400, result["message"])
        os.replace(tmp, cookies_path())
    expiries = [int(r[4]) for r in rows if r[5] in AUTH_NAMES and r[4].isdigit() and int(r[4]) > 0]
    return _write_status(uploaded_at=int(time.time()), source=source, count=len(rows),
                         expires_at=min(expiries) if expiries else None,
                         tested_at=int(time.time()), test=result)


def status() -> Dict:
    p = cookies_path()
    st = _read_status() if os.path.exists(p) else {}
    return {"configured": os.path.exists(p), **{k: st.get(k) for k in
            ("uploaded_at", "source", "count", "expires_at", "tested_at", "test")}}


# ------------------------------------------------------------------ scripts de extracción
BROWSERS = ["firefox", "chrome", "edge", "brave", "chromium", "opera", "vivaldi"]


def _script_unix(upload_url: str, binary: str) -> str:
    browsers = " ".join(BROWSERS + (["safari"] if binary == "yt-dlp_macos" else []))
    return f"""#!/bin/bash
# music-downloader: extrae tus cookies de YouTube de este equipo y las sube al servidor.
# Solo se envían cookies de youtube.com / google.com. El código de subida vence en 30 minutos.
set -u
UPLOAD="{upload_url}"
DIR="$(mktemp -d)"; trap 'rm -rf "$DIR"' EXIT
echo "Descargando yt-dlp..."
curl -fsSL -o "$DIR/yt-dlp" "https://github.com/yt-dlp/yt-dlp/releases/latest/download/{binary}" || {{ echo "No se pudo descargar yt-dlp"; read -r -p "Enter para salir"; exit 1; }}
chmod +x "$DIR/yt-dlp"
for B in {browsers}; do
  echo "Probando $B..."
  if "$DIR/yt-dlp" --quiet --no-warnings --cookies-from-browser "$B" --cookies "$DIR/all.txt" --skip-download "{TEST_VIDEO}" >/dev/null 2>&1 \\
     && grep -qE '\\.youtube\\.com.*(SID|LOGIN_INFO)' "$DIR/all.txt"; then
    grep -E '^#|youtube\\.com|google\\.com|googlevideo\\.com|youtu\\.be' "$DIR/all.txt" > "$DIR/yt.txt"
    echo "Encontré tu sesión de YouTube en $B. Subiendo..."
    curl -sS --fail-with-body -X POST --data-binary "@$DIR/yt.txt" -H "Content-Type: text/plain" "$UPLOAD&source=$B" && echo && echo "Listo. Ya puedes cerrar esta ventana."
    read -r -p "Enter para salir"; exit 0
  fi
  rm -f "$DIR/all.txt"
done
echo "No encontré una sesión de YouTube en ningún navegador."
echo "Entra a youtube.com con tu cuenta, CIERRA el navegador y vuelve a ejecutar este archivo."
read -r -p "Enter para salir"
"""


def _script_windows(upload_url: str) -> str:
    browsers = ",".join(f"'{b}'" for b in BROWSERS)
    ps = f"""$ErrorActionPreference='SilentlyContinue'
$up='{upload_url}'
$d=Join-Path $env:TEMP ('ytc-'+[guid]::NewGuid()); New-Item -ItemType Directory $d | Out-Null
try {{
  Write-Host 'Descargando yt-dlp...'
  Invoke-WebRequest -UseBasicParsing 'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe' -OutFile "$d\\yt-dlp.exe"
  if (-not (Test-Path "$d\\yt-dlp.exe")) {{ Write-Host 'No se pudo descargar yt-dlp'; exit 1 }}
  foreach ($b in @({browsers})) {{
    Write-Host "Probando $b..."
    & "$d\\yt-dlp.exe" --quiet --no-warnings --cookies-from-browser $b --cookies "$d\\all.txt" --skip-download '{TEST_VIDEO}' 2>$null | Out-Null
    if ((Test-Path "$d\\all.txt") -and (Select-String -Path "$d\\all.txt" -Pattern '\\.youtube\\.com.*(SID|LOGIN_INFO)' -Quiet)) {{
      $lines = Get-Content "$d\\all.txt" | Where-Object {{ $_ -match '^#|youtube\\.com|google\\.com|googlevideo\\.com|youtu\\.be' }}
      Write-Host "Encontre tu sesion de YouTube en $b. Subiendo..."
      try {{
        $r = Invoke-WebRequest -UseBasicParsing -Method Post -ContentType 'text/plain; charset=utf-8' -Body ($lines -join "`n") -Uri ($up + '&source=' + $b)
        Write-Host $r.Content; Write-Host 'Listo. Ya puedes cerrar esta ventana.'
      }} catch {{ Write-Host ('Error al subir: ' + $_.ErrorDetails.Message) }}
      exit 0
    }}
    Remove-Item "$d\\all.txt" -ErrorAction SilentlyContinue
  }}
  Write-Host 'No encontre una sesion de YouTube en ningun navegador.'
  Write-Host 'Entra a youtube.com con tu cuenta, CIERRA el navegador y vuelve a ejecutar este archivo.'
  Write-Host 'Nota: Chrome en Windows bloquea la lectura de sus cookies; usa Firefox o el metodo manual.'
}} finally {{ Remove-Item -Recurse -Force $d }}
"""
    import base64
    enc = base64.b64encode(ps.encode("utf-16-le")).decode()
    return ("@echo off\r\n"
            "rem music-downloader: extrae tus cookies de YouTube y las sube al servidor.\r\n"
            "rem Solo se envian cookies de youtube.com / google.com.\r\n"
            f"powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {enc}\r\n"
            "pause\r\n")


# ------------------------------------------------------------------ router
class CookiesReq(BaseModel):
    text: str


def build_router() -> APIRouter:
    r = APIRouter()

    @r.get("/api/youtube-cookies/status")
    async def get_status():
        return status()

    @r.post("/api/youtube-cookies")
    def upload(req: CookiesReq):
        save_cookies(req.text, "manual")
        return status()

    @r.post("/api/youtube-cookies/test")
    def retest():
        if not os.path.exists(cookies_path()):
            raise HTTPException(404, "No hay cookies guardadas")
        res = test_cookie_file(cookies_path())
        _write_status(tested_at=int(time.time()), test=res)
        return status()

    @r.delete("/api/youtube-cookies")
    async def remove():
        for p in (cookies_path(), STATUS_PATH):
            if os.path.exists(p):
                os.remove(p)
        return status()

    @r.get("/api/youtube-cookies/helper/{os_name}")
    async def helper(os_name: str, request: Request):
        if os_name not in ("windows", "mac", "linux"):
            raise HTTPException(400, "Sistema no válido")
        tok = secrets.token_urlsafe(24)
        now = time.time()
        with _lock:
            for k in [k for k, v in _tokens.items() if v < now]:
                _tokens.pop(k, None)
            _tokens[tok] = now + 30 * 60
        root = request.scope.get("root_path", "")
        upload_url = f"{str(request.base_url).rstrip('/')}{root}/api/youtube-cookies/upload?token={tok}"
        if os_name == "windows":
            body, name, mime = _script_windows(upload_url), "cookies-youtube.bat", "application/x-bat"
        elif os_name == "mac":
            body, name, mime = _script_unix(upload_url, "yt-dlp_macos"), "cookies-youtube.command", "text/x-shellscript"
        else:
            body, name, mime = _script_unix(upload_url, "yt-dlp_linux"), "cookies-youtube.sh", "text/x-shellscript"
        return Response(body, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @r.post("/api/youtube-cookies/upload", response_class=PlainTextResponse)
    async def upload_from_script(request: Request, token: str, source: str = "script"):
        with _lock:
            exp = _tokens.pop(token, None)
        if not exp or exp < time.time():
            raise HTTPException(403, "El código de subida venció o ya se usó. Descarga el script otra vez desde la página.")
        text = (await request.body()).decode("utf-8", "replace")
        import asyncio
        st = await asyncio.get_running_loop().run_in_executor(None, save_cookies, text, f"auto ({source[:20]})")
        t = st.get("test") or {}
        return f"OK: {t.get('message', 'cookies guardadas')}"

    return r
