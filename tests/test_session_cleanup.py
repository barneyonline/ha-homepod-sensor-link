"""Exercise backend classification through real aiohomekit connector cleanup."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohomekit.characteristic_cache import CharacteristicCacheMemory
from aiohomekit.controller.ip.pairing import IpPairing
from aiohomekit.exceptions import AuthenticationError, IncorrectPairingIdError, InvalidSignatureError

from custom_components.homepod_sensor_link.backend import HomeKitBackend
from custom_components.homepod_sensor_link.engine import AuthenticationFailure, RecoveryEngine, SafetyFailure
from test_recovery import fixture


class SessionCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.target, self.entry = fixture()
        self.entry.data.update(AccessoryIP="192.0.2.1", AccessoryPort=12345)
        self.controller = SimpleNamespace(_char_cache=CharacteristicCacheMemory())
        self.backend = HomeKitBackend(self.controller, None)
        self.backend.endpoint = AsyncMock(return_value={"AccessoryIP": "192.0.2.1", "AccessoryPort": 12345})

    async def pairing_with_failed_connector(self, error):
        pairing = IpPairing(self.controller, self.entry.data)

        async def reject():
            raise error

        task = asyncio.create_task(reject())
        try:
            await task
        except Exception:
            pass
        pairing.connection._connector = task
        pairing.connection._last_connector_error = error
        pairing.list_accessories_and_characteristics = AsyncMock(side_effect=error)
        return pairing

    async def test_cleanup_cannot_mask_classified_authentication_failure(self):
        pairing = await self.pairing_with_failed_connector(AuthenticationError("secret must not escape"))
        with patch("custom_components.homepod_sensor_link.backend.IpPairing", return_value=pairing):
            with self.assertRaises(AuthenticationFailure):
                await self.backend.read_ha(self.target)

    async def test_cleanup_cannot_mask_accessory_identity_safety_failure(self):
        pairing = await self.pairing_with_failed_connector(IncorrectPairingIdError("private identity"))
        with patch("custom_components.homepod_sensor_link.backend.IpPairing", return_value=pairing):
            with self.assertRaises(SafetyFailure):
                await self.backend.read_ha(self.target)

    async def test_source_rejection_remains_a_safety_failure(self):
        pairing = await self.pairing_with_failed_connector(AuthenticationError("secret must not escape"))
        with patch("custom_components.homepod_sensor_link.backend.IpPairing", return_value=pairing):
            with self.assertRaises(SafetyFailure):
                async with self.backend.session(self.target, source=True) as p:
                    await p.list_accessories_and_characteristics()

    async def test_invalid_accessory_signature_remains_safety_failure_after_cleanup(self):
        pairing = await self.pairing_with_failed_connector(InvalidSignatureError("private signature detail"))
        with patch("custom_components.homepod_sensor_link.backend.IpPairing", return_value=pairing):
            with self.assertRaises(SafetyFailure):
                await self.backend.read_ha(self.target)

    async def test_failed_cleanup_still_closes_open_transport(self):
        pairing = await self.pairing_with_failed_connector(AuthenticationError("private"))
        transport = pairing.connection.transport = Mock()
        with patch("custom_components.homepod_sensor_link.backend.IpPairing", return_value=pairing):
            with self.assertRaises(AuthenticationFailure):
                await self.backend.read_ha(self.target)
        transport.close.assert_called_once()

    async def test_transport_close_failure_preserves_primary_authentication(self):
        pairing = await self.pairing_with_failed_connector(AuthenticationError("test"))
        pairing.connection.transport = Mock(close=Mock(side_effect=OSError()))
        with patch("custom_components.homepod_sensor_link.backend.IpPairing", return_value=pairing):
            with self.assertRaises(AuthenticationFailure):
                await self.backend.read_ha(self.target)

    async def test_cleanup_does_not_turn_network_failure_into_auth_failure(self):
        pairing = await self.pairing_with_failed_connector(OSError("private network details"))
        with patch("custom_components.homepod_sensor_link.backend.IpPairing", return_value=pairing):
            with self.assertRaises(OSError):
                await self.backend.read_ha(self.target)

    async def test_cleanup_only_failure_does_not_authorize_repair(self):
        pairing = await self.pairing_with_failed_connector(AuthenticationError("private"))
        with patch("custom_components.homepod_sensor_link.backend.IpPairing", return_value=pairing):
            with self.assertRaises(OSError):
                async with self.backend.session(self.target):
                    pass

    async def test_cleanup_does_not_mask_cancellation(self):
        pairing = await self.pairing_with_failed_connector(AuthenticationError("private"))
        with patch("custom_components.homepod_sensor_link.backend.IpPairing", return_value=pairing):
            with self.assertRaises(asyncio.CancelledError):
                async with self.backend.session(self.target):
                    raise asyncio.CancelledError()

    async def test_actual_backend_failure_reaches_bounded_repair_trigger(self):
        pairs = [await self.pairing_with_failed_connector(AuthenticationError("private")) for _ in range(3)]
        now = [0]
        self.backend.restore_missing = AsyncMock(side_effect=OSError("stop before any write"))
        engine = RecoveryEngine([self.target], self.backend, AsyncMock(), AsyncMock(), AsyncMock(), lambda: now[0])
        with patch("custom_components.homepod_sensor_link.backend.IpPairing", side_effect=pairs):
            for now[0] in (0, 300, 600):
                await engine.check_all()
        self.backend.restore_missing.assert_awaited_once_with(self.target)
        self.assertEqual(engine.states[self.target["entry_id"]].attempts, [600])

    async def test_generic_diagnostic_retains_no_exception_message(self):
        now = [0]
        self.backend.read_ha = AsyncMock(side_effect=ValueError("sensitive test payload"))
        engine = RecoveryEngine([self.target], self.backend, AsyncMock(), AsyncMock(), AsyncMock(), lambda: now[0])
        await engine.check_all()
        state = engine.states[self.target["entry_id"]]
        self.assertEqual(state.last_error_type, "ValueError")
        self.assertTrue(state.last_error_location)
        self.assertNotIn("sensitive test payload", str(engine.snapshot()))
        self.assertEqual(state.auth_failures, 0)
