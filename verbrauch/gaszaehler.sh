#!/bin/bash
# Startet die Gasverbrauchsanzeige (falls nicht schon aktiv) und oeffnet den Browser.
#   http://localhost:8077/
# Die Messung selbst laeuft per Cron alle 5 Minuten (siehe crontab -l).
cd "$(dirname "$0")" || exit 1
PORT=8077

if ! curl -s -o /dev/null -m 2 "http://localhost:$PORT/"; then
    mkdir -p logs
    nohup python3 server.py --port $PORT >> logs/server.log 2>&1 &
    for _ in $(seq 1 25); do
        curl -s -o /dev/null -m 1 "http://localhost:$PORT/" && break
        sleep 0.3
    done
fi
xdg-open "http://localhost:$PORT/" >/dev/null 2>&1 &
echo "Gasverbrauch:  http://localhost:$PORT/"
