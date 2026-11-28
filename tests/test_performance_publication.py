"""Operation-count benchmarks with real HA reports and device churn."""

import asyncio
from copy import deepcopy
from datetime import timedelta
import unittest
from unittest.mock import Mock, patch

from homeassistant.components import persistent_notification
from homeassistant.util import dt as dt_util

from custom_components.homepod_sensor_link import coordinator, sensor, vault as vault_module
from custom_components.homepod_sensor_link.credentials import public_targets
from custom_components.homepod_sensor_link.engine import AuthenticationFailure, INTERVAL, WINDOW
from custom_components.homepod_sensor_link.entity import DeviceSensor
from custom_components.homepod_sensor_link.provisioning import make_owned
from custom_components.homepod_sensor_link.vault import Vault
import test_onboarding_v2 as fixtures


class PublicationPerformanceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.OnboardingTests.asyncSetUp
    asyncTearDown = fixtures.OnboardingTests.asyncTearDown
    make_runtime = fixtures.OnboardingTests.make_runtime

    async def test_verified_activation_scales_without_skipping_durable_checkpoints(self):
        config, runtime = await self.make_runtime(recovery=False)
        try:
            for count in (1, 16, 64):
                with self.subTest(devices=count):
                    await self.check_activation_counts(config, runtime, count)
        finally:
            await runtime.stop()

    async def test_activation_after_publication_retry_still_batches_ready_devices(self):
        config, runtime = await self.make_runtime(recovery=False)
        try:
            await self.check_activation_counts(config, runtime, 16, retry_publication=True)
        finally:
            await runtime.stop()

    async def test_post_pairing_reads_batch_verification_after_individual_checkpoints(self):
        count = 16
        records = {}
        for index in range(count):
            target = make_owned(f"disposable-{index}", self.target["accessory_key"], {})
            target["source"] = self.source
            records[target["entry_id"]] = {**self.record, "target": target}
        self.vault.data["records"] = deepcopy(records)
        await self.vault.save_states({})
        reads = dict.fromkeys(records, 0)
        awaiting_verification = set()
        ready = asyncio.Event()

        async def read(target):
            key = target["entry_id"]
            reads[key] += 1
            if reads[key] == 1:
                raise AuthenticationFailure()
            self.assertEqual(self.vault.record(key)["phase"], "accepted")
            awaiting_verification.add(key)
            if len(awaiting_verification) == count:
                ready.set()
            await ready.wait()
            return {"temperature": 21, "humidity": 45}

        async def restore(target):
            self.assertEqual(self.vault.record(target["entry_id"])["phase"], "write_uncertain")
            return True

        self.backend.read_ha.side_effect = read
        self.backend.restore_missing.side_effect = restore
        with patch.object(vault_module, "_write", wraps=vault_module._write) as write:
            results = await asyncio.wait_for(asyncio.gather(*(self.provisioner.inspect(key) for key in records)), 10)
        self.assertEqual(write.call_count, 2 * count + 1)
        self.assertEqual(reads, dict.fromkeys(records, 2))
        self.assertTrue(all(record["phase"] == "verified" and readings for record, readings in results))
        durable = (await Vault(self.hass).load()).records()
        self.assertEqual({key: record["phase"] for key, record in durable.items()}, dict.fromkeys(records, "verified"))

    async def check_activation_counts(self, config, runtime, count, *, retry_publication=False):
        records = {}
        for index in range(count):
            target = make_owned(f"disposable-{index}", self.target["accessory_key"], {})
            target["recovery"] = False
            records[target["entry_id"]] = {"target": target, "phase": "prepared"}
        self.vault.data["records"] = deepcopy(records)
        await self.vault.save_states({})
        runtime.records = records
        runtime.engine.states.clear()
        runtime.saved.clear()
        runtime.sync_targets()
        for state in runtime.engine.states.values():
            state.auth_since, state.auth_failures = 1, 2
            state.attempts = [runtime.engine.clock() - WINDOW - 1]
            state.publication_attempts = [1]
            state.last_error_type, state.last_error_location = "AuthenticationFailure", "test"
        platform = fixtures.sensor_platform(self.hass, config)
        await platform.async_add_entities(
            [
                sensor.Measurement(runtime, target, kind)
                for target in runtime.targets
                for kind in ("temperature", "humidity")
            ]
        )
        self.backend.read_ha.reset_mock(side_effect=True, return_value=True)
        self.backend.read_ha.return_value = {"temperature": 21, "humidity": 45}
        record_copies: list[int] = []
        snapshots = []
        write = vault_module._write
        verify = runtime.verify_publication
        checked = set()

        def record_checkpoint(config_dir, data):
            write(config_dir, data)
            snapshots.append({key: record["phase"] for key, record in data["records"].items()})

        async def verify_checkpoint(target):
            key = target["entry_id"]
            if retry_publication and key not in checked:
                checked.add(key)
                raise OSError("sensor_entities_not_ready")
            await verify(target)

        def count_record_copy(value):
            if isinstance(value, dict):
                inventory = value.get("records", value)
                if "target" in inventory and "phase" in inventory:
                    record_copies.append(1)
                else:
                    record_copies.extend(
                        1
                        for item in inventory.values()
                        if isinstance(item, dict) and {"target", "phase"} <= item.keys()
                    )
            return deepcopy(value)

        try:
            with (
                patch.object(vault_module, "deepcopy", side_effect=count_record_copy),
                patch.object(vault_module, "_write", side_effect=record_checkpoint) as committed,
                patch.object(vault_module.os, "fsync", wraps=vault_module.os.fsync) as fsync,
                patch.object(runtime, "verify_publication", side_effect=verify_checkpoint),
            ):
                snapshot = await runtime.update()
                self.assertEqual(self.backend.read_ha.await_count, count * (2 if retry_publication else 1))
                self.assertEqual(record_copies, [1] * (3 * count))
                self.assertEqual(committed.call_count, 3)
                self.assertEqual(fsync.call_count, 6)
                phases = ("verified", "verified", "active") if retry_publication else ("verified", "active", "active")
                self.assertEqual(snapshots, [dict.fromkeys(records, phase) for phase in phases])
            restored = await Vault(self.hass).load()
            self.assertEqual(set(snapshot), set(records))
            for key, record in restored.records().items():
                self.assertEqual(record["phase"], "active")
                self.assertTrue(record["commissioned"])
                state = snapshot[key]
                self.assertEqual(state, restored.data["states"][key])
                self.assertEqual(state["status"], "healthy")
                self.assertEqual((state["auth_since"], state["auth_failures"]), (None, 0))
                self.assertEqual((state["last_error_type"], state["last_error_location"]), (None, None))
                self.assertEqual((state["attempts"], state["publication_attempts"]), ([], []))
                self.assertLessEqual(state["last_checked"], state["last_verified"])
                self.assertLessEqual(state["last_verified"], state["last_completed"])
                await runtime.verify_publication(record["target"])
            self.assertEqual(runtime.pending_tasks, {})
            self.backend.restore_missing.assert_not_awaited()
            self.backend.read_ha.reset_mock()
            self.backend.read_ha.return_value = {"temperature": 22, "humidity": 46}
            await runtime.update()
            self.assertEqual(self.backend.read_ha.await_count, count)
            self.assertTrue(all(runtime.readings[key][1]["temperature"] == 22 for key in records))
        finally:
            await platform.async_reset()

    async def test_activation_reports_must_still_be_fresh_when_peer_check_finishes(self):
        for changed in ("removed", "expired", "authentication_due"):
            with self.subTest(changed=changed):
                config, runtime = await self.make_runtime(recovery=False)
                peer = make_owned("disposable-peer", self.target["accessory_key"], {})
                peer["recovery"] = False
                peer_key = peer["entry_id"]
                peer_record = {"target": peer, "phase": "active"}
                await self.vault.put(peer_key, peer_record)
                runtime.records[peer_key] = peer_record
                runtime.sync_targets()
                platform = fixtures.sensor_platform(self.hass, config)
                temperature, humidity = [
                    sensor.Measurement(runtime, self.target, kind) for kind in ("temperature", "humidity")
                ]
                await platform.async_add_entities([temperature, humidity])
                committed, release = asyncio.Event(), asyncio.Event()
                reads = []
                checkpoint = self.vault.checkpoint
                elapsed = 0

                async def commit_record(key, record, **kwargs):
                    saved = await checkpoint(key, record, **kwargs)
                    if saved and key == self.key and record["phase"] == "active":
                        committed.set()
                    return saved

                async def read(target):
                    if target["entry_id"] == peer_key:
                        await release.wait()
                        raise OSError()
                    reads.append(target["entry_id"])
                    return {"temperature": 20 + len(reads), "humidity": 45}

                self.backend.read_ha.side_effect = read
                with (
                    patch.object(self.vault, "checkpoint", side_effect=commit_record),
                    patch.object(coordinator, "monotonic", lambda: elapsed),
                ):
                    poll = asyncio.create_task(runtime.update())
                    try:
                        await asyncio.wait_for(committed.wait(), 2)
                        await runtime.verify_publication(self.target)
                        if changed == "expired":
                            timestamp, values = runtime.readings[self.key]
                            runtime.readings[self.key] = (timestamp - timedelta(seconds=2 * INTERVAL + 1), values)
                        elif changed == "removed":
                            await humidity.async_remove()
                        else:
                            elapsed = INTERVAL
                            self.assertTrue(temperature.available)
                            self.assertTrue(humidity.available)
                        release.set()
                        snapshot = await asyncio.wait_for(poll, 2)
                        self.assertEqual(reads, [self.key, self.key])
                        self.assertEqual(
                            snapshot[self.key]["status"],
                            "publication_unavailable" if changed == "removed" else "healthy",
                        )
                        self.assertEqual(runtime.readings[self.key][1]["temperature"], 22)
                    finally:
                        release.set()
                        await asyncio.gather(poll, return_exceptions=True)
                        await platform.async_reset()
                        await runtime.stop()

    async def test_actual_entity_reports_scale_linearly_and_stay_fresh(self):
        for count in (1, 16, 64):
            with self.subTest(devices=count):
                config, runtime = await self.make_runtime()
                for index in range(1, count):
                    target = make_owned(
                        f"disposable-{index}",
                        self.target["accessory_key"],
                        {"AccessoryIP": "192.0.2.1", "AccessoryPort": 12345},
                    )
                    runtime.records[target["entry_id"]] = {"target": target, "phase": "active"}
                runtime.sync_targets()
                runtime.coordinator.async_set_updated_data(runtime.engine.snapshot())
                platform = fixtures.sensor_platform(self.hass, config)
                entities = []
                for target in runtime.targets:
                    entities.extend(sensor.Measurement(runtime, target, kind) for kind in ("temperature", "humidity"))
                    entities.append(sensor.RecoveryStatus(runtime, target))
                reports = []
                original = DeviceSensor.async_write_ha_state

                def report(entity):
                    reports.append(entity.key)
                    original(entity)

                try:
                    with patch.object(DeviceSensor, "async_write_ha_state", report):
                        await platform.async_add_entities(entities)
                        reports.clear()
                        # Reproduce the previous per-sample global broadcast
                        # against the same actual entities for a count baseline.
                        for target in runtime.targets:
                            runtime.readings[target["entry_id"]] = (
                                dt_util.utcnow(),
                                {"temperature": 21, "humidity": 45},
                            )
                            runtime.coordinator.async_set_updated_data(runtime.engine.snapshot())
                        self.assertEqual(len(reports), 3 * count * count)
                        reports.clear()
                        with patch.object(runtime.engine, "snapshot", wraps=runtime.engine.snapshot) as snapshot:
                            for target in runtime.targets:
                                start = len(reports)
                                runtime.publish(target, {"temperature": 21, "humidity": 45})
                                self.assertEqual(reports[start:], [target["entry_id"]] * 2)
                                await runtime.verify_publication(target)
                            snapshot.assert_not_called()
                        self.assertEqual(len(reports), 2 * count)
                        # The final health/inventory broadcast is retained.
                        runtime.coordinator.async_set_updated_data(runtime.engine.snapshot())
                        self.assertEqual(len(reports), 5 * count)
                        missing = runtime.entities[self.key]["humidity"]
                        self.hass.states.async_remove(missing)
                        with self.assertRaisesRegex(OSError, "sensor_reports_not_fresh"):
                            await runtime.verify_publication(self.target)
                        await platform.async_reset()
                        self.assertEqual(runtime.measurement_listeners, {})
                        self.assertEqual(runtime.entities[self.key], {})
                finally:
                    await platform.async_reset()
                    await runtime.stop()

    async def test_removal_readd_cleans_notifications_and_platform_inventory(self):
        config, runtime = await self.make_runtime()
        add = Mock()
        unload = []
        peer = "unrelated-device"
        runtime.notifications[peer] = ("healthy",)
        try:
            with patch.object(type(config), "async_on_unload", side_effect=unload.append):
                await sensor.async_setup_entry(self.hass, config, add)
            for _ in range(4):
                runtime.notifications[self.key] = ("failed",)
                runtime.notifications["access_" + self.key] = ("rejected",)
                ids = [
                    "homepod_sensor_link_" + suffix
                    for suffix in (self.key, self.key + "_recovered", "access_" + self.key)
                ]
                for notification_id in ids:
                    persistent_notification.async_create(self.hass, "Disposable", notification_id=notification_id)
                with patch.object(
                    persistent_notification, "async_dismiss", wraps=persistent_notification.async_dismiss
                ) as dismiss:
                    await runtime.remove_device(self.key)
                    self.assertEqual({call.args[1] for call in dismiss.call_args_list}, set(ids))
                self.assertEqual(runtime.notifications, {peer: ("healthy",)})
                self.assertEqual(runtime.measurement_listeners, {})
                await self.vault.put(self.key, deepcopy(self.record))
                await runtime.accept_inventory(
                    {"device_keys": [self.key], "pins": {self.key: public_targets([self.target])[0]}}
                )
            self.assertEqual(add.call_count, 5)
            self.assertTrue(all(len(call.args[0]) == 3 for call in add.call_args_list))
        finally:
            for callback in unload:
                callback()
            await runtime.stop()

    async def test_listener_cleanup_cannot_remove_replacement_subscription(self):
        _, runtime = await self.make_runtime()
        old, replacement = Mock(), Mock()
        first = runtime.listen_measurements(self.key, old)
        second = runtime.listen_measurements(self.key, Mock())
        first()
        runtime.measurement_listeners.pop(self.key)
        remove_replacement = runtime.listen_measurements(self.key, replacement)
        second()
        runtime.publish(self.target, {"temperature": 20, "humidity": 40})
        replacement.assert_called_once()
        old.assert_not_called()
        remove_replacement()
        self.assertEqual(runtime.measurement_listeners, {})
        runtime.listen_measurements(self.key, replacement)
        await runtime.stop()
        self.assertEqual(runtime.measurement_listeners, {})

    async def test_real_platform_readds_removed_device_without_duplicate_listeners(self):
        config, runtime = await self.make_runtime()
        platform = fixtures.sensor_platform(self.hass, config)
        unload = []

        def add_entities(entities):
            self.hass.async_create_task(platform.async_add_entities(entities))

        try:
            with patch.object(type(config), "async_on_unload", side_effect=unload.append):
                await sensor.async_setup_entry(self.hass, config, add_entities)
            await self.hass.async_block_till_done()
            for _ in range(3):
                self.assertEqual(len(platform.entities), 3)
                self.assertEqual(len(runtime.measurement_listeners[self.key]), 2)
                runtime.publish(self.target, {"temperature": 21, "humidity": 45})
                await runtime.verify_publication(self.target)
                await runtime.remove_device(self.key)
                await self.hass.async_block_till_done()
                self.assertEqual(platform.entities, {})
                self.assertEqual(runtime.measurement_listeners, {})
                await self.vault.put(self.key, deepcopy(self.record))
                await runtime.accept_inventory(
                    {"device_keys": [self.key], "pins": {self.key: public_targets([self.target])[0]}}
                )
                await self.hass.async_block_till_done()
            self.assertEqual(len(platform.entities), 3)
            self.assertEqual(len(runtime.measurement_listeners[self.key]), 2)
        finally:
            for callback in unload:
                callback()
            await platform.async_reset()
            await runtime.stop()
