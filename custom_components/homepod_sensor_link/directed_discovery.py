"""Bounded HAP DNS-SD requests with temporary unicast reply sockets."""

from __future__ import annotations

import asyncio
import socket

from zeroconf._dns import DNSAddress, DNSQuestion, DNSService, DNSText
from zeroconf._protocol.incoming import DNSIncoming
from zeroconf._protocol.outgoing import DNSOutgoing
from zeroconf.asyncio import AsyncServiceInfo

HAP_TYPE = "_hap._tcp.local."
REPLY_TIMEOUT = 3


def service_infos(packet: bytes) -> list[AsyncServiceInfo]:
    """Decode fresh reply records without altering HA's shared discovery cache."""
    incoming = DNSIncoming(packet)
    if not incoming.valid or not incoming.is_response():
        return []
    records = [record for record in incoming.answers() if record.ttl > 0]
    infos = []
    for service in records:
        if not isinstance(service, DNSService) or not service.name.lower().endswith("." + HAP_TYPE):
            continue
        text = next((r for r in records if isinstance(r, DNSText) and r.name.lower() == service.name.lower()), None)
        if text is None:
            continue
        infos.append(
            AsyncServiceInfo(
                HAP_TYPE,
                service.name,
                server=service.server,
                port=service.port,
                properties=text.text,
                addresses=[
                    r.address for r in records if isinstance(r, DNSAddress) and r.name.lower() == service.server.lower()
                ],
            )
        )
    return infos


async def discover_hosts(hosts: list[str]) -> list[AsyncServiceInfo]:
    """Ask only validated export hints; always close each ephemeral UDP socket."""
    loop = asyncio.get_running_loop()

    async def request(host: str) -> list[AsyncServiceInfo]:
        try:
            with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setblocking(False)
                async with asyncio.timeout(REPLY_TIMEOUT):
                    await loop.sock_connect(sock, (host, 5353))
                    query = DNSOutgoing(0, multicast=False)
                    query.add_question(DNSQuestion(HAP_TYPE, 12, 1))
                    await loop.sock_sendall(sock, query.packets()[0])
                    while True:
                        infos = service_infos(await loop.sock_recv(sock, 9000))
                        if infos:
                            return infos
        except (OSError, TimeoutError, ValueError):
            return []

    return [info for result in await asyncio.gather(*(request(host) for host in hosts)) for info in result]
