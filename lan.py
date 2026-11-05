"""Descoberta das câmeras na rede local.

Broadcast UDP 8899 reproduzido do app oficial (captures/handshake_full_2026-11-04.pcap):
a câmera responde para a porta fixa 8900 do PC, com o devId em int64 LE no offset 28.
Só o firmware 37.x responde; o 40.x ignora, daí o scan de RTSP para associação manual.
"""
import ipaddress
import json
import os
import socket
import struct
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

DISCOVERY_PORT = 8899
REPLY_PORT = 8900
_VIRTUAL_IFACES = ("lo", "docker", "br-", "veth", "virbr")


def _lan_interfaces() -> list[tuple[ipaddress.IPv4Interface, str | None]]:
    """(endereço/prefixo, broadcast) das interfaces de rede física; ignora /32 (VPN) e bridges."""
    try:
        out = subprocess.run(["ip", "-j", "-4", "addr", "show"], capture_output=True, text=True, timeout=3).stdout
        ifaces = json.loads(out)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    result = []
    for iface in ifaces:
        if iface.get("ifname", "").startswith(_VIRTUAL_IFACES):
            continue
        for addr in iface.get("addr_info", []):
            if addr.get("prefixlen", 32) < 32:
                result.append((ipaddress.IPv4Interface(f"{addr['local']}/{addr['prefixlen']}"), addr.get("broadcast")))
    return result


def is_on_local_network(ip: str) -> bool:
    """False quando a câmera só é alcançável via VPN/rota (fora de casa)."""
    target = ipaddress.IPv4Address(ip)
    return any(target in iface.network for iface, _ in _lan_interfaces())


def reachable(ip: str, port: int = 554, timeout: float = 0.8) -> bool:
    """A câmera responde no RTSP? (False fora de casa sem VPN.)"""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _probe_packet(seq: int) -> bytes:
    return b"\x70\x02\x1c\x00" + bytes(8) + struct.pack("<I", seq) + bytes(4) + bytes.fromhex("000001020900c422")


def discover(timeout: float = 2.5) -> dict[str, str]:
    """devId -> IP das câmeras que respondem ao broadcast de descoberta."""
    found: dict[str, str] = {}
    try:
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        rx.bind(("", REPLY_PORT))
    except OSError:
        return found
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    rx.settimeout(0.3)
    # 255.255.255.255 sai pela rota padrão, que pode ser uma VPN; o broadcast da sub-rede não
    targets = ["255.255.255.255"] + [bcast for _, bcast in _lan_interfaces() if bcast]
    seq = int.from_bytes(os.urandom(4), "little")
    deadline = time.monotonic() + timeout
    next_probe = 0.0
    try:
        while time.monotonic() < deadline:
            if time.monotonic() >= next_probe:
                for target in targets:
                    try:
                        tx.sendto(_probe_packet(seq), (target, DISCOVERY_PORT))
                    except OSError:
                        pass
                seq = (seq + 1) & 0xFFFFFFFF
                next_probe = time.monotonic() + 0.8
            try:
                data, (ip, _port) = rx.recvfrom(2048)
            except socket.timeout:
                continue
            if len(data) >= 36 and data[:2] == b"\x70\x03":
                dev_id = int.from_bytes(data[28:36], "little")
                found[str(dev_id)] = ip
    finally:
        rx.close()
        tx.close()
    return found


def scan_rtsp_hosts(extra_ips: list[str] = (), port: int = 554) -> list[str]:
    """IPs com RTSP aberto nas /24 das interfaces locais e das câmeras já conhecidas
    (estas cobrem o caso de estar fora de casa, acessando a rede via VPN)."""
    networks = {ipaddress.IPv4Network(f"{ip}/24", strict=False) for ip in extra_ips}
    for iface, _ in _lan_interfaces():
        if iface.network.prefixlen >= 24:
            networks.add(iface.network)
    hosts = sorted({str(h) for net in networks for h in net.hosts()}, key=ipaddress.IPv4Address)

    def check(ip: str) -> str | None:
        try:
            with socket.create_connection((ip, port), timeout=0.5):
                return ip
        except OSError:
            return None

    with ThreadPoolExecutor(max_workers=64) as pool:
        return [ip for ip in pool.map(check, hosts) if ip]
