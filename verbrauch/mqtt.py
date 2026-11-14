#!/usr/bin/env python3
"""Minimaler MQTT-Client — nur so viel, wie fuer Home-Assistant-Discovery noetig ist.

Bewusst ohne externe Abhaengigkeit: Verbinden (CONNECT) und Veroeffentlichen
(PUBLISH, QoS 0, optional retained). Kein Abonnieren, keine Wiederholungen.
Fuer regelmaessig gesendete Messwerte reicht das: Faellt der Broker aus, wird
beim naechsten Messlauf einfach erneut gesendet.
"""

import socket
import struct


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
