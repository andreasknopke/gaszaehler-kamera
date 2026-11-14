#!/usr/bin/env python3
"""Lokaler Live-Viewer fuer die Hi3510-IP-Kameras (192.168.188.43/.44).

Beispiele:
    python3 viewer.py                              # Kamera .43, Port 8099, 180 Grad
    python3 viewer.py --camera 192.168.188.44 --port 8098 --rotate 0 \
                      --zoom 636,468,94,24         # Kamera .44 mit Zaehler-Zoom

Die Kamera liefert multipart/x-mixed-replace (MJPEG). Chrome zeigt das nicht
mehr direkt in <img> an, deshalb holt dieser Server die Frames ab und gibt sie
als einzelne JPEGs aus. Ein Hintergrund-Thread haelt die Verbindung offen und
legt immer das neueste Bild ab - der Browser bekommt es sofort, auch wenn die
Kamera gerade langsam liefert. Mit --zoom wird zusaetzlich ein Ausschnitt
vergroessert und kontrastverstaerkt ausgeliefert (/zoom.jpg).
"""

import argparse
import io
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from PIL import Image, ImageEnhance, ImageFilter
except ImportError:  # ohne Pillow laeuft der Viewer ohne Drehen/Zoom
    Image = None

_lock = threading.Lock()
_frame = {"data": None, "zoom": None, "time": 0.0, "count": 0}
_cfg = {"rotate": 0, "zoom": None, "scale": 10, "stream": ""}
_fehler = {"zeit": 0.0, "text": ""}


def fix_orientation(jpg):
    """Dreht das Bild gemaess --rotate. Ohne Pillow bleibt es unveraendert."""
    if not _cfg["rotate"] or Image is None:
        return jpg
    try:
        im = Image.open(io.BytesIO(jpg)).rotate(_cfg["rotate"], expand=True)
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=85)
        return out.getvalue()
    except Exception as exc:
        print("Drehen fehlgeschlagen:", exc)
        return jpg


