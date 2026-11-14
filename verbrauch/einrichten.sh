#!/bin/bash
# Richtet die automatische Messung ein (Cron: alle 5 Minuten) und prueft,
# ob alles laeuft. Mehrfach ausfuehrbar (ergaenzt den Cron-Eintrag nur einmal).
cd "$(dirname "$0")" || exit 1
set -u

echo "== Voraussetzungen =="
python3 -c "import numpy, PIL, scipy" 2>/dev/null \
    && echo "  Python-Pakete (numpy, Pillow, scipy): ok" \
    || echo "  FEHLT: python3-numpy python3-pil python3-scipy"

if [ ! -f config.json ]; then
    cp config.beispiel.json config.json
    echo "  config.json aus config.beispiel.json angelegt - bitte Kamera-URL anpassen."
fi

if [ -x "$HOME/.local/ocr/bin/tesseract" ]; then
    echo "  Schifterkennung: $("$HOME/.local/ocr/bin/tesseract" --version | head -1)"
else
    echo "  Hinweis: tesseract nicht gefunden (nur fuer die Gegenprobe noetig)."
    echo "  Nachinstalltion: ./mm/bin/micromamba create -y -p ~/.local/ocr -c conda-forge tesseract"
fi

echo
echo "== Cron-Eintrag =="
if crontab -l 2>/dev/null | grep -q "verbrauch/sample.py"; then
    echo "  vorhanden: */5 * * * * .../verbrauch/sample.py"
else
    { crontab -l 2>/dev/null
      echo "*/5 * * * * cd $PWD && /usr/bin/python3 sample.py >> logs/cron.log 2>&1"
    } | crontab -
    echo "  neu angelegt (Messung alle 5 Minuten)"
fi

echo
echo "== Viewer + Web-UI =="
../kamera.sh >/dev/null 2>&1 || true
./gaszaehler.sh

echo
echo "Naechste Schritte:"
echo "  1. Zaelerstand am Geraet ablesen und in der Web-UI unten bestaetigen"
echo "     (Feld 'Zaehlerstand korrigieren'), z. B. 8497,162"
echo "  2. In der Tarifkarte Arbeitspreis, Grundpreis, Brennwert und"
echo "     Zustandszahl aus der goldgas-Vertragsbestaetigung eintragen"
echo
echo "Protokoll: tail -f $PWD/logs/sample.log"
