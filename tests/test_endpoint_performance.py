"""Saved endpoints avoid browsing without retrying rejected identities or writes."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohomekit.exceptions import (
    AccessoryDisconnectedError,
    AuthenticationError,
    IncorrectPairingIdError,
    InvalidSignatureError,
)

from custom_components.homepod_sensor_link import backend as backend_module
from custom_components.homepod_sensor_link.backend import HomeKitBackend
from custom_components.homepod_sensor_link.engine import AuthenticationFailure, SafetyFailure
from test_recovery import fixture


class EndpointPerformanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.target, self.entry = fixture()
        self.entry.data.update(AccessoryIP="192.0.2.1", AccessoryPort=12345)
        self.backend = HomeKitBackend(Mock(), SimpleNamespace(zeroconf=object()))
        self.backend.controller.async_find = AsyncMock(side_effect=AssertionError("unexpected discovery"))

    def pairing(self, error=None):
        return SimpleNamespace(
            _ensure_connected=AsyncMock(side_effect=error),
            connection=SimpleNamespace(last_connector_error=None, transport=None),
            shutdown=AsyncMock(),
        )

    async def test_saved_endpoint_authenticates_without_waiting_for_discovery(self):
        pairing = self.pairing()
        with patch("custom_components.homepod_sensor_link.backend.IpPairing", return_value=pairing) as factory:
            async with self.backend.session(self.target) as active:
                self.assertIs(active, pairing)
                pairing._ensure_connected.assert_awaited_once()
            self.assertEqual(factory.call_args.args[1]["AccessoryIP"], "192.0.2.1")
        self.backend.controller.async_find.assert_not_awaited()
        pairing.shutdown.assert_awaited_once()

    async def test_failed_saved_connection_is_closed_before_discovery_and_new_address_is_reused(self):
        for error in (OSError(), AccessoryDisconnectedError("disposable")):
            first, second, third = self.pairing(error), self.pairing(), self.pairing()
            self.entry.data.update(AccessoryIP="192.0.2.1", AccessoryIPs=["192.0.2.1"])

            async def rediscover(_target, **_options):
                first.shutdown.assert_awaited_once()
                return {"AccessoryIP": "192.0.2.2", "AccessoryIPs": ["192.0.2.2"], "AccessoryPort": 23456}

            with (
                patch.object(self.backend, "endpoint", AsyncMock(side_effect=rediscover)) as endpoint,
                patch.object(backend_module, "IpPairing", side_effect=[first, second, third]) as factory,
            ):
                async with self.backend.session(self.target) as active:
                    self.assertIs(active, second)
                async with self.backend.session(self.target) as active:
                    self.assertIs(active, third)
                self.assertEqual(factory.call_args.args[1]["AccessoryIP"], "192.0.2.2")
                endpoint.assert_awaited_once()
            second.shutdown.assert_awaited_once()
            third.shutdown.assert_awaited_once()

    async def test_missing_address_uses_discovery_and_still_authenticates(self):
        self.entry.data.pop("AccessoryIP")
        pairing = self.pairing()
        with (
            patch.object(self.backend, "endpoint", AsyncMock(return_value={"AccessoryIP": "192.0.2.2"})),
            patch.object(backend_module, "IpPairing", return_value=pairing) as factory,
        ):
            async with self.backend.session(self.target):
                pairing._ensure_connected.assert_awaited_once()
            self.assertEqual(factory.call_args.args[1]["AccessoryIP"], "192.0.2.2")

    async def test_identity_rejections_never_trigger_discovery_fallback(self):
        for error, source, expected in (
            (AuthenticationError("disposable"), False, AuthenticationFailure),
            (AuthenticationError("disposable"), True, SafetyFailure),
            (InvalidSignatureError("disposable"), False, SafetyFailure),
        ):
            pairing = self.pairing(error)
            with (
                patch.object(self.backend, "endpoint", AsyncMock()) as endpoint,
                patch.object(backend_module, "IpPairing", return_value=pairing),
                self.assertRaises(expected),
            ):
                async with self.backend.session(self.target, source=source):
                    self.fail("Rejected connection must not reach the operation")
            endpoint.assert_not_awaited()
            pairing.shutdown.assert_awaited_once()

    async def test_operation_or_cleanup_failure_never_replays_the_operation(self):
        for cleanup in (False, True):
            pairing = self.pairing()
            operation = Mock()
            if cleanup:
                pairing.shutdown.side_effect = OSError()
            with (
                patch.object(self.backend, "endpoint", AsyncMock()) as endpoint,
                patch.object(backend_module, "IpPairing", return_value=pairing) as factory,
                self.assertRaises(OSError),
            ):
                async with self.backend.session(self.target):
                    operation()
                    if not cleanup:
                        raise OSError()
            operation.assert_called_once()
            factory.assert_called_once()
            endpoint.assert_not_awaited()

    async def test_saved_connect_timeout_drains_before_fallback(self):
        first, second = self.pairing(), self.pairing()
        drained = asyncio.Event()

        async def connecting():
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()

        first._ensure_connected.side_effect = connecting
        timeout = asyncio.timeout
        with (
            patch.object(
                backend_module.asyncio, "timeout", side_effect=lambda seconds: timeout(0 if seconds == 2 else seconds)
            ),
            patch.object(self.backend, "endpoint", AsyncMock(return_value={"AccessoryIP": "192.0.2.2"})) as endpoint,
            patch.object(backend_module, "IpPairing", side_effect=[first, second]),
        ):
            async with self.backend.session(self.target) as active:
                self.assertTrue(drained.is_set())
                self.assertIs(active, second)
            endpoint.assert_awaited_once()
        first.shutdown.assert_awaited_once()

    async def test_cancellation_during_connect_closes_without_discovery(self):
        pairing = self.pairing()
        begun = asyncio.Event()

        async def connecting():
            begun.set()
            await asyncio.Event().wait()

        pairing._ensure_connected.side_effect = connecting

        async def connect():
            async with self.backend.session(self.target):
                self.fail("Cancelled connection must not reach the operation")

        with (
            patch.object(self.backend, "endpoint", AsyncMock()) as endpoint,
            patch.object(backend_module, "IpPairing", return_value=pairing),
        ):
            task = asyncio.create_task(connect())
            await asyncio.wait_for(begun.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            endpoint.assert_not_awaited()
        pairing.shutdown.assert_awaited_once()

    async def test_reassigned_saved_address_rediscovered_and_authenticated_with_original_pins(self):
        first, second = self.pairing(IncorrectPairingIdError("disposable")), self.pairing()
        info = SimpleNamespace(async_request=AsyncMock(return_value=True))
        description = SimpleNamespace(id=self.target["accessory_id"], addresses=["192.0.2.2"], port=23456)
        self.backend.controller.async_find.side_effect = None
        self.backend.controller.async_find.return_value = SimpleNamespace(description=SimpleNamespace(name="candidate"))
        with (
            patch.object(backend_module, "AsyncServiceInfo", return_value=info),
            patch.object(backend_module.HomeKitService, "from_service_info", return_value=description),
            patch.object(backend_module, "IpPairing", side_effect=[first, second]) as factory,
        ):
            async with self.backend.session(self.target) as active:
                self.assertIs(active, second)
                first.shutdown.assert_awaited_once()
                second._ensure_connected.assert_awaited_once()
            pairing_data = factory.call_args.args[1]
            self.assertEqual(pairing_data["AccessoryIP"], "192.0.2.2")
            self.assertEqual(pairing_data["AccessoryLTPK"], self.target["accessory_key"])
            self.assertEqual(pairing_data["AccessoryPairingID"], self.target["pairing"]["AccessoryPairingID"])
        self.backend.controller.async_find.assert_awaited_once()
        second.shutdown.assert_awaited_once()

    async def test_saved_id_mismatch_requires_a_matched_discovery_before_retry(self):
        for result in (None, "unresolved", "wrong_id"):
            first = self.pairing(IncorrectPairingIdError("disposable"))
            self.backend.controller.async_find.side_effect = None
            self.backend.controller.async_find.return_value = (
                None if result is None else SimpleNamespace(description=SimpleNamespace(name="candidate"))
            )
            info = SimpleNamespace(async_request=AsyncMock(return_value=result == "wrong_id"))
            with (
                patch.object(backend_module, "AsyncServiceInfo", return_value=info),
                patch.object(
                    backend_module.HomeKitService, "from_service_info", return_value=SimpleNamespace(id="other")
                ),
                patch.object(backend_module, "IpPairing", return_value=first) as factory,
                self.assertRaises(SafetyFailure if result == "wrong_id" else OSError),
            ):
                async with self.backend.session(self.target):
                    self.fail("Unconfirmed rediscovery must not reach an operation")
            factory.assert_called_once()
            first.shutdown.assert_awaited_once()

    async def test_wrong_identity_after_matched_discovery_still_fails_closed(self):
        first, second = (
            self.pairing(IncorrectPairingIdError("disposable")),
            self.pairing(IncorrectPairingIdError("disposable")),
        )
        with (
            patch.object(
                self.backend,
                "endpoint",
                AsyncMock(
                    return_value={"AccessoryIP": "192.0.2.2", "AccessoryIPs": ["192.0.2.2"], "AccessoryPort": 12345}
                ),
            ) as endpoint,
            patch.object(backend_module, "IpPairing", side_effect=[first, second]) as factory,
            self.assertRaises(SafetyFailure),
        ):
            async with self.backend.session(self.target):
                self.fail("A rejected rediscovered identity must not reach an operation")
        endpoint.assert_awaited_once_with(self.target, require_discovery=True)
        self.assertEqual(factory.call_count, 2)
        first.shutdown.assert_awaited_once()
        second.shutdown.assert_awaited_once()

    async def test_last_connector_id_mismatch_can_rediscover_but_signature_or_auth_failures_cannot(self):
        for error in (TimeoutError(), InvalidSignatureError("disposable"), AuthenticationError("disposable")):
            first, second = self.pairing(error), self.pairing()
            first.connection.last_connector_error = IncorrectPairingIdError("disposable")
            with (
                patch.object(
                    self.backend, "endpoint", AsyncMock(return_value={"AccessoryIP": "192.0.2.2"})
                ) as endpoint,
                patch.object(backend_module, "IpPairing", side_effect=[first, second]) as factory,
            ):
                if isinstance(error, TimeoutError):
                    async with self.backend.session(self.target) as active:
                        self.assertIs(active, second)
                    endpoint.assert_awaited_once_with(self.target, require_discovery=True)
                else:
                    with self.assertRaises(SafetyFailure):
                        async with self.backend.session(self.target):
                            self.fail("Signature/authentication rejection must not reach an operation")
                    factory.assert_called_once()
                    endpoint.assert_not_awaited()
            first.shutdown.assert_awaited_once()

    async def test_id_mismatch_during_operation_is_not_retried(self):
        pairing = self.pairing()
        with (
            patch.object(self.backend, "endpoint", AsyncMock()) as endpoint,
            patch.object(backend_module, "IpPairing", return_value=pairing) as factory,
            self.assertRaises(SafetyFailure),
        ):
            async with self.backend.session(self.target):
                raise IncorrectPairingIdError("disposable")
        factory.assert_called_once()
        endpoint.assert_not_awaited()
