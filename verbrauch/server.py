#!/usr/bin/env python3
"""Web-UI fuer die Gasverbrauchsanzeige.

Start:  python3 server.py [--port 8077]
"""

import argparse
import calendar
import json
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import energiemix
import gas
import strom
import wasser

BASE = Path(__file__).resolve().parent
PORT = 8077

EINHEITEN = {
    "stunde":  {"format": "%d.%m. %H",  "anzahl": 48,   "std_pro_strich": 1},
    "tag":     {"format": "%d.%m.",     "anzahl": 31,   "std_pro_strich": 24},
    "monat":   {"format": "%m/%Y",      "anzahl": 12,   "std_pro_strich": 24 * 30.4375},
    "jahr":    {"format": "%Y",         "anzahl": 5,    "std_pro_strich": 24 * 365.25},
}


# ------------------------------------------------------------------ Aggregation

def periodenschluessel(ts, einheit):
    d = datetime.fromtimestamp(ts)
    if einheit == "stunde":
        return d.strftime("%Y-%m-%d %H:00")
    if einheit == "tag":
        return d.strftime("%Y-%m-%d")
    if einheit == "monat":
        return d.strftime("%Y-%m")
    return d.strftime("%Y")


def periodenliste(einheit, anzahl):
    """Die letzten `anzahl` Perioden,aelteste zuerst, mit Schluesseln."""
    jetzt = datetime.now()
    out = []
    if einheit == "stunde":
        start = jetzt.replace(minute=0, second=0, microsecond=0)
        schritt = timedelta(hours=1)
    elif einheit == "tag":
        start = jetzt.replace(hour=0, minute=0, second=0, microsecond=0)
        schritt = timedelta(days=1)
    elif einheit == "monat":
        start = jetzt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        schritt = None
    else:
        start = jetzt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        schritt = None

    if einheit in ("stunde", "tag"):
        for i in range(anzahl - 1, -1, -1):
            out.append((start - i * schritt).strftime(
                "%Y-%m-%d %H:00" if einheit == "stunde" else "%Y-%m-%d"))
    elif einheit == "monat":
        y, m = start.year, start.month
        keys = []
        for _ in range(anzahl):
            keys.append(f"{y:04d}-{m:02d}")
            m -= 1
            if m == 0:
                m, y = 12, y - 1
        out = list(reversed(keys))
    else:
        out = [str(y) for y in range(jetzt.year - anzahl + 1, jetzt.year + 1)]
    return out


