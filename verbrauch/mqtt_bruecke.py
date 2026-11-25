#!/usr/bin/env python3
"""Messwerte per MQTT an Home Assistant (Discovery).

Home Assistant legt damit automatisch ein Geraet "Gaszähler" an, dem alle
Entitaeten zugeordnet sind (Zählerstand, Verbrauch und Kosten je Zeitraum,
Leistung, letzte Messung). Voraussetzung ist ein MQTT-Broker, z. B. das
Mosquitto-Add-on, und in config.json:

    "mqtt": {
      "aktiv": true,
      "host": "192.168.188.10",
      "port": 1883,
      "benutzer": "mqtt",
      "passwort": "...",
      "basis": "gaszaehler",
      "entdeckung": "homeassistant"
    }

Aufruf:
    python3 mqtt_bruecke.py            # einmal senden
    python3 mqtt_bruecke.py --zeigen   # nur anzeigen, ohne Broker
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gas
import mqtt
import server

GERAET = {
    "identifiers": ["gaszaehler_kamera"],
    "name": "Gaszähler",
    "manufacturer": "Eigenbau",
    "model": "BK-G4, Ablesung per IP-Kamera",
    "sw_version": "1.1",
}

# objekt, Anzeigename, Feld aus der Zusammenfassung, Einheit, Optionen
ENTITAETEN = [
    ("stand", "Zählerstand", "zaehlerstand_m3", "m³",
     {"device_class": "gas", "state_class": "total_increasing",
      "icon": "mdi:counter", "suggested_display_precision": 1, "genauigkeit": 1}),
    ("verbrauch_heute", "Verbrauch heute", "verbrauch_heute_m3", "m³",
     {"device_class": "gas", "state_class": "measurement",
      "icon": "mdi:fire", "suggested_display_precision": 2}),
    ("verbrauch_woche", "Verbrauch 7 Tage", "verbrauch_woche_m3", "m³",
     {"device_class": "gas", "state_class": "measurement",
      "icon": "mdi:calendar-week", "suggested_display_precision": 1}),
    ("verbrauch_monat", "Verbrauch 30 Tage", "verbrauch_monat_m3", "m³",
     {"device_class": "gas", "state_class": "measurement",
      "icon": "mdi:calendar-month", "suggested_display_precision": 1}),
    ("verbrauch_jahr", "Verbrauch 365 Tage", "verbrauch_jahr_m3", "m³",
     {"device_class": "gas", "state_class": "measurement",
      "icon": "mdi:calendar", "suggested_display_precision": 0}),
    ("leistung", "Leistung (24 h)", "verbrauch_stunde_m3", "m³/h",
     {"state_class": "measurement", "icon": "mdi:speedometer",
      "suggested_display_precision": 3}),
    ("leistung_aktuell", "Leistung (aktuell)", "leistung_aktuell_m3h", "m³/h",
     {"state_class": "measurement", "icon": "mdi:fire",
      "suggested_display_precision": 2, "genauigkeit": 3}),
    ("kosten_heute", "Kosten heute", "kosten_heute_eur", "€",
     {"device_class": "monetary", "state_class": "measurement",
      "icon": "mdi:currency-eur", "suggested_display_precision": 2}),
    ("kosten_monat", "Kosten 30 Tage", "kosten_monat_eur", "€",
     {"device_class": "monetary", "state_class": "measurement",
      "icon": "mdi:currency-eur", "suggested_display_precision": 2}),
    ("kosten_jahr", "Kosten 365 Tage", "kosten_jahr_eur", "€",
     {"device_class": "monetary", "state_class": "measurement",
      "icon": "mdi:currency-eur", "suggested_display_precision": 0}),
    ("ablesung", "Ablesung laut Bild", "ablesung", None,
     {"icon": "mdi:digits", "entity_category": "diagnostic"}),
    ("letzte_messung", "Letzte Messung", "letzte_messung", None,
     {"device_class": "timestamp", "icon": "mdi:clock-outline",
      "entity_category": "diagnostic"}),
    ("bild_alter", "Bildalter", "alter_sekunden", "s",
     {"device_class": "duration", "state_class": "measurement",
      "icon": "mdi:timer-outline", "entity_category": "diagnostic",
      "suggested_display_precision": 0}),
]

# Nach 3 Messlaeufen ohne Update gilt das Geraet als nicht erreichbar
WASSER_GERAET = {
    "identifiers": ["wasserzaehler_kamera"],
    "name": "Wasserzähler",
    "manufacturer": "Eigenbau",
    "model": "Wasserzähler, Ablesung per IP-Kamera",
    "sw_version": "1.1",
}

# objekt, Anzeigename, Feld aus den Kennzahlen, Einheit, Optionen
WASSER_ENTITAETEN = [
    ("stand", "Zählerstand", "stand_m3", "m³",
     {"device_class": "water", "state_class": "total_increasing",
      "icon": "mdi:water", "suggested_display_precision": 0, "genauigkeit": 0}),
    ("verbrauch_heute", "Verbrauch heute", "tag_m3", "m³",
     {"device_class": "water", "state_class": "measurement",
      "icon": "mdi:water-outline", "suggested_display_precision": 2}),
    ("leistung_aktuell", "Durchfluss (aktuell)", "leistung_aktuell_m3h", "m³/h",
     {"state_class": "measurement", "device_class": "volume_flow_rate",
      "icon": "mdi:water-pump", "suggested_display_precision": 3, "genauigkeit": 3}),
    ("verbrauch_woche", "Verbrauch 7 Tage", "woche_m3", "m³",
     {"device_class": "water", "state_class": "measurement",
      "icon": "mdi:calendar-week", "suggested_display_precision": 2}),
    ("verbrauch_monat", "Verbrauch 30 Tage", "monat_m3", "m³",
     {"device_class": "water", "state_class": "measurement",
      "icon": "mdi:calendar-month", "suggested_display_precision": 1}),
    ("verbrauch_jahr", "Verbrauch 365 Tage", "jahr_m3", "m³",
     {"device_class": "water", "state_class": "measurement",
      "icon": "mdi:calendar", "suggested_display_precision": 0}),
    ("kosten_heute", "Kosten heute", "tag_eur", "€",
     {"device_class": "monetary", "state_class": "measurement",
      "icon": "mdi:currency-eur", "suggested_display_precision": 2}),
    ("kosten_monat", "Kosten 30 Tage", "monat_eur", "€",
     {"device_class": "monetary", "state_class": "measurement",
      "icon": "mdi:currency-eur", "suggested_display_precision": 2}),
    ("kosten_jahr", "Kosten 365 Tage", "jahr_eur", "€",
     {"device_class": "monetary", "state_class": "measurement",
      "icon": "mdi:currency-eur", "suggested_display_precision": 0}),
    ("ablesung", "Ablesung laut Bild", "ablesung", None,
     {"icon": "mdi:water-pump", "entity_category": "diagnostic"}),
    ("letzte_messung", "Letzte Messung", "letzte_messung", None,
     {"device_class": "timestamp", "icon": "mdi:clock-outline",
      "entity_category": "diagnostic"}),
    ("bild_alter", "Bildalter", "alter_sekunden", "s",
     {"device_class": "duration", "state_class": "measurement",
      "icon": "mdi:timer-outline", "entity_category": "diagnostic",
      "suggested_display_precision": 0}),
]

ABLAUF_SEKUNDEN = 900        # Gas: nach 15 min ohne Wert nicht verfuegbar
ABLAUF_WASSER = 93600        # Wasser: taegliche Messung -> 26 Stunden


def gas_leistung_kurz(minuten=20):
    """Momentaner Gasfluss in m3/h - gleiche Filterung wie die Webseite.

    Die Kamera liest alle 2 Minuten. Ein einzelner OCR-Fehlgriff (rollende
    Einerwalze, +1,0 m3) darf hier nicht als Spitze auftauchen, deshalb werden
    nur die physikalisch moeglichen Zuwaechse summiert (server._zuwaechse,
    0,12 m3/min) und durch die Fensterdauer geteilt - nicht die Differenz
    erster/letzter Messwert gebildet.
    """
    grenze = int(time.time() - minuten * 60)
    con = gas.db()
    try:
        reihe = con.execute(
            "SELECT ts, m3, quelle FROM messungen WHERE ts >= ? ORDER BY ts",
            (grenze,)).fetchall()
    finally:
        con.close()
    if len(reihe) < 2:
        return 0.0
    dauer = (reihe[-1][0] - reihe[0][0]) / 3600.0
    if dauer <= 0:
        return 0.0
    zu = server._zuwaechse(reihe, 5.0, server.KORREKTUR_QUELLEN, 0.12)
    return round(max(0.0, sum(d for _t0, _t1, d in zu) / dauer), 3)


MEDIAN_N = 5              # Anzahl Messwerte fuer den Median-Zaehlerstand
RUECKWAERTS_M3 = 0.2      # so weit darf der veroeffentlichte Stand je Lauf sinken
VORWAERTS_PRO_MIN = 0.12  # m3/min - physikalische Obergrenze der Zaehlerdrehung


def stand_median(fallback=None):
    """Median der letzten Messwerte als Zaehlerstand.

    Die Einerwalze rollt mechanisch; die Kamera liest sie dabei gelegentlich
    eine Ziffer weiter (+1,0 m3). Ein solcher Einzelausreisser verschwindet im
    Median ueber MEDIAN_N Messwerte, ein echter Zuwachs erscheint mit hoechstens
    zwei Messungen Verzoegerung.
    """
    con = gas.db()
    try:
        reihe = con.execute(
            "SELECT m3 FROM messungen WHERE quelle NOT IN ('start', 'manuell') "
            "ORDER BY ts DESC LIMIT ?", (MEDIAN_N,)).fetchall()
    finally:
        con.close()
    werte = sorted(float(z[0]) for z in reihe if z[0] is not None)
    if not werte:
        return fallback
    return werte[len(werte) // 2]


def _stand_plausibel(wert, schluessel="mqtt_stand", pro_minute=VORWAERTS_PRO_MIN,
                     max_sprung=30.0):
    """Plausiblen Zaehlerstand veroeffentlichen (weiches Klemmen).

    HA zaehlt bei einem total_increasing-Sensor Vorwaertsspruenge als
    Verbrauch und Rueckwaertsspruenge als negatives Delta. Deshalb:
    - Vorwaerts ist nur erlaubt, was der Zaehler in der verstrichenen Zeit
      gedreht haben kann (pro_minute) plus 0,25 Reserve.
    - Rueckwaerts wird *nie* veroeffentlicht (fester Stand), weil Home
      Assistant daraus ein negatives Delta macht.
    """
    if wert is None:
        return None
    wert = float(wert)
    con = gas.db()
    try:
        def hole(schluessel):
            zeile = con.execute("SELECT wert FROM zustand WHERE schluessel = ?",
                                (schluessel,)).fetchone()
            if not zeile or zeile[0] in (None, ""):
                return None
            try:
                return float(zeile[0])
            except (TypeError, ValueError):
                return None

        def setze(schluessel, wert_):
            con.execute("INSERT OR REPLACE INTO zustand (schluessel, wert) VALUES (?, ?)",
                        (schluessel, wert_ if isinstance(wert_, str) else "%.3f" % wert_))
            con.commit()

        letzter = hole(f"{schluessel}_ok")
        ts_letzter = hole(f"{schluessel}_ts")
        jetzt = time.time()
        if letzter is None or ts_letzter is None:      # erster Lauf: uebernehmen
            setze(f"{schluessel}_ok", wert)
            setze(f"{schluessel}_ts", "%.0f" % jetzt)
            return wert
        minuten = max(2.0, (jetzt - ts_letzter) / 60.0)
        grenze = pro_minute * minuten + 0.25
        if wert > letzter + grenze:
            # Nach oben ist nur erlaubt, was sich beim naechsten Lauf
            # bestaetigt (z. B. Nachholung nach einem Stream-Ausfall). Ein
            # einmaliger Fehlgriff bleibt damit draussen.
            kandidat = hole(f"{schluessel}_kandidat")
            ts_kandidat = hole(f"{schluessel}_kandidat_ts")
            if (kandidat is not None and abs(kandidat - wert) < 0.001
                    and ts_kandidat is not None and jetzt - ts_kandidat <= 1200
                    and wert - letzter <= max_sprung):
                setze(f"{schluessel}_kandidat", "")
                setze(f"{schluessel}_ok", wert)
                setze(f"{schluessel}_ts", "%.0f" % jetzt)
                return wert
            setze(f"{schluessel}_kandidat", wert)
            setze(f"{schluessel}_kandidat_ts", "%.0f" % jetzt)
            return letzter                              # unbestaetigt
        if wert < letzter:
            # Nie rueckwaerts veroeffentlichen: Home Assistant macht aus
            # jedem Rueckgang ein negatives Delta (sichtbar als negativer
            # Gasfluss im Energie-Dashboard). Korrekturen nach unten laufen
            # ueber den echten Zaehlerstand - der steigt ja weiter und holt
            # den veroeffentlichten Wert von selbst ein.
            return letzter
        setze(f"{schluessel}_kandidat", "")
        setze(f"{schluessel}_ok", wert)
        setze(f"{schluessel}_ts", "%.0f" % jetzt)
        return wert
    finally:
        con.close()


def werte():
    """Aktuelle Messwerte flach als Dict."""
    cfg = gas.load_config()
    zus = server.zusammenfassung(cfg["tarif"]) or {}
    if "stand" not in zus:
        return {}
    ts = zus.get("stand_ts", time.time())
    try:
        letzte = datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError):
        letzte = ""
    return {
        "zaehlerstand_m3": _stand_plausibel(stand_median(zus.get("stand"))),
        "verbrauch_heute_m3": zus.get("tag_m3"),
        "verbrauch_woche_m3": zus.get("woche_m3"),
        "verbrauch_monat_m3": zus.get("monat_m3"),
        "verbrauch_jahr_m3": zus.get("jahr_m3"),
        "verbrauch_stunde_m3": zus.get("pro_stunde_m3"),
        "leistung_aktuell_m3h": gas_leistung_kurz(),
        "kosten_heute_eur": zus.get("tag_eur"),
        "kosten_monat_eur": zus.get("monat_eur"),
        "kosten_jahr_eur": zus.get("jahr_eur"),
        "ablesung": zus.get("ablesung") or "",
        "letzte_messung": letzte,
        "alter_sekunden": max(0, int(time.time() - ts)),
    }


def wasser_werte():
    """Aktuelle Wasserwerte flach als Dict."""
    cfg = gas.load_config()
    zus = server.wasser_kennzahlen((cfg.get("wasser") or {}).get("tarif") or {}) or {}
    if not zus or zus.get("stand_m3") is None:
        return {}
    deltas = server.wasser_deltas()
    jetzt = time.time()

    def summe(sekunden):
        return round(sum(d for t, d in deltas if t >= jetzt - sekunden), 3)

    alter = zus.get("alter_sekunden")
    letzte = (time.time() - alter) if alter is not None else None
    try:
        iso = (datetime.fromtimestamp(letzte).astimezone().isoformat(timespec="seconds")
               if letzte else "")
    except (OSError, OverflowError):
        iso = ""
    return {
        # Auch fuer Wasser nie rueckwaerts veroeffentlichen (negatives Delta
        # im HA-Energie-Dashboard). Hoechstens 0,10 m3/min sind plausibel.
        "stand_m3": _stand_plausibel(zus.get("stand_m3"), "mqtt_wasser_stand", 0.10,
                                     max_sprung=5.0),
        "tag_m3": summe(86400),
        "leistung_aktuell_m3h": round(summe(1800) * 2, 3),
        "woche_m3": summe(7 * 86400),
        "monat_m3": summe(30.4375 * 86400),
        "jahr_m3": summe(365.25 * 86400),
        "tag_eur": zus.get("tag_eur"),
        "monat_eur": zus.get("monat_eur"),
        "jahr_eur": zus.get("jahr_eur"),
        "ablesung": zus.get("ablesung") or "",
        "letzte_messung": iso,
        "alter_sekunden": zus.get("alter_sekunden"),
    }


def _pakete_fuer(daten, entitaeten, geraet, basis, entdeckung, ablauf=ABLAUF_SEKUNDEN):
    verfuegbar = f"{basis}/verfuegbar"
    liste = [(verfuegbar, "online")]
    for objekt, name, feld, einheit, optionen in entitaeten:
        wert = daten.get(feld)
        if wert is None:
            continue
        genauigkeit = optionen.get("genauigkeit")
        if isinstance(wert, float):
            inhalt = f"{wert:.{genauigkeit}f}" if genauigkeit is not None else repr(wert)
        else:
            inhalt = str(wert)
        konfig = {
            "name": name,
            "unique_id": f"{basis}_{objekt}",
            "state_topic": f"{basis}/{objekt}",
            "availability_topic": verfuegbar,
            "payload_available": "online",
            "payload_not_available": "offline",
            "expire_after": ablauf,
            "device": geraet,
        }
        if einheit:
            konfig["unit_of_measurement"] = einheit
        for schluessel in ("device_class", "state_class", "icon",
                           "suggested_display_precision", "entity_category"):
            if schluessel in optionen:
                konfig[schluessel] = optionen[schluessel]
        liste.append((f"{entdeckung}/sensor/{basis}/{objekt}/config",
                      json.dumps(konfig, ensure_ascii=False)))
        liste.append((f"{basis}/{objekt}", inhalt))
    return liste


def pakete(cfg=None):
    """Alle (Thema, Inhalt)-Paare fuer Discovery und Zustaende."""
    cfg = cfg or gas.load_config()
    k = cfg.get("mqtt") or {}
    basis = k.get("basis", "gaszaehler")
    entdeckung = k.get("entdeckung", "homeassistant")
    verfuegbar = f"{basis}/verfuegbar"

    daten = werte()
    if not daten:
        raise RuntimeError("keine Messwerte vorhanden")

    liste = _pakete_fuer(daten, ENTITAETEN, GERAET, basis, entdeckung)

    # Wasserzaehler (eigenes Geraet)
    try:
        wdaten = wasser_werte()
    except Exception:
        wdaten = {}
    if wdaten:
        wbasis = k.get("basis_wasser", "wasserzaehler")
        liste += _pakete_fuer(wdaten, WASSER_ENTITAETEN, WASSER_GERAET,
                              wbasis, entdeckung, ablauf=ABLAUF_WASSER)
    return liste


def sende(cfg=None, zeigen=False):
    """Discovery-Konfigurationen und Zustaende veroeffentlichen."""
    cfg = cfg or gas.load_config()
    k = cfg.get("mqtt") or {}
    liste = pakete(cfg)

    if zeigen:
        for thema, inhalt in liste:
            print(f"{thema}\n    {inhalt}")
        return len(liste)

    if not k.get("aktiv"):
        return 0

    with mqtt.MqttClient(k.get("host", "127.0.0.1"), k.get("port", 1883),
                         k.get("benutzer"), k.get("passwort"),
                         client_id=k.get("client_id", "gaszaehler")) as client:
        for thema, inhalt in liste:
            client.publish(thema, inhalt, behalten=True)
    return len(liste)


def main():
    p = argparse.ArgumentParser(description="Messwerte an Home Assistant senden")
    p.add_argument("--zeigen", action="store_true",
                   help="Pakete nur anzeigen, nichts senden")
    args = p.parse_args()
    cfg = gas.load_config()
    if not args.zeigen and not (cfg.get("mqtt") or {}).get("aktiv"):
        print("MQTT ist in config.json nicht aktiviert (mqtt.aktiv = false).")
        print("Zum Prüfen: python3 mqtt_bruecke.py --zeigen")
        return 1
    anzahl = sende(cfg, zeigen=args.zeigen)
    if args.zeigen:
        print(f"\n{anzahl} MQTT-Nachrichten angezeigt (nichts gesendet).")
    else:
        print(f"{anzahl} MQTT-Nachrichten gesendet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
