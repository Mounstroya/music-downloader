#!/usr/bin/env python3
"""Construye la demo estática de Music Downloader para GitHub Pages.

Renderiza las plantillas reales de ``backend/templates`` con Jinja2 (con un
``request`` y un ``url_for`` falsos), copia ``backend/static`` tal cual e inyecta
``mock-api.js``, que simula el API en el navegador. Así la demo siempre muestra la
interfaz actual de la app, sin duplicarla.

Estructura de salida (por defecto ``demo/dist/``)::

    index.html              página de inicio (demo/landing)
    landing.css, images/    recursos de la página de inicio
    app/index.html          buscador          (backend/templates/index.html)
    app/playlist/index.html revisar playlist  (backend/templates/playlist.html)
    app/cookies/index.html  cookies de YouTube (backend/templates/cookies.html)
    app/static/             copia de backend/static
    app/demo-kit/           mock-api.js y demo.css

Todas las rutas son relativas (``<base href="./">`` o ``"../"``), así que la salida
funciona igual en ``https://usuario.github.io/music-downloader/`` que en la raíz de
un dominio o en ``python3 -m http.server``.

Uso::

    pip install jinja2
    python3 demo/build.py            # genera demo/dist
    python3 demo/build.py --out /tmp/sitio
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

try:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
except ImportError:  # pragma: no cover
    sys.exit("Falta Jinja2: pip install jinja2")

DEMO_DIR = Path(__file__).resolve().parent
ROOT = DEMO_DIR.parent
BACKEND = ROOT / "backend"
TEMPLATES = BACKEND / "templates"
STATIC = BACKEND / "static"
KIT_DIR = "demo-kit"  # carpeta de la demo dentro de app/ (sin "_" para que Jekyll no la ignore)

# plantilla -> carpeta (relativa a app/) donde queda su index.html
PAGES = {
    "index.html": "",
    "playlist.html": "playlist",
    "cookies.html": "cookies",
}


class FakeRequest:
    """Lo mínimo que usan las plantillas: ``request.scope.get('root_path', '')``."""

    def __init__(self, root_path: str):
        self.scope = {"root_path": root_path, "type": "http"}


def banner_html(page: str) -> str:
    # el <base> de todas las páginas apunta a app/, así que "../" es la página de inicio
    return (
        f'<div class="md-demo-banner" data-page="{page}" role="note">'
        '<span><b>Demo:</b> los datos son de ejemplo y no se descarga nada.</span>'
        '<a href="../#instalar">Instálala en tu equipo →</a>'
        '</div>'
    )


def render_page(env: Environment, template: str, subdir: str) -> str:
    depth = len([p for p in subdir.split("/") if p])
    up = "../" * depth
    # <base href="{{ root_path }}/">  ->  "./" en app/, "../" en app/playlist/
    root_path = up.rstrip("/") if depth else "."

    def url_for(name: str, **params) -> str:
        if name == "static":
            return f"{up}static/{params.get('path', '')}"
        # cualquier otra ruta nombrada: relativa a app/
        return f"{up}{params.get('path', name)}"

    html = env.get_template(template).render(request=FakeRequest(root_path), url_for=url_for)

    # 1) mock del API justo después de <base>: corre antes que cualquier script de la app
    head = (f'\n    <link rel="stylesheet" href="{KIT_DIR}/demo.css">'
            f'\n    <script src="{KIT_DIR}/mock-api.js"></script>')
    html, n = re.subn(r"(<base\b[^>]*>)", lambda m: m.group(1) + head, html, count=1, flags=re.I)
    if not n:
        raise SystemExit(f"{template}: no encontré <base href>; la demo necesita esa etiqueta")

    # 2) franja de "Demo" al inicio del <body>
    page = Path(template).stem
    html, n = re.subn(r"(<body\b[^>]*>)", lambda m: m.group(1) + "\n" + banner_html(page), html,
                      count=1, flags=re.I)
    if not n:
        raise SystemExit(f"{template}: no encontré <body>")
    return html


def build(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    app = out / "app"
    app.mkdir(parents=True)

    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]))
    for template, subdir in PAGES.items():
        dest = app / subdir / "index.html"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(render_page(env, template, subdir), encoding="utf-8")
        print(f"  app/{(subdir + '/') if subdir else ''}index.html  <- templates/{template}")

    shutil.copytree(STATIC, app / "static")
    kit = app / KIT_DIR
    kit.mkdir()
    for f in ("mock-api.js", "demo.css"):
        shutil.copy2(DEMO_DIR / f, kit / f)

    # página de inicio + capturas del README
    for f in (DEMO_DIR / "landing").iterdir():
        if f.is_file():
            shutil.copy2(f, out / f.name)
    (out / "images").mkdir()
    for img in (ROOT / "images").glob("*.png"):
        shutil.copy2(img, out / "images" / img.name)

    # Pages con Actions no usa Jekyll, pero por si alguien publica la carpeta a mano
    (out / ".nojekyll").write_text("")
    print(f"Listo: {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=DEMO_DIR / "dist", help="carpeta de salida (se borra antes)")
    args = ap.parse_args()
    build(args.out.resolve())


if __name__ == "__main__":
    main()
