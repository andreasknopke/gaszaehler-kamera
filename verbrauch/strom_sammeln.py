#!/usr/bin/env python3
"""Netzbezug des Wechselrichters erfassen (fuer Cron).

    python3 strom_sammeln.py            # Stand ablegen
    python3 strom_sammeln.py --zeigen   # nur anzeigen
    python3 strom_sammeln.py --anker    # Tagesbeginn aus den Zaehlerstaenden setzen
"""

import argparse
import sys
from datetime import datetime

import gas
import strom


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--zeigen", action="store_true", help="nur lesen, nichts speichern")
    p.add_argument("--anker", action="store_true",
                   help="Nullpunkt auf den Tagesbeginn setzen (gesamt minus heute)")
    a = p.parse_args()
    cfg = gas.load_config()

    if a.zeigen:
        thema, kwh = strom.stand_aus_broker(cfg)
        print(f"{thema} = {kwh:.3f} kWh")
        return 0

    if a.anker:
        _t, gesamt = strom.stand_aus_broker(cfg, thema="sofar_hyd/bezug_gesamt")
        _t2, heute = strom.stand_aus_broker(cfg, thema="sofar_hyd/bezug_heute")
        mitternacht = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        strom.anker_setzen(gesamt - heute, ts=int(mitternacht.timestamp()))
        print(f"Anker: {gesamt - heute:.2f} kWh um {mitternacht:%d.%m.%Y %H:%M} "
              f"(gesamt {gesamt:.2f} - heute {heute:.2f})")

    ts, kwh, neu = strom.sammeln(cfg)
    print(("gespeichert" if neu else "unveraendert") + f": {kwh:.3f} kWh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
