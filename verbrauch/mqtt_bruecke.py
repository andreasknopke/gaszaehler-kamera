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

ABLAUF_SEKUNDEN = 900


def _monoton(schluessel, wert):
    """Kumulativen Zaehlerstand nie kleiner veroeffentlichen als zuletzt.

    HA behandelt Sensoren mit state_class total_increasing bei jedem Rueckwaerts-
    sprung als Zaehlerreset (Warnung im Log, verfaelschte Statistik). Die
    Ablesung schwankt aber gelegentlich um eine Zehntelstelle.
    """
    if wert is None:
        return None
    con = gas.db()
    try:
        zeile = con.execute("SELECT wert FROM zustand WHERE schluessel = ?",
                            (schluessel,)).fetchone()
        vorher = None
        if zeile and zeile[0] not in (None, ""):
            try:
                vorher = float(zeile[0])
            except ValueError:
                vorher = None
        neu = float(wert) if vorher is None else max(float(wert), vorher)
        if vorher is None or neu != vorher:
            con.execute("INSERT OR REPLACE INTO zustand (schluessel, wert) VALUES (?, ?)",
                        (schluessel, str(neu)))
            con.commit()
        return neu
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
        "zaehlerstand_m3": _monoton("mqtt_stand_max", zus.get("stand")),
        "verbrauch_heute_m3": zus.get("tag_m3"),
        "verbrauch_woche_m3": zus.get("woche_m3"),
        "verbrauch_monat_m3": zus.get("monat_m3"),
        "verbrauch_jahr_m3": zus.get("jahr_m3"),
        "verbrauch_stunde_m3": zus.get("pro_stunde_m3"),
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
        "stand_m3": zus.get("stand_m3"),
        "tag_m3": summe(86400),
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


def _pakete_fuer(daten, entitaeten, geraet, basis, entdeckung):
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
            "expire_after": ABLAUF_SEKUNDEN,
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
                              wbasis, entdeckung)
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
