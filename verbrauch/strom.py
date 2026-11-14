#!/usr/bin/env python3
"""Strombezug aus dem Netz — Erfassung und Datenhaltung.

Der Wechselrichter (Sofar HYD 10KTL-3PH) fuehrt einen kumulativen Zaehler des
Netzbezugs und veroeffentlicht ihn ueber den Dienst sofar-mqtt als retained
MQTT-Nachricht (``sofar_hyd/bezug_gesamt``, Einheit kWh). Jeder Messlauf legt
einen Stand in der Tabelle ``strom`` ab; die Auswertung je Stunde/Tag/Monat/Jahr
entsteht in server.py aus den Differenzen aufeinanderfolgender Staende — genau
wie beim Gaszaehler.
"""

import time
from datetime import datetime
from pathlib import Path

import gas
import mqtt

BASE = Path(__file__).resolve().parent
THEMA_STANDARD = "sofar_hyd/bezug_gesamt"

SCHEMA = """
CREATE TABLE IF NOT EXISTS strom (
    ts     INTEGER PRIMARY KEY,
    kwh    REAL NOT NULL,
    quelle TEXT NOT NULL
)
"""


def db():
    con = gas.db()
    con.execute(SCHEMA)
    con.commit()
    return con


def letzte():
    """(zeitstempel, kwh) des juengsten Standes oder (None, None)."""
    con = db()
    try:
        zeile = con.execute("SELECT ts, kwh FROM strom ORDER BY ts DESC LIMIT 1").fetchone()
        return (zeile[0], zeile[1]) if zeile else (None, None)
    finally:
        con.close()


def speichere(ts, kwh, quelle="mqtt"):
    con = db()
    try:
        con.execute("INSERT OR REPLACE INTO strom (ts, kwh, quelle) VALUES (?, ?, ?)",
                    (int(ts), float(kwh), quelle))
        con.commit()
    finally:
        con.close()


def anker_setzen(kwh, ts=None):
    """Startpunkt (z. B. Tagesbeginn) — zaehlt selbst nicht als Verbrauch."""
    speichere(int(ts or time.time()), kwh, "anker")


def stand_aus_broker(cfg=None, thema=None, timeout=12):
    """Kumulativen Zaehlerstand aus dem MQTT-Broker lesen."""
    cfg = cfg or gas.load_config()
    m = cfg.get("mqtt") or {}
    if not m.get("host"):
        raise RuntimeError("kein MQTT-Broker in config.json eingetragen")
    thema = thema or (cfg.get("strom") or {}).get("thema") or THEMA_STANDARD
    with mqtt.MqttClient(m["host"], int(m.get("port") or 1883),
                         m.get("benutzer"), m.get("passwort"),
                         client_id="gasanzeige-strom", timeout=timeout) as client:
        roh = client.lesen(thema, timeout=timeout)
    return thema, float(str(roh).strip().replace(",", "."))


def sammeln(cfg=None, erzwingen=False, mindestabstand=600):
    """Einmal messen und ablegen. Liefert (ts, kwh, neu)."""
    cfg = cfg or gas.load_config()
    _thema, kwh = stand_aus_broker(cfg)
    ts_alt, kwh_alt = letzte()
    jetzt = int(time.time())
    if (not erzwingen and kwh_alt is not None
            and abs(kwh - kwh_alt) < 1e-9 and jetzt - (ts_alt or 0) < mindestabstand):
        return ts_alt, kwh_alt, False
    speichere(jetzt, kwh, "mqtt")
    return jetzt, kwh, True
