"""Finish runtime coverage at cancellation, native publication and shared registry seams."""

import asyncio
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components import homepod_sensor_link as integration
from custom_components.homepod_sensor_link import coordinator
from custom_components.homepod_sensor_link.credentials import DOMAIN
from custom_components.homepod_sensor_link.engine import AuthenticationFailure, SafetyFailure
from custom_components.homepod_sensor_link.provisioning import make_owned
import test_onboarding_v2 as fixtures
from test_boundary_failures import native_config


class RuntimeCompletionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.OnboardingTests.asyncSetUp
    asyncTearDown = fixtures.OnboardingTests.asyncTearDown
    make_runtime = fixtures.OnboardingTests.make_runtime

    async def test_active_sample_publishes_while_pending_read_waits(self):
        _, runtime = await self.make_runtime(recovery=False)
        active = runtime.records[self.key]
        active["phase"] = "active"
        pending = make_owned("synthetic-pending", self.target["accessory_key"], {})
        pending["recovery"] = False
        pending_key = pending["entry_id"]
        record = {"target": pending, "phase": "prepared"}
        await self.vault.put(self.key, active)
        await self.vault.put(pending_key, record)
        runtime.records[pending_key] = record
        runtime.sync_targets()
        pending_started, release, published = asyncio.Event(), asyncio.Event(), asyncio.Event()
        runtime.listen_measurements(self.key, published.set)
        runtime.engine.verify_publication = AsyncMock()

        async def read(target):
            if target["entry_id"] == pending_key:
                pending_started.set()
                await release.wait()
                raise OSError()
            await pending_started.wait()
            return {"temperature": 21, "humidity": 45}

        self.backend.read_ha.side_effect = read
        poll = asyncio.create_task(runtime.update())
        try:
            await asyncio.wait_for(published.wait(), 2)
            self.assertFalse(poll.done())
            self.assertTrue(runtime.engine.lock.locked())
            self.assertEqual(runtime.readings[self.key][1]["temperature"], 21)
            release.set()
            await asyncio.wait_for(poll, 2)
            self.assertEqual(self.backend.read_ha.await_count, 2)
            self.assertEqual(runtime.pending_tasks, {})
            self.assertEqual((await runtime.store.async_load())[self.key]["status"], "healthy")
        finally:
            release.set()
            await asyncio.gather(poll, return_exceptions=True)
            await runtime.stop()

    async def test_pending_journal_failure_does_not_interrupt_reserved_active_repair(self):
        _, runtime = await self.make_runtime()
        active = runtime.records[self.key]
        active["phase"] = "active"
        await self.vault.put(self.key, active)
        pending = make_owned("synthetic-pending", self.target["accessory_key"], {})
        key = pending["entry_id"]
        record = {"target": pending, "phase": "prepared"}
        await self.vault.put(key, record)
        runtime.records[key] = record
        runtime.sync_targets()
        runtime.engine.started = 0
        runtime.engine.clock = lambda: 900
        state = runtime.engine.states[self.key]
        state.auth_failures, state.auth_since = 2, 0
        runtime.engine.verify_publication = AsyncMock()
        runtime.verify_publication = AsyncMock()
        reserved, failed, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        put = self.vault.put

        async def read(target):
            if target["entry_id"] == key:
                await reserved.wait()
                raise OSError()
            if not release.is_set():
                raise AuthenticationFailure()
            return {"temperature": 21, "humidity": 45}

        async def repair(_target):
            self.assertEqual((await runtime.store.async_load())[self.key]["attempts"], [900])
            reserved.set()
            await release.wait()
            return True

        async def put_record(device_key, record):
            if device_key == key:
                failed.set()
                raise OSError()
            return await put(device_key, record)

        self.backend.read_ha.side_effect = read
        self.backend.restore_missing.side_effect = repair
        with patch.object(self.vault, "put", side_effect=put_record):
            poll = asyncio.create_task(runtime.update())
            try:
                await asyncio.wait_for(failed.wait(), 2)
                self.assertFalse(poll.done())
                release.set()
                with self.assertRaises(UpdateFailed):
                    await asyncio.wait_for(poll, 2)
                saved = (await runtime.store.async_load())[self.key]
                self.assertEqual((saved["attempts"], saved["repairs"], saved["publication_pending"]), ([900], 1, False))
                self.assertEqual(self.vault.record(key), record)
                self.assertEqual(runtime.pending_tasks, {})
            finally:
                release.set()
                await asyncio.gather(poll, return_exceptions=True)
                await runtime.stop()

    async def test_cancel_interrupts_verified_probe_without_same_poll_retry(self):
        _, runtime = await self.make_runtime(recovery=False)
        active = deepcopy(runtime.records[self.key])
        active_target = make_owned("synthetic-active", self.target["accessory_key"], {})
        active_target["recovery"] = False
        active_key = active_target["entry_id"]
        active.update(target=active_target, phase="active")
        await self.vault.put(active_key, active)
        runtime.records[active_key] = active
        runtime.sync_targets()
        pending_started, pending_exited, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        runtime.engine.verify_publication = AsyncMock()

        async def read(target):
            if target["entry_id"] == self.key:
                pending_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    pending_exited.set()
            await release.wait()
            return {"temperature": 21, "humidity": 45}

        self.backend.read_ha.side_effect = read
        poll = asyncio.create_task(runtime.update())
        cancel = None
        try:
            await asyncio.wait_for(pending_started.wait(), 2)
            cancel = asyncio.create_task(runtime.cancel_device(self.key))
            await asyncio.wait_for(pending_exited.wait(), 2)
            self.assertFalse(poll.done())
            release.set()
            await asyncio.wait_for(asyncio.gather(poll, cancel), 2)
            self.assertEqual(self.backend.read_ha.await_count, 2)
            self.assertTrue(self.vault.record(self.key)["cancelled"])
            self.assertNotIn("authorised_boot", self.vault.record(self.key))
            self.assertEqual(runtime.pending_tasks, {})
        finally:
            release.set()
            poll.cancel()
            await asyncio.gather(poll, *([cancel] if cancel else []), return_exceptions=True)
            await runtime.stop()

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
                await runtime.verify_publication(self.target)
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
