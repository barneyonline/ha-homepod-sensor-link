"""Finish runtime coverage at cancellation, native publication and shared registry seams."""

import asyncio
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components import homepod_sensor_link as integration
from custom_components.homepod_sensor_link import coordinator
from custom_components.homepod_sensor_link.credentials import DOMAIN
from custom_components.homepod_sensor_link.engine import SafetyFailure
import test_onboarding_v2 as fixtures
from test_recovery import fixture
from test_boundary_failures import native_config


class RuntimeCompletionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.OnboardingTests.asyncSetUp
    asyncTearDown = fixtures.OnboardingTests.asyncTearDown
    make_runtime = fixtures.OnboardingTests.make_runtime

    async def test_pending_samples_publish_but_setup_waits_for_actual_entities(self):
        config, runtime = await self.make_runtime()
        self.backend.read_ha.side_effect = None
        self.backend.read_ha.return_value = {"temperature": 21, "humidity": 45}
        try:
            await runtime.inspect_pending(self.key)
            self.assertEqual(runtime.readings[self.key][1]["temperature"], 21)
            self.assertEqual(runtime.records[self.key]["phase"], "verified")
            self.assertIn("source", runtime.records[self.key]["target"])
            with self.assertRaises(OSError):
                await runtime.verify_native(self.target)
            await runtime.read_target(self.target)
            with (
                patch.object(runtime.engine, "check_all", AsyncMock()),
                patch.object(runtime.audit, "check", AsyncMock()),
            ):
                runtime.engine.states[self.key].status = "healthy"
                await runtime.update()
                runtime.records[self.key]["phase"] = "active"
                await runtime.update()
            record = deepcopy(runtime.records[self.key])
            with patch.object(runtime.provisioner, "inspect", AsyncMock(return_value=(record, None))):
                await runtime.inspect_pending(self.key)
            runtime.records[self.key]["phase"] = "prepared"
            runtime.records[self.key]["target"].pop("source")
            await self.vault.put(self.key, runtime.records[self.key])
            self.backend.read_ha.side_effect = integration.SafetyFailure()
            await runtime.resume_device(self.key)
            self.assertEqual(runtime.records[self.key]["phase"], "blocked")
            await runtime.cancel_device(self.key)
            self.assertTrue(self.vault.records()[self.key]["cancelled"])
            with patch.object(coordinator, "conflicting_recovery", return_value=True), self.assertRaises(SafetyFailure):
                await runtime.resume_device(self.key)
        finally:
            await runtime.stop()

    async def test_removal_stop_and_parent_cancel_drain_inflight_inspection(self):
        for operation in ("remove", "stop", "cancel"):
            config, runtime = await self.make_runtime()
            entered = asyncio.Event()

            async def blocked(*args, **kwargs):
                entered.set()
                await asyncio.Event().wait()

            with patch.object(runtime.provisioner, "inspect", blocked):
                pending = asyncio.create_task(runtime.inspect_pending(self.key))
                await entered.wait()
                if operation == "remove":
                    await runtime.remove_device(self.key)
                    await pending
                    self.assertNotIn(self.key, self.vault.records())
                elif operation == "stop":
                    await runtime.stop()
                    await pending
                    self.assertIn(self.key, self.vault.records())
                else:
                    pending.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await pending
                self.assertEqual(runtime.pending_tasks, {})
            await runtime.stop()

    async def test_native_reload_uses_real_registry_ids_and_waits_for_new_reports(self):
        config, runtime = await self.make_runtime()
        target, original = fixture()
        native = native_config(original.data)
        self.hass.config_entries._entries[native.entry_id] = native
        target["entry_id"] = native.entry_id
        registry = er.async_get(self.hass)
        for kind in ("temperature", "humidity"):
            registry.async_get_or_create(
                "sensor", "homekit_controller", "native_" + kind, config_entry=native, original_device_class=kind
            )
        ids = runtime.native_sensor_ids(target)
        try:
            with patch.object(self.hass.config_entries, "async_reload", AsyncMock(return_value=False)):
                with self.assertRaises(OSError):
                    await runtime.reload_entry(target)

            async def sample(_seconds):
                for entity_id in ids:
                    self.hass.states.async_set(entity_id, "21")

            with (
                patch.object(self.hass.config_entries, "async_reload", AsyncMock(return_value=True)),
                patch.object(coordinator.asyncio, "sleep", sample),
                patch.object(runtime, "verify_native", AsyncMock()) as verify,
            ):
                await runtime.reload_entry(target)
                verify.assert_awaited_once_with(target)
        finally:
            await runtime.stop()

    async def test_remove_registry_preserves_foreign_associations_and_entities(self):
        config, runtime = await self.make_runtime()
        other = native_config({})
        self.hass.config_entries._entries[other.entry_id] = other
        devices, entities = dr.async_get(self.hass), er.async_get(self.hass)
        unrelated = devices.async_get_or_create(config_entry_id=config.entry_id, identifiers={(DOMAIN, "unrelated")})
        shared = devices.async_get_or_create(config_entry_id=config.entry_id, identifiers={(DOMAIN, self.key)})
        devices.async_get_or_create(config_entry_id=other.entry_id, identifiers={(DOMAIN, self.key)})
        foreign = entities.async_get_or_create(
            "sensor", "other_platform", "foreign", config_entry=other, device_id=shared.id
        )
        owned = entities.async_get_or_create("sensor", DOMAIN, "owned", config_entry=config, device_id=shared.id)
        try:
            await runtime.remove_device(self.key)
            self.assertIsNotNone(devices.async_get(unrelated.id))
            self.assertIsNotNone(entities.async_get(foreign.entity_id))
            self.assertIsNone(entities.async_get(owned.entity_id))
            self.assertIsNotNone(devices.async_get(shared.id))
            self.assertEqual(entities.async_get(foreign.entity_id).device_id, shared.id)
        finally:
            await runtime.stop()

    async def test_unload_keeps_foreign_owner_and_removal_aborts_pending_flows(self):
        config, runtime = await self.make_runtime()
        self.hass.data[integration.OWNER] = "foreign-owner"
        with patch.object(self.hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)):
            await integration.async_unload_entry(self.hass, config)
        self.assertEqual(self.hass.data[integration.OWNER], "foreign-owner")
        self.hass.data[integration.OWNER] = config.entry_id
        with (
            patch.object(
                self.hass.config_entries.flow, "async_progress_by_handler", return_value=[{"flow_id": "pending"}]
            ),
            patch.object(self.hass.config_entries.flow, "async_abort") as abort,
        ):
            await integration.async_remove_entry(self.hass, config)
            abort.assert_called_once_with("pending")
        self.assertNotIn(integration.OWNER, self.hass.data)
