#!/usr/bin/env python3
"""Wasserzaehler (Kamera .44) — Erfassung und Datenhaltung.

Der Wasserzaehler zeigt fuenf Vorkommastellen (z. B. 00007 m3); die kleinen
Zeigeruhren darunter sind zu klein zum Lesen. Weil der Zaehler nur sehr
langsam laeuft (eine Stelle etwa alle paar Tage), braucht es keine
Hochfrequenz: Es genuegt, den Stand alle halbe Stunde zu pruefen.

Gezaehlt wird bewusst nur die *Einerwalze*: Sie springt bei jedem Kubikmeter
genau eine Stelle weiter - auch beim Ueberlauf 9 -> 0. Die hoeheren Stellen
ergeben sich aus dem Stand und werden nur als Referenzbild nachgefuehrt.
Damit kann ein Uebertrag (00009 -> 00010) nicht doppelt gezaehlt werden.

Bildquelle ist der lokale Viewer (Port 8098), der das Bild bereits im
Sekundentakt vom Kamerastream holt. Das Bild liegt dort um 270 Grad gedreht
vor und wird hier in die Anzeige-Orientierung gebracht.
"""

import time
from pathlib import Path

import numpy as np
from PIL import Image

import gas

BASE = Path(__file__).resolve().parent
ZELLEN_DIR = BASE / "wasser_zellen"
ZELLEN_DIR.mkdir(exist_ok=True)

# Ziffernfenster im gedrehten Bild (720x1280): (x, y, halbe Breite, halbe Hoehe)
ZELLEN = [
    (178, 764, 12, 14), (205, 763, 12, 14), (231, 762, 12, 14),
    (259, 760, 12, 14), (284, 758, 12, 14),
]
EINER = 4                      # Index der Einerwalze (letzte Ziffer)
GROESSE = (32, 40)

AEHNLICH = 0.90                # darunter: Ziffer hat sich geaendert
STABIL = 0.97                  # darueber: gleiche Ziffer (nur minimal anders)

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
    im = gas._bild_aus(w.get("url") or "http://localhost:8098/frame.jpg",
                       int(w.get("drehen", 0)))
    return im


def zellen_aus(im):
    """Fuenf normierte Zellbilder der Vorkommastellen."""
    out = []
    for cx, cy, hw, hh in ZELLEN:
        c = im.crop((cx - hw, cy - hh, cx + hw, cy + hh)).resize(GROESSE, Image.LANCZOS)
        v = np.asarray(c, dtype=np.float32)
        out.append((v - v.mean()) / (v.std() + 1e-6))
    return out


def _aehnlichkeit(a, b):
    return float((a * b).mean()) if a.shape == b.shape else -1.0


# -------------------------------------------------------- Referenzen/Zaehlung

def _pfad(i):
    return ZELLEN_DIR / f"zelle{i}.npy"


def lade(i):
    p = _pfad(i)
    return np.load(p) if p.exists() else None


def speichere(i, arr):
    np.save(_pfad(i), arr)


def zuruecksetzen():
    for datei in ZELLEN_DIR.glob("zelle*.npy"):
        datei.unlink()


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


# -------------------------------------------------------------------- Messung

def schritt(zellen, con):
    """Ein Abtastschritt. Rueckgabe: (wert, delta, details)."""
    basis = basiswert(con)
    if basis is None:
        return None, 0.0, [{"hinweis": "kein Basiswert gesetzt"}]

    delta, details = 0.0, []
    for i, bild in enumerate(zellen):
        ref = lade(i)
        if ref is None:
            speichere(i, bild)
            continue
        sim = _aehnlichkeit(bild, ref)
        if sim >= STABIL:
            speichere(i, bild)                  # gleiche Ziffer, langsam mitziehen
            continue
        if sim >= AEHNLICH:
            # Unklarer Bereich: weder zaehlen noch die Referenz ersetzen.
            # Die Walze dreht langsam weiter und wird beim naechsten Lauf
            # klar als Wechsel erkannt.
            continue
        # Walze hat sich geaendert: nur die Einerwalze zaehlt als +1 m3.
        if i == EINER:
            delta += 1.0
            details.append({"zelle": i, "delta": 1.0, "aehnlichkeit": round(sim, 3)})
        else:
            details.append({"zelle": i, "delta": 0.0, "aehnlichkeit": round(sim, 3),
                            "hinweis": "Uebertrag - zaehlt ueber die Einerwalze"})
        speichere(i, bild)

    if delta:
        setze_basiswert(con, basis + delta)
    return basis + delta, delta, details


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


def anker_setzen(wert, ts=None, quelle="anker"):
    """Startpunkt setzen und Referenzbilder neu aufnehmen."""
    con = db()
    try:
        setze_basiswert(con, wert)
        speichere_messung(int(ts or time.time()), wert, quelle, ablesung_text(wert))
    finally:
        con.close()
    zuruecksetzen()
    for i, z in enumerate(zellen_aus(bild())):
        speichere(i, z)
    return wert


def ablesung_text(wert):
    """7.0 -> '00007' (so viele Vorkommastellen wie der Zaehler zeigt)."""
    return f"{int(round(float(wert))):05d}"


def sammeln(cfg=None, erzwingen=False):
    """Einmal messen. Rueckgabe: (wert, delta, details, neu)."""
    con = db()
    try:
        if basiswert(con) is None:
            raise RuntimeError("kein Basiswert - erst mit --anker setzen")
        zellen = zellen_aus(bild(cfg))
        wert, delta, details = schritt(zellen, con)
        ts_letzte, m3_letzte = letzte()
        jetzt = int(time.time())
        neu = (erzwingen or delta > 0 or m3_letzte is None
               or abs(m3_letzte - wert) > 1e-6
               or jetzt - (ts_letzte or 0) > 3600)
        if neu:
            speichere_messung(jetzt, wert, "zaehlung", ablesung_text(wert))
        return wert, delta, details, neu
    finally:
        con.close()
