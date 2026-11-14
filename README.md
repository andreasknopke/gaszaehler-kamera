# Gaszähler mit IP-Kamera

Zwei Bausteine, die zusammengehören:

**`viewer.py`** — ein kleiner lokaler Live-Viewer für Hi3510-Kameras. Er holt
den MJPEG-Strom der Kamera, dreht und schneidet das Bild und liefert einzelne
JPEGs über HTTP aus (Browser zeigen MJPEG nicht mehr zuverlässig in `<img>`).
Die Seite zeigt zusätzlich, wie alt das letzte Kamerabild ist — damit ist
„live" von „hängt" zu unterscheiden. Optional mit vergrößertem
Zähler-Ausschnitt.

**`verbrauch/`** — die eigentliche Zählerablesung: Die Software erkennt
weitergelaufene Ziffernräder, liest die Ziffern über eine selbst gelernte
Vorlagenbank und stellt Verbrauch und Kosten in einer Web-Ansicht dar
(stündlich, täglich, monatlich, jährlich). Details:
[verbrauch/README.md](verbrauch/README.md).

## Schnellstart

```bash
git clone https://github.com/andreasknopke/gaszaehler-kamera.git
cd gaszaehler-kamera/verbrauch
pip install -r requirements.txt          # numpy, Pillow, scipy
cp config.beispiel.json config.json     # Kamera-URL und Tarif anpassen
./einrichten.sh                         # prüft Umgebung, legt Cron an, startet UI
```

Danach:

* Web-Ansicht: <http://localhost:8077/>
* Live-Bild: <http://localhost:8099/>
* Einmalig den Zählerstand vom Display in der Web-Ansicht bestätigen
  („Zählerstand korrigieren") — das ist der Anker für alle weiteren Messungen.

## Idee

Klassische OCR scheitert an den unscharfen, kleinen Nachkomma-Walzen. Das
Projekt nutzt stattdessen die Mechanik des Zählers: Eine Walze läuft immer nur
eine Stelle weiter, und die neue Ziffer ist damit bekannt (alte + 1). So baut
sich die Vorlagenbank selbst auf. Gezählt wird nur, wenn ein zweites Bild
wenige Sekunden später bestätigt, dass die Walze fertig gedreht hat.

## Lizenz

MIT — siehe [LICENSE](LICENSE).
