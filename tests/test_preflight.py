"""Fresh setup verification and controlled service/diagnostic failures."""

import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.helpers import issue_registry as ir

from custom_components import homepod_sensor_link as integration
from custom_components.homepod_sensor_link.credentials import DOMAIN
from custom_components.homepod_sensor_link.diagnostics import async_get_config_entry_diagnostics
from custom_components.homepod_sensor_link.engine import AuthenticationFailure, SafetyFailure
import test_onboarding_v2 as fixtures


class PreflightTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.OnboardingTests.asyncSetUp
    asyncTearDown = fixtures.OnboardingTests.asyncTearDown
    make_runtime = fixtures.OnboardingTests.make_runtime

    async def test_startup_probes_are_read_only_and_all_network_failure_retries_setup(self):
        config, runtime = await self.make_runtime()
        try:
            for error, status in (
                (AuthenticationFailure(), "authentication_failed"),
                (OSError(), "unreachable"),
                (SafetyFailure(), "blocked"),
            ):
                self.backend.read_ha.side_effect = error
                if isinstance(error, OSError):
                    with self.assertRaises(ConfigEntryNotReady):
                        await runtime.preflight()
                else:
                    await runtime.preflight()
                self.assertEqual(runtime.engine.states[self.key].status, status)
                self.backend.restore_missing.assert_not_called()
            self.backend.read_ha.reset_mock()
            await runtime.preflight()
            self.backend.read_ha.assert_not_called()
            runtime.engine.states[self.key].blocked = False
            self.backend.read_ha.side_effect = None
            self.backend.read_ha.return_value = {"temperature": 20, "humidity": 40}
            await runtime.preflight()
            self.assertEqual(runtime.readings[self.key][1], {"temperature": 20, "humidity": 40})
            runtime.engine.targets.clear()
            await runtime.preflight()
        finally:
            await runtime.stop()

    async def test_refresh_errors_are_controlled_and_repair_clears_after_retry(self):
        config, runtime = await self.make_runtime()
        try:
            with patch.object(runtime, "_update", AsyncMock(side_effect=ValueError("synthetic-private-detail"))):
                with self.assertRaises(UpdateFailed) as error:
                    await runtime.update()
                self.assertNotIn("synthetic-private-detail", str(error.exception))
            registry = ir.async_get(self.hass)
            self.assertIsNotNone(registry.async_get_issue(DOMAIN, "checks_unavailable"))
            with patch.object(runtime, "_update", AsyncMock(return_value=runtime.engine.snapshot())):
                await runtime.update()
            self.assertIsNone(registry.async_get_issue(DOMAIN, "checks_unavailable"))
        finally:
            await runtime.stop()

    async def test_actions_translate_operational_failure_and_unloaded_diagnostics_omit_entry_data(self):
        self.assertEqual((await async_get_config_entry_diagnostics(self.hass, fixtures.entry()))["devices"], [])
        await integration.async_setup(self.hass, {})
        config, runtime = await self.make_runtime()
        self.hass.data[integration.OWNER] = config.entry_id
        try:
            runtime.coordinator.last_update_success = False
            with patch.object(runtime.coordinator, "async_refresh", AsyncMock()), self.assertRaises(HomeAssistantError):
                await self.hass.services.async_call(DOMAIN, "check_now", {}, blocking=True)
            with patch.object(runtime.audit, "check", AsyncMock(side_effect=ValueError("synthetic-private-detail"))):
                with self.assertRaises(HomeAssistantError) as error:
                    await self.hass.services.async_call(DOMAIN, "check_recovery_access", {}, blocking=True)
                self.assertNotIn("synthetic-private-detail", str(error.exception))
        finally:
            await runtime.stop()
