#!/bin/bash
# Startet beide Kamera-Viewer (falls nicht schon aktiv) und oeffnet den Browser.
#   .43 (Utility-Raum, 180 Grad gedreht)  -> http://localhost:8099/
#   .44 (frontal auf den Zaehler, mit Zoom) -> http://localhost:8098/
# Kamera-IPs lassen sich per Umgebungsvariable setzen:
#   KAMERA_1=192.168.1.50 KAMERA_2=192.168.1.51 ./kamera.sh
cd "$(dirname "$0")" || exit 1

KAMERA_1=${KAMERA_1:-192.168.188.43}
KAMERA_2=${KAMERA_2:-192.168.188.44}

start() {  # $1=port  $2..=Argumente fuer viewer.py
    local port="$1"; shift
    if ! curl -s -o /dev/null -m 2 "http://localhost:$port/"; then
        nohup python3 viewer.py "$@" >> "viewer$port.log" 2>&1 &
        for _ in $(seq 1 20); do
            curl -s -o /dev/null -m 1 "http://localhost:$port/" && break
            sleep 0.3
        done
    fi
    xdg-open "http://localhost:$port/" >/dev/null 2>&1 &
}

start 8099 --camera "$KAMERA_1" --port 8099 --rotate 180 --zoom 110,250,680,175 --scale 2
# Wasserzaehler: NICHT dauerhaft starten - die Kamera soll nur laufen, wenn
# jemand hinschaut. Der Viewer wird bei Bedarf ueber die Weboberflaeche
# gestartet (/wasserbild) und beendet sich nach 10 Minuten ohne Abruf selbst.
# Zum manuellen Schaue:
#   python3 viewer.py --camera "$KAMERA_2" --port 8098 --rotate 270 \
#                     --zoom 160,725,175,75 --scale 8 --idle-exit 600

# Gasverbrauchsanzeige (Cron misst unabhaengig davon weiter)
verbrauch/gaszaehler.sh >/dev/null 2>&1 || true
