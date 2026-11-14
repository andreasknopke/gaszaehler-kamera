#!/usr/bin/env python3
"""Startwert des Zaehlers ermitteln und die Referenzbilder der Walzen anlernen.

    python3 startwert_ermitteln.py            # OCR-Ergebnis anzeigen
    python3 startwert_ermitteln.py --setzen   # als Basiswert uebernehmen
    python3 startwert_ermitteln.py --wert 8497,162

Der Wert kann jederzeit auch in der Web-UI korrigiert werden
(Schaltflaeche 'Zaehlerstand korrigieren').
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gas
import tracker
import ziffern


def haupt_kandidaten():
    cfg = gas.load_config()
    im = gas.hole_bild(cfg)
    kh, kn, b0, b1 = gas.lese_zaehler(im)
    return kh, kn


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--setzen", action="store_true", help="OCR-Wert als Basiswert speichern")
    p.add_argument("--wert", help="Basiswert direkt setzen, z.B. 8497,162")
    args = p.parse_args()

    konfig = gas.load_config()

    if args.wert:
        wert = float(args.wert.replace(",", "."))
    else:
        kh, kn = haupt_kandidaten()
        print("Kandidaten Hauptziffern:", kh[:6])
        print("Kandidaten Nachkomma  :", kn[:6])
        if not kh:
            print("OCR konnte keine Hauptziffern lesen. Wert manuell angeben:")
            print("  python3 startwert_ermitteln.py --wert 8497,162")
            return 1
        haupt = kh[0][0]
        nach = kn[0][0] if kn else "000"
        wert = int(haupt) + int((nach + "000")[:3]) / 1000.0
        print(f"vorgeschlagener Wert: {wert:.3f} m3")
        if not args.setzen:
            print("Mit --setzen oder --wert uebernehmen.")
            return 0

    con = gas.db()
    rollend = []
    try:
        tracker.setze_basiswert(con, wert)
        gas.speichere_messung(time.time(), wert, "start", 1.0, "Startwert")

        # Referenzbilder der Walzen und Ziffernbank neu anlernen
        tracker.zuruecksetzen()
        cfg = gas.load_config()
        cfg["erfassung"]["startwert_m3"] = wert
        # Ein vom Nutzer genannter/eingetragener Wert gilt als bestaetigt.
        cfg["erfassung"]["basis_bestaetigt"] = True
        gas.save_config(cfg)

        im = gas.hole_bild(cfg)
        if gas.pruefe_orientierung(im) == 180:
            # Bild liegt verkehrt: Kamera-/Viewer-Drehung in der Konfiguration
            # korrigieren, damit kuenftige Messungen stimmen.
            cfg["kamera"]["drehen"] = (int(cfg["kamera"].get("drehen", 0)) + 180) % 360
            cfg["kamera"]["drehen_ansicht"] = (int(cfg["kamera"].get("drehen_ansicht", 0)) + 180) % 360
            gas.save_config(cfg)
            im = im.rotate(180, expand=True)
            print("Hinweis: Bildlage war verdreht, Kameradrehung in config.json korrigiert.")
        zellen = tracker.zellen_aus(im)
        time.sleep(1.5)
        zellen2 = tracker.zellen_aus(gas.hole_bild(cfg))
        rollend = [i for i in range(8)
                   if tracker._aehnlichkeit(zellen[i], zellen2[i]) < tracker.ROLLT]

        bank = ziffern.Ziffernbank()
        gelernt = bank.setze_basis(zellen, wert, zellen2)

        tracker.schritt(zellen, con, bank=bank, zellen2=zellen2,
                        rollend=rollend)                 # Referenzbilder setzen
    finally:
        con.close()
    print(f"Basiswert gesetzt: {wert:.3f} m3")
    print(f"Ziffernbank kalibriert mit: {ziffern.text(gelernt)}")
    if rollend:
        print(f"Walzen {rollend} drehen gerade - sie lernen beim naechsten "
              f"Wechsel selbst (Anzeige dort '-').")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
