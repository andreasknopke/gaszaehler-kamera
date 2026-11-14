#!/usr/bin/env python3
"""Web-UI fuer die Gasverbrauchsanzeige.

Start:  python3 server.py [--port 8077]
"""

import argparse
import json
import sqlite3
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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


def _deltas():
    """(zeitpunkt, verbrauch_m3) - Korrektur- und Startwerte sind Anker, kein Verbrauch."""
    con = gas.db()
    try:
        reihe = con.execute("SELECT ts, m3, quelle FROM messungen ORDER BY ts").fetchall()
    finally:
        con.close()
    diffs, vorher = [], None
    for ts, m3, quelle in reihe:
        if vorher is not None and quelle not in KORREKTUR_QUELLEN:
            diff = m3 - vorher[1]
            if 0 <= diff <= 5:                       # Ausreisser/Luecken verwerfen
                diffs.append((ts, diff))
        vorher = (ts, m3)
    return diffs


def serie(einheit, anzahl, tarif):
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
    vorher = None
    for ts, m3, quelle in reihe:
        if vorher is not None and quelle not in KORREKTUR_QUELLEN:
            diff = m3 - vorher[1]
            if 0 <= diff <= 5:
                for schluessel, anteil in perioden_anteile(vorher[0], ts, diff, einheit).items():
                    pro_periode[schluessel] = pro_periode.get(schluessel, 0.0) + anteil
        vorher = (ts, m3)

    spalten = []
    for schluessel in periodenliste(einheit, anzahl):
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

def strom_deltas():
    """(zeitpunkt, kWh) — Ankerpunkte zaehlen nicht als Verbrauch."""
    con = strom.db()
    try:
        reihe = con.execute("SELECT ts, kwh, quelle FROM strom ORDER BY ts").fetchall()
    finally:
        con.close()
    diffs, vorher = [], None
    for ts, kwh, quelle in reihe:
        if vorher is not None and quelle != "anker":
            diff = kwh - vorher[1]
            if 0 <= diff <= 100:                    # Zaehlerspruenge verwerfen
                diffs.append((ts, diff))
        vorher = (ts, kwh)
    return diffs


def strom_zuwaechse():
    """(von, bis, kWh) — Zuwaechse mit ihrem Zeitintervall."""
    con = strom.db()
    try:
        reihe = con.execute("SELECT ts, kwh, quelle FROM strom ORDER BY ts").fetchall()
    finally:
        con.close()
    zuwaechse, vorher = [], None
    for ts, kwh, quelle in reihe:
        if vorher is not None and quelle != "anker":
            diff = kwh - vorher[1]
            if 0 <= diff <= 100:
                zuwaechse.append((vorher[0], ts, diff))
        vorher = (ts, kwh)
    return zuwaechse


def strom_serie(einheit, anzahl, tarif):
    """Netzbezug (kWh und EUR) je Periode."""
    pro_periode = {}
    for t0, t1, diff in strom_zuwaechse():
        for schluessel, anteil in perioden_anteile(t0, t1, diff, einheit).items():
            pro_periode[schluessel] = pro_periode.get(schluessel, 0.0) + anteil
    ap = float(tarif.get("arbeitspreis_ct_kwh") or 0)
    spalten = []
    for schluessel in periodenliste(einheit, anzahl):
        kwh = round(pro_periode.get(schluessel, 0.0), 3)
        std = EINHEITEN[einheit]["std_pro_strich"]
        spalten.append({
            "periode": schluessel,
            "kwh": kwh,
            "eur": round(kwh * ap / 100, 2),
            "gesamt_eur": round(kwh * ap / 100 + gas.grundkosten(std, tarif), 2),
        })
    return spalten