def periodenliste_fuer(einheit, zeitraum):
    """Perioden fuer einen gewaehlten Tag/Monat/Jahr (Datum als YYYY-MM-DD).

    stunde -> die 24 Stunden dieses Tages
    tag    -> die Tage dieses Monats
    monat  -> die 12 Monate dieses Jahres
    jahr   -> die letzten fuenf Jahre
    """
    try:
        d = datetime.strptime(str(zeitraum)[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    if einheit == "stunde":
        start = d.replace(hour=0, minute=0, second=0, microsecond=0)
        return [(start + timedelta(hours=h)).strftime("%Y-%m-%d %H:00") for h in range(24)]
    if einheit == "tag":
        start = d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        naechster = (start + timedelta(days=32)).replace(day=1)
        aus, lauf = [], start
        while lauf < naechster:
            aus.append(lauf.strftime("%Y-%m-%d"))
            lauf += timedelta(days=1)
        return aus
    if einheit == "monat":
        return [f"{d.year:04d}-{m:02d}" for m in range(1, 13)]
    return periodenliste("jahr", 5)


def prognose(deltas, jetzt=None):
    """Hochrechnung aus *allen* Tagen mit Monatsprofil.

    Statt nur den letzten Tag fortzuschreiben, wird je Kalendermonat der
    mittlere Tagesverbrauch aus den vorhandenen Daten gebildet. Das Bild
    baut sich ueber das Jahr auf: Der Sommer (wenig Heizung, viel Solar)
    bekommt ein eigenes Monatsmittel, sobald Daten dafuer vorliegen.

    Monate ohne Daten werden mit dem Mittel aller vorhandenen Monate
    angesetzt - ehrlicher als eine erfundene Saisonkurve.

    -> dict mit stunde/tag/monat/jahr, oder None ohne brauchbare Daten
    """
    jetzt = jetzt or datetime.now()
    pro_tag, messpunkte = {}, {}
    for ts, diff in deltas:
        key = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        pro_tag[key] = pro_tag.get(key, 0.0) + diff
        messpunkte[key] = messpunkte.get(key, 0) + 1

    heute = jetzt.strftime("%Y-%m-%d")
    voll = {k: v for k, v in pro_tag.items()
            if k != heute and messpunkte.get(k, 0) >= 20}
    if not voll:
        return None

    pro_monat = {}
    for tag, summe in voll.items():
        pro_monat.setdefault(int(tag[5:7]), []).append(summe)
    mittel = {m: sum(werte) / len(werte) for m, werte in pro_monat.items()}
    gesamt = sum(mittel.values()) / len(mittel)

    aktuell = mittel.get(jetzt.month, gesamt)
    tage_im_monat = calendar.monthrange(jetzt.year, jetzt.month)[1]
    jahr = sum(mittel.get(m, gesamt) * calendar.monthrange(jetzt.year, m)[1]
               for m in range(1, 13))
    return {
        "stunde": aktuell / 24,
        "tag": aktuell,
        "monat": aktuell * tage_im_monat,
        "jahr": jahr,
        "monate_mit_daten": len(mittel),
        "tage_gesamt": len(voll),
        "basis": "monat" if jetzt.month in mittel else "gesamt",
    }


KORREKTUR_QUELLEN = ("start", "manuell")


def _periodenende(t, einheit):
    """Erster Zeitpunkt nach t, der in der naechsten Periode liegt."""
    d = datetime.fromtimestamp(t)
    if einheit == "stunde":
        e = d.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    elif einheit == "tag":
        e = d.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    elif einheit == "monat":
        e = (d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
             + timedelta(days=32)).replace(day=1)
    else:
        e = d.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        e = e.replace(year=e.year + 1)
    return e.timestamp()


def perioden_anteile(t0, t1, wert, einheit):
    """Einen Zuwachs gleichmaessig auf die Perioden verteilen, die er abdeckt.

    Ohne das landet der gesamte Zuwachs seit der letzten Messung in der
    Periode der neuen Messung - nach einer Luecke (Nacht, Ausfall) also
    alles in einer einzigen Stunde.
    """
    if t1 <= t0:
        return {periodenschluessel(t1, einheit): wert}
    dauer = t1 - t0
    anteile = {}
    t = t0
    while t < t1:
        schluessel = periodenschluessel(t, einheit)
        ende = min(t1, _periodenende(t, einheit))
        anteil = wert * (ende - t) / dauer
        anteile[schluessel] = anteile.get(schluessel, 0.0) + anteil
        t = ende
    return anteile


def _bereinigen(reihe, anker_quellen, tol=0.05, fenster=8):
    """Fehlablesungen rueckwirkend entfernen.

    Eine Spitze (ploetzlich hoeherer Wert), die wenige Messungen spaeter
    wieder zurueckfaellt, ist keine echte Zaehlung: Ein Gaszaehler laeuft
    nur vorwaerts. Solche Punkte werden verworfen. Ohne das zieht ein
    einzelner Ausreisser den Bezugswert dauerhaft nach oben, und jede
    spaetere echte Messung gilt als "rueckwaerts" und faellt weg.
    """
    werte = [z[1] for z in reihe]
    ergebnis = []
    for i, zeile in enumerate(reihe):
        ts, wert, quelle = zeile
        if quelle in anker_quellen:
            ergebnis.append(zeile)
            continue
        zukunft = werte[i + 1:i + 1 + fenster]
        if zukunft and wert > min(zukunft) + tol:
            continue                                  # Spitze: faellt gleich zurueck
        ergebnis.append(zeile)
    return ergebnis


def _zuwaechse(reihe, max_diff, anker_quellen):
    """(von, bis, zuwachs) aus einer Zaehlerreihe.

    Zuerst werden Fehlablesungen entfernt (_bereinigen), dann bleibt bei
    einem Rueckwaerts-Sprung der letzte gueltige Wert stehen, statt den
    anschliessenden "Erholungssprung" als Verbrauch zu buchen.
    """
    reihe = _bereinigen(reihe, anker_quellen)
    ergebnis, letzter = [], None
    for ts, wert, quelle in reihe:
        if letzter is None or quelle in anker_quellen:
            letzter = (ts, wert)
            continue
        if wert < letzter[1]:
            continue                                  # Rueckwaerts = Fehlablesung
        diff = wert - letzter[1]
        if diff <= max_diff:
            ergebnis.append((letzter[0], ts, diff))
        letzter = (ts, wert)
    return ergebnis


def _deltas():
    """(zeitpunkt, verbrauch_m3) - Korrektur- und Startwerte sind Anker, kein Verbrauch."""
    con = gas.db()
    try:
        reihe = con.execute("SELECT ts, m3, quelle FROM messungen ORDER BY ts").fetchall()
    finally:
        con.close()
    return [(ts, diff) for _t0, ts, diff in
            _zuwaechse(reihe, 5.0, KORREKTUR_QUELLEN)]


def serie(einheit, anzahl, tarif, zeitraum=None):
    """Verbrauch (m3/kWh/EUR) je Periode aus der Messreihe.

    Zuwaechse werden gleichmaessig auf die Perioden verteilt, die sie
    abdecken - nach einer Messluecke landet sonst alles in einer Stunde.
    """
    pro_periode = {}
    con = gas.db()
    try:
        reihe = con.execute("SELECT ts, m3, quelle FROM messungen ORDER BY ts").fetchall()
    finally:
        con.close()
    for t0, t1, diff in _zuwaechse(reihe, 5.0, KORREKTUR_QUELLEN):
        for schluessel, anteil in perioden_anteile(t0, t1, diff, einheit).items():
            pro_periode[schluessel] = pro_periode.get(schluessel, 0.0) + anteil

    spalten = []
    for schluessel in (periodenliste_fuer(einheit, zeitraum)
                       or periodenliste(einheit, anzahl)):
        m3 = round(pro_periode.get(schluessel, 0.0), 3)
        kwh = gas.kwh(m3, tarif)
        std = EINHEITEN[einheit]["std_pro_strich"]
        spalten.append({
            "periode": schluessel,
            "m3": m3,
            "kwh": round(kwh, 2),
            "eur": round(gas.kosten_energie(m3, tarif), 2),
            "gesamt_eur": round(gas.kosten_energie(m3, tarif)
                                + gas.grundkosten(std, tarif), 2),
        })
    return spalten


# ------------------------------------------------------- Strom (Netzbezug)

def strom_deltas(art="netz"):
    """(zeitpunkt, kWh) — Ankerpunkte zaehlen nicht als Verbrauch."""
    con = strom.db(art)
    try:
        reihe = con.execute(
            f"SELECT ts, kwh, quelle FROM {strom.tabelle(art)} ORDER BY ts").fetchall()
    finally:
        con.close()
    return [(ts, diff) for _t0, ts, diff in strom_zuwaechse(art)]


def strom_zuwaechse(art="netz"):
    """(von, bis, kWh) — Zuwaechse mit ihrem Zeitintervall."""
    con = strom.db(art)
    try:
        reihe = con.execute(
            f"SELECT ts, kwh, quelle FROM {strom.tabelle(art)} ORDER BY ts").fetchall()
    finally:
        con.close()
    return _zuwaechse(reihe, 100.0, ("anker",))


def strom_serie(einheit, anzahl, tarif, art="netz", zeitraum=None):
    """Netzbezug bzw. Hausverbrauch (kWh und EUR) je Periode."""
    pro_periode = {}
    for t0, t1, diff in strom_zuwaechse(art):
        for schluessel, anteil in perioden_anteile(t0, t1, diff, einheit).items():
            pro_periode[schluessel] = pro_periode.get(schluessel, 0.0) + anteil
    ap = float(tarif.get("arbeitspreis_ct_kwh") or 0)
    spalten = []
    for schluessel in (periodenliste_fuer(einheit, zeitraum)
                       or periodenliste(einheit, anzahl)):
        kwh = round(pro_periode.get(schluessel, 0.0), 3)
        std = EINHEITEN[einheit]["std_pro_strich"]
        spalten.append({
            "periode": schluessel,
            "kwh": kwh,
            "eur": round(kwh * ap / 100, 2),
            "gesamt_eur": round(kwh * ap / 100 + gas.grundkosten(std, tarif), 2),
        })
    return spalten


def strom_kennzahlen(tarif, art="netz"):
    jetzt = datetime.now()
    ap = float(tarif.get("arbeitspreis_ct_kwh") or 0)
    deltas = strom_deltas(art)
    tag = monat = jahr = gesamt = 0.0
    for ts, diff in deltas:
        d = datetime.fromtimestamp(ts)
        gesamt += diff
        if d.date() == jetzt.date():
            tag += diff
        if (d.year, d.month) == (jetzt.year, jetzt.month):
            monat += diff
        if d.year == jetzt.year:
            jahr += diff
    def rate(sekunden):
        """Mittlerer Bezug in kWh/h ueber das Zeitfenster."""
        grenze = time.time() - sekunden
        im_fenster = [(t, d) for t, d in deltas if t >= grenze]
        if not im_fenster:
            return 0.0
        dauer = (time.time() - max(grenze, im_fenster[0][0])) / 3600.0
        if dauer < 0.15:                              # unter 9 min: zu ungenau
            return 0.0
        return sum(d for _t, d in im_fenster) / dauer

    grund = float(tarif.get("grundpreis_eur_monat") or 0)

    def kosten(kwh, stunden):
        return round(kwh * ap / 100 + grund * stunden / (24 * 30.4375), 2)

    prog = prognose(deltas)
    if prog:
        pro_stunde, pro_tag = prog["stunde"], prog["tag"]
        pro_monat, pro_jahr = prog["monat"], prog["jahr"]
    else:
        pro_stunde = rate(86400) or rate(7 * 86400)
        pro_tag = pro_stunde * 24
        pro_monat = pro_stunde * 24 * 30.4375
        pro_jahr = pro_stunde * 24 * 365.25
    ts_letzte, stand = strom.letzte(art)
    return {
        "art": art,
        "stand_kwh": round(stand, 2) if stand is not None else None,
        "stand_text": (datetime.fromtimestamp(ts_letzte).strftime("%d.%m. %H:%M")
                       if ts_letzte else None),
        "alter_sekunden": max(0, int(time.time() - ts_letzte)) if ts_letzte else None,
        "tag_kwh": round(tag, 2), "tag_eur": round(tag * ap / 100, 2),
        "monat_kwh": round(monat, 2), "monat_eur": round(monat * ap / 100, 2),
        "jahr_kwh": round(jahr, 2), "jahr_eur": round(jahr * ap / 100, 2),
        "gesamt_kwh": round(gesamt, 2),
        "pro_stunde_kwh": round(pro_stunde, 3),
        "pro_stunde_eur": kosten(pro_stunde, 1),
        "pro_tag_kwh": round(pro_tag, 2),
        "pro_tag_eur": kosten(pro_tag, 24),
        "pro_monat_kwh": round(pro_monat, 1),
        "pro_monat_eur": kosten(pro_monat, 24 * 30.4375),
        "pro_jahr_kwh": round(pro_jahr, 0),
        "pro_jahr_eur": kosten(pro_jahr, 24 * 365.25),
        "prognose_basis": prog["basis"] if prog else "letzte24h",
        "prognose_monate": prog["monate_mit_daten"] if prog else 0,
        "prognose_tage": prog["tage_gesamt"] if prog else 0,
        "messpunkte": len(deltas),
    }


# ---------------------------------------------------- Wasser (Kamera .44)

def wasser_deltas():
    """(zeitpunkt, m3) — Anker zaehlen nicht als Verbrauch."""
    con = wasser.db()
    try:
        reihe = con.execute("SELECT ts, m3, quelle FROM wasser ORDER BY ts").fetchall()
    finally:
        con.close()
    return [(ts, diff) for _t0, ts, diff in wasser_zuwaechse()]


def wasser_zuwaechse():
    """(von, bis, m3) — Zuwaechse mit ihrem Zeitintervall."""
    con = wasser.db()
    try:
        reihe = con.execute("SELECT ts, m3, quelle FROM wasser ORDER BY ts").fetchall()
    finally:
        con.close()
    return _zuwaechse(reihe, 20.0, ("anker",))


def wasser_serie(einheit, anzahl, tarif, zeitraum=None):
    """Wasserverbrauch (m3 und EUR) je Periode."""
    pro_periode = {}
    for t0, t1, diff in wasser_zuwaechse():
        for schluessel, anteil in perioden_anteile(t0, t1, diff, einheit).items():
            pro_periode[schluessel] = pro_periode.get(schluessel, 0.0) + anteil
    preis = (float(tarif.get("wasserpreis_eur_m3") or 0)
             + float(tarif.get("abwasser_eur_m3") or 0))
    spalten = []
    for schluessel in (periodenliste_fuer(einheit, zeitraum)
                       or periodenliste(einheit, anzahl)):
        m3 = round(pro_periode.get(schluessel, 0.0), 2)
        std = EINHEITEN[einheit]["std_pro_strich"]
        spalten.append({
            "periode": schluessel,
            "m3": m3,
            "eur": round(m3 * preis, 2),
            "gesamt_eur": round(m3 * preis + gas.grundkosten(std, tarif), 2),
        })
    return spalten


def wasser_kennzahlen(tarif):
    jetzt = datetime.now()
    preis = (float(tarif.get("wasserpreis_eur_m3") or 0)
             + float(tarif.get("abwasser_eur_m3") or 0))
    deltas = wasser_deltas()
    tag = monat = jahr = gesamt = 0.0
    for ts, diff in deltas:
        d = datetime.fromtimestamp(ts)
        gesamt += diff
        if d.date() == jetzt.date():
            tag += diff
        if (d.year, d.month) == (jetzt.year, jetzt.month):
            monat += diff
        if d.year == jetzt.year:
            jahr += diff
    ts_letzte, stand = wasser.letzte()
    grund = float(tarif.get("grundpreis_eur_monat") or 0)
    return {
        "stand_m3": round(stand, 0) if stand is not None else None,
        "ablesung": wasser.ablesung_text(stand) if stand is not None else None,
        "stand_text": (datetime.fromtimestamp(ts_letzte).strftime("%d.%m. %H:%M")
                       if ts_letzte else None),
        "alter_sekunden": max(0, int(time.time() - ts_letzte)) if ts_letzte else None,
        "tag_m3": round(tag, 2), "tag_eur": round(tag * preis + grund / 30.4375, 2),
        "monat_m3": round(monat, 2), "monat_eur": round(monat * preis + grund, 2),
        "jahr_m3": round(jahr, 2), "jahr_eur": round(jahr * preis + grund * 12, 2),
        "gesamt_m3": round(gesamt, 2),
        "messpunkte": len(deltas),
    }


def _port_offen(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.6):
            return True
    except OSError:
        return False


def _viewer_starten(cfg):
    """Wasser-Viewer bei Bedarf starten (Kamera sonst untaetig)."""
    v = (cfg.get("wasser") or {}).get("viewer") or {}
    port = int(v.get("port", 8098))
    if _port_offen(port):
        return port
    befehl = [sys.executable, str(BASE.parent / "viewer.py"),
              "--camera", v.get("kamera", "192.168.188.44"),
              "--port", str(port),
              "--rotate", str(v.get("drehen", 270)),
              "--idle-exit", str(v.get("idle_exit", 600))]
    if v.get("zoom"):
        befehl += ["--zoom", str(v["zoom"]), "--scale", str(v.get("scale", 8))]
    try:
        subprocess.Popen(befehl, cwd=str(BASE.parent),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception:
        return port
    for _ in range(25):
        time.sleep(0.3)
        if _port_offen(port):
            break
    return port


def _energiemix_sicher():
    """Strommix; bei Netzproblemen None statt Fehler."""
    try:
        return energiemix.aktuell()
    except Exception:
        return None


def zusammenfassung(tarif):
    """Kennzahlen fuer die Karten und die Prognosen."""
    con = gas.db()
    try:
        letzte = con.execute("SELECT ts, m3 FROM messungen ORDER BY ts DESC LIMIT 1").fetchone()
        ablesung = con.execute(
            "SELECT ablesung FROM messungen ORDER BY ts DESC LIMIT 1").fetchone()
        messpunkte = con.execute(
            "SELECT COUNT(*) FROM messungen WHERE quelle NOT IN ('start','manuell')"
        ).fetchone()[0]
    finally:
        con.close()
    if not letzte:
        return {}

    diffs = _deltas()

    def verbrauch(sekunden):
        grenze = time.time() - sekunden
        return round(sum(d for t, d in diffs if t >= grenze), 3)

    def rate(sekunden):
        """Mittlerer Verbrauch in m3/h ueber das Zeitfenster."""
        grenze = time.time() - sekunden
        im_fenster = [t for t, _ in diffs if t >= grenze]
        if not im_fenster:
            return 0.0
        dauer = (time.time() - max(grenze, im_fenster[0])) / 3600.0
        if dauer < 0.15:                              # unter 9 min: zu ungenau
            return 0.0
        return sum(d for t, d in diffs if t >= grenze) / dauer

    letzter_ts, letzter_wert = letzte
    prog = prognose(diffs)
    if prog:
        pro_stunde, pro_tag = prog["stunde"], prog["tag"]
        pro_monat, pro_jahr = prog["monat"], prog["jahr"]
    else:
        pro_stunde = rate(86400) or rate(7 * 86400)
        pro_tag = pro_stunde * 24
        pro_monat = pro_stunde * 24 * 30.4375
        pro_jahr = pro_stunde * 24 * 365.25
    tag, woche = verbrauch(86400), verbrauch(7 * 86400)
    monat, jahr = verbrauch(30.4375 * 86400), verbrauch(365.25 * 86400)

    return {
        "stand": letzter_wert,
        "stand_ts": letzter_ts,
        "stand_text": datetime.fromtimestamp(letzter_ts).strftime("%d.%m.%Y %H:%M"),
        "ablesung": (ablesung[0] if ablesung else "") or "",
        "messpunkte": messpunkte,
        "tag_m3": tag,
        "woche_m3": woche,
        "monat_m3": monat,
        "jahr_m3": jahr,
        "tag_eur": round(gas.kosten_energie(tag, tarif)
                         + gas.grundkosten(24, tarif), 2),
        "monat_eur": round(gas.kosten_energie(monat, tarif)
                           + gas.grundkosten(24 * 30.4375, tarif), 2),
        "jahr_eur": round(gas.kosten_energie(jahr, tarif)
                          + gas.grundkosten(24 * 365.25, tarif), 2),
        "pro_stunde_m3": round(pro_stunde, 4),
        "pro_stunde_eur": round(gas.kosten_energie(pro_stunde, tarif)
                                + gas.grundkosten(1, tarif), 3),
        "pro_tag_m3": round(pro_tag, 3),
        "pro_tag_eur": round(gas.kosten_energie(pro_tag, tarif)
                             + gas.grundkosten(24, tarif), 2),
        "pro_monat_m3": round(pro_monat, 2),
        "pro_monat_eur": round(gas.kosten_energie(pro_monat, tarif)
                               + gas.grundkosten(24 * 30.4375, tarif), 2),
        "pro_jahr_m3": round(pro_jahr, 1),
        "pro_jahr_eur": round(gas.kosten_energie(pro_jahr, tarif)
                              + gas.grundkosten(24 * 365.25, tarif), 2),
        "prognose_basis": prog["basis"] if prog else "letzte24h",
        "prognose_monate": prog["monate_mit_daten"] if prog else 0,
        "prognose_tage": prog["tage_gesamt"] if prog else 0,
    }


def letzte_messungen(n=40):
    con = gas.db()
    try:
        return [
            {"ts": t, "zeit": datetime.fromtimestamp(t).strftime("%d.%m. %H:%M:%S"),
             "m3": m, "quelle": q, "roh": r, "ablesung": a}
            for t, m, q, r, a in con.execute(
                "SELECT ts, m3, quelle, roh, ablesung FROM messungen ORDER BY ts DESC LIMIT ?",
                (n,)).fetchall()
        ]
    finally:
        con.close()


def lernstand():
    import ziffern
    bank = ziffern.Ziffernbank()
    return {"vollstaendig": bank.vollstaendig(), "stand": bank.lernstand(),
            "labels": bank.labels(), "anzahl": sum(len(v) for v in bank.lernstand().values())}


def notiz_lesen():
    con = gas.db()
    try:
        row = con.execute("SELECT wert FROM zustand WHERE schluessel='letzte_notiz'").fetchone()
    finally:
        con.close()
    return row[0] if row else ""


def fotos(n=24):
    detail = sorted((BASE / "fotos").glob("zaehler-detail-*.jpg"))
    if detail:
        liste = [p.name for p in detail][-n:]
    else:
        liste = [p.name for p in sorted((BASE / "fotos").glob("zaehler-*.jpg"))][-n:]
    # Einzelbilder der Walzen (Diagnose) immer mit anbieten
    return liste + [p.name for p in sorted((BASE / "fotos").glob("zaehler-walze*.png"))]


# ------------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "Gasverbrauch/1.0"

    def log_message(self, *a):
        pass

    def _json(self, daten, code=200):
        body = json.dumps(daten, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _datei(self, pfad, typ):
        try:
            body = Path(pfad).read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        frage = parse_qs(url.query)
        cfg = gas.load_config()
        tarif = cfg["tarif"]

        if url.path in ("/", "/index.html"):
            return self._datei(BASE / "static" / "index.html", "text/html; charset=utf-8")
        if url.path == "/style.css":
            return self._datei(BASE / "static" / "style.css", "text/css; charset=utf-8")
        if url.path == "/app.js":
            return self._datei(BASE / "static" / "app.js", "application/javascript; charset=utf-8")
        if url.path.startswith("/fotos/"):
            name = Path(url.path).name
            if ".." in name:
                return self.send_error(400)
            typ = "image/png" if name.lower().endswith(".png") else "image/jpeg"
            return self._datei(BASE / "fotos" / name, typ)

        if url.path == "/api/homeassistant":
            # Flaches JSON fuer Home-Assistant-REST-Sensoren und Skripte.
            k = zusammenfassung(tarif)
            if not k:
                return self._json({"fehler": "keine Messwerte"}, 503)
            ts = k.get("stand_ts", time.time())
            return self._json({
                "zaehlerstand_m3": k["stand"],
                "ablesung": k.get("ablesung", ""),
                "verbrauch_heute_m3": k.get("tag_m3"),
                "verbrauch_woche_m3": k.get("woche_m3"),
                "verbrauch_monat_m3": k.get("monat_m3"),
                "verbrauch_jahr_m3": k.get("jahr_m3"),
                "verbrauch_stunde_m3": k.get("pro_stunde_m3"),
                "kosten_heute_eur": k.get("tag_eur"),
                "kosten_monat_eur": k.get("monat_eur"),
                "kosten_jahr_eur": k.get("jahr_eur"),
                "letzte_messung": datetime.fromtimestamp(ts).astimezone()
                                  .isoformat(timespec="seconds"),
                "alter_sekunden": max(0, int(time.time() - ts)),
                "tarif_bestaetigt": bool(tarif.get("bestaetigt")),
            })

        if url.path == "/api/uebersicht":
            einheit = (frage.get("einheit", ["tag"])[0]).lower()
            if einheit not in EINHEITEN:
                einheit = "tag"
            anzahl = int(frage.get("anzahl", [EINHEITEN[einheit]["anzahl"]])[0])
            zeitraum = (frage.get("zeitraum", [""])[0] or None)
            return self._json({
                "einheit": einheit,
                "zeitraum": zeitraum,
                "spalten": serie(einheit, anzahl, tarif, zeitraum),
                "kennzahlen": zusammenfassung(tarif),
                "tarif": tarif,
                "fotos": fotos(),
                "messungen": letzte_messungen(),
                "basis_bestaetigt": bool(cfg["erfassung"].get("basis_bestaetigt")),
                "bank": lernstand(),
                "notiz": notiz_lesen(),
                "strom": {
                    "kennzahlen": strom_kennzahlen((cfg.get("strom") or {}).get("tarif") or {}),
                    "tarif": (cfg.get("strom") or {}).get("tarif") or {},
                },
                "energiemix": _energiemix_sicher(),
                "wasser": {
                    "kennzahlen": wasser_kennzahlen((cfg.get("wasser") or {}).get("tarif") or {}),
                    "tarif": (cfg.get("wasser") or {}).get("tarif") or {},
                },
                "serverzeit": time.time(),
            })
        if url.path == "/wasserbild":
            # Wasser-Viewer nur starten, wenn jemand schauen will - sonst
            # laeuft die Kamera den ganzen Tag unnoetig.
            port = _viewer_starten(cfg)
            host = (self.headers.get("Host") or "localhost").split(":")[0]
            self.send_response(302)
            self.send_header("Location", f"http://{host}:{port}/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if url.path == "/api/energiemix":
            try:
                return self._json(energiemix.aktuell())
            except Exception as exc:
                return self._json({"fehler": str(exc)}, 503)
        if url.path == "/api/wasser":
            einheit = (frage.get("einheit", ["tag"])[0]).lower()
            if einheit not in EINHEITEN:
                einheit = "tag"
            anzahl = int(frage.get("anzahl", [EINHEITEN[einheit]["anzahl"]])[0])
            zeitraum = (frage.get("zeitraum", [""])[0] or None)
            tarif_w = (cfg.get("wasser") or {}).get("tarif") or {}
            return self._json({
                "einheit": einheit,
                "zeitraum": zeitraum,
                "spalten": wasser_serie(einheit, anzahl, tarif_w, zeitraum),
                "kennzahlen": wasser_kennzahlen(tarif_w),
                "tarif": tarif_w,
            })
        if url.path == "/api/strom":
            einheit = (frage.get("einheit", ["tag"])[0]).lower()
            if einheit not in EINHEITEN:
                einheit = "tag"
            art = (frage.get("art", ["netz"])[0]).lower()
            if art not in strom.ARTEN:
                art = "netz"
            anzahl = int(frage.get("anzahl", [EINHEITEN[einheit]["anzahl"]])[0])
            zeitraum = (frage.get("zeitraum", [""])[0] or None)
            tarif_s = (cfg.get("strom") or {}).get("tarif") or {}
            return self._json({
                "einheit": einheit,
                "art": art,
                "zeitraum": zeitraum,
                "spalten": strom_serie(einheit, anzahl, tarif_s, art, zeitraum),
                "kennzahlen": strom_kennzahlen(tarif_s, art),
                "tarif": tarif_s,
            })
        if url.path == "/api/stromtarif":
            return self._json({"tarif": (cfg.get("strom") or {}).get("tarif") or {}})
        return self.send_error(404)

    def do_POST(self):
        url = urlparse(self.path)
        laenge = int(self.headers.get("Content-Length", 0))
        try:
            daten = json.loads(self.rfile.read(laenge) or b"{}")
        except json.JSONDecodeError:
            return self._json({"fehler": "ungueltiges JSON"}, 400)

        cfg = gas.load_config()
        if url.path == "/api/stromtarif":
            strom_cfg = cfg.setdefault("strom", {})
            t = strom_cfg.setdefault("tarif", {})
            for feld in ("anbieter", "tarifname", "hinweis"):
                if feld in daten:
                    t[feld] = daten[feld]
            for feld in ("arbeitspreis_ct_kwh", "grundpreis_eur_monat"):
                if feld in daten:
                    try:
                        t[feld] = float(str(daten[feld]).replace(",", "."))
                    except ValueError:
                        return self._json({"fehler": f"{feld} ist keine Zahl"}, 400)
            if "bestaetigt" in daten:
                t["bestaetigt"] = bool(daten["bestaetigt"])
            gas.save_config(cfg)
            return self._json({"ok": True, "tarif": t})
        if url.path == "/api/wasserkorrektur":
            roh = str(daten.get("wert", "")).replace(",", ".").strip()
            try:
                wert = float(roh)
            except ValueError:
                return self._json({"fehler": "kein Wert"}, 400)
            if not 0 <= wert <= 99999:
                return self._json({"fehler": "Wert ausserhalb 0..99999"}, 400)
            wasser.anker_setzen(wert)
            return self._json({"ok": True, "wert": wert,
                               "ablesung": wasser.ablesung_text(wert)})
        if url.path == "/api/wassertarif":
            wcfg = cfg.setdefault("wasser", {})
            tw = wcfg.setdefault("tarif", {})
            for feld in ("anbieter", "tarifname", "hinweis"):
                if feld in daten:
                    tw[feld] = daten[feld]
            for feld in ("wasserpreis_eur_m3", "abwasser_eur_m3",
                         "grundpreis_eur_monat"):
                if feld in daten:
                    try:
                        tw[feld] = float(str(daten[feld]).replace(",", "."))
                    except ValueError:
                        return self._json({"fehler": f"{feld} ist keine Zahl"}, 400)
            if "bestaetigt" in daten:
                tw["bestaetigt"] = bool(daten["bestaetigt"])
            gas.save_config(cfg)
            return self._json({"ok": True, "tarif": tw})
        if url.path == "/api/tarif":
            for feld in ("anbieter", "tarifname", "hinweis"):
                if feld in daten:
                    cfg["tarif"][feld] = daten[feld]
            for feld in ("arbeitspreis_ct_kwh", "grundpreis_eur_monat",
                         "brennwert_kwh_m3", "zustandszahl"):
                if feld in daten:
                    try:
                        cfg["tarif"][feld] = float(str(daten[feld]).replace(",", "."))
                    except ValueError:
                        return self._json({"fehler": f"{feld} ist keine Zahl"}, 400)
            cfg["tarif"]["bestaetigt"] = True
            cfg["tarif"]["hinweis"] = ""
            gas.save_config(cfg)
            return self._json({"ok": True, "tarif": cfg["tarif"]})
        if url.path == "/api/korrektur":
            try:
                wert = float(str(daten.get("wert", "")).replace(",", "."))
            except ValueError:
                return self._json({"fehler": "kein Wert"}, 400)
            gas.speichere_messung(time.time(), wert, "manuell", 1.0, "Korrektur im Browser")
            cfg["erfassung"]["startwert_m3"] = wert
            cfg["erfassung"]["basis_bestaetigt"] = True
            gas.save_config(cfg)
            con = gas.db()
            try:
                import tracker
                import ziffern
                tracker.setze_basiswert(con, wert)
                tracker.zuruecksetzen()          # Referenzbilder neu anlernen
                zellen = tracker.zellen_aus(gas.hole_bild(cfg))
                bank = ziffern.Ziffernbank()
                gelernt = bank.setze_basis(zellen, wert)
                tracker.schritt(zellen, con, bank=bank)   # Referenzbilder setzen
            finally:
                con.close()
            return self._json({"ok": True, "wert": wert,
                               "ablesung": ziffern.text(gelernt)})

        return self.send_error(404)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    gas.db().close()
    httpd = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"Gasverbrauch-UI:  http://localhost:{args.port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
