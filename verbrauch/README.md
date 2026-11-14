# Gaszähler ablesen mit einer IP-Kamera

Kleinautomatik für einen Balgengaszähler (BK‑G4): Eine Hi3510‑IP‑Kamera
schaut auf das Ziffernwerk, die Software erkennt weitergelaufene Ziffernräder,
liest die Ziffern selbst und schreibt den Zählerstand in eine SQLite‑Datenbank.
Dazu gibt es eine Web‑Ansicht mit Verbrauch und Kosten — stündlich, täglich,
monatlich, jährlich — inklusive Tarifrechnung.

Läuft auf einem gewöhnlichen Linux‑Rechner im Dauerbetrieb, ohne root, ohne
Cloud. Die Messung startet per Cron, die Auswertung läuft lokal im Browser.

## Aufruf

| | |
|---|---|
| **Web‑Ansicht** | <http://localhost:8077/> |
| Start/Öffnen | `./gaszaehler.sh` |
| Einrichtung prüfen | `./einrichten.sh` |
| Messprotokoll | `tail -f logs/sample.log` |
| Kamerabild (Live) | <http://localhost:8099/> (Viewer, 180° gedreht) |

## Voraussetzungen

* Python 3 mit `numpy`, `Pillow`, `scipy`
* eine Kamera, die ein Einzelbild oder einen MJPEG‑Strom liefert (hier
  Hi3510 unter `/cgi-bin/hi3510/snap.cgi`)
* optional `tesseract` für die OCR‑Gegenprobe
* Cron (oder systemd‑Timer) für den regelmäßigen Messlauf

`./einrichten.sh` prüft die Umgebung, legt den Cron‑Eintrag an und startet
Viewer und Web‑Ansicht.

## So funktioniert die Erkennung

1. **Bild holen** – direkt vom Kamerastream (`snap.cgi?&-getstream&-chn=11`),
   ersatzweise über den mitgelieferten Viewer. Der Stream ist ein *endloser*
   MJPEG‑Strom; es wird deshalb in Blöcken gelesen, bis ein vollständiges
   JPEG vorliegt (ein einzelnes `read(N)` läuft sonst in den Timeout und
   liefert ein beliebiges Bild aus der Mitte).
2. **Zählerfenster finden** – die dunklen Rechtecke (m³‑Block und
   Nachkommablock) werden per Zusammenhangsanalyse im Bild gesucht.
3. **Begradigen** – die Kamera schaut schräg auf den Zähler; die Zeile wird
   über eine ausgeglichene Gerade gedreht.
4. **Ziffernräder zählen** (`tracker.py`) – jede der acht Walzen hat ein
   Referenzbild. Weicht das aktuelle Bild deutlich ab, ist die Walze
   weitergelaufen und wird dem Stellenwert entsprechend gebucht. Bestätigt
   wird ein Schritt mit einem **zweiten Bild wenige Sekunden später**: nur
   wenn die Walze dort stillsteht, war sie fertig gedreht.
5. **Ziffern lesen** (`ziffern.py`) – jede Walze hat eine eigene Lernbank.
   Bei einem bestätigten Schritt weiß das System die neue Ziffer
   (alte + 1) und legt das Bild als Vorlage ab; so entstehen alle zehn
   Ziffern je Walze von selbst. Gelesen wird per Ähnlichkeitsvergleich
   (Nearest Neighbor) mit Mindestabstand zur zweitbesten Ziffer.
6. **Ziffernablesung per OCR** (`ablesen_ocr.py`) – RapidOCR (ONNX) liest
   jede Walze **einzeln** und mit hoher Zuverlässigkeit; Tesseract scheitert
   auf diesen weichen Ziffern (getestet: 1 von 5). Der entscheidende Trick:
   die Ziffer wird fünfmal nebeneinander an den Erkenner gegeben, sonst wird
   ein einzelnes Zeichen als Textzeile verworfen. Das läuft bei jedem Messlauf
   (rund 1 s) und **korrigiert die Zählung** – auch dann, wenn die Lernbank
   sich sicher ist, denn genau dort lagen die Fehler.
