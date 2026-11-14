#!/usr/bin/env python3
"""Wasserzaehler erfassen (fuer Cron).

    python3 wasser_sammeln.py              # Stand pruefen und ablegen
    python3 wasser_sammeln.py --zeigen     # nur anzeigen
    python3 wasser_sammeln.py --anker 7    # Startwert vom Display setzen
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wasser


def main():
    p = argparse.ArgumentParser(description="Wasserzaehler ablesen")
    p.add_argument("--zeigen", action="store_true", help="nur anzeigen")
    p.add_argument("--anker", help="Startwert setzen, z. B. 7")
    a = p.parse_args()

    if a.anker:
        wert = float(str(a.anker).replace(",", "."))
        wasser.anker_setzen(wert)
        print(f"Anker gesetzt: {wasser.ablesung_text(wert)} m3")
        return 0

    if a.zeigen:
        zellen = wasser.zellen_aus(wasser.bild())
        wert = wasser.basiswert(wasser.db())
        print(f"Basiswert: {wasser.ablesung_text(wert) if wert else '—'}")
        for i, z in enumerate(zellen):
            ref = wasser.lade(i)
            sim = wasser._aehnlichkeit(z, ref) if ref is not None else None
            print(f"  Zelle {i}: " + (f"Ähnlichkeit {sim:.3f}" if sim is not None else "keine Referenz"))
        return 0

    wert, delta, details, neu = wasser.sammeln()

    # Home Assistant (MQTT-Discovery) - darf die Messung nie stoeren
    try:
        cfg = wasser.gas.load_config()
        if (cfg.get("mqtt") or {}).get("aktiv"):
            import mqtt_bruecke
            mqtt_bruecke.sende(cfg)
    except Exception as exc:
        print("MQTT:", repr(exc))

    print(f"{wasser.ablesung_text(wert)} m3"
          f"{' (+' + str(delta) + ')' if delta else ''}"
          f"{'' if neu else ' (unverändert)'}")
    for d in details:
        print("  ", d)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
