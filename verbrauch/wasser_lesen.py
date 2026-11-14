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

# Ziffernfenster im aufrechten Bild (720x1280): (x, y, halbe Breite, halbe Hoehe)
ZELLEN = [
    (175, 761, 12, 16), (201, 761, 12, 16), (228, 760, 12, 16),
    (255, 759, 12, 16), (281, 757, 12, 16),
]

# Zeichen, die der Erkenner fuer Ziffern haeufig zurueckgibt
ZUORDNUNG = {
    "D": "0", "O": "0", "o": "0", "Q": "0", "U": "0",
    "I": "1", "l": "1", "|": "1", "i": "1",
    "Z": "2", "z": "2",
    "E": "3",
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


def zelle_lesen(ocr, im, x, y, hw, hh):
    b = im.crop((x - hw, y - hh, x + hw, y + hh)).convert("L")
    b = ziffer_isolieren(b) or b
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


def lesen(im, ocr=None):
    ocr = ocr or lade_ocr()
    ziffern, scores = [], []
    for x, y, hw, hh in ZELLEN:
        z, s = zelle_lesen(ocr, im, x, y, hw, hh)
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
