#!/usr/bin/env python3
"""Aktueller Strommix in Deutschland (Fraunhofer ISE, Energy-Charts).

Woher die Daten kommen
----------------------
Electricity Maps liefert die Daten nur ueber eine kostenpflichtige API bzw.
blockt direkte Abrufe (HTTP 403). Die Energy-Charts-API des Fraunhofer ISE
ist dagegen frei, ohne Schluessel und in 15-Minuten-Aufloesung verfuegbar:

    https://api.energy-charts.info/public_power?country=de
    https://api.energy-charts.info/co2eq?country=de

`public_power` liefert die Erzeugung je Energietraeger in MW, `co2eq` die
CO2-Intensitaet in g/kWh. Beides wird auf die letzte vollstaendige
Viertelstunde bezogen und zu wenigen Kategorien zusammengefasst.

Die Antwort wird 15 Minuten zwischengespeichert - haeufiger aendert sich
nichts, und die Seite fragt oefter ab.
"""

import json
import time
import urllib.request
from datetime import datetime

BASIS = "https://api.energy-charts.info"
LAND = "de"
ZWISCHENSPEICHER_SEKUNDEN = 900

# Kategorie, Quellen der API, Farbe fuer die Anzeige
KATEGORIEN = [
    ("Wind",       ["Wind offshore", "Wind onshore"], "#7ee2d0"),
    ("Solar",      ["Solar"], "#ffd166"),
    ("Biomasse",   ["Biomass", "Waste"], "#8fbc6a"),
    ("Wasserkraft", ["Hydro Run-of-River", "Hydro water reservoir",
                     "Hydro pumped storage"], "#4aa3df"),
    ("Kohle",      ["Fossil brown coal / lignite", "Fossil hard coal"], "#8b6b4a"),
    ("Gas",        ["Fossil gas", "Fossil coal-derived gas"], "#b06a8f"),
    ("Sonstige",   ["Fossil oil", "Geothermal", "Others"], "#7a8699"),
]

_SCHLUESSEL = {name: (kat, farbe) for kat, namen, farbe in KATEGORIEN for name in namen}
_cache = {"zeit": 0.0, "daten": None}


def _hole_json(pfad, timeout=15):
    url = f"{BASIS}/{pfad}"
    req = urllib.request.Request(url, headers={"User-Agent": "gaszaehler/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as antwort:
        return json.load(antwort)


def _letzter_gueltiger(ts_liste, werte):
    for t, w in reversed(list(zip(ts_liste, werte))):
        if w is not None:
            return t, w
    return None, None


def _co2_intensitaet(zeitstempel=None):
    daten = _hole_json(f"co2eq?country={LAND}")
    ts = daten.get("unix_seconds") or []
    if zeitstempel is not None:
        # Wert moeglichst am selben Zeitpunkt wie der Mix
        for t, w in zip(ts, daten.get("co2eq") or []):
            if w is not None and t == zeitstempel:
                return w
    t, w = _letzter_gueltiger(ts, daten.get("co2eq") or [])
    return w


def hole(timeout=15):
    """Aktueller Mix und CO2-Intensitaet. -> dict"""
    daten = _hole_json(f"public_power?country={LAND}", timeout=timeout)
    ts = daten.get("unix_seconds") or []
    arten = daten.get("production_types") or []
    if not ts:
        raise RuntimeError("keine Zeitreihe von Energy-Charts")

    # letzter Zeitpunkt, zu dem alle Erzeugungsarten einen Wert haben
    index = None
    for i in range(len(ts) - 1, -1, -1):
        if all(len(a.get("data") or []) > i and a["data"][i] is not None
               for a in arten):
            index = i
            break
    if index is None:
        index = len(ts) - 1

    werte = {a["name"]: float(a["data"][index] or 0) for a in arten}
    roh = {}
    for name, wert in werte.items():
        if name in _SCHLUESSEL:
            roh[name] = max(0.0, wert)          # negative Werte = Verbrauch/Export

    summen = {}
    for name, wert in roh.items():
        kategorie = _SCHLUESSEL[name][0]
        summen[kategorie] = summen.get(kategorie, 0.0) + wert

    gesamt = sum(summen.values()) or 1.0
    anteile = []
    for kategorie, _namen, farbe in KATEGORIEN:
        mw = summen.get(kategorie, 0.0)
        anteile.append({"name": kategorie, "mw": round(mw, 1),
                        "anteil": round(mw / gesamt * 100, 1), "farbe": farbe})

    zeit = ts[index]
    try:
        co2 = _co2_intensitaet(zeit)
    except Exception:
        co2 = None

    return {
        "zeit": int(zeit),
        "zeit_text": datetime.fromtimestamp(zeit).strftime("%d.%m.%Y %H:%M"),
        "alter_sekunden": max(0, int(time.time() - zeit)),
        "gesamt_mw": round(gesamt, 1),
        "anteile": anteile,
        "co2_g_kwh": round(co2, 1) if co2 is not None else None,
        "last_mw": round(werte.get("Load") or 0, 1),
        "erneuerbar_prozent": next(
            (round(float(a["data"][index]), 1)
             for a in arten if a["name"] == "Renewable share of generation"
             and a["data"][index] is not None), None),
        "quelle": "Fraunhofer ISE, Energy-Charts",
        "quelle_url": "https://www.energy-charts.info/",
    }


def aktuell(timeout=15, erzwingen=False):
    """Zwischengespeicherte Fassung von hole()."""
    jetzt = time.time()
    if (not erzwingen and _cache["daten"] is not None
            and jetzt - _cache["zeit"] < ZWISCHENSPEICHER_SEKUNDEN):
        return _cache["daten"]
    daten = hole(timeout=timeout)
    _cache["daten"] = daten
    _cache["zeit"] = jetzt
    return daten


def main():
    daten = aktuell(erzwingen=True)
    print(f"Strommix Deutschland, {daten['zeit_text']} "
          f"({daten['alter_sekunden'] // 60} min alt)")
    print(f"  CO2-Intensitaet: {daten['co2_g_kwh']} g/kWh")
    print(f"  Erzeugung: {daten['gesamt_mw']:.0f} MW, "
          f"Last {daten['last_mw']:.0f} MW, "
          f"erneuerbar {daten['erneuerbar_prozent']} %")
    for a in sorted(daten["anteile"], key=lambda x: -x["anteil"]):
        if a["anteil"] >= 0.1:
            print(f"    {a['name']:12} {a['anteil']:5.1f} %  ({a['mw']:.0f} MW)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
