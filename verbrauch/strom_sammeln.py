#!/usr/bin/env python3
"""Stromkennzahlen des Wechselrichters erfassen (fuer Cron).

    python3 strom_sammeln.py            # Netzbezug und Hausverbrauch ablegen
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
        for art in strom.ARTEN:
            thema, kwh = strom.stand_aus_broker(cfg, art=art)
            print(f"{art:5s} {thema} = {kwh:.3f} kWh")
        return 0

    if a.anker:
        mitternacht = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        for art in strom.ARTEN:
            _t, gesamt = strom.stand_aus_broker(cfg, art=art)
            _t2, heute = strom.stand_aus_broker(cfg, thema_=strom.thema_heute(art), art=art)
            strom.anker_setzen(gesamt - heute, ts=int(mitternacht.timestamp()), art=art)
            print(f"Anker {art:5s}: {gesamt - heute:8.2f} kWh um {mitternacht:%d.%m.%Y %H:%M} "
                  f"(gesamt {gesamt:.2f} - heute {heute:.2f})")

    for art, (ts, kwh, neu) in strom.sammeln_alle(cfg).items():
        print(f"gespeichert {art:5s}: {kwh:.3f} kWh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
