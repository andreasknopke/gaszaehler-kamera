#!/usr/bin/env python3
"""Kernlogik: Bild holen -> Zaehlerbloecke -> OCR -> Plausibilitaet -> Datenbank.

Wird von sample.py (Cron) und server.py (Web-UI) benutzt.

Erkennungsstrategie
-------------------
1. Zaehlerbloecke werden als grosse dunkle Rechtecke im Bild gefunden
   (unabhaengig von der exakten Kameraposition).
2. Die Zaehlerzeile wird per Scheren waagerecht gezogen (die Kamera schaut
   schraeg auf den Zaehler).
3. Tesseract liest Haupt- und Nachkommablotz in mehreren Vorverarbeitungs-
   varianten -> Kandidatenliste.
4. Der Kandidat wird gegen den letzten Messwert geprueft: der Zaehler laeuft
   nur vorwaerts und hoechstens um max_anstieg_m3_pro_minute. Damit fallen
   OCR-Fehler auf, ohne die Zeitreihe zu verfaelschen.
"""

import io
import math
import json
import re
import sqlite3
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter, ImageOps
from scipy import ndimage

BASE = Path(__file__).resolve().parent
CFG_PATH = BASE / "config.json"
DB_PATH = BASE / "daten.db"
FOTO_DIR = BASE / "fotos"
LOG_DIR = BASE / "logs"
TESSERACT = Path.home() / ".local/ocr/bin/tesseract"

