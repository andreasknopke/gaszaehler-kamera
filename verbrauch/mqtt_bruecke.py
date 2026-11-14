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
ABLAUF_SEKUNDEN = 900


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
        "zaehlerstand_m3": zus.get("stand"),
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

    liste = [(verfuegbar, "online")]
    for objekt, name, feld, einheit, optionen in ENTITAETEN:
        wert = daten.get(feld)
        if wert is None:
            continue
        genauigkeit = optionen.get("genauigkeit")
        if isinstance(wert, float):
            inhalt = f"{wert:.{genauigkeit}f}" if genauigkeit else repr(wert)
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
            "device": GERAET,
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
