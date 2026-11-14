#!/usr/bin/env python3
"""Stromkennzahlen des Wechselrichters — Erfassung und Datenhaltung.

Zwei kumulative Zaehler werden erfasst, beide als retained MQTT-Nachricht vom
Dienst sofar-mqtt:

* ``sofar_hyd/bezug_gesamt``     — Netzbezug (art="netz", Tabelle ``strom``)
* ``sofar_hyd/verbrauch_gesamt`` — Hausverbrauch/Lastbezug (art="haus", Tabelle ``haus``)

Die Auswertung je Stunde/Tag/Monat/Jahr entsteht in server.py aus den Differenzen
aufeinanderfolgender Staende — genau wie beim Gaszaehler.
"""

import time
from pathlib import Path

import gas
import mqtt

BASE = Path(__file__).resolve().parent

# art -> (Tabelle, MQTT-Thema Gesamtstand, MQTT-Thema Tageswert)
ARTEN = {
    "netz": ("strom", "sofar_hyd/bezug_gesamt", "sofar_hyd/bezug_heute"),
    "haus": ("haus", "sofar_hyd/verbrauch_gesamt", "sofar_hyd/verbrauch_heute"),
}
THEMA_STANDARD = ARTEN["netz"][1]

SCHEMA = """
CREATE TABLE IF NOT EXISTS {tabelle} (
    ts     INTEGER PRIMARY KEY,
    kwh    REAL NOT NULL,
    quelle TEXT NOT NULL
)
"""


def tabelle(art="netz"):
    if art not in ARTEN:
        raise ValueError(f"unbekannte Art: {art}")
    return ARTEN[art][0]


def thema(art="netz"):
    return ARTEN[art][1]


def thema_heute(art="netz"):
    return ARTEN[art][2]


def db(art="netz"):
    con = gas.db()
    con.execute(SCHEMA.format(tabelle=tabelle(art)))
    con.commit()
    return con


def letzte(art="netz"):
    """(zeitstempel, kwh) des juengsten Standes oder (None, None)."""
    con = db(art)
    try:
        zeile = con.execute(
            f"SELECT ts, kwh FROM {tabelle(art)} ORDER BY ts DESC LIMIT 1").fetchone()
        return (zeile[0], zeile[1]) if zeile else (None, None)
    finally:
        con.close()


def speichere(ts, kwh, quelle="mqtt", art="netz"):
    con = db(art)
    try:
        con.execute(
            f"INSERT OR REPLACE INTO {tabelle(art)} (ts, kwh, quelle) VALUES (?, ?, ?)",
            (int(ts), float(kwh), quelle))
        con.commit()
    finally:
        con.close()


def anker_setzen(kwh, ts=None, art="netz"):
    """Startpunkt (z. B. Tagesbeginn) — zaehlt selbst nicht als Verbrauch."""
    speichere(int(ts or time.time()), kwh, "anker", art)


def stand_aus_broker(cfg=None, thema_=None, timeout=12, art="netz"):
    """Kumulativen Zaehlerstand aus dem MQTT-Broker lesen."""
    cfg = cfg or gas.load_config()
    m = cfg.get("mqtt") or {}
    if not m.get("host"):
        raise RuntimeError("kein MQTT-Broker in config.json eingetragen")
    if thema_ is None:
        # Das Thema haengt an der Art (Netzbezug vs. Hausverbrauch). Ein Eintrag
        # strom.thema in der Config gilt nur fuer den Netzbezug, sonst wuerde der
        # Hausverbrauch den Netzbezugszaehler lesen.
        thema_ = thema(art)
        if art == "netz" and (cfg.get("strom") or {}).get("thema"):
            thema_ = cfg["strom"]["thema"]
    with mqtt.MqttClient(m["host"], int(m.get("port") or 1883),
                         m.get("benutzer"), m.get("passwort"),
                         client_id="gasanzeige-strom", timeout=timeout) as client:
        roh = client.lesen(thema_, timeout=timeout)
    return thema_, float(str(roh).strip().replace(",", "."))


def sammeln(cfg=None, erzwingen=False, mindestabstand=600, art="netz"):
    """Einmal messen und ablegen. Liefert (ts, kwh, neu).

    Es wird *immer* gespeichert, auch wenn der Zaehler unveraendert ist: Nur so
    ist der Zeitpunkt jeder Messung bekannt und die Stundenwerte lassen sich
    sauber zuordnen.
    """
    cfg = cfg or gas.load_config()
    _thema, kwh = stand_aus_broker(cfg, art=art)
    jetzt = int(time.time())
    speichere(jetzt, kwh, "mqtt", art)
    return jetzt, kwh, True


def sammeln_alle(cfg=None):
    """Beide Zaehler (Netzbezug und Hausverbrauch) in einem Lauf."""
    cfg = cfg or gas.load_config()
    ergebnis = {}
    for art in ARTEN:
        ergebnis[art] = sammeln(cfg, art=art)
    return ergebnis
