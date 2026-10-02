#!/usr/bin/env python3
"""Assembla la demo: l'interfaccia vera (app/static) + un finto server nel browser.

  python demo/build.py                      # crea demo/demo.html (file unico, apribile con doppio clic)
  python demo/build.py --fragment OUT.html  # variante senza <html>/<head>/<body>, per la pubblicazione online

Va rilanciato dopo ogni modifica a app/static/ per tenere la demo allineata.
"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
DEMO = ROOT / "demo"

TITLE = "Jetson AI Server"
BANNER = ('<div class="demo-banner" role="note"><b>Demo</b> · dati di esempio: niente viene salvato né inviato. '
          "Il server vero gira sul tuo Jetson.</div>")
DEMO_CSS = """
.demo-banner { background: var(--info-soft); color: var(--info); font-size: .82rem; line-height: 1.4;
  padding: 6px 16px; text-align: center; border-bottom: 1px solid var(--border); }
.demo-banner b { letter-spacing: .08em; text-transform: uppercase; margin-right: 2px; }
"""


def data_url(path: Path, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def script(text: str) -> str:
    # Un "</script" dentro uno script inline chiuderebbe il tag in anticipo.
    return text.replace("</script", "<\\/script")


def build(fragment: bool) -> str:
    css = (STATIC / "style.css").read_text(encoding="utf-8") + DEMO_CSS
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    backend_js = (DEMO / "backend.js").read_text(encoding="utf-8")
    coco = json.loads((DEMO / "coco_classes.json").read_text(encoding="utf-8"))
    result = json.loads((DEMO / "real_result.json").read_text(encoding="utf-8"))
    assets = {
        "video": data_url(DEMO / "assets" / "annotato.mp4", "video/mp4"),
        "video_webm": data_url(DEMO / "assets" / "annotato.webm", "video/webm"),
        "image": data_url(DEMO / "assets" / "annotata.jpg", "image/jpeg"),
    }
    data_js = (
        f"window.DEMO_ASSETS = {json.dumps(assets)};\n"
        f"window.DEMO_RESULT = {json.dumps(result, ensure_ascii=False)};\n"
        f"window.DEMO_COCO = {json.dumps(coco)};\n"
    )
    body = (
        f"{BANNER}\n<div id=\"app\"></div>\n"
        f"<script>{script(data_js)}</script>\n"
        f"<script>{script(backend_js)}</script>\n"
        f"<script>{script(app_js)}</script>\n"
    )
    if fragment:
        return f"<title>{TITLE}</title>\n<style>\n{css}\n</style>\n{body}"
    return (
        '<!doctype html>\n<html lang="it">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        f"<title>{TITLE}</title>\n<style>\n{css}\n</style>\n</head>\n<body>\n{body}</body>\n</html>\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fragment", metavar="OUT", help="scrive la variante per la pubblicazione online")
    args = parser.parse_args()
    if args.fragment:
        Path(args.fragment).write_text(build(True), encoding="utf-8")
        print(f"Scritto {args.fragment}")
    else:
        out = DEMO / "demo.html"
        out.write_text(build(False), encoding="utf-8")
        print(f"Scritto {out} ({out.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
