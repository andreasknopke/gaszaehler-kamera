#!/usr/bin/env python3
"""Ziffernerkennung fuer die Zahlerraeder.

Warum keine fertige OCR?  Tesseract kann mit den unscharfen, gedrehten
Nahaufnahmen der kleinen Nachkomma-Walzen nicht zuverlaessig umgehen (liest
z. B. 504 statt 169).  Stattdessen lernt das System die Ziffern selbst:

* Eine Walze zeigt immer nur eine von 10 Ziffern und laeuft immer nur eine
  Stelle weiter.  Wechselt das Bild einer Walze, ist die neue Ziffer also
  zwingend (alte Ziffer + 1) mod 10.  Damit entstehen die Vorlagen von
  selbst, ganz ohne von Hand beschriftete Bilder.
* Einmal pro Kalibrierung wird der Basiswert vom Display uebernommen; damit
  kennen alle acht Walzen ihren Startwert.
* Danach kann jede Walze unabhaengig gelesen werden.  Zaehlt das Zahlwerk
  einen Schritt, der der Ablesung widerspricht, wird das protokolliert.

Speicher: bank/zelle<i>.npz  (Vorlagen, Schlussel = Ziffer)
          bank/labels.json   (aktuelle Ziffer je Walze)
"""

import json
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
BANK_DIR = BASE / "bank"
LABELS = BANK_DIR / "labels.json"

GEWICHTE = [10000.0, 1000.0, 100.0, 10.0, 1.0, 0.1, 0.01, 0.001]

MAX_VORLAGEN = 8          # Vorlagen je Ziffer und Walze
MIN_ABSTAND = 0.04        # Abstand zur zweitbesten Ziffer, damit "sicher"
SICHERHEIT_MIN = 0.93     # Aehnlichkeit selbst fuer dieselbe Ziffer

# Die beiden feinen Walzen (0,01 und 0,001) drehen bei Verbrauch zu schnell
# (eine Ziffer pro ~30 s bzw. wenige Sekunden). Sie werden weder gelernt noch
# gelesen - jede Vorlage waere nur eine Mischung zweier Ziffern.
NICHT_LERNEN = {6, 7}

def _ahnlichkeit(a, b):
    return float((a * b).mean())


