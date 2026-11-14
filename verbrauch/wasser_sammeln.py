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
        con = wasser.db()
        basis = wasser.basiswert(con)
        con.close()
        print(f"Basiswert: {wasser.ablesung_text(basis) if basis else '—'}")
        wert, ziffern, scores, hinweis = wasser.stand_lesen(bisher=basis)
        print(f"  gelesen: {ziffern}  Konfidenz {[round(s,2) for s in scores]}"
              f"  → {wasser.ablesung_text(wert) if wert is not None else '—'}"
              + (f"  ({hinweis})" if hinweis else ""))
        return 0

    wert, delta, hinweis, neu = wasser.sammeln()

    # Home Assistant (MQTT-Discovery) - darf die Messung nie stoeren
    try:
        cfg = wasser.gas.load_config()
        if (cfg.get("mqtt") or {}).get("aktiv"):
            import mqtt_bruecke
            mqtt_bruecke.sende(cfg)
    except Exception as exc:
        print("MQTT:", repr(exc))

    print(f"{wasser.ablesung_text(wert)} m3"
          f"{' (+' + str(round(delta, 3)) + ')' if delta else ''}"
          f"{'' if neu else ' (unverändert)'}"
          + (f"  [{hinweis}]" if hinweis else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