def strom_kennzahlen(tarif):
    jetzt = datetime.now()
    ap = float(tarif.get("arbeitspreis_ct_kwh") or 0)
    deltas = strom_deltas()
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
    ts_letzte, stand = strom.letzte()
    return {
        "stand_kwh": round(stand, 2) if stand is not None else None,
        "stand_text": (datetime.fromtimestamp(ts_letzte).strftime("%d.%m. %H:%M")
                       if ts_letzte else None),
        "alter_sekunden": max(0, int(time.time() - ts_letzte)) if ts_letzte else None,
        "tag_kwh": round(tag, 2), "tag_eur": round(tag * ap / 100, 2),
        "monat_kwh": round(monat, 2), "monat_eur": round(monat * ap / 100, 2),
        "jahr_kwh": round(jahr, 2), "jahr_eur": round(jahr * ap / 100, 2),
        "gesamt_kwh": round(gesamt, 2),
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
    diffs, vorher = [], None
    for ts, m3, quelle in reihe:
        if vorher is not None and quelle != "anker":
            diff = m3 - vorher[1]
            if 0 <= diff <= 20:                     # Zaehlerspruenge verwerfen
                diffs.append((ts, diff))
        vorher = (ts, m3)
    return diffs


def wasser_zuwaechse():
    """(von, bis, m3) — Zuwaechse mit ihrem Zeitintervall."""
    con = wasser.db()
    try:
        reihe = con.execute("SELECT ts, m3, quelle FROM wasser ORDER BY ts").fetchall()
    finally:
        con.close()
    zuwaechse, vorher = [], None
    for ts, m3, quelle in reihe:
        if vorher is not None and quelle != "anker":
            diff = m3 - vorher[1]
            if 0 <= diff <= 20:
                zuwaechse.append((vorher[0], ts, diff))
        vorher = (ts, m3)
    return zuwaechse


def wasser_serie(einheit, anzahl, tarif):
    """Wasserverbrauch (m3 und EUR) je Periode."""
    pro_periode = {}
    for t0, t1, diff in wasser_zuwaechse():
        for schluessel, anteil in perioden_anteile(t0, t1, diff, einheit).items():
            pro_periode[schluessel] = pro_periode.get(schluessel, 0.0) + anteil
    preis = (float(tarif.get("wasserpreis_eur_m3") or 0)
             + float(tarif.get("abwasser_eur_m3") or 0))
    spalten = []
    for schluessel in periodenliste(einheit, anzahl):
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
    pro_stunde = rate(86400) or rate(7 * 86400)
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
        "pro_tag_m3": round(pro_stunde * 24, 3),
        "pro_tag_eur": round(gas.kosten_energie(pro_stunde * 24, tarif)
                             + gas.grundkosten(24, tarif), 2),
        "pro_monat_m3": round(pro_stunde * 24 * 30.4375, 2),
        "pro_monat_eur": round(gas.kosten_energie(pro_stunde * 24 * 30.4375, tarif)
                               + gas.grundkosten(24 * 30.4375, tarif), 2),
        "pro_jahr_m3": round(pro_stunde * 24 * 365.25, 1),
        "pro_jahr_eur": round(gas.kosten_energie(pro_stunde * 24 * 365.25, tarif)
                              + gas.grundkosten(24 * 365.25, tarif), 2),
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
            return self._json({
                "einheit": einheit,
                "spalten": serie(einheit, anzahl, tarif),
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
                "wasser": {
                    "kennzahlen": wasser_kennzahlen((cfg.get("wasser") or {}).get("tarif") or {}),
                    "tarif": (cfg.get("wasser") or {}).get("tarif") or {},
                },
                "serverzeit": time.time(),
            })
        if url.path == "/api/wasser":
            einheit = (frage.get("einheit", ["tag"])[0]).lower()
            if einheit not in EINHEITEN:
                einheit = "tag"
            anzahl = int(frage.get("anzahl", [EINHEITEN[einheit]["anzahl"]])[0])
            tarif_w = (cfg.get("wasser") or {}).get("tarif") or {}
            return self._json({
                "einheit": einheit,
                "spalten": wasser_serie(einheit, anzahl, tarif_w),
                "kennzahlen": wasser_kennzahlen(tarif_w),
                "tarif": tarif_w,
            })
        if url.path == "/api/strom":
            einheit = (frage.get("einheit", ["tag"])[0]).lower()
            if einheit not in EINHEITEN:
                einheit = "tag"
            anzahl = int(frage.get("anzahl", [EINHEITEN[einheit]["anzahl"]])[0])
            tarif_s = (cfg.get("strom") or {}).get("tarif") or {}
            return self._json({
                "einheit": einheit,
                "spalten": strom_serie(einheit, anzahl, tarif_s),
                "kennzahlen": strom_kennzahlen(tarif_s),
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