7. **Gegenprobe** – Ablesung und Zählung werden gegeneinander geprüft.
   Weicht die gelesene Ziffernfolge um mindestens eine Auflösungsstufe von
   der Zählung ab, gewinnt die Ablesung (z. B. wenn ein Schritt durchgerutscht
   ist). Zusätzlich liest Tesseract etwa stündlich den m³‑Block und
   protokolliert das Ergebnis.

Der absolute Zählerstand kommt aus dem **Basiswert**. Deshalb einmalig
wichtig: den Wert vom Display im Feld *Zählerstand korrigieren* eintragen.
Damit wird auch die Lernbank auf die richtigen Ziffern gesetzt, und ab dann
liest das System die Stellen selbst weiter.

## Auflösung und Grenzen

* **Auflösung 0,1 m³.** Ausgelesen werden die fünf Vorkomma‑Stellen und die
  erste Nachkommastelle. Die 0,01er‑Walze dreht bei Verbrauch etwa alle 30 s
  weiter, die 0,001er praktisch dauernd — im 5‑Minuten‑Takt ist beides nicht
  sauber les‑ oder zählbar. Beide werden nicht geführt und in der Anzeige als
  gestrichelte Stellen dargestellt. Für Stunde, Tag, Monat und Preis reicht
  0,1 m³ (≈ 1 kWh) gut aus.
* **Lichtwechsel.** Nachts schaltet die Kamera auf Infrarot, tagsüber zurück.
  Ändern sich dabei mehrere Hauptwalzen gleichzeitig, gilt das als
  Beleuchtungswechsel: Die Referenzbilder und die Vorlagen der betroffenen
  Ziffern werden ersetzt, es wird nichts gezählt.
* **Schräge und Unschärfe.** Für den Bildvergleich ist das unkritisch, für
  eine echte OCR ist das Bild zu weich — die Tesseract‑Gegenprobe ist
  deshalb nur ein Plausibilitätscheck.
* **Ein Anker pro Stelle.** Der Startwert muss einmal vom Display abgelesen
  werden; danach läuft alles automatisch. Wird die Kamera versetzt, ist ein
  neuer Anker nötig.
* **Korrektur auf ganze m³.** Sind die Nachkommastellen im Bild nicht sicher
  lesbar, korrigiert die Ablesung immer noch die Vorkommastellen (Auflösung
  1 m³). Ein verpasster 0,1-Schritt kann sich dadurch nicht dauerhaft
  festsetzen.

## Verzeichnis

```
gas.py                  Bild holen, Zählerfenster, OCR, Tarif, SQLite
tracker.py              Ziffernräder vergleichen und zählen
ziffern.py              Lernbank: Ziffern lesen und lernen
sample.py               ein Messlauf (per Cron alle 5 Minuten)
lernen.py               Serienbilder aufnehmen und Ziffern nachlernen
server.py               Web-UI + JSON-API (Port 8077)
ablesen_ocr.py          Ziffernablesung per RapidOCR (ONNX), einzeln je Walze
mqtt.py                 minimaler MQTT-Client (ohne Zusatzpaket)
mqtt_bruecke.py         Messwerte an Home Assistant (MQTT-Discovery)
startwert_ermitteln.py  Basiswert setzen / Walzen neu anlernen
einrichten.sh           Cron-Eintrag anlegen, Umgebung prüfen
gaszaehler.sh           Web-UI starten und Browser öffnen
config.beispiel.json    Vorlage für die eigene config.json
static/                 HTML, CSS, JavaScript der Web-Ansicht
```

Nicht im Repo (werden zur Laufzeit angelegt): `config.json`, `daten.db`,
`fotos/`, `zellen/`, `bank/`, `logs/`.

## Tarif

`config.json` → `tarif` (oder direkt in der Web‑Ansicht editieren):

| Feld | Bedeutung | Wo im Vertrag |
|---|---|---|
| `arbeitspreis_ct_kwh` | Arbeitspreis, brutto | „Preis pro Kilowattstunde“ |
| `grundpreis_eur_monat` | Grundpreis, brutto | „Grundpreis pro Monat“ |
| `brennwert_kwh_m3` | Brennwert | Abrechnung „Brennwert“ (meist 9,5–10,6) |
| `zustandszahl` | Zustandszahl | Abrechnung „Zustandszahl“ (meist 0,9–1,0) |

Umrechnung: `kWh = m³ × Brennwert × Zustandszahl`,
`Kosten = kWh × Arbeitspreis + Grundpreis (zeitanteilig)`.

