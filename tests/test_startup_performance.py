"""Optional name discovery must not delay fresh sensor startup."""

import asyncio
from datetime import timedelta
import logging
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.core import CoreState
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.entity_platform import EntityPlatform

from custom_components import homepod_sensor_link as integration
from custom_components.homepod_sensor_link import coordinator as runtime_module
from custom_components.homepod_sensor_link import onboarding
from custom_components.homepod_sensor_link.credentials import DOMAIN
from custom_components.homepod_sensor_link.sensor import Measurement
from custom_components.homepod_sensor_link.engine import AuthenticationFailure
import test_onboarding_v2 as fixtures
import test_runtime_failures as lifecycle


class StartupPerformanceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.OnboardingTests.asyncSetUp
    asyncTearDown = fixtures.OnboardingTests.asyncTearDown
    network_boundary = lifecycle.RuntimeFailureTests.network_boundary
    make_runtime = fixtures.OnboardingTests.make_runtime

    async def prepare_entry(self):
        self.record["phase"] = "active"
        await self.vault.put(self.key, self.record)
        config = fixtures.entry({"device_keys": [self.key]})
        self.hass.config_entries._entries[config.entry_id] = config
        self.backend.read_ha.side_effect = None
        self.backend.read_ha.return_value = {"temperature": 21, "humidity": 45}
        return config

    async def test_optional_name_discovery_does_not_block_setup(self):
        config = await self.prepare_entry()
        release = asyncio.Event()

        async def slow_names(*_args, **_kwargs):
            await release.wait()

        with (
            self.network_boundary(),
            patch.object(runtime_module, "HomeKitBackend", return_value=self.backend),
            patch.object(integration, "discover_devices", AsyncMock(side_effect=slow_names)) as names,
        ):
            try:
                self.assertTrue(await asyncio.wait_for(integration.async_setup_entry(self.hass, config), 1))
                names.assert_not_awaited()
                self.backend.read_ha.assert_awaited_once()
                self.assertEqual(config.runtime_data.readings[self.key][1], {"temperature": 21, "humidity": 45})
            finally:
                release.set()
                runtime = getattr(config, "runtime_data", None)
                if runtime:
                    await runtime.stop()

    async def test_cold_start_runs_names_in_background_and_shutdown_drains_them(self):
        config = await self.prepare_entry()
        self.hass.state = CoreState.not_running
        begun, cancelled = asyncio.Event(), asyncio.Event()

        async def slow_names(*_args, **_kwargs):
            begun.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with (
            self.network_boundary(),
            patch.object(runtime_module, "HomeKitBackend", return_value=self.backend),
            patch.object(integration, "async_at_started", async_at_started),
            patch.object(integration, "discover_devices", AsyncMock(side_effect=slow_names)) as names,
        ):
            await integration.async_setup_entry(self.hass, config)
            runtime = config.runtime_data
            names.assert_not_awaited()
            with patch.object(runtime.coordinator, "async_refresh", AsyncMock()) as refresh:
                await asyncio.wait_for(self.hass.async_start(), 1)
                await asyncio.wait_for(begun.wait(), 1)
                refresh.assert_awaited_once()
            self.assertFalse(runtime.name_refresh_task.done())
            await runtime.stop()
            self.assertTrue(cancelled.is_set())
            self.assertTrue(runtime.name_refresh_task.done())
            self.assertTrue(runtime.closed)

    async def test_name_refresh_failure_and_stop_during_initial_refresh_are_safe(self):
        for stop_during_refresh in (False, True):
            with self.subTest(stop_during_refresh=stop_during_refresh):
                config = await self.prepare_entry()
                started = []
                with (
                    self.network_boundary(),
                    patch.object(runtime_module, "HomeKitBackend", return_value=self.backend),
                    patch.object(
                        integration,
                        "async_at_started",
                        side_effect=lambda hass, callback: started.append(callback) or (lambda: None),
                    ),
                    patch.object(integration, "refresh_names", AsyncMock(side_effect=OSError())) as names,
                ):
                    await integration.async_setup_entry(self.hass, config)
                    runtime = config.runtime_data

                    async def refresh():
                        if stop_during_refresh:
                            await runtime.stop()

                    with patch.object(runtime.coordinator, "async_refresh", side_effect=refresh):
                        await started[0](self.hass)
                    if stop_during_refresh:
                        self.assertIsNone(runtime.name_refresh_task)
                        names.assert_not_awaited()
                    else:
                        await runtime.name_refresh_task
                        names.assert_awaited_once()
                    await runtime.stop()
                self.hass.data.pop(integration.OWNER)

    async def test_unreachable_sensor_still_prevents_setup_without_name_discovery(self):
        config = await self.prepare_entry()
        self.backend.read_ha.side_effect = OSError()
        with (
            self.network_boundary(),
            patch.object(runtime_module, "HomeKitBackend", return_value=self.backend),
            patch.object(integration, "refresh_names", AsyncMock()) as names,
            self.assertRaises(ConfigEntryNotReady),
        ):
            await integration.async_setup_entry(self.hass, config)
        names.assert_not_awaited()
        self.backend.restore_missing.assert_not_awaited()
        self.assertNotIn(integration.OWNER, self.hass.data)

    async def test_stop_during_initial_refresh_does_not_launch_discovery_after_close(self):
        config = await self.prepare_entry()
        started = []

        async def discover(*_args):
            await asyncio.Event().wait()

        with (
            self.network_boundary(),
            patch.object(runtime_module, "HomeKitBackend", return_value=self.backend),
            patch.object(
                integration, "async_at_started", side_effect=lambda hass, cb: started.append(cb) or (lambda: None)
            ),
            patch.object(integration, "discover_devices", AsyncMock(side_effect=discover)) as discovery,
        ):
            await integration.async_setup_entry(self.hass, config)
            runtime = config.runtime_data
            try:
                with patch.object(runtime.coordinator, "async_refresh", side_effect=runtime.stop):
                    await started[0](self.hass)
                self.assertTrue(runtime.closed)
                discovery.assert_not_awaited()
                self.assertIsNone(runtime.name_refresh_task)
            finally:
                if runtime.name_refresh_task is not None:
                    runtime.name_refresh_task.cancel()
                    await asyncio.gather(runtime.name_refresh_task, return_exceptions=True)
                await runtime.stop()

    async def test_late_discovery_reply_during_stop_cannot_update_names(self):
        config = await self.prepare_entry()
        begun = asyncio.Event()
        started = []

        async def discover(*_args):
            begun.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return {self.target["accessory_id"]: {"friendly_name": "Late Test HomePod"}}

        with (
            self.network_boundary(),
            patch.object(runtime_module, "HomeKitBackend", return_value=self.backend),
            patch.object(
                integration, "async_at_started", side_effect=lambda hass, cb: started.append(cb) or (lambda: None)
            ),
            patch.object(integration, "discover_devices", AsyncMock(side_effect=discover)),
            patch.object(integration, "refresh_names", AsyncMock()) as refresh_names,
        ):
            await integration.async_setup_entry(self.hass, config)
            runtime = config.runtime_data
            try:
                with patch.object(runtime.coordinator, "async_refresh", AsyncMock()):
                    await started[0](self.hass)
                await asyncio.wait_for(begun.wait(), 1)
                await asyncio.wait_for(runtime.stop(), 1)
                self.assertTrue(runtime.closed)
                self.assertTrue(runtime.name_refresh_task.done())
                refresh_names.assert_not_awaited()
                self.assertEqual(self.vault.record(self.key)["name"], self.record["name"])
            finally:
                await runtime.stop()

    async def test_recent_preflight_is_used_only_for_initial_refresh(self):
        config = await self.prepare_entry()
        self.hass.state = CoreState.not_running
        with (
            self.network_boundary(),
            patch.object(runtime_module, "HomeKitBackend", return_value=self.backend),
            patch.object(integration, "async_at_started", async_at_started),
        ):
            await integration.async_setup_entry(self.hass, config)
            runtime = config.runtime_data
            platform = EntityPlatform(
                hass=self.hass,
                logger=logging.getLogger(__name__),
                domain="sensor",
                platform_name=DOMAIN,
                platform=None,
                scan_interval=timedelta(seconds=300),
                entity_namespace=None,
            )
            platform.config_entry = config
            try:
                await platform.async_add_entities(
                    [Measurement(runtime, runtime.targets[0], kind) for kind in ("temperature", "humidity")]
                )
                sample_time = runtime.readings[self.key][0]
                await self.hass.async_start()
                await self.hass.async_block_till_done()
                await runtime.name_refresh_task
                self.backend.read_ha.assert_awaited_once()
                self.assertEqual(runtime.readings[self.key][0], sample_time)
                self.assertEqual(runtime.engine.states[self.key].status, "healthy")
                await runtime.coordinator.async_refresh()
                self.assertEqual(self.backend.read_ha.await_count, 2)
                self.backend.restore_missing.assert_not_awaited()
            finally:
                await platform.async_reset()
                await runtime.stop()

    async def test_name_discovery_does_not_hold_the_inventory_lock(self):
        config = await self.prepare_entry()
        begun, release = asyncio.Event(), asyncio.Event()
        started = []

        async def discover(*_args):
            begun.set()
            await release.wait()
            return {self.target["accessory_id"]: {"friendly_name": "Renamed Test HomePod"}}

        with (
            self.network_boundary(),
            patch.object(runtime_module, "HomeKitBackend", return_value=self.backend),
            patch.object(
                integration, "async_at_started", side_effect=lambda hass, cb: started.append(cb) or (lambda: None)
            ),
            patch.object(integration, "discover_devices", AsyncMock(side_effect=discover)),
            patch.object(onboarding, "discover_devices", AsyncMock(side_effect=AssertionError("duplicate discovery"))),
            patch.object(integration, "refresh_names", onboarding.refresh_names),
        ):
            await integration.async_setup_entry(self.hass, config)
            runtime = config.runtime_data
            try:
                with patch.object(runtime.coordinator, "async_refresh", AsyncMock()):
                    await started[0](self.hass)
                await asyncio.wait_for(begun.wait(), 1)
                self.assertFalse(runtime.engine.lock.locked())
                await asyncio.wait_for(runtime.remove_device(self.key), 1)
                release.set()
                await runtime.name_refresh_task
                self.assertNotIn(self.key, self.vault.records())
                self.assertNotIn(self.key, runtime.records)
            finally:
                release.set()
                await runtime.stop()

    async def test_name_discovery_preserves_policy_changes_made_while_browsing(self):
        config = await self.prepare_entry()
        begun, release = asyncio.Event(), asyncio.Event()
        started = []

        async def discover(*_args):
            begun.set()
            await release.wait()
            return {self.target["accessory_id"]: {"friendly_name": "Renamed Test HomePod"}}

        with (
            self.network_boundary(),
            patch.object(runtime_module, "HomeKitBackend", return_value=self.backend),
            patch.object(
                integration, "async_at_started", side_effect=lambda hass, cb: started.append(cb) or (lambda: None)
            ),
            patch.object(integration, "discover_devices", AsyncMock(side_effect=discover)),
            patch.object(integration, "refresh_names", onboarding.refresh_names),
        ):
            await integration.async_setup_entry(self.hass, config)
            runtime = config.runtime_data
            try:
                with patch.object(runtime.coordinator, "async_refresh", AsyncMock()):
                    await started[0](self.hass)
                await asyncio.wait_for(begun.wait(), 1)
                self.assertTrue(await asyncio.wait_for(runtime.set_recovery(self.key, False), 1))
                release.set()
                await runtime.name_refresh_task
                record = self.vault.record(self.key)
                self.assertEqual(record["name"], "Renamed Test HomePod")
                self.assertFalse(record["target"]["recovery"])
                self.assertNotIn("source", record["target"])
            finally:
                release.set()
                await runtime.stop()

    async def test_expired_changed_missing_and_pending_preflight_samples_are_read_again(self):
        for reason in ("expired", "future", "identity_changed", "missing", "pending", "replaced", "no_preflight"):
            with self.subTest(reason=reason):
                config, runtime = await self.make_runtime()
                self.backend.read_ha.reset_mock()
                self.backend.read_ha.side_effect = None
                self.backend.read_ha.return_value = {"temperature": 21, "humidity": 45}
                target = runtime.engine.targets[0]
                try:
                    if reason != "no_preflight":
                        await runtime.preflight()
                        before = runtime.readings[self.key][0]
                    else:
                        before = runtime_module.dt_util.utcnow()
                    now = before
                    if reason == "expired":
                        now += timedelta(seconds=runtime_module.STARTUP_SAMPLE_MAX_AGE)
                    elif reason == "future":
                        now -= timedelta(seconds=1)
                    elif reason == "identity_changed":
                        target["pairing"]["AccessoryPort"] = 23456
                    elif reason == "missing":
                        runtime.readings.clear()
                    elif reason == "pending":
                        runtime.engine.states[self.key].publication_pending = True
                    elif reason == "replaced":
                        runtime.readings[self.key] = (
                            before + timedelta(seconds=1),
                            {"temperature": 22, "humidity": 46},
                        )
                    initial_reads = self.backend.read_ha.await_count
                    with (
                        patch.object(runtime_module.dt_util, "utcnow", return_value=now),
                        patch.object(runtime.engine, "verify_publication", AsyncMock()),
                    ):
                        await runtime.startup_refresh()
                    self.assertGreater(self.backend.read_ha.await_count, initial_reads)
                    self.assertEqual(runtime._startup_samples, {})
                    self.assertFalse(runtime._use_startup_samples)
                    self.backend.restore_missing.assert_not_awaited()
                finally:
                    await runtime.stop()

    async def test_preflight_failure_is_not_reused_or_counted_twice_as_authentication_evidence(self):
        config, runtime = await self.make_runtime()
        try:
            runtime.records[self.key]["phase"] = "active"
            self.backend.read_ha.reset_mock()
            self.backend.read_ha.side_effect = AuthenticationFailure()
            await runtime.preflight()
            self.assertEqual(runtime.engine.states[self.key].auth_failures, 0)
            await runtime.startup_refresh()
            self.assertEqual(self.backend.read_ha.await_count, 2)
            self.assertEqual(runtime.engine.states[self.key].auth_failures, 1)
            self.backend.restore_missing.assert_not_awaited()
        finally:
            await runtime.stop()

    async def test_missing_entity_reports_do_not_turn_preflight_into_healthy_publication(self):
        config, runtime = await self.make_runtime()
        try:
            self.backend.read_ha.reset_mock()
            self.backend.read_ha.side_effect = None
            self.backend.read_ha.return_value = {"temperature": 21, "humidity": 45}
            await runtime.preflight()
            await runtime.startup_refresh()
            self.assertEqual(runtime.engine.states[self.key].status, "publication_pending")
            self.assertGreater(self.backend.read_ha.await_count, 1)
            self.backend.restore_missing.assert_not_awaited()
        finally:
            await runtime.stop()

    async def test_cancelled_initial_refresh_discards_preflight_reuse(self):
        config, runtime = await self.make_runtime()
        try:
            self.backend.read_ha.side_effect = None
            self.backend.read_ha.return_value = {"temperature": 21, "humidity": 45}
            await runtime.preflight()
            with (
                patch.object(runtime.coordinator, "async_refresh", AsyncMock(side_effect=asyncio.CancelledError)),
                self.assertRaises(asyncio.CancelledError),
            ):
                await runtime.startup_refresh()
            self.assertEqual(runtime._startup_samples, {})
            self.assertFalse(runtime._use_startup_samples)
        finally:
            await runtime.stop()

    async def test_clock_correction_cannot_extend_preflight_sample_lifetime(self):
        config, runtime = await self.make_runtime()
        runtime.records[self.key]["phase"] = "active"
        self.backend.read_ha.reset_mock()
        self.backend.read_ha.side_effect = None
        self.backend.read_ha.return_value = {"temperature": 21, "humidity": 45}
        wall_clock = runtime_module.dt_util.utcnow()
        loop_clock = self.hass.loop.time
        elapsed = [0]
        try:
            with (
                patch.object(runtime_module.dt_util, "utcnow", return_value=wall_clock),
                patch.object(self.hass.loop, "time", side_effect=lambda: loop_clock() + elapsed[0]),
                patch.object(runtime.engine, "verify_publication", AsyncMock()),
            ):
                await runtime.preflight()
                elapsed[0] = runtime_module.STARTUP_SAMPLE_MAX_AGE + 1
                await runtime.startup_refresh()
                self.assertEqual(self.backend.read_ha.await_count, 2)
        finally:
            await runtime.stop()