def make_zoom(jpg):
    """Ausschnitt vergroessern, lokalen Kontrast anheben, schaerfen."""
    rect = _cfg["zoom"]
    if not rect or Image is None:
        return None
    try:
        x, y, w, h = rect
        im = Image.open(io.BytesIO(jpg)).convert("L")
        c = im.crop((x, y, x + w, y + h))
        # lokale Kontrastnormierung: Bild / unscharfe Version
        blur = c.filter(ImageFilter.GaussianBlur(max(3, w // 6)))
        px = c.load()
        bp = blur.load()
        for yy in range(c.height):
            for xx in range(c.width):
                v = px[xx, yy] / max(bp[xx, yy], 8) * 128
                px[xx, yy] = int(min(255, max(0, v)))
        c = c.resize((c.width * _cfg["scale"], c.height * _cfg["scale"]), Image.LANCZOS)
        c = c.filter(ImageFilter.UnsharpMask(radius=2, percent=160, threshold=2))
        c = ImageEnhance.Contrast(c).enhance(1.3)
        out = io.BytesIO()
        c.save(out, format="JPEG", quality=92)
        return out.getvalue()
    except Exception as exc:
        print("Zoom fehlgeschlagen:", exc)
        return None


def read_frames():
    """Haelt den Stream offen und legt jedes fertige JPEG in den Cache."""
    while True:
        try:
            with urllib.request.urlopen(_cfg["stream"], timeout=20) as resp:
                buf = b""
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        start = buf.find(b"\xff\xd8")
                        if start < 0:
                            buf = buf[-2:]
                            break
                        end = buf.find(b"\xff\xd9", start)
                        if end < 0:
                            buf = buf[start:]
                            break
                        jpg = fix_orientation(buf[start:end + 2])
                        buf = buf[end + 2:]
                        zoom = make_zoom(jpg)
                        with _lock:
                            _frame["data"] = jpg
                            _frame["zoom"] = zoom
                            _frame["time"] = time.time()
                            _frame["count"] += 1
        except Exception as exc:
            _fehler["zeit"] = time.time()
            _fehler["text"] = str(exc)
            print("Stream-Fehler:", exc, "- neuer Versuch in 2s")
            time.sleep(2)


PAGE = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>Kamera {camera}</title>
<style>
  html, body {{ margin:0; height:100%; background:#111; color:#ddd;
                font-family:system-ui, sans-serif; }}
  #wrap {{ display:flex; flex-direction:column; align-items:center;
           justify-content:center; min-height:100%; gap:12px; padding:12px;
           box-sizing:border-box; }}
  img {{ background:#000; }}
  #full {{ max-width:96vw; max-height:60vh; }}
  #zoom {{ width:min(96vw, 1100px); image-rendering:auto; }}
  #bar {{ font-size:14px; color:#888; }}
  a {{ color:#8ab; }}
  .stale {{ color:#c96; }}
  h2 {{ font-size:15px; font-weight:600; color:#aaa; margin:0; }}
</style>
</head>
<body>
<div id="wrap">
{zoom_block}
  <img id="full" src="/frame.jpg" alt="Kamerabild">
  <div id="bar">
    <span id="status">lade ...</span> &middot;
    <a href="/frame.jpg" download="kamera.jpg">Foto speichern</a> &middot;
    <a href="{stream}" target="_blank">Original-Stream</a>
  </div>
</div>
<script>
const full = document.getElementById('full');
const zoom = document.getElementById('zoom');
const status = document.getElementById('status');
let fails = 0, letzteZahl = null, letzteZeit = null;

function tick() {{
  const t = performance.now();
  const a = new Image();
  a.onload = () => {{ full.src = a.src; }};
  a.onerror = () => {{ fails++; }};
  a.src = '/frame.jpg?t=' + t;
  if (zoom) {{
    const z = new Image();
    z.onload = () => {{ zoom.src = z.src; }};
    z.src = '/zoom.jpg?t=' + t;
  }}
  setTimeout(tick, 300);
}}

async function statusTick() {{
  try {{
    const r = await fetch('/status', {{cache: 'no-store'}});
    const s = await r.json();
    const jetzt = performance.now();
    let rate = '';
    if (letzteZahl !== null) {{
      const dt = (jetzt - letzteZeit) / 1000;
      if (dt > 0) rate = ' \u00b7 ' + ((s.bilder - letzteZahl) / dt).toFixed(1) + ' Bilder/s';
    }}
    letzteZahl = s.bilder; letzteZeit = jetzt;
    status.textContent = (s.alter < 0 ? 'noch kein Bild von der Kamera'
        : 'letztes Kamerabild: vor ' + s.alter.toFixed(1) + ' s') + rate
      + (fails ? ' \u00b7 ' + fails + ' Abruffehler' : '')
      + (s.fehler ? ' \u00b7 Streamfehler: ' + s.fehler : '');
    status.className = (s.alter < 0 || s.alter > 5 || s.fehler) ? 'stale' : '';
  }} catch (e) {{
    status.textContent = 'Viewer antwortet nicht';
    status.className = 'stale';
  }}
  setTimeout(statusTick, 1000);
}}

tick();
statusTick();
</script>
</body>
</html>
"""

ZOOM_BLOCK = '<h2>Zähler-Ausschnitt (vergrößert, Kontrast angehoben)</h2>\n  <img id="zoom" src="/zoom.jpg" alt="Zähler-Zoom">'


class Handler(BaseHTTPRequestHandler):
    def _send(self, data, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/frame.jpg", "/zoom.jpg"):
            with _lock:
                data = _frame["data"] if path == "/frame.jpg" else _frame["zoom"]
            if data is None:
                self.send_error(503, "Noch kein Bild von der Kamera")
                return
            self._send(data, "image/jpeg")
        elif path == "/status":
            with _lock:
                zeit, zahl = _frame["time"], _frame["count"]
            jetzt = time.time()
            alter = round(jetzt - zeit, 2) if zeit else -1
            frischer_fehler = bool(_fehler["zeit"]) and jetzt - _fehler["zeit"] < 30
            body = json.dumps({
                "alter": alter, "bilder": zahl,
                "fehler": _fehler["text"] if frischer_fehler else "",
            }).encode()
            self._send(body, "application/json")
        elif path in ("/", "/index.html"):
            body = PAGE.format(
                camera=_cfg["stream"].split("/")[2],
                stream=_cfg["stream"].replace("&", "&amp;"),
                zoom_block=ZOOM_BLOCK if _cfg["zoom"] else "",
            ).encode()
            self._send(body, "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass


def main():
    ap = argparse.ArgumentParser(description="Live-Viewer fuer Hi3510-Kameras")
    ap.add_argument("--camera", default="192.168.188.43", help="IP der Kamera")
    ap.add_argument("--port", type=int, default=8099, help="lokaler HTTP-Port")
    ap.add_argument("--rotate", type=int, default=180, choices=[0, 90, 180, 270])
    ap.add_argument("--zoom", default="", help="Ausschnitt x,y,breite,hoehe")
    ap.add_argument("--scale", type=int, default=10, help="Vergroesserung fuer --zoom")
    args = ap.parse_args()

    _cfg["rotate"] = args.rotate
    _cfg["scale"] = args.scale
    _cfg["stream"] = ("http://%s/cgi-bin/hi3510/snap.cgi?&-getstream&-chn=11"
                      % args.camera)
    if args.zoom:
        _cfg["zoom"] = tuple(int(v) for v in args.zoom.split(","))

    threading.Thread(target=read_frames, daemon=True).start()
    print(f"Viewer laeuft auf http://localhost:{args.port}/  (Strg+C beendet)")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
