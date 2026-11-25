#!/usr/bin/env python3
"""Kamera-Waechter: merkt, wenn der Gas-Stream steht, und greift ein.

Hintergrund: Die Kamera (Firmware 2015) kann ihren Encoder aufhaengen -
sie antwortet dann noch auf Ping und Web-UI, liefert im Stream aber nur
Multipart-Rahmen ohne Bilddaten (Nullbytes). Der Viewer zeigt weiter das
letzte gute Bild, die Ablesung steht. Ein Neustart der Kamera hilft.

Der Waechter laeuft alle 5 Minuten (Cron) und unterscheidet:

* Bild frisch            -> nichts tun
* Viewer antwortet nicht -> Viewer neu starten
* Bild steht, Kamera an  -> Kamera neu starten (Encoder haengt)
* Kamera aus/weg         -> nur melden, ein Neustart ist nicht moeglich

Eingriffe werden begrenzt (30 Minuten Pause, nach drei erfolglosen
Versuchen 6 Stunden), damit eine wirklich defekte Kamera nicht in einer
Neustart-Schleife landet.

Aufruf:
    kamera_waechter.py            # pruefen und ggf. eingreifen
    kamera_waechter.py --status   # nur berichten
    kamera_waechter.py --trocken  # Eingriffe nur anzeigen
"""

import argparse
import http.client
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASIS = Path(__file__).resolve().parent
LOG = BASIS / "logs" / "waechter.log"
SPERRE = BASIS / "logs" / "waechter.lock"
DB = BASIS / "daten.db"
VIEWER = BASIS.parent / "viewer.py"

SCHWELLE = 900          # so lange darf das Bild hoechstens stehen (Sekunden)
PAUSE = 1800            # Mindestabstand zwischen zwei Kamera-Neustarts
MAX_VERSUCHE = 3        # danach laengere Pause
LANGE_PAUSE = 6 * 3600
MELDE_PAUSE = 1800      # "Kamera nicht erreichbar" hoechstens so oft melden


# ------------------------------------------------------------------ Hilfen

def log(*teile):
    LOG.parent.mkdir(exist_ok=True)
    zeile = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(t) for t in teile)
    print(zeile)
    try:
        if LOG.exists() and LOG.stat().st_size > 2_000_000:
            LOG.replace(LOG.with_suffix(".log.1"))
        with LOG.open("a", encoding="utf-8") as f:
            f.write(zeile + "\n")
    except OSError:
        pass


def hole(con, schluessel):
    zeile = con.execute("SELECT wert FROM zustand WHERE schluessel = ?",
                        (schluessel,)).fetchone()
    if not zeile or zeile[0] in (None, ""):
        return None
    try:
        return float(zeile[0])
    except (TypeError, ValueError):
        return zeile[0]


def setze(con, schluessel, wert):
    con.execute("INSERT OR REPLACE INTO zustand (schluessel, wert) VALUES (?, ?)",
                (schluessel, wert))
    con.commit()


def notiz(text):
    """Kurze Meldung in der Web-UI anzeigen."""
    try:
        con = sqlite3.connect(DB, timeout=10)
        try:
            con.execute("INSERT OR REPLACE INTO zustand VALUES ('letzte_notiz', ?)",
                        (text,))
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:
        pass


def bild_alter(cfg):
    try:
        import gas
        return gas.bild_alter(cfg)
    except Exception as exc:
        log("Bildalter nicht lesbar:", repr(exc))
        return None


def kamera_adresse(cfg):
    """(Host, Port) aus der Direkt-URL."""
    url = (cfg.get("kamera") or {}).get("url_direkt") or ""
    teile = url.split("/")
    if len(teile) < 3 or not teile[2]:
        return None, None
    host, _, port = teile[2].partition(":")
    try:
        return host, int(port or 80)
    except ValueError:
        return host, 80


def reboot_url(cfg):
    eigen = (cfg.get("kamera") or {}).get("reboot_url")
    if eigen:
        return eigen
    url = (cfg.get("kamera") or {}).get("url_direkt") or ""
    teile = url.split("/")
    basis = "/".join(teile[:3]) if len(teile) >= 3 else ""
    return basis + "/cgi-bin/hi3510/param.cgi?cmd=reboot"


