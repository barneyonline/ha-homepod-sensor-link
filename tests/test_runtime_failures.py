"""Lifecycle failure isolation, service availability and actual runtime boundaries."""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.components import persistent_notification
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components import homepod_sensor_link as integration
from custom_components.homepod_sensor_link import coordinator as runtime_module
from custom_components.homepod_sensor_link.engine import (
    COOLDOWN,
    GRACE,
    AuthenticationFailure,
    DeviceState,
    SafetyFailure,
)
from custom_components.homepod_sensor_link.credentials import DOMAIN, public_targets
from custom_components.homepod_sensor_link.vault import Vault
import test_onboarding_v2 as fixtures
from test_boundary_failures import native_config


class RuntimeFailureTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.OnboardingTests.asyncSetUp
    asyncTearDown = fixtures.OnboardingTests.asyncTearDown
    make_runtime = fixtures.OnboardingTests.make_runtime

    @contextmanager
    def network_boundary(self, controller=None):
        controller = controller or SimpleNamespace(async_start=AsyncMock(), async_stop=AsyncMock())
        with (
            patch.object(integration.zeroconf, "async_get_async_instance", AsyncMock()),
            patch.object(integration, "IpController", return_value=controller),
            patch.object(integration, "async_at_started", return_value=lambda: None),
            patch.object(self.hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        ):
            yield controller

    async def test_services_exist_without_loaded_entry_and_use_current_runtime_after_reload(self):
        await integration.async_setup(self.hass, {})
        for name in ("check_now", "check_recovery_access"):
            with self.assertRaises(ServiceValidationError):
                await self.hass.services.async_call(DOMAIN, name, {}, blocking=True, return_response=True)
        config, runtime = await self.make_runtime()
        self.hass.data[integration.OWNER] = config.entry_id
        try:
            with (
                patch.object(runtime.coordinator, "async_refresh", AsyncMock()) as refresh,
                patch.object(runtime.audit, "check", AsyncMock()) as audit,
            ):
                for name in ("check_now", "check_recovery_access"):
                    response = await self.hass.services.async_call(
                        DOMAIN, name, {}, blocking=True, return_response=True
                    )
                    self.assertIn("devices", response)
                refresh.assert_awaited_once()
                audit.assert_awaited_once_with(force=True)
            runtime.stopped = True
            with self.assertRaises(ServiceValidationError):
                await self.hass.services.async_call(DOMAIN, "check_now", {}, blocking=True)
        finally:
            await runtime.stop()

    async def test_setup_rejects_version_inventory_and_public_admin_pin_changes(self):
        with self.assertRaises(ConfigEntryNotReady):
            await integration.async_setup_entry(self.hass, native_config({}))
        await self.vault.put(self.key, self.record)
        invalid = [
            {"device_keys": [self.key, self.key]},
            {"device_keys": ["missing"]},
            {"device_keys": [self.key], "pins": {self.key: {"changed": True}}},
            {"device_keys": [self.key], "admin_pins": {self.key: {"changed": True}}},
        ]
        for data in invalid:
            with self.subTest(keys=list(data)), self.assertRaises(ConfigEntryNotReady):
                await integration.async_setup_entry(self.hass, fixtures.entry(data))
            self.assertNotIn(integration.OWNER, self.hass.data)
        duplicate = deepcopy(self.record)
        duplicate["target"]["entry_id"] = "second"
        await self.vault.put("second", duplicate)
        with self.assertRaises(ConfigEntryNotReady):
            await integration.async_setup_entry(self.hass, fixtures.entry({"device_keys": [self.key, "second"]}))

    async def test_setup_failures_drain_controller_preserve_primary_cancel_and_keep_busy_owner(self):
        await self.vault.put(self.key, self.record)
        config = fixtures.entry({"device_keys": [self.key]})
        for primary, cleanup in ((OSError(), None), (asyncio.CancelledError(), OSError())):
            controller = SimpleNamespace(
                async_start=AsyncMock(side_effect=primary), async_stop=AsyncMock(side_effect=cleanup)
            )
            with (
                self.network_boundary(controller),
                self.assertRaises(
                    type(primary) if isinstance(primary, asyncio.CancelledError) else ConfigEntryNotReady
                ),
            ):
                await integration.async_setup_entry(self.hass, config)
            controller.async_stop.assert_awaited_once()
            self.assertNotIn(integration.OWNER, self.hass.data)
        fake = SimpleNamespace(
            initialize=AsyncMock(side_effect=OSError()), stop=AsyncMock(side_effect=OSError()), closed=False
        )
        with (
            self.network_boundary(),
            patch.object(integration, "Runtime", return_value=fake),
            self.assertRaises(ConfigEntryNotReady),
        ):
            await integration.async_setup_entry(self.hass, config)
        self.assertEqual(self.hass.data.pop(integration.OWNER), config.entry_id)
        fake.closed = True
        fake.stop.side_effect = None
        with (
            self.network_boundary(),
            patch.object(integration, "Runtime", return_value=fake),
            self.assertRaises(ConfigEntryNotReady),
        ):
            await integration.async_setup_entry(self.hass, config)
        self.assertNotIn(integration.OWNER, self.hass.data)

    async def test_start_stop_hooks_and_failed_unload_keep_services_and_owner_consistent(self):
        await integration.async_setup(self.hass, {})
        await self.vault.put(self.key, self.record)
        config = fixtures.entry({"device_keys": [self.key]})
        self.hass.config_entries._entries[config.entry_id] = config
        started = []
        with (
            self.network_boundary(),
            patch.object(runtime_module, "HomeKitBackend", return_value=self.backend),
            patch.object(
                integration,
                "async_at_started",
                side_effect=lambda hass, callback: started.append(callback) or (lambda: None),
            ),
        ):
            await integration.async_setup_entry(self.hass, config)
            runtime = config.runtime_data
            with patch.object(runtime.coordinator, "async_refresh", AsyncMock()) as refresh:
                await started[0](self.hass)
                refresh.assert_awaited_once()
            with patch.object(self.hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)):
                self.assertFalse(await integration.async_unload_entry(self.hass, config))
                self.assertFalse(runtime.stopped)
            await self.hass.async_stop(force=True)
            self.assertTrue(runtime.closed)
            # A startup callback already queued before unload cannot restart
            # the watchdog or polling after the runtime has been stopped.
            with (
                patch.object(runtime.watchdog, "start") as start_watchdog,
                patch.object(runtime.coordinator, "async_refresh", AsyncMock()) as refresh,
            ):
                await started[0](self.hass)
                start_watchdog.assert_not_called()
                refresh.assert_not_awaited()
            with patch.object(self.hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)):
                await integration.async_unload_entry(self.hass, config)
            self.assertNotIn(integration.OWNER, self.hass.data)

    async def test_unloaded_device_removal_cleans_only_pinned_inventory_and_rejects_busy_owner(self):
        await self.vault.put(self.key, self.record)
        config = fixtures.entry(
            {
                "device_keys": [self.key],
                "pins": {self.key: public_targets([self.target])[0]},
                "admin_pins": {self.key: self.record["source_pin"]},
            }
        )
        self.hass.config_entries._entries[config.entry_id] = config
        invalid = SimpleNamespace(identifiers={})
        self.assertFalse(await integration.async_remove_config_entry_device(self.hass, config, invalid))
        device = SimpleNamespace(identifiers={(DOMAIN, self.key)})
        self.hass.data[integration.OWNER] = "other"
        self.assertFalse(await integration.async_remove_config_entry_device(self.hass, config, device))
        self.hass.data.pop(integration.OWNER)
        self.assertTrue(await integration.async_remove_config_entry_device(self.hass, config, device))
        self.assertEqual(config.data, {"device_keys": [], "pins": {}, "admin_pins": {}})
        self.assertEqual(self.vault.records(), {})
        await integration.async_remove_entry(self.hass, config)

    async def test_runtime_policy_state_and_private_storage_fail_closed(self):
        with self.assertRaises(SafetyFailure):
            Vault(self.hass).records()
        config, runtime = await self.make_runtime()
        try:
            for field, value in (("recovery", "enabled"), ("cancelled", "false")):
                record = deepcopy(self.record)
                if field == "recovery":
                    record["target"][field] = value
                else:
                    record[field] = value
                await self.vault.put(self.key, record)
                with self.assertRaises(SafetyFailure):
                    await runtime.initialize()
            record = deepcopy(self.record)
            record["phase"] = "needs_credentials"
            await self.vault.put(self.key, record)
            await runtime.initialize()
            self.assertEqual(runtime.engine.targets, [])
            record["phase"] = "verified"
            record["target"].pop("source")
            await self.vault.put(self.key, record)
            await runtime.initialize()
            self.assertEqual(runtime.records[self.key]["phase"], "blocked")
            with (
                patch.object(runtime.store, "async_load", AsyncMock(return_value=[])),
                self.assertRaises(SafetyFailure),
            ):
                await runtime.initialize()
        finally:
            await runtime.stop()

    async def test_pending_resume_policy_removal_and_native_freshness_paths(self):
        config, runtime = await self.make_runtime()
        try:
            runtime.records[self.key]["phase"] = "prepared"
            returned = deepcopy(runtime.records[self.key])
            with (
                patch.object(
                    runtime.provisioner,
                    "inspect",
                    AsyncMock(return_value=(returned, {"temperature": 20, "humidity": 40})),
                ),
                patch.object(runtime, "finish_publication", AsyncMock()) as finish,
            ):
                await runtime.resume_device(self.key)
                self.assertIn(self.key, runtime.readings)
                finish.assert_awaited_once()
            runtime.records[self.key]["target"].pop("source")
            self.assertFalse(await runtime.set_recovery(self.key, True))
            runtime.records[self.key]["target"]["source"] = self.source
            self.assertTrue(await runtime.set_recovery(self.key, True))
            runtime.records[self.key]["phase"] = "active"
            self.assertTrue(await runtime.set_recovery(self.key, False))
            self.assertNotIn("source", runtime.records[self.key]["target"])
            await runtime.remove_device("not-managed")
            runtime.entities[self.key] = {"temperature": "sensor.disposable_t", "humidity": "sensor.disposable_h"}
            runtime.readings[self.key] = (dt_util.utcnow(), {"temperature": 20, "humidity": 40})
            with self.assertRaises(OSError):
                await runtime.verify_publication(self.target)
            with (
                patch.object(runtime, "read_target", AsyncMock()),
                patch.object(runtime, "verify_publication", AsyncMock()) as verify,
            ):
                await runtime.refresh_publication(self.target)
                verify.assert_awaited_once()
            runtime.stopped = True
            await runtime.update()
        finally:
            await runtime.stop()

    async def test_failed_automatic_repair_alerts_and_successful_retry_clears_it_silently(self):
        _, runtime = await self.make_runtime()
        engine = runtime.engine
        failure_id = f"{DOMAIN}_{self.key}"
        updates = []

        @callback
        def record_update(update_type, notifications):
            updates.extend((update_type, notification_id) for notification_id in notifications)

        unsubscribe = persistent_notification.async_register_callback(self.hass, record_update)
        try:
            self.backend.restore_missing.side_effect = [OSError(), True]
            with (
                patch.object(engine, "clock") as clock,
                patch.object(engine, "refresh_publication", AsyncMock()),
                patch.object(engine, "verify_publication", AsyncMock()),
            ):
                for elapsed in (0, GRACE / 2, GRACE):
                    clock.return_value = engine.started + elapsed
                    await engine.check_all()
                await self.hass.async_block_till_done()
                self.assertEqual(engine.states[self.key].status, "repair_failed")
                self.assertEqual(updates, [(persistent_notification.UpdateType.ADDED, failure_id)])

                self.backend.read_ha.side_effect = [AuthenticationFailure(), {"temperature": 20, "humidity": 40}]
                clock.return_value += COOLDOWN
                await engine.check_all()
                await self.hass.async_block_till_done()
                self.assertEqual(engine.states[self.key].status, "healthy")
                self.assertEqual(engine.states[self.key].repairs, 1)
                self.assertEqual(
                    updates,
                    [
                        (persistent_notification.UpdateType.ADDED, failure_id),
                        (persistent_notification.UpdateType.REMOVED, failure_id),
                    ],
                )
        finally:
            unsubscribe()
            await runtime.stop()

    async def test_failure_notifications_deduplicate_and_clear_without_a_success_alert(self):
        _, runtime = await self.make_runtime()
        state = DeviceState(status="blocked", blocked=True, reason="identity_validation_failed")
        failure_id = f"{DOMAIN}_{self.key}"
        try:
            with (
                patch.object(runtime_module.persistent_notification, "async_create") as create,
                patch.object(runtime_module.persistent_notification, "async_dismiss") as dismiss,
            ):
                await runtime.notify(self.target, state, False)
                await runtime.notify(self.target, state, False)
                create.assert_called_once()
                self.assertEqual(create.call_args.kwargs["title"], "HomePod recovery needs attention")
                self.assertEqual(create.call_args.kwargs["notification_id"], failure_id)
                self.assertNotIn(self.target["ha_id"], str(create.call_args))
                state.status, state.last_repair, state.last_verified, state.repairs = "healthy", 1, 1, 1
                await runtime.notify(self.target, state, True)
                create.assert_called_once()
                dismiss.assert_called_once_with(self.hass, failure_id)
                state.recovery_status = "unreachable"
                await runtime.notify_access(self.target, state, False)
                await runtime.notify_access(self.target, state, False)
                self.assertEqual(create.call_count, 2)
                state.recovery_status = "verified"
                await runtime.notify_access(self.target, state, True)
                self.assertEqual(dismiss.call_count, 2)
                dismiss.assert_called_with(self.hass, f"{DOMAIN}_access_{self.key}")
        finally:
            await runtime.stop()
