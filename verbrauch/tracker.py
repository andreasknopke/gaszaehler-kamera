#!/usr/bin/env python3
"""Verbrauchszaehlung ueber Ziffernwechsel der Zaehlerwalzen.

Idee: Die 8 Walzen (5x m3, 3x Nachkomma) stehen fest im Bild. Aendert sich das
Bild einer Walze, ist sie um eine Stelle weitergelaufen. Damit laesst sich der
Verbrauch exakt aufsummieren, ohne die Ziffern zu lesen - unabhaengig von der
Qualitaet der Schrifterkennung. OCR dient nur als Gegenprobe.

Ein Wechsel zaehlt erst, wenn er in zwei aufeinanderfolgenden Bildern bestaetigt
wird ('pending'). Damit fuehrt kein mitten beim Rollen aufgenommenes Bild und
kein Bildrumpeln zu einer Fehlbuchung.
"""

import time
from pathlib import Path

import numpy as np
from PIL import Image

BASE = Path(__file__).resolve().parent
ZELLEN_DIR = BASE / "zellen"
ZELLEN_DIR.mkdir(exist_ok=True)

# Zellgeometrie im gedrehten Bild: (x, y, halbe Breite, halbe Hoehe)
ZELLEN = [
    (201, 318, 34, 62), (264, 318, 34, 62), (327, 318, 34, 62),
    (391, 318, 34, 62), (457, 318, 34, 62),
    (537, 372, 40, 52), (610, 372, 40, 52), (700, 372, 40, 52),
]
# Stellenwert je Walze: 10^4 ... 10^-3 m3
# Nur bis zur ersten Nachkommastelle (0,1 m3). Die 0,01er dreht bei Verbrauch
# etwa alle 30 s weiter, die 0,001er praktisch dauernd - beides ist im
# Minutentakt nicht sauber les- oder zaehlbar. Gewicht 0 = wird ignoriert.
GEWICHTE = [10000.0, 1000.0, 100.0, 10.0, 1.0, 0.1, 0.0, 0.0]
GROESSE = (36, 56)
AEHNLICH = 0.96          # darunter: Walze ist klar weitergelaufen
                         # (gemessen: Ziffernwechsel 0,91-0,96 / gleiche
                         #  Ziffer 0,999 / Belichtung 0,97-0,985)
STABIL = 0.993           # darueber: wirklich unveraendert
ROLLT = 0.95             # darunter (Bild zu Bild): Walze steht mitten im Wechsel
MAX_GLEICHZEITIG = 3     # mehr gleichzeitige Wechsel = Kamera verrueckt


def zellen_aus(im):
    """8 normierte Zellbilder aus dem gedrehten Kamerabild schneiden."""
    out = []
    for cx, cy, hw, hh in ZELLEN:
        c = im.crop((cx - hw, cy - hh, cx + hw, cy + hh)).resize(GROESSE, Image.LANCZOS)
        v = np.asarray(c, dtype=np.float32)
        out.append((v - v.mean()) / (v.std() + 1e-6))
    return out


def _aehnlichkeit(a, b):
    if a.shape != b.shape:
        return -1.0
    return float((a * b).mean())


def _pfad(i):
    return ZELLEN_DIR / f"zelle{i}.npy"


def _pending_pfad(i):
    return ZELLEN_DIR / f"zelle{i}.pending.npy"


def lade(i):
    p = _pfad(i)
    return np.load(p) if p.exists() else None


def lade_pending(i):
    p = _pending_pfad(i)
    return np.load(p) if p.exists() else None


def speichere_zelle(i, arr):
    np.save(_pfad(i), arr)


def speichere_pending(i, arr):
    np.save(_pending_pfad(i), arr)


def loesche_pending(i):
    p = _pending_pfad(i)
    if p.exists():
        p.unlink()


def zuruecksetzen():
    """Referenzbilder entfernen (z. B. nach Korrektur des Basiswerts)."""
    for datei in list(ZELLEN_DIR.glob("zelle*.npy")) + list(ZELLEN_DIR.glob("*.pending.npy")):
        datei.unlink()


def basiswert(con):
    row = con.execute("SELECT wert FROM zustand WHERE schluessel='basis_m3'").fetchone()
    return float(row[0]) if row else None


def setze_basiswert(con, wert):
    con.execute("INSERT OR REPLACE INTO zustand VALUES ('basis_m3', ?)", (str(wert),))
    con.commit()


def notiz(con, text):
    con.execute("INSERT OR REPLACE INTO zustand VALUES ('letzte_notiz', ?)", (text,))
    con.commit()


def _lerne_wechsel(bank, zelle, schritte, bild):
    """Nach einer weitergelaufenen Walze: Label weiterdrehen und Vorlage sichern."""
    if bank is None:
        return
    labels = bank.labels()
    if labels[zelle] is None:
        ziffer, _, sicher = bank.klassifiziere(zelle, bild)
        if not sicher:
            return
        labels[zelle] = ziffer
        bank.setze_labels(labels)
        bank.lerne(zelle, ziffer, bild)
        return
    neu = (int(labels[zelle]) + int(schritte)) % 10
    labels[zelle] = neu
    bank.setze_labels(labels)
    bank.lerne(zelle, neu, bild)


def _schritte(bank, zelle, bild):
    """Anzahl der Walzenschritte aus der Ziffernerkennung (1..5).

    Walzen laufen nur vorwaerts, also ist die Differenz zum bekannten Label
    genau die Anzahl der Schritte. Ist die Ziffer noch nicht sicher lesbar,
    wird ein Schritt angenommen - bei einer Minute Messabstand der Normalfall.
    """
    if bank is None:
        return 1
    labels = bank.labels()
    if labels[zelle] is None:
        return 1
    ziffer, _, sicher = bank.klassifiziere(zelle, bild)
    if not sicher:
        return 1
    abstand = (int(ziffer) - int(labels[zelle])) % 10
    return abstand if 1 <= abstand <= 5 else 1


