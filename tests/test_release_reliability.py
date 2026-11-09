"""Cancellation must survive real HAP connector cleanup."""

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohomekit.controller.ip.pairing import IpPairing

import test_session_cleanup as cleanup_fixtures
import test_onboarding_v2 as onboarding_fixtures


class ReleaseReliabilityTests(unittest.IsolatedAsyncioTestCase):
    setUp = cleanup_fixtures.SessionCleanupTests.setUp

    async def test_successful_shutdown_closes_transport_without_cancelling_session(self):
        pairing = IpPairing(self.controller, self.entry.data)
        transport = pairing.connection.transport = Mock()
        with patch("custom_components.homepod_sensor_link.backend.IpPairing", return_value=pairing):
            async with self.backend.session(self.target) as active:
                self.assertIs(active, pairing)
        transport.close.assert_called_once()
        self.assertIsNone(pairing.connection.transport)
        self.assertEqual(asyncio.current_task().cancelling(), 0)

    async def test_cancellation_during_connector_shutdown_is_not_swallowed(self):
        pairing = IpPairing(self.controller, self.entry.data)
        transport = pairing.connection.transport = Mock()
        connecting, closing = asyncio.Event(), asyncio.Event()

        async def connector():
            connecting.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                closing.set()
                await asyncio.Event().wait()

        pairing.connection._connector = asyncio.create_task(connector())
        await connecting.wait()

        async def session():
            async with self.backend.session(self.target):
                pass

        with patch("custom_components.homepod_sensor_link.backend.IpPairing", return_value=pairing):
            checking = asyncio.create_task(session())
            try:
                await asyncio.wait_for(closing.wait(), 1)
                checking.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await checking
                transport.close.assert_called_once()
                self.assertIsNone(pairing.connection.transport)
            finally:
                checking.cancel()
                await asyncio.gather(checking, return_exceptions=True)


class RemovalReliabilityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = onboarding_fixtures.OnboardingTests.asyncSetUp
    asyncTearDown = onboarding_fixtures.OnboardingTests.asyncTearDown
    make_runtime = onboarding_fixtures.OnboardingTests.make_runtime

    async def test_failed_credential_removal_retains_inventory_and_can_retry(self):
        config, runtime = await self.make_runtime()
        try:
            with patch.object(self.vault, "remove", AsyncMock(side_effect=OSError("synthetic write failure"))):
                with self.assertRaises(OSError):
                    await runtime.remove_device(self.key)
            self.assertIn(self.key, runtime.records)
            self.assertIn(self.key, runtime.engine.states)
            self.assertIn(self.key, config.data["device_keys"])
            self.assertIn(self.key, self.vault.records())
            await runtime.remove_device(self.key)
            self.assertNotIn(self.key, runtime.records)
            self.assertNotIn(self.key, runtime.engine.states)
            self.assertNotIn(self.key, config.data["device_keys"])
            self.assertNotIn(self.key, self.vault.records())
        finally:
            await runtime.stop()

    async def test_cancelled_removal_after_durable_commit_remains_retryable(self):
        config, runtime = await self.make_runtime()
        remove = self.vault.remove

        async def interrupted_remove(key):
            await remove(key)
            raise asyncio.CancelledError

        try:
            with patch.object(self.vault, "remove", interrupted_remove):
                with self.assertRaises(asyncio.CancelledError):
                    await runtime.remove_device(self.key)
            self.assertIn(self.key, runtime.records)
            self.assertIn(self.key, config.data["device_keys"])
            self.assertNotIn(self.key, self.vault.records())
            await runtime.remove_device(self.key)
            self.assertNotIn(self.key, runtime.records)
            self.assertNotIn(self.key, config.data["device_keys"])
        finally:
            await runtime.stop()
