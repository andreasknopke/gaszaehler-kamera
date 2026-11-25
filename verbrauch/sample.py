#!/usr/bin/env python3
"""Cron-Lauf: ein Messpunkt.

Reihenfolge:
  1. Bild von der Kamera holen
  2. acht Walzenbilder ausschneiden
  3. Zahlwerk: Walzenwechsel zaehlen (und dabei Ziffern vorlagen lernen)
  4. mit der Lernbank alle acht Ziffern lesen -> echte Ablesung inkl. Nachkomma
  5. Ablesung gegen die Zaehlung pruefen, plausiblen Wert speichern
  6. stuendlich Foto ins Archiv, stündlich Gegenprobe per Tesseract
"""

import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gas
import tracker
import ziffern

BASIS = Path(__file__).resolve().parent
SPERRE = BASIS / ".sample.lock"
LOG = BASIS / "logs" / "sample.log"
FOTO_DIR = BASIS / "fotos"


def log(*teile):
    LOG.parent.mkdir(exist_ok=True)
    zeile = datetime.now().strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(t) for t in teile)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(zeile + "\n")


def _zustand(con, schluessel, vorgabe=None):
    zeile = con.execute("SELECT wert FROM zustand WHERE schluessel=?",
                        (schluessel,)).fetchone()
    return zeile[0] if zeile else vorgabe


def _zustand_setzen(con, schluessel, wert):
    con.execute("INSERT OR REPLACE INTO zustand VALUES (?, ?)",
                (schluessel, str(wert)))
    con.commit()


def durchfluss(con, fenster_minuten=60):
    """Mittlerer Verbrauch der letzten Stunde in m3/h (None ohne Daten)."""
    grenze = int(time.time() - fenster_minuten * 60)
    reihe = con.execute("SELECT ts, m3 FROM messungen WHERE ts >= ? ORDER BY ts",
                        (grenze,)).fetchall()
    if len(reihe) < 2:
        return None
    (t0, m0), (t1, m1) = reihe[0], reihe[-1]
    if t1 <= t0 or m1 < m0:
        return None
    return max(0.0, min(6.0, (m1 - m0) / ((t1 - t0) / 3600.0)))


MAX_M3_PRO_MINUTE = 0.12                             # physikalische Obergrenze Gas


def letzte_aenderung(con):
    """Zeitpunkt, seit dem der gespeicherte Wert unveraendert ist."""
    reihe = con.execute("SELECT ts, m3 FROM messungen ORDER BY ts DESC LIMIT 3000"
                        ).fetchall()
    if not reihe:
        return None
    wert = float(reihe[0][1])
    for ts, m3 in reihe[1:]:
        if abs(float(m3) - wert) > 1e-9:
            return ts
    return reihe[-1][0]


def foto_noetig():
    fotos = sorted(FOTO_DIR.glob("zaehler-*.jpg"))
    return time.time() - (fotos[-1].stat().st_mtime if fotos else 0) > 3600


def haupt_ocr():
    """Besten OCR-Text der Hauptziffern (Gegenprobe, alle 15 Minuten)."""
    try:
        kh, _, _, _ = gas.lese_zaehler(gas.hole_bild())
        return kh[0][0] if kh else ""
    except Exception as exc:
        log("OCR-Fehler:", repr(exc))
        return ""


def ablesung_aus_wert(wert):
    """Ziffernfolge zum gespeicherten Wert.

    So viele Nachkommastellen wie der Wert wirklich hat, der Rest als '-'.
    8505.0 -> 08505,0--   8505.04 -> 08505,04-   8505.042 -> 08505,042
    """
    wert = round(float(wert), 3)
    ziffern_liste = ziffern.ziffern_aus_wert(wert)
    text = f"{wert:.3f}".rstrip("0")
    stellen = len(text.split(".")[1]) if "." in text else 0
    gewichte = [10000, 1000, 100, 10, 1, 0.1, 0.01, 0.001]
    for i, g in enumerate(gewichte):
        if 5 + stellen <= i:
            ziffern_liste[i] = None
    return ziffern.text(ziffern_liste)


