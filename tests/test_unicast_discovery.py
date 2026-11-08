"""Directed DNS-SD uses ephemeral reply ports and validates complete advertisements."""

import asyncio
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from zeroconf import ServiceInfo
from zeroconf._dns import DNSAddress, DNSService, DNSText
from zeroconf._protocol.incoming import DNSIncoming
from zeroconf._protocol.outgoing import DNSOutgoing

from custom_components.homepod_sensor_link import directed_discovery as discovery


def packet(*, name="Device._hap._tcp.local.", text=True, ttl=120, address=True):
    outgoing = DNSOutgoing(0x8400, multicast=False)
    outgoing.add_answer_at_time(DNSService(name, 33, 1, ttl, 0, 0, 23456, "DEVICE.local."), 0)
    if text:
        info = ServiceInfo(discovery.HAP_TYPE, name, properties={"id": "disposable", "md": "HomePod"})
        outgoing.add_answer_at_time(DNSText(name.upper(), 16, 1, ttl, info.text), 0)
    if address:
        outgoing.add_answer_at_time(DNSAddress("device.local.", 1, 1, ttl, socket.inet_aton("192.0.2.1")), 0)
    return outgoing.packets()[0]


class PacketTests(unittest.TestCase):
    def test_airplay_replies_are_parsed_only_for_the_requested_service_type(self):
        kind = "_airplay._tcp.local."
        name = "Test Room." + kind
        info = ServiceInfo(kind, name, properties={"model": "AudioAccessory5,1"})
        outgoing = DNSOutgoing(0x8400, multicast=False)
        outgoing.add_answer_at_time(DNSService(name, 33, 1, 120, 0, 0, 7000, "device.local."), 0)
        outgoing.add_answer_at_time(DNSText(name, 16, 1, 120, info.text), 0)
        outgoing.add_answer_at_time(DNSAddress("device.local.", 1, 1, 120, socket.inet_aton("192.0.2.1")), 0)
        reply = outgoing.packets()[0]
        self.assertEqual(discovery.service_infos(reply), [])
        self.assertEqual(discovery.service_infos(reply, kind)[0].name, name)
        self.assertEqual(discovery.service_infos(reply, kind)[0].decoded_properties["model"], "AudioAccessory5,1")

    def test_complete_reply_preserves_current_port_and_advertised_address(self):
        infos = discovery.service_infos(packet())
        self.assertEqual(len(infos), 1)
        self.assertEqual(infos[0].port, 23456)
        self.assertEqual(infos[0].parsed_addresses(), ["192.0.2.1"])
        self.assertEqual(infos[0].decoded_properties["id"], "disposable")

    def test_malformed_queries_expired_unrelated_and_incomplete_records(self):
        for value in (
            b"",
            DNSOutgoing(0).packets()[0],
            packet(ttl=0),
            packet(name="Other._airplay._tcp.local.", text=False),
            packet(text=False),
        ):
            with self.subTest(length=len(value)):
                self.assertEqual(discovery.service_infos(value), [])
        infos = discovery.service_infos(packet(address=False))
        self.assertEqual(infos[0].parsed_addresses(), [])


class SocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_ephemeral_connected_queries_use_no_known_answers_and_close_every_socket(self):
        sockets = []

        def make(family, kind):
            sock = Mock()
            sock.__enter__ = Mock(return_value=sock)
            sock.__exit__ = Mock(return_value=False)
            sockets.append(sock)
            self.assertIn(family, (socket.AF_INET, socket.AF_INET6))
            self.assertEqual(kind, socket.SOCK_DGRAM)
            return sock

        def send(sock, data):
            incoming = DNSIncoming(data)
            self.assertEqual(incoming.questions[0].type, 12)
            self.assertEqual(incoming.questions[0].name, discovery.HAP_TYPE)
            self.assertEqual(incoming.answers(), [])

        loop = SimpleNamespace(
            sock_connect=AsyncMock(),
            sock_sendall=AsyncMock(side_effect=send),
            sock_recv=AsyncMock(side_effect=[b"", packet(), packet()]),
        )
        with (
            patch.object(discovery.asyncio, "get_running_loop", return_value=loop),
            patch.object(discovery.socket, "socket", side_effect=make),
        ):
            infos = await discovery.discover_hosts(["192.0.2.1", "2001:db8::1"])
        self.assertEqual(len(infos), 2)
        self.assertEqual(loop.sock_connect.await_count, 2)
        for sock in sockets:
            sock.setblocking.assert_called_once_with(False)
            sock.bind.assert_not_called()
            sock.__exit__.assert_called_once()

    async def test_failure_isolated_and_cancellation_closes_socket(self):
        for error in (OSError(), TimeoutError(), ValueError(), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                sock = Mock()
                sock.__enter__ = Mock(return_value=sock)
                sock.__exit__ = Mock(return_value=False)
                loop = SimpleNamespace(sock_connect=AsyncMock(side_effect=error))
                with (
                    patch.object(discovery.asyncio, "get_running_loop", return_value=loop),
                    patch.object(discovery.socket, "socket", return_value=sock),
                ):
                    if isinstance(error, asyncio.CancelledError):
                        with self.assertRaises(asyncio.CancelledError):
                            await discovery.discover_hosts(["192.0.2.1"])
                    else:
                        self.assertEqual(await discovery.discover_hosts(["192.0.2.1"]), [])
                sock.__exit__.assert_called_once()
