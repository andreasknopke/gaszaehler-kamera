#!/usr/bin/env python3
"""Lernlauf fuer die Ziffern der Zaehlerwalzen.

Im Minutentakt laesst sich aus einer drehenden Walze keine Ziffernfolge
ableiten (bei 1 m3/h springt die 0,001-Walze alle 3 s weiter). Dieser Lauf
nimmt deshalb eine Serie von Bildern im Sekundenabstand auf:

* Zaehlerwalzen laufen nur vorwaerts. Jede neu gesehene, *stabile* Ziffer ist
  also genau ein Schritt weiter als die vorherige.
* Stabile Ziffer = sie ist im naechsten Bild noch (fast) gleich; ein mitten
  im Wechsel stehendes Bild wird dadurch aussortiert.
* Aus der Reihenfolge ergeben sich die Labels: aktuelle Ziffer + 1, + 2, ...

Damit fuellen sich die Banken der Nachkomma-Walzen in wenigen Minuten, ohne
dass jemand Ziffern von Hand beschriften muss.

    python3 lernen.py               # 50 Bilder, ~1 Minute
    python3 lernen.py --bilder 80
"""

import argparse
import fcntl
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gas
import tracker
import ziffern

STABIL = 0.99
GRUPPE = 0.93      # so aehnlich muessen zwei Bilder derselben Ziffer sein
NEUE = 0.85        # so unaehnlich muss eine *andere* Ziffer sein
                   # (Belichtungsschwankungen liegen bei ~0,96-0,97)


def aehn(a, b):
    return float((a * b).mean())


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bilder", type=int, default=50, help="Anzahl Serienbilder")
    p.add_argument("--abstand", type=float, default=1.0, help="Sekunden zwischen den Bildern")
    args = p.parse_args()

    bank = ziffern.Ziffernbank()
    labels = bank.labels()
    print(f"Start-Labels: {ziffern.text([l for l in labels])}")

    # nicht gleichzeitig mit dem Cron-Lauf in die Bank schreiben
    sperre = (Path(__file__).resolve().parent / ".sample.lock").open("w")
    fcntl.flock(sperre, fcntl.LOCK_EX)

    serien = [[] for _ in range(8)]
    for k in range(args.bilder):
        zellen = tracker.zellen_aus(gas.hole_bild())
        for i in range(8):
            serien[i].append(zellen[i])
        if k % 10 == 0:
            print(f"  Bild {k + 1}/{args.bilder}", flush=True)
        time.sleep(args.abstand)

    gelernt = {}
    for i, serie in enumerate(serien):
        # stabile Bilder: im naechsten Bild noch fast gleich
        stabil = [serie[k] for k in range(len(serie) - 1)
                  if aehn(serie[k], serie[k + 1]) >= STABIL]
        # aufeinanderfolgende gleiche zusammenfassen
        gruppen = []
        for bild in stabil:
            if not gruppen:
                gruppen.append([bild])
                continue
            s = aehn(bild, gruppen[-1][-1])
            if s >= GRUPPE:
                gruppen[-1].append(bild)
            elif s < NEUE:
                gruppen.append([bild])
            # dazwischen: Uebergang zwischen zwei Ziffern, nicht werten

        schritte = len(gruppen) - 1
        if schritte <= 0:
            continue
        if schritte > 9:
            print(f"Walze {i}: {len(gruppen)} Ziffern in der Messzeit - zu schnell, "
                  f"wird uebersprungen")
            continue
        if labels[i] is None:
            print(f"Walze {i}: {schritte} neue Ziffern, aber kein Start-Label - "
                  f"uebersprungen (Basiswert neu setzen)")
            continue

        for n, gruppe in enumerate(gruppen[1:], start=1):
            ziffer = (int(labels[i]) + n) % 10
            if bank.lerne(i, ziffer, gruppe[len(gruppe) // 2]):
                gelernt.setdefault(i, []).append(ziffer)
        labels[i] = (int(labels[i]) + schritte) % 10
        bank.setze_labels(labels)
        print(f"Walze {i}: +{schritte} Ziffern gelernt "
              f"({''.join(str(z) for z in gelernt.get(i, []))})")

    # Wert aus der Ablesung neu setzen, damit Zaehlung und Ablesung gleichauf sind
    zellen = tracker.zellen_aus(gas.hole_bild())
    gez, wert, details, komplett, aufloesung = bank.lies(zellen)
    print(f"\nAblesung jetzt: {ziffern.text(gez)}  (Aufloesung {aufloesung:g} m3)")
    if wert is not None and aufloesung <= 0.1:
        con = gas.db()
        try:
            tracker.setze_basiswert(con, wert)
            gas.speichere_messung(time.time(), wert, "manuell",
                                  0.95, "Lernlauf", ziffern.text(gez))
        finally:
            con.close()
        print(f"Messwert auf {wert:.3f} m3 gesetzt.")
    else:
        print("Ablesung noch nicht ausreichend - Messwert unveraendert.")
    print("Lernstand:", {i: sorted(v) for i, v in bank.lernstand().items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