def ocr_ablesung(cfg, erwartet=None):
    """RapidOCR als Rueckfallebene, wenn die Lernbank unsicher ist.

    Laeuft im eigenen venv (onnxruntime liegt nicht im System-Python).
    Rueckgabe: (ziffern, wert, aufloesung) oder None.

    `erwartet` ist der gezaehlte Wert. Er dient nur dazu, eine einzelne
    offene Stelle zu ergaenzen (siehe unten) - nie dazu, das Ergebnis zu
    ersetzen.
    """
    o = cfg.get("ocr") or {}
    if not o.get("aktiv"):
        return None
    python = Path(os.path.expanduser(o.get("python", "~/.local/ocrenv/bin/python")))
    if not python.exists():
        log("OCR: Interpreter fehlt:", python)
        return None
    try:
        lauf = subprocess.run([str(python), str(BASIS / "ablesen_ocr.py"), "--json"],
                              capture_output=True, text=True, timeout=90)
        zeilen = [z for z in lauf.stdout.strip().splitlines() if z.startswith("{")]
        if not zeilen:
            log("OCR: keine Ausgabe:", (lauf.stderr or "").strip()[:200])
            return None
        daten = json.loads(zeilen[-1])
    except Exception as exc:
        log("OCR-Fehler:", repr(exc))
        return None

    liste = daten.get("ziffern") or []
    if len(liste) != 8:
        log("OCR verworfen (nicht 8 Stellen):", liste)
        return None
    gewichte = ziffern.GEWICHTE
    erste = next((i for i, z in enumerate(liste) if z is None), None)
    if erste is None:
        return liste, sum(z * g for z, g in zip(liste, gewichte)), gewichte[-1]
    if any(z is not None for z in liste[erste:]):
        # Luecke mitten drin. Die Zehntelwalze steht bei laufendem Verbrauch
        # regelmaessig zwischen zwei Ziffern und wird dann nicht erkannt.
        # Ist nur diese eine Stelle offen und die Hundertstelstelle gelesen,
        # laesst sie sich aus der Zaehlung ergaenzen - die feineren Stellen
        # kommen weiterhin aus dem Bild.
        if (erwartet is not None and erste == 5
                and all(z is not None for z in liste[:5])
                and liste[6] is not None):
            versuch = list(liste)
            versuch[5] = int(erwartet / gewichte[5]) % 10
            rest = next((i for i in range(6, len(versuch)) if versuch[i] is None),
                        len(versuch))
            if all(z is None for z in versuch[rest:]):
                wert = sum(z * g for z, g in zip(versuch[:rest], gewichte))
                if abs(wert - erwartet) <= 0.5:
                    aufloesung = (10 * gewichte[rest] if rest < len(versuch)
                                  else gewichte[-1])
                    return versuch, wert, aufloesung
        log("OCR verworfen (Luecke mitten drin):", liste)
        return None                                    # Luecke mitten drin
    return (liste,
            sum(z * g for z, g in zip(liste[:erste], gewichte)),
            10 * gewichte[erste])


