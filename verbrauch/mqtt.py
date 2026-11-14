#!/usr/bin/env python3
"""Minimaler MQTT-Client — nur so viel, wie fuer Home-Assistant-Discovery noetig ist.

Bewusst ohne externe Abhaengigkeit: Verbinden (CONNECT), Veroeffentlichen
(PUBLISH, QoS 0, optional retained) und Lesen einer retained Nachricht
(SUBSCRIBE + PUBLISH, QoS 0). Keine Wiederholungen, kein QoS-Fluss.
Fuer regelmaessig gesendete Messwerte reicht das: Faellt der Broker aus, wird
beim naechsten Messlauf einfach erneut gesendet.
"""

import socket
import struct
import time


def _varint(zahl):
    """MQTT-Remaining-Length (7 Bit pro Byte)."""
    teile = b""
    while True:
        byte = zahl % 128
        zahl //= 128
        if zahl:
            teile += bytes([byte | 0x80])
        else:
            return teile + bytes([byte])


def _text(wert):
    roh = wert.encode("utf-8")
    return struct.pack("!H", len(roh)) + roh


class MqttClient:
    def __init__(self, host, port=1883, benutzer=None, passwort=None,
                 client_id="gaszaehler", timeout=8):
        self.host = host
        self.port = int(port)
        self.benutzer = benutzer or None
        self.passwort = passwort or None
        self.client_id = client_id
        self.timeout = timeout
        self.sock = None

    # ------------------------------------------------------------------ Aufbau
    def verbinden(self):
        self.sock = socket.create_connection((self.host, self.port), self.timeout)
        flags = 0x02                                   # clean session
        if self.benutzer:
            flags |= 0x80
        if self.passwort:
            flags |= 0x40

        nutzlast = _text("MQTT") + bytes([4, flags]) + struct.pack("!H", 60)
        nutzlast += _text(self.client_id)
        if self.benutzer:
            nutzlast += _text(self.benutzer)
        if self.passwort:
            nutzlast += _text(self.passwort)
        self.sock.sendall(bytes([0x10]) + _varint(len(nutzlast)) + nutzlast)

        antwort = b""
        while len(antwort) < 4:
            teil = self.sock.recv(4 - len(antwort))
            if not teil:
                raise ConnectionError("Broker hat die Verbindung geschlossen")
            antwort += teil
        if antwort[0] != 0x20:
            raise RuntimeError(f"kein CONNACK (0x{antwort[0]:02x})")
        if antwort[3] != 0:
            codes = {1: "falsche Protokollversion", 2: "Client-ID abgelehnt",
                     3: "Server nicht verfuegbar", 4: "Benutzer/Passwort falsch",
                     5: "nicht autorisiert"}
            raise RuntimeError("MQTT verweigert: "
                               + codes.get(antwort[3], f"Code {antwort[3]}"))
        return self

    # ----------------------------------------------------------- Veroeffentlichen
    def publish(self, thema, inhalt, behalten=True):
        daten = inhalt.encode("utf-8") if isinstance(inhalt, str) else inhalt
        nutzlast = _text(thema) + daten
        kopf = 0x30 | (0x01 if behalten else 0x00)     # PUBLISH, QoS 0
        self.sock.sendall(bytes([kopf]) + _varint(len(nutzlast)) + nutzlast)

    # ------------------------------------------------------------------ Abonnieren
    def _lies_bytes(self, anzahl, ende):
        daten = b""
        while len(daten) < anzahl:
            rest = ende - time.time()
            if rest <= 0:
                raise TimeoutError("keine Nachricht innerhalb der Frist")
            self.sock.settimeout(rest)
            teil = self.sock.recv(anzahl - len(daten))
            if not teil:
                raise ConnectionError("Broker hat die Verbindung geschlossen")
            daten += teil
        return daten

    def abonnieren(self, thema):
        nutzlast = struct.pack("!H", 1) + _text(thema) + bytes([0])
        self.sock.sendall(bytes([0x82]) + _varint(len(nutzlast)) + nutzlast)

    def lesen(self, thema, timeout=None):
        """Abonnieren und auf die naechste (ggf. retained) Nachricht warten."""
        if not self.sock:
            self.verbinden()
        self.abonnieren(thema)
        ende = time.time() + (timeout or self.timeout)
        while True:
            kopf = self._lies_bytes(1, ende)[0]
            laenge, mult = 0, 1
            while True:
                byte = self._lies_bytes(1, ende)[0]
                laenge += (byte & 0x7F) * mult
                if not byte & 0x80:
                    break
                mult *= 128
            rumpf = self._lies_bytes(laenge, ende)
            if kopf >> 4 != 3:                       # kein PUBLISH (z. B. SUBACK)
                continue
            themenlaenge = struct.unpack("!H", rumpf[:2])[0]
            empfangen = rumpf[2:2 + themenlaenge].decode("utf-8")
            qos = (kopf >> 1) & 0x03
            rest = rumpf[2 + themenlaenge:]
            if qos:                                  # Paket-ID bei QoS > 0
                rest = rest[2:]
            if empfangen == thema:
                return rest.decode("utf-8", "replace")

    # ------------------------------------------------------------------ Abbau
    def trennen(self):
        if not self.sock:
            return
        try:
            self.sock.sendall(b"\xe0\x00")             # DISCONNECT
        except OSError:
            pass
        try:
            self.sock.close()
        finally:
            self.sock = None

    def __enter__(self):
        return self.verbinden()

    def __exit__(self, *_):
        self.trennen()