def schritt(zellen, con, jetzt=None, bank=None, zellen2=None, rollend=()):
    """Ein Abtastschritt. Rueckgabe: (wert, delta, details)

    zellen2: zweites Bild, wenige Sekunden nach dem ersten. Damit wird
    geprueft, ob eine Walze fertig gedreht hat oder noch rollt.
    rollend: Walzen, die sich von Bild zu Bild weiterbewegen (die feinen
    Nachkomma-Walzen). Sie werden weder gezaehlt noch als Referenz gespeichert.

    Wichtig: Die Referenz einer Walze wird *nur* bei einem gezaehlten Schritt
    oder bei einem erkannten Belichtungswechsel (mehrere Walzen gleichzeitig)
    ersetzt. Sonst wuerde eine langsam drehende Walze ihren eigenen Vergleich
    nachziehen - genau so gingen vorher Schritte verloren.
    """
    jetzt = jetzt or time.time()
    basis = basiswert(con)
    if basis is None:
        return None, 0.0, [{"hinweis": "kein Basiswert gesetzt"}]

    rollend = set(rollend)
    aenderungen = []        # (zelle, bild) - bestaetigte Schritte
    verdacht = []           # alle Zellen mit Veraenderung (auch rollende)

    # Lichtwechsel erkennen: die fuenf Hauptwalzen stehen praktisch immer
    # still. Aendern sich *mehrere* davon gleichzeitig, war es die
    # Beleuchtung (z. B. IR-Umschaltung) und nicht der Zaehler. Dann nur die
    # Referenzen nachziehen. (Eine einzelne Hauptwalze darf ruhig springen -
    # das ist ein echter 10/100/1000-m3-Schritt.)
    haupt_sims = [_aehnlichkeit(zellen[i], lade(i)) for i in range(5) if lade(i) is not None]
    if haupt_sims and sum(1 for s in haupt_sims if s < 0.99) >= 3:
        labels = bank.labels() if bank is not None else []
        for i in list(range(6)):
            neu = zellen2[i] if zellen2 is not None else zellen[i]
            speichere_zelle(i, neu)
            # Vorlage an das neue Licht anpassen. Die Ziffer hat sich dabei
            # nicht geaendert (nur die Helligkeit), deshalb wird die Vorlage
            # *ersetzt* statt eine weitere danebengelegt.
            if bank is not None and len(labels) > i and labels[i] is not None:
                if zellen2 is None or _aehnlichkeit(zellen[i], zellen2[i]) >= 0.995:
                    try:
                        bank.ersetze(i, labels[i], zellen[i])
                    except Exception:
                        pass
        notiz(con, f"{time.strftime('%H:%M')} Belichtungswechsel erkannt - "
                   f"Referenzbilder/Vorlagen nachgezogen, nichts gezaehlt")
        return basis, 0.0, [{"hinweis": "Belichtungswechsel"}]

    for i, bild in enumerate(zellen):
        if i in rollend or GEWICHTE[i] == 0:
            continue
        ref = lade(i)
        if ref is None:
            speichere_zelle(i, bild)
            continue

        sim = _aehnlichkeit(bild, ref)
        if sim >= 0.985:
            # Walze steht still. Hier wird *nichts* gelernt: das Bild unter
            # dem aktuellen Label abzulegen hat die Bank frueher vergiftet
            # (dasselbe Bild unter mehreren Ziffern). Gelernt wird nur bei
            # einem bestaetigten Schritt, wo die neue Ziffer feststeht.
            continue
        if sim >= AEHNLICH:
            # Unklarer Bereich (0,96 .. 0,985): weder buchen noch die
            # Referenz nachziehen. Die Walze dreht langsam weiter und wird
            # beim naechsten Bild klar als Schritt erkannt.
            continue

        verdacht.append(i)

        # Rollt die Walze noch? Dann in diesem Lauf nicht buchen.
        if zellen2 is not None:
            if _aehnlichkeit(bild, zellen2[i]) < STABIL:
                speichere_pending(i, zellen2[i])
                continue
        else:
            pending = lade_pending(i)
            if pending is None or _aehnlichkeit(bild, pending) < AEHNLICH:
                speichere_pending(i, bild)
                continue
            loesche_pending(i)

        aenderungen.append((i, bild))

    # Viele Walzen gleichzeitig veraendert = Kamera oder Licht, nicht der
    # Zaehler. Dann nur die Referenzen nachziehen und nichts buchen.
    if len(verdacht) > MAX_GLEICHZEITIG:
        for i in verdacht:
            speichere_zelle(i, (zellen2[i] if zellen2 is not None else zellen[i]))
            loesche_pending(i)
        notiz(con, f"{time.strftime('%H:%M')} - {len(verdacht)} Walzen gleichzeitig "
                   f"geaendert: vermutlich Kamera verstellt/Lichtwechsel, "
                   f"bitte Zählerstand prüfen")
        aenderungen = []

    wechsel = []
    for i, bild in aenderungen:
        anzahl = _schritte(bank, i, bild)
        wechsel.append({"zelle": i, "schritte": anzahl, "delta": GEWICHTE[i] * anzahl})
        speichere_zelle(i, bild)
        _lerne_wechsel(bank, i, anzahl, bild)

    delta = sum(w["delta"] for w in wechsel)
    if delta:
        setze_basiswert(con, basis + delta)     # Wert festschreiben
    return basis + delta, delta, wechsel