def main():
    LOG.parent.mkdir(exist_ok=True)
    FOTO_DIR.mkdir(exist_ok=True)

    with SPERRE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0                                    # vorherige Messung laeuft noch

        cfg = gas.load_config()

        # Steht das Kamerabild? Dann keine Messung speichern - sonst entstehen
        # hunderte gleiche Werte und der Zaehler wirkt stehengeblieben.
        alter = gas.bild_alter(cfg)
        if alter is not None and alter > 180:
            log(f"Kamerabild steht seit {alter / 60:.0f} min - keine Messung")
            return 0

        con = gas.db()
        try:
            if tracker.basiswert(con) is None:
                start = float(cfg["erfassung"]["startwert_m3"])
                tracker.setze_basiswert(con, start)
                gas.speichere_messung(time.time(), start, "start", 1.0, "Basiswert")

            letzte = gas.letzte_messung()
            minuten = max(1.0, (time.time() - letzte[0]) / 60.0)

            im = gas.hole_bild(cfg)
            zellen = tracker.zellen_aus(im)
            bank = ziffern.Ziffernbank()

            # Zweites Bild kurz danach: damit laesst sich unterscheiden, ob
            # eine Walze fertig gedreht hat oder mitten im Wechsel steht.
            time.sleep(1.5)
            zellen2 = tracker.zellen_aus(gas.hole_bild(cfg))

            # --- 3) Walzen erkennen, die sich dauerhaft weiterdrehen
            rollend = {i for i in range(8)
                       if tracker._aehnlichkeit(zellen[i], zellen2[i]) < tracker.ROLLT}

            # --- 4) Zahlwerk: Wechsel zaehlen, dabei Vorlagen lernen
            wert, delta, wechsel = tracker.schritt(zellen, con, bank=bank,
                                                   zellen2=zellen2, rollend=rollend)
            if wert is None:
                log("kein Basiswert")
                return 1

            # --- 5) Ziffern lesen (rollende Walzen bleiben offen)
            gez, gelesen, details, komplett, aufloesung = bank.lies(zellen, rollend)

            # Unabhaengige OCR (RapidOCR) alle paar Minuten. Sie hat Vorrang
            # vor der Lernbank: Die Bank kann sich irren (falsch gelernte
            # Ziffer), die OCR liest das Bild direkt.
            stempel = BASIS / "logs" / ".ocr_zuletzt"
            letzte_ocr = stempel.stat().st_mtime if stempel.exists() else 0
            intervall = float((cfg.get("ocr") or {}).get("intervall_sekunden", 300))
            if time.time() - letzte_ocr > intervall:
                ocr = ocr_ablesung(cfg, wert)
                if ocr and ocr[1] is not None and ocr[2] <= 0.1:
                    gez, gelesen, aufloesung = ocr
                    komplett = aufloesung <= 0.0015
                    stempel.write_text(str(int(time.time())))
                    log(f"OCR-Ablesung: {ziffern.text(gez)} "
                        f"(Aufloesung {aufloesung:g} m3)")

            # Labels nachziehen, damit die +1-Zaehlung weiterläuft
            labels = bank.labels()
            if any(l is None for l in labels):
                for i, (l, z) in enumerate(zip(labels, gez)):
                    if l is None and z is not None:
                        labels[i] = z
                bank.setze_labels(labels)

            ablesung = ziffern.text(gez)
            quelle = "zahlwerk"

            # --- 6) Ablesung gegen Zaehlung pruefen
            # Die Ablesung ist abgeschnitten, sobald feine Walzen rollen:
            # "08504,---" heisst 8504,0 <= Wert < 8505,0 (Aufloesung 1 m3).
            # Auch dann muss die Ablesung die Zaehlung korrigieren duerfen -
            # sonst bleibt ein verpasster 0,1-Schritt fuer immer stehen.
            # Ist die Ablesung stabil? Eine Fehlablesung ist fluechtig, eine
            # echte Korrektur (z. B. ein falsch gezaehlter Schritt) bleibt.
            vorher_ocr = _zustand(con, "letzte_ocr")
            stabil = (vorher_ocr is not None
                      and abs(gelesen - float(vorher_ocr)) <= 0.08
                      if gelesen is not None else False)
            if gelesen is not None:
                _zustand_setzen(con, "letzte_ocr", gelesen)

            # Erwarteter Zuwachs seit der letzten Messung. Der aktuelle
            # Durchfluss ist bekannt - ein Sprung um eine ganze
            # Vorkommastelle ist damit unplausibel, solange der Stream
            # nicht laenger ausgefallen ist (dann waechst die Toleranz mit
            # der verstrichenen Zeit).
            rate = durchfluss(con) or 0.0
            erwartet = rate * minuten / 60.0
            toleranz = min(2.0, max(0.25, 3.0 * erwartet))
            unten = max(1.0, toleranz)

            # Nach einem Ausfall (Bild stand, Stream weg) liegt die Ablesung
            # weiter vorne, als es die kurze Zeitspanne erlaubt. Erlaubt ist,
            # was der Zaehler seit der letzten *Aenderung* hoechstens gedreht
            # haben kann - und nur, wenn die Ablesung bestaetigt ist.
            ts_aend = letzte_aenderung(con)
            stunden = (time.time() - ts_aend) / 3600.0 if ts_aend else 0.0
            nachhol_max = min(30.0, MAX_M3_PRO_MINUTE * stunden * 60.0 + 0.25)

            if komplett:
                if (gelesen < wert - 0.02 or gelesen > wert + toleranz) and not stabil:
                    # Ein Gaszaehler laeuft nicht rueckwaerts: eine einmalige
                    # kleinere Ablesung ist eine Fehlablesung und wird
                    # verworfen. Meldet die OCR denselben Wert zweimal, ist es
                    # eine echte Korrektur (falsch gezaehlter Schritt).
                    tracker.notiz(con, f"{time.strftime('%H:%M')} Ablesung "
                                       f"{ablesung} passt nicht zur Erwartung "
                                       f"({wert:.3f} + max. {toleranz:.2f} m3 in "
                                       f"{minuten:.0f} min) - verworfen")
                    log(f"Ablesung verworfen: {ablesung} vs {wert:.3f} "
                        f"(Toleranz +{toleranz:.2f} m3, Durchfluss {rate:.2f} m3/h)")
                else:
                    if abs(gelesen - wert) > 0.0015:
                        tracker.setze_basiswert(con, gelesen)
                        tracker.notiz(con, f"{time.strftime('%H:%M')} Zahlwerk sagte "
                                           f"{wert:.3f}, Ablesung {gelesen:.3f} uebernommen")
                        log(f"Zaehlung korrigiert: {wert:.3f} -> {gelesen:.3f}")
                    wert = gelesen
                    quelle = "ablesung"
            elif gelesen is not None and aufloesung <= 1.0:
                abweichung = wert - gelesen
                nachhol = (stabil and abweichung < 0
                           and -abweichung <= nachhol_max)
                if -0.0005 <= abweichung < aufloesung + 0.0005:
                    quelle = "ablesung"            # Ablesung bestaetigt die Zaehlung
                elif nachhol or (-toleranz <= abweichung <= unten and (
                        abweichung <= 0.02 or stabil)):
                    # Kleine Abweichung oder bestaetigte Nachholung: auf die
                    # gelesene Stelle einrasten.
                    neu = round(gelesen + min(max(abweichung, 0.0), aufloesung - 0.001), 3)
                    tracker.setze_basiswert(con, neu)
                    tracker.notiz(con, f"{time.strftime('%H:%M')} Zahlwerk {wert:.3f} "
                                       f"passt nicht zur Ablesung {ablesung}; "
                                       f"auf {neu:.3f} korrigiert")
                    if nachhol:
                        log(f"Nachholung nach Ausfall: {wert:.3f} -> {neu:.3f} "
                            f"({ablesung}, Wert stand {stunden:.1f} h still)")
                    else:
                        log(f"Zaehlung korrigiert: {wert:.3f} -> {neu:.3f} ({ablesung})")
                    wert = neu
                    quelle = "ablesung"
                else:
                    tracker.notiz(con, f"{time.strftime('%H:%M')} Ablesung {ablesung} "
                                       f"weicht stark von der Zählung {wert:.3f} ab - "
                                       f"bitte Zählerstand prüfen")
                    log(f"Ablesung springt: {ablesung} vs {wert:.3f} - nicht uebernommen")

            # --- 6) Gegenprobe per Tesseract (etwa stuendlich) - nur protokollieren.
            # Auf diesem Bild ist sie zu grob fuer eine Warnung (verwechselt
            # z. B. 4 und 6); der Text bleibt fuer die Fehlersuche in 'roh'.
            roh = json.dumps({"zellen": details, "wechsel": wechsel}, ensure_ascii=False)
            if int(time.time()) % 3600 < 600:
                haupt = haupt_ocr()
                if haupt:
                    roh = f"OCR {haupt} | " + roh

            # Die Anzeige wird immer aus dem gespeicherten Wert gebildet -
            # so kann sie ihm nie widersprechen (vorher stand dort nach einem
            # gescheiterten OCR-Lauf noch die alte Bank-Ablesung).
            ablesung = ablesung_aus_wert(wert)

            gas.speichere_messung(time.time(), wert, quelle,
                                  0.95 if quelle == "ablesung" else 0.7, roh, ablesung)
            log(f"{wert:.3f} ({quelle}) abgelesen={ablesung}",
                f"wechsel={wechsel}" if wechsel else "")

            # Home Assistant (MQTT-Discovery) - darf die Messung nie stoeren
            if (cfg.get("mqtt") or {}).get("aktiv"):
                try:
                    import mqtt_bruecke
                    mqtt_bruecke.sende(cfg)
                except Exception as exc:
                    log("MQTT-Fehler:", repr(exc))

            if foto_noetig():
                stempel = datetime.now().strftime("%Y%m%d-%H%M")
                im.save(FOTO_DIR / f"zaehler-{stempel}.jpg", quality=85)
                im.crop((60, 200, 790, 470)).save(
                    FOTO_DIR / f"zaehler-detail-{stempel}.jpg", quality=92)
                log("Foto archiviert", stempel)

            aufheben_bis = time.time() - float(cfg["erfassung"].get("foto_archiv_tage", 3)) * 86400
            for datei in FOTO_DIR.glob("zaehler-*.jpg"):
                if datei.stat().st_mtime < aufheben_bis:
                    datei.unlink()
        except Exception as exc:
            log("FEHLER:", repr(exc))
            return 1
        finally:
            con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