def kamera_erreichbar(cfg, versuche=2, timeout=3):
    """Laeuft die Kamera ueberhaupt (Strom, Netz)?

    Das Web-Interface laesst einzelne Anfragen haengen, waehrend der Stream
    weiterlaeuft; ein HTTP-Timeout wuerde die Kamera faelschlich als aus
    betrachten. Deshalb TCP (mehrfach) und ersatzweise Ping.
    """
    host, port = kamera_adresse(cfg)
    if not host:
        return False
    for _ in range(versuche):
        try:
            with socket.create_connection((host, port), timeout):
                return True
        except OSError:
            time.sleep(1)
    try:
        return subprocess.run(["ping", "-c", "1", "-W", "2", host],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def viewer_port(cfg):
    v = (cfg.get("kamera") or {}).get("viewer") or {}
    if v.get("port"):
        return int(v["port"])
    url = (cfg.get("kamera") or {}).get("url_ansicht") or ""
    teile = url.split("/")
    if len(teile) >= 3:
        _, _, port = teile[2].partition(":")
        try:
            return int(port or 80)
        except ValueError:
            pass
    return None


def port_offen(port, timeout=2):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout) as s:
            s.close()
        return True
    except OSError:
        return False


def viewer_pids(port):
    """PIDs des Viewers, der auf diesem Port hoert."""
    pids = []
    for eintrag in Path("/proc").iterdir():
        if not eintrag.name.isdigit():
            continue
        try:
            teile = (eintrag / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if not any(b"viewer.py" in t for t in teile):
            continue
        if b"--port" not in teile:
            continue
        i = teile.index(b"--port")
        if i + 1 < len(teile) and teile[i + 1].decode(errors="ignore") == str(port):
            pids.append(int(eintrag.name))
    return pids


def viewer_starten(cfg, trocken=False):
    """Viewer (neu) starten - Parameter aus kamera.viewer in config.json."""
    v = (cfg.get("kamera") or {}).get("viewer")
    if not v:
        log("Viewer-Konfiguration fehlt (kamera.viewer in config.json)")
        return False
    port = int(v.get("port", 8099))
    for pid in viewer_pids(port):
        log(f"Viewer PID {pid} beenden")
        if not trocken:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    if trocken:
        log("(trocken) Viewer wuerde gestartet")
        return True
    time.sleep(1)
    befehl = [sys.executable, str(VIEWER), "--camera", str(v.get("kamera", "")),
              "--port", str(port), "--rotate", str(v.get("drehen", 0))]
    if v.get("zoom"):
        befehl += ["--zoom", str(v["zoom"])]
    if v.get("scale"):
        befehl += ["--scale", str(v["scale"])]
    ziel = (BASIS / "logs" / f"viewer-{port}.log").open("ab")
    subprocess.Popen(befehl, cwd=str(VIEWER.parent), stdout=ziel, stderr=ziel,
                     start_new_session=True)
    log("Viewer gestartet:", " ".join(befehl))
    return True


def kamera_neustart(cfg, trocken=False):
    url = reboot_url(cfg)
    log("Kamera-Neustart:", url)
    if trocken:
        log("(trocken) kein Neustart ausgeloest")
        return True
    try:
        # Die Kamera antwortet auf diesen Aufruf nicht - sie startet
        # stattdessen nach wenigen Sekunden neu (Timeout ist normal).
        urllib.request.urlopen(url, timeout=5).read(64)
    except (urllib.error.URLError, ConnectionError, OSError, http.client.HTTPException):
        pass                                         # Verbindungsabbruch ist normal
    except Exception as exc:
        log("Neustart-Aufruf:", repr(exc))
    return True


def melde_unerreichbar(con, jetzt, trocken=False):
    letzte = float(hole(con, "waechter_meldung_ts") or 0)
    if jetzt - letzte < MELDE_PAUSE:
        return
    if not trocken:
        setze(con, "waechter_meldung_ts", "%.0f" % jetzt)
    log("Kamera antwortet nicht (Netz/Strom) - kein Neustart moeglich")
    if not trocken:
        notiz(f"{time.strftime('%H:%M')} Gaskamera antwortet nicht "
              f"(Netz/Strom prüfen)")


# -------------------------------------------------------------------- Ablauf

def pruefen(cfg, trocken=False):
    alter = bild_alter(cfg)
    con = sqlite3.connect(DB, timeout=15)
    try:
        versuche = int(hole(con, "waechter_versuche") or 0)
        letzter = float(hole(con, "waechter_letzter_versuch") or 0)
        jetzt = time.time()

        # 1) Alles gut
        if alter is not None and alter <= SCHWELLE:
            if versuche:
                setze(con, "waechter_versuche", "0")
                nachricht = (f"{time.strftime('%H:%M')} Kamerastream wieder da "
                             f"(Bild {alter:.0f} s alt)")
                log(nachricht, f"nach {versuche} Eingriff(en)")
                notiz(nachricht)
            return 0

        # 2) Der Viewer antwortet nicht
        if alter is None:
            port = viewer_port(cfg)
            if port and not port_offen(port):
                log("Viewer laeuft nicht")
            elif kamera_erreichbar(cfg):
                log("Viewer antwortet nicht, Kamera schon")
            else:
                melde_unerreichbar(con, jetzt, trocken)
                return 0
            if viewer_starten(cfg, trocken):
                if not trocken:
                    notiz(f"{time.strftime('%H:%M')} Bild-Viewer der Gaskamera "
                          f"neu gestartet")
            return 0

        # 3) Bild steht - ist die Kamera ueberhaupt da?
        if not kamera_erreichbar(cfg):
            melde_unerreichbar(con, jetzt, trocken)
            return 0

        # 4) Kamera ist da, liefert aber kein Bild -> Encoder haengt
        pause = PAUSE if versuche < MAX_VERSUCHE else LANGE_PAUSE
        if letzter and jetzt - letzter < pause:
            log(f"Bild steht seit {alter / 60:.0f} min - letzter Neustart vor "
                f"{(jetzt - letzter) / 60:.0f} min, warte "
                f"{(pause - (jetzt - letzter)) / 60:.0f} min")
            return 0

        log(f"Bild steht seit {alter / 60:.0f} min, Kamera antwortet "
            f"- Neustart (Versuch {versuche + 1})")
        kamera_neustart(cfg, trocken)
        if not trocken:
            setze(con, "waechter_versuche", str(versuche + 1))
            setze(con, "waechter_letzter_versuch", "%.0f" % jetzt)
            notiz(f"{time.strftime('%H:%M')} Kamerastream stand "
                  f"{alter / 60:.0f} min - Kamera neu gestartet")
        return 0
    finally:
        con.close()


def main():
    p = argparse.ArgumentParser(description="Ueberwacht den Kamera-Stream")
    p.add_argument("--status", action="store_true",
                   help="nur Zustand berichten, nichts tun")
    p.add_argument("--trocken", action="store_true",
                   help="Eingriffe nur anzeigen")
    args = p.parse_args()

    (BASIS / "logs").mkdir(exist_ok=True)
    with SPERRE.open("w") as lock:
        try:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0

        sys.path.insert(0, str(BASIS))
        import gas
        cfg = gas.load_config()

        if args.status:
            alter = bild_alter(cfg)
            print("Bildalter:", "unbekannt (Viewer antwortet nicht)" if alter is None
                  else f"{alter:.0f} s")
            print("Kamera erreichbar:", "ja" if kamera_erreichbar(cfg) else "nein")
            con = sqlite3.connect(DB, timeout=15)
            try:
                versuche = hole(con, "waechter_versuche")
                letzter = hole(con, "waechter_letzter_versuch")
            finally:
                con.close()
            print("Eingriffe seit letzter Erholung:", int(versuche or 0))
            if letzter:
                print("Letzter Eingriff:",
                      datetime.fromtimestamp(float(letzter)).isoformat(timespec="seconds"))
            return 0

        return pruefen(cfg, trocken=args.trocken)


if __name__ == "__main__":
    raise SystemExit(main())
