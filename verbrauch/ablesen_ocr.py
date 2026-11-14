#!/usr/bin/env python3
"""Ziffernablesung per RapidOCR (ONNX) — jede Walze einzeln.

Warum nicht Tesseract? Auf diesen kleinen, weichen Ziffern liefert es selbst
mit vielen Vorstufen meist nichts (getestet: 1 von 5 Ziffern). RapidOCR
erkennt die Ziffern dagegen zuverlaessig — aber nur, wenn man ihm jede Walze
einzeln gibt und die Ziffer mehrfach nebeneinander legt: ein einzelnes Zeichen
wird sonst als Textzeile verarbeitet und verworfen.

Ablauf je Walze:
  1. Zellausschnitt holen (feste Geometrie aus tracker.ZELLEN)
  2. lokale Beleuchtung abziehen (Bild minus Weichzeichnung)
  3. groesstes zusammenhaengendes Objekt = die Ziffer
  4. auf 24x48 normieren, fuenfmal nebeneinander setzen
  5. RapidOCR ohne Textsuche (use_det=False) → Zeichen + Konfidenz

Ausgabe als JSON, damit sample.py es ohne Import lesen kann.

Laeuft im eigenen venv (onnxruntime + rapidocr):

    ~/.local/ocrenv/bin/python ablesen_ocr.py --json
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
from PIL import Image, ImageFilter

MIN_SCORE = 0.50          # darunter gilt eine Ziffer als nicht gelesen
                          # (die Einer-Walze steht oft mitten im Wechsel
                          #  und liefert dann nur 0,5-0,8 Konfidenz)
MIN_HOEHE = 12            # Pixel; kleinere Objekte sind Rauschen


def lade_ocr():
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        raise SystemExit(
            "rapidocr fehlt. Einmalig installieren:\n"
            "  python3 -m venv ~/.local/ocrenv\n"
            "  ~/.local/ocrenv/bin/pip install rapidocr-onnxruntime numpy pillow scipy"
        )
    return RapidOCR()


def zelle_ausschnitt(im, i):
    import tracker
    cx, cy, hw, hh = tracker.ZELLEN[i]
    return im.crop((cx - hw - 8, cy - hh - 8, cx + hw + 8, cy + hh + 8))


def ziffer_im_ausschnitt(crop):
    """Ziffer finden: zusammenhaengendes helles Objekt in Zifferngroesse.

    Wichtig: nicht einfach das groesste Objekt nehmen - der Zellrahmen ist
    deutlich groesser als die Ziffer und verschluckt sie sonst (gemessen:
    Rahmen 82x138 px, Ziffer 23x48 px).
    """
    from scipy import ndimage
    c = crop.convert("L").filter(ImageFilter.MedianFilter(3))
    a = np.asarray(c, dtype=np.float32)
    blur = np.asarray(c.filter(ImageFilter.GaussianBlur(15)), dtype=np.float32)
    norm = a - blur
    maske = ndimage.binary_closing(norm > 12, np.ones((3, 3)))
    lbl, _ = ndimage.label(maske)
    objekte = ndimage.find_objects(lbl)
    beste = None
    for k, sl in enumerate(objekte, 1):
        if sl is None:
            continue
        h = sl[0].stop - sl[0].start
        w = sl[1].stop - sl[1].start
        flaeche = int((lbl[sl] == k).sum())
        # zifferntypische Maße: ca. 25-70 px hoch, 8-45 px breit
        if h < MIN_HOEHE or h > 70 or w < 8 or w > 45 or flaeche < 60:
            continue
        if beste is None or flaeche > beste[0]:
            beste = (flaeche, sl)
    if beste is None:
        return None
    sl = beste[1]
    y0, y1 = sl[0].start, sl[0].stop
    x0, x1 = sl[1].start, sl[1].stop
    stueck = norm[y0:y1, x0:x1]
    stueck = stueck - stueck.min()
    stueck = stueck / max(stueck.max(), 1) * 255
    return Image.fromarray((255 - stueck).astype(np.uint8))


def ziffer_lesen(ocr, bild):
    """Ziffer mehrfach nebeneinander -> Zeichen + Konfidenz."""
    if bild is None:
        return None, 0.0
    b = bild.resize((24, 48), Image.LANCZOS)
    anzahl, abstand = 5, 10
    leinwand = Image.new("L", (anzahl * 24 + (anzahl + 1) * abstand, 68), 255)
    for k in range(anzahl):
        leinwand.paste(b, (abstand + k * (24 + abstand), 10))
    ergebnis, _ = ocr(np.asarray(leinwand.convert("RGB")),
                      use_det=False, use_cls=False, use_rec=True)
    if not ergebnis:
        return None, 0.0
    text, score = ergebnis[0][0], float(ergebnis[0][1])
    if not text:
        return None, 0.0
    return Counter(text).most_common(1)[0][0], round(score, 3)


def ablesen(im, ocr=None, min_score=MIN_SCORE):
    """Alle acht Walzen lesen -> (ziffern, scores)."""
    ocr = ocr or lade_ocr()
    ziffern, scores = [], []
    for i in range(8):
        z, s = ziffer_lesen(ocr, ziffer_im_ausschnitt(zelle_ausschnitt(im, i)))
        if z is not None and (s < min_score or not z.isdigit()):
            z = None
        ziffern.append(int(z) if z is not None else None)
        scores.append(s)
    return ziffern, scores


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true", help="JSON statt Text")
    p.add_argument("--bild", help="statt Kamera ein vorhandenes Bild lesen")
    args = p.parse_args()

    if args.bild:
        im = Image.open(args.bild).convert("L")
    else:
        import gas
        im = gas.hole_bild()

    ocr = lade_ocr()
    ziffern, scores = ablesen(im, ocr)

    haupt = "".join("." if z is None else str(z) for z in ziffern[:5])
    nach = "".join("." if z is None else str(z) for z in ziffern[5:])
    ablesung = f"{haupt},{nach}"

    if args.json:
        print(json.dumps({"ziffern": ziffern, "scores": scores,
                          "ablesung": ablesung}, ensure_ascii=False))
    else:
        for i, (z, s) in enumerate(zip(ziffern, scores)):
            print(f"  Zelle {i}: {z}  (Konfidenz {s:.2f})")
        print(f"\nAblesung: {ablesung}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