class Ziffernbank:
    def __init__(self, ordner=BANK_DIR):
        self.ordner = Path(ordner)
        self.ordner.mkdir(parents=True, exist_ok=True)
        self.vorlagen = {}                       # zelle -> {ziffer: [arrays]}
        self._laden()

    # ------------------------------------------------------------------ laden
    def _laden(self):
        for datei in sorted(self.ordner.glob("zelle*.npz")):
            try:
                zelle = int(datei.stem[5:])
            except ValueError:
                continue
            mit = np.load(datei)
            self.vorlagen[zelle] = {int(k): [mit[k][i] for i in range(len(mit[k]))]
                                    for k in mit.files}

    def _sichere(self, zelle):
        daten = self.vorlagen.get(zelle, {})
        if not daten:
            return
        self.ordner.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            self.ordner / f"zelle{zelle}.npz",
            **{str(z): np.stack(liste) for z, liste in sorted(daten.items())})

    # ------------------------------------------------------------------ lernen
    def lerne(self, zelle, ziffer, bild):
        """Vorlage hinzufuegen, wenn sie sich von den bisherigen unterscheidet."""
        ziffer = int(ziffer) % 10
        regal = self.vorlagen.setdefault(zelle, {})
        liste = regal.setdefault(ziffer, [])
        for alt in liste:
            if _ahnlichkeit(bild, alt) > 0.985:
                return False                                  # Duplikat
        liste.append(bild.astype(np.float32))
        if len(liste) > MAX_VORLAGEN:
            liste.pop(0)
        self._sichere(zelle)
        return True

    def setze_basis(self, zellen, wert, zellen2=None):
        """Bank neu aufbauen und Walzen mit den Ziffern des Basiswerts starten.

        zellen2: Bild wenige Sekunden spaeter. Walzen, die sich dazwischen
        bewegen, bekommen zwar ihr Label, aber *keine* Vorlage - ihr Bild
        waere nur eine Mischung zweier Ziffern.
        """
        self.vorlagen = {}
        for datei in self.ordner.glob("zelle*.npz"):
            datei.unlink()
        ziffern = ziffern_aus_wert(wert)
        for i, (bild, ziffer) in enumerate(zip(zellen, ziffern)):
            if i in NICHT_LERNEN:
                ziffern[i] = None
                continue
            if zellen2 is not None and _ahnlichkeit(bild, zellen2[i]) < 0.995:
                continue                      # dreht gerade: kein Vorbild
            self.lerne(i, ziffer, bild)
        self.setze_labels(ziffern)
        return ziffern

    # ------------------------------------------------------------------ labels
    def labels(self):
        if LABELS.exists():
            try:
                return [None if x is None else int(x)
                        for x in json.loads(LABELS.read_text())]
            except (ValueError, OSError):
                pass
        return [None] * 8

    def setze_labels(self, labels):
        self.ordner.mkdir(parents=True, exist_ok=True)
        LABELS.write_text(json.dumps([int(x) if x is not None else None for x in labels]))

    def ersetze(self, zelle, ziffer, bild):
        """Vorlage einer Ziffer ersetzen (z. B. nach einem Lichtwechsel)."""
        zelle, ziffer = int(zelle), int(ziffer) % 10
        self.vorlagen.setdefault(zelle, {})[ziffer] = [bild.astype(np.float32)]
        self._sichere(zelle)
        return True

    def vergiss(self, zellen):
        """Vorlagen und Label fuer Walzen loeschen (z. B. weil sie gerade drehen)."""
        for i in zellen:
            self.vorlagen.pop(int(i), None)
            datei = self.ordner / f"zelle{int(i)}.npz"
            if datei.exists():
                datei.unlink()
        labels = self.labels()
        for i in zellen:
            labels[int(i)] = None
        self.setze_labels(labels)

    def lernstand(self):
        return {i: sorted(self.vorlagen.get(i, {})) for i in range(8)}

    def vollstaendig(self):
        return all(len(self.vorlagen.get(i, {})) >= 10 for i in range(8))

    # ---------------------------------------------------------------- erkennen
    def klassifiziere(self, zelle, bild):
        """-> (ziffer or None, sicherheit, sicher)

        Solange eine Walze erst wenige Ziffern kennt, ist der Abstand zur
        zweitbesten Ziffer wenig aussagekraeftig. Deshalb wird die Messlatte
        mit wachsendem Lernstand strenger bzw. lockerer gesetzt.
        """
        regal = self.vorlagen.get(zelle)
        if not regal:
            return None, 0.0, False
        scores = []
        for ziffer, liste in regal.items():
            scores.append((max(_ahnlichkeit(bild, v) for v in liste), ziffer))
        scores.sort(reverse=True)
        beste, ziffer = scores[0]
        zweit = scores[1][0] if len(scores) > 1 else -1.0
        anzahl = len(regal)
        if anzahl >= 6:
            sicher = beste >= 0.93 and (beste - zweit) >= 0.04
        elif anzahl >= 3:
            sicher = beste >= 0.96 and (beste - zweit) >= 0.04
        else:
            # Bei ein bis zwei Vorlagen laesst sich eine Ziffer nicht
            # identifizieren - nur "dieselbe Ziffer" erkennen. Gemessen:
            # gleiche Ziffer unter anderem Licht 0,97-1,0 / fremde Ziffer 0,956.
            sicher = beste >= 0.975
        return (ziffer if sicher else None), round(beste, 3), sicher

    def lies(self, zellen, rollend=()):
        """Alle acht Walzen lesen -> (ziffern, wert, details, komplett, aufloesung)

        Unbekannt darf nur ein *Endstueck* sein (die feinen Stellen unten).
        Fehlt eine Stelle in der Mitte, ist keine Aussage moeglich, sonst
        wuerde aus 0849?,27 faelschlich 0849,27.
        """
        rollend = set(rollend)
        ziffern, details = [], []
        for i, bild in enumerate(zellen):
            if i in rollend:
                ziffern.append(None)
                details.append({"zelle": i, "ziffer": None, "ahnlichkeit": 0.0,
                                "rollt": True})
                continue
            ziffer, sicherheit, sicher = self.klassifiziere(i, bild)
            ziffern.append(ziffer)
            details.append({"zelle": i, "ziffer": ziffer, "ahnlichkeit": sicherheit,
                            "rollt": False})

        erste = next((i for i, z in enumerate(ziffern) if z is None), None)
        if erste is None:
            wert = sum(z * g for z, g in zip(ziffern, GEWICHTE))
            return ziffern, wert, details, True, GEWICHTE[-1]
        if any(z is not None for z in ziffern[erste:]):
            return ziffern, None, details, False, 0.0     # Luecke mitten drin
        wert = sum(z * g for z, g in zip(ziffern[:erste], GEWICHTE))
        # Der unbekannte Rest deckt 10 Stellen der ersten offenen Walze ab,
        # z. B. "08498,2--" -> [8498,20 ; 8498,30)
        return ziffern, wert, details, False, 10 * GEWICHTE[erste]


def ziffern_aus_wert(wert):
    """8498.162 -> [0, 8, 4, 9, 8, 1, 6, 2]"""
    wert = round(float(wert), 3)
    ganzzahlen = int(wert)
    nachkomma = int(round((wert - ganzzahlen) * 1000))
    return [int(c) for c in f"{ganzzahlen:05d}"] + [int(c) for c in f"{nachkomma:03d}"]


def text(ziffern):
    """[0,8,4,9,8,2,None] -> '08498,27-' (unbekannt als Strich)"""
    if not ziffern:
        return "-"
    haupt = "".join("-" if z is None else str(z) for z in ziffern[:5])
    nach = "".join("-" if z is None else str(z) for z in ziffern[5:])
    return f"{haupt},{nach}"
