"""Operation-count benchmarks with real HA reports and device churn."""

from copy import deepcopy
from datetime import timedelta
import logging
import unittest
from unittest.mock import Mock, patch

from homeassistant.components import persistent_notification
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.util import dt as dt_util

from custom_components.homepod_sensor_link import sensor
from custom_components.homepod_sensor_link.credentials import DOMAIN, public_targets
from custom_components.homepod_sensor_link.entity import DeviceSensor
from custom_components.homepod_sensor_link.provisioning import make_owned
import test_onboarding_v2 as fixtures


class PublicationPerformanceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.OnboardingTests.asyncSetUp
    asyncTearDown = fixtures.OnboardingTests.asyncTearDown
    make_runtime = fixtures.OnboardingTests.make_runtime

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
                                runtime.publish(target, {"temperature": 22, "humidity": 46})
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
