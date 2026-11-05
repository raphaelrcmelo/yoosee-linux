"""Controle PTZ das câmeras Yoosee/Gwell via ONVIF (porta TCP 5000).

Quirks do firmware (ver docs/API_YOOSEE.md, seção "PTZ — RESOLVIDO via ONVIF"):
toda chamada exige WS-Security, senão o servidor fecha o socket sem responder;
só ContinuousMove funciona (Stop/RelativeMove derrubam a conexão), então o
movimento para via <Timeout>; velocidade abaixo de 1.0 não move; comandos
sobrepostos são descartados pela câmera.
"""
import base64
import datetime
import hashlib
import os
import socket
import threading
import time

ONVIF_PORT = 5000
PROFILE_TOKEN = "IPCProfilesToken0"
STEP_SECONDS = 1
DIRECTIONS = {
    "left": (-1.0, 0.0),
    "right": (1.0, 0.0),
    "up": (0.0, 1.0),
    "down": (0.0, -1.0),
}


def _security_header(user: str, password: str) -> str:
    nonce = os.urandom(16)
    created = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    return (
        '<s:Header><Security s:mustUnderstand="1" '
        'xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        f"<UsernameToken><Username>{user}</Username>"
        '<Password Type="http://docs.oasis-open.org/wss/2004/01/'
        f'oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>'
        '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/'
        f'oasis-200401-wss-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}</Nonce>'
        '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/'
        f'oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>'
        "</UsernameToken></Security></s:Header>"
    )


def soap_call(ip: str, user: str, password: str, service: str, body: str, timeout: float = 5) -> str:
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tt="http://www.onvif.org/ver10/schema">'
        f"{_security_header(user, password)}<s:Body>{body}</s:Body></s:Envelope>"
    ).encode()
    request = (
        f"POST /onvif/{service} HTTP/1.1\r\nHost: {ip}:{ONVIF_PORT}\r\n"
        "Content-Type: application/soap+xml; charset=utf-8\r\n"
        f"Content-Length: {len(envelope)}\r\nConnection: close\r\n\r\n"
    ).encode() + envelope
    with socket.create_connection((ip, ONVIF_PORT), timeout=timeout) as sock:
        sock.sendall(request)
        received = b""
        while True:
            data = sock.recv(4096)
            if not data:
                break
            received += data
            head, sep, rest = received.partition(b"\r\n\r\n")
            if sep:
                length = next((int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
                               if line.lower().startswith(b"content-length:")), None)
                if length is not None and len(rest) >= length:
                    break
    return received.decode(errors="replace")


def continuous_move(ip: str, user: str, password: str, direction: str, seconds: float = STEP_SECONDS) -> bool:
    x, y = DIRECTIONS[direction]
    body = (
        '<ContinuousMove xmlns="http://www.onvif.org/ver20/ptz/wsdl">'
        f"<ProfileToken>{PROFILE_TOKEN}</ProfileToken>"
        f'<Velocity><tt:PanTilt x="{x}" y="{y}"/></Velocity>'
        f"<Timeout>PT{seconds:g}S</Timeout></ContinuousMove>"
    )
    response = soap_call(ip, user, password, "ptz_service", body)
    return "ContinuousMoveResponse" in response


class PtzController:
    """Um por câmera. Ignora cliques enquanto o movimento anterior não terminou,
    porque a câmera descarta comandos que chegam sobrepostos."""

    def __init__(self, ip: str, user: str, password: str):
        self.ip = ip
        self.user = user
        self.password = password
        self._busy_until = 0.0
        self._lock = threading.Lock()

    def move(self, direction: str, on_result=None, seconds: float = STEP_SECONDS) -> bool:
        with self._lock:
            now = time.monotonic()
            if now < self._busy_until:
                return False
            self._busy_until = now + seconds + 0.5

        def worker():
            try:
                ok = continuous_move(self.ip, self.user, self.password, direction, seconds)
            except OSError:
                ok = False
            if on_result is not None:
                on_result(ok)

        threading.Thread(target=worker, daemon=True).start()
        return True
