"""
build_web.py - build the static web demo of the ops dashboard (for Vercel or any static host).

    .venv\\Scripts\\python tools\\build_web.py        -> writes autotrack/web/

The web demo is the SAME dashboard page (static/index.html), but instead of talking to
server.py it runs the real airport simulation in the visitor's browser (Pyodide, in a Web
Worker - see web_src/). Nothing is pre-recorded: it's the actual sim.py/tracker.py code.
The Live ESP32 and Room demo tabs are hidden (they need a laptop's Bluetooth).

Re-run this after changing the simulation code or the page, then commit autotrack/web/.
Deploy: Vercel project with Root Directory = autotrack/web, Framework = Other, no build command.
Try it locally:  .venv\\Scripts\\python -m http.server 8080 --directory web  ->  http://127.0.0.1:8080
"""

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "web"
SRC = ROOT / "web_src"

PY_MODULES = ["__init__", "layout", "world", "radio", "positioning", "tracker", "securebag", "feed", "sim", "webapi"]
DATA = ["data/replay.json", "bhs/data/network_layout.json", "securebag_cache/verdicts.json"]

HIDE_CSS = """<style>
  /* web demo: these tabs need the real laptop + ESP32 */
  nav button[data-tab="live"], nav button[data-tab="room"] { display: none !important; }
</style>
"""


def build():
    if not (ROOT / "data" / "replay.json").exists():
        from build_replay import build as build_replay  # noqa: E402  (same tools/ folder)
        build_replay()
    shutil.rmtree(OUT, ignore_errors=True)
    (OUT / "py").mkdir(parents=True)

    # the page: identical, plus the shim (before the page's own script) and the hidden tabs
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert html.count("<script>") >= 1 and "</head>" in html
    html = html.replace("</head>", HIDE_CSS + "</head>", 1)
    html = html.replace("<script>", '<script src="/pyshim.js"></script>\n<script>', 1)
    html = html.replace("<title>", "<title>[web demo] ", 1)
    (OUT / "index.html").write_text(html, encoding="utf-8")

    # CSS and the SecureBag photos, at the same /static/... paths the page uses
    shutil.copy(ROOT / "static" / "style.css", OUT / "style.css.tmp")
    (OUT / "static").mkdir()
    shutil.move(str(OUT / "style.css.tmp"), str(OUT / "static" / "style.css"))
    shutil.copytree(ROOT / "static" / "bags", OUT / "static" / "bags")

    # the worker + shim
    for f in ("sim-worker.js", "pyshim.js"):
        shutil.copy(SRC / f, OUT / f)

    # the Python the browser runs, and the data it reads
    files = []
    for m in PY_MODULES:
        rel = f"autotrack/{m}.py"
        (OUT / "py" / "autotrack").mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, OUT / "py" / rel)
        files.append(rel)
    for rel in DATA:
        (OUT / "py" / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, OUT / "py" / rel)
        files.append(rel)
    (OUT / "py" / "files.json").write_text(json.dumps(files, indent=1), encoding="utf-8")

    size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"Built {OUT} ({len(files)} Python/data files, {size / 1e6:.1f} MB total)")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    build()