Solange die Platzhalter aktiv sind, zeigt die Seite ein orangefarbenes
Warnbanner.

## Home Assistant

Zwei Wege — beide ohne Cloud, beide lokal.

### A) MQTT-Discovery (empfohlen: erscheint als Gerät)

Voraussetzung: ein MQTT-Broker, z. B. das Mosquitto-Add-on in Home Assistant.
In `config.json` eintragen:

```json
"mqtt": {
  "aktiv": true,
  "host": "192.168.188.10",
  "port": 1883,
  "benutzer": "mqtt",
  "passwort": "…",
  "basis": "gaszaehler",
  "entdeckung": "homeassistant"
}
```

Beim nächsten Messlauf (oder sofort mit `python3 mqtt_bruecke.py`) meldet sich
das Gerät **Gaszähler** an, mit diesen Entitäten:

| Entität | Einheit | Bedeutung |
|---|---|---|
| Zählerstand | m³ | Gesamtstand, `total_increasing` (fürs Energie-Dashboard) |
| Verbrauch heute / 7 / 30 / 365 Tage | m³ | rollierende Zeiträume |
| Leistung (24 h) | m³/h | mittlerer Verbrauch |
| Kosten heute / 30 / 365 Tage | € | inkl. anteiligem Grundpreis |
| Ablesung laut Bild | – | gelesene Ziffern, z. B. `08503,7--` |
| Letzte Messung, Bildalter | – / s | Diagnose |

Bleiben 15 Minuten neue Werte aus, markiert Home Assistant die Entitäten als
nicht verfügbar (`expire_after`). Der MQTT-Client ist selbst geschrieben
(`mqtt.py`, rund 100 Zeilen) — es wird kein Zusatzpaket gebraucht.

Prüfen ohne Broker: `python3 mqtt_bruecke.py --zeigen`

### B) REST-Sensor (ohne Broker)

`/api/homeassistant` liefert alle Werte als flaches JSON. In
`configuration.yaml`:

```yaml
rest:
  - resource: http://192.168.188.10:8077/api/homeassistant
    scan_interval: 300
    sensor:
      - name: "Gaszähler Stand"
        value_template: "{{ value_json.zaehlerstand_m3 }}"
        unit_of_measurement: "m³"
        device_class: gas
        state_class: total_increasing
      - name: "Gasverbrauch heute"
        value_template: "{{ value_json.verbrauch_heute_m3 }}"
        unit_of_measurement: "m³"
        device_class: gas
        state_class: measurement
      - name: "Gaskosten heute"
        value_template: "{{ value_json.kosten_heute_eur }}"
        unit_of_measurement: "EUR"
        device_class: monetary
```

Der REST-Weg erzeugt Entitäten, aber kein zusammenhängendes Gerät — dafür
braucht es MQTT-Discovery (A) oder eine eigene Integration.

## Häufige Fälle

* **`Zählerfenster nicht gefunden`** – Kamera hat sich verstellt oder das
  Bild ist zu dunkel. Livebild unter <http://localhost:8099/> prüfen,
  Ausrichtung korrigieren, dann `python3 startwert_ermitteln.py --wert 8503,6`.
* **Wert weicht vom Display ab** – im Feld *Zählerstand korrigieren* den
  angezeigten Displaywert eintragen. Referenzbilder und Lernbank werden dabei
  neu gesetzt.
* **Ablesung zeigt Striche** – die Ziffernvorlagen passen gerade nicht
  (Lichtwechsel, Kamera versetzt). Die Zählung läuft weiter; ein neuer
  Basiswert setzt die Bank zurück.
* **Cron läuft nicht** – `crontab -l` muss enthalten:
  `*/2 * * * * cd .../verbrauch && /usr/bin/python3 sample.py >> logs/cron.log 2>&1`
  (Messung alle 5 Minuten – nötig, weil die 0,1‑m³‑Walze bei Verbrauch in
  unter 10 Minuten weiterspringt)

## Stand

Das ist ein privates Bastelprojekt, das seit November 2026 an einem
Gasanschluss läuft. Es ist bewusst klein gehalten: keine Cloud, kein
Frontend‑Framework, keine Abhängigkeit von einer Online‑OCR.