for d in (FOTO_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- Konfiguration

def load_config():
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def save_config(cfg):
    CFG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------------- Datenbank

SCHEMA = """
CREATE TABLE IF NOT EXISTS messungen (
    ts        INTEGER PRIMARY KEY,
    m3        REAL NOT NULL,
    quelle    TEXT NOT NULL,     -- ocr | manuell | start
    konfidenz REAL,
    roh       TEXT               -- erkannter Rohtext / Details
);
CREATE INDEX IF NOT EXISTS idx_ts ON messungen(ts);
CREATE TABLE IF NOT EXISTS zustand (
    schluessel TEXT PRIMARY KEY,
    wert        TEXT
);
"""


def db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.executescript(SCHEMA)
    spalten = [r[1] for r in con.execute("PRAGMA table_info(messungen)")]
    if "ablesung" not in spalten:
        con.execute("ALTER TABLE messungen ADD COLUMN ablesung TEXT")
        con.commit()
    return con


def letzte_messung():
    con = db()
    try:
        return con.execute(
            "SELECT ts, m3, quelle FROM messungen ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()


def speichere_messung(ts, m3, quelle, konfidenz=None, roh=None, ablesung=None):
    con = db()
    try:
        con.execute(
            "INSERT OR REPLACE INTO messungen (ts, m3, quelle, konfidenz, roh, ablesung)"
            " VALUES (?,?,?,?,?,?)",
            (int(ts), round(float(m3), 4), quelle, konfidenz, roh, ablesung),
        )
        con.commit()
    finally:
        con.close()


# ----------------------------------------------------------------------- Bild

def _jpeg(daten):
    s = daten.find(b"\xff\xd8")
    e = daten.find(b"\xff\xd9", s)
    if s < 0 or e < 0:
        raise ValueError("kein JPEG")
    return daten[s:e + 2]


def _hole_jpeg(url, maxbytes=3_000_000, timeout=12):
    """Erstes vollstaendiges JPEG holen.

    Die Kamera liefert auf der Stream-URL einen *endlosen* MJPEG-Strom. Ein
    simples read(N) laeuft deshalb in den Timeout (und liefert dabei ein
    beliebiges Bild aus der Mitte des Stroms). Wir lesen stattdessen in
    Bloecken, bis ein JPEG von SOI bis EOI komplett ist.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "verbrauch/1.0"})
    puffer = b""
    with urllib.request.urlopen(req, timeout=timeout) as antwort:
        while len(puffer) < maxbytes:
            teil = antwort.read(65536)
            if not teil:
                break
            puffer += teil
            start = puffer.find(b"\xff\xd8")
            if start >= 0:
                ende = puffer.find(b"\xff\xd9", start + 2)
                if ende >= 0:
                    return puffer[start:ende + 2]
    raise ValueError("kein vollstaendiges JPEG im Datenstrom")


def _bild_aus(url, drehen=0):
    im = Image.open(io.BytesIO(_hole_jpeg(url)))
    im.load()
    im = im.convert("L")
    if drehen:
        im = im.rotate(drehen, expand=True)
    return im


def pruefe_orientierung(im):
    """180-Grad-Lage per OCR bestimmen (nur bei der Kalibrierung noetig).

    Rueckgabe: 0 oder 180. In der richtigen Lage liest Tesseract die
    Hauptziffern, in der verkehrten praktisch nichts.
    """
    def treffer(bild):
        try:
            haupt, _, _, _ = lese_zaehler(bild)
        except Exception:
            return 0
        return sum(len(t) for t, _ in haupt)

    gerade, gekippt = treffer(im), treffer(im.rotate(180, expand=True))
    return 180 if gekippt > gerade else 0


def hole_bild(cfg=None):
    cfg = cfg or load_config()
    k = cfg["kamera"]
    fehler = []
    # Der Viewer liefert das Bild bereits in Anzeige-Orientierung, die Kamera
    # direkt dagegen um 180 Grad verdreht. Deshalb pro Quelle eigener Winkel.
    quellen = [
        (k["url_direkt"], k.get("drehen", 0)),
        (k["url_ansicht"], k.get("drehen_ansicht", 0)),
    ]
    for url, drehen in quellen:
        try:
            return _bild_aus(url, drehen)
        except Exception as exc:
            fehler.append(f"{url}: {exc}")
    raise RuntimeError("Kamera nicht erreichbar: " + " | ".join(fehler))


def zaehlerbloecke(im):
    """Die beiden dunklen Zaehlerfenster als (x0, x1, y0, y1) finden."""
    a = np.asarray(im, dtype=np.float32)
    m = a < 85
    lbl, n = ndimage.label(m)
    bloecke = []
    for i, sl in enumerate(ndimage.find_objects(lbl), 1):
        if sl is None:
            continue
        h = sl[0].stop - sl[0].start
        w = sl[1].stop - sl[1].start
        if 90 <= h <= 260 and 140 <= w <= 700:
            bloecke.append((sl[1].start, sl[1].stop, sl[0].start, sl[0].stop, w * h))
    bloecke.sort(key=lambda b: b[0])
    if len(bloecke) < 2:
        raise RuntimeError("Zaehlerfenster nicht gefunden")
    return [b[:4] for b in bloecke[:2]]


def scheren(im, bloecke=None):
    """Zaehlerzeile waagerecht ziehen: Steigung aus der Streifenmitte."""
    a = np.asarray(im, dtype=np.float32)
    H, W = a.shape
    if bloecke:
        y0 = max(0, min(b[2] for b in bloecke) - 30)
        y1 = min(H, max(b[3] for b in bloecke) + 30)
        xa = max(20, min(b[0] for b in bloecke) - 40)
        xb = min(W - 20, max(b[1] for b in bloecke) + 40)
    else:
        y0, y1, xa, xb = 150, H - 80, 40, W - 40
    xs, ys = [], []
    for x in range(xa, xb, 8):
        win = a[y0:y1, x]
        if len(win) < 80:
            continue
        sums = np.convolve(win, np.ones(60), "valid")
        if len(sums) == 0:
            continue
        ys.append(y0 + int(np.argmin(sums)) + 30)
        xs.append(x)
    if len(xs) < 30:
        raise RuntimeError("Zaehlerzeile nicht gefunden")
    m, b = np.polyfit(xs, ys, 1)
    out = np.zeros_like(a)
    for x in range(W):
        dy = int(round(m * (x - 40)))
        if dy >= 0:
            out[:H - dy, x] = a[dy:, x]
        else:
            out[-dy:, x] = a[:H + dy, x]
    return out, m, float(np.median([m * x + b for x in xs]))


def block_bild(geschoren, block, mitte):
    """Block aus dem geschorenen Bild schneiden - Zeilenlage dort neu suchen."""
    x0, x1 = block[0], block[1]
    spalte = geschoren[:, x0:x1]
    profil = spalte.mean(axis=1)
    # dunkelsten zusammenhaengenden Bereich (Zaehlerfenster) finden
    dunkel = profil < np.percentile(profil, 35)
    laeufe, start = [], None
    for i, d in enumerate(dunkel):
        if d and start is None:
            start = i
        if not d and start is not None:
            if i - start > 40:
                laeufe.append((start, i))
            start = None
    if start is not None and len(dunkel) - start > 40:
        laeufe.append((start, len(dunkel)))
    if not laeufe:
        y0, y1 = int(mitte) - 60, int(mitte) + 60
    else:
        y0, y1 = max(laeufe, key=lambda r: r[1] - r[0])
    h = int((y1 - y0) * 0.68)
    yc = (y0 + y1) // 2
    crop = geschoren[max(0, yc - h):min(geschoren.shape[0], yc + h), x0:x1]
    return Image.fromarray(crop.clip(0, 255).astype(np.uint8))


# ------------------------------------------------------------------------ OCR

def _tesseract(img):
    p = LOG_DIR / "_ocr.png"
    img.save(p)
    r = subprocess.run(
        [str(TESSERACT), str(p), "stdout", "--psm", "7",
         "-c", "tessedit_char_whitelist=0123456789"],
        capture_output=True, text=True, timeout=60,
    )
    return re.sub(r"[^0-9]", "", r.stdout)


def block_gedreht(im, block, winkel):
    """Block ausschneiden und um den Zaehlerwinkel begradigen."""
    x0, x1, y0, y1 = block
    c = im.crop((x0 - 8, y0 - 8, x1 + 8, y1 + 8))
    return c.rotate(winkel, resample=Image.BICUBIC, expand=True, fillcolor=0)


def ocr_kandidaten(gedreht, erwartete_laenge):
    """Mehrere Vorverarbeitungen -> Kandidatenliste (Text -> Stimmen)."""
    cy = gedreht.height // 2
    treffer = {}
    for links, rechts in ((0, 0), (12, 12), (20, 20), (30, 10)):
        band = gedreht.crop((links, cy - 62, gedreht.width - rechts, cy + 62))
        for scale in (4, 5):
            for pct in (None, 45, 55, 65, 75):
                x = band.resize((band.width * scale, band.height * scale), Image.LANCZOS)
                x = ImageOps.autocontrast(x, cutoff=1)
                if pct is not None:
                    a = np.asarray(x, dtype=np.float32)
                    t = np.percentile(a, pct)
                    x = Image.fromarray(np.where(a > t, 255, 0).astype(np.uint8))
                x = ImageOps.invert(x)
                t = _tesseract(x)
                if not t or (erwartete_laenge and len(t) != erwartete_laenge):
                    continue
                treffer[t] = treffer.get(t, 0) + 1
    return sorted(treffer.items(), key=lambda kv: -kv[1])


# ---------------------------------------------------------------- Auswertung

def lese_zaehler(im, erwartete_laenge=(5, 3)):
    bloecke = zaehlerbloecke(im)
    _, m, _ = scheren(im, bloecke)
    winkel = math.degrees(math.atan(m))
    bilder = [block_gedreht(im, b, winkel) for b in bloecke]
    kh = ocr_kandidaten(bilder[0], erwartete_laenge[0])
    kn = ocr_kandidaten(bilder[1], erwartete_laenge[1])
    return kh, kn, bilder[0], bilder[1]


def wert_aus(haupt, nach):
    return int(haupt) + int(nach) / 1000.0


def plausibler_wert(kandidaten, letzter_wert, max_anstieg, minuten):
    """Kandidaten gegen den letzten Wert filtern (monoton, begrenzte Rate)."""
    if letzter_wert is None:
        # erster Wert: haeufigster Kandidat
        for (h, hn), _ in kandidaten:
            return wert_aus(h, hn), 1.0
        return None, 0.0
    oben = letzter_wert + max(max_anstieg * max(minuten, 1), 0.02)
    gueltig = []
    for (h, hn), stimmen in kandidaten:
        w = wert_aus(h, hn)
        if letzter_wert - 0.002 <= w <= oben:
            gueltig.append((w, stimmen))
    if not gueltig:
        return None, 0.0
    gueltig.sort(key=lambda x: (-x[1], x[0]))
    max_stimmen = gueltig[0][1]
    beste = [w for w, s in gueltig if s == max_stimmen]
    return min(beste), max_stimmen


# --------------------------------------------------------------------- Tarif

def kwh(wert_m3, tarif):
    return wert_m3 * float(tarif["brennwert_kwh_m3"]) * float(tarif["zustandszahl"])


def kosten_energie(wert_m3, tarif):
    return kwh(wert_m3, tarif) * float(tarif["arbeitspreis_ct_kwh"]) / 100.0


def grundkosten(stunden, tarif):
    return float(tarif["grundpreis_eur_monat"]) * stunden / (24 * 30.4375)


# ------------------------------------------------------------------- Messung

def messung_durchfuehren(cfg=None, speichern=True):
    """Einmal messen. Rueckgabe: dict mit status, wert, konfidenz, ..."""
    cfg = cfg or load_config()
    e = cfg["erfassung"]

    letzte = letzte_messung()
    if letzte is None:
        speichere_messung(time.time(), float(e["startwert_m3"]), "start", 1.0, None)
        letzte = letzte_messung()
    ts_letzte, letzter_wert = letzte[0], letzte[1]
    minuten = max(1.0, (time.time() - ts_letzte) / 60.0)

    im = hole_bild(cfg)
    kh, kn, haupt_bild, nach_bild = lese_zaehler(im)

    kombis = []
    for h, hs in kh[:4]:
        for n, ns in kn[:4]:
            kombis.append(((h, n), min(hs, ns)))
    kombis.sort(key=lambda k: -k[1])

    wert, stimmen = plausibler_wert(
        kombis, letzter_wert, float(e["max_anstieg_m3_pro_minute"]), minuten
    )

    ergebnis = {
        "status": "ok" if wert is not None else "unsicher",
        "wert": wert,
        "letzter_wert": letzter_wert,
        "kandidaten_haupt": kh,
        "kandidaten_nach": kn,
        "stimmen": stimmen,
        "minuten_seit_letzter": round(minuten, 1),
    }
    if wert is not None and speichern:
        speichere_messung(time.time(), wert, "ocr", min(stimmen / 12.0, 1.0),
                          f"{kh[0][0] if kh else ''},{kn[0][0] if kn else ''}")
        ergebnis["delta"] = round(wert - letzter_wert, 3)

    # Fotos fuer die Dokumentation (max. 1 pro Stunde)
    try:
        if time.time() - _letztes_foto() > 3600:
            stempel = datetime.now().strftime("%Y%m%d-%H%M%S")
            im.save(FOTO_DIR / f"zaehler-{stempel}.jpg", quality=85)
            haupt_bild.save(FOTO_DIR / f"ocr-haupt-{stempel}.png")
    except Exception:
        pass
    return ergebnis


def _letztes_foto():
    fotos = sorted(FOTO_DIR.glob("zaehler-*.jpg"))
    return fotos[-1].stat().st_mtime if fotos else 0
