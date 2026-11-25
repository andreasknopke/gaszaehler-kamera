#!/usr/bin/env python3
"""Wasserzaehler ablesen: die fuenf Vorkommastellen per RapidOCR.

Die Ziffern sind klein (rund 15x21 px), dunkel auf hellem Grund und von
Reflexen auf dem Glas unterbrochen. Tesseract scheitert daran; RapidOCR
erkennt sie zuverlaessig, wenn man ihm jede Ziffer einzeln und mehrfach
nebeneinander gibt. Einige Zeichen kommen als Buchstabe zurueck (die 0 sieht
aus wie ein D) - dafuer gibt es eine Zuordnungstabelle.

Die Zellen sind fest: die Kamera haengt fest, die Positionen sind stabil.
Die Erkennung wird anschliessend in wasser.py gegen den letzten Stand
geprueft (nicht kleiner, hoechstens ein paar Kubikmeter mehr).

Ausgabe als JSON:

    ~/.local/ocrenv/bin/python wasser_lesen.py --bild /tmp/w.jpg
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
from PIL import Image, ImageEnhance, ImageOps
from scipy import ndimage

# Suchbereich der Ziffernzeile im aufrechten Bild (720x1280).
# Die Ziffern werden bei jedem Lauf neu gesucht: Die Kamera verschiebt sich
# gelegentlich um einige Pixel, feste Zellen wuerden dann daneben liegen.
SUCHBEREICH = (140, 690, 420, 800)
HW, HH = 12, 16          # halbe Fenstergroesse um eine gefundene Ziffer

# Zuletzt gueltige Ziffernpositionen (Fallback, wenn die Suche scheitert).
STANDARD_ZELLEN = [
    (220, 737), (247, 737), (271, 736), (299, 739), (325, 734),
]

# Zeichen, die der Erkenner fuer Ziffern haeufig zurueckgibt
ZUORDNUNG = {
    "D": "0", "O": "0", "o": "0", "Q": "0", "U": "0",
    "I": "1", "l": "1", "|": "1", "i": "1",
    "Z": "2", "z": "2",
    "E": "3", "L": "1", "C": "0",
    "A": "4",
    "S": "5", "s": "5",
    "G": "6", "b": "6",
    "T": "7", "?": "7",
    "B": "8",
    "g": "9", "q": "9",
}


def lade_ocr():
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        raise SystemExit("rapidocr fehlt - siehe README (venv ~/.local/ocrenv)")
    return RapidOCR()


def ziffer_isolieren(b):
    """Dunkle Ziffer im Ausschnitt finden und eng zuschneiden."""
    a = np.asarray(b, dtype=np.float32)
    maske = ndimage.binary_closing(a < (np.percentile(a, 50) - 15), np.ones((2, 2)))
    lbl, _ = ndimage.label(maske)
    mitte = (b.width / 2, b.height / 2)
    beste, beste_abstand = None, 1e9
    for k, sl in enumerate(ndimage.find_objects(lbl), 1):
        if sl is None:
            continue
        h = sl[0].stop - sl[0].start
        w = sl[1].stop - sl[1].start
        flaeche = int((lbl[sl] == k).sum())
        if not (12 <= h <= 32 and 4 <= w <= 22 and flaeche > 40):
            continue
        cx = (sl[1].start + sl[1].stop) / 2
        cy = (sl[0].start + sl[0].stop) / 2
        abstand = abs(cx - mitte[0]) + abs(cy - mitte[1])
        if abstand < beste_abstand:
            beste, beste_abstand = sl, abstand
    if beste is None:
        return None
    y0 = max(0, beste[0].start - 2)
    y1 = min(b.height, beste[0].stop + 2)
    x0 = max(0, beste[1].start - 2)
    x1 = min(b.width, beste[1].stop + 2)
    return b.crop((x0, y0, x1, y1))


def ziffern_finden(im):
    """Ziffernzeile suchen: fuenf Ziffern mit gleichmaessigem Abstand.

    Ueber ein Spaltenprofil statt ueber zusammenhaengende Flaechen: Die
    erste Ziffer klebt oft am dunklen Zaehlerrand, und Reflexe zerteilen
    eine Ziffer in zwei Stuecke. Das Profil findet trotzdem fuenf Luecken
    mit gleichmaessigem Abstand.
    """
    x0, y0, x1, y1 = SUCHBEREICH
    band_y0, band_y1 = y0 + 25, y1 - 30
    band = im.crop((x0, band_y0, x1, band_y1)).convert("L")
    a = np.asarray(band, dtype=np.float32)
    hp = a - ndimage.uniform_filter(a, size=9)
    profil = ndimage.uniform_filter1d((hp < -10).sum(axis=0).astype(float), 3)
    lbl, _ = ndimage.label(profil >= 4)

    kandidaten = []
    for sl in ndimage.find_objects(lbl):
        if sl is None:
            continue
        w = sl[0].stop - sl[0].start
        if w < 5:
            continue
        streifen = hp[:, sl[0].start:sl[0].stop]
        zeilen = np.where((streifen < -10).any(axis=1))[0]
        mitte_y = int(zeilen.mean()) + band_y0 if len(zeilen) else (band_y0 + band_y1) // 2
        kandidaten.append((sl[0].start + w // 2 + x0, mitte_y))
    kandidaten.sort()
    if not kandidaten:
        return []

    # Die Kamera haengt fest; meist genuegt es, die bekannte Zellenlage um
    # die gemessene Verschiebung nachzuziehen. Das ist robuster als eine
    # Kette zu suchen: die erste Ziffer klebt am Rand, rechts steht das "m".
    verschiebungen, hoehen = [], []
    for sx, sy in STANDARD_ZELLEN:
        naechste = min(kandidaten, key=lambda k: abs(k[0] - sx))
        if abs(naechste[0] - sx) <= 15:
            verschiebungen.append(naechste[0] - sx)
            hoehen.append(naechste[1] - sy)
    if len(verschiebungen) >= 3:
        dx = int(np.median(verschiebungen))
        dy = int(np.median(hoehen))
        return [(sx + dx, sy + dy) for sx, sy in STANDARD_ZELLEN]

    # Sonst: Kette von fuenf Ziffern mit gleichmaessigem Abstand suchen.
    beste, beste_guete = [], -1e9
    for i in range(len(kandidaten) - 4):
        gruppe = kandidaten[i:i + 5]
        abst = [gruppe[k + 1][0] - gruppe[k][0] for k in range(4)]
        if not all(20 <= d <= 34 for d in abst):
            continue
        if max(abs(y - gruppe[0][1]) for _, y in gruppe) > 12:
            continue
        guete = min(abst) - max(abst)
        if guete > beste_guete:
            beste, beste_guete = gruppe, guete
    return beste


def _zelle_deuten(ocr, b):
    a = np.asarray(b, dtype=np.float32)
    if np.median(a) < 128:                     # dunkler Grund: invertieren
        b = ImageOps.invert(b)
    ziel = 128
    b = ImageEnhance.Contrast(b).enhance(1.6)
    faktor = ziel / b.height
    b = b.resize((max(4, int(b.width * faktor)), ziel), Image.LANCZOS)
    b = ImageOps.expand(b, 6, 255)
    leinwand = Image.new("L", (5 * b.width + 6 * 8, ziel + 16), 255)
    for k in range(5):
        leinwand.paste(b, (8 + k * (b.width + 8), 8))
    ergebnis, _ = ocr(np.asarray(leinwand.convert("RGB")),
                      use_det=False, use_cls=False, use_rec=True)
    if not ergebnis:
        return None, 0.0
    text, score = ergebnis[0][0], float(ergebnis[0][1])
    if not text:
        return None, 0.0
    zeichen = Counter(text).most_common(1)[0][0]
    return ZUORDNUNG.get(zeichen, zeichen), round(score, 3)


def zelle_lesen(ocr, im, x, y, hw, hh):
    b = im.crop((x - hw, y - hh, x + hw, y + hh)).convert("L")
    b = ziffer_isolieren(b) or b
    # Zwei Vorverarbeitungen: die "1" erkennt der Erkenner nur im Hochpass
    # (helle Reflexe daneben), die "0" unter Reflexen dagegen nur ohne.
    a = np.asarray(b, dtype=np.float32)
    hp = a - ndimage.uniform_filter(a, size=7)
    hp = (hp - hp.min()) / max(1e-6, hp.max() - hp.min()) * 255
    varianten = [b, Image.fromarray(hp.astype(np.uint8))]
    beste = (None, 0.0)
    for variante in varianten:
        z, s = _zelle_deuten(ocr, variante)
        if s > beste[1]:
            beste = (z, s)
    return beste


def lesen(im, ocr=None):
    ocr = ocr or lade_ocr()
    gefunden = ziffern_finden(im)
    if len(gefunden) != 5:
        gefunden = STANDARD_ZELLEN
    ziffern, scores = [], []
    for x, y in gefunden:
        z, s = zelle_lesen(ocr, im, x, y, HW, HH)
        if z is None or not z.isdigit():
            z = None
        ziffern.append(int(z) if z is not None else None)
        scores.append(s)
    return ziffern, scores


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bild", required=True, help="Pfad zum Kamerabild")
    p.add_argument("--json", action="store_true")
    a = p.parse_args()
    im = Image.open(a.bild)
    ziffern, scores = lesen(im)
    if a.json:
        print(json.dumps({"ziffern": ziffern, "scores": scores}, ensure_ascii=False))
    else:
        text = "".join("?" if z is None else str(z) for z in ziffern)
        print(f"Ziffern: {text}  (Konfidenz {[round(s, 2) for s in scores]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
