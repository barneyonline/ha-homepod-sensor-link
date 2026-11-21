"""Optional name discovery must not delay fresh sensor startup."""

import asyncio
from contextlib import contextmanager
from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant import loader
from homeassistant.components import sensor as core_sensor
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.core import CoreState
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr, entity_registry as er, frame
from homeassistant.helpers.entity_platform import EntityPlatform, async_get_platforms
from homeassistant.helpers.start import async_at_started

from custom_components import homepod_sensor_link as integration
from custom_components.homepod_sensor_link import coordinator as runtime_module
from custom_components.homepod_sensor_link import config_flow, onboarding, sensor
from custom_components.homepod_sensor_link.credentials import DOMAIN
from custom_components.homepod_sensor_link.provisioning import make_owned
from custom_components.homepod_sensor_link.sensor import Measurement
from custom_components.homepod_sensor_link.engine import AuthenticationFailure
import test_onboarding_v2 as fixtures
import test_runtime_failures as lifecycle
from test_boundary_failures import native_config


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

    async def prepare_mixed_entry(self):
        config = await self.prepare_entry()
        peer = make_owned("synthetic-peer", self.target["accessory_key"], {})
        peer["recovery"] = False
        await self.vault.put(peer["entry_id"], {"target": peer, "phase": "active"})
        self.hass.config_entries.async_update_entry(config, data={"device_keys": [self.key, peer["entry_id"]]})
        return config, peer

    async def add_sensors(self, config, platform):
        entities = []
        await sensor.async_setup_entry(self.hass, config, entities.extend)
        await platform.async_add_entities(entities)

    async def test_healthy_reports_reach_ha_before_a_stalled_preflight_peer_finishes(self):
        self.enable_native_sensor_platform()
        config, peer = await self.prepare_mixed_entry()
        begun, forwarded, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        forward_platforms = self.hass.config_entries.async_forward_entry_setups

        async def read(target):
            if target["entry_id"] == peer["entry_id"]:
                begun.set()
                await release.wait()
                raise OSError()
            return {"temperature": 21, "humidity": 45}

        async def forward(*args):
            await forward_platforms(*args)
            forwarded.set()

        self.backend.read_ha.side_effect = read
        with (
            self.native_network_boundary() as controller,
            patch.object(self.hass.config_entries, "async_forward_entry_setups", AsyncMock(side_effect=forward)) as fwd,
        ):
            setup = asyncio.create_task(self.hass.config_entries.async_setup(config.entry_id))
            try:
                async with asyncio.timeout(2):
                    await begun.wait()
                    await forwarded.wait()
                self.assertFalse(setup.done())
                runtime = config.runtime_data
                await runtime.verify_publication(self.target)
                self.assertEqual(set(runtime.entities[self.key]), {"temperature", "humidity"})
                with self.assertRaises(OSError):
                    await runtime.verify_publication(peer)
                release.set()
                self.assertTrue(await asyncio.wait_for(setup, 2))
                fwd.assert_awaited_once()
                self.assertEqual(self.backend.read_ha.await_count, 2)
                self.assertEqual(runtime.engine.states[peer["entry_id"]].status, "unreachable")
                self.assertEqual(runtime.engine.states[self.key].auth_failures, 0)
                self.backend.restore_missing.assert_not_awaited()
                self.assertTrue(await self.hass.config_entries.async_unload(config.entry_id))
                self.assertFalse(async_get_platforms(self.hass, DOMAIN))
                self.assertEqual(runtime.measurement_listeners, {})
                self.assertEqual(runtime._startup_samples, {})
                self.assertNotIn(integration.OWNER, self.hass.data)
                controller.async_stop.assert_awaited_once()
            finally:
                release.set()
                await asyncio.gather(setup, return_exceptions=True)
                if config.state is ConfigEntryState.LOADED:
                    await self.hass.config_entries.async_unload(config.entry_id)

    async def test_failed_preflight_quiesces_queued_refresh_before_platform_teardown(self):
        config = await self.prepare_entry()
        self.hass.state = CoreState.running
        saving, fail_save = asyncio.Event(), asyncio.Event()
        refresh_waiting = asyncio.Event()
        unloading, finish_unload = asyncio.Event(), asyncio.Event()

        async def save(_runtime, _state):
            saving.set()
            await fail_save.wait()
            raise OSError("synthetic startup save failure")

        async def unload(*_args):
            unloading.set()
            await finish_unload.wait()
            return True

        with (
            self.network_boundary(),
            patch.object(runtime_module, "HomeKitBackend", return_value=self.backend),
            patch.object(runtime_module.Runtime, "save_state", save),
            patch.object(self.hass.config_entries, "async_unload_platforms", side_effect=unload),
        ):
            setup = asyncio.create_task(integration.async_setup_entry(self.hass, config))
            refresh = None
            try:
                await asyncio.wait_for(saving.wait(), 2)
                runtime = config.runtime_data
                check_all = runtime.engine.check_all

                async def check_when_unlocked(check_devices):
                    refresh_waiting.set()
                    await check_all(check_devices)

                with (
                    patch.object(runtime.engine, "check_all", side_effect=check_when_unlocked),
                    patch.object(runtime, "_check_devices", AsyncMock()) as check_devices,
                ):
                    refresh = asyncio.create_task(runtime.update())
                    await asyncio.wait_for(refresh_waiting.wait(), 2)
                    self.assertTrue(runtime.engine.lock.locked())
                    fail_save.set()
                    await asyncio.wait_for(unloading.wait(), 2)
                    await asyncio.wait_for(refresh, 2)
                    check_devices.assert_not_awaited()
                    self.assertTrue(runtime.stopped)

                finish_unload.set()
                with self.assertRaises(ConfigEntryNotReady):
                    await asyncio.wait_for(setup, 2)
            finally:
                fail_save.set()
                finish_unload.set()
                await asyncio.gather(setup, return_exceptions=True)
                if refresh is not None:
                    await asyncio.gather(refresh, return_exceptions=True)

    async def test_failed_setup_rolls_back_forwarding_and_drains_probes(self):
        for failure in ("forward", "journal", "cancel", "probe_cancelled", "rollback_refused", "rollback_failed"):
            with self.subTest(failure=failure):
                config, peer = await self.prepare_mixed_entry()
                platform = fixtures.sensor_platform(self.hass, config)
                probing, published, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()
                finish_probe, finish_forward = asyncio.Event(), asyncio.Event()
                unloading, finish_unload = asyncio.Event(), asyncio.Event()

                async def read(target):
                    if target["entry_id"] != peer["entry_id"]:
                        await probing.wait()
                        return {"temperature": 21, "humidity": 45}
                    probing.set()
                    try:
                        await finish_probe.wait()
                        if failure == "probe_cancelled":
                            raise asyncio.CancelledError
                        raise OSError()
                    finally:
                        exited.set()

                async def forward(*_args):
                    await self.add_sensors(config, platform)
                    published.set()
                    if failure == "forward":
                        raise OSError()
                    if failure == "cancel":
                        await finish_forward.wait()

                async def unload(*_args):
                    self.assertTrue(exited.is_set())
                    if failure == "cancel":
                        self.assertTrue(finish_forward.is_set())
                        unloading.set()
                        await finish_unload.wait()
                    if failure == "rollback_refused":
                        return False
                    if failure == "rollback_failed":
                        raise OSError()
                    await platform.async_reset()
                    return True

                self.backend.read_ha.side_effect = read
                rollback_failed = failure in ("rollback_refused", "rollback_failed")
                journal = OSError() if failure == "journal" or rollback_failed else None
                with (
                    self.network_boundary() as controller,
                    patch.object(runtime_module, "HomeKitBackend", return_value=self.backend),
                    patch.object(runtime_module.Runtime, "save_state", AsyncMock(side_effect=journal)),
                    patch.object(self.hass.config_entries, "async_forward_entry_setups", side_effect=forward),
                    patch.object(
                        self.hass.config_entries, "async_unload_platforms", side_effect=unload
                    ) as unload_sensors,
                ):
                    setup = asyncio.create_task(integration.async_setup_entry(self.hass, config))
                    try:
                        await asyncio.wait_for(published.wait(), 2)
                        error = ConfigEntryNotReady
                        if journal or failure == "probe_cancelled":
                            finish_probe.set()
                        if failure == "probe_cancelled":
                            error = asyncio.CancelledError
                        if failure == "cancel":
                            error = asyncio.CancelledError
                            setup.cancel()
                            await asyncio.wait_for(exited.wait(), 2)
                            self.assertFalse(setup.done())
                            finish_forward.set()
                            await asyncio.wait_for(unloading.wait(), 2)
                            setup.cancel()
                            await asyncio.sleep(0)
                            self.assertFalse(setup.done())
                            finish_unload.set()
                        with self.assertRaises(error):
                            await asyncio.wait_for(setup, 2)
                        unload_sensors.assert_awaited_once()
                        controller.async_stop.assert_awaited_once()
                        self.assertEqual(config.runtime_data._startup_samples, {})
                        self.assertTrue(config.runtime_data.closed)
                        if rollback_failed:
                            self.assertEqual(self.hass.data[integration.OWNER], config.entry_id)
                        else:
                            self.assertEqual(platform.entities, {})
                            self.assertNotIn(integration.OWNER, self.hass.data)
                    finally:
                        finish_forward.set()
                        finish_probe.set()
                        finish_unload.set()
                        await asyncio.gather(setup, return_exceptions=True)
                        await platform.async_reset()
                        await config._async_process_on_unload(self.hass)
                        self.hass.data.pop(integration.OWNER, None)
                        await self.vault.remove(peer["entry_id"])

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

    async def test_setup_retry_finishes_previous_rollback_before_forwarding(self):
        config = await self.prepare_entry()
        retry_owner = fixtures.entry({"device_keys": [self.key]})
        self.hass.config_entries._entries[retry_owner.entry_id] = retry_owner
        unload = AsyncMock(side_effect=[False, OSError(), False, True, True])
        with (
            self.network_boundary() as controller,
            patch.object(runtime_module, "HomeKitBackend", return_value=self.backend),
            patch.object(runtime_module.Runtime, "save_state", AsyncMock(side_effect=[OSError(), None])),
            patch.object(self.hass.config_entries, "async_forward_entry_setups", AsyncMock()) as forward,
            patch.object(self.hass.config_entries, "async_unload_platforms", unload),
        ):
            with self.assertRaises(ConfigEntryNotReady):
                await integration.async_setup_entry(self.hass, config)
            previous = config.runtime_data
            with self.assertRaises(ConfigEntryNotReady):
                await integration.async_setup_entry(self.hass, retry_owner)
            unload.assert_awaited_once()
            for _ in range(2):
                with self.assertRaises(ConfigEntryNotReady):
                    await integration.async_setup_entry(self.hass, config)
                forward.assert_awaited_once()
                controller.async_start.assert_awaited_once()
                self.assertIs(config.runtime_data, previous)
            try:
                self.assertTrue(await integration.async_setup_entry(self.hass, config))
                self.assertIsNot(config.runtime_data, previous)
                self.assertEqual(unload.await_count, 4)
                self.assertEqual(forward.await_count, 2)
                self.assertEqual(controller.async_start.await_count, 2)
                self.assertEqual(self.hass.data[integration.OWNER], config.entry_id)
                self.assertTrue(await integration.async_unload_entry(self.hass, config))
                self.assertNotIn(integration.OWNER, self.hass.data)
            finally:
                await config._async_process_on_unload(self.hass)
                await config.runtime_data.stop()

    async def test_setup_retry_finishes_the_previous_controller_shutdown(self):
        for forwarded in (False, True):
            with self.subTest(forwarded=forwarded):
                config = await self.prepare_entry()
                controller = SimpleNamespace(
                    async_start=AsyncMock(), async_stop=AsyncMock(side_effect=[OSError(), None, None])
                )
                journal = AsyncMock(side_effect=[OSError() if forwarded else None, None])
                if not forwarded:
                    self.backend.read_ha.side_effect = OSError()
                with (
                    self.network_boundary(controller),
                    patch.object(runtime_module, "HomeKitBackend", return_value=self.backend),
                    patch.object(runtime_module.Runtime, "save_state", journal),
                    patch.object(
                        self.hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
                    ) as unload,
                ):
                    with self.assertRaises(ConfigEntryNotReady):
                        await integration.async_setup_entry(self.hass, config)
                    previous = config.runtime_data
                    self.assertFalse(previous.closed)
                    self.assertEqual(self.hass.data[integration.OWNER], config.entry_id)
                    self.backend.read_ha.side_effect = None
                    try:
                        self.assertTrue(await integration.async_setup_entry(self.hass, config))
                        self.assertTrue(previous.closed)
                        self.assertIsNot(config.runtime_data, previous)
                        self.assertEqual(unload.await_count, int(forwarded))
                        self.assertEqual(controller.async_stop.await_count, 2)
                        self.assertTrue(await integration.async_unload_entry(self.hass, config))
                    finally:
                        await config._async_process_on_unload(self.hass)
                        await config.runtime_data.stop()

    async def test_cancelled_preflight_probe_cancels_and_drains_a_stalled_peer(self):
        _, runtime = await self.make_runtime(recovery=False)
        runtime.records[self.key]["phase"] = "active"
        peer = make_owned("disposable-stalled-peer", self.target["accessory_key"], {})
        runtime.records[peer["entry_id"]] = {"target": peer, "phase": "active"}
        runtime.sync_targets()
        entered, drained = asyncio.Event(), asyncio.Event()

        async def read(target):
            if target["entry_id"] == self.key:
                await entered.wait()
                raise asyncio.CancelledError
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()

        self.backend.read_ha.side_effect = read
        try:
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(runtime.preflight(), 2)
            self.assertTrue(drained.is_set())
            self.assertFalse(runtime.engine.lock.locked())
            self.backend.restore_missing.assert_not_awaited()
        finally:
            await runtime.stop()

    def enable_native_sensor_platform(self):
        (Path(self.directory.name) / "custom_components").symlink_to(Path(integration.__file__).resolve().parents[1])
        loader.async_setup(self.hass)
        frame.async_setup(self.hass)
        self.hass.config.skip_pip = True
        self.hass.config.components.update({"zeroconf", "http"})

    @contextmanager
    def native_network_boundary(self):
        controller = SimpleNamespace(async_start=AsyncMock(), async_stop=AsyncMock())
        with (
            patch.object(integration.zeroconf, "async_get_async_instance", AsyncMock()),
            patch.object(integration, "IpController", return_value=controller),
            patch.object(runtime_module, "HomeKitBackend", return_value=self.backend),
            patch.object(integration, "async_at_started", return_value=lambda: None),
        ):
            yield controller

    async def prepare_refused_native_rollback(self):
        config = await self.prepare_entry()
        with (
            self.native_network_boundary(),
            patch.object(runtime_module.Runtime, "save_state", AsyncMock(side_effect=OSError())),
            patch.object(self.hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)),
        ):
            self.assertFalse(await self.hass.config_entries.async_setup(config.entry_id))
        self.assertEqual(config.state, ConfigEntryState.SETUP_RETRY)
        self.assertEqual(self.hass.data[integration.OWNER], config.entry_id)
        [platform] = [p for p in async_get_platforms(self.hass, DOMAIN) if p.config_entry is config]
        self.assertEqual(len(platform.entities), 3)
        measurements = config.runtime_data.entities[self.key]
        self.assertEqual(set(measurements), {"temperature", "humidity"})
        for entity_id in measurements.values():
            self.assertEqual(self.hass.states.get(entity_id).state, "unavailable")
        return config, platform

    async def test_native_retry_clears_platform_left_registered_by_partial_teardown(self):
        self.enable_native_sensor_platform()
        config = await self.prepare_entry()
        other_platform = fixtures.sensor_platform(self.hass, fixtures.entry())
        other_platform.async_prepare()
        destroy = EntityPlatform.async_destroy
        failed = False

        async def fail_first_destroy(platform):
            nonlocal failed
            if platform.config_entry is config and not failed:
                failed = True
                raise OSError("disposable teardown failure")
            await destroy(platform)

        try:
            with (
                self.native_network_boundary(),
                patch.object(runtime_module.Runtime, "save_state", AsyncMock(side_effect=[OSError(), None])),
                patch.object(EntityPlatform, "async_destroy", fail_first_destroy),
            ):
                with self.assertLogs(level="ERROR"):
                    self.assertFalse(await self.hass.config_entries.async_setup(config.entry_id))
                self.assertEqual(config.state, ConfigEntryState.SETUP_RETRY)
                [old_platform] = [p for p in async_get_platforms(self.hass, DOMAIN) if p.config_entry is config]
                self.assertEqual(len(old_platform.entities), 3)
                await config.async_setup_locked(self.hass)
                self.assertEqual(config.state, ConfigEntryState.LOADED)
                self.assertEqual(old_platform.entities, {})
                registered = async_get_platforms(self.hass, DOMAIN)
                self.assertNotIn(old_platform, registered)
                self.assertIn(other_platform, registered)
                await config.runtime_data.verify_publication(self.target)
                self.assertTrue(await self.hass.config_entries.async_unload(config.entry_id))
                self.assertEqual(async_get_platforms(self.hass, DOMAIN), [other_platform])
        finally:
            await other_platform.async_destroy()

    async def test_native_unload_finishes_refused_startup_rollback(self):
        self.enable_native_sensor_platform()
        for operation in ("unload", "remove", "remove_with_retry"):
            with self.subTest(operation=operation):
                config, platform = await self.prepare_refused_native_rollback()
                if operation == "unload":
                    self.assertTrue(await self.hass.config_entries.async_unload(config.entry_id))
                elif operation == "remove":
                    self.assertEqual(
                        await self.hass.config_entries.async_remove(config.entry_id), {"require_restart": False}
                    )
                else:
                    entered, release = asyncio.Event(), asyncio.Event()
                    core_unload = core_sensor.async_unload_entry
                    first_attempt = True

                    async def retry_unload(*args):
                        nonlocal first_attempt
                        if first_attempt:
                            first_attempt = False
                            raise OSError()
                        entered.set()
                        await release.wait()
                        await core_unload(*args)

                    with patch.object(core_sensor, "async_unload_entry", side_effect=retry_unload):
                        try:
                            await self.hass.config_entries.async_remove(config.entry_id)
                            await asyncio.wait_for(entered.wait(), 2)
                            self.assertEqual(self.vault.records(), {})
                            self.assertEqual(self.hass.data[integration.OWNER], config.entry_id)
                            self.assertIn(platform, async_get_platforms(self.hass, DOMAIN))
                        finally:
                            release.set()
                            await self.hass.async_block_till_done()
                await self.hass.async_block_till_done()
                self.assertEqual(config.state, ConfigEntryState.NOT_LOADED)
                self.assertEqual(platform.entities, {})
                self.assertNotIn(platform, async_get_platforms(self.hass, DOMAIN))
                self.assertNotIn(integration.OWNER, self.hass.data)

    async def test_native_replacement_retries_teardown_after_removed_entry(self):
        self.enable_native_sensor_platform()
        config, platform = await self.prepare_refused_native_rollback()
        with patch.object(core_sensor, "async_unload_entry", AsyncMock(side_effect=OSError())) as unload:
            await self.hass.config_entries.async_remove(config.entry_id)
            await self.hass.async_block_till_done()
            self.assertEqual(unload.await_count, 2)
            self.assertEqual(self.vault.records(), {})
            self.assertEqual(self.hass.data[integration.OWNER], config.entry_id)
            self.assertIn(platform, async_get_platforms(self.hass, DOMAIN))
            self.assertIsNone(self.hass.config_entries.async_get_entry(config.entry_id))
            replacement = await self.prepare_entry()
            with self.native_network_boundary():
                self.assertFalse(await self.hass.config_entries.async_setup(replacement.entry_id))
            self.assertEqual(replacement.state, ConfigEntryState.SETUP_RETRY)
            self.assertFalse(hasattr(replacement, "runtime_data"))
            self.assertEqual(unload.await_count, 3)

        with self.native_network_boundary():
            await replacement.async_setup_locked(self.hass)
        self.assertEqual(replacement.state, ConfigEntryState.LOADED)
        self.assertNotIn(platform, async_get_platforms(self.hass, DOMAIN))
        self.assertEqual(self.hass.data[integration.OWNER], replacement.entry_id)
        await replacement.runtime_data.verify_publication(self.target)
        self.assertTrue(await self.hass.config_entries.async_unload(replacement.entry_id))

    async def test_native_removal_retries_incomplete_storage_cleanup_before_provisioning(self):
        self.enable_native_sensor_platform()
        for failed_stage in ("vault", "legacy_state"):
            with self.subTest(failed_stage=failed_stage):
                config, platform = await self.prepare_refused_native_rollback()
                purge = self.vault.purge
                failure_pending = True

                async def purge_vault():
                    if failed_stage == "vault" and failure_pending:
                        raise OSError()
                    await purge()

                async def remove_state():
                    if failed_stage == "legacy_state" and failure_pending:
                        raise OSError()

                store = SimpleNamespace(async_remove=AsyncMock(side_effect=remove_state))
                with (
                    patch.object(self.vault, "purge", side_effect=purge_vault) as purge_mock,
                    patch.object(integration, "Store", return_value=store),
                ):
                    with self.assertLogs(level="ERROR"):
                        await self.hass.config_entries.async_remove(config.entry_id)
                    await self.hass.async_block_till_done()
                    self.assertIsNone(self.hass.config_entries.async_get_entry(config.entry_id))
                    self.assertNotIn(platform, async_get_platforms(self.hass, DOMAIN))
                    self.assertEqual(self.hass.data[integration.OWNER], config.entry_id)
                    self.assertEqual(bool(self.vault.records()), failed_stage == "vault")

                    flow = config_flow.HomePodSensorLinkFlow()
                    flow.hass, flow.context, flow.flow_id, flow.handler = self.hass, {"source": "user"}, "flow", DOMAIN
                    with patch.object(config_flow, "controller_session") as session:
                        self.assertEqual(await flow._prepare(), [])
                        session.assert_not_called()

                    accessory_id = self.target["accessory_id"].lower()
                    request = fixtures.current_request.set(fixtures.Request(peer="127.0.0.1"))
                    try:
                        with patch.object(
                            config_flow, "discover_devices", AsyncMock(return_value={accessory_id: {"name": "HomePod"}})
                        ) as discover:
                            self.assertEqual((await flow.async_step_user())["reason"], "previous_cleanup_failed")
                            discover.assert_not_awaited()
                            failure_pending = False
                            self.assertEqual((await flow.async_step_user())["step_id"], "devices")
                            self.assertIn(accessory_id, flow._discovered)
                    finally:
                        fixtures.current_request.reset(request)
                    self.assertEqual(self.vault.records(), {})
                    self.assertNotIn(integration.OWNER, self.hass.data)
                    if failed_stage == "legacy_state":
                        purge_mock.assert_awaited_once()

                replacement = await self.prepare_entry()
                with self.native_network_boundary():
                    self.assertTrue(await self.hass.config_entries.async_setup(replacement.entry_id))
                await replacement.runtime_data.verify_publication(self.target)
                self.assertEqual(
                    await self.hass.config_entries.async_remove(replacement.entry_id), {"require_restart": False}
                )

    async def test_provisioning_waiter_rechecks_cleanup_replaced_during_removal(self):
        self.enable_native_sensor_platform()
        config, _ = await self.prepare_refused_native_rollback()
        removing, release = asyncio.Event(), asyncio.Event()
        remove = integration._remove_entry

        async def paused_removal(*args):
            removing.set()
            await release.wait()
            await remove(*args)

        flow = config_flow.HomePodSensorLinkFlow()
        flow.hass = self.hass
        with (
            patch.object(integration, "_remove_entry", side_effect=paused_removal),
            patch.object(self.vault, "purge", AsyncMock(side_effect=OSError())) as purge,
            patch.object(config_flow, "controller_session") as session,
        ):
            removal = asyncio.create_task(self.hass.config_entries.async_remove(config.entry_id))
            preparing = None
            try:
                await asyncio.wait_for(removing.wait(), 2)
                self.assertIsNone(self.hass.config_entries.async_get_entry(config.entry_id))
                preparing = asyncio.create_task(flow._prepare())
                await asyncio.sleep(0)
                self.assertFalse(preparing.done())
                with self.assertLogs(level="ERROR"):
                    release.set()
                    await asyncio.wait_for(removal, 2)
                self.assertEqual(await asyncio.wait_for(preparing, 2), [])
                session.assert_not_called()
                await self.hass.async_block_till_done()
                self.assertEqual(purge.await_count, 3)
                self.assertIn(self.key, self.vault.records())
                self.assertEqual(self.hass.data[integration.OWNER], config.entry_id)
            finally:
                release.set()
                await asyncio.gather(removal, *([preparing] if preparing else []), return_exceptions=True)
        self.assertTrue(await integration.async_finish_removed_entry_cleanup(self.hass))

    async def test_native_removal_drains_rollback_and_vault_purge_when_cancelled(self):
        self.enable_native_sensor_platform()
        config, platform = await self.prepare_refused_native_rollback()
        other = native_config({})
        self.hass.config_entries._entries[other.entry_id] = other
        devices, entities = dr.async_get(self.hass), er.async_get(self.hass)
        native_device = devices.async_get_or_create(
            config_entry_id=other.entry_id, identifiers={(other.domain, "disposable-foreign")}
        )
        foreign = entities.async_get_or_create(
            "sensor", "other_platform", "foreign", config_entry=other, device_id=native_device.id
        )
        self.assertEqual(len(er.async_entries_for_config_entry(entities, config.entry_id)), 3)
        store = self.hass.config_entries._store
        await store.async_save(self.hass.config_entries._data_to_save())
        self.assertIn(config.entry_id, Path(store.path).read_text())
        entered, release = asyncio.Event(), asyncio.Event()
        core_unload = core_sensor.async_unload_entry

        async def blocked_unload(*args):
            entered.set()
            await release.wait()
            await core_unload(*args)

        with patch.object(core_sensor, "async_unload_entry", side_effect=blocked_unload):
            removal = asyncio.create_task(self.hass.config_entries.async_remove(config.entry_id))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertIsNone(self.hass.config_entries.async_get_entry(config.entry_id))
                self.assertTrue(self.vault.records())
                for _ in range(2):
                    removal.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(removal.done())
                release.set()
                self.assertEqual(await removal, {"require_restart": False})
            finally:
                release.set()
                await asyncio.gather(removal, return_exceptions=True)
        await self.hass.async_block_till_done()
        self.assertEqual(self.vault.records(), {})
        self.assertNotIn(integration.OWNER, self.hass.data)
        self.assertNotIn(platform, async_get_platforms(self.hass, DOMAIN))
        self.assertEqual(er.async_entries_for_config_entry(entities, config.entry_id), [])
        self.assertEqual(dr.async_entries_for_config_entry(devices, config.entry_id), [])
        self.assertEqual(devices.async_get(native_device.id).config_entry_id, other.entry_id)
        self.assertIsNotNone(entities.async_get(foreign.entity_id))
        self.hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
        await self.hass.async_block_till_done()
        self.assertEqual(
            [row["entry_id"] for row in json.loads(Path(store.path).read_text())["data"]["entries"]], [other.entry_id]
        )

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
        config, _ = await self.prepare_mixed_entry()
        self.backend.read_ha.side_effect = OSError()
        with (
            self.network_boundary() as controller,
            patch.object(runtime_module, "HomeKitBackend", return_value=self.backend),
            patch.object(self.hass.config_entries, "async_forward_entry_setups", AsyncMock()) as forward,
            patch.object(integration, "refresh_names", AsyncMock()) as names,
            self.assertRaises(ConfigEntryNotReady),
        ):
            await integration.async_setup_entry(self.hass, config)
        self.assertEqual(self.backend.read_ha.await_count, 2)
        self.assertEqual(config.runtime_data.entities, {})
        self.assertEqual(config.runtime_data._startup_samples, {})
        controller.async_stop.assert_awaited_once()
        forward.assert_not_awaited()
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
            platform = fixtures.sensor_platform(self.hass, config)
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
