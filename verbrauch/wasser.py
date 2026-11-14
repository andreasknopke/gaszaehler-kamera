#!/usr/bin/env python3
"""Wasserzaehler (Kamera .44) — Ablesung und Datenhaltung.

Der Zaehler zeigt fuenf Vorkommastellen (00007 m3); die kleinen Zeigeruhren
darunter sind zu klein. Gelesen wird per RapidOCR (wasser_lesen.py, laeuft im
eigenen venv), nicht per Bildvergleich:

Ein Bildvergleich zaehlt zwar zuverlaessig, wenn sich eine Walze dreht - aber
er zaehlt auch dann, wenn sich nur die Kamera minimal verschiebt oder das
Licht wechselt. Bei einem Zaehler, der sich nur alle paar Tage aendert, waere
ein solcher Fehlzaehler monatelang unbemerkt. Deshalb wird der Stand direkt
gelesen und gegen den letzten geprueft:

* nicht kleiner als der letzte Stand
* hoechstens ein paar Kubikmeter mehr (der Zaehler laeuft langsam)

Passt die Ablesung nicht, bleibt der alte Wert stehen und es wird ein Hinweis
gesetzt - geraten wird nicht.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import gas

BASE = Path(__file__).resolve().parent

SCHEMA = """
CREATE TABLE IF NOT EXISTS wasser (
    ts        INTEGER PRIMARY KEY,
    m3        REAL NOT NULL,
    quelle    TEXT NOT NULL,
    ablesung  TEXT,
    konfidenz REAL
);
CREATE TABLE IF NOT EXISTS wasser_zustand (
    schluessel TEXT PRIMARY KEY,
    wert       TEXT
);
"""

MAX_ANSTIEG = 5.0          # m3 seit der letzten Messung (sehr grosszuegig)


def db():
    con = gas.db()
    con.executescript(SCHEMA)
    con.commit()
    return con


# ----------------------------------------------------------------------- Bild

def bild(cfg=None):
    """Kamerabild in Anzeige-Orientierung (Ziffern waagerecht)."""
    cfg = cfg or gas.load_config()
    w = cfg.get("wasser") or {}
    return gas._bild_aus(w.get("url") or "http://localhost:8098/frame.jpg",
                         0)


def _zustand_lesen(con, schluessel, vorgabe=None):
    zeile = con.execute("SELECT wert FROM wasser_zustand WHERE schluessel=?",
                        (schluessel,)).fetchone()
    return zeile[0] if zeile else vorgabe


def _zustand_schreiben(con, schluessel, wert):
    con.execute("INSERT OR REPLACE INTO wasser_zustand VALUES (?, ?)",
                (schluessel, str(wert)))
    con.commit()


def basiswert(con):
    wert = _zustand_lesen(con, "basis_m3")
    return float(wert) if wert is not None else None


def setze_basiswert(con, wert):
    _zustand_schreiben(con, "basis_m3", f"{float(wert):.3f}")


def notiz(con, text):
    _zustand_schreiben(con, "letzte_notiz", text)


def ablesung_text(wert):
    """7.0 -> '00007' (so viele Vorkommastellen wie der Zaehler zeigt)."""
    return f"{int(round(float(wert))):05d}"


# -------------------------------------------------------------------- OCR

def ocr_lesen(cfg, pfad):
    """RapidOCR im venv aufrufen -> (ziffern, scores) oder None."""
    o = cfg.get("ocr") or {}
    python = Path(__import__("os").path.expanduser(
        o.get("python", "~/.local/ocrenv/bin/python")))
    if not python.exists():
        return None
    try:
        lauf = subprocess.run([str(python), str(BASE / "wasser_lesen.py"),
                               "--bild", str(pfad), "--json"],
                              capture_output=True, text=True, timeout=90)
        zeilen = [z for z in lauf.stdout.strip().splitlines() if z.startswith("{")]
        if not zeilen:
            return None
        daten = json.loads(zeilen[-1])
    except Exception:
        return None
    ziffern = daten.get("ziffern") or []
    if len(ziffern) != 5:
        return None
    return ziffern, daten.get("scores") or []


def stand_lesen(cfg=None, bisher=None):
    """Bild holen, Ziffern lesen, mit dem bisherigen Stand ergaenzen.

    -> (wert, ziffern, scores, hinweis)
    """
    cfg = cfg or gas.load_config()
    bild_pfad = Path("/tmp/wasser_ablesung.jpg")
    bild(cfg).save(bild_pfad, quality=92)
    ergebnis = ocr_lesen(cfg, bild_pfad)
    if not ergebnis:
        return None, [None] * 5, [], "OCR nicht verfuegbar"
    ziffern, scores = ergebnis

    # Nur sichere Ziffern uebernehmen. Die fuehrenden Stellen liegen oft
    # unter Reflexen auf dem Glas; sie aendern sich jahrelang nicht und
    # werden aus dem bisherigen Stand ergaenzt.
    if bisher is not None:
        alt = [int(c) for c in ablesung_text(bisher)]
    else:
        alt = [0, 0, 0, 0, 0]
    gefuellt, uebernommen = [], []
    for a, z, s in zip(alt, ziffern, scores or [0] * 5):
        if z is not None and s >= 0.50:
            gefuellt.append(z)
        else:
            gefuellt.append(a)
            uebernommen.append(z)
    wert = float("".join(str(z) for z in gefuellt))
    hinweis = ""
    if uebernommen:
        hinweis = (f"{len(uebernommen)} unsichere Stelle(n) aus dem letzten "
                   f"Stand uebernommen")
    return wert, ziffern, scores, hinweis


# -------------------------------------------------------------------- Messung

def letzte():
    con = db()
    try:
        zeile = con.execute("SELECT ts, m3 FROM wasser ORDER BY ts DESC LIMIT 1").fetchone()
        return (zeile[0], zeile[1]) if zeile else (None, None)
    finally:
        con.close()


def speichere_messung(ts, m3, quelle, ablesung=None, konfidenz=None):
    con = db()
    try:
        con.execute("INSERT OR REPLACE INTO wasser (ts, m3, quelle, ablesung, konfidenz)"
                    " VALUES (?,?,?,?,?)",
                    (int(ts), round(float(m3), 3), quelle, ablesung, konfidenz))
        con.commit()
    finally:
        con.close()


def sammeln(cfg=None, erzwingen=False):
    """Einmal lesen und pruefen. -> (wert, delta, hinweis, neu)"""
    cfg = cfg or gas.load_config()
    con = db()
    try:
        basis = basiswert(con)
        ts_letzte, m3_letzte = letzte()
        bisher = m3_letzte if m3_letzte is not None else basis
        wert, ziffern, scores, hinweis = stand_lesen(cfg, bisher)

        if wert is None:
            notiz(con, f"{time.strftime('%H:%M')} Wasser: {hinweis}")
            return (bisher or 0.0), 0.0, hinweis, False

        schnitt = (sum(s for s in scores if s) / max(1, sum(1 for s in scores if s)))
        delta = wert - (bisher if bisher is not None else wert)
        if bisher is not None and (delta < 0 or delta > MAX_ANSTIEG):
            notiz(con, f"{time.strftime('%H:%M')} Wasser-Ablesung "
                       f"{ablesung_text(wert)} passt nicht zum letzten Stand "
                       f"{ablesung_text(bisher)} - bitte pruefen")
            return bisher, 0.0, "Ablesung unplausibel", False

        jetzt = int(time.time())
        neu = (erzwingen or delta != 0 or m3_letzte is None
               or jetzt - (ts_letzte or 0) > 3600)
        if neu:
            speichere_messung(jetzt, wert, "ocr", ablesung_text(wert),
                              round(schnitt, 3))
            setze_basiswert(con, wert)
        return wert, delta, hinweis, neu
    finally:
        con.close()


def anker_setzen(wert, ts=None, quelle="anker"):
    """Startpunkt setzen (z. B. vom Display abgelesen)."""
    con = db()
    try:
        setze_basiswert(con, wert)
        speichere_messung(int(ts or time.time()), wert, quelle, ablesung_text(wert), 1.0)
    finally:
        con.close()
    return wert
